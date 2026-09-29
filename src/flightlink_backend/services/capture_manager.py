from __future__ import annotations

import subprocess
from typing import Protocol
from uuid import UUID

from ..config import settings
from ..schemas.channels import RouterRuntimePublic


class CaptureManagerError(RuntimeError):
    """Raised when a per-channel packet capture service cannot be controlled."""


class CaptureManager(Protocol):
    def start(self, channel_id: UUID) -> None: ...

    def stop(self, channel_id: UUID) -> None: ...

    def restart(self, channel_id: UUID) -> None: ...

    def status(self, channel_id: UUID) -> RouterRuntimePublic: ...


class SystemdCaptureManager:
    def _invoke(self, action: str, channel_id: UUID) -> subprocess.CompletedProcess[str]:
        command = [
            str(settings.sudo_path),
            "-n",
            str(settings.router_control_helper),
            f"capture-{action}",
            str(channel_id),
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
            raise CaptureManagerError(
                "Capture service control helper is unavailable or timed out"
            ) from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")
            if len(detail) > 400:
                detail = detail[:400]
            raise CaptureManagerError(detail or f"Capture service action '{action}' failed")
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
        except CaptureManagerError as exc:
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
        try:
            pid = int(properties.get("MainPID", "0")) or None
        except ValueError:
            pid = None
        return RouterRuntimePublic(
            state=state_map.get(active_state, "unknown"),
            substate=properties.get("SubState") or None,
            pid=pid,
            active_since=properties.get("ActiveEnterTimestamp") or None,
            result=properties.get("Result") or None,
        )


class DisabledCaptureManager:
    def start(self, channel_id: UUID) -> None:
        return None

    def stop(self, channel_id: UUID) -> None:
        return None

    def restart(self, channel_id: UUID) -> None:
        return None

    def status(self, channel_id: UUID) -> RouterRuntimePublic:
        return RouterRuntimePublic(state="unknown", error="Capture process management is disabled")


_manager: CaptureManager | None = None


def get_capture_manager() -> CaptureManager:
    global _manager
    if _manager is None:
        if settings.router_manager_mode == "disabled":
            _manager = DisabledCaptureManager()
        else:
            _manager = SystemdCaptureManager()
    return _manager


def reconcile_capture_service(
    channel_id: UUID,
    *,
    enabled: bool,
    restart_if_active: bool = False,
) -> RouterRuntimePublic:
    manager = get_capture_manager()
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
    except CaptureManagerError as exc:
        return manager.status(channel_id).model_copy(update={"error": str(exc)})
    return manager.status(channel_id)


def stop_capture_service(channel_id: UUID) -> None:
    if settings.router_manager_mode != "disabled":
        get_capture_manager().stop(channel_id)


class CaptureRetentionWorker:
    def __init__(self, interval_seconds: int) -> None:
        import threading

        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="flightlink-pcap-retention", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        import logging

        from .capture_files import clean_capture_files

        logger = logging.getLogger(__name__)
        while not self._stop_event.is_set():
            try:
                count, size = clean_capture_files()
                if count:
                    logger.info("Pruned %s PCAP files (%s bytes)", count, size)
            except Exception:
                logger.exception("Unable to prune packet capture files")
            self._stop_event.wait(self.interval_seconds)


def set_capture_manager_for_process(manager: CaptureManager | None) -> None:
    global _manager
    _manager = manager
