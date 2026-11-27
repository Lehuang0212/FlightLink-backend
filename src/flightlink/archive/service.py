"""Session archiving engine (`flightlink-archive`).

Implements P4 + approved incremental changes:
1. Online detection ONLY from valid DTU-inbound FC HEARTBEAT (excludes IMEI, text 'heart',
   GCS/MAVProxy own heartbeats). Configurable ~1Hz heartbeat & 15s offline grace.
2. Continuous disk writes to `<archive_dir>/<group_id>/<session_id>/` (`router.log`,
   `mavproxy.log`, `metadata.json`), persisted ingestion cursors, deduplication by stable
   `event_id`, explicit gap tracking (API restart never splits sessions; observer drops
   record gaps rather than faking physical shutdown).
3. At most one active session per group; atomic crash-recoverable sealing; index NEVER
   marks half-written files complete; `change_seq` assigned strictly on seal completion so
   `/archive/changes` emits ONLY sealed sessions.
4. Explicit source availability (`available` vs `missing_on_server` vs `degraded_quota_exceeded`)
   so local sync clients distinguish missing cloud source from client download failure.
5. 7x24h retention from `ended_at`; active sessions never pruned; download lease prevents
   pruning racing an active download; group deletion seals active session with
   `admin_group_deleted` while preserving existing sessions' original `expires_at`.
6. Single-disk quota protection: degrades/stops log writes and records gaps without silently
   early-deleting archives, mutating comm config, or blocking `stop` operations.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from flightlink.archive.storage import (
    append_log_line,
    file_sha256_and_size,
    safe_rmtree,
    write_json_atomic,
)
from flightlink.clock import Clock, SystemClock, format_rfc3339, parse_rfc3339
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.errors import ErrorDetailItem, GoneError, NotFoundError
from flightlink.ids import new_event_id, new_file_id, new_session_id, new_token_id
from flightlink.schemas.archive import (
    ArchiveArtifactDescriptor,
    ArchiveChangeItem,
    ArchiveChangesResponse,
    ArchiveEntryPageResponse,
    ArchiveGap,
    ArchiveGroupListResponse,
    ArchiveGroupSummary,
    ArchiveSessionDetail,
    ArchiveSessionListResponse,
    ArchiveSessionSummary,
    ArchiveSourceAvailability,
    SessionCloseReason,
)
from flightlink.schemas.logs import LiveLogPageResponse, LogEntry, LogLevel, LogSource


class ArchiveService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        clock: Clock | None = None,
        free_bytes_override: int | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock or db.clock or SystemClock()
        # Optional override for deterministic single-disk quota tests
        self.free_bytes_override: int | None = free_bytes_override
        self.settings.resolved_archive_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Disk Quota Inspection (never early-deletes or mutates comm config)
    # ------------------------------------------------------------------
    def _disk_quota_exceeded(self) -> tuple[bool, str | None]:
        archive_dir = self.settings.resolved_archive_dir
        if self.free_bytes_override is not None:
            free_bytes = self.free_bytes_override
        else:
            probe = archive_dir if archive_dir.exists() else archive_dir.parent
            try:
                free_bytes = int(shutil.disk_usage(probe).free)
            except OSError:
                free_bytes = self.settings.disk_min_free_bytes + 1

        if free_bytes <= self.settings.disk_min_free_bytes:
            return True, f"free_bytes={free_bytes} <= min_free_bytes={self.settings.disk_min_free_bytes}"

        used = 0
        if archive_dir.exists():
            for p in archive_dir.rglob("*"):
                if p.is_file():
                    try:
                        used += p.stat().st_size
                    except OSError:
                        pass
        if used >= self.settings.archive_max_bytes:
            return True, f"archive_used_bytes={used} >= archive_max_bytes={self.settings.archive_max_bytes}"
        return False, None

    # ------------------------------------------------------------------
    # Collector Cursor Persistence
    # ------------------------------------------------------------------
    def get_collector_cursor(self, group_id: str, source: LogSource) -> str | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT cursor_value FROM archive_cursors WHERE group_id = ? AND source = ?",
                (group_id, source),
            ).fetchone()
            return str(row["cursor_value"]) if row else None

    def set_collector_cursor(self, group_id: str, source: LogSource, cursor_value: str) -> None:
        with self.db.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO archive_cursors(group_id, source, cursor_value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, source) DO UPDATE SET
                    cursor_value = excluded.cursor_value,
                    updated_at = excluded.updated_at
                """,
                (group_id, source, cursor_value, self.clock.now_iso()),
            )

    # ------------------------------------------------------------------
    # FC Heartbeat Online Detection & Continuous Session Ingestion
    # ------------------------------------------------------------------
    def record_valid_fc_heartbeat(
        self,
        *,
        group_id: str,
        sysid: int,
        dtu_peer: str | None = None,
    ) -> str:
        """Open or refresh the single active session for `group_id` upon valid DTU-inbound FC heartbeat."""
        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            group = conn.execute(
                "SELECT * FROM port_groups WHERE group_id = ? AND deleted_at IS NULL",
                (group_id,),
            ).fetchone()
            if group is None:
                raise NotFoundError("GROUP_NOT_FOUND", f"Port group '{group_id}' not found.")

            # Ensure archive_groups snapshot exists & stays synced
            conn.execute(
                """
                INSERT INTO archive_groups(
                    group_id, name_snapshot, dtu_protocol, dtu_port_snapshot,
                    gcs_protocol_snapshot, gcs_port_snapshot, deleted, created_at, updated_at
                ) VALUES (?, ?, 'udp', ?, ?, ?, 0, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    name_snapshot = excluded.name_snapshot,
                    dtu_port_snapshot = excluded.dtu_port_snapshot,
                    gcs_protocol_snapshot = excluded.gcs_protocol_snapshot,
                    gcs_port_snapshot = excluded.gcs_port_snapshot,
                    updated_at = excluded.updated_at
                """,
                (
                    group_id,
                    group["name"],
                    group["dtu_port"],
                    group["gcs_protocol"],
                    group["gcs_port"],
                    now_iso,
                    now_iso,
                ),
            )

            active = conn.execute(
                "SELECT session_id FROM archive_sessions WHERE group_id = ? AND status = 'active' AND purged_at IS NULL",
                (group_id,),
            ).fetchone()
            if active is not None:
                session_id = str(active["session_id"])
                conn.execute(
                    """
                    UPDATE archive_sessions
                    SET last_heartbeat_at = ?, sysid = COALESCE(sysid, ?),
                        group_name_snapshot = ?
                    WHERE session_id = ?
                    """,
                    (now_iso, sysid, group["name"], session_id),
                )
            else:
                session_id = new_session_id()
                dir_rel = f"{group_id}/{session_id}"
                (self.settings.resolved_archive_dir / dir_rel).mkdir(parents=True, exist_ok=True)
                conn.execute(
                    """
                    INSERT INTO archive_sessions(
                        session_id, group_id, group_name_snapshot, dtu_port_snapshot,
                        gcs_protocol_snapshot, gcs_port_snapshot, status, sysid,
                        first_observed_at, last_heartbeat_at, dir_relpath
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        group_id,
                        group["name"],
                        group["dtu_port"],
                        group["gcs_protocol"],
                        group["gcs_port"],
                        sysid,
                        now_iso,
                        now_iso,
                        dir_rel,
                    ),
                )
                for src, fname, ctype in (
                    ("router", "router.log", "text/plain; charset=utf-8"),
                    ("mavproxy", "mavproxy.log", "text/plain; charset=utf-8"),
                    ("metadata", "metadata.json", "application/json"),
                ):
                    conn.execute(
                        """
                        INSERT INTO archive_files(
                            file_id, session_id, source, availability, logical_name, content_type, line_count
                        ) VALUES (?, ?, ?, 'in_progress', ?, ?, 0)
                        """,
                        (new_file_id(src), session_id, src, fname, ctype),
                    )

            conn.execute(
                """
                UPDATE group_runtime_status
                SET sysid = ?, fc_state = 'online', dtu_peer = COALESCE(?, dtu_peer),
                    last_activity_at = ?, observed_at = ?
                WHERE group_id = ?
                """,
                (sysid, dtu_peer, now_iso, now_iso, group_id),
            )
            return session_id

    def append_log_event(
        self,
        *,
        group_id: str,
        source: LogSource,
        level: LogLevel,
        text: str,
        event_id: str | None = None,
        ts: str | None = None,
        collector_cursor: str | None = None,
    ) -> bool:
        """Continuously write a log event into live buffer and active session disk file.

        Deduplicates by stable `event_id`. Enforces single-disk quota degradation without
        blocking channel control or silently deleting archives.
        Returns True if newly inserted, False if duplicate or degraded-skipped.
        """
        evt_id = event_id or new_event_id()
        ts_iso = ts or self.clock.now_iso()
        quota_exceeded, quota_reason = self._disk_quota_exceeded()

        with self.db.write_transaction() as conn:
            # Always record in live_logs (dedup by event_id)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO live_logs(event_id, group_id, source, level, ts, text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (evt_id, group_id, source, level, ts_iso, text),
            )
            inserted_live = cur.rowcount > 0

            # Trim live buffer if above cap and advance min valid seq
            cnt_row = conn.execute("SELECT COUNT(*) AS cnt, MIN(seq) AS min_seq FROM live_logs").fetchone()
            if cnt_row and int(cnt_row["cnt"]) > self.settings.live_log_buffer_max_rows:
                overflow = int(cnt_row["cnt"]) - self.settings.live_log_buffer_max_rows
                cutoff_row = conn.execute(
                    "SELECT seq FROM live_logs ORDER BY seq ASC LIMIT 1 OFFSET ?",
                    (overflow,),
                ).fetchone()
                if cutoff_row:
                    cutoff_seq = int(cutoff_row["seq"])
                    conn.execute("DELETE FROM live_logs WHERE seq < ?", (cutoff_seq,))
                    conn.execute(
                        "UPDATE meta_config SET value = ?, updated_at = ? WHERE key = 'live_log_min_valid_seq'",
                        (str(cutoff_seq), self.clock.now_iso()),
                    )

            if collector_cursor is not None:
                conn.execute(
                    """
                    INSERT INTO archive_cursors(group_id, source, cursor_value, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(group_id, source) DO UPDATE SET
                        cursor_value = excluded.cursor_value,
                        updated_at = excluded.updated_at
                    """,
                    (group_id, source, collector_cursor, self.clock.now_iso()),
                )

            active = conn.execute(
                "SELECT session_id, dir_relpath FROM archive_sessions WHERE group_id = ? AND status = 'active' AND purged_at IS NULL",
                (group_id,),
            ).fetchone()
            if active is None:
                return inserted_live

            session_id = str(active["session_id"])
            dir_rel = str(active["dir_relpath"])

            if quota_exceeded:
                # Degrade disk writes and record explicit quota gap; NEVER delete archives early!
                existing_open_gap = conn.execute(
                    """
                    SELECT id FROM archive_gaps
                    WHERE session_id = ? AND source = ? AND reason = 'disk_quota_exceeded' AND ended_at IS NULL
                    """,
                    (session_id, source),
                ).fetchone()
                if existing_open_gap is None:
                    conn.execute(
                        """
                        INSERT INTO archive_gaps(session_id, source, started_at, reason, detail)
                        VALUES (?, ?, ?, 'disk_quota_exceeded', ?)
                        """,
                        (session_id, source, ts_iso, quota_reason),
                    )
                conn.execute(
                    """
                    UPDATE archive_files
                    SET availability = 'degraded_quota_exceeded', missing_reason = ?
                    WHERE session_id = ? AND source = ?
                    """,
                    (quota_reason or "disk_quota_exceeded", session_id, source),
                )
                return False

            cur_ent = conn.execute(
                """
                INSERT OR IGNORE INTO archive_entries(session_id, source, event_id, level, ts, text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, source, evt_id, level, ts_iso, text),
            )
            if cur_ent.rowcount == 0:
                return False

            relpath = f"{dir_rel}/{source}.log"
            disk_path = self.settings.resolved_archive_dir / relpath
            line = f"{ts_iso} [{level}] [{evt_id}] {text}"
            append_log_line(disk_path, line)

            conn.execute(
                """
                UPDATE archive_files
                SET relpath = ?, line_count = line_count + 1
                WHERE session_id = ? AND source = ?
                """,
                (relpath, session_id, source),
            )
            return True

    def record_gap(
        self,
        *,
        group_id: str,
        source: Literal["router", "mavproxy", "all"],
        reason: Literal["observer_disconnected", "archive_restarted", "disk_quota_exceeded", "journal_gap"],
        started_at: str | None = None,
        ended_at: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self.db.write_transaction() as conn:
            active = conn.execute(
                "SELECT session_id FROM archive_sessions WHERE group_id = ? AND status = 'active' AND purged_at IS NULL",
                (group_id,),
            ).fetchone()
            if active is None:
                return
            conn.execute(
                """
                INSERT INTO archive_gaps(session_id, source, started_at, ended_at, reason, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(active["session_id"]),
                    source,
                    started_at or self.clock.now_iso(),
                    ended_at,
                    reason,
                    detail,
                ),
            )

    def check_heartbeat_timeouts(self) -> list[str]:
        """Close & seal any active session whose `last_heartbeat_at` exceeded offline grace."""
        now = self.clock.now()
        grace = timedelta(seconds=self.settings.fc_heartbeat_grace_seconds)
        expired_group_ids: list[str] = []
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT session_id, group_id, last_heartbeat_at FROM archive_sessions WHERE status = 'active' AND purged_at IS NULL"
            ).fetchall()
            for r in rows:
                last_hb = parse_rfc3339(str(r["last_heartbeat_at"]))
                if last_hb is not None and (now - last_hb) >= grace:
                    expired_group_ids.append(str(r["group_id"]))

        sealed_ids: list[str] = []
        for gid in expired_group_ids:
            sid = self.seal_active_session_for_group(gid, close_reason="fc_heartbeat_timeout")
            if sid:
                sealed_ids.append(sid)
                with self.db.write_transaction() as conn:
                    conn.execute(
                        "UPDATE group_runtime_status SET fc_state = 'offline', observed_at = ? WHERE group_id = ?",
                        (self.clock.now_iso(), gid),
                    )
        return sealed_ids

    # ------------------------------------------------------------------
    # Atomic Crash-Recoverable Sealing
    # ------------------------------------------------------------------
    def seal_active_session_for_group(
        self,
        group_id: str,
        *,
        close_reason: SessionCloseReason,
    ) -> str | None:
        with self.db.write_transaction() as conn:
            row = conn.execute(
                """
                SELECT session_id FROM archive_sessions
                WHERE group_id = ? AND status IN ('active', 'sealing') AND purged_at IS NULL
                ORDER BY first_observed_at DESC LIMIT 1
                """,
                (group_id,),
            ).fetchone()
            if row is None:
                return None
            session_id = str(row["session_id"])
            conn.execute(
                "UPDATE archive_sessions SET status = 'sealing', close_reason = COALESCE(close_reason, ?) WHERE session_id = ?",
                (close_reason, session_id),
            )
        return self._finalize_sealing_session(session_id, close_reason=close_reason)

    def recover_sealing_sessions(self) -> list[str]:
        """Finish any sessions interrupted in `sealing` state after crash/restart."""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT session_id, close_reason FROM archive_sessions WHERE status = 'sealing' AND purged_at IS NULL"
            ).fetchall()
        recovered: list[str] = []
        for r in rows:
            sid = self._finalize_sealing_session(
                str(r["session_id"]),
                close_reason=(r["close_reason"] or "recovery_closed"),
            )
            if sid:
                recovered.append(sid)
        return recovered

    def _finalize_sealing_session(
        self,
        session_id: str,
        *,
        close_reason: SessionCloseReason,
    ) -> str:
        now = self.clock.now()
        now_iso = format_rfc3339(now) or ""
        ecs_instance_id = self.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"

        with self.db.read() as conn:
            sess = conn.execute(
                "SELECT * FROM archive_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if sess is None:
                raise NotFoundError("SESSION_NOT_FOUND", f"Session '{session_id}' not found.")
            file_rows = conn.execute(
                "SELECT * FROM archive_files WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            gap_rows = conn.execute(
                "SELECT * FROM archive_gaps WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            ag = conn.execute(
                "SELECT deleted FROM archive_groups WHERE group_id = ?",
                (sess["group_id"],),
            ).fetchone()

        ended_at = sess["ended_at"] or now_iso
        ended_dt = parse_rfc3339(ended_at) or now
        expires_at = format_rfc3339(ended_dt + timedelta(seconds=self.settings.archive_retention_seconds)) or ""
        dir_rel = str(sess["dir_relpath"])
        session_dir = self.settings.resolved_archive_dir / dir_rel
        session_dir.mkdir(parents=True, exist_ok=True)

        # Compute exact file states for router & mavproxy (distinguishing missing_on_server vs available!)
        finalized_files: dict[str, dict[str, Any]] = {}
        for fr in file_rows:
            src = str(fr["source"])
            if src == "metadata":
                continue
            relpath = fr["relpath"] or f"{dir_rel}/{src}.log"
            fpath = self.settings.resolved_archive_dir / relpath
            was_degraded = str(fr["availability"]) == "degraded_quota_exceeded"
            if fpath.exists() and fpath.is_file() and fpath.stat().st_size > 0:
                digest, size = file_sha256_and_size(fpath)
                avail: ArchiveSourceAvailability = "degraded_quota_exceeded" if was_degraded else "available"
                finalized_files[src] = {
                    "file_id": str(fr["file_id"]),
                    "availability": avail,
                    "relpath": relpath,
                    "size_bytes": size,
                    "sha256": digest,
                    "line_count": int(fr["line_count"]),
                    "missing_reason": fr["missing_reason"] if was_degraded else None,
                }
            else:
                reason = fr["missing_reason"] or (
                    "no_mavproxy_output_recorded" if src == "mavproxy" else "no_router_log_recorded"
                )
                avail = "degraded_quota_exceeded" if was_degraded else "missing_on_server"
                finalized_files[src] = {
                    "file_id": str(fr["file_id"]),
                    "availability": avail,
                    "relpath": None,
                    "size_bytes": None,
                    "sha256": None,
                    "line_count": 0,
                    "missing_reason": reason,
                }

        gaps_payload = [
            {
                "source": str(g["source"]),
                "started_at": str(g["started_at"]),
                "ended_at": g["ended_at"] or ended_at,
                "reason": str(g["reason"]),
                "detail": g["detail"],
            }
            for g in gap_rows
        ]

        # Build and atomically write metadata.json BEFORE committing sealed index!
        meta_doc = {
            "schema_version": 1,
            "ecs_instance_id": ecs_instance_id,
            "session_id": session_id,
            "group_id": str(sess["group_id"]),
            "group_name_snapshot": str(sess["group_name_snapshot"]),
            "dtu_port_snapshot": int(sess["dtu_port_snapshot"]),
            "gcs_protocol_snapshot": str(sess["gcs_protocol_snapshot"]),
            "gcs_port_snapshot": int(sess["gcs_port_snapshot"]),
            "group_deleted": bool(ag["deleted"]) if ag else False,
            "sysid": sess["sysid"],
            "first_observed_at": str(sess["first_observed_at"]),
            "last_heartbeat_at": str(sess["last_heartbeat_at"]),
            "ended_at": ended_at,
            "sealed_at": now_iso,
            "expires_at": expires_at,
            "close_reason": close_reason,
            "sources": {
                src: {
                    "file_id": info["file_id"] if info["availability"] in ("available", "empty") else None,
                    "availability": info["availability"],
                    "size_bytes": info["size_bytes"],
                    "sha256": info["sha256"],
                    "line_count": info["line_count"],
                    "missing_reason": info["missing_reason"],
                }
                for src, info in finalized_files.items()
            },
            "gaps": gaps_payload,
        }
        meta_relpath = f"{dir_rel}/metadata.json"
        meta_sha, meta_size = write_json_atomic(
            self.settings.resolved_archive_dir / meta_relpath,
            meta_doc,
        )

        # Single atomic DB commit: assign monotonic change_seq & mark sealed ONLY after files are on disk!
        with self.db.write_transaction() as conn:
            # Close any open gaps
            conn.execute(
                "UPDATE archive_gaps SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
                (ended_at, session_id),
            )
            for src, info in finalized_files.items():
                conn.execute(
                    """
                    UPDATE archive_files
                    SET availability = ?, relpath = ?, size_bytes = ?, sha256 = ?,
                        line_count = ?, missing_reason = ?
                    WHERE session_id = ? AND source = ?
                    """,
                    (
                        info["availability"],
                        info["relpath"],
                        info["size_bytes"],
                        info["sha256"],
                        info["line_count"],
                        info["missing_reason"],
                        session_id,
                        src,
                    ),
                )
            conn.execute(
                """
                UPDATE archive_files
                SET availability = 'available', relpath = ?, size_bytes = ?, sha256 = ?,
                    line_count = 1, missing_reason = NULL
                WHERE session_id = ? AND source = 'metadata'
                """,
                (meta_relpath, meta_size, meta_sha, session_id),
            )

            seq_row = conn.execute(
                "SELECT value FROM meta_config WHERE key = 'archive_change_seq'"
            ).fetchone()
            next_seq = (int(seq_row["value"]) if seq_row else 0) + 1
            conn.execute(
                "UPDATE meta_config SET value = ?, updated_at = ? WHERE key = 'archive_change_seq'",
                (str(next_seq), now_iso),
            )
            conn.execute(
                """
                UPDATE archive_sessions
                SET status = 'sealed', change_seq = COALESCE(change_seq, ?),
                    ended_at = ?, sealed_at = ?, expires_at = ?, close_reason = ?
                WHERE session_id = ?
                """,
                (next_seq, ended_at, now_iso, expires_at, close_reason, session_id),
            )
        return session_id

    # ------------------------------------------------------------------
    # Download Leases & 7-Day Retention Cleanup (never races downloads!)
    # ------------------------------------------------------------------
    def acquire_download_lease(self, session_id: str, *, ttl_seconds: int = 120) -> str:
        now = self.clock.now()
        lease_id = new_token_id()
        with self.db.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO archive_download_leases(lease_id, session_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    lease_id,
                    session_id,
                    format_rfc3339(now),
                    format_rfc3339(now + timedelta(seconds=ttl_seconds)),
                ),
            )
        return lease_id

    def release_download_lease(self, lease_id: str) -> None:
        with self.db.write_transaction() as conn:
            conn.execute("DELETE FROM archive_download_leases WHERE lease_id = ?", (lease_id,))

    def purge_expired_sessions(self) -> list[str]:
        """Purge sealed sessions past `expires_at` (7x24h from `ended_at`).

        - Active sessions are NEVER purged.
        - Sessions with an active (non-expired) download lease are skipped until download finishes.
        - Advances `archive_min_valid_seq` so clients offline past retention get `410 CURSOR_EXPIRED`.
        """
        now = self.clock.now()
        now_iso = format_rfc3339(now) or ""
        purged_ids: list[str] = []
        dirs_to_delete: list[Path] = []

        with self.db.write_transaction() as conn:
            # Clean stale leases
            conn.execute("DELETE FROM archive_download_leases WHERE expires_at <= ?", (now_iso,))

            rows = conn.execute(
                """
                SELECT session_id, change_seq, expires_at, dir_relpath
                FROM archive_sessions
                WHERE status = 'sealed' AND purged_at IS NULL AND expires_at IS NOT NULL
                ORDER BY change_seq ASC
                """
            ).fetchall()

            max_purged_seq = 0
            for r in rows:
                exp_dt = parse_rfc3339(str(r["expires_at"]))
                if exp_dt is None or now < exp_dt:
                    continue
                sid = str(r["session_id"])
                active_lease = conn.execute(
                    "SELECT 1 FROM archive_download_leases WHERE session_id = ? AND expires_at > ? LIMIT 1",
                    (sid, now_iso),
                ).fetchone()
                if active_lease is not None:
                    continue  # Never race an in-progress download!

                conn.execute(
                    "UPDATE archive_sessions SET purged_at = ? WHERE session_id = ?",
                    (now_iso, sid),
                )
                conn.execute("DELETE FROM archive_entries WHERE session_id = ?", (sid,))
                purged_ids.append(sid)
                if r["change_seq"] is not None:
                    max_purged_seq = max(max_purged_seq, int(r["change_seq"]))
                dirs_to_delete.append(self.settings.resolved_archive_dir / str(r["dir_relpath"]))

            if max_purged_seq > 0:
                cur_min = conn.execute(
                    "SELECT value FROM meta_config WHERE key = 'archive_min_valid_seq'"
                ).fetchone()
                prev_min = int(cur_min["value"]) if cur_min else 0
                if max_purged_seq > prev_min:
                    conn.execute(
                        "UPDATE meta_config SET value = ?, updated_at = ? WHERE key = 'archive_min_valid_seq'",
                        (str(max_purged_seq), now_iso),
                    )

        for d in dirs_to_delete:
            safe_rmtree(d)
        return purged_ids

    # ------------------------------------------------------------------
    # Read-Only Query Methods (NEVER start capture or return bodies on lists!)
    # ------------------------------------------------------------------
    def _build_artifact_descriptors(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        session_status: str,
    ) -> list[ArchiveArtifactDescriptor]:
        rows = conn.execute(
            "SELECT * FROM archive_files WHERE session_id = ? ORDER BY CASE source WHEN 'router' THEN 1 WHEN 'mavproxy' THEN 2 ELSE 3 END",
            (session_id,),
        ).fetchall()
        descriptors: list[ArchiveArtifactDescriptor] = []
        for r in rows:
            avail = str(r["availability"])
            readable = session_status == "sealed" and avail in ("available", "empty", "degraded_quota_exceeded") and r["relpath"] is not None
            fid = str(r["file_id"]) if readable else None
            descriptors.append(
                ArchiveArtifactDescriptor(
                    source=r["source"],
                    availability=r["availability"],
                    file_id=fid,
                    logical_name=str(r["logical_name"]),
                    content_type=str(r["content_type"]),
                    size_bytes=r["size_bytes"],
                    sha256=r["sha256"],
                    line_count=int(r["line_count"]) if r["line_count"] is not None else 0,
                    missing_reason=r["missing_reason"],
                    read_url=f"/api/v1/archive/sessions/{session_id}/files/{fid}" if fid else None,
                )
            )
        return descriptors

    def _build_session_summary(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        ecs_instance_id: str,
        group_deleted: bool,
    ) -> ArchiveSessionSummary:
        sid = str(row["session_id"])
        files = self._build_artifact_descriptors(conn, sid, str(row["status"]))
        gap_cnt = int(
            conn.execute(
                "SELECT COUNT(*) AS cnt FROM archive_gaps WHERE session_id = ?",
                (sid,),
            ).fetchone()["cnt"]
        )
        has_missing = any(
            f.availability in ("missing_on_server", "degraded_quota_exceeded")
            for f in files
        )
        return ArchiveSessionSummary(
            ecs_instance_id=ecs_instance_id,
            session_id=sid,
            group_id=str(row["group_id"]),
            group_name_snapshot=str(row["group_name_snapshot"]),
            dtu_port_snapshot=int(row["dtu_port_snapshot"]),
            gcs_protocol_snapshot=row["gcs_protocol_snapshot"],
            gcs_port_snapshot=int(row["gcs_port_snapshot"]),
            group_deleted=group_deleted,
            status=row["status"],
            sysid=row["sysid"],
            first_observed_at=str(row["first_observed_at"]),
            last_heartbeat_at=str(row["last_heartbeat_at"]),
            ended_at=row["ended_at"],
            sealed_at=row["sealed_at"],
            expires_at=row["expires_at"],
            close_reason=row["close_reason"],
            has_gaps=gap_cnt > 0,
            has_missing_sources=has_missing,
            files=files,
        )

    def _is_session_expired(self, row: sqlite3.Row) -> bool:
        if row["purged_at"] is not None:
            return True
        if row["status"] == "sealed" and row["expires_at"]:
            exp = parse_rfc3339(str(row["expires_at"]))
            if exp is not None and self.clock.now() >= exp:
                return True
        return False

    def _build_archive_group_summary(
        self,
        conn: sqlite3.Connection,
        g: sqlite3.Row,
        *,
        ecs_instance_id: str,
    ) -> ArchiveGroupSummary:
        gid = str(g["group_id"])
        sess_rows = conn.execute(
            """
            SELECT * FROM archive_sessions
            WHERE group_id = ? AND purged_at IS NULL
            ORDER BY first_observed_at DESC, session_id DESC
            """,
            (gid,),
        ).fetchall()
        live_sessions = [s for s in sess_rows if not self._is_session_expired(s)]
        active_cnt = sum(1 for s in live_sessions if s["status"] != "sealed")
        sealed_cnt = sum(1 for s in live_sessions if s["status"] == "sealed")
        latest = (
            self._build_session_summary(
                conn,
                live_sessions[0],
                ecs_instance_id=ecs_instance_id,
                group_deleted=bool(g["deleted"]),
            )
            if live_sessions
            else None
        )
        return ArchiveGroupSummary(
            group_id=gid,
            name_snapshot=str(g["name_snapshot"]),
            dtu_protocol="udp",
            dtu_port_snapshot=int(g["dtu_port_snapshot"]),
            gcs_protocol_snapshot=g["gcs_protocol_snapshot"],
            gcs_port_snapshot=int(g["gcs_port_snapshot"]),
            deleted=bool(g["deleted"]),
            deleted_at=g["deleted_at"],
            active_session_count=active_cnt,
            sealed_session_count=sealed_cnt,
            latest_session=latest,
        )

    def list_archive_groups(self, *, include_deleted: bool = True) -> ArchiveGroupListResponse:
        ecs_instance_id = self.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"
        now_iso = self.clock.now_iso()
        items: list[ArchiveGroupSummary] = []

        with self.db.read() as conn:
            where = "" if include_deleted else "WHERE deleted = 0"
            groups = conn.execute(
                f"SELECT * FROM archive_groups {where} ORDER BY created_at ASC, group_id ASC"
            ).fetchall()

            for g in groups:
                items.append(
                    self._build_archive_group_summary(
                        conn,
                        g,
                        ecs_instance_id=ecs_instance_id,
                    )
                )

        return ArchiveGroupListResponse(
            ecs_instance_id=ecs_instance_id,
            items=items,
            observed_at=now_iso,
        )

    def get_archive_group(self, group_id: str) -> ArchiveGroupSummary:
        ecs_instance_id = self.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"
        with self.db.read() as conn:
            g = conn.execute(
                "SELECT * FROM archive_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if g is None:
                raise NotFoundError(
                    "ARCHIVE_GROUP_NOT_FOUND",
                    f"Archive group '{group_id}' not found.",
                )
            return self._build_archive_group_summary(
                conn,
                g,
                ecs_instance_id=ecs_instance_id,
            )

    def list_group_sessions(
        self,
        group_id: str,
        *,
        status: str = "all",
        cursor: str | None = None,
        limit: int = 30,
    ) -> ArchiveSessionListResponse:
        ecs_instance_id = self.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"
        with self.db.read() as conn:
            g = conn.execute(
                "SELECT deleted FROM archive_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if g is None:
                raise NotFoundError("ARCHIVE_GROUP_NOT_FOUND", f"Archive group '{group_id}' not found.")

            clauses = ["group_id = ?", "purged_at IS NULL"]
            params: list[Any] = [group_id]
            if status in ("active", "sealed"):
                clauses.append("status = ?")
                params.append(status)
            if cursor:
                clauses.append("first_observed_at < ?")
                params.append(cursor)

            rows = conn.execute(
                f"SELECT * FROM archive_sessions WHERE {' AND '.join(clauses)} ORDER BY first_observed_at DESC",
                params,
            ).fetchall()
            live = [r for r in rows if not self._is_session_expired(r)]
            has_more = len(live) > limit
            sliced = live[:limit]
            items = [
                self._build_session_summary(
                    conn,
                    r,
                    ecs_instance_id=ecs_instance_id,
                    group_deleted=bool(g["deleted"]),
                )
                for r in sliced
            ]
            next_cursor = str(sliced[-1]["first_observed_at"]) if has_more and sliced else None
            return ArchiveSessionListResponse(
                group_id=group_id,
                items=items,
                next_cursor=next_cursor,
                has_more=has_more,
            )

    def get_session_detail(self, session_id: str) -> ArchiveSessionDetail:
        ecs_instance_id = self.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM archive_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("SESSION_NOT_FOUND", f"Archive session '{session_id}' not found.")
            if self._is_session_expired(row):
                raise GoneError(
                    "ARCHIVE_EXPIRED",
                    "Archive session has exceeded the 7-day cloud retention window.",
                    details=[ErrorDetailItem(field="session_id", message=f"expired_at={row['expires_at']}")],
                )
            g = conn.execute(
                "SELECT deleted FROM archive_groups WHERE group_id = ?",
                (row["group_id"],),
            ).fetchone()
            summary = self._build_session_summary(
                conn,
                row,
                ecs_instance_id=ecs_instance_id,
                group_deleted=bool(g["deleted"]) if g else False,
            )
            gap_rows = conn.execute(
                "SELECT * FROM archive_gaps WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            gaps = [
                ArchiveGap(
                    source=gr["source"],
                    started_at=str(gr["started_at"]),
                    ended_at=gr["ended_at"],
                    reason=gr["reason"],
                    detail=gr["detail"],
                )
                for gr in gap_rows
            ]
            return ArchiveSessionDetail(**summary.model_dump(), gaps=gaps)

    def get_session_entries(
        self,
        session_id: str,
        *,
        source: LogSource,
        level: str = "all",
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> ArchiveEntryPageResponse:
        with self.db.read() as conn:
            sess = conn.execute(
                "SELECT * FROM archive_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if sess is None:
                raise NotFoundError("SESSION_NOT_FOUND", f"Archive session '{session_id}' not found.")
            if self._is_session_expired(sess):
                raise GoneError(
                    "ARCHIVE_EXPIRED",
                    "Archive session has exceeded the 7-day cloud retention window.",
                )
            f_row = conn.execute(
                "SELECT availability, missing_reason FROM archive_files WHERE session_id = ? AND source = ?",
                (session_id, source),
            ).fetchone()
            clauses = ["session_id = ?", "source = ?"]
            params: list[Any] = [session_id, source]
            if level != "all":
                clauses.append("level = ?")
                params.append(level)
            if q:
                clauses.append("LOWER(text) LIKE ?")
                params.append(f"%{q.strip().lower()}%")
            if cursor and cursor.isdigit():
                clauses.append("seq > ?")
                params.append(int(cursor))
            params.append(limit + 1)

            rows = conn.execute(
                f"SELECT * FROM archive_entries WHERE {' AND '.join(clauses)} ORDER BY seq ASC LIMIT ?",
                params,
            ).fetchall()
            has_more = len(rows) > limit
            sliced = rows[:limit]
            items = [
                LogEntry(
                    event_id=str(r["event_id"]),
                    group_id=str(sess["group_id"]),
                    source=r["source"],
                    level=r["level"],
                    ts=str(r["ts"]),
                    text=str(r["text"]),
                )
                for r in sliced
            ]
            next_cursor = str(sliced[-1]["seq"]) if sliced else cursor
            gap_rows = conn.execute(
                "SELECT * FROM archive_gaps WHERE session_id = ? AND source IN (?, 'all') ORDER BY id ASC",
                (session_id, source),
            ).fetchall()
            gaps = [
                ArchiveGap(
                    source=gr["source"],
                    started_at=str(gr["started_at"]),
                    ended_at=gr["ended_at"],
                    reason=gr["reason"],
                    detail=gr["detail"],
                )
                for gr in gap_rows
            ]
            return ArchiveEntryPageResponse(
                session_id=session_id,
                source=source,
                source_availability=f_row["availability"] if f_row else "missing_on_server",
                missing_reason=f_row["missing_reason"] if f_row else "source_not_recorded",
                items=items,
                gaps=gaps,
                next_cursor=next_cursor,
                has_more=has_more,
            )

    def list_archive_changes(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ArchiveChangesResponse:
        """Incremental sealed-session manifest ONLY (no active sessions, no file bodies)."""
        ecs_instance_id = self.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"
        now_iso = self.clock.now_iso()

        with self.db.read() as conn:
            min_valid_seq = int(
                (conn.execute("SELECT value FROM meta_config WHERE key = 'archive_min_valid_seq'").fetchone() or {"value": "0"})["value"]
            )
            after_seq = 0
            if cursor is not None and cursor != "":
                if not cursor.startswith("chg_") or not cursor[4:].isdigit():
                    raise GoneError(
                        "CURSOR_EXPIRED",
                        "Invalid or expired archive sync cursor; reconcile from an empty cursor.",
                        details=[ErrorDetailItem(field="cursor", message="reconcile_required=true")],
                    )
                after_seq = int(cursor[4:])
                if min_valid_seq > 0 and after_seq < min_valid_seq:
                    raise GoneError(
                        "CURSOR_EXPIRED",
                        "Sync cursor is older than the retained archive horizon; record a retention-window gap and reconcile from an empty cursor.",
                        details=[
                            ErrorDetailItem(field="cursor", message="reconcile_required=true"),
                            ErrorDetailItem(field="min_valid_seq", message=str(min_valid_seq)),
                        ],
                    )

            rows = conn.execute(
                """
                SELECT s.*, ag.deleted AS ag_deleted
                FROM archive_sessions s
                JOIN archive_groups ag ON ag.group_id = s.group_id
                WHERE s.status = 'sealed' AND s.purged_at IS NULL AND s.change_seq > ?
                ORDER BY s.change_seq ASC
                """,
                (after_seq,),
            ).fetchall()

            live = [r for r in rows if not self._is_session_expired(r)]
            has_more = len(live) > limit
            sliced = live[:limit]

            oldest_row = conn.execute(
                """
                SELECT MIN(sealed_at) AS oldest
                FROM archive_sessions
                WHERE status = 'sealed' AND purged_at IS NULL AND (expires_at IS NULL OR expires_at > ?)
                """,
                (now_iso,),
            ).fetchone()
            oldest_sealed = oldest_row["oldest"] if oldest_row else None

            items: list[ArchiveChangeItem] = []
            for r in sliced:
                sid = str(r["session_id"])
                files = self._build_artifact_descriptors(conn, sid, "sealed")
                gap_rows = conn.execute(
                    "SELECT * FROM archive_gaps WHERE session_id = ? ORDER BY id ASC",
                    (sid,),
                ).fetchall()
                gaps = [
                    ArchiveGap(
                        source=gr["source"],
                        started_at=str(gr["started_at"]),
                        ended_at=gr["ended_at"],
                        reason=gr["reason"],
                        detail=gr["detail"],
                    )
                    for gr in gap_rows
                ]
                items.append(
                    ArchiveChangeItem(
                        change_seq=int(r["change_seq"]),
                        ecs_instance_id=ecs_instance_id,
                        group_id=str(r["group_id"]),
                        group_name_snapshot=str(r["group_name_snapshot"]),
                        dtu_port_snapshot=int(r["dtu_port_snapshot"]),
                        gcs_protocol_snapshot=r["gcs_protocol_snapshot"],
                        gcs_port_snapshot=int(r["gcs_port_snapshot"]),
                        group_deleted=bool(r["ag_deleted"]),
                        session_id=sid,
                        sysid=r["sysid"],
                        first_observed_at=str(r["first_observed_at"]),
                        last_heartbeat_at=str(r["last_heartbeat_at"]),
                        ended_at=str(r["ended_at"]),
                        sealed_at=str(r["sealed_at"]),
                        expires_at=str(r["expires_at"]),
                        close_reason=r["close_reason"],
                        files=files,
                        gaps=gaps,
                    )
                )

            if sliced:
                next_cursor = f"chg_{int(sliced[-1]['change_seq'])}"
            elif cursor:
                next_cursor = cursor
            else:
                cur_seq = int(
                    (conn.execute("SELECT value FROM meta_config WHERE key = 'archive_change_seq'").fetchone() or {"value": "0"})["value"]
                )
                next_cursor = f"chg_{cur_seq}"

            return ArchiveChangesResponse(
                ecs_instance_id=ecs_instance_id,
                retention_seconds=self.settings.archive_retention_seconds,
                oldest_available_sealed_at=oldest_sealed,
                items=items,
                next_cursor=next_cursor,
                has_more=has_more,
                observed_at=now_iso,
            )

    def resolve_session_file(
        self,
        *,
        session_id: str,
        file_id: str,
    ) -> tuple[Path, dict[str, str]]:
        """Resolve an authenticated sealed artifact file for reading (with expiry & missing checks)."""
        with self.db.read() as conn:
            sess = conn.execute(
                "SELECT * FROM archive_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if sess is None:
                raise NotFoundError("SESSION_NOT_FOUND", f"Archive session '{session_id}' not found.")
            if self._is_session_expired(sess):
                raise GoneError(
                    "ARCHIVE_EXPIRED",
                    "Archive session has exceeded the 7-day cloud retention window.",
                    details=[ErrorDetailItem(field="session_id", message=f"expired_at={sess['expires_at']}")],
                )
            if sess["status"] != "sealed":
                raise NotFoundError("ARCHIVE_FILE_NOT_FOUND", "Session is not sealed yet.")

            fr = conn.execute(
                "SELECT * FROM archive_files WHERE session_id = ? AND file_id = ?",
                (session_id, file_id),
            ).fetchone()
            if fr is None:
                raise NotFoundError("ARCHIVE_FILE_NOT_FOUND", f"Archive file '{file_id}' not found.")
            if fr["availability"] == "missing_on_server" or not fr["relpath"]:
                raise NotFoundError(
                    "ARCHIVE_SOURCE_MISSING",
                    f"Cloud archive does not contain source '{fr['source']}' for this session.",
                    details=[ErrorDetailItem(field="source", message=str(fr["missing_reason"] or "missing_on_server"))],
                )
            disk_path = self.settings.resolved_archive_dir / str(fr["relpath"])
            if not disk_path.exists():
                raise NotFoundError("ARCHIVE_FILE_NOT_FOUND", "Archive file missing on disk.")

            meta = {
                "file_id": str(fr["file_id"]),
                "source": str(fr["source"]),
                "logical_name": str(fr["logical_name"]),
                "content_type": str(fr["content_type"]),
                "sha256": str(fr["sha256"] or ""),
                "expires_at": str(sess["expires_at"] or ""),
            }
            return disk_path, meta

    def resolve_session_download_by_source(
        self,
        *,
        session_id: str,
        source: Literal["router", "mavproxy", "metadata", "all"],
    ) -> tuple[list[tuple[str, Path]], dict[str, str]]:
        """Resolve CLI/compat download targets for `source=router|mavproxy|metadata|all`."""
        with self.db.read() as conn:
            sess = conn.execute(
                "SELECT * FROM archive_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if sess is None:
                raise NotFoundError("SESSION_NOT_FOUND", f"Archive session '{session_id}' not found.")
            if self._is_session_expired(sess):
                raise GoneError(
                    "ARCHIVE_EXPIRED",
                    "Archive session has exceeded the 7-day cloud retention window.",
                )

            if source == "all":
                rows = conn.execute(
                    "SELECT * FROM archive_files WHERE session_id = ? AND relpath IS NOT NULL",
                    (session_id,),
                ).fetchall()
                files = []
                for r in rows:
                    p = self.settings.resolved_archive_dir / str(r["relpath"])
                    if p.exists():
                        files.append((str(r["logical_name"]), p))
                if not files:
                    raise NotFoundError("ARCHIVE_SOURCE_MISSING", "No archive files available for this session.")
                return files, {
                    "expires_at": str(sess["expires_at"] or ""),
                    "group_name": str(sess["group_name_snapshot"]),
                }

            fr = conn.execute(
                "SELECT * FROM archive_files WHERE session_id = ? AND source = ?",
                (session_id, source),
            ).fetchone()
            if fr is None or not fr["relpath"]:
                raise NotFoundError(
                    "ARCHIVE_SOURCE_MISSING",
                    f"Cloud archive does not contain source '{source}' for this session.",
                    details=[ErrorDetailItem(field="source", message=str(fr["missing_reason"] if fr else "missing_on_server"))],
                )
            p = self.settings.resolved_archive_dir / str(fr["relpath"])
            if not p.exists():
                raise NotFoundError("ARCHIVE_FILE_NOT_FOUND", "Archive file missing on disk.")
            return [(str(fr["logical_name"]), p)], {
                "content_type": str(fr["content_type"]),
                "sha256": str(fr["sha256"] or ""),
                "expires_at": str(sess["expires_at"] or ""),
                "logical_name": str(fr["logical_name"]),
            }

    # ------------------------------------------------------------------
    # Live Log Querying (Read-Only, Cursor-Based)
    # ------------------------------------------------------------------
    def query_live_logs(
        self,
        *,
        group_id: str | None = None,
        source: str = "all",
        level: str = "all",
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> LiveLogPageResponse:
        now_iso = self.clock.now_iso()
        with self.db.read() as conn:
            min_valid = int(
                (conn.execute("SELECT value FROM meta_config WHERE key = 'live_log_min_valid_seq'").fetchone() or {"value": "0"})["value"]
            )
            after_seq = 0
            if cursor is not None and cursor != "":
                if not cursor.startswith("log_") or not cursor[4:].isdigit():
                    raise GoneError("CURSOR_EXPIRED", "Invalid or expired live log cursor.")
                after_seq = int(cursor[4:])
                if min_valid > 0 and after_seq < min_valid:
                    raise GoneError("CURSOR_EXPIRED", "Live log cursor fell behind buffer horizon.")

            clauses = ["seq > ?"]
            params: list[Any] = [after_seq]
            if group_id:
                clauses.append("group_id = ?")
                params.append(group_id)
            if source in ("router", "mavproxy"):
                clauses.append("source = ?")
                params.append(source)
            if level in ("DEBUG", "INFO", "WARN", "ERROR"):
                clauses.append("level = ?")
                params.append(level)
            if q:
                clauses.append("LOWER(text) LIKE ?")
                params.append(f"%{q.strip().lower()}%")
            params.append(limit + 1)

            rows = conn.execute(
                f"SELECT * FROM live_logs WHERE {' AND '.join(clauses)} ORDER BY seq ASC LIMIT ?",
                params,
            ).fetchall()
            has_more = len(rows) > limit
            sliced = rows[:limit]
            items = [
                LogEntry(
                    event_id=str(r["event_id"]),
                    group_id=str(r["group_id"]),
                    source=r["source"],
                    level=r["level"],
                    ts=str(r["ts"]),
                    text=str(r["text"]),
                )
                for r in sliced
            ]
            if sliced:
                next_cursor = f"log_{int(sliced[-1]['seq'])}"
            elif cursor:
                next_cursor = cursor
            else:
                max_row = conn.execute("SELECT MAX(seq) AS mx FROM live_logs").fetchone()
                next_cursor = f"log_{int(max_row['mx'] or 0)}"

        return LiveLogPageResponse(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
            observed_at=now_iso,
        )
