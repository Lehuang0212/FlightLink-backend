from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..config import settings


class RouterConfigError(RuntimeError):
    """Raised when a managed mavlink-router config cannot be written."""


def config_file_path(channel_id: str) -> Path:
    return settings.router_config_dir / f"channel-{channel_id}.conf"


def render_router_config(
    *,
    uav_udp_port: int,
    ground_station_port: int,
    ground_station_protocol: str,
    monitor_udp_port: int,
) -> str:
    tcp_server_port = (
        ground_station_port if ground_station_protocol == "tcp" else 0
    )
    lines = [
        "[General]",
        "ReportStats = true",
        "DebugLogLevel = info",
        f"TcpServerPort = {tcp_server_port}",
        "",
        "[UdpEndpoint uav_uplink]",
        "Mode = Server",
        "Address = 0.0.0.0",
        f"Port = {uav_udp_port}",
        "",
        "[UdpEndpoint telemetry_monitor]",
        "Mode = Normal",
        "Address = 127.0.0.1",
        f"Port = {monitor_udp_port}",
    ]
    if ground_station_protocol == "udp":
        lines.extend(
            [
                "",
                "[UdpEndpoint ground_station]",
                "Mode = Server",
                "Address = 0.0.0.0",
                f"Port = {ground_station_port}",
            ]
        )
    return "\n".join(lines) + "\n"


def write_router_config(channel_id: str, content: str) -> Path:
    config_path = config_file_path(channel_id)
    config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        dir=config_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            temporary_path.chmod(0o640)
        os.replace(temporary_path, config_path)
        if os.name == "posix":
            directory_fd = os.open(config_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise RouterConfigError("Unable to write the managed router config") from exc
    return config_path


def remove_router_config(channel_id: str) -> None:
    try:
        config_file_path(channel_id).unlink(missing_ok=True)
    except OSError as exc:
        raise RouterConfigError("Unable to remove the managed router config") from exc
