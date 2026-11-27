"""Channel lifecycle & crash-recovery execution engine (`flightlink-agent`).

Key invariants:
- Single executor for systemd / router configs / bypass MAVProxy / dedicated Aliyun SG.
- Per-group serial execution via per-group locks.
- NEVER wraps cloud API calls or systemd commands inside SQLite transactions.
- Every external mutation persists ownership/intent first and can be reconciled on restart.
- Never auto-starts groups on config file discovery or host reboot.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Literal

from flightlink.adapters.interfaces import (
    CloudAuthTemporaryError,
    CloudSecurityGroupRule,
    CloudTimeoutError,
    InfrastructureError,
    PortPreemptedError,
    ProcessSupervisor,
    SecurityGroupClient,
    SocketInspector,
    build_ownership_marker,
)
from flightlink.adapters.router_config import (
    derive_internal_tap_port,
    remove_router_config,
    write_router_config_atomic,
)
from flightlink.archive.service import ArchiveService
from flightlink.clock import Clock, SystemClock
from flightlink.config import Settings
from flightlink.db import Database


class LifecycleAgent:
    """Executes persisted operations per group serially and reconciles runtime reality."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        supervisor: ProcessSupervisor,
        sockets: SocketInspector,
        sg_client: SecurityGroupClient,
        archive_service: ArchiveService | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.supervisor = supervisor
        self.sockets = sockets
        self.sg_client = sg_client
        self.archive_service = archive_service
        self.clock = clock or db.clock or SystemClock()
        self._group_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, group_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._group_locks.get(group_id)
            if lock is None:
                lock = threading.Lock()
                self._group_locks[group_id] = lock
            return lock

    def _ecs_instance_id(self) -> str:
        return self.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"

    def _sg_config(self) -> tuple[str | None, list[str], list[str]]:
        sg_id = self.db.get_meta("aliyun_security_group_id") or self.settings.aliyun_security_group_id
        dtu_cidrs = self.settings.dtu_allowed_cidrs or list(self.db.get_meta_json("dtu_allowed_cidrs", []) or [])
        gcs_cidrs = self.settings.gcs_allowed_cidrs or list(self.db.get_meta_json("gcs_allowed_cidrs", []) or [])
        return sg_id, list(dtu_cidrs), list(gcs_cidrs)

    def _desired_sg_specs(
        self,
        *,
        group_id: str,
        dtu_port: int,
        gcs_protocol: Literal["tcp", "udp"],
        gcs_port: int,
    ) -> list[dict[str, Any]]:
        sg_id, dtu_cidrs, gcs_cidrs = self._sg_config()
        if not sg_id:
            return []
        ecs_id = self._ecs_instance_id()
        specs: list[dict[str, Any]] = []
        for cidr in dtu_cidrs:
            specs.append(
                {
                    "role": "dtu",
                    "protocol": "udp",
                    "port": dtu_port,
                    "source_cidr": cidr,
                    "security_group_id": sg_id,
                    "ownership_marker": build_ownership_marker(ecs_id, group_id, "dtu"),
                }
            )
        for cidr in gcs_cidrs:
            specs.append(
                {
                    "role": "gcs",
                    "protocol": gcs_protocol,
                    "port": gcs_port,
                    "source_cidr": cidr,
                    "security_group_id": sg_id,
                    "ownership_marker": build_ownership_marker(ecs_id, group_id, "gcs"),
                }
            )
        return specs

    # ------------------------------------------------------------------
    # Step & Operation State Helpers
    # ------------------------------------------------------------------
    def _set_step(
        self,
        operation_id: str,
        step_name: str,
        status: str,
        *,
        detail: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            started = now_iso if status == "running" else None
            finished = now_iso if status in ("succeeded", "failed", "needs_verification", "skipped") else None
            conn.execute(
                """
                UPDATE operation_steps
                SET status = ?,
                    started_at = COALESCE(started_at, ?),
                    finished_at = COALESCE(?, finished_at),
                    detail = COALESCE(?, detail),
                    error_code = ?,
                    error_message = ?
                WHERE operation_id = ? AND step_name = ?
                """,
                (status, started, finished, detail, error_code, error_message, operation_id, step_name),
            )
            conn.execute(
                "UPDATE operations SET updated_at = ? WHERE operation_id = ?",
                (now_iso, operation_id),
            )

    def _finish_operation(
        self,
        operation_id: str,
        status: str,
        *,
        retryable: bool = False,
        retry_hint: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            conn.execute(
                """
                UPDATE operations
                SET status = ?, retryable = ?, retry_hint = ?,
                    error_code = ?, error_message = ?,
                    finished_at = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    status,
                    1 if retryable else 0,
                    retry_hint,
                    error_code,
                    error_message,
                    now_iso,
                    now_iso,
                    operation_id,
                ),
            )
            conn.execute(
                """
                UPDATE port_groups
                SET active_operation_id = CASE WHEN active_operation_id = ? THEN NULL ELSE active_operation_id END,
                    updated_at = ?
                WHERE active_operation_id = ?
                """,
                (operation_id, now_iso, operation_id),
            )

    def _is_generation_current(self, group_id: str, op_generation: int, *, allow_deleting: bool = False) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT operation_generation, desired_state, deleted_at FROM port_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if row is None or row["deleted_at"] is not None:
                return False
            if not allow_deleting and row["desired_state"] == "deleting":
                return False
            return int(row["operation_generation"]) == op_generation

    # ------------------------------------------------------------------
    # Cloud Security Group Helpers (OUTSIDE DB transactions!)
    # ------------------------------------------------------------------
    def _reconcile_authorize_specs(
        self,
        *,
        group_id: str,
        specs: list[dict[str, Any]],
    ) -> tuple[bool, str | None]:
        """Authorize desired SG specs with timeout query-first reconciliation.

        Returns (verified_ok, error_detail).
        """
        sg_id, _, _ = self._sg_config()
        if not sg_id:
            # No cloud SG bound yet -> mark unconfigured cleanly (valid offline / local mode)
            with self.db.write_transaction() as conn:
                conn.execute(
                    "UPDATE group_runtime_status SET sg_state = 'unconfigured', sg_observed_at = ?, sg_detail = ? WHERE group_id = ?",
                    (self.clock.now_iso(), "No security group bound", group_id),
                )
            return True, None

        if not specs:
            # SG bound, but CIDRs not explicitly configured -> do NOT open 0.0.0.0/0 implicitly!
            with self.db.write_transaction() as conn:
                conn.execute(
                    "UPDATE group_runtime_status SET sg_state = 'needs_verification', sg_id = ?, sg_observed_at = ?, sg_detail = ? WHERE group_id = ?",
                    (sg_id, self.clock.now_iso(), "Explicit DTU/GCS source CIDRs are not configured", group_id),
                )
            return False, "DTU/GCS allowed CIDRs must be explicitly configured before authorizing cloud rules"

        now_iso = self.clock.now_iso()
        # Persist pending_create ledger rows before calling cloud API
        with self.db.write_transaction() as conn:
            for spec in specs:
                existing = conn.execute(
                    """
                    SELECT id FROM security_group_rules
                    WHERE group_id = ? AND role = ? AND protocol = ? AND port = ? AND source_cidr = ? AND security_group_id = ?
                    """,
                    (
                        group_id,
                        spec["role"],
                        spec["protocol"],
                        spec["port"],
                        spec["source_cidr"],
                        spec["security_group_id"],
                    ),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO security_group_rules(
                            group_id, role, protocol, port, source_cidr, security_group_id,
                            ownership_marker, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_create', ?, ?)
                        """,
                        (
                            group_id,
                            spec["role"],
                            spec["protocol"],
                            spec["port"],
                            spec["source_cidr"],
                            spec["security_group_id"],
                            spec["ownership_marker"],
                            now_iso,
                            now_iso,
                        ),
                    )

        # Call cloud API OUTSIDE DB transaction
        timeout_or_auth_err: str | None = None
        for spec in specs:
            try:
                rule = self.sg_client.authorize_rule(
                    security_group_id=spec["security_group_id"],
                    protocol=spec["protocol"],
                    port=spec["port"],
                    source_cidr=spec["source_cidr"],
                    description=spec["ownership_marker"],
                )
                with self.db.write_transaction() as conn:
                    conn.execute(
                        """
                        UPDATE security_group_rules
                        SET cloud_rule_id = ?, status = 'present', updated_at = ?, last_verified_at = ?
                        WHERE group_id = ? AND role = ? AND protocol = ? AND port = ? AND source_cidr = ? AND security_group_id = ?
                        """,
                        (
                            rule.rule_id,
                            self.clock.now_iso(),
                            self.clock.now_iso(),
                            group_id,
                            spec["role"],
                            spec["protocol"],
                            spec["port"],
                            spec["source_cidr"],
                            spec["security_group_id"],
                        ),
                    )
            except (CloudTimeoutError, CloudAuthTemporaryError) as exc:
                timeout_or_auth_err = exc.message
                # Timeout -> query live cloud rules to see if rule was actually created!
                break

        # Re-query actual cloud rules to verify ownership & presence
        try:
            live_rules = self.sg_client.list_rules(security_group_id=sg_id)
        except (CloudTimeoutError, CloudAuthTemporaryError) as exc:
            with self.db.write_transaction() as conn:
                conn.execute(
                    "UPDATE group_runtime_status SET sg_state = 'needs_verification', sg_id = ?, sg_observed_at = ?, sg_detail = ? WHERE group_id = ?",
                    (sg_id, self.clock.now_iso(), exc.message, group_id),
                )
            return False, exc.message

        matched_all = True
        now_verify = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            for spec in specs:
                match = next(
                    (
                        r
                        for r in live_rules
                        if r.protocol == spec["protocol"]
                        and r.port == spec["port"]
                        and r.source_cidr == spec["source_cidr"]
                        and r.description == spec["ownership_marker"]
                    ),
                    None,
                )
                if match is not None:
                    conn.execute(
                        """
                        UPDATE security_group_rules
                        SET cloud_rule_id = ?, status = 'present', updated_at = ?, last_verified_at = ?
                        WHERE group_id = ? AND role = ? AND protocol = ? AND port = ? AND source_cidr = ? AND security_group_id = ?
                        """,
                        (
                            match.rule_id,
                            now_verify,
                            now_verify,
                            group_id,
                            spec["role"],
                            spec["protocol"],
                            spec["port"],
                            spec["source_cidr"],
                            spec["security_group_id"],
                        ),
                    )
                else:
                    matched_all = False
                    conn.execute(
                        """
                        UPDATE security_group_rules
                        SET status = 'unknown', updated_at = ?
                        WHERE group_id = ? AND role = ? AND protocol = ? AND port = ? AND source_cidr = ? AND security_group_id = ?
                        """,
                        (
                            now_verify,
                            group_id,
                            spec["role"],
                            spec["protocol"],
                            spec["port"],
                            spec["source_cidr"],
                            spec["security_group_id"],
                        ),
                    )
            conn.execute(
                "UPDATE group_runtime_status SET sg_state = ?, sg_id = ?, sg_observed_at = ?, sg_detail = ? WHERE group_id = ?",
                (
                    "synced" if matched_all else "needs_verification",
                    sg_id,
                    now_verify,
                    None if matched_all else (timeout_or_auth_err or "Cloud rules verification incomplete"),
                    group_id,
                ),
            )
        return matched_all, None if matched_all else (timeout_or_auth_err or "Cloud rules not confirmed")

    def _revoke_owned_rules(
        self,
        *,
        group_id: str,
        keep_keys: set[tuple[str, str, int, str]] | None = None,
    ) -> tuple[bool, str | None]:
        """Revoke cloud rules owned by `group_id` (excluding `keep_keys`), verifying clear via live list."""
        sg_id, _, _ = self._sg_config()
        if not sg_id:
            with self.db.write_transaction() as conn:
                conn.execute("DELETE FROM security_group_rules WHERE group_id = ?", (group_id,))
            return True, None

        ecs_id = self._ecs_instance_id()
        owner_prefix = f"flightlink:v1:{ecs_id}:{group_id}:"
        keep = keep_keys or set()

        try:
            live_rules = self.sg_client.list_rules(security_group_id=sg_id)
        except (CloudTimeoutError, CloudAuthTemporaryError) as exc:
            with self.db.write_transaction() as conn:
                conn.execute(
                    "UPDATE group_runtime_status SET sg_state = 'needs_verification', sg_id = ?, sg_observed_at = ?, sg_detail = ? WHERE group_id = ?",
                    (sg_id, self.clock.now_iso(), exc.message, group_id),
                )
            return False, exc.message

        # ONLY touch rules carrying THIS group's ownership marker — NEVER manual rules!
        owned_live = [r for r in live_rules if r.description.startswith(owner_prefix)]
        to_revoke: list[CloudSecurityGroupRule] = []
        for r in owned_live:
            role = r.description[len(owner_prefix) :]
            if (role, r.protocol, r.port, r.source_cidr) not in keep:
                to_revoke.append(r)

        revoke_err: str | None = None
        for r in to_revoke:
            try:
                self.sg_client.revoke_rule(security_group_id=sg_id, rule_id=r.rule_id)
            except (CloudTimeoutError, CloudAuthTemporaryError) as exc:
                revoke_err = exc.message
                break

        # Query live cloud state again to confirm whether revocation actually succeeded!
        try:
            after_rules = self.sg_client.list_rules(security_group_id=sg_id)
        except (CloudTimeoutError, CloudAuthTemporaryError) as exc:
            with self.db.write_transaction() as conn:
                conn.execute(
                    "UPDATE group_runtime_status SET sg_state = 'needs_verification', sg_id = ?, sg_observed_at = ?, sg_detail = ? WHERE group_id = ?",
                    (sg_id, self.clock.now_iso(), exc.message, group_id),
                )
            return False, exc.message

        remaining = [
            r
            for r in after_rules
            if r.description.startswith(owner_prefix)
            and (r.description[len(owner_prefix) :], r.protocol, r.port, r.source_cidr) not in keep
        ]
        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            if not remaining:
                if keep:
                    # Delete only superseded rule rows
                    rows = conn.execute(
                        "SELECT id, role, protocol, port, source_cidr FROM security_group_rules WHERE group_id = ?",
                        (group_id,),
                    ).fetchall()
                    for row in rows:
                        if (row["role"], row["protocol"], int(row["port"]), str(row["source_cidr"])) not in keep:
                            conn.execute("DELETE FROM security_group_rules WHERE id = ?", (row["id"],))
                else:
                    conn.execute("DELETE FROM security_group_rules WHERE group_id = ?", (group_id,))
                conn.execute(
                    "UPDATE group_runtime_status SET sg_state = 'synced', sg_id = ?, sg_observed_at = ?, sg_detail = NULL WHERE group_id = ?",
                    (sg_id, now_iso, group_id),
                )
                return True, None
            else:
                for rem in remaining:
                    role = rem.description[len(owner_prefix) :]
                    conn.execute(
                        """
                        UPDATE security_group_rules
                        SET status = 'pending_delete', cloud_rule_id = ?, updated_at = ?
                        WHERE group_id = ? AND role = ? AND protocol = ? AND port = ? AND source_cidr = ?
                        """,
                        (rem.rule_id, now_iso, group_id, role, rem.protocol, rem.port, rem.source_cidr),
                    )
                conn.execute(
                    "UPDATE group_runtime_status SET sg_state = 'needs_verification', sg_id = ?, sg_observed_at = ?, sg_detail = ? WHERE group_id = ?",
                    (sg_id, now_iso, revoke_err or f"{len(remaining)} owned rules still present", group_id),
                )
                return False, revoke_err or f"{len(remaining)} owned rules still present"

    # ------------------------------------------------------------------
    # Main Dispatch Loop & Single-Operation Execution
    # ------------------------------------------------------------------
    def process_pending_operations(self) -> list[str]:
        """Process all currently pending operations in FIFO order (per-group serial)."""
        self.db.record_component_heartbeat("flightlink-agent", "ok", "Agent polling loop active")
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT operation_id FROM operations WHERE status = 'pending' ORDER BY created_at ASC, rowid ASC"
            ).fetchall()
        processed: list[str] = []
        for r in rows:
            op_id = str(r["operation_id"])
            self.execute_operation(op_id)
            processed.append(op_id)
        return processed

    def execute_operation(self, operation_id: str) -> None:
        with self.db.read() as conn:
            op = conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if op is None or op["status"] != "pending":
                return
            group_id = str(op["group_id"])
            action = str(op["action"])
            op_gen = int(op["generation"])
            payload = json.loads(str(op["payload_json"]))

        lock = self._lock_for(group_id)
        with lock:
            now_iso = self.clock.now_iso()
            # Claim operation atomically and re-check generation before touching anything!
            with self.db.write_transaction() as conn:
                fresh_op = conn.execute(
                    "SELECT status FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if fresh_op is None or fresh_op["status"] != "pending":
                    return
                group = conn.execute(
                    "SELECT * FROM port_groups WHERE group_id = ?",
                    (group_id,),
                ).fetchone()
                if group is None or group["deleted_at"] is not None:
                    conn.execute(
                        "UPDATE operations SET status = 'cancelled', finished_at = ?, updated_at = ?, error_code = 'GROUP_ALREADY_DELETED' WHERE operation_id = ?",
                        (now_iso, now_iso, operation_id),
                    )
                    return

                # Fence stale non-delete operations!
                if action != "delete" and (
                    group["desired_state"] == "deleting"
                    or int(group["operation_generation"]) != op_gen
                ):
                    conn.execute(
                        "UPDATE operations SET status = 'cancelled', finished_at = ?, updated_at = ?, error_code = 'STALE_GENERATION', error_message = 'Superseded by newer operation generation.' WHERE operation_id = ?",
                        (now_iso, now_iso, operation_id),
                    )
                    return
                if action == "delete" and int(group["operation_generation"]) != op_gen:
                    conn.execute(
                        "UPDATE operations SET status = 'cancelled', finished_at = ?, updated_at = ?, error_code = 'STALE_GENERATION' WHERE operation_id = ?",
                        (now_iso, now_iso, operation_id),
                    )
                    return

                conn.execute(
                    "UPDATE operations SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ? WHERE operation_id = ?",
                    (now_iso, now_iso, operation_id),
                )

            if action == "create":
                self._exec_create(operation_id, group_id, op_gen)
            elif action in ("start", "restart"):
                self._exec_start(operation_id, group_id, op_gen, is_restart=(action == "restart"))
            elif action == "stop":
                self._exec_stop(operation_id, group_id, op_gen)
            elif action == "update":
                self._exec_update(operation_id, group_id, op_gen, payload)
            elif action == "delete":
                self._exec_delete(operation_id, group_id, op_gen)
            elif action == "sync_security_group":
                self._exec_sync_sg(operation_id, group_id, op_gen)
            elif action == "verify":
                self._exec_verify(operation_id, group_id, op_gen)

    # ------------------------------------------------------------------
    # Create: ends in `configured_stopped` (NEVER auto-starts!)
    # ------------------------------------------------------------------
    def _exec_create(self, op_id: str, group_id: str, op_gen: int) -> None:
        with self.db.read() as conn:
            g = conn.execute("SELECT * FROM port_groups WHERE group_id = ?", (group_id,)).fetchone()
        if g is None or not self._is_generation_current(group_id, op_gen):
            self._finish_operation(op_id, "cancelled", error_code="STALE_GENERATION")
            return

        self._set_step(op_id, "render_router_config", "running")
        try:
            write_router_config_atomic(
                self.settings.resolved_router_config_dir,
                group_id=group_id,
                dtu_port=int(g["dtu_port"]),
                gcs_protocol=g["gcs_protocol"],
                gcs_port=int(g["gcs_port"]),
            )
            self._set_step(op_id, "render_router_config", "succeeded")
        except Exception as exc:
            self._set_step(op_id, "render_router_config", "failed", error_code="CONFIG_WRITE_FAILED", error_message=str(exc))
            with self.db.write_transaction() as conn:
                conn.execute(
                    "UPDATE port_groups SET lifecycle_state = 'error', desired_state = 'stopped', updated_at = ? WHERE group_id = ?",
                    (self.clock.now_iso(), group_id),
                )
            self._finish_operation(op_id, "failed", retryable=True, error_code="CONFIG_WRITE_FAILED", error_message=str(exc))
            return

        self._set_step(op_id, "sync_security_group", "running")
        specs = self._desired_sg_specs(
            group_id=group_id,
            dtu_port=int(g["dtu_port"]),
            gcs_protocol=g["gcs_protocol"],
            gcs_port=int(g["gcs_port"]),
        )
        ok, err = self._reconcile_authorize_specs(group_id=group_id, specs=specs)
        if not ok:
            self._set_step(op_id, "sync_security_group", "needs_verification", error_code="SG_VERIFY_FAILED", error_message=err)
            self._set_step(op_id, "verify_security_group", "needs_verification", error_code="SG_VERIFY_FAILED", error_message=err)
            with self.db.write_transaction() as conn:
                conn.execute(
                    """
                    UPDATE port_groups
                    SET lifecycle_state = 'needs_verification', desired_state = 'stopped',
                        verification_reason = ?, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (err, self.clock.now_iso(), group_id),
                )
            self._finish_operation(
                op_id,
                "needs_verification",
                retryable=True,
                retry_hint="Verify cloud security group rules and retry create operation.",
                error_code="SG_VERIFY_FAILED",
                error_message=err,
            )
            return

        self._set_step(op_id, "sync_security_group", "succeeded")
        self._set_step(op_id, "verify_security_group", "succeeded")
        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            conn.execute(
                """
                UPDATE port_groups
                SET lifecycle_state = 'configured_stopped', desired_state = 'stopped',
                    verification_reason = NULL, updated_at = ?
                WHERE group_id = ?
                """,
                (now_iso, group_id),
            )
            conn.execute(
                "UPDATE group_runtime_status SET router_state = 'stopped', router_observed_at = ?, observed_at = ? WHERE group_id = ?",
                (now_iso, now_iso, group_id),
            )
        self._finish_operation(op_id, "succeeded")

    # ------------------------------------------------------------------
    # Start / Restart: preflight generation + socket check + verify bind
    # ------------------------------------------------------------------
    def _exec_start(self, op_id: str, group_id: str, op_gen: int, *, is_restart: bool) -> None:
        self._set_step(op_id, "preflight_checks", "running")
        with self.db.read() as conn:
            g = conn.execute("SELECT * FROM port_groups WHERE group_id = ?", (group_id,)).fetchone()
        if (
            g is None
            or g["deleted_at"] is not None
            or g["desired_state"] != "running"
            or int(g["operation_generation"]) != op_gen
        ):
            self._set_step(op_id, "preflight_checks", "skipped", detail="Superseded by stop/delete/newer generation")
            self._finish_operation(op_id, "cancelled", error_code="STALE_GENERATION")
            return

        endpoints: list[tuple[Literal["udp", "tcp"], int]] = [
            ("udp", int(g["dtu_port"])),
            (g["gcs_protocol"], int(g["gcs_port"])),
        ]
        conflicts = self.sockets.check_ports_available(group_id=group_id, endpoints=endpoints)
        if conflicts:
            first = conflicts[0]
            msg = f"Port {first.protocol}/{first.port} is preempted by {first.process_name} (pid={first.pid})"
            self._set_step(op_id, "preflight_checks", "failed", error_code="PORT_PREEMPTED", error_message=msg)
            with self.db.write_transaction() as conn:
                conn.execute(
                    "UPDATE port_groups SET lifecycle_state = 'error', desired_state = 'stopped', updated_at = ? WHERE group_id = ?",
                    (self.clock.now_iso(), group_id),
                )
                conn.execute(
                    "UPDATE group_runtime_status SET router_state = 'error', router_verified_listening = 0, router_detail = ?, router_observed_at = ?, observed_at = ? WHERE group_id = ?",
                    (msg, self.clock.now_iso(), self.clock.now_iso(), group_id),
                )
            self._finish_operation(op_id, "failed", retryable=True, error_code="PORT_PREEMPTED", error_message=msg)
            return

        self._set_step(op_id, "preflight_checks", "succeeded")
        self._set_step(op_id, "ensure_router_config", "running")
        cfg_path = write_router_config_atomic(
            self.settings.resolved_router_config_dir,
            group_id=group_id,
            dtu_port=int(g["dtu_port"]),
            gcs_protocol=g["gcs_protocol"],
            gcs_port=int(g["gcs_port"]),
        )
        self._set_step(op_id, "ensure_router_config", "succeeded")

        # Re-check generation & intent immediately before starting external process!
        if not self._is_generation_current(group_id, op_gen):
            self._finish_operation(op_id, "cancelled", error_code="STALE_GENERATION")
            return
        with self.db.read() as conn:
            g_recheck = conn.execute("SELECT desired_state FROM port_groups WHERE group_id = ?", (group_id,)).fetchone()
            if g_recheck is None or g_recheck["desired_state"] != "running":
                self._finish_operation(op_id, "cancelled", error_code="SUPERSEDED_BY_STOP")
                return

        self._set_step(op_id, "start_router_and_verify_bind", "running")
        try:
            if is_restart:
                self.supervisor.stop_router(group_id)
            rt_info = self.supervisor.start_router(
                group_id=group_id,
                config_path=str(cfg_path),
                dtu_port=int(g["dtu_port"]),
                gcs_protocol=g["gcs_protocol"],
                gcs_port=int(g["gcs_port"]),
            )
            verified, bound = self.sockets.verify_group_listening(group_id=group_id, endpoints=endpoints)
            if not verified:
                self.supervisor.stop_router(group_id)
                raise InfrastructureError(
                    "ROUTER_BIND_VERIFY_FAILED",
                    f"Router process started (pid={rt_info.pid}) but expected sockets were not verified listening (found={bound}).",
                )
        except InfrastructureError as exc:
            self._set_step(op_id, "start_router_and_verify_bind", "failed", error_code=exc.code, error_message=exc.message)
            with self.db.write_transaction() as conn:
                conn.execute(
                    "UPDATE port_groups SET lifecycle_state = 'error', desired_state = 'stopped', updated_at = ? WHERE group_id = ?",
                    (self.clock.now_iso(), group_id),
                )
                conn.execute(
                    "UPDATE group_runtime_status SET router_state = 'error', router_pid = NULL, router_verified_listening = 0, router_detail = ?, router_observed_at = ?, observed_at = ? WHERE group_id = ?",
                    (exc.message, self.clock.now_iso(), self.clock.now_iso(), group_id),
                )
            self._finish_operation(op_id, "failed", retryable=exc.retryable, error_code=exc.code, error_message=exc.message)
            return

        self._set_step(op_id, "start_router_and_verify_bind", "succeeded", detail=f"pid={rt_info.pid} bound={bound}")

        # Start passive bypass MAVProxy (observer failure NEVER fails or stops Router!)
        self._set_step(op_id, "start_mavproxy_bypass", "running")
        tap_port = derive_internal_tap_port(group_id)
        mav_state = "waiting"
        mav_pid = None
        mav_detail = None
        try:
            mav_info = self.supervisor.start_mavproxy(
                group_id=group_id,
                internal_endpoint=f"udpin:127.0.0.1:{tap_port}",
            )
            mav_pid = mav_info.pid
            self._set_step(op_id, "start_mavproxy_bypass", "succeeded", detail=f"tap=127.0.0.1:{tap_port}")
        except Exception as exc:
            mav_state = "error"
            mav_detail = str(exc)
            self._set_step(op_id, "start_mavproxy_bypass", "failed", error_code="MAVPROXY_BYPASS_DEGRADED", error_message=str(exc))

        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            # Final check: if stop/delete arrived during start, stop immediately!
            cur_g = conn.execute(
                "SELECT desired_state, operation_generation FROM port_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if cur_g is None or cur_g["desired_state"] != "running" or int(cur_g["operation_generation"]) != op_gen:
                stop_needed = True
            else:
                stop_needed = False
                conn.execute(
                    "UPDATE port_groups SET lifecycle_state = 'running', verification_reason = NULL, updated_at = ? WHERE group_id = ?",
                    (now_iso, group_id),
                )
                conn.execute(
                    """
                    UPDATE group_runtime_status
                    SET router_state = 'running', router_unit_name = ?, router_pid = ?,
                        router_uptime_seconds = ?, router_bound_endpoints_json = ?,
                        router_verified_listening = 1, router_observed_at = ?, router_detail = NULL,
                        mavproxy_state = ?, mavproxy_pid = ?, mavproxy_observed_at = ?, mavproxy_detail = ?,
                        observed_at = ?
                    WHERE group_id = ?
                    """,
                    (
                        rt_info.unit_name,
                        rt_info.pid,
                        rt_info.uptime_seconds,
                        json.dumps(bound),
                        now_iso,
                        mav_state,
                        mav_pid,
                        now_iso,
                        mav_detail,
                        now_iso,
                        group_id,
                    ),
                )
        if stop_needed:
            self.supervisor.stop_mavproxy(group_id)
            self.supervisor.stop_router(group_id)
            self._finish_operation(op_id, "cancelled", error_code="SUPERSEDED_DURING_START")
            return
        self._finish_operation(op_id, "succeeded")

    # ------------------------------------------------------------------
    # Stop: intent already persisted as stopped; stop processes & verify
    # ------------------------------------------------------------------
    def _exec_stop(self, op_id: str, group_id: str, op_gen: int) -> None:
        self._set_step(op_id, "stop_channel_processes", "running")
        self.supervisor.stop_mavproxy(group_id)
        self.supervisor.stop_router(group_id)
        self._set_step(op_id, "stop_channel_processes", "succeeded")

        self._set_step(op_id, "verify_stopped", "running")
        rt = self.supervisor.inspect_router(group_id)
        if rt.active:
            self._set_step(op_id, "verify_stopped", "failed", error_code="ROUTER_STILL_ACTIVE", error_message="Router still active after stop")
            self._finish_operation(op_id, "needs_verification", retryable=True, error_code="ROUTER_STILL_ACTIVE")
            return

        self._set_step(op_id, "verify_stopped", "succeeded")
        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            conn.execute(
                """
                UPDATE port_groups
                SET desired_state = 'stopped', lifecycle_state = 'configured_stopped',
                    updated_at = ?
                WHERE group_id = ? AND desired_state != 'deleting'
                """,
                (now_iso, group_id),
            )
            conn.execute(
                """
                UPDATE group_runtime_status
                SET router_state = 'stopped', router_pid = NULL, router_uptime_seconds = 0,
                    router_bound_endpoints_json = '[]', router_verified_listening = 0,
                    router_observed_at = ?, mavproxy_state = 'disconnected', mavproxy_pid = NULL,
                    mavproxy_observed_at = ?, fc_state = 'offline', observed_at = ?
                WHERE group_id = ?
                """,
                (now_iso, now_iso, now_iso, group_id),
            )
        if self.archive_service is not None:
            try:
                self.archive_service.seal_active_session_for_group(
                    group_id, close_reason="admin_group_stopped"
                )
            except Exception:
                pass  # Log/archive faults NEVER block stopping channel!
        self._finish_operation(op_id, "succeeded")

    # ------------------------------------------------------------------
    # Update: rename-no-restart | stopped-no-start | running-candidate-switch
    # ------------------------------------------------------------------
    def _exec_update(self, op_id: str, group_id: str, op_gen: int, payload: dict[str, Any]) -> None:
        with self.db.read() as conn:
            g = conn.execute("SELECT * FROM port_groups WHERE group_id = ?", (group_id,)).fetchone()
        if g is None or not self._is_generation_current(group_id, op_gen):
            self._finish_operation(op_id, "cancelled", error_code="STALE_GENERATION")
            return

        candidate = payload.get("candidate") or (json.loads(g["candidate_json"]) if g["candidate_json"] else None)
        if not candidate:
            self._finish_operation(op_id, "failed", error_code="MISSING_CANDIDATE", error_message="Candidate payload missing")
            return

        new_name = str(candidate["name"])
        new_dtu = int(candidate["dtu_port"])
        new_gcs_proto: Literal["tcp", "udp"] = candidate["gcs_protocol"]
        new_gcs = int(candidate["gcs_port"])
        requires_sg_sync = bool(candidate["requires_sg_sync"])
        requires_restart = bool(candidate["requires_restart"])
        prev = candidate["previous"]

        # Case 1: Rename only -> NO restart, NO SG mutation!
        if not requires_sg_sync:
            self._set_step(op_id, "apply_metadata_only", "running")
            now_iso = self.clock.now_iso()
            with self.db.write_transaction() as conn:
                conn.execute(
                    """
                    UPDATE port_groups
                    SET name = ?, config_version = config_version + 1,
                        candidate_version = NULL, candidate_json = NULL, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (new_name, now_iso, group_id),
                )
                conn.execute(
                    "UPDATE archive_groups SET name_snapshot = ?, updated_at = ? WHERE group_id = ?",
                    (new_name, now_iso, group_id),
                )
            self._set_step(op_id, "apply_metadata_only", "succeeded")
            self._finish_operation(op_id, "succeeded")
            return

        # Case 2 & 3: Port or protocol changed!
        # Check whether candidate has already been committed (for retry of cleanup_superseded_sg only!)
        already_committed = (
            int(g["dtu_port"]) == new_dtu
            and str(g["gcs_protocol"]) == new_gcs_proto
            and int(g["gcs_port"]) == new_gcs
            and g["candidate_version"] is None
        )

        if not already_committed:
            # Step: prepare_candidate_config
            self._set_step(op_id, "prepare_candidate_config", "running")
            cand_endpoints: list[tuple[Literal["udp", "tcp"], int]] = [
                ("udp", new_dtu),
                (new_gcs_proto, new_gcs),
            ]
            conflicts = self.sockets.check_ports_available(group_id=group_id, endpoints=cand_endpoints)
            if conflicts:
                first = conflicts[0]
                msg = f"Candidate port {first.protocol}/{first.port} preempted by {first.process_name}"
                self._rollback_candidate_state(group_id, prev["lifecycle_state"])
                self._set_step(op_id, "prepare_candidate_config", "failed", error_code="PORT_PREEMPTED", error_message=msg)
                self._finish_operation(op_id, "failed", retryable=True, error_code="PORT_PREEMPTED", error_message=msg)
                return

            write_router_config_atomic(
                self.settings.resolved_router_config_dir,
                group_id=group_id,
                dtu_port=new_dtu,
                gcs_protocol=new_gcs_proto,
                gcs_port=new_gcs,
                suffix=".candidate",
            )
            self._set_step(op_id, "prepare_candidate_config", "succeeded")

            # Step: authorize_candidate_sg
            self._set_step(op_id, "authorize_candidate_sg", "running")
            cand_specs = self._desired_sg_specs(
                group_id=group_id,
                dtu_port=new_dtu,
                gcs_protocol=new_gcs_proto,
                gcs_port=new_gcs,
            )
            sg_ok, sg_err = self._reconcile_authorize_specs(group_id=group_id, specs=cand_specs)
            if not sg_ok:
                # Candidate failed before switch -> recover known old config; DO NOT touch running group!
                remove_router_config(self.settings.resolved_router_config_dir, group_id, suffix=".candidate")
                self._rollback_candidate_state(group_id, prev["lifecycle_state"])
                self._set_step(op_id, "authorize_candidate_sg", "needs_verification", error_code="SG_VERIFY_FAILED", error_message=sg_err)
                self._finish_operation(
                    op_id,
                    "needs_verification",
                    retryable=True,
                    retry_hint="Candidate security group authorization did not complete; existing active channel was preserved.",
                    error_code="SG_VERIFY_FAILED",
                    error_message=sg_err,
                )
                return
            self._set_step(op_id, "authorize_candidate_sg", "succeeded")

            # If running group, switch ONLY this group and verify new sockets before committing!
            bound: list[str] = []
            rt_info = None
            if requires_restart:
                self._set_step(op_id, "switch_router_to_candidate", "running")
                try:
                    self.supervisor.stop_router(group_id)
                    cfg_path = write_router_config_atomic(
                        self.settings.resolved_router_config_dir,
                        group_id=group_id,
                        dtu_port=new_dtu,
                        gcs_protocol=new_gcs_proto,
                        gcs_port=new_gcs,
                    )
                    rt_info = self.supervisor.start_router(
                        group_id=group_id,
                        config_path=str(cfg_path),
                        dtu_port=new_dtu,
                        gcs_protocol=new_gcs_proto,
                        gcs_port=new_gcs,
                    )
                    self._set_step(op_id, "switch_router_to_candidate", "succeeded")
                    self._set_step(op_id, "verify_candidate_listening", "running")
                    verified, bound = self.sockets.verify_group_listening(group_id=group_id, endpoints=cand_endpoints)
                    if not verified:
                        raise InfrastructureError(
                            "CANDIDATE_BIND_VERIFY_FAILED",
                            f"Candidate endpoints {cand_endpoints} not verified listening (found={bound}).",
                        )
                    self._set_step(op_id, "verify_candidate_listening", "succeeded", detail=f"bound={bound}")
                except InfrastructureError as exc:
                    # Candidate switch failed -> recover known old config within this edit!
                    self._set_step(op_id, "verify_candidate_listening", "failed", error_code=exc.code, error_message=exc.message)
                    self.supervisor.stop_router(group_id)
                    old_cfg = write_router_config_atomic(
                        self.settings.resolved_router_config_dir,
                        group_id=group_id,
                        dtu_port=int(prev["dtu_port"]),
                        gcs_protocol=prev["gcs_protocol"],
                        gcs_port=int(prev["gcs_port"]),
                    )
                    remove_router_config(self.settings.resolved_router_config_dir, group_id, suffix=".candidate")
                    try:
                        old_rt = self.supervisor.start_router(
                            group_id=group_id,
                            config_path=str(old_cfg),
                            dtu_port=int(prev["dtu_port"]),
                            gcs_protocol=prev["gcs_protocol"],
                            gcs_port=int(prev["gcs_port"]),
                        )
                        old_eps: list[tuple[Literal["udp", "tcp"], int]] = [
                            ("udp", int(prev["dtu_port"])),
                            (prev["gcs_protocol"], int(prev["gcs_port"])),
                        ]
                        old_ok, old_bound = self.sockets.verify_group_listening(group_id=group_id, endpoints=old_eps)
                        restored_state = "running" if old_ok else "needs_verification"
                        with self.db.write_transaction() as conn:
                            conn.execute(
                                """
                                UPDATE group_runtime_status
                                SET router_state = ?, router_pid = ?, router_bound_endpoints_json = ?,
                                    router_verified_listening = ?, router_observed_at = ?, observed_at = ?
                                WHERE group_id = ?
                                """,
                                (
                                    "running" if old_ok else "needs_verification",
                                    old_rt.pid if old_ok else None,
                                    json.dumps(old_bound),
                                    1 if old_ok else 0,
                                    self.clock.now_iso(),
                                    self.clock.now_iso(),
                                    group_id,
                                ),
                            )
                    except Exception:
                        restored_state = "needs_verification"

                    self._rollback_candidate_state(group_id, restored_state)
                    # Best-effort revoke candidate-only SG rules while keeping old rules
                    old_specs = self._desired_sg_specs(
                        group_id=group_id,
                        dtu_port=int(prev["dtu_port"]),
                        gcs_protocol=prev["gcs_protocol"],
                        gcs_port=int(prev["gcs_port"]),
                    )
                    old_keep = {(s["role"], s["protocol"], s["port"], s["source_cidr"]) for s in old_specs}
                    self._revoke_owned_rules(group_id=group_id, keep_keys=old_keep)
                    self._finish_operation(
                        op_id,
                        "failed" if restored_state == "running" else "needs_verification",
                        retryable=True,
                        error_code=exc.code,
                        error_message=exc.message,
                    )
                    return
            else:
                # Stopped group: write active config ONLY, NEVER start Router!
                write_router_config_atomic(
                    self.settings.resolved_router_config_dir,
                    group_id=group_id,
                    dtu_port=new_dtu,
                    gcs_protocol=new_gcs_proto,
                    gcs_port=new_gcs,
                )

            remove_router_config(self.settings.resolved_router_config_dir, group_id, suffix=".candidate")

            # Step: commit_candidate_version (ONLY after new listen succeeded for running group!)
            self._set_step(op_id, "commit_candidate_version", "running")
            now_iso = self.clock.now_iso()
            with self.db.write_transaction() as conn:
                old_ports = {int(prev["dtu_port"]), int(prev["gcs_port"])}
                new_ports = {new_dtu, new_gcs}
                superseded_ports = old_ports - new_ports

                # Remove old active reservations; keep superseded ports as 'releasing' until old SG revoked
                conn.execute(
                    "DELETE FROM port_reservations WHERE group_id = ? AND reservation_kind = 'active'",
                    (group_id,),
                )
                conn.execute(
                    """
                    UPDATE port_reservations
                    SET reservation_kind = 'active'
                    WHERE group_id = ? AND reservation_kind = 'candidate'
                    """,
                    (group_id,),
                )
                for p in superseded_ports:
                    role = "dtu" if p == int(prev["dtu_port"]) else "gcs"
                    proto = "udp" if role == "dtu" else prev["gcs_protocol"]
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO port_reservations(port, reservation_kind, group_id, role, protocol, created_at)
                        VALUES (?, 'releasing', ?, ?, ?, ?)
                        """,
                        (p, group_id, role, proto, now_iso),
                    )

                final_lifecycle = "running" if requires_restart else "configured_stopped"
                conn.execute(
                    """
                    UPDATE port_groups
                    SET name = ?, dtu_port = ?, gcs_protocol = ?, gcs_port = ?,
                        config_version = config_version + 1,
                        candidate_version = NULL, candidate_json = NULL,
                        lifecycle_state = ?, verification_reason = NULL, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (new_name, new_dtu, new_gcs_proto, new_gcs, final_lifecycle, now_iso, group_id),
                )
                conn.execute(
                    """
                    UPDATE archive_groups
                    SET name_snapshot = ?, dtu_port_snapshot = ?, gcs_protocol_snapshot = ?,
                        gcs_port_snapshot = ?, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (new_name, new_dtu, new_gcs_proto, new_gcs, now_iso, group_id),
                )
                if requires_restart and rt_info is not None:
                    conn.execute(
                        """
                        UPDATE group_runtime_status
                        SET router_state = 'running', router_pid = ?, router_bound_endpoints_json = ?,
                            router_verified_listening = 1, router_observed_at = ?, observed_at = ?
                        WHERE group_id = ?
                        """,
                        (rt_info.pid, json.dumps(bound), now_iso, now_iso, group_id),
                    )
            self._set_step(op_id, "commit_candidate_version", "succeeded")

        # Step: cleanup_superseded_sg (if this fails after new link succeeded -> cleanup-only, NEVER rollback!)
        self._set_step(op_id, "cleanup_superseded_sg", "running")
        new_specs = self._desired_sg_specs(
            group_id=group_id,
            dtu_port=new_dtu,
            gcs_protocol=new_gcs_proto,
            gcs_port=new_gcs,
        )
        keep_keys = {(s["role"], s["protocol"], s["port"], s["source_cidr"]) for s in new_specs}
        revoked_ok, revoke_err = self._revoke_owned_rules(group_id=group_id, keep_keys=keep_keys)
        if not revoked_ok:
            # DO NOT ROLLBACK! New link stays active & committed; only mark SG cleanup pending!
            self._set_step(op_id, "cleanup_superseded_sg", "needs_verification", error_code="OLD_SG_REVOKE_PENDING", error_message=revoke_err)
            self._finish_operation(
                op_id,
                "needs_verification",
                retryable=True,
                retry_hint="New channel is active and committed; retry only cleans up superseded cloud security group rules.",
                error_code="OLD_SG_REVOKE_PENDING",
                error_message=revoke_err,
            )
            return

        # Old SG rules verified gone -> release superseded ports!
        with self.db.write_transaction() as conn:
            conn.execute(
                "DELETE FROM port_reservations WHERE group_id = ? AND reservation_kind = 'releasing'",
                (group_id,),
            )
        self._set_step(op_id, "cleanup_superseded_sg", "succeeded")
        self._finish_operation(op_id, "succeeded")

    def _rollback_candidate_state(self, group_id: str, restored_lifecycle: str) -> None:
        with self.db.write_transaction() as conn:
            conn.execute(
                "DELETE FROM port_reservations WHERE group_id = ? AND reservation_kind = 'candidate'",
                (group_id,),
            )
            conn.execute(
                """
                UPDATE port_groups
                SET candidate_version = NULL, candidate_json = NULL,
                    lifecycle_state = ?, updated_at = ?
                WHERE group_id = ?
                """,
                (restored_lifecycle, self.clock.now_iso(), group_id),
            )

    # ------------------------------------------------------------------
    # Delete: stop -> revoke owned SG -> verify clear -> release ports -> seal archive
    # ------------------------------------------------------------------
    def _exec_delete(self, op_id: str, group_id: str, op_gen: int) -> None:
        # Step: stop_channel_processes
        self._set_step(op_id, "stop_channel_processes", "running")
        self.supervisor.stop_mavproxy(group_id)
        self.supervisor.stop_router(group_id)
        remove_router_config(self.settings.resolved_router_config_dir, group_id)
        remove_router_config(self.settings.resolved_router_config_dir, group_id, suffix=".candidate")
        try:
            (self.settings.resolved_runtime_dir / f"mavproxy-{group_id}.env").unlink()
        except FileNotFoundError:
            pass
        now_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            conn.execute(
                """
                UPDATE group_runtime_status
                SET router_state = 'stopped', router_pid = NULL, router_bound_endpoints_json = '[]',
                    router_verified_listening = 0, router_observed_at = ?,
                    mavproxy_state = 'disconnected', mavproxy_pid = NULL, mavproxy_observed_at = ?,
                    fc_state = 'offline', observed_at = ?
                WHERE group_id = ?
                """,
                (now_iso, now_iso, now_iso, group_id),
            )
        self._set_step(op_id, "stop_channel_processes", "succeeded")

        # Independently seal active archive session (archive fault NEVER blocks stopping channel!)
        self._set_step(op_id, "seal_active_archive", "running")
        if self.archive_service is not None:
            try:
                sealed_sid = self.archive_service.seal_active_session_for_group(
                    group_id, close_reason="admin_group_deleted"
                )
                self._set_step(
                    op_id,
                    "seal_active_archive",
                    "succeeded",
                    detail=f"sealed={sealed_sid}" if sealed_sid else "no_active_session",
                )
            except Exception as exc:
                # Never block stopping/deleting channel on archive failure
                self._set_step(
                    op_id,
                    "seal_active_archive",
                    "failed",
                    error_code="ARCHIVE_SEAL_WARNING",
                    error_message=str(exc),
                )
        else:
            self._set_step(op_id, "seal_active_archive", "skipped", detail="No archive service attached")

        # Step: revoke_security_group_rules
        self._set_step(op_id, "revoke_security_group_rules", "running")
        revoked_ok, revoke_err = self._revoke_owned_rules(group_id=group_id, keep_keys=None)
        if not revoked_ok:
            self._set_step(op_id, "revoke_security_group_rules", "needs_verification", error_code="SG_REVOKE_PENDING", error_message=revoke_err)
            self._set_step(op_id, "verify_cleanup_and_release_ports", "pending")
            with self.db.write_transaction() as conn:
                conn.execute(
                    """
                    UPDATE port_groups
                    SET desired_state = 'deleting', lifecycle_state = 'cleanup_pending',
                        verification_reason = ?, updated_at = ?
                    WHERE group_id = ?
                    """,
                    (revoke_err, self.clock.now_iso(), group_id),
                )
            self._finish_operation(
                op_id,
                "needs_verification",
                retryable=True,
                retry_hint="Channel is stopped; retry deletion to verify and finish cloud security group cleanup before releasing ports.",
                error_code="SG_REVOKE_PENDING",
                error_message=revoke_err,
            )
            return

        self._set_step(op_id, "revoke_security_group_rules", "succeeded")

        # Step: verify_cleanup_and_release_ports
        self._set_step(op_id, "verify_cleanup_and_release_ports", "running")
        rt = self.supervisor.inspect_router(group_id)
        if rt.active:
            self._set_step(op_id, "verify_cleanup_and_release_ports", "needs_verification", error_code="ROUTER_STILL_ACTIVE")
            self._finish_operation(op_id, "needs_verification", retryable=True, error_code="ROUTER_STILL_ACTIVE")
            return

        done_iso = self.clock.now_iso()
        with self.db.write_transaction() as conn:
            # Release ports ONLY after router stopped & cloud ownership verified clear!
            conn.execute("DELETE FROM port_reservations WHERE group_id = ?", (group_id,))
            conn.execute(
                """
                UPDATE port_groups
                SET deleted_at = ?, lifecycle_state = 'deleting', desired_state = 'deleting',
                    active_operation_id = NULL, verification_reason = NULL, updated_at = ?
                WHERE group_id = ?
                """,
                (done_iso, done_iso, group_id),
            )
            conn.execute(
                """
                UPDATE archive_groups
                SET deleted = 1, deleted_at = ?, updated_at = ?
                WHERE group_id = ?
                """,
                (done_iso, done_iso, group_id),
            )
        self._set_step(op_id, "verify_cleanup_and_release_ports", "succeeded")
        self._finish_operation(op_id, "succeeded")

    # ------------------------------------------------------------------
    # Sync Security Group & Verify Actions
    # ------------------------------------------------------------------
    def _exec_sync_sg(self, op_id: str, group_id: str, op_gen: int) -> None:
        with self.db.read() as conn:
            g = conn.execute("SELECT * FROM port_groups WHERE group_id = ?", (group_id,)).fetchone()
        if g is None or g["deleted_at"] is not None:
            self._finish_operation(op_id, "cancelled", error_code="GROUP_NOT_FOUND")
            return
        self._set_step(op_id, "reconcile_security_group", "running")
        specs = self._desired_sg_specs(
            group_id=group_id,
            dtu_port=int(g["dtu_port"]),
            gcs_protocol=g["gcs_protocol"],
            gcs_port=int(g["gcs_port"]),
        )
        ok, err = self._reconcile_authorize_specs(group_id=group_id, specs=specs)
        if not ok:
            # Temporary RAM failure NEVER restarts already-running channels!
            self._set_step(op_id, "reconcile_security_group", "needs_verification", error_code="SG_SYNC_FAILED", error_message=err)
            self._finish_operation(op_id, "needs_verification", retryable=True, error_code="SG_SYNC_FAILED", error_message=err)
            return

        keep = {(s["role"], s["protocol"], s["port"], s["source_cidr"]) for s in specs}
        rev_ok, rev_err = self._revoke_owned_rules(group_id=group_id, keep_keys=keep)
        if not rev_ok:
            self._set_step(op_id, "verify_security_group", "needs_verification", error_code="SG_CLEANUP_PENDING", error_message=rev_err)
            self._finish_operation(op_id, "needs_verification", retryable=True, error_code="SG_CLEANUP_PENDING", error_message=rev_err)
            return
        with self.db.write_transaction() as conn:
            conn.execute("DELETE FROM port_reservations WHERE group_id = ? AND reservation_kind = 'releasing'", (group_id,))
        self._set_step(op_id, "reconcile_security_group", "succeeded")
        self._set_step(op_id, "verify_security_group", "succeeded")
        self._finish_operation(op_id, "succeeded")

    def _exec_verify(self, op_id: str, group_id: str, op_gen: int) -> None:
        self._set_step(op_id, "inspect_actual_runtime", "running")
        self.reconcile_group_runtime(group_id)
        self._set_step(op_id, "inspect_actual_runtime", "succeeded")
        self._set_step(op_id, "reconcile_state", "succeeded")
        self._finish_operation(op_id, "succeeded")

    # ------------------------------------------------------------------
    # Startup Crash Recovery & Firewall Diff (P2 / P3 / P5)
    # ------------------------------------------------------------------
    def reconcile_on_startup(self, *, ecs_reboot: bool = False) -> dict[str, Any]:
        """Survey systemd/sockets/cloud rules on agent startup.

        - Never auto-starts groups because config files exist.
        - On ECS reboot (`ecs_reboot=True`), groups remain stopped by default.
        - Marks crashed in-flight operations as `needs_verification` (delete -> `cleanup_pending`).
        - Preserves evidence without guessing or restarting healthy channels on temp RAM errors.
        """
        now_iso = self.clock.now_iso()
        report: dict[str, Any] = {
            "interrupted_operations": [],
            "groups_reconciled": [],
            "needs_verification_groups": [],
        }

        # 1. Handle operations interrupted mid-flight by crash
        with self.db.write_transaction() as conn:
            in_flight = conn.execute(
                "SELECT operation_id, group_id, action FROM operations WHERE status = 'running'"
            ).fetchall()
            for op in in_flight:
                op_id = str(op["operation_id"])
                gid = str(op["group_id"])
                act = str(op["action"])
                conn.execute(
                    """
                    UPDATE operations
                    SET status = 'needs_verification', retryable = 1,
                        error_code = 'AGENT_RESTARTED_IN_FLIGHT',
                        error_message = 'Agent restarted while operation was executing; verify actual state before retrying.',
                        retry_hint = 'Inspect state and retry operation.',
                        finished_at = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (now_iso, now_iso, op_id),
                )
                if act == "delete":
                    conn.execute(
                        """
                        UPDATE port_groups
                        SET desired_state = 'deleting', lifecycle_state = 'cleanup_pending',
                            verification_reason = 'Delete interrupted by agent restart',
                            active_operation_id = NULL, updated_at = ?
                        WHERE group_id = ? AND deleted_at IS NULL
                        """,
                        (now_iso, gid),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE port_groups
                        SET lifecycle_state = 'needs_verification',
                            verification_reason = 'Operation interrupted by agent restart',
                            active_operation_id = NULL, updated_at = ?
                        WHERE group_id = ? AND deleted_at IS NULL
                        """,
                        (now_iso, gid),
                    )
                report["interrupted_operations"].append(op_id)

        # 2. Reconcile every non-deleted group against actual processes/sockets/cloud
        with self.db.read() as conn:
            groups = conn.execute(
                "SELECT group_id FROM port_groups WHERE deleted_at IS NULL"
            ).fetchall()

        for g in groups:
            gid = str(g["group_id"])
            state = self.reconcile_group_runtime(gid, ecs_reboot=ecs_reboot)
            report["groups_reconciled"].append(gid)
            if state in ("needs_verification", "cleanup_pending"):
                report["needs_verification_groups"].append(gid)

        # 3. Stop any stray router running for an already-deleted group
        running_groups = set(self.supervisor.list_running_router_groups())
        with self.db.read() as conn:
            active_ids = {
                str(r["group_id"])
                for r in conn.execute("SELECT group_id FROM port_groups WHERE deleted_at IS NULL").fetchall()
            }
        for orphan_gid in running_groups - active_ids:
            self.supervisor.stop_mavproxy(orphan_gid)
            self.supervisor.stop_router(orphan_gid)

        self.db.record_component_heartbeat("flightlink-agent", "ok", "Startup reconciliation complete")
        return report

    def reconcile_group_runtime(self, group_id: str, *, ecs_reboot: bool = False) -> str:
        with self.db.read() as conn:
            g = conn.execute(
                "SELECT * FROM port_groups WHERE group_id = ? AND deleted_at IS NULL",
                (group_id,),
            ).fetchone()
        if g is None:
            return "deleted"

        now_iso = self.clock.now_iso()
        rt = self.supervisor.inspect_router(group_id)
        mav = self.supervisor.inspect_mavproxy(group_id)
        endpoints: list[tuple[Literal["udp", "tcp"], int]] = [
            ("udp", int(g["dtu_port"])),
            (g["gcs_protocol"], int(g["gcs_port"])),
        ]
        verified, bound = self.sockets.verify_group_listening(group_id=group_id, endpoints=endpoints)

        # If group is deleting/cleanup_pending, make sure processes stay stopped (NEVER resurrect!)
        if g["desired_state"] == "deleting" or g["lifecycle_state"] in ("deleting", "cleanup_pending"):
            if rt.active:
                self.supervisor.stop_mavproxy(group_id)
                self.supervisor.stop_router(group_id)
            with self.db.write_transaction() as conn:
                conn.execute(
                    """
                    UPDATE group_runtime_status
                    SET router_state = 'stopped', router_pid = NULL, router_verified_listening = 0,
                        router_observed_at = ?, observed_at = ?
                    WHERE group_id = ?
                    """,
                    (now_iso, now_iso, group_id),
                )
            return "cleanup_pending"

        # Determine reconciled lifecycle without guessing or auto-starting!
        new_lifecycle = str(g["lifecycle_state"])
        new_desired = str(g["desired_state"])
        ver_reason = g["verification_reason"]
        router_state = "running" if (rt.active and verified) else ("needs_verification" if rt.active else "stopped")

        if ecs_reboot and not rt.active and new_desired == "running":
            # ECS reboot default: do NOT bring up groups automatically
            new_desired = "stopped"
            new_lifecycle = "configured_stopped"
            ver_reason = None
        elif rt.active and not verified:
            new_lifecycle = "needs_verification"
            ver_reason = f"Router pid={rt.pid} active but expected sockets {endpoints} not verified (bound={bound})"
        elif not rt.active and new_desired == "running" and not ecs_reboot:
            new_lifecycle = "needs_verification"
            ver_reason = "Desired state was running but router process is not active"
        elif rt.active and verified and new_desired == "stopped":
            new_lifecycle = "needs_verification"
            ver_reason = "Router is listening despite desired_state=stopped"

        with self.db.write_transaction() as conn:
            conn.execute(
                """
                UPDATE port_groups
                SET desired_state = ?, lifecycle_state = ?, verification_reason = ?, updated_at = ?
                WHERE group_id = ?
                """,
                (new_desired, new_lifecycle, ver_reason, now_iso, group_id),
            )
            conn.execute(
                """
                UPDATE group_runtime_status
                SET router_state = ?, router_unit_name = ?, router_pid = ?,
                    router_uptime_seconds = ?, router_bound_endpoints_json = ?,
                    router_verified_listening = ?, router_observed_at = ?,
                    mavproxy_state = ?, mavproxy_pid = ?, mavproxy_observed_at = ?,
                    observed_at = ?
                WHERE group_id = ?
                """,
                (
                    router_state,
                    rt.unit_name,
                    rt.pid,
                    rt.uptime_seconds if rt.active else 0,
                    json.dumps(bound),
                    1 if (rt.active and verified) else 0,
                    now_iso,
                    "waiting" if mav.active else "disconnected",
                    mav.pid,
                    now_iso,
                    now_iso,
                    group_id,
                ),
            )
        return new_lifecycle

    def compute_firewall_diff(self) -> dict[str, Any]:
        """Read-only diff between desired FlightLink-owned rules and live Aliyun SG rules."""
        sg_id, _, _ = self._sg_config()
        if not sg_id:
            return {"configured": False, "security_group_id": None, "missing": [], "extra_owned": [], "manual_ignored": 0}

        ecs_id = self._ecs_instance_id()
        instance_prefix = f"flightlink:v1:{ecs_id}:"
        live_rules = self.sg_client.list_rules(security_group_id=sg_id)

        expected: dict[tuple[str, str, int, str], dict[str, Any]] = {}
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT group_id, dtu_port, gcs_protocol, gcs_port FROM port_groups WHERE deleted_at IS NULL AND desired_state != 'deleting'"
            ).fetchall()
        for r in rows:
            for spec in self._desired_sg_specs(
                group_id=str(r["group_id"]),
                dtu_port=int(r["dtu_port"]),
                gcs_protocol=r["gcs_protocol"],
                gcs_port=int(r["gcs_port"]),
            ):
                key = (spec["ownership_marker"], spec["protocol"], spec["port"], spec["source_cidr"])
                expected[key] = spec

        live_owned: dict[tuple[str, str, int, str], CloudSecurityGroupRule] = {}
        manual_count = 0
        for lr in live_rules:
            if lr.description.startswith(instance_prefix):
                live_owned[(lr.description, lr.protocol, lr.port, lr.source_cidr)] = lr
            else:
                manual_count += 1

        missing = [spec for k, spec in expected.items() if k not in live_owned]
        extra_owned = [
            {
                "rule_id": lr.rule_id,
                "protocol": lr.protocol,
                "port": lr.port,
                "source_cidr": lr.source_cidr,
                "description": lr.description,
            }
            for k, lr in live_owned.items()
            if k not in expected
        ]
        return {
            "configured": True,
            "security_group_id": sg_id,
            "missing": missing,
            "extra_owned": extra_owned,
            "manual_ignored": manual_count,
            "in_sync": not missing and not extra_owned,
        }
