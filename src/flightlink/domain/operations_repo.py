"""Atomic control-plane transactions & read queries (`flightlink-api` and `flightlink-cli`).

Strict architectural boundary:
- NEVER calls systemd, sockets, or Aliyun APIs inside this module!
- Every write method commits intent, config/candidate version, operation generation,
  idempotency record, and port reservations in ONE SQLite `BEGIN IMMEDIATE` transaction,
  then returns the queued `OperationDetail` (`202 Accepted`).
- Every read method is 100% side-effect free.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from flightlink.clock import Clock, SystemClock, format_rfc3339, parse_rfc3339
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.errors import (
    ConflictError,
    ErrorDetailItem,
    NotFoundError,
    PreconditionFailedError,
    UnauthorizedError,
    ValidationDomainError,
)
from flightlink.ids import new_group_id, new_operation_id, new_token_id, new_user_id
from flightlink.schemas.auth import TokenResponse, UserProfile, UserRole
from flightlink.schemas.groups import (
    ComponentHealth,
    CreateGroupRequest,
    DiskQuotaStatus,
    GroupDetail,
    GroupListResponse,
    GroupSummary,
    GroupUpdateDryRunResponse,
    MavproxyServiceStatus,
    ObservedGcsCommand,
    OverviewMetrics,
    OverviewResponse,
    RouterServiceStatus,
    SecurityGroupRuleView,
    SecurityGroupServiceStatus,
    TelemetrySnapshot,
    UpdateGroupRequest,
)
from flightlink.schemas.operations import (
    OperationAction,
    OperationDetail,
    OperationListResponse,
    OperationStep,
)
from flightlink.security import (
    TokenExpiredError,
    TokenInvalidError,
    create_access_token,
    decode_access_token,
    generate_opaque_secret,
    hash_password,
    sha256_hex,
    verify_password,
)


def parse_if_match_version(if_match: str | None) -> int | None:
    if if_match is None or not if_match.strip():
        return None
    raw = if_match.strip().strip('"').strip("'")
    if raw.startswith("W/"):
        raw = raw[2:].strip().strip('"')
    if raw.lower().startswith("v"):
        raw = raw[1:]
    if not raw.isdigit():
        raise ValidationDomainError(
            "Invalid If-Match header format.",
            details=[ErrorDetailItem(field="If-Match", message='Expected "v<N>" or "<N>"')],
        )
    version = int(raw)
    if version < 1:
        raise ValidationDomainError(
            "Invalid If-Match version.",
            details=[ErrorDetailItem(field="If-Match", message="Version must be >= 1")],
        )
    return version


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    return sha256_hex(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


class ControlPlaneRepository:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.clock = clock or db.clock or SystemClock()

    # ------------------------------------------------------------------
    # Users & Authentication (NO demo accounts!)
    # ------------------------------------------------------------------
    def create_user(
        self,
        *,
        username: str,
        password: str,
        role: UserRole = "admin",
        display_name: str | None = None,
    ) -> UserProfile:
        username_clean = username.strip()
        if not username_clean:
            raise ValidationDomainError("Username cannot be empty.")
        if len(password) < 8:
            raise ValidationDomainError("Password must be at least 8 characters.")
        now_iso = self.clock.now_iso()
        user_id = new_user_id()
        pw_hash = hash_password(password)
        disp = (display_name or username_clean).strip()
        try:
            with self.db.write_transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO users(user_id, username, display_name, password_hash, role, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    """,
                    (user_id, username_clean, disp, pw_hash, role, now_iso),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("USER_EXISTS", f"User '{username_clean}' already exists.") from exc
        return UserProfile(
            user_id=user_id,
            username=username_clean,
            display_name=disp,
            role=role,
            created_at=now_iso,
            last_login_at=None,
        )

    def login(self, *, username: str, password: str) -> TokenResponse:
        now = self.clock.now()
        now_iso = format_rfc3339(now) or ""
        with self.db.write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? AND is_active = 1",
                (username.strip(),),
            ).fetchone()
            if row is None or not verify_password(password, str(row["password_hash"])):
                raise UnauthorizedError("INVALID_CREDENTIALS", "Invalid username or password.")

            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE user_id = ?",
                (now_iso, row["user_id"]),
            )
            access_token, access_exp = create_access_token(
                secret=self.settings.jwt_secret,
                user_id=str(row["user_id"]),
                username=str(row["username"]),
                role=str(row["role"]),
                ttl_seconds=self.settings.access_token_ttl_seconds,
                clock=self.clock,
            )
            refresh_raw = generate_opaque_secret("flr")
            refresh_exp = now + timedelta(seconds=self.settings.refresh_token_ttl_seconds)
            conn.execute(
                """
                INSERT INTO refresh_tokens(token_id, user_id, token_hash, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    new_token_id(),
                    str(row["user_id"]),
                    sha256_hex(refresh_raw),
                    format_rfc3339(refresh_exp),
                    now_iso,
                ),
            )
            profile = UserProfile(
                user_id=str(row["user_id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                role=row["role"],
                created_at=str(row["created_at"]),
                last_login_at=now_iso,
            )
            return TokenResponse(
                access_token=access_token,
                refresh_token=refresh_raw,
                expires_at=format_rfc3339(access_exp) or "",
                refresh_expires_at=format_rfc3339(refresh_exp) or "",
                user=profile,
            )

    def refresh_session(self, *, refresh_token: str) -> TokenResponse:
        now = self.clock.now()
        now_iso = format_rfc3339(now) or ""
        token_hash = sha256_hex(refresh_token)
        with self.db.write_transaction() as conn:
            tok_row = conn.execute(
                """
                SELECT rt.*, u.username, u.display_name, u.role, u.created_at AS user_created_at, u.last_login_at, u.is_active
                FROM refresh_tokens rt
                JOIN users u ON u.user_id = rt.user_id
                WHERE rt.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if tok_row is None or tok_row["revoked_at"] is not None or not tok_row["is_active"]:
                raise UnauthorizedError("TOKEN_INVALID", "Refresh token is invalid or revoked.")
            exp_dt = parse_rfc3339(str(tok_row["expires_at"]))
            if exp_dt is None or now >= exp_dt:
                raise UnauthorizedError("TOKEN_EXPIRED", "Refresh token has expired.")

            # Rotate refresh token
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = ? WHERE token_id = ?",
                (now_iso, tok_row["token_id"]),
            )
            new_refresh = generate_opaque_secret("flr")
            new_refresh_exp = now + timedelta(seconds=self.settings.refresh_token_ttl_seconds)
            conn.execute(
                """
                INSERT INTO refresh_tokens(token_id, user_id, token_hash, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    new_token_id(),
                    str(tok_row["user_id"]),
                    sha256_hex(new_refresh),
                    format_rfc3339(new_refresh_exp),
                    now_iso,
                ),
            )
            access_token, access_exp = create_access_token(
                secret=self.settings.jwt_secret,
                user_id=str(tok_row["user_id"]),
                username=str(tok_row["username"]),
                role=str(tok_row["role"]),
                ttl_seconds=self.settings.access_token_ttl_seconds,
                clock=self.clock,
            )
            profile = UserProfile(
                user_id=str(tok_row["user_id"]),
                username=str(tok_row["username"]),
                display_name=str(tok_row["display_name"]),
                role=tok_row["role"],
                created_at=str(tok_row["user_created_at"]),
                last_login_at=tok_row["last_login_at"],
            )
            return TokenResponse(
                access_token=access_token,
                refresh_token=new_refresh,
                expires_at=format_rfc3339(access_exp) or "",
                refresh_expires_at=format_rfc3339(new_refresh_exp) or "",
                user=profile,
            )

    def logout(self, *, user_id: str, refresh_token: str | None = None) -> None:
        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            if refresh_token:
                conn.execute(
                    "UPDATE refresh_tokens SET revoked_at = ? WHERE user_id = ? AND token_hash = ? AND revoked_at IS NULL",
                    (now_iso, user_id, sha256_hex(refresh_token)),
                )
            else:
                conn.execute(
                    "UPDATE refresh_tokens SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (now_iso, user_id),
                )

    def create_api_token(
        self,
        *,
        username: str,
        name: str,
        role: UserRole = "viewer",
        ttl_days: int | None = None,
    ) -> tuple[str, str]:
        now = self.clock.now()
        now_iso = format_rfc3339(now) or ""
        raw_token = generate_opaque_secret("flk")
        token_id = new_token_id()
        expires_at = format_rfc3339(now + timedelta(days=ttl_days)) if ttl_days else None
        with self.db.write_transaction() as conn:
            user = conn.execute(
                "SELECT user_id FROM users WHERE username = ? AND is_active = 1",
                (username,),
            ).fetchone()
            if user is None:
                raise NotFoundError("USER_NOT_FOUND", f"User '{username}' not found.")
            conn.execute(
                """
                INSERT INTO api_tokens(token_id, user_id, name, token_hash, role, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token_id, str(user["user_id"]), name, sha256_hex(raw_token), role, expires_at, now_iso),
            )
        return token_id, raw_token

    def authenticate_bearer(self, bearer_token: str) -> UserProfile:
        now = self.clock.now()
        if bearer_token.startswith("flk_"):
            token_hash = sha256_hex(bearer_token)
            with self.db.read() as conn:
                row = conn.execute(
                    """
                    SELECT t.token_id, t.role AS token_role, t.expires_at, t.revoked_at,
                           u.user_id, u.username, u.display_name, u.created_at, u.last_login_at, u.is_active
                    FROM api_tokens t
                    JOIN users u ON u.user_id = t.user_id
                    WHERE t.token_hash = ?
                    """,
                    (token_hash,),
                ).fetchone()
            if row is None or row["revoked_at"] is not None or not row["is_active"]:
                raise UnauthorizedError("TOKEN_INVALID", "API token is invalid or revoked.")
            if row["expires_at"]:
                exp = parse_rfc3339(str(row["expires_at"]))
                if exp is not None and now >= exp:
                    raise UnauthorizedError("TOKEN_EXPIRED", "API token has expired.")
            return UserProfile(
                user_id=str(row["user_id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"]),
                role=row["token_role"],
                created_at=str(row["created_at"]),
                last_login_at=row["last_login_at"],
            )

        try:
            payload = decode_access_token(
                bearer_token,
                secret=self.settings.jwt_secret,
                clock=self.clock,
            )
        except TokenExpiredError as exc:
            raise UnauthorizedError("TOKEN_EXPIRED", "Access token has expired.") from exc
        except TokenInvalidError as exc:
            raise UnauthorizedError("TOKEN_INVALID", "Access token is invalid.") from exc

        user_id = str(payload["sub"])
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ? AND is_active = 1",
                (user_id,),
            ).fetchone()
        if row is None:
            raise UnauthorizedError("TOKEN_INVALID", "User account no longer active.")
        return UserProfile(
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=row["role"],
            created_at=str(row["created_at"]),
            last_login_at=row["last_login_at"],
        )

    # ------------------------------------------------------------------
    # Idempotency & Operation Construction Helpers (inside transaction)
    # ------------------------------------------------------------------
    def _check_idempotency(
        self,
        conn: sqlite3.Connection,
        *,
        idempotency_key: str | None,
        method: str,
        path: str,
        payload_hash: str,
    ) -> OperationDetail | None:
        if not idempotency_key:
            return None
        row = conn.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row["method"]) != method
            or str(row["path"]) != path
            or str(row["payload_sha256"]) != payload_hash
        ):
            raise ConflictError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency-Key was already used with a different request payload.",
            )
        return self._load_operation(conn, str(row["operation_id"]))

    def _insert_operation(
        self,
        conn: sqlite3.Connection,
        *,
        group_id: str,
        action: OperationAction,
        generation: int,
        requested_by: str,
        idempotency_key: str | None,
        method: str,
        path: str,
        payload: dict[str, Any],
        steps: list[tuple[str, str]],
    ) -> OperationDetail:
        op_id = new_operation_id()
        now_iso = self.clock.now_iso()
        conn.execute(
            """
            INSERT INTO operations(
                operation_id, group_id, action, status, generation, idempotency_key,
                requested_by, payload_json, retryable, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                op_id,
                group_id,
                action,
                generation,
                idempotency_key,
                requested_by,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now_iso,
                now_iso,
            ),
        )
        for idx, (step_name, step_status) in enumerate(steps, start=1):
            started = now_iso if step_status == "succeeded" else None
            finished = now_iso if step_status == "succeeded" else None
            conn.execute(
                """
                INSERT INTO operation_steps(operation_id, step_order, step_name, status, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (op_id, idx, step_name, step_status, started, finished),
            )
        if idempotency_key:
            conn.execute(
                """
                INSERT INTO idempotency_keys(idempotency_key, method, path, payload_sha256, operation_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (idempotency_key, method, path, canonical_payload_hash(payload), op_id, now_iso),
            )
        return self._load_operation(conn, op_id)

    def _load_operation(self, conn: sqlite3.Connection, operation_id: str) -> OperationDetail:
        row = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("OPERATION_NOT_FOUND", f"Operation '{operation_id}' not found.")
        step_rows = conn.execute(
            "SELECT * FROM operation_steps WHERE operation_id = ? ORDER BY step_order ASC",
            (operation_id,),
        ).fetchall()
        steps = [
            OperationStep(
                step_name=str(s["step_name"]),
                status=s["status"],
                started_at=s["started_at"],
                finished_at=s["finished_at"],
                detail=s["detail"],
                error_code=s["error_code"],
                error_message=s["error_message"],
            )
            for s in step_rows
        ]
        return OperationDetail(
            operation_id=str(row["operation_id"]),
            group_id=str(row["group_id"]),
            action=row["action"],
            status=row["status"],
            generation=int(row["generation"]),
            idempotency_key=row["idempotency_key"],
            requested_by=str(row["requested_by"]),
            retryable=bool(row["retryable"]),
            retry_hint=row["retry_hint"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            steps=steps,
            created_at=str(row["created_at"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            updated_at=str(row["updated_at"]),
        )

    def _ensure_ports_free_for_group(
        self,
        conn: sqlite3.Connection,
        *,
        group_id: str,
        dtu_port: int,
        gcs_port: int,
    ) -> None:
        if dtu_port == gcs_port:
            raise ValidationDomainError(
                "gcs_port must differ from dtu_port",
                details=[ErrorDetailItem(field="gcs_port", message="gcs_port cannot equal dtu_port")],
            )
        for field_name, port in (("dtu_port", dtu_port), ("gcs_port", gcs_port)):
            row = conn.execute(
                """
                SELECT pr.group_id, pg.name, pr.reservation_kind
                FROM port_reservations pr
                LEFT JOIN port_groups pg ON pg.group_id = pr.group_id
                WHERE pr.port = ? AND pr.group_id != ?
                LIMIT 1
                """,
                (port, group_id),
            ).fetchone()
            if row is not None:
                owner_name = row["name"] or row["group_id"]
                raise ConflictError(
                    "PORT_CONFLICT",
                    f"Port {port} is already reserved by port group '{owner_name}'.",
                    details=[ErrorDetailItem(field=field_name, message=f"Port {port} is in use")],
                )

    def _has_active_operation(self, conn: sqlite3.Connection, group_id: str) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT operation_id, action, status
            FROM operations
            WHERE group_id = ? AND status IN ('pending', 'running')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (group_id,),
        ).fetchone()

    # ------------------------------------------------------------------
    # Atomic Enqueue Methods (Single DB Transaction -> 202 Accepted)
    # ------------------------------------------------------------------
    def enqueue_create_group(
        self,
        req: CreateGroupRequest,
        *,
        requested_by: str,
        idempotency_key: str | None = None,
    ) -> OperationDetail:
        payload = {
            "name": req.name,
            "dtu_port": req.dtu_port,
            "gcs_protocol": req.gcs_protocol,
            "gcs_port": req.gcs_port,
        }
        payload_hash = canonical_payload_hash(payload)

        with self.db.write_transaction() as conn:
            existing = self._check_idempotency(
                conn,
                idempotency_key=idempotency_key,
                method="POST",
                path="/api/v1/groups",
                payload_hash=payload_hash,
            )
            if existing is not None:
                return existing

            active_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS cnt FROM port_groups WHERE deleted_at IS NULL"
                ).fetchone()["cnt"]
            )
            if active_count >= self.settings.max_groups:
                raise ConflictError(
                    "GROUP_LIMIT_REACHED",
                    f"Maximum of {self.settings.max_groups} port groups reached.",
                )

            dup_name = conn.execute(
                "SELECT group_id FROM port_groups WHERE name = ? AND deleted_at IS NULL",
                (req.name,),
            ).fetchone()
            if dup_name is not None:
                raise ConflictError(
                    "GROUP_NAME_CONFLICT",
                    f"Group name '{req.name}' is already in use.",
                    details=[ErrorDetailItem(field="name", message="Name already exists")],
                )

            group_id = new_group_id()
            self._ensure_ports_free_for_group(
                conn,
                group_id=group_id,
                dtu_port=req.dtu_port,
                gcs_port=req.gcs_port,
            )

            now_iso = self.clock.now_iso()
            conn.execute(
                """
                INSERT INTO port_groups(
                    group_id, name, dtu_protocol, dtu_port, gcs_protocol, gcs_port,
                    config_version, desired_state, lifecycle_state, operation_generation,
                    created_at, updated_at
                ) VALUES (?, ?, 'udp', ?, ?, ?, 1, 'stopped', 'configured_stopped', 1, ?, ?)
                """,
                (group_id, req.name, req.dtu_port, req.gcs_protocol, req.gcs_port, now_iso, now_iso),
            )
            # Reserve both ports globally in DB
            conn.execute(
                """
                INSERT INTO port_reservations(port, reservation_kind, group_id, role, protocol, created_at)
                VALUES (?, 'active', ?, 'dtu', 'udp', ?), (?, 'active', ?, 'gcs', ?, ?)
                """,
                (req.dtu_port, group_id, now_iso, req.gcs_port, group_id, req.gcs_protocol, now_iso),
            )
            conn.execute(
                """
                INSERT INTO group_runtime_status(group_id, router_state, mavproxy_state, sg_state, observed_at)
                VALUES (?, 'stopped', 'disconnected', 'syncing', ?)
                """,
                (group_id, now_iso),
            )
            # Independent archive identity created immediately (supports active-group empty state)
            conn.execute(
                """
                INSERT INTO archive_groups(
                    group_id, name_snapshot, dtu_protocol, dtu_port_snapshot,
                    gcs_protocol_snapshot, gcs_port_snapshot, deleted, created_at, updated_at
                ) VALUES (?, ?, 'udp', ?, ?, ?, 0, ?, ?)
                """,
                (group_id, req.name, req.dtu_port, req.gcs_protocol, req.gcs_port, now_iso, now_iso),
            )

            op = self._insert_operation(
                conn,
                group_id=group_id,
                action="create",
                generation=1,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                method="POST",
                path="/api/v1/groups",
                payload=payload,
                steps=[
                    ("reserve_resources", "succeeded"),
                    ("render_router_config", "pending"),
                    ("sync_security_group", "pending"),
                    ("verify_security_group", "pending"),
                ],
            )
            conn.execute(
                "UPDATE port_groups SET active_operation_id = ? WHERE group_id = ?",
                (op.operation_id, group_id),
            )
            return op

    def _compute_update_plan(
        self,
        conn: sqlite3.Connection,
        group_id: str,
        req: UpdateGroupRequest,
        *,
        if_match_version: int | None,
    ) -> tuple[sqlite3.Row, dict[str, Any], bool, bool, list[str]]:
        group = conn.execute(
            "SELECT * FROM port_groups WHERE group_id = ? AND deleted_at IS NULL",
            (group_id,),
        ).fetchone()
        if group is None:
            raise NotFoundError("GROUP_NOT_FOUND", f"Port group '{group_id}' not found.")

        if group["desired_state"] == "deleting" or group["lifecycle_state"] in ("deleting", "cleanup_pending"):
            raise ConflictError("GROUP_DELETING", "Port group is being deleted and cannot be modified.")

        expected_version = if_match_version if if_match_version is not None else req.expected_config_version
        if expected_version is not None and expected_version != int(group["config_version"]):
            raise PreconditionFailedError(
                "Group configuration version has changed; refresh group details before retrying.",
                details=[
                    ErrorDetailItem(
                        field="config_version",
                        message=f"expected={expected_version}, current={group['config_version']}",
                    )
                ],
            )

        new_name = req.name if req.name is not None else str(group["name"])
        new_dtu_port = req.dtu_port if req.dtu_port is not None else int(group["dtu_port"])
        new_gcs_protocol = req.gcs_protocol if req.gcs_protocol is not None else str(group["gcs_protocol"])
        new_gcs_port = req.gcs_port if req.gcs_port is not None else int(group["gcs_port"])

        if new_dtu_port == new_gcs_port:
            raise ValidationDomainError(
                "gcs_port must differ from dtu_port",
                details=[ErrorDetailItem(field="gcs_port", message="gcs_port cannot equal dtu_port")],
            )

        if new_name != str(group["name"]):
            dup = conn.execute(
                "SELECT group_id FROM port_groups WHERE name = ? AND group_id != ? AND deleted_at IS NULL",
                (new_name, group_id),
            ).fetchone()
            if dup is not None:
                raise ConflictError(
                    "GROUP_NAME_CONFLICT",
                    f"Group name '{new_name}' is already in use.",
                    details=[ErrorDetailItem(field="name", message="Name already exists")],
                )

        self._ensure_ports_free_for_group(
            conn,
            group_id=group_id,
            dtu_port=new_dtu_port,
            gcs_port=new_gcs_port,
        )

        endpoints_changed = (
            new_dtu_port != int(group["dtu_port"])
            or new_gcs_protocol != str(group["gcs_protocol"])
            or new_gcs_port != int(group["gcs_port"])
        )
        was_running = str(group["desired_state"]) == "running" and str(group["lifecycle_state"]) == "running"
        requires_restart = endpoints_changed and was_running
        requires_sg_sync = endpoints_changed

        planned_steps: list[str] = ["reserve_candidate"]
        if not endpoints_changed:
            planned_steps.append("apply_metadata_only")
        else:
            planned_steps.extend(["prepare_candidate_config", "authorize_candidate_sg"])
            if requires_restart:
                planned_steps.extend(["switch_router_to_candidate", "verify_candidate_listening"])
            planned_steps.extend(["commit_candidate_version", "cleanup_superseded_sg"])

        diff = {
            "name": {"from": str(group["name"]), "to": new_name},
            "dtu_port": {"from": int(group["dtu_port"]), "to": new_dtu_port},
            "gcs_protocol": {"from": str(group["gcs_protocol"]), "to": new_gcs_protocol},
            "gcs_port": {"from": int(group["gcs_port"]), "to": new_gcs_port},
        }
        return group, diff, requires_restart, requires_sg_sync, planned_steps

    def dry_run_update_group(
        self,
        group_id: str,
        req: UpdateGroupRequest,
        *,
        if_match_version: int | None = None,
    ) -> GroupUpdateDryRunResponse:
        with self.db.read() as conn:
            _, diff, requires_restart, requires_sg_sync, planned_steps = self._compute_update_plan(
                conn,
                group_id,
                req,
                if_match_version=if_match_version,
            )
            return GroupUpdateDryRunResponse(
                group_id=group_id,
                valid=True,
                requires_restart=requires_restart,
                requires_sg_sync=requires_sg_sync,
                planned_steps=planned_steps,
                diff=diff,
            )

    def enqueue_update_group(
        self,
        group_id: str,
        req: UpdateGroupRequest,
        *,
        requested_by: str,
        idempotency_key: str | None = None,
        if_match_version: int | None = None,
    ) -> OperationDetail:
        payload = {
            "group_id": group_id,
            "name": req.name,
            "dtu_port": req.dtu_port,
            "gcs_protocol": req.gcs_protocol,
            "gcs_port": req.gcs_port,
            "expected_config_version": if_match_version if if_match_version is not None else req.expected_config_version,
        }
        payload_hash = canonical_payload_hash(payload)
        path = f"/api/v1/groups/{group_id}"

        with self.db.write_transaction() as conn:
            existing = self._check_idempotency(
                conn,
                idempotency_key=idempotency_key,
                method="PATCH",
                path=path,
                payload_hash=payload_hash,
            )
            if existing is not None:
                return existing

            group, diff, requires_restart, requires_sg_sync, planned_steps = self._compute_update_plan(
                conn,
                group_id,
                req,
                if_match_version=if_match_version,
            )

            busy = self._has_active_operation(conn, group_id)
            if busy is not None:
                raise ConflictError(
                    "GROUP_BUSY",
                    f"Port group already has an active operation '{busy['operation_id']}'.",
                )

            now_iso = self.clock.now_iso()
            new_gen = int(group["operation_generation"]) + 1
            new_candidate_version = int(group["config_version"]) + 1
            new_name = diff["name"]["to"]
            new_dtu_port = int(diff["dtu_port"]["to"])
            new_gcs_protocol = str(diff["gcs_protocol"]["to"])
            new_gcs_port = int(diff["gcs_port"]["to"])

            candidate = {
                "name": new_name,
                "dtu_port": new_dtu_port,
                "gcs_protocol": new_gcs_protocol,
                "gcs_port": new_gcs_port,
                "requires_restart": requires_restart,
                "requires_sg_sync": requires_sg_sync,
                "previous": {
                    "name": str(group["name"]),
                    "dtu_port": int(group["dtu_port"]),
                    "gcs_protocol": str(group["gcs_protocol"]),
                    "gcs_port": int(group["gcs_port"]),
                    "config_version": int(group["config_version"]),
                    "lifecycle_state": str(group["lifecycle_state"]),
                },
            }

            if requires_sg_sync:
                # Reserve candidate ports alongside active ports (allowed for same group_id!)
                conn.execute(
                    "DELETE FROM port_reservations WHERE group_id = ? AND reservation_kind = 'candidate'",
                    (group_id,),
                )
                conn.execute(
                    """
                    INSERT INTO port_reservations(port, reservation_kind, group_id, role, protocol, created_at)
                    VALUES (?, 'candidate', ?, 'dtu', 'udp', ?), (?, 'candidate', ?, 'gcs', ?, ?)
                    """,
                    (new_dtu_port, group_id, now_iso, new_gcs_port, group_id, new_gcs_protocol, now_iso),
                )

            conn.execute(
                """
                UPDATE port_groups
                SET candidate_version = ?, candidate_json = ?, operation_generation = ?,
                    lifecycle_state = CASE WHEN ? THEN 'updating' ELSE lifecycle_state END,
                    updated_at = ?
                WHERE group_id = ?
                """,
                (
                    new_candidate_version,
                    json.dumps(candidate, ensure_ascii=False),
                    new_gen,
                    1 if requires_sg_sync else 0,
                    now_iso,
                    group_id,
                ),
            )

            steps = [(s, "succeeded" if s == "reserve_candidate" else "pending") for s in planned_steps]
            op = self._insert_operation(
                conn,
                group_id=group_id,
                action="update",
                generation=new_gen,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                method="PATCH",
                path=path,
                payload={**payload, "candidate": candidate},
                steps=steps,
            )
            conn.execute(
                "UPDATE port_groups SET active_operation_id = ? WHERE group_id = ?",
                (op.operation_id, group_id),
            )
            return op

    def enqueue_delete_group(
        self,
        group_id: str,
        *,
        requested_by: str,
        idempotency_key: str | None = None,
        if_match_version: int | None = None,
    ) -> OperationDetail:
        payload = {"group_id": group_id, "expected_config_version": if_match_version}
        payload_hash = canonical_payload_hash(payload)
        path = f"/api/v1/groups/{group_id}"

        with self.db.write_transaction() as conn:
            existing = self._check_idempotency(
                conn,
                idempotency_key=idempotency_key,
                method="DELETE",
                path=path,
                payload_hash=payload_hash,
            )
            if existing is not None:
                return existing

            group = conn.execute(
                "SELECT * FROM port_groups WHERE group_id = ? AND deleted_at IS NULL",
                (group_id,),
            ).fetchone()
            if group is None:
                raise NotFoundError("GROUP_NOT_FOUND", f"Port group '{group_id}' not found.")

            if if_match_version is not None and if_match_version != int(group["config_version"]):
                raise PreconditionFailedError(
                    "Group configuration version mismatch.",
                    details=[
                        ErrorDetailItem(
                            field="config_version",
                            message=f"expected={if_match_version}, current={group['config_version']}",
                        )
                    ],
                )

            now_iso = self.clock.now_iso()
            # FIRST TRANSACTION: mark deleting, revoke start intent, bump generation to fence old tasks!
            new_gen = int(group["operation_generation"]) + 1
            conn.execute(
                """
                UPDATE operations
                SET status = 'cancelled', finished_at = ?, updated_at = ?,
                    error_code = 'SUPERSEDED_BY_DELETE',
                    error_message = 'Cancelled because group deletion was requested.'
                WHERE group_id = ? AND status = 'pending' AND action != 'delete'
                """,
                (now_iso, now_iso, group_id),
            )
            conn.execute(
                """
                UPDATE port_groups
                SET desired_state = 'deleting', lifecycle_state = 'deleting',
                    operation_generation = ?, candidate_version = NULL, candidate_json = NULL,
                    updated_at = ?
                WHERE group_id = ?
                """,
                (new_gen, now_iso, group_id),
            )
            # Mark all port reservations as releasing so no other group can grab them before verification
            conn.execute(
                """
                UPDATE port_reservations
                SET reservation_kind = 'releasing'
                WHERE group_id = ? AND reservation_kind = 'active'
                """,
                (group_id,),
            )
            conn.execute(
                "DELETE FROM port_reservations WHERE group_id = ? AND reservation_kind = 'candidate'",
                (group_id,),
            )
            # Snapshot latest name/ports to archive_groups immediately
            conn.execute(
                """
                UPDATE archive_groups
                SET name_snapshot = ?, dtu_port_snapshot = ?, gcs_protocol_snapshot = ?,
                    gcs_port_snapshot = ?, updated_at = ?
                WHERE group_id = ?
                """,
                (
                    group["name"],
                    group["dtu_port"],
                    group["gcs_protocol"],
                    group["gcs_port"],
                    now_iso,
                    group_id,
                ),
            )

            op = self._insert_operation(
                conn,
                group_id=group_id,
                action="delete",
                generation=new_gen,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                method="DELETE",
                path=path,
                payload=payload,
                steps=[
                    ("mark_deleting_and_fence", "succeeded"),
                    ("stop_channel_processes", "pending"),
                    ("revoke_security_group_rules", "pending"),
                    ("verify_cleanup_and_release_ports", "pending"),
                    ("seal_active_archive", "pending"),
                ],
            )
            conn.execute(
                "UPDATE port_groups SET active_operation_id = ? WHERE group_id = ?",
                (op.operation_id, group_id),
            )
            return op

    def enqueue_group_action(
        self,
        group_id: str,
        action_slug: str,
        *,
        requested_by: str,
        idempotency_key: str | None = None,
        if_match_version: int | None = None,
        expected_config_version: int | None = None,
    ) -> OperationDetail:
        action_map: dict[str, OperationAction] = {
            "start": "start",
            "stop": "stop",
            "restart": "restart",
            "sync-security-group": "sync_security_group",
            "verify": "verify",
        }
        if action_slug not in action_map:
            raise ValidationDomainError(f"Unsupported action '{action_slug}'.")
        action = action_map[action_slug]
        exp_ver = if_match_version if if_match_version is not None else expected_config_version
        payload = {"group_id": group_id, "action": action, "expected_config_version": exp_ver}
        payload_hash = canonical_payload_hash(payload)
        path = f"/api/v1/groups/{group_id}/actions/{action_slug}"

        with self.db.write_transaction() as conn:
            existing = self._check_idempotency(
                conn,
                idempotency_key=idempotency_key,
                method="POST",
                path=path,
                payload_hash=payload_hash,
            )
            if existing is not None:
                return existing

            group = conn.execute(
                "SELECT * FROM port_groups WHERE group_id = ? AND deleted_at IS NULL",
                (group_id,),
            ).fetchone()
            if group is None:
                raise NotFoundError("GROUP_NOT_FOUND", f"Port group '{group_id}' not found.")

            if exp_ver is not None and exp_ver != int(group["config_version"]):
                raise PreconditionFailedError(
                    "Group configuration version mismatch.",
                    details=[
                        ErrorDetailItem(
                            field="config_version",
                            message=f"expected={exp_ver}, current={group['config_version']}",
                        )
                    ],
                )

            if group["desired_state"] == "deleting" or group["lifecycle_state"] in ("deleting", "cleanup_pending"):
                if action != "verify":
                    raise ConflictError(
                        "GROUP_DELETING",
                        "Port group is deleting/cleanup_pending and cannot be started or modified.",
                    )

            if action in ("start", "restart") and group["lifecycle_state"] == "needs_verification":
                raise ConflictError(
                    "INVALID_STATE_TRANSITION",
                    "Port group requires verification before starting.",
                )

            now_iso = self.clock.now_iso()
            new_gen = int(group["operation_generation"]) + 1

            if action == "stop":
                # Persist forbidden-to-run intent FIRST and fence any queued start/restart tasks!
                conn.execute(
                    """
                    UPDATE operations
                    SET status = 'cancelled', finished_at = ?, updated_at = ?,
                        error_code = 'SUPERSEDED_BY_STOP',
                        error_message = 'Cancelled because stop was requested.'
                    WHERE group_id = ? AND status = 'pending' AND action IN ('start', 'restart')
                    """,
                    (now_iso, now_iso, group_id),
                )
                conn.execute(
                    """
                    UPDATE port_groups
                    SET desired_state = 'stopped', lifecycle_state = 'stopping',
                        operation_generation = ?, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (new_gen, now_iso, group_id),
                )
                steps = [
                    ("persist_stop_intent", "succeeded"),
                    ("stop_channel_processes", "pending"),
                    ("verify_stopped", "pending"),
                ]
            elif action in ("start", "restart"):
                busy = self._has_active_operation(conn, group_id)
                if busy is not None:
                    raise ConflictError(
                        "GROUP_BUSY",
                        f"Port group already has active operation '{busy['operation_id']}'.",
                    )
                conn.execute(
                    """
                    UPDATE port_groups
                    SET desired_state = 'running', lifecycle_state = 'starting',
                        operation_generation = ?, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (new_gen, now_iso, group_id),
                )
                steps = [
                    ("preflight_checks", "pending"),
                    ("ensure_router_config", "pending"),
                    ("start_router_and_verify_bind", "pending"),
                    ("start_mavproxy_bypass", "pending"),
                ]
            elif action == "sync_security_group":
                busy = self._has_active_operation(conn, group_id)
                if busy is not None:
                    raise ConflictError(
                        "GROUP_BUSY",
                        f"Port group already has active operation '{busy['operation_id']}'.",
                    )
                steps = [
                    ("reconcile_security_group", "pending"),
                    ("verify_security_group", "pending"),
                ]
            else:  # verify
                steps = [
                    ("inspect_actual_runtime", "pending"),
                    ("reconcile_state", "pending"),
                ]

            op = self._insert_operation(
                conn,
                group_id=group_id,
                action=action,
                generation=new_gen,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                method="POST",
                path=path,
                payload=payload,
                steps=steps,
            )
            conn.execute(
                "UPDATE port_groups SET active_operation_id = ? WHERE group_id = ?",
                (op.operation_id, group_id),
            )
            return op

    def enqueue_retry_operation(
        self,
        operation_id: str,
        *,
        requested_by: str,
        idempotency_key: str | None = None,
    ) -> OperationDetail:
        path = f"/api/v1/operations/{operation_id}/retry"
        payload = {"retry_of": operation_id}
        payload_hash = canonical_payload_hash(payload)

        with self.db.write_transaction() as conn:
            existing = self._check_idempotency(
                conn,
                idempotency_key=idempotency_key,
                method="POST",
                path=path,
                payload_hash=payload_hash,
            )
            if existing is not None:
                return existing

            orig = conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if orig is None:
                raise NotFoundError("OPERATION_NOT_FOUND", f"Operation '{operation_id}' not found.")
            if orig["status"] not in ("failed", "needs_verification") or not orig["retryable"]:
                raise ConflictError(
                    "OPERATION_NOT_RETRYABLE",
                    f"Operation '{operation_id}' in status '{orig['status']}' cannot be retried.",
                )

            group_id = str(orig["group_id"])
            group = conn.execute(
                "SELECT * FROM port_groups WHERE group_id = ? AND deleted_at IS NULL",
                (group_id,),
            ).fetchone()
            if group is None:
                raise NotFoundError("GROUP_NOT_FOUND", f"Port group '{group_id}' already deleted.")

            # Deleting/cleanup_pending groups can ONLY continue delete/cleanup, NEVER resurrect!
            action: OperationAction = orig["action"]
            if group["desired_state"] == "deleting" or group["lifecycle_state"] in ("deleting", "cleanup_pending"):
                if action != "delete":
                    raise ConflictError(
                        "GROUP_DELETING",
                        "Cannot retry non-delete operation on a deleting/cleanup_pending group.",
                    )

            now_iso = self.clock.now_iso()
            new_gen = int(group["operation_generation"]) + 1
            conn.execute(
                "UPDATE port_groups SET operation_generation = ?, updated_at = ? WHERE group_id = ?",
                (new_gen, now_iso, group_id),
            )
            orig_payload = json.loads(str(orig["payload_json"]))
            orig_payload["retry_of"] = operation_id

            step_rows = conn.execute(
                "SELECT step_name, status FROM operation_steps WHERE operation_id = ? ORDER BY step_order ASC",
                (operation_id,),
            ).fetchall()
            steps = [
                (str(s["step_name"]), "pending" if s["status"] != "succeeded" else "succeeded")
                for s in step_rows
            ]
            op = self._insert_operation(
                conn,
                group_id=group_id,
                action=action,
                generation=new_gen,
                requested_by=requested_by,
                idempotency_key=idempotency_key,
                method="POST",
                path=path,
                payload=orig_payload,
                steps=steps,
            )
            conn.execute(
                "UPDATE port_groups SET active_operation_id = ? WHERE group_id = ?",
                (op.operation_id, group_id),
            )
            return op

    # ------------------------------------------------------------------
    # Strictly Read-Only Queries (ZERO side effects!)
    # ------------------------------------------------------------------
    def _is_stale(self, observed_at: str | None) -> bool:
        if not observed_at:
            return True
        dt = parse_rfc3339(observed_at)
        if dt is None:
            return True
        age = (self.clock.now() - dt).total_seconds()
        return age > self.settings.stale_after_seconds

    def _build_group_summary(self, conn: sqlite3.Connection, row: sqlite3.Row) -> GroupSummary:
        group_id = str(row["group_id"])
        rt = conn.execute(
            "SELECT * FROM group_runtime_status WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        rule_rows = conn.execute(
            "SELECT * FROM security_group_rules WHERE group_id = ? ORDER BY id ASC",
            (group_id,),
        ).fetchall()

        rules = [
            SecurityGroupRuleView(
                rule_id=r["cloud_rule_id"],
                role=r["role"],
                protocol=r["protocol"],
                port=int(r["port"]),
                source_cidr=str(r["source_cidr"]),
                status=r["status"],
            )
            for r in rule_rows
        ]
        observed_at = str(rt["observed_at"]) if rt else str(row["updated_at"])
        router_obs = rt["router_observed_at"] if rt else None
        mav_obs = rt["mavproxy_observed_at"] if rt else None
        sg_obs = rt["sg_observed_at"] if rt else None

        return GroupSummary(
            group_id=group_id,
            name=str(row["name"]),
            dtu_protocol="udp",
            dtu_port=int(row["dtu_port"]),
            gcs_protocol=row["gcs_protocol"],
            gcs_port=int(row["gcs_port"]),
            config_version=int(row["config_version"]),
            candidate_version=row["candidate_version"],
            desired_state=row["desired_state"],
            lifecycle_state=row["lifecycle_state"],
            operation_generation=int(row["operation_generation"]),
            active_operation_id=row["active_operation_id"],
            verification_reason=row["verification_reason"],
            sysid=rt["sysid"] if rt else None,
            fc_state=rt["fc_state"] if rt else "offline",
            dtu_peer=rt["dtu_peer"] if rt else None,
            gcs_state=rt["gcs_state"] if rt else "disconnected",
            gcs_peer=rt["gcs_peer"] if rt else None,
            link_quality_pct=rt["link_quality_pct"] if rt else None,
            rtt_ms=rt["rtt_ms"] if rt else None,
            last_activity_at=rt["last_activity_at"] if rt else None,
            router=RouterServiceStatus(
                state=rt["router_state"] if rt else "stopped",
                unit_name=rt["router_unit_name"] if rt else f"flightlink-router@{group_id}.service",
                pid=rt["router_pid"] if rt else None,
                uptime_seconds=rt["router_uptime_seconds"] if rt else None,
                bound_endpoints=json.loads(rt["router_bound_endpoints_json"]) if rt else [],
                verified_listening=bool(rt["router_verified_listening"]) if rt else False,
                observed_at=router_obs,
                stale=self._is_stale(router_obs) if router_obs else False,
                detail=rt["router_detail"] if rt else None,
            ),
            mavproxy=MavproxyServiceStatus(
                state=rt["mavproxy_state"] if rt else "disconnected",
                unit_name=rt["mavproxy_unit_name"] if rt else f"flightlink-mavproxy@{group_id}.service",
                pid=rt["mavproxy_pid"] if rt else None,
                uptime_seconds=rt["mavproxy_uptime_seconds"] if rt else None,
                observed_at=mav_obs,
                stale=self._is_stale(mav_obs) if mav_obs else False,
                detail=rt["mavproxy_detail"] if rt else None,
            ),
            security_group=SecurityGroupServiceStatus(
                state=rt["sg_state"] if rt else "unconfigured",
                security_group_id=rt["sg_id"] if rt else self.settings.aliyun_security_group_id,
                owned_rule_count=sum(1 for r in rules if r.status == "present"),
                rules=rules,
                observed_at=sg_obs,
                stale=self._is_stale(sg_obs) if sg_obs else False,
                detail=rt["sg_detail"] if rt else None,
            ),
            observed_at=observed_at,
            stale=self._is_stale(observed_at),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def list_groups(self, *, q: str | None = None, filter_mode: str = "all") -> GroupListResponse:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM port_groups WHERE deleted_at IS NULL ORDER BY created_at ASC"
            ).fetchall()
            summaries = [self._build_group_summary(conn, r) for r in rows]

        if q:
            needle = q.strip().lower()
            summaries = [
                g
                for g in summaries
                if needle in g.name.lower()
                or needle in str(g.dtu_port)
                or needle in str(g.gcs_port)
                or (g.sysid is not None and needle in str(g.sysid))
            ]

        if filter_mode == "online":
            summaries = [g for g in summaries if g.router.state == "running" and g.fc_state == "online"]
        elif filter_mode == "idle":
            summaries = [g for g in summaries if not (g.router.state == "running" and g.fc_state == "online")]
        elif filter_mode == "attention":
            summaries = [
                g
                for g in summaries
                if g.lifecycle_state in ("needs_verification", "error", "cleanup_pending")
                or g.security_group.state in ("drift", "needs_verification", "error")
            ]

        return GroupListResponse(
            items=summaries,
            total=len(summaries),
            max_groups=5,
            observed_at=self.clock.now_iso(),
        )

    def get_group_detail(self, group_id: str) -> GroupDetail:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM port_groups WHERE group_id = ? AND deleted_at IS NULL",
                (group_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    "GROUP_NOT_FOUND",
                    f"Port group '{group_id}' does not exist or has been deleted.",
                )
            summary = self._build_group_summary(conn, row)
            rt = conn.execute(
                "SELECT telemetry_json, telemetry_observed_at, recent_gcs_commands_json FROM group_runtime_status WHERE group_id = ?",
                (group_id,),
            ).fetchone()

        raw_tele = json.loads(rt["telemetry_json"]) if rt and rt["telemetry_json"] else {}
        tele_obs = rt["telemetry_observed_at"] if rt else None
        # Strictly return None for any missing telemetry field (never fabricate 0/false)
        telemetry = TelemetrySnapshot(
            flight_mode=raw_tele.get("flight_mode"),
            armed=raw_tele.get("armed"),
            battery_remaining_pct=raw_tele.get("battery_remaining_pct"),
            battery_voltage_v=raw_tele.get("battery_voltage_v"),
            gps_satellites=raw_tele.get("gps_satellites"),
            gps_fix_type=raw_tele.get("gps_fix_type"),
            gps_hdop=raw_tele.get("gps_hdop"),
            altitude_msl_m=raw_tele.get("altitude_msl_m"),
            relative_altitude_m=raw_tele.get("relative_altitude_m"),
            ground_speed_mps=raw_tele.get("ground_speed_mps"),
            rx_rate_kbps=raw_tele.get("rx_rate_kbps"),
            tx_rate_kbps=raw_tele.get("tx_rate_kbps"),
            packets_lost=raw_tele.get("packets_lost"),
            packets_received=raw_tele.get("packets_received"),
            packet_loss_pct=raw_tele.get("packet_loss_pct"),
            observed_at=tele_obs,
            stale=self._is_stale(tele_obs) if tele_obs else False,
        )
        raw_cmds = json.loads(rt["recent_gcs_commands_json"]) if rt and rt["recent_gcs_commands_json"] else []
        cmds = [ObservedGcsCommand(**c) for c in raw_cmds]

        return GroupDetail(
            **summary.model_dump(),
            telemetry=telemetry,
            recent_gcs_commands=cmds,
        )

    def get_overview(self) -> OverviewResponse:
        now_iso = self.clock.now_iso()
        ecs_instance_id = self.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"
        groups_resp = self.list_groups()
        groups = groups_resp.items

        with self.db.read() as conn:
            hb_rows = {
                str(r["component_name"]): r
                for r in conn.execute("SELECT * FROM component_heartbeats").fetchall()
            }

        components: list[ComponentHealth] = []
        any_stale = False
        for name in ("flightlink-api", "flightlink-agent", "flightlink-archive"):
            if name == "flightlink-api":
                components.append(
                    ComponentHealth(
                        name="flightlink-api",
                        status="ok",
                        observed_at=now_iso,
                        stale=False,
                        detail="Read/control API ready",
                    )
                )
            elif name in hb_rows:
                hb = hb_rows[name]
                stale = self._is_stale(str(hb["observed_at"]))
                if stale:
                    any_stale = True
                components.append(
                    ComponentHealth(
                        name=name,  # type: ignore[arg-type]
                        status=hb["status"],
                        observed_at=str(hb["observed_at"]),
                        stale=stale,
                        detail=hb["detail"],
                    )
                )
            else:
                any_stale = True
                components.append(
                    ComponentHealth(
                        name=name,  # type: ignore[arg-type]
                        status="unknown",
                        observed_at=None,
                        stale=True,
                        detail="No heartbeat recorded yet",
                    )
                )

        disk = self._inspect_disk_quota(now_iso)
        metrics = OverviewMetrics(
            group_count=len(groups),
            max_groups=5,
            running_router_count=sum(1 for g in groups if g.router.state == "running"),
            online_fc_count=sum(1 for g in groups if g.fc_state == "online"),
            needs_verification_count=sum(
                1 for g in groups if g.lifecycle_state == "needs_verification"
            ),
        )
        return OverviewResponse(
            ecs_instance_id=ecs_instance_id,
            observed_at=now_iso,
            stale=any_stale,
            stale_after_seconds=self.settings.stale_after_seconds,
            components=components,
            metrics=metrics,
            disk=disk,
            groups=groups,
        )

    def _inspect_disk_quota(self, now_iso: str) -> DiskQuotaStatus:
        archive_dir = self.settings.resolved_archive_dir
        probe_path: Path = archive_dir if archive_dir.exists() else archive_dir.parent
        while not probe_path.exists() and probe_path.parent != probe_path:
            probe_path = probe_path.parent
        try:
            usage = shutil.disk_usage(probe_path)
            total_bytes = int(usage.total)
            free_bytes = int(usage.free)
        except OSError:
            total_bytes = None
            free_bytes = None

        archive_used = 0
        if archive_dir.exists():
            for p in archive_dir.rglob("*"):
                if p.is_file():
                    try:
                        archive_used += p.stat().st_size
                    except OSError:
                        pass

        status = "ok"
        if (
            (free_bytes is not None and free_bytes <= self.settings.disk_min_free_bytes)
            or archive_used >= self.settings.archive_max_bytes
        ):
            status = "degraded_quota_exceeded"
        elif free_bytes is not None and free_bytes <= self.settings.disk_warn_free_bytes:
            status = "warning"

        return DiskQuotaStatus(
            archive_dir=str(archive_dir),
            status=status,  # type: ignore[arg-type]
            total_bytes=total_bytes,
            free_bytes=free_bytes,
            archive_used_bytes=archive_used,
            observed_at=now_iso,
        )

    def get_operation(self, operation_id: str) -> OperationDetail:
        with self.db.read() as conn:
            return self._load_operation(conn, operation_id)

    def list_operations(
        self,
        *,
        group_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> OperationListResponse:
        clauses: list[str] = []
        params: list[Any] = []
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if cursor:
            clauses.append("created_at < ?")
            params.append(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit + 1)

        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT operation_id, created_at FROM operations {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            has_more = len(rows) > limit
            sliced = rows[:limit]
            items = [self._load_operation(conn, str(r["operation_id"])) for r in sliced]
            next_cursor = str(sliced[-1]["created_at"]) if has_more and sliced else None
        return OperationListResponse(items=items, next_cursor=next_cursor, has_more=has_more)
