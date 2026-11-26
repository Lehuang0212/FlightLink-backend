"""Per-group mavlink-routerd configuration renderer.

Critical guarantees:
1. When `gcs_protocol == 'udp'`, explicitly sets `TcpServerPort = 0` so mavlink-routerd
   NEVER opens its built-in default TCP 5760 port!
2. Internal bypass observation endpoint for MAVProxy binds strictly to 127.0.0.1 using an
   internal loopback port derived deterministically outside business ports — never consuming
   user ports and never opening public security group rules.
3. Config files are written atomically (write temp + fsync + os.replace).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal


def derive_internal_tap_port(group_id: str) -> int:
    """Derive a deterministic loopback-only observation port in 39000..48999 for MAVProxy bypass."""
    digest = hashlib.sha256(group_id.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:2], "big") % 10000
    return 39000 + offset


def render_router_config(
    *,
    group_id: str,
    dtu_port: int,
    gcs_protocol: Literal["tcp", "udp"],
    gcs_port: int,
    internal_tap_port: int | None = None,
) -> str:
    """Render an isolated mavlink-routerd config for one port group."""
    tap_port = internal_tap_port or derive_internal_tap_port(group_id)
    # CRITICAL: TcpServerPort MUST be 0 for UDP GCS groups to prevent default TCP 5760!
    tcp_server_port = gcs_port if gcs_protocol == "tcp" else 0

    lines = [
        f"# Managed by flightlink-agent for group_id={group_id}",
        "[General]",
        f"TcpServerPort = {tcp_server_port}",
        "ReportStats = false",
        "MavlinkDialect = auto",
        "",
        "[UdpEndpoint dtu]",
        "Mode = Server",
        "Address = 0.0.0.0",
        f"Port = {dtu_port}",
        "",
    ]

    if gcs_protocol == "udp":
        lines.extend(
            [
                "[UdpEndpoint gcs]",
                "Mode = Server",
                "Address = 0.0.0.0",
                f"Port = {gcs_port}",
                "",
            ]
        )

    # Passive loopback-only bypass tap for MAVProxy observer (no public SG rule, no user port).
    lines.extend(
        [
            "[UdpEndpoint mavproxy_bypass]",
            "Mode = Normal",
            "Address = 127.0.0.1",
            f"Port = {tap_port}",
            "",
        ]
    )
    return "\n".join(lines)


def write_router_config_atomic(
    config_dir: Path,
    *,
    group_id: str,
    dtu_port: int,
    gcs_protocol: Literal["tcp", "udp"],
    gcs_port: int,
    suffix: str = "",
) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / f"{group_id}{suffix}.conf"
    tmp = config_dir / f".{group_id}{suffix}.conf.tmp"
    content = render_router_config(
        group_id=group_id,
        dtu_port=dtu_port,
        gcs_protocol=gcs_protocol,
        gcs_port=gcs_port,
    )
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(tmp, 0o644)
    except OSError:
        pass
    os.replace(tmp, target)
    return target


def remove_router_config(config_dir: Path, group_id: str, suffix: str = "") -> None:
    path = config_dir / f"{group_id}{suffix}.conf"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
