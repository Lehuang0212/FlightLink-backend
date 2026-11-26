"""Linux systemd unit supervisor and listening socket verifier (`ss`)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Literal

from flightlink.adapters.interfaces import (
    InfrastructureError,
    ListeningSocket,
    UnitRuntimeInfo,
)


def parse_ss_output(raw: str) -> list[ListeningSocket]:
    """Parse `ss -H -l -n -u -t -p` lines into ListeningSocket records."""
    sockets: list[ListeningSocket] = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        netid = parts[0].lower()
        if netid not in ("tcp", "udp"):
            continue
        local_addr = parts[4]
        # Format: 0.0.0.0:5760 or [::]:5760 or *:14552
        if ":" not in local_addr:
            continue
        host, port_s = local_addr.rsplit(":", 1)
        if not port_s.isdigit():
            continue
        port = int(port_s)
        proc_blob = " ".join(parts[6:]) if len(parts) > 6 else ""
        proc_match = re.search(r'"([^"]+)"', proc_blob)
        pid_match = re.search(r"pid=(\d+)", proc_blob)
        sockets.append(
            ListeningSocket(
                protocol=netid,  # type: ignore[arg-type]
                address=host,
                port=port,
                pid=int(pid_match.group(1)) if pid_match else None,
                process_name=proc_match.group(1) if proc_match else "unknown",
                group_id=None,
            )
        )
    return sockets


class LinuxSystemdSupervisor:
    """Manages per-group `flightlink-router@<group_id>.service` and `flightlink-mavproxy@<group_id>.service`."""

    def __init__(self, *, runtime_dir: Path | None = None) -> None:
        self.runtime_dir = runtime_dir or Path("/var/lib/flightlink/run")

    def _run_systemctl(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=20,
        )

    def start_router(
        self,
        *,
        group_id: str,
        config_path: str,
        dtu_port: int,
        gcs_protocol: Literal["tcp", "udp"],
        gcs_port: int,
    ) -> UnitRuntimeInfo:
        unit = f"flightlink-router@{group_id}.service"
        res = self._run_systemctl(["start", unit], check=False)
        if res.returncode != 0:
            raise InfrastructureError("ROUTER_START_FAILED", res.stderr.strip() or f"Failed to start {unit}")
        return self.inspect_router(group_id)

    def stop_router(self, group_id: str) -> None:
        self._run_systemctl(["stop", f"flightlink-router@{group_id}.service"], check=False)

    def inspect_router(self, group_id: str) -> UnitRuntimeInfo:
        return self._inspect_unit(f"flightlink-router@{group_id}.service")

    def start_mavproxy(self, *, group_id: str, internal_endpoint: str) -> UnitRuntimeInfo:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        env_path = self.runtime_dir / f"mavproxy-{group_id}.env"
        env_path.write_text(f"INTERNAL_TAP_ENDPOINT={internal_endpoint}\n", encoding="utf-8", newline="\n")
        try:
            os.chmod(env_path, 0o644)
        except OSError:
            pass
        unit = f"flightlink-mavproxy@{group_id}.service"
        res = self._run_systemctl(["start", unit], check=False)
        if res.returncode != 0:
            raise InfrastructureError("MAVPROXY_START_FAILED", res.stderr.strip() or f"Failed to start {unit}")
        return self.inspect_mavproxy(group_id)

    def stop_mavproxy(self, group_id: str) -> None:
        self._run_systemctl(["stop", f"flightlink-mavproxy@{group_id}.service"], check=False)

    def inspect_mavproxy(self, group_id: str) -> UnitRuntimeInfo:
        return self._inspect_unit(f"flightlink-mavproxy@{group_id}.service")

    def _inspect_unit(self, unit_name: str) -> UnitRuntimeInfo:
        res = self._run_systemctl(
            ["show", unit_name, "-p", "ActiveState", "-p", "MainPID", "-p", "ExecMainStartTimestampMonotonic"],
            check=False,
        )
        props: dict[str, str] = {}
        for line in res.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
        active = props.get("ActiveState") == "active"
        pid_raw = props.get("MainPID", "0")
        pid = int(pid_raw) if pid_raw.isdigit() and int(pid_raw) > 0 else None
        return UnitRuntimeInfo(unit_name=unit_name, active=active, pid=pid)

    def list_running_router_groups(self) -> list[str]:
        res = self._run_systemctl(
            ["list-units", "--type=service", "--state=running", "--no-legend", "flightlink-router@*.service"],
            check=False,
        )
        groups: list[str] = []
        for line in res.stdout.splitlines():
            unit = line.split()[0] if line.split() else ""
            m = re.match(r"^flightlink-router@(.+)\.service$", unit)
            if m:
                groups.append(m.group(1))
        return groups


class LinuxSocketInspector:
    """Verifies real listening sockets via `ss` and maps PIDs to managed router units."""

    def __init__(self, supervisor: LinuxSystemdSupervisor) -> None:
        self.supervisor = supervisor

    def list_listening_sockets(self) -> list[ListeningSocket]:
        res = subprocess.run(
            ["ss", "-H", "-l", "-n", "-u", "-t", "-p"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if res.returncode != 0:
            return []
        return parse_ss_output(res.stdout)

    def check_ports_available(
        self,
        *,
        group_id: str,
        endpoints: list[tuple[Literal["udp", "tcp"], int]],
    ) -> list[ListeningSocket]:
        rt = self.supervisor.inspect_router(group_id)
        own_pid = rt.pid
        wanted = set(endpoints)
        conflicts: list[ListeningSocket] = []
        for s in self.list_listening_sockets():
            if own_pid is not None and s.pid == own_pid:
                continue
            if (s.protocol, s.port) in wanted:
                conflicts.append(s)
        return conflicts

    def verify_group_listening(
        self,
        *,
        group_id: str,
        endpoints: list[tuple[Literal["udp", "tcp"], int]],
        timeout_seconds: float = 3.0,
    ) -> tuple[bool, list[str]]:
        """Poll briefly after Type=simple systemd start so mavlink-routerd has time to bind sockets."""
        deadline = time.monotonic() + timeout_seconds
        last_bound: list[str] = []
        while True:
            rt = self.supervisor.inspect_router(group_id)
            if not rt.active or rt.pid is None:
                return False, last_bound
            owned = [s for s in self.list_listening_sockets() if s.pid == rt.pid]
            owned_pairs = {(s.protocol, s.port) for s in owned}
            last_bound = sorted(f"{s.protocol}://{s.address}:{s.port}" for s in owned)
            # Explicitly reject stray default TCP 5760 when not requested!
            if ("tcp", 5760) in owned_pairs and ("tcp", 5760) not in set(endpoints):
                return False, last_bound
            if all(ep in owned_pairs for ep in endpoints):
                return True, last_bound
            if time.monotonic() >= deadline:
                return False, last_bound
            time.sleep(0.1)
