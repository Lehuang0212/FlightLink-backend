from __future__ import annotations

from dataclasses import dataclass
import subprocess
from typing import Protocol
from uuid import UUID

from ..config import settings
from ..schemas.channels import RouterRuntimePublic


class RouterManagerError(RuntimeError):
    """Raised when a router service cannot be controlled or inspected."""


@dataclass(frozen=True, slots=True)
class RouterLog:
    lines: list[str]


class RouterManager(Protocol):
    def start(self, channel_id: UUID) -> None: ...

    def stop(self, channel_id: UUID) -> None: ...

    def restart(self, channel_id: UUID) -> None: ...

    def status(self, channel_id: UUID) -> RouterRuntimePublic: ...

    def logs(self, channel_id: UUID, limit: int) -> RouterLog: ...


class SystemdRouterManager:
    def _invoke(
        self,
        action: str,
        channel_id: UUID,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            str(settings.sudo_path),
            "-n",
            str(settings.router_control_helper),
            action,
            str(channel_id),
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.router_command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RouterManagerError("Router service control helper is unavailable or timed out") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")
            if len(detail) > 400:
                detail = detail[:400]
            raise RouterManagerError(detail or f"Router service action '{action}' failed")
        return result

    def start(self, channel_id: UUID) -> None:
        self._invoke("start", channel_id)

    def stop(self, channel_id: UUID) -> None:
        self._invoke("stop", channel_id)

    def restart(self, channel_id: UUID) -> None:
        self._invoke("restart", channel_id)

    def status(self, channel_id: UUID) -> RouterRuntimePublic:
        try:
            result = self._invoke("status", channel_id)
        except RouterManagerError as exc:
            return RouterRuntimePublic(state="unknown", error=str(exc))

        properties: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value

        active_state = properties.get("ActiveState", "").lower()
        state_map = {
            "active": "active",
            "inactive": "inactive",
            "failed": "failed",
            "activating": "starting",
            "deactivating": "stopping",
        }
        state = state_map.get(active_state, "unknown")
        raw_pid = properties.get("MainPID", "0")
        try:
            pid_value = int(raw_pid)
        except ValueError:
            pid_value = 0
        result_value = properties.get("Result")
        return RouterRuntimePublic(
            state=state,
            substate=properties.get("SubState") or None,
            pid=pid_value or None,
            active_since=properties.get("ActiveEnterTimestamp") or None,
            result=result_value or None,
        )

    def logs(self, channel_id: UUID, limit: int) -> RouterLog:
        result = self._invoke("logs", channel_id, str(limit))
        return RouterLog(lines=result.stdout.splitlines())


class DisabledRouterManager:
    def start(self, channel_id: UUID) -> None:
        return None

    def stop(self, channel_id: UUID) -> None:
        return None

    def restart(self, channel_id: UUID) -> None:
        return None

    def status(self, channel_id: UUID) -> RouterRuntimePublic:
        return RouterRuntimePublic(state="unknown", error="Router process management is disabled")

    def logs(self, channel_id: UUID, limit: int) -> RouterLog:
        raise RouterManagerError("Router process management is disabled")


_manager: RouterManager | None = None


def get_router_manager() -> RouterManager:
    global _manager
    if _manager is None:
        if settings.router_manager_mode == "disabled":
            _manager = DisabledRouterManager()
        else:
            _manager = SystemdRouterManager()
    return _manager


def reconcile_router_service(
    channel_id: UUID,
    *,
    enabled: bool,
    restart_if_active: bool = False,
) -> RouterRuntimePublic:
    manager = get_router_manager()
    if settings.router_manager_mode == "disabled":
        return manager.status(channel_id)

    current = manager.status(channel_id)
    try:
        if enabled:
            if current.state == "active":
                if restart_if_active:
                    manager.restart(channel_id)
            elif current.state != "starting":
                manager.start(channel_id)
        elif current.state in {"active", "starting", "stopping"}:
            manager.stop(channel_id)
    except RouterManagerError as exc:
        return manager.status(channel_id).model_copy(update={"error": str(exc)})
    return manager.status(channel_id)


def restart_router_service(channel_id: UUID) -> RouterRuntimePublic:
    if settings.router_manager_mode == "disabled":
        raise RouterManagerError("Router process management is disabled")
    manager = get_router_manager()
    manager.restart(channel_id)
    return manager.status(channel_id)


def stop_router_service(channel_id: UUID) -> None:
    if settings.router_manager_mode == "disabled":
        return
    get_router_manager().stop(channel_id)


def set_router_manager_for_process(manager: RouterManager | None) -> None:
    """Replace the manager instance, primarily for application-level integration."""
    global _manager
    _manager = manager
