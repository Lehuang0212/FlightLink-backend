"""Daemon entrypoint for `flightlink-archive`."""

from __future__ import annotations

import signal
import time

from flightlink.archive.service import ArchiveService
from flightlink.config import Settings
from flightlink.db import Database


def main() -> None:
    settings = Settings()
    db = Database(settings.resolved_db_path)
    db.init_schema(ecs_instance_id=settings.ecs_instance_id)
    archive = ArchiveService(db, settings)
    archive.recover_sealing_sessions()

    stop = False

    def _handle(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    while not stop:
        db.record_component_heartbeat("flightlink-archive", "ok", "Archive collector running")
        archive.check_heartbeat_timeouts()
        archive.purge_expired_sessions()
        time.sleep(1.0)


if __name__ == "__main__":
    main()
