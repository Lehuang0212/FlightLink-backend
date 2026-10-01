from __future__ import annotations

import sqlite3
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

from ..api.auth import AdminPublic, get_current_admin
from ..database import get_db
from ..schemas.channels import ChannelPublic, ChannelWrite, RouterRuntimePublic
from ..services.capture_files import (
    CaptureFileError,
    list_capture_files,
    resolve_closed_capture_file,
)
from ..services.capture_manager import (
    CaptureManagerError,
    get_capture_manager,
)
from ..services.channels import (
    ChannelConfigFailure,
    ChannelConflict,
    ChannelNotFound,
    get_channel,
    list_channels,
)
from ..services.channel_security_group import (
    ChannelSecurityGroupFailure,
    create_managed_channel,
    delete_managed_channel,
    update_managed_channel,
)
from ..services.channel_operations import channel_operation_lock
from ..services.router_manager import (
    RouterManagerError,
    get_router_manager,
    restart_router_service,
)
from ..services.telemetry import get_mavlink_telemetry_monitor

router = APIRouter(prefix="/channels", tags=["port channels"])


def _with_runtime(
    channel: ChannelPublic,
    *,
    error: str | None = None,
    capture_runtime: RouterRuntimePublic | None = None,
) -> ChannelPublic:
    runtime = get_router_manager().status(channel.id)
    if error:
        runtime = runtime.model_copy(update={"error": error})
    telemetry_monitor = get_mavlink_telemetry_monitor()
    try:
        telemetry_monitor.start(
            channel.id,
            channel.monitor_udp_port,
            channel.uav_udp_port,
            channel.ground_station_port,
        )
    except RuntimeError:
        pass
    telemetry = telemetry_monitor.snapshot(
        channel.id,
        ground_station_protocol=channel.ground_station_protocol,
        ground_station_port=channel.ground_station_port,
    )
    return channel.model_copy(
        update={
            "runtime": runtime,
            "capture_runtime": capture_runtime or get_capture_manager().status(channel.id),
            "telemetry": telemetry,
        }
    )


@router.get("", response_model=list[ChannelPublic])
def read_channels(
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> list[ChannelPublic]:
    channels: list[ChannelPublic] = []
    for listed_channel in list_channels(connection):
        with channel_operation_lock(listed_channel.id):
            try:
                current_channel = get_channel(connection, listed_channel.id)
            except ChannelNotFound:
                continue
            channels.append(_with_runtime(current_channel))
    return channels


@router.post("", response_model=ChannelPublic, status_code=status.HTTP_201_CREATED)
def add_channel(
    payload: ChannelWrite,
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> ChannelPublic:
    try:
        return create_managed_channel(connection, payload)
    except ChannelConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ChannelConfigFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Router config generation failed; channel changes were rolled back",
        ) from exc


@router.get("/{channel_id}", response_model=ChannelPublic)
def read_channel(
    channel_id: UUID,
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> ChannelPublic:
    with channel_operation_lock(channel_id):
        try:
            return _with_runtime(get_channel(connection, channel_id))
        except ChannelNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found") from exc


@router.put("/{channel_id}", response_model=ChannelPublic)
def replace_channel(
    channel_id: UUID,
    payload: ChannelWrite,
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> ChannelPublic:
    with channel_operation_lock(channel_id):
        try:
            return update_managed_channel(connection, channel_id, payload)
        except ChannelNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found") from exc
        except ChannelConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ChannelConfigFailure as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Router config generation failed; channel changes were rolled back",
            ) from exc
        except ChannelSecurityGroupFailure as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_channel(
    channel_id: UUID,
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> Response:
    with channel_operation_lock(channel_id):
        try:
            delete_managed_channel(connection, channel_id)
        except ChannelNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found") from exc
        except ChannelConfigFailure as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Router config removal failed; channel changes were rolled back",
            ) from exc
        except CaptureFileError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Packet capture config could not be removed: {exc}",
            ) from exc
        except RouterManagerError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Router service could not be stopped; channel was not deleted: {exc}",
            ) from exc
        except CaptureManagerError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Packet capture service could not be stopped; channel was not deleted: {exc}",
            ) from exc
        except ChannelSecurityGroupFailure as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{channel_id}/restart", response_model=ChannelPublic)
def restart_channel(
    channel_id: UUID,
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> ChannelPublic:
    with channel_operation_lock(channel_id):
        try:
            channel = get_channel(connection, channel_id)
        except ChannelNotFound as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found") from exc
        if not channel.enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Disabled channels cannot be restarted",
            )
        try:
            runtime = restart_router_service(channel_id)
        except RouterManagerError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Router service restart failed: {exc}",
            ) from exc
        return _with_runtime(channel.model_copy(update={"runtime": runtime}))


@router.get("/{channel_id}/logs")
def read_channel_logs(
    channel_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> dict[str, object]:
    try:
        get_channel(connection, channel_id)
    except ChannelNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found") from exc
    try:
        result = get_router_manager().logs(channel_id, limit)
    except RouterManagerError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Router logs are unavailable: {exc}",
        ) from exc
    return {"channel_id": str(channel_id), "lines": result.lines}


@router.get("/{channel_id}/messages")
def read_flight_messages(
    channel_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> dict[str, object]:
    try:
        get_channel(connection, channel_id)
    except ChannelNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found") from exc
    messages = get_mavlink_telemetry_monitor().messages(channel_id, limit)
    return {"channel_id": str(channel_id), "messages": messages}


@router.get("/{channel_id}/packets")
def read_packet_summaries(
    channel_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> dict[str, object]:
    try:
        get_channel(connection, channel_id)
    except ChannelNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        ) from exc
    packets = get_mavlink_telemetry_monitor().packets(channel_id, limit)
    return {"channel_id": str(channel_id), "packets": packets}


@router.get("/{channel_id}/captures")
def read_capture_files(
    channel_id: UUID,
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> dict[str, object]:
    try:
        get_channel(connection, channel_id)
    except ChannelNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        ) from exc
    active = get_capture_manager().status(channel_id).state == "active"
    try:
        captures = list_capture_files(channel_id, capture_active=active)
    except CaptureFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Packet capture files are unavailable: {exc}",
        ) from exc
    return {"channel_id": str(channel_id), "captures": captures}


@router.get("/{channel_id}/captures/{file_name}")
def download_capture_file(
    channel_id: UUID,
    file_name: str,
    _: AdminPublic = Depends(get_current_admin),
    connection: sqlite3.Connection = Depends(get_db),
) -> FileResponse:
    try:
        get_channel(connection, channel_id)
    except ChannelNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found"
        ) from exc
    active = get_capture_manager().status(channel_id).state == "active"
    try:
        path = resolve_closed_capture_file(channel_id, file_name, capture_active=active)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The newest capture segment is still being written",
        ) from exc
    except (FileNotFoundError, CaptureFileError) as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Capture file not found"
        ) from exc
    return FileResponse(
        path,
        media_type="application/vnd.tcpdump.pcap",
        filename=path.name,
    )
