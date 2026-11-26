#!/usr/bin/env python3
"""FlightLink 1.0 local archive auto-save & incremental sync client (reference implementation).

Implements all requirements of the FlightLink 1.0 approved incremental change:
1. Default automatic saving of every sealed FC session:
   - Router systemd log (`router.log`)
   - MAVProxy session log (`mavproxy.log`)
   - Session metadata (`metadata.json`)
   Windows default directory: `<System Documents>/FlightLink/Logs` resolved locally via OS API;
   ECS NEVER dictates client filesystem paths.
2. Polls `GET /api/v1/archive/changes` (sealed metadata only, no file bodies) and downloads
   files via `GET /api/v1/archive/sessions/{session_id}/files/{file_id}`.
3. Unified task model for auto-download and manual retry, deduplicated by
   `(ecs_instance_id, session_id, source)`. Downloads to `.part` temp file, verifies byte size
   and SHA-256, then atomically renames (`os.replace`). Verified files are NEVER re-downloaded.
4. Persists discovered pending tasks locally BEFORE advancing `next_cursor`, so one failed
   session never blocks later sessions and never gets dropped by cursor advancement.
5. Strictly distinguishes:
   - `saved`: all available files verified on disk, zero server-side source gaps
   - `saved_with_source_gaps`: all available files verified, but cloud itself lacked a source
     (`missing_on_server` / `degraded_quota_exceeded`) -> NOT marked fully saved, NOT retried
   - `failed`: client auto-download failed after retries -> triggers UI failure notice +
     「重新下载日志」 jumping to `#/history/{group_id}?slice={session_id}`
   - `expired_on_server`: cloud returned `410 ARCHIVE_EXPIRED` -> stops retrying, never loops
   Distinguishes error causes: `network_error`, `disk_permission_denied`, `disk_full`,
   `cloud_expired`, `checksum_mismatch`.
6. Local client is the sole authority for save status (never inferred from HTTP 200 by ECS).
7. Cloud 7x24h expiry NEVER deletes locally saved files; offline past retention (`410 CURSOR_EXPIRED`)
   reports a retention-window gap and reconciles from an empty cursor.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import platform
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import httpx


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_default_logs_root() -> Path:
    """Resolve OS-specific default local save directory (NEVER dictated by ECS).

    - Windows: System "Documents" known folder -> `FlightLink/Logs`
    - macOS/Linux: `~/Documents/FlightLink/Logs` (or XDG_DOCUMENTS_DIR)
    """
    system = platform.system()
    if system == "Windows":
        try:
            # FOLDERID_Documents = {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", ctypes.c_ulong),
                    ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            u = UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")
            guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            path_ptr = ctypes.c_wchar_p()
            hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
            if hr == 0 and path_ptr.value:
                docs = Path(path_ptr.value)
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                return docs / "FlightLink" / "Logs"
        except Exception:
            pass
        return Path.home() / "Documents" / "FlightLink" / "Logs"

    xdg_docs = os.environ.get("XDG_DOCUMENTS_DIR")
    if xdg_docs:
        return Path(xdg_docs).expanduser() / "FlightLink" / "Logs"
    return Path.home() / "Documents" / "FlightLink" / "Logs"


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
    return h.hexdigest(), total


LOCAL_STATE_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sync_cursors (
    ecs_instance_id TEXT PRIMARY KEY,
    cursor TEXT,
    last_sync_at TEXT,
    retention_gap_detected INTEGER NOT NULL DEFAULT 0,
    retention_gap_note TEXT
);

CREATE TABLE IF NOT EXISTS local_sessions (
    ecs_instance_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    group_name_snapshot TEXT NOT NULL,
    group_deleted INTEGER NOT NULL DEFAULT 0,
    ended_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    close_reason TEXT NOT NULL,
    local_status TEXT NOT NULL CHECK (local_status IN (
        'pending', 'downloading', 'saved', 'saved_with_source_gaps', 'failed', 'expired_on_server'
    )),
    last_error_category TEXT,
    last_error_message TEXT,
    show_retry_action INTEGER NOT NULL DEFAULT 0,
    retry_target_route TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ecs_instance_id, session_id)
);

CREATE TABLE IF NOT EXISTS local_files (
    ecs_instance_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('router', 'mavproxy', 'metadata')),
    file_id TEXT,
    logical_name TEXT NOT NULL,
    server_availability TEXT NOT NULL,
    server_missing_reason TEXT,
    expected_size INTEGER,
    expected_sha256 TEXT,
    local_path TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'verified', 'missing_on_server', 'failed', 'expired_on_server'
    )),
    error_category TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ecs_instance_id, session_id, source),
    FOREIGN KEY (ecs_instance_id, session_id) REFERENCES local_sessions(ecs_instance_id, session_id) ON DELETE CASCADE
);
"""


@dataclass
class SyncCycleSummary:
    discovered_sessions: int = 0
    files_downloaded: int = 0
    files_skipped_verified: int = 0
    files_failed: int = 0
    files_expired: int = 0
    retention_gap_detected: bool = False
    retention_gap_note: str | None = None


def classify_os_error(exc: OSError) -> tuple[str, str]:
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return "disk_permission_denied", f"Local directory write permission denied: {exc}"
    if exc.errno == errno.ENOSPC:
        return "disk_full", f"Insufficient local disk space: {exc}"
    return "disk_io_error", f"Local disk I/O error: {exc}"


class ArchiveAutoSaveClient:
    """Durable local archive auto-save and manual-retry synchronization engine."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        logs_root: Path | None = None,
        state_db_path: Path | None = None,
        http_client: httpx.Client | None = None,
        max_auto_attempts: int = 2,
        write_hook: Callable[[Path, bytes], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.logs_root = Path(logs_root) if logs_root is not None else resolve_default_logs_root()
        self.state_db_path = Path(state_db_path) if state_db_path is not None else (self.logs_root / ".flightlink-sync.sqlite3")
        self.max_auto_attempts = max(1, max_auto_attempts)
        self._owns_client = http_client is None
        self.client = http_client or httpx.Client(base_url=self.base_url, timeout=30.0)
        # Optional test hook for simulating disk permission / ENOSPC failures cleanly
        self._write_hook = write_hook
        self._init_state_db()

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _connect(self) -> sqlite3.Connection:
        self.state_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.state_db_path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_state_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(LOCAL_STATE_SCHEMA)
        finally:
            conn.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer_token}"}

    def _get_saved_cursor(self) -> tuple[str | None, str | None]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT ecs_instance_id, cursor FROM sync_cursors ORDER BY last_sync_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None, None
            return str(row["ecs_instance_id"]), row["cursor"]
        finally:
            conn.close()

    def _persist_discovered_batch_and_advance_cursor(
        self,
        *,
        ecs_instance_id: str,
        items: list[dict[str, Any]],
        next_cursor: str | None,
        retention_gap_detected: bool = False,
        retention_gap_note: str | None = None,
    ) -> int:
        """Persist ALL discovered pending tasks locally BEFORE advancing `next_cursor`."""
        now_iso = utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for item in items:
                session_id = str(item["session_id"])
                group_id = str(item["group_id"])
                existing_sess = conn.execute(
                    "SELECT local_status FROM local_sessions WHERE ecs_instance_id = ? AND session_id = ?",
                    (ecs_instance_id, session_id),
                ).fetchone()
                if existing_sess is None:
                    conn.execute(
                        """
                        INSERT INTO local_sessions(
                            ecs_instance_id, session_id, group_id, group_name_snapshot,
                            group_deleted, ended_at, expires_at, close_reason,
                            local_status, show_retry_action, retry_target_route, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                        """,
                        (
                            ecs_instance_id,
                            session_id,
                            group_id,
                            str(item["group_name_snapshot"]),
                            1 if item.get("group_deleted") else 0,
                            str(item["ended_at"]),
                            str(item["expires_at"]),
                            str(item["close_reason"]),
                            f"#/history/{group_id}?slice={session_id}",
                            now_iso,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE local_sessions
                        SET group_deleted = ?, group_name_snapshot = ?, updated_at = ?
                        WHERE ecs_instance_id = ? AND session_id = ?
                        """,
                        (
                            1 if item.get("group_deleted") else 0,
                            str(item["group_name_snapshot"]),
                            now_iso,
                            ecs_instance_id,
                            session_id,
                        ),
                    )

                for fdesc in item.get("files", []):
                    src = str(fdesc["source"])
                    avail = str(fdesc["availability"])
                    existing_f = conn.execute(
                        "SELECT status, local_path FROM local_files WHERE ecs_instance_id = ? AND session_id = ? AND source = ?",
                        (ecs_instance_id, session_id, src),
                    ).fetchone()
                    if existing_f is not None and existing_f["status"] == "verified":
                        continue  # Already verified locally -> never overwrite or re-queue

                    if avail in ("available", "empty") and fdesc.get("file_id"):
                        init_status = "pending"
                        err_cat = None
                        err_msg = None
                    elif avail == "degraded_quota_exceeded" and fdesc.get("file_id"):
                        # Partial/degraded file exists on server -> still download it, while retaining gap info
                        init_status = "pending"
                        err_cat = None
                        err_msg = None
                    else:
                        # Cloud itself lacks this log source -> NEVER treat as download failure or retry!
                        init_status = "missing_on_server"
                        err_cat = "cloud_source_missing"
                        err_msg = str(fdesc.get("missing_reason") or "missing_on_server")

                    conn.execute(
                        """
                        INSERT INTO local_files(
                            ecs_instance_id, session_id, source, file_id, logical_name,
                            server_availability, server_missing_reason, expected_size, expected_sha256,
                            status, error_category, error_message, attempt_count, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                        ON CONFLICT(ecs_instance_id, session_id, source) DO UPDATE SET
                            file_id = COALESCE(excluded.file_id, local_files.file_id),
                            server_availability = excluded.server_availability,
                            server_missing_reason = excluded.server_missing_reason,
                            expected_size = excluded.expected_size,
                            expected_sha256 = excluded.expected_sha256,
                            updated_at = excluded.updated_at
                        WHERE local_files.status != 'verified'
                        """,
                        (
                            ecs_instance_id,
                            session_id,
                            src,
                            fdesc.get("file_id"),
                            str(fdesc["logical_name"]),
                            avail,
                            fdesc.get("missing_reason"),
                            fdesc.get("size_bytes"),
                            fdesc.get("sha256"),
                            init_status,
                            err_cat,
                            err_msg,
                            now_iso,
                        ),
                    )

            # ONLY AFTER all pending tasks are durably persisted -> advance cursor!
            conn.execute(
                """
                INSERT INTO sync_cursors(ecs_instance_id, cursor, last_sync_at, retention_gap_detected, retention_gap_note)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ecs_instance_id) DO UPDATE SET
                    cursor = excluded.cursor,
                    last_sync_at = excluded.last_sync_at,
                    retention_gap_detected = MAX(sync_cursors.retention_gap_detected, excluded.retention_gap_detected),
                    retention_gap_note = COALESCE(excluded.retention_gap_note, sync_cursors.retention_gap_note)
                """,
                (
                    ecs_instance_id,
                    next_cursor,
                    now_iso,
                    1 if retention_gap_detected else 0,
                    retention_gap_note,
                ),
            )
            conn.execute("COMMIT")
            return len(items)
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def poll_manifest(self) -> tuple[int, bool, str | None]:
        """Discover newly sealed sessions from `/api/v1/archive/changes` without downloading bodies."""
        _, cursor = self._get_saved_cursor()
        discovered = 0
        retention_gap = False
        retention_note: str | None = None

        while True:
            params: dict[str, Any] = {"limit": 50}
            if cursor:
                params["cursor"] = cursor
            resp = self.client.get("/api/v1/archive/changes", params=params, headers=self._headers())
            if resp.status_code == 410:
                body = resp.json()
                code = body.get("error", {}).get("code")
                if code == "CURSOR_EXPIRED":
                    # Client was offline past cloud 7-day retention window -> record gap & reconcile from empty cursor!
                    retention_gap = True
                    retention_note = (
                        "Client offline past cloud 7-day retention window; earlier sealed sessions may have expired before sync."
                    )
                    cursor = None
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            data = resp.json()
            ecs_id = str(data["ecs_instance_id"])
            items = list(data.get("items", []))
            next_cursor = data.get("next_cursor")

            discovered += self._persist_discovered_batch_and_advance_cursor(
                ecs_instance_id=ecs_id,
                items=items,
                next_cursor=next_cursor,
                retention_gap_detected=retention_gap,
                retention_gap_note=retention_note,
            )
            cursor = next_cursor
            if not data.get("has_more"):
                break
        return discovered, retention_gap, retention_note

    def _target_path(self, ecs_instance_id: str, group_id: str, session_id: str, logical_name: str) -> Path:
        return self.logs_root / ecs_instance_id / group_id / session_id / logical_name

    def _is_already_verified_on_disk(self, row: sqlite3.Row) -> bool:
        if row["status"] != "verified" or not row["local_path"]:
            return False
        path = Path(str(row["local_path"]))
        if not path.exists() or not path.is_file():
            return False
        exp_sha = row["expected_sha256"]
        exp_size = row["expected_size"]
        if exp_sha:
            got_sha, got_size = sha256_file(path)
            if got_sha != exp_sha or (exp_size is not None and got_size != int(exp_size)):
                return False
        return True

    def _recompute_session_status(self, conn: sqlite3.Connection, ecs_instance_id: str, session_id: str) -> None:
        files = conn.execute(
            "SELECT * FROM local_files WHERE ecs_instance_id = ? AND session_id = ?",
            (ecs_instance_id, session_id),
        ).fetchall()
        now_iso = utc_now_iso()

        has_pending = any(f["status"] == "pending" for f in files)
        failed_files = [f for f in files if f["status"] == "failed"]
        expired_files = [f for f in files if f["status"] == "expired_on_server"]
        server_gaps = [
            f
            for f in files
            if f["status"] == "missing_on_server"
            or f["server_availability"] in ("missing_on_server", "degraded_quota_exceeded")
        ]

        if failed_files:
            first = failed_files[0]
            local_status = "failed"
            show_retry = 1
            err_cat = first["error_category"]
            err_msg = first["error_message"]
        elif expired_files:
            first = expired_files[0]
            local_status = "expired_on_server"
            show_retry = 0  # Do NOT loop retry expired cloud files!
            err_cat = "cloud_expired"
            err_msg = first["error_message"] or "Expired on cloud after 7-day retention window"
        elif has_pending:
            local_status = "pending"
            show_retry = 0
            err_cat = None
            err_msg = None
        elif server_gaps:
            missing_desc = ", ".join(f"{f['source']}({f['server_missing_reason'] or f['server_availability']})" for f in server_gaps)
            local_status = "saved_with_source_gaps"
            show_retry = 0  # Cloud itself lacked this source -> NOT a client download failure, do NOT prompt retry!
            err_cat = "cloud_source_missing"
            err_msg = f"Cloud archive lacked source(s): {missing_desc}"
        else:
            local_status = "saved"
            show_retry = 0
            err_cat = None
            err_msg = None

        conn.execute(
            """
            UPDATE local_sessions
            SET local_status = ?, last_error_category = ?, last_error_message = ?,
                show_retry_action = ?, updated_at = ?
            WHERE ecs_instance_id = ? AND session_id = ?
            """,
            (local_status, err_cat, err_msg, show_retry, now_iso, ecs_instance_id, session_id),
        )

    def _sync_single_session(
        self,
        *,
        ecs_instance_id: str,
        session_id: str,
        summary: SyncCycleSummary,
        manual_retry: bool,
    ) -> None:
        conn = self._connect()
        try:
            sess = conn.execute(
                "SELECT * FROM local_sessions WHERE ecs_instance_id = ? AND session_id = ?",
                (ecs_instance_id, session_id),
            ).fetchone()
            if sess is None:
                return
            group_id = str(sess["group_id"])
            files = conn.execute(
                "SELECT * FROM local_files WHERE ecs_instance_id = ? AND session_id = ? ORDER BY source ASC",
                (ecs_instance_id, session_id),
            ).fetchall()
        finally:
            conn.close()

        for frow in files:
            # Skip cloud-missing or cloud-expired files permanently!
            if frow["status"] in ("missing_on_server", "expired_on_server"):
                continue

            # Skip already verified files on disk (never re-download!)
            if self._is_already_verified_on_disk(frow):
                summary.files_skipped_verified += 1
                continue

            if not manual_retry and frow["status"] == "failed" and int(frow["attempt_count"]) >= self.max_auto_attempts:
                continue

            file_id = frow["file_id"]
            if not file_id:
                continue

            dest_path = self._target_path(ecs_instance_id, group_id, session_id, str(frow["logical_name"]))
            max_tries = 1 if manual_retry else max(1, self.max_auto_attempts - int(frow["attempt_count"]))

            for _ in range(max_tries):
                ok, terminal = self._download_and_verify_one(
                    ecs_instance_id=ecs_instance_id,
                    session_id=session_id,
                    source=str(frow["source"]),
                    file_id=str(file_id),
                    expected_size=frow["expected_size"],
                    expected_sha256=frow["expected_sha256"],
                    dest_path=dest_path,
                    summary=summary,
                )
                if ok or terminal:
                    break

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._recompute_session_status(conn, ecs_instance_id, session_id)
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _download_and_verify_one(
        self,
        *,
        ecs_instance_id: str,
        session_id: str,
        source: str,
        file_id: str,
        expected_size: int | None,
        expected_sha256: str | None,
        dest_path: Path,
        summary: SyncCycleSummary,
    ) -> tuple[bool, bool]:
        """Download to temp `.part` file, verify size+SHA-256, atomically rename.

        Returns `(succeeded, is_terminal_stop)`.
        """
        now_iso = utc_now_iso()
        url = f"/api/v1/archive/sessions/{session_id}/files/{file_id}"
        try:
            resp = self.client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            self._record_file_error(
                ecs_instance_id, session_id, source,
                status="failed", error_category="network_error",
                error_message=f"Network error downloading {source}: {exc}",
            )
            summary.files_failed += 1
            return False, False

        if resp.status_code == 410:
            self._record_file_error(
                ecs_instance_id, session_id, source,
                status="expired_on_server", error_category="cloud_expired",
                error_message="Cloud archive exceeded 7-day retention window (HTTP 410 ARCHIVE_EXPIRED).",
            )
            summary.files_expired += 1
            return False, True  # Terminal: NEVER loop retry expired cloud files!

        if resp.status_code == 404:
            code = ""
            try:
                code = str(resp.json().get("error", {}).get("code", ""))
            except Exception:
                pass
            if code == "ARCHIVE_SOURCE_MISSING":
                self._record_file_error(
                    ecs_instance_id, session_id, source,
                    status="missing_on_server", error_category="cloud_source_missing",
                    error_message="Cloud archive lacks this log source (HTTP 404 ARCHIVE_SOURCE_MISSING).",
                )
                return False, True
            self._record_file_error(
                ecs_instance_id, session_id, source,
                status="failed", error_category="server_not_found",
                error_message=f"Server returned 404 ({code}) for {source}.",
            )
            summary.files_failed += 1
            return False, False

        if resp.status_code >= 500 or resp.status_code != 200:
            self._record_file_error(
                ecs_instance_id, session_id, source,
                status="failed", error_category="network_error",
                error_message=f"Unexpected HTTP {resp.status_code} downloading {source}.",
            )
            summary.files_failed += 1
            return False, False

        # HTTP 200 is NOT yet "saved"! Must write to temp file, verify SHA-256, and atomic rename.
        content = resp.content
        got_sha = hashlib.sha256(content).hexdigest()
        got_size = len(content)
        header_sha = resp.headers.get("X-Content-SHA256")
        ref_sha = expected_sha256 or header_sha

        if (expected_size is not None and got_size != int(expected_size)) or (ref_sha and got_sha != ref_sha):
            self._record_file_error(
                ecs_instance_id, session_id, source,
                status="failed", error_category="checksum_mismatch",
                error_message=f"Checksum/size mismatch for {source}: got size={got_size} sha={got_sha}",
            )
            summary.files_failed += 1
            return False, False

        tmp_path = dest_path.parent / f".{dest_path.name}.part"
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if self._write_hook is not None:
                self._write_hook(tmp_path, content)
            else:
                with open(tmp_path, "wb") as fh:
                    fh.write(content)
                    fh.flush()
                    os.fsync(fh.fileno())
            # Verify written temp file before atomic rename
            disk_sha, disk_size = sha256_file(tmp_path)
            if disk_sha != got_sha or disk_size != got_size:
                raise OSError("Written temp file verification failed")
            os.replace(tmp_path, dest_path)
        except OSError as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            cat, msg = classify_os_error(exc)
            self._record_file_error(
                ecs_instance_id, session_id, source,
                status="failed", error_category=cat, error_message=msg,
            )
            summary.files_failed += 1
            # Local permission / disk-full errors are terminal for this auto cycle (surface specific reason immediately)
            return False, True

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE local_files
                SET local_path = ?, status = 'verified', error_category = NULL, error_message = NULL,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE ecs_instance_id = ? AND session_id = ? AND source = ?
                """,
                (str(dest_path), now_iso, ecs_instance_id, session_id, source),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        summary.files_downloaded += 1
        return True, True

    def _record_file_error(
        self,
        ecs_instance_id: str,
        session_id: str,
        source: str,
        *,
        status: str,
        error_category: str,
        error_message: str,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE local_files
                SET status = ?, error_category = ?, error_message = ?,
                    attempt_count = attempt_count + 1, updated_at = ?
                WHERE ecs_instance_id = ? AND session_id = ? AND source = ?
                """,
                (status, error_category, error_message, utc_now_iso(), ecs_instance_id, session_id, source),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def sync_once(self) -> SyncCycleSummary:
        """Run one full discovery + non-blocking download pass."""
        summary = SyncCycleSummary()
        discovered, gap, note = self.poll_manifest()
        summary.discovered_sessions = discovered
        summary.retention_gap_detected = gap
        summary.retention_gap_note = note

        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT ecs_instance_id, session_id
                FROM local_sessions
                WHERE local_status IN ('pending', 'downloading', 'failed')
                ORDER BY ended_at ASC
                """
            ).fetchall()
        finally:
            conn.close()

        for r in rows:
            # Independent per-session processing: one failed session NEVER blocks later sessions!
            self._sync_single_session(
                ecs_instance_id=str(r["ecs_instance_id"]),
                session_id=str(r["session_id"]),
                summary=summary,
                manual_retry=False,
            )
        return summary

    def retry_session(self, session_id: str, *, ecs_instance_id: str | None = None) -> SyncCycleSummary:
        """Manual retry ("重新下载日志") reusing the exact same sync task — downloads ONLY missing files."""
        summary = SyncCycleSummary()
        conn = self._connect()
        try:
            if ecs_instance_id:
                row = conn.execute(
                    "SELECT ecs_instance_id FROM local_sessions WHERE ecs_instance_id = ? AND session_id = ?",
                    (ecs_instance_id, session_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT ecs_instance_id FROM local_sessions WHERE session_id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
        finally:
            conn.close()

        if row is None:
            return summary
        self._sync_single_session(
            ecs_instance_id=str(row["ecs_instance_id"]),
            session_id=session_id,
            summary=summary,
            manual_retry=True,
        )
        return summary

    def get_session_state(self, session_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            sess = conn.execute(
                "SELECT * FROM local_sessions WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            if sess is None:
                return None
            files = conn.execute(
                "SELECT * FROM local_files WHERE ecs_instance_id = ? AND session_id = ? ORDER BY source ASC",
                (sess["ecs_instance_id"], session_id),
            ).fetchall()
            return {
                **dict(sess),
                "show_retry_action": bool(sess["show_retry_action"]),
                "group_deleted": bool(sess["group_deleted"]),
                "files": [dict(f) for f in files],
            }
        finally:
            conn.close()

    def get_cursor_state(self, ecs_instance_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sync_cursors WHERE ecs_instance_id = ?",
                (ecs_instance_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="FlightLink 1.0 local archive auto-save client")
    parser.add_argument("--base-url", required=True, help="ECS API base URL, e.g. https://ecs.example.com")
    parser.add_argument("--token", required=True, help="Bearer JWT or revocable viewer API token (flk_...)")
    parser.add_argument("--logs-root", type=Path, default=None, help="Override OS-default logs directory")
    parser.add_argument("--interval", type=int, default=0, help="Poll interval in seconds (0 = run once)")
    parser.add_argument("--retry-session", default=None, help="Manually retry missing files for one session ID")
    args = parser.parse_args()

    sync = ArchiveAutoSaveClient(
        base_url=args.base_url,
        bearer_token=args.token,
        logs_root=args.logs_root,
    )
    try:
        if args.retry_session:
            res = sync.retry_session(args.retry_session)
            print(f"[retry] session={args.retry_session} downloaded={res.files_downloaded} skipped={res.files_skipped_verified} failed={res.files_failed} expired={res.files_expired}")
            return 0 if res.files_failed == 0 else 1

        while True:
            res = sync.sync_once()
            print(
                f"[sync] root={sync.logs_root} discovered={res.discovered_sessions} "
                f"downloaded={res.files_downloaded} skipped={res.files_skipped_verified} "
                f"failed={res.files_failed} expired={res.files_expired} gap={res.retention_gap_detected}"
            )
            if res.retention_gap_detected and res.retention_gap_note:
                print(f"[warning] {res.retention_gap_note}", file=sys.stderr)
            if args.interval <= 0:
                break
            time.sleep(args.interval)
        return 0
    finally:
        sync.close()


if __name__ == "__main__":
    raise SystemExit(main())
