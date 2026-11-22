from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import sqlite3

from fastapi import Depends, FastAPI

from .api.auth import router as auth_router
from .api.channels import router as channels_router
from .api.integrations import router as integrations_router
from .config import settings
from .services.channel_runtime import reconcile_channel_runtime
from .database import connect, get_db, initialize_database
from .services.channels import list_channels, synchronize_channel_configs
from .services.capture_manager import CaptureRetentionWorker
from .services.telemetry import get_mavlink_telemetry_monitor

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    initialize_database()
    telemetry_monitor = get_mavlink_telemetry_monitor()
    retention_worker: CaptureRetentionWorker | None = None
    connection = connect()
    try:
        synchronize_channel_configs(connection)
        channels = list_channels(connection)
        for channel in channels:
            current = reconcile_channel_runtime(channel)
            if settings.router_manager_mode != 'disabled':
                for name, service in [('router', current.runtime), ('capture', current.capture_runtime)]:
                    if service.error:
                        logger.error('Channel %s: %s recovery: %s', channel.id, name, service.error)
        retention_worker = CaptureRetentionWorker(settings.pcap_cleanup_interval_seconds)
        retention_worker.start()
    finally:
        connection.close()
    try:
        yield
    finally:
        if retention_worker is not None:
            retention_worker.stop()
        telemetry_monitor.stop_all()


app = FastAPI(
    title="FlightLink-Console API",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(auth_router, prefix="/api/v1")
app.include_router(channels_router, prefix="/api/v1")
app.include_router(integrations_router, prefix="/api/v1")


@app.get("/api/v1/health", tags=["system"])
def health(connection: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
    connection.execute("SELECT 1").fetchone()
    return {"status": "ok", "database": "ok"}
