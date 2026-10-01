from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from ..config import settings
from ..schemas.channels import CaptureFilePublic

_CAPTURE_NAME = re.compile(
    r"^channel-([0-9a-f-]{36})-([0-9a-f]{8})-(\d{8}-\d{6})-(\d{10})\.pcap$"
)
logger = logging.getLogger(__name__)


class CaptureFileError(RuntimeError):
    """Raised when a managed packet-capture file cannot be handled safely."""


def capture_config_path(channel_id: UUID | str) -> Path:
    return settings.capture_config_dir / f"channel-{UUID(str(channel_id))}.json"


def write_capture_config(
    channel_id: UUID,
    *,
    uav_udp_port: int,
    ground_station_port: int,
    ground_station_protocol: str,
    monitor_udp_port: int,
) -> Path:
    path = capture_config_path(channel_id)
    config = {
        "channel_id": str(channel_id),
        "uav_udp_port": uav_udp_port,
        "ground_station_port": ground_station_port,
        "ground_station_protocol": ground_station_protocol,
        "monitor_udp_port": monitor_udp_port,
        "rotate_seconds": settings.pcap_rotate_seconds,
    }
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(config, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            temporary_path.chmod(0o640)
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise CaptureFileError("Unable to write the packet capture config") from exc
    return path


def remove_capture_config(channel_id: UUID) -> None:
    try:
        capture_config_path(channel_id).unlink(missing_ok=True)
    except OSError as exc:
        raise CaptureFileError("Unable to remove the packet capture config") from exc


def _parse_capture_name(path: Path) -> tuple[UUID, str, datetime] | None:
    match = _CAPTURE_NAME.fullmatch(path.name)
    if match is None:
        return None
    try:
        channel_id = UUID(match.group(1))
        started_at = datetime.fromtimestamp(int(match.group(4)), tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return channel_id, match.group(2), started_at


def _active_capture_session(channel_id: UUID) -> str | None:
    marker = settings.capture_dir / f"channel-{channel_id}.active"
    try:
        if marker.is_symlink() or not marker.is_file():
            return None
        stat = marker.stat(follow_symlinks=False)
        if time.time() - stat.st_mtime > 30:
            return None
        session_id = marker.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return session_id if re.fullmatch(r"[0-9a-f]{8}", session_id) else None


def list_capture_files(channel_id: UUID, *, capture_active: bool) -> list[CaptureFilePublic]:
    directory = settings.capture_dir
    try:
        candidates = [
            path
            for path in directory.iterdir()
            if path.name.startswith(f"channel-{channel_id}-")
            and path.suffix == ".pcap"
            and not path.is_symlink()
            and path.is_file()
        ]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CaptureFileError("Unable to list packet capture files") from exc

    parsed: list[tuple[Path, datetime, int]] = []
    for path in candidates:
        parsed_name = _parse_capture_name(path)
        if parsed_name is None or parsed_name[0] != channel_id:
            continue
        try:
            size = path.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        parsed.append((path, parsed_name[2], size))
    parsed.sort(key=lambda item: item[1], reverse=True)
    active_session = _active_capture_session(channel_id) if capture_active else None
    active_paths = [
        (path, parsed_name[2])
        for path in candidates
        if (parsed_name := _parse_capture_name(path)) is not None
        and parsed_name[0] == channel_id
        and parsed_name[1] == active_session
    ]
    newest_active_path = max(active_paths, key=lambda item: item[1])[0] if active_paths else None
    entries: list[CaptureFilePublic] = []
    for path, started_at, size in parsed:
        is_current = active_session is not None and path == newest_active_path
        entries.append(
            CaptureFilePublic(
                file_name=path.name,
                started_at=started_at.isoformat(),
                size_bytes=size,
                is_current=is_current,
            )
        )
    return entries


def resolve_closed_capture_file(
    channel_id: UUID,
    file_name: str,
    *,
    capture_active: bool,
) -> Path:
    if "/" in file_name or "\\" in file_name or file_name in {".", ".."}:
        raise FileNotFoundError(file_name)
    entries = list_capture_files(channel_id, capture_active=capture_active)
    entry = next((item for item in entries if item.file_name == file_name), None)
    if entry is None:
        raise FileNotFoundError(file_name)
    if entry.is_current:
        raise PermissionError("Capture file is still being written")
    parsed = _parse_capture_name(Path(file_name))
    if parsed is None or parsed[0] != channel_id:
        raise FileNotFoundError(file_name)
    path = settings.capture_dir / file_name
    try:
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(file_name)
    except OSError as exc:
        raise FileNotFoundError(file_name) from exc
    return path


def clean_capture_files() -> tuple[int, int]:
    """Prune expired/over-limit closed files; return deleted file and byte counts."""
    directory = settings.capture_dir
    try:
        paths = list(directory.iterdir())
    except FileNotFoundError:
        return 0, 0
    except OSError as exc:
        raise CaptureFileError("Unable to inspect packet capture storage") from exc

    files: list[tuple[Path, UUID, str, datetime, int, float]] = []
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        parsed = _parse_capture_name(path)
        if parsed is None:
            continue
        try:
            stat = path.stat(follow_symlinks=False)
        except OSError:
            continue
        files.append((path, parsed[0], parsed[1], parsed[2], stat.st_size, stat.st_mtime))

    now = time.time()
    protected: set[Path] = set()
    active_sessions: dict[UUID, str] = {}
    for channel_id in {item[1] for item in files}:
        active_session = _active_capture_session(channel_id)
        if active_session is not None:
            active_sessions[channel_id] = active_session
    newest_by_session: dict[tuple[UUID, str], tuple[Path, datetime]] = {}
    for path, channel_id, session_id, started_at, _, _ in files:
        key = (channel_id, session_id)
        current = newest_by_session.get(key)
        if current is None or started_at > current[1]:
            newest_by_session[key] = (path, started_at)
    for channel_id, session_id in active_sessions.items():
        newest = newest_by_session.get((channel_id, session_id))
        if newest is not None:
            protected.add(newest[0])

    deleted_count = 0
    deleted_bytes = 0

    def remove(item: tuple[Path, UUID, str, datetime, int, float]) -> bool:
        nonlocal deleted_count, deleted_bytes
        path, _, _, _, size, _ = item
        if path in protected:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError as exc:
            logger.warning("Unable to remove expired packet capture file %s: %s", path, exc)
            return False
        deleted_count += 1
        deleted_bytes += size
        return True

    expires_before = now - settings.pcap_retention_seconds
    for item in files:
        if item[5] < expires_before:
            remove(item)

    remaining = [item for item in files if item[0].exists()]
    total_bytes = sum(item[4] for item in remaining)
    if total_bytes > settings.pcap_max_bytes:
        for item in sorted(remaining, key=lambda entry: entry[3]):
            if total_bytes <= settings.pcap_max_bytes:
                break
            if remove(item):
                total_bytes -= item[4]
    return deleted_count, deleted_bytes
