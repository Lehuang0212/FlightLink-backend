from __future__ import annotations

import threading
from uuid import UUID


_registry_lock = threading.Lock()
_channel_locks: dict[str, threading.RLock] = {}


def channel_operation_lock(channel_id: UUID | str) -> threading.RLock:
    """Return the process-local lock coordinating one channel's lifecycle."""
    key = str(channel_id)
    with _registry_lock:
        lock = _channel_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _channel_locks[key] = lock
        return lock
