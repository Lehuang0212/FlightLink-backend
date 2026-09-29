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
from .database import connect, get_db, initialize_database
from .services.channels import list_channels, synchronize_channel_configs
from .services.capture_files import CaptureFileError, write_capture_config
from .services.capture_manager import (
    CaptureRetentionWorker,
    reconcile_capture_service,
)
from .services.router_manager import reconcile_router_service
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
            try:
                write_capture_config(
                    channel.id,
                    uav_udp_port=channel.uav_udp_port,
                    ground_station_port=channel.ground_station_port,
                    ground_station_protocol=channel.ground_station_protocol,
                    monitor_udp_port=channel.monitor_udp_port,
                )
            except CaptureFileError as exc:
                logger.error(
                    "Unable to write packet capture config for channel %s: %s",
                    channel.id,
                    exc,
                )
            try:
                telemetry_monitor.start(
                    channel.id,
                    channel.monitor_udp_port,
                    channel.uav_udp_port,
                    channel.ground_station_port,
                )
            except RuntimeError as exc:
                logger.error(
                    "Unable to start MAVLink telemetry monitor for channel %s: %s",
                    channel.id,
                    exc,
                )
        if settings.router_manager_mode != "disabled":
            for channel in channels:
                runtime = reconcile_router_service(channel.id, enabled=channel.enabled)
                if runtime.error:
                    logger.error(
                        "Unable to reconcile mavlink-router channel %s: %s",
                        channel.id,
                        runtime.error,
                    )
                capture_runtime = reconcile_capture_service(
                    channel.id, enabled=channel.enabled
                )
                if capture_runtime.error:
                    logger.error(
                        "Unable to reconcile packet capture channel %s: %s",
                        channel.id,
                        capture_runtime.error,
                    )
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
