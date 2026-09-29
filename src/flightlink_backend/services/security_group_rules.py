from __future__ import annotations

import ipaddress
import sqlite3
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from ..config import Settings, settings
from ..schemas.channels import ChannelWrite
from .aliyun_security_group import (
    DisabledSecurityGroupProvider,
    IngressRule,
    SecurityGroupProvider,
    SecurityGroupRule,
    SecurityGroupSnapshot,
    get_security_group_provider,
)
from .security_group_records import (
    SecurityGroupRuleRecord,
    list_rule_records,
    remove_rule_record,
    save_rule_record,
    set_channel_security_group_state,
)


SyncState = Literal["disabled", "pending", "synced", "cleanup_pending", "error"]
_SYNC_ERROR = (
    "Unable to synchronize Aliyun security-group rules. Check the ECS RAM role, "
    "security-group configuration, and network access."
)
_REVOKE_ERROR = (
    "Unable to verify Aliyun security-group rule cleanup. The saved rule mapping "
    "was retained for a safe retry."
)


@dataclass(frozen=True, slots=True)
class SecurityGroupSyncResult:
    state: SyncState
    error: str | None
    managed_rule_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DesiredRule:
    role: Literal["uav", "ground_station"]
    protocol: Literal["tcp", "udp"]
    port: int
    source_cidr: str
    description: str

    def as_ingress_rule(self) -> IngressRule:
        return IngressRule(
            protocol=self.protocol,
            port=self.port,
            source_cidr=self.source_cidr,
            description=self.description,
        )


def _ownership_marker(channel_id: UUID, role: str) -> str:
    return f"flightlink:channel:{channel_id}:role:{role}"


def _description(channel_id: UUID, role: str) -> str:
    return f"FlightLink {_ownership_marker(channel_id, role)} managed ingress rule"


def _parse_cidrs(value: str | None) -> list[str]:
    if value is None:
        raise ValueError("CIDR source configuration is required")
    cidrs: list[str] = []
    seen: set[str] = set()
    for entry in value.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        network = ipaddress.ip_network(candidate, strict=False)
        normalized = str(network)
        if normalized not in seen:
            seen.add(normalized)
            cidrs.append(normalized)
    if not cidrs:
        raise ValueError("At least one CIDR source is required")
    return cidrs


def _desired_rules(
    channel_id: UUID, payload: ChannelWrite, config: Settings
) -> list[_DesiredRule]:
    if not payload.enabled:
        return []

    rules = [
        _DesiredRule(
            role="uav",
            protocol="udp",
            port=payload.uav_udp_port,
            source_cidr=cidr,
            description=_description(channel_id, "uav"),
        )
        for cidr in _parse_cidrs(config.aliyun_uav_source_cidrs)
    ]
    rules.extend(
        _DesiredRule(
            role="ground_station",
            protocol=payload.ground_station_protocol,
            port=payload.ground_station_port,
            source_cidr=cidr,
            description=_description(channel_id, "ground_station"),
        )
        for cidr in _parse_cidrs(config.aliyun_gcs_source_cidrs)
    )
    return rules


def _port_range_matches(port_range: str, port: int) -> bool:
    pieces = port_range.split("/", maxsplit=1)
    return len(pieces) == 2 and pieces[0] == str(port) and pieces[1] == str(port)


def _source_matches(actual: str | None, expected: str) -> bool:
    if actual is None:
        return False
    try:
        return ipaddress.ip_network(actual, strict=False) == ipaddress.ip_network(
            expected, strict=False
        )
    except ValueError:
        return False


def _same_rule(rule: SecurityGroupRule, desired: _DesiredRule) -> bool:
    return (
        rule.direction.lower() == "ingress"
        and rule.protocol.lower() == desired.protocol
        and _port_range_matches(rule.port_range, desired.port)
        and _source_matches(rule.source_cidr, desired.source_cidr)
        and rule.policy.lower() == "accept"
    )


def _has_marker(rule: SecurityGroupRule, channel_id: UUID, role: str) -> bool:
    return _ownership_marker(channel_id, role) in (rule.description or "").lower()


def _record_matches(record: SecurityGroupRuleRecord, desired: _DesiredRule) -> bool:
    return (
        record.role == desired.role
        and record.protocol == desired.protocol
        and record.port == desired.port
        and _source_matches(record.source_cidr, desired.source_cidr)
    )


def _save_active_owned_rule(
    connection: sqlite3.Connection,
    channel_id: UUID,
    desired: _DesiredRule,
    cloud_rule: SecurityGroupRule,
) -> None:
    records = list_rule_records(connection, channel_id)
    existing = next(
        (record for record in records if record.rule_id == cloud_rule.rule_id), None
    )
    if existing is None:
        existing = next(
            (
                record
                for record in records
                if record.rule_id is None
                and record.state == "pending_discovery"
                and _record_matches(record, desired)
            ),
            None,
        )
    save_rule_record(
        connection,
        SecurityGroupRuleRecord(
            id=existing.id if existing else None,
            rule_id=cloud_rule.rule_id,
            channel_id=channel_id,
            role=desired.role,
            protocol=desired.protocol,
            port=desired.port,
            source_cidr=desired.source_cidr,
            description=desired.description,
            state="active",
        ),
    )


def _ensure_pending_record(
    connection: sqlite3.Connection, channel_id: UUID, desired: _DesiredRule
) -> None:
    if any(
        record.rule_id is None
        and record.state == "pending_discovery"
        and _record_matches(record, desired)
        for record in list_rule_records(connection, channel_id)
    ):
        return
    save_rule_record(
        connection,
        SecurityGroupRuleRecord(
            id=None,
            rule_id=None,
            channel_id=channel_id,
            role=desired.role,
            protocol=desired.protocol,
            port=desired.port,
            source_cidr=desired.source_cidr,
            description=desired.description,
            state="pending_discovery",
        ),
    )


def _find_owned_matches(
    snapshot: SecurityGroupSnapshot,
    channel_id: UUID,
    desired: _DesiredRule,
) -> list[SecurityGroupRule]:
    return [
        rule
        for rule in snapshot.rules
        if rule.rule_id
        and _same_rule(rule, desired)
        and _has_marker(rule, channel_id, desired.role)
    ]


def _managed_ids(
    connection: sqlite3.Connection, channel_id: UUID
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                record.rule_id
                for record in list_rule_records(connection, channel_id)
                if record.rule_id is not None
            }
        )
    )


def _finish(
    connection: sqlite3.Connection,
    channel_id: UUID,
    state: SyncState,
    error: str | None = None,
) -> SecurityGroupSyncResult:
    set_channel_security_group_state(connection, channel_id, state, error)
    return SecurityGroupSyncResult(
        state=state,
        error=error,
        managed_rule_ids=_managed_ids(connection, channel_id),
    )


def _provider_is_disabled(provider: SecurityGroupProvider) -> bool:
    return isinstance(provider, DisabledSecurityGroupProvider) or not getattr(
        provider, "enabled", True
    )


def ensure_channel_rules(
    connection: sqlite3.Connection,
    channel_id: UUID,
    payload: ChannelWrite,
) -> SecurityGroupSyncResult:
    provider = get_security_group_provider()
    if _provider_is_disabled(provider):
        return _finish(connection, channel_id, "disabled")
    if not payload.enabled:
        return revoke_channel_rules(connection, channel_id)

    try:
        desired_rules = _desired_rules(channel_id, payload, settings)
    except (ValueError, TypeError):
        return _finish(connection, channel_id, "error", _SYNC_ERROR)

    missing_configuration = getattr(provider, "missing_configuration", []) or []
    if missing_configuration:
        return _finish(connection, channel_id, "error", _SYNC_ERROR)

    try:
        snapshot = provider.describe_security_group()
    except Exception:
        return _finish(connection, channel_id, "error", _SYNC_ERROR)

    failed = False
    for desired in desired_rules:
        matching = [rule for rule in snapshot.rules if _same_rule(rule, desired)]
        owned = [
            rule
            for rule in matching
            if _has_marker(rule, channel_id, desired.role)
        ]
        if owned:
            for rule in owned:
                _save_active_owned_rule(connection, channel_id, desired, rule)
            continue
        if matching:
            # An exact manually managed rule can satisfy this listener, but it never
            # becomes a FlightLink-owned mapping and cannot later be revoked here.
            continue

        _ensure_pending_record(connection, channel_id, desired)
        try:
            provider.authorize_rule(desired.as_ingress_rule())
        except Exception:
            try:
                snapshot = provider.describe_security_group()
            except Exception:
                failed = True
                break
            recovered = _find_owned_matches(snapshot, channel_id, desired)
            for rule in recovered:
                _save_active_owned_rule(connection, channel_id, desired, rule)
            if not recovered:
                failed = True
                break
            continue

        try:
            snapshot = provider.describe_security_group()
        except Exception:
            failed = True
            break
        recovered = _find_owned_matches(snapshot, channel_id, desired)
        if not recovered:
            failed = True
            break
        for rule in recovered:
            _save_active_owned_rule(connection, channel_id, desired, rule)

    unresolved = any(
        record.rule_id is None and record.state == "pending_discovery"
        for record in list_rule_records(connection, channel_id)
    )
    if failed or unresolved:
        return _finish(connection, channel_id, "error", _SYNC_ERROR)
    return _finish(connection, channel_id, "synced")


def revoke_channel_rules(
    connection: sqlite3.Connection,
    channel_id: UUID,
) -> SecurityGroupSyncResult:
    provider = get_security_group_provider()
    if _provider_is_disabled(provider):
        return _finish(connection, channel_id, "disabled")

    return _revoke_records(
        connection, channel_id, list_rule_records(connection, channel_id), provider
    )


def revoke_channel_rule_records(
    connection: sqlite3.Connection,
    channel_id: UUID,
    record_ids: set[int],
) -> SecurityGroupSyncResult:
    """Revoke only the supplied mapping rows, for compensating an update."""
    provider = get_security_group_provider()
    if _provider_is_disabled(provider):
        return _finish(connection, channel_id, "disabled")
    selected = [
        record
        for record in list_rule_records(connection, channel_id)
        if record.id in record_ids
    ]
    return _revoke_records(connection, channel_id, selected, provider)


def _revoke_records(
    connection: sqlite3.Connection,
    channel_id: UUID,
    records: list[SecurityGroupRuleRecord],
    provider: SecurityGroupProvider,
) -> SecurityGroupSyncResult:

    failed = False
    cleanup_pending = False
    for record in records:
        current = record
        if current.rule_id is None:
            try:
                snapshot = provider.describe_security_group()
            except Exception:
                failed = True
                continue
            matching = [
                rule
                for rule in snapshot.rules
                if rule.rule_id
                and _has_marker(rule, channel_id, current.role)
                and rule.protocol.lower() == current.protocol
                and _port_range_matches(rule.port_range, current.port)
                and _source_matches(rule.source_cidr, current.source_cidr)
                and rule.policy.lower() == "accept"
            ]
            if not matching:
                remove_rule_record(connection, current.id)
                continue
            current = SecurityGroupRuleRecord(
                id=current.id,
                rule_id=matching[0].rule_id,
                channel_id=current.channel_id,
                role=current.role,
                protocol=current.protocol,
                port=current.port,
                source_cidr=current.source_cidr,
                description=current.description,
                state="pending_revoke",
            )
            save_rule_record(connection, current)

        pending = SecurityGroupRuleRecord(
            id=current.id,
            rule_id=current.rule_id,
            channel_id=current.channel_id,
            role=current.role,
            protocol=current.protocol,
            port=current.port,
            source_cidr=current.source_cidr,
            description=current.description,
            state="pending_revoke",
        )
        save_rule_record(connection, pending)
        try:
            provider.revoke_rule(current.rule_id)
        except Exception:
            try:
                snapshot = provider.describe_security_group()
            except Exception:
                failed = True
                continue
            if any(rule.rule_id == current.rule_id for rule in snapshot.rules):
                cleanup_pending = True
                continue
            # The ID is absent from a fresh complete snapshot, so the stale mapping
            # is safe to remove without searching for a broader rule tuple.
        remove_rule_record(connection, current.id)

    if failed:
        return _finish(connection, channel_id, "error", _REVOKE_ERROR)
    if cleanup_pending:
        return _finish(connection, channel_id, "cleanup_pending", _REVOKE_ERROR)
    return _finish(connection, channel_id, "synced")


def cleanup_stale_channel_rules(
    connection: sqlite3.Connection,
    channel_id: UUID,
    payload: ChannelWrite,
) -> SecurityGroupSyncResult:
    """Remove mapped rules no longer required by the active channel config."""
    provider = get_security_group_provider()
    if _provider_is_disabled(provider):
        return _finish(connection, channel_id, "disabled")
    try:
        desired = _desired_rules(channel_id, payload, settings)
    except (ValueError, TypeError):
        return _finish(connection, channel_id, "error", _SYNC_ERROR)
    desired_records = [
        record
        for record in list_rule_records(connection, channel_id)
        if any(_record_matches(record, item) for item in desired)
    ]
    desired_ids = {record.id for record in desired_records}
    stale_ids = {
        record.id
        for record in list_rule_records(connection, channel_id)
        if record.id is not None and record.id not in desired_ids
    }
    if not stale_ids:
        return _finish(connection, channel_id, "synced")
    selected = [
        record
        for record in list_rule_records(connection, channel_id)
        if record.id in stale_ids
    ]
    return _revoke_records(connection, channel_id, selected, provider)


def rule_record_ids_for_payload(
    connection: sqlite3.Connection,
    channel_id: UUID,
    payload: ChannelWrite,
) -> set[int]:
    """Return persisted mappings that are required by a specific channel payload."""
    desired = _desired_rules(channel_id, payload, settings)
    return {
        record.id
        for record in list_rule_records(connection, channel_id)
        if record.id is not None
        and any(_record_matches(record, item) for item in desired)
    }
