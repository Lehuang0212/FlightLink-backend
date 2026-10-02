from __future__ import annotations

import time
import threading
from collections.abc import Callable
from typing import TypeVar
from uuid import UUID

from .channel_operations import channel_operation_lock

T = TypeVar('T')


class ServiceReadCache:
    """Short-lived process-local reads, serialized with channel lifecycle operations."""

    def __init__(self) -> None:
        self._values: dict[tuple[UUID, str], tuple[float, object]] = {}
        self._lock = threading.Lock()

    def read(self, channel_id: UUID, key: str, ttl: float, loader: Callable[[], T]) -> T:
        with channel_operation_lock(channel_id):
            now = time.monotonic()
            with self._lock:
                # Keep even unused/deleted-channel entries bounded by their short expiry.
                for expired in [item for item, entry in self._values.items() if now >= entry[0]]:
                    del self._values[expired]
                cached = self._values.get((channel_id, key))
            if cached is not None and now < cached[0]:
                return cached[1]  # type: ignore[return-value]
            value = loader()
            with self._lock:
                self._values[(channel_id, key)] = (time.monotonic() + ttl, value)
            return value

    def invalidate(self, channel_id: UUID) -> None:
        with channel_operation_lock(channel_id):
            with self._lock:
                for key in [key for key in self._values if key[0] == channel_id]:
                    del self._values[key]
