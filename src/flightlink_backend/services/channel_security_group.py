from __future__ import annotations

import sqlite3
from uuid import UUID, uuid4

from ..schemas.channels import ChannelPublic, ChannelWrite
from .capture_files import remove_capture_config
from .capture_manager import stop_capture_service
from .channel_runtime import reconcile_channel_runtime, runtime_matches_channel
from .channel_operations import channel_operation_lock
from .channels import (
    create_channel,
    delete_channel,
    get_channel,
    update_channel,
)
from .router_manager import stop_router_service
from .security_group_records import (
    list_rule_records,
    set_channel_security_group_state,
)
from .security_group_rules import (
    SecurityGroupSyncResult,
    cleanup_stale_channel_rules,
    ensure_channel_rules,
    revoke_channel_rule_records,
    revoke_channel_rules,
    rule_record_ids_for_payload,
)
from .telemetry import get_mavlink_telemetry_monitor


_UPDATE_ERROR = (
    "The requested channel update did not reach its active runtime state. "
    "The previous local configuration was restored."
)
_ROLLBACK_ERROR = (
    "The channel runtime failed and its previous local configuration could not be "
    "restored. Both ingress mappings were retained for manual retry."
)
_DELETE_ERROR = (
    "Aliyun security-group cleanup did not complete. The channel and its rule "
    "mappings were retained for a safe retry."
)


class ChannelSecurityGroupFailure(RuntimeError):
    """Raised when a managed cloud-rule operation cannot safely complete."""


def _payload_for(channel: ChannelPublic) -> ChannelWrite:
    return ChannelWrite(
        uav_udp_port=channel.uav_udp_port,
        ground_station_port=channel.ground_station_port,
        ground_station_protocol=channel.ground_station_protocol,
        enabled=channel.enabled,
    )


def _new_mapping_ids(
    connection: sqlite3.Connection,
    channel_id: UUID,
    previous_ids: set[int],
    protected_ids: set[int],
) -> set[int]:
    return {
        record.id
        for record in list_rule_records(connection, channel_id)
        if record.id is not None
        and record.id not in previous_ids
        and record.id not in protected_ids
    }


def _compensate_new_mappings(
    connection: sqlite3.Connection,
    channel_id: UUID,
    record_ids: set[int],
) -> bool:
    if not record_ids:
        return True
    result = revoke_channel_rule_records(connection, channel_id, record_ids)
    return result.state in {"synced", "disabled"} and not any(
        record.id in record_ids for record in list_rule_records(connection, channel_id)
    )


def _set_update_failure(
    connection: sqlite3.Connection,
    channel_id: UUID,
    *,
    cleanup_failed: bool,
) -> None:
    set_channel_security_group_state(
        connection,
        channel_id,
        "cleanup_pending" if cleanup_failed else "error",
        _DELETE_ERROR if cleanup_failed else _UPDATE_ERROR,
    )


def _runtime_ready(channel: ChannelPublic) -> bool:
    return runtime_matches_channel(channel)


def create_managed_channel(
    connection: sqlite3.Connection,
    payload: ChannelWrite,
) -> ChannelPublic:
    """Create local state, sync ingress, then start runtime only when safe."""
    channel_id = uuid4()
    with channel_operation_lock(channel_id):
        channel = create_channel(connection, payload, channel_id=channel_id)
        result = ensure_channel_rules(connection, channel.id, payload)
        channel = get_channel(connection, channel.id)
        if result.state not in {"synced", "disabled"}:
            return channel
        return reconcile_channel_runtime(channel)


def update_managed_channel(
    connection: sqlite3.Connection,
    channel_id: UUID,
    payload: ChannelWrite,
) -> ChannelPublic:
    with channel_operation_lock(channel_id):
        return _update_managed_channel_locked(connection, channel_id, payload)


def _update_managed_channel_locked(
    connection: sqlite3.Connection,
    channel_id: UUID,
    payload: ChannelWrite,
) -> ChannelPublic:
    """Add replacement ingress, switch runtime, then clean old managed rules."""
    old_channel = get_channel(connection, channel_id)
    old_payload = _payload_for(old_channel)
    previous_record_ids = {
        record.id
        for record in list_rule_records(connection, channel_id)
        if record.id is not None
    }

    if payload.enabled:
        sync_result = ensure_channel_rules(connection, channel_id, payload)
    else:
        # Keep the old ingress in place until runtime has stopped successfully.
        sync_result = SecurityGroupSyncResult(
            state="synced",
            error=None,
            managed_rule_ids=(),
        )
    try:
        protected_old_ids = rule_record_ids_for_payload(
            connection, channel_id, old_payload
        )
    except (ValueError, TypeError):
        # If old requirements cannot be reconstructed, preserve all discovered rules.
        protected_old_ids = {
            record.id
            for record in list_rule_records(connection, channel_id)
            if record.id is not None
        }
    new_record_ids = _new_mapping_ids(
        connection, channel_id, previous_record_ids, protected_old_ids
    )

    if sync_result.state not in {"synced", "disabled"}:
        cleanup_ok = _compensate_new_mappings(
            connection, channel_id, new_record_ids
        )
        if not cleanup_ok:
            set_channel_security_group_state(
                connection, channel_id, "cleanup_pending", _DELETE_ERROR
            )
        else:
            set_channel_security_group_state(
                connection,
                channel_id,
                "error",
                sync_result.error or _UPDATE_ERROR,
            )
        return get_channel(connection, channel_id)

    try:
        updated = update_channel(connection, channel_id, payload)
    except Exception:
        cleanup_ok = _compensate_new_mappings(
            connection, channel_id, new_record_ids
        )
        if cleanup_ok:
            set_channel_security_group_state(
                connection,
                channel_id,
                old_channel.security_group.state,
                old_channel.security_group.error,
            )
        else:
            set_channel_security_group_state(
                connection, channel_id, "cleanup_pending", _DELETE_ERROR
            )
        raise

    active_result = reconcile_channel_runtime(updated, restart_if_active=True)
    if not _runtime_ready(active_result):
        try:
            restored = update_channel(connection, channel_id, old_payload)
        except Exception:
            set_channel_security_group_state(
                connection, channel_id, "error", _ROLLBACK_ERROR
            )
            return get_channel(connection, channel_id)
        restored_runtime = reconcile_channel_runtime(
            restored, restart_if_active=True
        )
        cleanup_ok = _compensate_new_mappings(
            connection, channel_id, new_record_ids
        )
        _set_update_failure(
            connection, channel_id, cleanup_failed=not cleanup_ok
        )
        current = get_channel(connection, channel_id)
        return restored_runtime.model_copy(update={"security_group": current.security_group})

    cleanup_result = cleanup_stale_channel_rules(connection, channel_id, payload)
    current = get_channel(connection, channel_id)
    if cleanup_result.state == "error":
        return active_result.model_copy(update={"security_group": current.security_group})
    return active_result.model_copy(update={"security_group": current.security_group})


def delete_managed_channel(
    connection: sqlite3.Connection,
    channel_id: UUID,
) -> None:
    with channel_operation_lock(channel_id):
        _delete_managed_channel_locked(connection, channel_id)


def _delete_managed_channel_locked(
    connection: sqlite3.Connection,
    channel_id: UUID,
) -> None:
    """Stop services, revoke mapped rules, then remove channel-owned local state."""
    get_channel(connection, channel_id)
    stop_capture_service(channel_id)
    stop_router_service(channel_id)

    result = revoke_channel_rules(connection, channel_id)
    remaining = list_rule_records(connection, channel_id)
    if result.state not in {"synced", "disabled"} or remaining:
        raise ChannelSecurityGroupFailure(_DELETE_ERROR)

    remove_capture_config(channel_id)
    delete_channel(connection, channel_id)
    get_mavlink_telemetry_monitor().stop(channel_id)
