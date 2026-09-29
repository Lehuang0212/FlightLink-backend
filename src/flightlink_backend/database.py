from __future__ import annotations

import sqlite3
import os
from collections.abc import Generator

from .config import settings

SCHEMA_VERSION = 4


def connect() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        settings.database_path,
        timeout=5.0,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    if os.name == "posix":
        settings.database_path.chmod(0o600)
    return connection


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at INTEGER NOT NULL,
            last_login_at INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE admin_sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX admin_sessions_user_id_idx ON admin_sessions(user_id)"
    )
    connection.execute(
        "CREATE INDEX admin_sessions_expires_at_idx ON admin_sessions(expires_at)"
    )


def _migration_2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE port_channels (
            id TEXT PRIMARY KEY,
            uav_udp_port INTEGER NOT NULL CHECK (uav_udp_port BETWEEN 1 AND 65535),
            ground_station_port INTEGER NOT NULL CHECK (ground_station_port BETWEEN 1 AND 65535),
            ground_station_protocol TEXT NOT NULL CHECK (ground_station_protocol IN ('tcp', 'udp')),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE channel_listeners (
            transport TEXT NOT NULL CHECK (transport IN ('tcp', 'udp')),
            port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
            channel_id TEXT NOT NULL REFERENCES port_channels(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('uav', 'ground_station')),
            PRIMARY KEY (transport, port),
            UNIQUE (channel_id, role)
        )
        """
    )
    connection.execute(
        "CREATE INDEX channel_listeners_channel_id_idx ON channel_listeners(channel_id)"
    )


def _migration_3(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE port_channels ADD COLUMN monitor_udp_port INTEGER")
    used_ports = {
        int(row[0])
        for row in connection.execute(
            "SELECT port FROM channel_listeners WHERE transport = 'udp'"
        ).fetchall()
    }
    next_port = 40000
    for row in connection.execute("SELECT id FROM port_channels ORDER BY rowid").fetchall():
        while next_port in used_ports and next_port < 50000:
            next_port += 1
        if next_port >= 50000:
            raise RuntimeError("No local UDP ports remain for existing telemetry monitors")
        connection.execute(
            "UPDATE port_channels SET monitor_udp_port = ? WHERE id = ?",
            (next_port, row["id"]),
        )
        used_ports.add(next_port)
        next_port += 1
    connection.execute(
        "CREATE UNIQUE INDEX port_channels_monitor_udp_port_idx "
        "ON port_channels(monitor_udp_port)"
    )
    connection.execute(
        """
        CREATE TRIGGER port_channels_avoid_monitor_port_insert
        BEFORE INSERT ON port_channels
        WHEN EXISTS (
            SELECT 1 FROM channel_listeners
            WHERE transport = 'udp' AND port = NEW.monitor_udp_port
        )
        BEGIN
            SELECT RAISE(ABORT, 'monitor port conflicts with a UDP listener');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER channel_listeners_avoid_monitor_port_insert
        BEFORE INSERT ON channel_listeners
        WHEN NEW.transport = 'udp' AND EXISTS (
            SELECT 1 FROM port_channels WHERE monitor_udp_port = NEW.port
        )
        BEGIN
            SELECT RAISE(ABORT, 'UDP port is reserved for telemetry monitoring');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER channel_listeners_avoid_monitor_port_update
        BEFORE UPDATE OF transport, port ON channel_listeners
        WHEN NEW.transport = 'udp' AND EXISTS (
            SELECT 1 FROM port_channels WHERE monitor_udp_port = NEW.port
        )
        BEGIN
            SELECT RAISE(ABORT, 'UDP port is reserved for telemetry monitoring');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER monitor_port_avoids_udp_listener_update
        BEFORE UPDATE OF monitor_udp_port ON port_channels
        WHEN EXISTS (
            SELECT 1 FROM channel_listeners
            WHERE transport = 'udp' AND port = NEW.monitor_udp_port
        )
        BEGIN
            SELECT RAISE(ABORT, 'monitor port conflicts with a UDP listener');
        END
        """
    )


def _migration_4(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        ALTER TABLE port_channels
        ADD COLUMN security_group_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (security_group_state IN (
            'disabled', 'pending', 'synced', 'cleanup_pending', 'error'
        ))
        """
    )
    connection.execute(
        "ALTER TABLE port_channels ADD COLUMN security_group_error TEXT"
    )
    connection.execute(
        """
        CREATE TABLE security_group_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id TEXT UNIQUE,
            channel_id TEXT NOT NULL REFERENCES port_channels(id) ON DELETE RESTRICT,
            role TEXT NOT NULL CHECK (role IN ('uav', 'ground_station')),
            protocol TEXT NOT NULL CHECK (protocol IN ('tcp', 'udp')),
            port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
            source_cidr TEXT NOT NULL,
            description TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('pending_discovery', 'active', 'pending_revoke')
            )
        )
        """
    )
    connection.execute(
        "CREATE INDEX security_group_rules_channel_role_idx "
        "ON security_group_rules(channel_id, role)"
    )
MIGRATIONS = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
}


def initialize_database() -> None:
    connection = connect()
    try:
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"SQLite refused WAL mode (current mode: {mode})")

        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than this application "
                f"supports ({SCHEMA_VERSION})"
            )

        while current_version < SCHEMA_VERSION:
            next_version = current_version + 1
            migration = MIGRATIONS.get(next_version)
            if migration is None:
                raise RuntimeError(f"Missing database migration {next_version}")
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration(connection)
                connection.execute(f"PRAGMA user_version = {next_version}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            current_version = next_version
    finally:
        connection.close()


def get_db() -> Generator[sqlite3.Connection, None, None]:
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()
