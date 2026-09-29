from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .security_group import SecurityGroupChannelPublic

GroundStationProtocol = Literal["tcp", "udp"]
RouterState = Literal["active", "inactive", "failed", "starting", "stopping", "unknown"]


class RouterRuntimePublic(BaseModel):
    state: RouterState = "unknown"
    substate: str | None = None
    pid: int | None = None
    active_since: str | None = None
    result: str | None = None
    error: str | None = None


class RadioStatusPublic(BaseModel):
    rssi: int | None = None
    remote_rssi: int | None = None
    noise: int | None = None
    remote_noise: int | None = None
    tx_buffer_percent: int | None = None
    receive_errors: int | None = None


class FlightMessagePublic(BaseModel):
    received_at: str
    severity: int | None = None
    severity_name: str
    text: str


class PacketSummaryPublic(BaseModel):
    captured_at: str
    protocol: str | None = None
    source: str | None = None
    destination: str | None = None
    length_bytes: int | None = None
    summary: str


class CaptureFilePublic(BaseModel):
    file_name: str
    started_at: str | None = None
    size_bytes: int
    is_current: bool = False


class ChannelTelemetryPublic(BaseModel):
    monitor_state: Literal["listening", "unavailable"] = "unavailable"
    monitor_error: str | None = None
    uav_online: bool = False
    uav_peer: str | None = None
    system_id: int | None = None
    component_id: int | None = None
    vehicle_type: str | None = None
    autopilot: str | None = None
    flight_mode: str | None = None
    armed: bool | None = None
    battery_remaining_percent: int | None = None
    battery_voltage_v: float | None = None
    gps_fix_type: int | None = None
    gps_satellites: int | None = None
    altitude_m: float | None = None
    relative_altitude_m: float | None = None
    ground_speed_m_s: float | None = None
    link_quality_percent: float | None = None
    packets_received_window: int = 0
    packets_lost_window: int = 0
    last_packet_at: str | None = None
    last_packet_age_seconds: float | None = None
    uav_rx_bytes_per_second: float = 0
    gcs_tx_bytes_per_second: float = 0
    ground_station_connected: bool = False
    ground_station_peer: str | None = None
    ground_station_last_packet_at: str | None = None
    radio_status: RadioStatusPublic | None = None


class ChannelWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uav_udp_port: int = Field(ge=1, le=65535)
    ground_station_port: int = Field(ge=1, le=65535)
    ground_station_protocol: GroundStationProtocol = "tcp"
    enabled: bool = True


class ChannelPublic(BaseModel):
    id: UUID
    uav_udp_port: int
    ground_station_port: int
    monitor_udp_port: int
    ground_station_protocol: GroundStationProtocol
    enabled: bool
    config_file_name: str
    created_at: datetime
    updated_at: datetime
    security_group: SecurityGroupChannelPublic = Field(
        default_factory=SecurityGroupChannelPublic
    )
    runtime: RouterRuntimePublic = Field(default_factory=RouterRuntimePublic)
    capture_runtime: RouterRuntimePublic = Field(default_factory=RouterRuntimePublic)
    telemetry: ChannelTelemetryPublic = Field(default_factory=ChannelTelemetryPublic)
