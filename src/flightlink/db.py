"""SQLite WAL control-plane and archive index database.

Separately persists:
- desired runtime intent (`desired_state`)
- active config version (`config_version`) and candidate version (`candidate_version`, `candidate_json`)
- observed actual service state (`group_runtime_status`, `component_heartbeats`)
- operations and per-step execution ledger (`operations`, `operation_steps`, `idempotency_keys`)
- ECS-wide unique port reservations (`port_reservations` with cross-group uniqueness triggers)
- cloud security group rule ownership ledger (`security_group_rules`)
- independent archive group identity, session index, sealed artifacts, entries, gaps, cursors, and download leases.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from flightlink.clock import Clock, SystemClock
from flightlink.ids import new_instance_id

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS meta_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_tokens (
    token_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('admin', 'viewer')),
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS port_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    dtu_protocol TEXT NOT NULL DEFAULT 'udp' CHECK (dtu_protocol = 'udp'),
    dtu_port INTEGER NOT NULL CHECK (dtu_port BETWEEN 1 AND 65535),
    gcs_protocol TEXT NOT NULL CHECK (gcs_protocol IN ('tcp', 'udp')),
    gcs_port INTEGER NOT NULL CHECK (gcs_port BETWEEN 1 AND 65535),
    config_version INTEGER NOT NULL DEFAULT 1,
    candidate_version INTEGER,
    candidate_json TEXT,
    desired_state TEXT NOT NULL CHECK (desired_state IN ('stopped', 'running', 'deleting')),
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN (
        'configured_stopped', 'starting', 'running', 'stopping',
        'updating', 'deleting', 'cleanup_pending', 'needs_verification', 'error'
    )),
    operation_generation INTEGER NOT NULL DEFAULT 1,
    active_operation_id TEXT,
    verification_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK (dtu_port != gcs_port)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_port_groups_active_name
    ON port_groups(name)
    WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS port_reservations (
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    reservation_kind TEXT NOT NULL CHECK (reservation_kind IN ('active', 'candidate', 'releasing')),
    group_id TEXT NOT NULL REFERENCES port_groups(group_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('dtu', 'gcs')),
    protocol TEXT NOT NULL CHECK (protocol IN ('udp', 'tcp')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (port, reservation_kind)
);

CREATE INDEX IF NOT EXISTS idx_port_reservations_group ON port_reservations(group_id);

-- Enforce ECS-wide cross-group port exclusivity at the database layer.
CREATE TRIGGER IF NOT EXISTS trg_port_reservations_no_cross_group_insert
BEFORE INSERT ON port_reservations
BEGIN
    SELECT RAISE(ABORT, 'PORT_OWNED_BY_OTHER_GROUP')
    WHERE EXISTS (
        SELECT 1 FROM port_reservations
        WHERE port = NEW.port AND group_id != NEW.group_id
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_port_reservations_no_cross_group_update
BEFORE UPDATE OF port, group_id ON port_reservations
BEGIN
    SELECT RAISE(ABORT, 'PORT_OWNED_BY_OTHER_GROUP')
    WHERE EXISTS (
        SELECT 1 FROM port_reservations
        WHERE port = NEW.port AND group_id != NEW.group_id
    );
END;

CREATE TABLE IF NOT EXISTS group_runtime_status (
    group_id TEXT PRIMARY KEY REFERENCES port_groups(group_id) ON DELETE CASCADE,
    router_state TEXT NOT NULL DEFAULT 'stopped',
    router_unit_name TEXT,
    router_pid INTEGER,
    router_uptime_seconds INTEGER,
    router_bound_endpoints_json TEXT NOT NULL DEFAULT '[]',
    router_verified_listening INTEGER NOT NULL DEFAULT 0,
    router_observed_at TEXT,
    router_detail TEXT,
    mavproxy_state TEXT NOT NULL DEFAULT 'disconnected',
    mavproxy_unit_name TEXT,
    mavproxy_pid INTEGER,
    mavproxy_uptime_seconds INTEGER,
    mavproxy_observed_at TEXT,
    mavproxy_detail TEXT,
    sg_state TEXT NOT NULL DEFAULT 'unconfigured',
    sg_id TEXT,
    sg_observed_at TEXT,
    sg_detail TEXT,
    sysid INTEGER,
    fc_state TEXT NOT NULL DEFAULT 'offline',
    dtu_peer TEXT,
    gcs_state TEXT NOT NULL DEFAULT 'disconnected',
    gcs_peer TEXT,
    link_quality_pct INTEGER,
    rtt_ms INTEGER,
    last_activity_at TEXT,
    telemetry_json TEXT NOT NULL DEFAULT '{}',
    telemetry_observed_at TEXT,
    recent_gcs_commands_json TEXT NOT NULL DEFAULT '[]',
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS security_group_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL REFERENCES port_groups(group_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('dtu', 'gcs')),
    protocol TEXT NOT NULL CHECK (protocol IN ('udp', 'tcp')),
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    source_cidr TEXT NOT NULL,
    security_group_id TEXT NOT NULL,
    cloud_rule_id TEXT,
    ownership_marker TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('present', 'pending_create', 'pending_delete', 'drift', 'unknown')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_verified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sg_rules_group ON security_group_rules(group_id);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'create', 'update', 'delete', 'start', 'stop', 'restart', 'sync_security_group', 'verify'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'running', 'succeeded', 'failed', 'needs_verification', 'cancelled'
    )),
    generation INTEGER NOT NULL,
    idempotency_key TEXT,
    requested_by TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    retryable INTEGER NOT NULL DEFAULT 0,
    retry_hint TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_group ON operations(group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status, created_at ASC);

CREATE TABLE IF NOT EXISTS operation_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
    step_order INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'running', 'succeeded', 'failed', 'needs_verification', 'skipped'
    )),
    started_at TEXT,
    finished_at TEXT,
    detail TEXT,
    error_code TEXT,
    error_message TEXT,
    UNIQUE (operation_id, step_name)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_heartbeats (
    component_name TEXT PRIMARY KEY CHECK (component_name IN ('flightlink-api', 'flightlink-agent', 'flightlink-archive')),
    status TEXT NOT NULL CHECK (status IN ('ok', 'degraded', 'stopped', 'unknown')),
    observed_at TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS live_logs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    group_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('router', 'mavproxy')),
    level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR')),
    ts TEXT NOT NULL,
    text TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_live_logs_group_seq ON live_logs(group_id, seq);

CREATE TABLE IF NOT EXISTS archive_groups (
    group_id TEXT PRIMARY KEY,
    name_snapshot TEXT NOT NULL,
    dtu_protocol TEXT NOT NULL DEFAULT 'udp',
    dtu_port_snapshot INTEGER NOT NULL,
    gcs_protocol_snapshot TEXT NOT NULL CHECK (gcs_protocol_snapshot IN ('tcp', 'udp')),
    gcs_port_snapshot INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_sessions (
    session_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES archive_groups(group_id) ON DELETE CASCADE,
    group_name_snapshot TEXT NOT NULL,
    dtu_port_snapshot INTEGER NOT NULL,
    gcs_protocol_snapshot TEXT NOT NULL CHECK (gcs_protocol_snapshot IN ('tcp', 'udp')),
    gcs_port_snapshot INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'sealing', 'sealed')),
    change_seq INTEGER UNIQUE,
    sysid INTEGER,
    first_observed_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    ended_at TEXT,
    sealed_at TEXT,
    expires_at TEXT,
    close_reason TEXT,
    purged_at TEXT,
    dir_relpath TEXT NOT NULL
);

-- At most one active (unsealed) session per group at any time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_archive_one_active_session_per_group
    ON archive_sessions(group_id)
    WHERE status = 'active' AND purged_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_archive_sessions_group ON archive_sessions(group_id, first_observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_archive_sessions_change_seq ON archive_sessions(change_seq);

CREATE TABLE IF NOT EXISTS archive_files (
    file_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES archive_sessions(session_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('router', 'mavproxy', 'metadata')),
    availability TEXT NOT NULL CHECK (availability IN (
        'available', 'missing_on_server', 'empty', 'degraded_quota_exceeded', 'in_progress'
    )),
    logical_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    relpath TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    line_count INTEGER NOT NULL DEFAULT 0,
    missing_reason TEXT,
    UNIQUE (session_id, source)
);

CREATE TABLE IF NOT EXISTS archive_entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES archive_sessions(session_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('router', 'mavproxy')),
    event_id TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR')),
    ts TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (session_id, source, event_id)
);

CREATE INDEX IF NOT EXISTS idx_archive_entries_session_source_seq
    ON archive_entries(session_id, source, seq);

CREATE TABLE IF NOT EXISTS archive_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES archive_sessions(session_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('router', 'mavproxy', 'all')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    reason TEXT NOT NULL CHECK (reason IN (
        'observer_disconnected', 'archive_restarted', 'disk_quota_exceeded', 'journal_gap'
    )),
    detail TEXT
);

CREATE TABLE IF NOT EXISTS archive_cursors (
    group_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('router', 'mavproxy')),
    cursor_value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (group_id, source)
);

CREATE TABLE IF NOT EXISTS archive_download_leases (
    lease_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES archive_sessions(session_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class Database:
    """Thread-safe SQLite WAL database wrapper with explicit immediate write transactions."""

    def __init__(self, db_path: Path, *, clock: Clock | None = None) -> None:
        self.db_path = Path(db_path)
        self.clock: Clock = clock or SystemClock()

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,  # explicit BEGIN / COMMIT / ROLLBACK
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def init_schema(self, *, ecs_instance_id: str | None = None) -> str:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA_SQL)
            now_iso = self.clock.now_iso()
            row = conn.execute(
                "SELECT value FROM meta_config WHERE key = 'ecs_instance_id'"
            ).fetchone()
            if row is None:
                instance_id = ecs_instance_id or new_instance_id()
                conn.execute(
                    "INSERT INTO meta_config(key, value, updated_at) VALUES (?, ?, ?)",
                    ("ecs_instance_id", instance_id, now_iso),
                )
            else:
                instance_id = str(row["value"])
            for k, v in (
                ("archive_change_seq", "0"),
                ("archive_min_valid_seq", "0"),
                ("live_log_min_valid_seq", "0"),
            ):
                conn.execute(
                    "INSERT OR IGNORE INTO meta_config(key, value, updated_at) VALUES (?, ?, ?)",
                    (k, v, now_iso),
                )
            return instance_id
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Execute a single atomic BEGIN IMMEDIATE transaction.

        Never wrap cloud API or systemd calls inside this context manager.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM meta_config WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO meta_config(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, self.clock.now_iso()),
            )

    def get_meta_json(self, key: str, default: object = None) -> object:
        raw = self.get_meta(key)
        if raw is None:
            return default
        return json.loads(raw)

    def set_meta_json(self, key: str, value: object) -> None:
        self.set_meta(key, json.dumps(value, ensure_ascii=False))

    def record_component_heartbeat(
        self,
        component_name: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        with self.write_transaction() as conn:
            conn.execute(
                """
                INSERT INTO component_heartbeats(component_name, status, observed_at, detail)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(component_name) DO UPDATE SET
                    status = excluded.status,
                    observed_at = excluded.observed_at,
                    detail = excluded.detail
                """,
                (component_name, status, self.clock.now_iso(), detail),
            )

    def health_check(self) -> bool:
        try:
            with self.read() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False
