"""UTC clock abstraction for deterministic time-based testing (retention, timeouts)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_rfc3339(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_rfc3339(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime: ...

    def now_iso(self) -> str: ...


class SystemClock:
    def now(self) -> datetime:
        return utc_now()

    def now_iso(self) -> str:
        return format_rfc3339(self.now()) or ""


class ManualClock:
    """Controllable clock for deterministic timeout and 7-day retention tests."""

    def __init__(self, initial: datetime | None = None) -> None:
        self._current = initial or datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
        if self._current.tzinfo is None:
            self._current = self._current.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current

    def now_iso(self) -> str:
        return format_rfc3339(self._current) or ""

    def set(self, dt: datetime) -> datetime:
        self._current = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return self._current

    def advance(self, **kwargs: float) -> datetime:
        self._current = self._current + timedelta(**kwargs)
        return self._current
