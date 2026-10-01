from __future__ import annotations

import sqlite3
import socket
import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

from ..schemas.channels import ChannelPublic, ChannelWrite
from ..schemas.security_group import SecurityGroupChannelPublic
from .router_config import (
    RouterConfigError,
    config_file_path,
    remove_router_config,
    render_router_config,
    write_router_config,
)


class ChannelNotFound(LookupError):
    pass


class ChannelConflict(ValueError):
    pass


class ChannelConfigFailure(RuntimeError):
    pass


def _as_public(row: sqlite3.Row) -> ChannelPublic:
    return ChannelPublic(
        id=UUID(row["id"]),
        uav_udp_port=row["uav_udp_port"],
        ground_station_port=row["ground_station_port"],
        monitor_udp_port=row["monitor_udp_port"],
        ground_station_protocol=row["ground_station_protocol"],
        enabled=bool(row["enabled"]),
        config_file_name=config_file_path(row["id"]).name,
        created_at=datetime.fromtimestamp(row["created_at"], tz=timezone.utc),
        updated_at=datetime.fromtimestamp(row["updated_at"], tz=timezone.utc),
        security_group=SecurityGroupChannelPublic(
            state=row["security_group_state"],
            error=row["security_group_error"],
        ),
    )


def _get_row(connection: sqlite3.Connection, channel_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM port_channels WHERE id = ?",
        (channel_id,),
    ).fetchone()
    if row is None:
        raise ChannelNotFound(channel_id)
    return row


def _render_row_config(row: sqlite3.Row) -> str:
    return render_router_config(
        uav_udp_port=row["uav_udp_port"],
        ground_station_port=row["ground_station_port"],
        ground_station_protocol=row["ground_station_protocol"],
        monitor_udp_port=row["monitor_udp_port"],
    )


def _allocate_monitor_udp_port(
    connection: sqlite3.Connection,
    payload: ChannelWrite,
) -> int:
    """Find a free loopback UDP port reserved for this channel's telemetry mirror."""
    excluded_ports = {payload.uav_udp_port}
    if payload.ground_station_protocol == "udp":
        excluded_ports.add(payload.ground_station_port)
    for port in range(40000, 50000):
        if port in excluded_ports:
            continue
        used = connection.execute(
            """
            SELECT 1 FROM port_channels WHERE monitor_udp_port = ?
            UNION ALL
            SELECT 1 FROM channel_listeners WHERE transport = 'udp' AND port = ?
            LIMIT 1
            """,
            (port, port),
        ).fetchone()
        if used:
            continue
        candidate = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            candidate.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            candidate.close()
        return port
    raise ChannelConflict("No free local UDP port is available for telemetry monitoring")


def _write_listeners(
    connection: sqlite3.Connection,
    channel_id: str,
    payload: ChannelWrite,
) -> None:
    connection.execute(
        "INSERT INTO channel_listeners(transport, port, channel_id, role) VALUES ('udp', ?, ?, 'uav')",
        (payload.uav_udp_port, channel_id),
    )
    connection.execute(
        "INSERT INTO channel_listeners(transport, port, channel_id, role) VALUES (?, ?, ?, 'ground_station')",
        (payload.ground_station_protocol, payload.ground_station_port, channel_id),
    )


def _write_config_or_rollback(
    connection: sqlite3.Connection,
    channel_id: str,
    old_config: str | None,
) -> None:
    connection.rollback()
    try:
        if old_config is None:
            remove_router_config(channel_id)
        else:
            write_router_config(channel_id, old_config)
    except RouterConfigError as exc:
        raise ChannelConfigFailure("Unable to restore the previous router config") from exc


def list_channels(connection: sqlite3.Connection) -> list[ChannelPublic]:
    rows = connection.execute(
        "SELECT * FROM port_channels ORDER BY uav_udp_port, id"
    ).fetchall()
    return [_as_public(row) for row in rows]


def get_channel(connection: sqlite3.Connection, channel_id: UUID) -> ChannelPublic:
    return _as_public(_get_row(connection, str(channel_id)))


def create_channel(
    connection: sqlite3.Connection,
    payload: ChannelWrite,
    *,
    channel_id: UUID | None = None,
) -> ChannelPublic:
    channel_id = str(channel_id or uuid4())
    now = int(time.time())
    connection.execute("BEGIN IMMEDIATE")
    try:
        monitor_udp_port = _allocate_monitor_udp_port(connection, payload)
        connection.execute(
            """
            INSERT INTO port_channels(
                id, uav_udp_port, ground_station_port, ground_station_protocol,
                enabled, created_at, updated_at, monitor_udp_port
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                payload.uav_udp_port,
                payload.ground_station_port,
                payload.ground_station_protocol,
                int(payload.enabled),
                now,
                now,
                monitor_udp_port,
            ),
        )
        _write_listeners(connection, channel_id, payload)
        row = _get_row(connection, channel_id)
        write_router_config(channel_id, _render_row_config(row))
        connection.commit()
    except sqlite3.IntegrityError as exc:
        _write_config_or_rollback(connection, channel_id, None)
        raise ChannelConflict("One of the requested local listener ports is already assigned") from exc
    except RouterConfigError as exc:
        _write_config_or_rollback(connection, channel_id, None)
        raise ChannelConfigFailure(str(exc)) from exc
    except Exception:
        _write_config_or_rollback(connection, channel_id, None)
        raise
    return _as_public(_get_row(connection, channel_id))


def update_channel(
    connection: sqlite3.Connection,
    channel_id: UUID,
    payload: ChannelWrite,
) -> ChannelPublic:
    key = str(channel_id)
    _get_row(connection, key)
    config_path = config_file_path(key)
    old_config = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    now = int(time.time())

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            UPDATE port_channels
            SET uav_udp_port = ?, ground_station_port = ?, ground_station_protocol = ?,
                enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                payload.uav_udp_port,
                payload.ground_station_port,
                payload.ground_station_protocol,
                int(payload.enabled),
                now,
                key,
            ),
        )
        connection.execute("DELETE FROM channel_listeners WHERE channel_id = ?", (key,))
        _write_listeners(connection, key, payload)
        row = _get_row(connection, key)
        write_router_config(key, _render_row_config(row))
        connection.commit()
    except sqlite3.IntegrityError as exc:
        _write_config_or_rollback(connection, key, old_config)
        raise ChannelConflict("One of the requested local listener ports is already assigned") from exc
    except RouterConfigError as exc:
        _write_config_or_rollback(connection, key, old_config)
        raise ChannelConfigFailure(str(exc)) from exc
    except Exception:
        _write_config_or_rollback(connection, key, old_config)
        raise
    return _as_public(_get_row(connection, key))


def delete_channel(connection: sqlite3.Connection, channel_id: UUID) -> None:
    key = str(channel_id)
    _get_row(connection, key)
    config_path = config_file_path(key)
    old_config = config_path.read_text(encoding="utf-8") if config_path.exists() else None

    connection.execute("BEGIN IMMEDIATE")
    try:
        remove_router_config(key)
        connection.execute("DELETE FROM port_channels WHERE id = ?", (key,))
        connection.commit()
    except RouterConfigError as exc:
        _write_config_or_rollback(connection, key, old_config)
        raise ChannelConfigFailure(str(exc)) from exc
    except Exception:
        _write_config_or_rollback(connection, key, old_config)
        raise


def synchronize_channel_configs(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT * FROM port_channels ORDER BY id").fetchall()
    for row in rows:
        try:
            write_router_config(row["id"], _render_row_config(row))
        except RouterConfigError as exc:
            raise ChannelConfigFailure(str(exc)) from exc
