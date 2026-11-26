"""Adapter interfaces and deterministic fault-injectable fake infrastructure for testing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from flightlink.clock import Clock, SystemClock


class InfrastructureError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class CloudTimeoutError(InfrastructureError):
    """Cloud API call timed out; remote state may or may not have been applied."""

    def __init__(self, message: str = "Cloud security group API timed out") -> None:
        super().__init__("CLOUD_API_TIMEOUT", message, retryable=True)


class CloudAuthTemporaryError(InfrastructureError):
    """Temporary RAM role / STS token error; must not restart running channels."""

    def __init__(self, message: str = "Temporary RAM role credential failure") -> None:
        super().__init__("CLOUD_RAM_AUTH_TEMPORARY", message, retryable=True)


class PortPreemptedError(InfrastructureError):
    """A port is occupied by an unmanaged external OS process."""

    def __init__(self, port: int, protocol: str, owner: str = "external_process") -> None:
        super().__init__(
            "PORT_PREEMPTED",
            f"Port {protocol}/{port} is already occupied by {owner}",
            retryable=True,
        )
        self.port = port
        self.protocol = protocol
        self.owner = owner


@dataclass(frozen=True)
class ListeningSocket:
    protocol: Literal["udp", "tcp"]
    address: str
    port: int
    pid: int | None
    process_name: str
    group_id: str | None = None


@dataclass
class UnitRuntimeInfo:
    unit_name: str
    active: bool
    pid: int | None = None
    uptime_seconds: int = 0
    bound_endpoints: list[str] = field(default_factory=list)
    detail: str | None = None


@dataclass(frozen=True)
class CloudSecurityGroupRule:
    rule_id: str
    security_group_id: str
    protocol: Literal["udp", "tcp"]
    port: int
    source_cidr: str
    description: str


def build_ownership_marker(ecs_instance_id: str, group_id: str, role: str) -> str:
    return f"flightlink:v1:{ecs_instance_id}:{group_id}:{role}"


class ProcessSupervisor(Protocol):
    def start_router(
        self,
        *,
        group_id: str,
        config_path: str,
        dtu_port: int,
        gcs_protocol: Literal["tcp", "udp"],
        gcs_port: int,
    ) -> UnitRuntimeInfo: ...

    def stop_router(self, group_id: str) -> None: ...

    def inspect_router(self, group_id: str) -> UnitRuntimeInfo: ...

    def start_mavproxy(self, *, group_id: str, internal_endpoint: str) -> UnitRuntimeInfo: ...

    def stop_mavproxy(self, group_id: str) -> None: ...

    def inspect_mavproxy(self, group_id: str) -> UnitRuntimeInfo: ...

    def list_running_router_groups(self) -> list[str]: ...


class SocketInspector(Protocol):
    def list_listening_sockets(self) -> list[ListeningSocket]: ...

    def check_ports_available(
        self,
        *,
        group_id: str,
        endpoints: list[tuple[Literal["udp", "tcp"], int]],
    ) -> list[ListeningSocket]:
        """Return any sockets occupying the desired endpoints that are NOT owned by `group_id`."""
        ...

    def verify_group_listening(
        self,
        *,
        group_id: str,
        endpoints: list[tuple[Literal["udp", "tcp"], int]],
    ) -> tuple[bool, list[str]]: ...


class SecurityGroupClient(Protocol):
    def validate_connection(self, *, region: str, security_group_id: str, ram_role: str | None) -> dict[str, str]: ...

    def list_rules(self, *, security_group_id: str) -> list[CloudSecurityGroupRule]: ...

    def authorize_rule(
        self,
        *,
        security_group_id: str,
        protocol: Literal["udp", "tcp"],
        port: int,
        source_cidr: str,
        description: str,
    ) -> CloudSecurityGroupRule: ...

    def revoke_rule(self, *, security_group_id: str, rule_id: str) -> None: ...


class FakeInfrastructure:
    """In-memory deterministic systemd + socket + Aliyun SG adapter with fault injection.

    Implements ProcessSupervisor, SocketInspector, and SecurityGroupClient.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self._pid_seq = 5000
        self._rule_seq = 100
        self.routers: dict[str, dict[str, object]] = {}
        self.mavproxies: dict[str, dict[str, object]] = {}
        self.external_sockets: list[ListeningSocket] = []
        self.cloud_rules: dict[str, CloudSecurityGroupRule] = {}

        # Restart / event counters for asserting non-interference across groups
        self.router_start_counts: dict[str, int] = {}
        self.router_stop_counts: dict[str, int] = {}

        # Fault injection knobs
        self.fail_authorize_timeout_applied: set[tuple[str, int]] = set()  # (protocol, port): applies on cloud THEN times out
        self.fail_authorize_timeout_unapplied: set[tuple[str, int]] = set()  # times out WITHOUT applying
        self.fail_revoke_timeout_applied: set[str] = set()  # rule_id or "*": revokes on cloud THEN times out
        self.fail_revoke_timeout_unapplied: set[str] = set()  # rule_id or "*": times out WITHOUT revoking
        self.fail_list_rules_temp_ram: bool = False
        self.fail_authorize_temp_ram: bool = False
        self.fail_revoke_temp_ram: bool = False
        self.fail_start_router_groups: set[str] = set()
        self.fail_bind_verify_groups: set[str] = set()

    # -- Helper methods for tests --
    def add_external_socket(
        self,
        protocol: Literal["udp", "tcp"],
        port: int,
        process_name: str = "external-daemon",
        pid: int = 9999,
    ) -> None:
        self.external_sockets.append(
            ListeningSocket(
                protocol=protocol,
                address="0.0.0.0",
                port=port,
                pid=pid,
                process_name=process_name,
                group_id=None,
            )
        )

    def clear_external_sockets(self) -> None:
        self.external_sockets.clear()

    def add_manual_cloud_rule(
        self,
        *,
        security_group_id: str,
        protocol: Literal["udp", "tcp"],
        port: int,
        source_cidr: str = "0.0.0.0/0",
        description: str = "manual-admin-rule",
    ) -> CloudSecurityGroupRule:
        self._rule_seq += 1
        rule = CloudSecurityGroupRule(
            rule_id=f"sgr-manual-{self._rule_seq}",
            security_group_id=security_group_id,
            protocol=protocol,
            port=port,
            source_cidr=source_cidr,
            description=description,
        )
        self.cloud_rules[rule.rule_id] = rule
        return rule

    # -- SocketInspector --
    def list_listening_sockets(self) -> list[ListeningSocket]:
        sockets: list[ListeningSocket] = list(self.external_sockets)
        for gid, info in self.routers.items():
            if not info.get("active"):
                continue
            pid = int(info["pid"])  # type: ignore[arg-type]
            for proto, port in info["endpoints"]:  # type: ignore[index]
                sockets.append(
                    ListeningSocket(
                        protocol=proto,
                        address="0.0.0.0",
                        port=port,
                        pid=pid,
                        process_name="mavlink-routerd",
                        group_id=gid,
                    )
                )
        return sockets

    def check_ports_available(
        self,
        *,
        group_id: str,
        endpoints: list[tuple[Literal["udp", "tcp"], int]],
    ) -> list[ListeningSocket]:
        conflicts: list[ListeningSocket] = []
        wanted = set(endpoints)
        for sock in self.list_listening_sockets():
            if sock.group_id == group_id:
                continue
            if (sock.protocol, sock.port) in wanted:
                conflicts.append(sock)
        return conflicts

    def verify_group_listening(
        self,
        *,
        group_id: str,
        endpoints: list[tuple[Literal["udp", "tcp"], int]],
    ) -> tuple[bool, list[str]]:
        if group_id in self.fail_bind_verify_groups:
            return False, []
        owned = [s for s in self.list_listening_sockets() if s.group_id == group_id]
        owned_pairs = {(s.protocol, s.port) for s in owned}
        bound_labels = sorted(f"{s.protocol}://0.0.0.0:{s.port}" for s in owned)
        ok = all(ep in owned_pairs for ep in endpoints)
        # Also verify no stray default TCP 5760 when not requested
        if ("tcp", 5760) in owned_pairs and ("tcp", 5760) not in set(endpoints):
            return False, bound_labels
        return ok, bound_labels

    # -- ProcessSupervisor --
    def start_router(
        self,
        *,
        group_id: str,
        config_path: str,
        dtu_port: int,
        gcs_protocol: Literal["tcp", "udp"],
        gcs_port: int,
    ) -> UnitRuntimeInfo:
        if group_id in self.fail_start_router_groups:
            raise InfrastructureError("ROUTER_START_FAILED", f"Failed to start router for {group_id}")
        endpoints: list[tuple[Literal["udp", "tcp"], int]] = [
            ("udp", dtu_port),
            (gcs_protocol, gcs_port),
        ]
        conflicts = self.check_ports_available(group_id=group_id, endpoints=endpoints)
        if conflicts:
            first = conflicts[0]
            raise PortPreemptedError(first.port, first.protocol, first.process_name)

        self._pid_seq += 1
        pid = self._pid_seq
        self.routers[group_id] = {
            "active": True,
            "pid": pid,
            "started_at": self.clock.now(),
            "config_path": config_path,
            "endpoints": endpoints,
        }
        self.router_start_counts[group_id] = self.router_start_counts.get(group_id, 0) + 1
        return self.inspect_router(group_id)

    def stop_router(self, group_id: str) -> None:
        if group_id in self.routers and self.routers[group_id].get("active"):
            self.routers[group_id]["active"] = False
            self.routers[group_id]["pid"] = None
            self.routers[group_id]["endpoints"] = []
            self.router_stop_counts[group_id] = self.router_stop_counts.get(group_id, 0) + 1

    def inspect_router(self, group_id: str) -> UnitRuntimeInfo:
        unit_name = f"flightlink-router@{group_id}.service"
        info = self.routers.get(group_id)
        if not info or not info.get("active"):
            return UnitRuntimeInfo(unit_name=unit_name, active=False)
        started_at = info["started_at"]
        uptime = max(0, int((self.clock.now() - started_at).total_seconds()))  # type: ignore[operator]
        endpoints = [f"{p}://0.0.0.0:{port}" for p, port in info["endpoints"]]  # type: ignore[index]
        return UnitRuntimeInfo(
            unit_name=unit_name,
            active=True,
            pid=int(info["pid"]),  # type: ignore[arg-type]
            uptime_seconds=uptime,
            bound_endpoints=sorted(endpoints),
        )

    def start_mavproxy(self, *, group_id: str, internal_endpoint: str) -> UnitRuntimeInfo:
        self._pid_seq += 1
        self.mavproxies[group_id] = {
            "active": True,
            "pid": self._pid_seq,
            "started_at": self.clock.now(),
            "internal_endpoint": internal_endpoint,
        }
        return self.inspect_mavproxy(group_id)

    def stop_mavproxy(self, group_id: str) -> None:
        if group_id in self.mavproxies:
            self.mavproxies[group_id]["active"] = False
            self.mavproxies[group_id]["pid"] = None

    def inspect_mavproxy(self, group_id: str) -> UnitRuntimeInfo:
        unit_name = f"flightlink-mavproxy@{group_id}.service"
        info = self.mavproxies.get(group_id)
        if not info or not info.get("active"):
            return UnitRuntimeInfo(unit_name=unit_name, active=False)
        started_at = info["started_at"]
        uptime = max(0, int((self.clock.now() - started_at).total_seconds()))  # type: ignore[operator]
        return UnitRuntimeInfo(
            unit_name=unit_name,
            active=True,
            pid=int(info["pid"]),  # type: ignore[arg-type]
            uptime_seconds=uptime,
        )

    def list_running_router_groups(self) -> list[str]:
        return [gid for gid, info in self.routers.items() if info.get("active")]

    # -- SecurityGroupClient --
    def validate_connection(
        self, *, region: str, security_group_id: str, ram_role: str | None
    ) -> dict[str, str]:
        if self.fail_list_rules_temp_ram:
            raise CloudAuthTemporaryError()
        return {
            "region": region,
            "security_group_id": security_group_id,
            "ram_role": ram_role or "FlightLinkEcsSecurityGroupRole",
            "status": "verified",
        }

    def list_rules(self, *, security_group_id: str) -> list[CloudSecurityGroupRule]:
        if self.fail_list_rules_temp_ram:
            raise CloudAuthTemporaryError()
        return [r for r in self.cloud_rules.values() if r.security_group_id == security_group_id]

    def authorize_rule(
        self,
        *,
        security_group_id: str,
        protocol: Literal["udp", "tcp"],
        port: int,
        source_cidr: str,
        description: str,
    ) -> CloudSecurityGroupRule:
        if self.fail_authorize_temp_ram:
            raise CloudAuthTemporaryError()
        key = (protocol, port)
        if key in self.fail_authorize_timeout_unapplied:
            self.fail_authorize_timeout_unapplied.discard(key)
            raise CloudTimeoutError(f"Timeout authorizing {protocol}/{port} (not applied)")

        # Idempotent if exact rule with same description already exists
        for existing in self.cloud_rules.values():
            if (
                existing.security_group_id == security_group_id
                and existing.protocol == protocol
                and existing.port == port
                and existing.source_cidr == source_cidr
                and existing.description == description
            ):
                if key in self.fail_authorize_timeout_applied:
                    self.fail_authorize_timeout_applied.discard(key)
                    raise CloudTimeoutError(f"Timeout authorizing {protocol}/{port} (already applied)")
                return existing

        self._rule_seq += 1
        rule = CloudSecurityGroupRule(
            rule_id=f"sgr-fl-{self._rule_seq}",
            security_group_id=security_group_id,
            protocol=protocol,
            port=port,
            source_cidr=source_cidr,
            description=description,
        )
        self.cloud_rules[rule.rule_id] = rule
        if key in self.fail_authorize_timeout_applied:
            self.fail_authorize_timeout_applied.discard(key)
            raise CloudTimeoutError(f"Timeout authorizing {protocol}/{port} (applied remotely)")
        return rule

    def revoke_rule(self, *, security_group_id: str, rule_id: str) -> None:
        if self.fail_revoke_temp_ram:
            raise CloudAuthTemporaryError()
        if rule_id in self.fail_revoke_timeout_unapplied or "*" in self.fail_revoke_timeout_unapplied:
            self.fail_revoke_timeout_unapplied.discard(rule_id)
            raise CloudTimeoutError(f"Timeout revoking {rule_id} (still present remotely)")
        if rule_id in self.fail_revoke_timeout_applied or "*" in self.fail_revoke_timeout_applied:
            self.fail_revoke_timeout_applied.discard(rule_id)
            self.cloud_rules.pop(rule_id, None)
            raise CloudTimeoutError(f"Timeout revoking {rule_id} (already removed remotely)")
        rule = self.cloud_rules.get(rule_id)
        if rule and rule.security_group_id == security_group_id:
            del self.cloud_rules[rule_id]
