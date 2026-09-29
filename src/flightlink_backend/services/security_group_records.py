from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal
from uuid import UUID


SecurityGroupSyncState = Literal[
    "disabled", "pending", "synced", "cleanup_pending", "error"
]
SecurityGroupRuleState = Literal[
    "pending_discovery", "active", "pending_revoke"
]
SecurityGroupRuleRole = Literal["uav", "ground_station"]
SecurityGroupRuleProtocol = Literal["tcp", "udp"]


@dataclass(frozen=True, slots=True)
class SecurityGroupRuleRecord:
    id: int | None
    rule_id: str | None
    channel_id: UUID
    role: SecurityGroupRuleRole
    protocol: SecurityGroupRuleProtocol
    port: int
    source_cidr: str
    description: str
    state: SecurityGroupRuleState


def _record_from_row(row: sqlite3.Row) -> SecurityGroupRuleRecord:
    return SecurityGroupRuleRecord(
        id=row["id"],
        rule_id=row["rule_id"],
        channel_id=UUID(row["channel_id"]),
        role=row["role"],
        protocol=row["protocol"],
        port=row["port"],
        source_cidr=row["source_cidr"],
        description=row["description"],
        state=row["state"],
    )


def list_rule_records(
    connection: sqlite3.Connection,
    channel_id: UUID,
) -> list[SecurityGroupRuleRecord]:
    rows = connection.execute(
        """
        SELECT id, rule_id, channel_id, role, protocol, port,
               source_cidr, description, state
        FROM security_group_rules
        WHERE channel_id = ?
        ORDER BY role, id
        """,
        (str(channel_id),),
    ).fetchall()
    return [_record_from_row(row) for row in rows]


def save_rule_record(
    connection: sqlite3.Connection,
    record: SecurityGroupRuleRecord,
) -> int:
    values = (
        record.rule_id,
        str(record.channel_id),
        record.role,
        record.protocol,
        record.port,
        record.source_cidr,
        record.description,
        record.state,
    )
    if record.id is None:
        cursor = connection.execute(
            """
            INSERT INTO security_group_rules(
                rule_id, channel_id, role, protocol, port,
                source_cidr, description, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return int(cursor.lastrowid)

    cursor = connection.execute(
        """
        UPDATE security_group_rules
        SET rule_id = ?, channel_id = ?, role = ?, protocol = ?, port = ?,
            source_cidr = ?, description = ?, state = ?
        WHERE id = ?
        """,
        (*values, record.id),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"Security-group rule record {record.id} was not found")
    return record.id


def set_channel_security_group_state(
    connection: sqlite3.Connection,
    channel_id: UUID,
    state: SecurityGroupSyncState,
    error: str | None = None,
) -> None:
    cursor = connection.execute(
        """
        UPDATE port_channels
        SET security_group_state = ?, security_group_error = ?
        WHERE id = ?
        """,
        (state, error, str(channel_id)),
    )
    if cursor.rowcount != 1:
        raise LookupError(f"Port channel {channel_id} was not found")


def remove_rule_record(connection: sqlite3.Connection, record_id: int) -> None:
    connection.execute(
        "DELETE FROM security_group_rules WHERE id = ?",
        (record_id,),
    )
