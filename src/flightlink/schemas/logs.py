"""Live log schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

LogSource = Literal["router", "mavproxy"]
LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR"]


class LogEntry(BaseModel):
    event_id: str
    group_id: str
    source: LogSource
    level: LogLevel
    ts: str
    text: str


class LiveLogPageResponse(BaseModel):
    items: list[LogEntry]
    next_cursor: str | None = None
    has_more: bool = False
    observed_at: str
