from __future__ import annotations

from ..config import settings
from ..schemas.channels import ChannelPublic
from .capture_files import CaptureFileError, write_capture_config
from .capture_manager import get_capture_manager, reconcile_capture_service
from .router_manager import reconcile_router_service
from .telemetry import get_mavlink_telemetry_monitor


def reconcile_channel_runtime(
    channel: ChannelPublic,
    *,
    restart_if_active: bool = False,
) -> ChannelPublic:
    """Apply telemetry, router, and capture runtime state for one local channel."""
    telemetry_monitor = get_mavlink_telemetry_monitor()
    try:
        telemetry_monitor.start(
            channel.id,
            channel.monitor_udp_port,
            channel.uav_udp_port,
            channel.ground_station_port,
        )
    except RuntimeError:
        # The channel remains manageable; its telemetry snapshot reports availability.
        pass

    runtime = reconcile_router_service(
        channel.id,
        enabled=channel.enabled,
        restart_if_active=restart_if_active,
    )
    capture_manager = get_capture_manager()
    capture_runtime = capture_manager.status(channel.id)
    try:
        write_capture_config(
            channel.id,
            uav_udp_port=channel.uav_udp_port,
            ground_station_port=channel.ground_station_port,
            ground_station_protocol=channel.ground_station_protocol,
            monitor_udp_port=channel.monitor_udp_port,
        )
        capture_runtime = reconcile_capture_service(
            channel.id,
            enabled=channel.enabled,
            restart_if_active=restart_if_active,
        )
    except CaptureFileError as exc:
        capture_runtime = capture_runtime.model_copy(update={"error": str(exc)})

    telemetry = telemetry_monitor.snapshot(
        channel.id,
        ground_station_protocol=channel.ground_station_protocol,
        ground_station_port=channel.ground_station_port,
    )
    return channel.model_copy(
        update={
            "runtime": runtime,
            "capture_runtime": capture_runtime,
            "telemetry": telemetry,
        }
    )


def runtime_matches_channel(channel: ChannelPublic) -> bool:
    """Return whether managed services reached the requested enabled state."""
    if settings.router_manager_mode == "disabled":
        return True

    if channel.runtime.error or channel.capture_runtime.error:
        return False
    if channel.enabled:
        expected = {"active", "starting"}
    else:
        expected = {"inactive", "stopping"}
    return (
        channel.runtime.state in expected
        and channel.capture_runtime.state in expected
    )
