"""Overview, port group, service status, 12-item nullable telemetry, and write request schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

GcsProtocol = Literal["tcp", "udp"]
DesiredState = Literal["stopped", "running", "deleting"]
GroupLifecycleState = Literal[
    "configured_stopped",
    "starting",
    "running",
    "stopping",
    "updating",
    "deleting",
    "cleanup_pending",
    "needs_verification",
    "error",
]
GroupActionType = Literal["start", "stop", "restart", "sync-security-group", "verify"]


class ComponentHealth(BaseModel):
    name: Literal["flightlink-api", "flightlink-agent", "flightlink-archive"]
    status: Literal["ok", "degraded", "stopped", "unknown"]
    observed_at: str | None = None
    stale: bool = False
    detail: str | None = None


class DiskQuotaStatus(BaseModel):
    archive_dir: str
    status: Literal["ok", "warning", "degraded_quota_exceeded"]
    total_bytes: int | None = None
    free_bytes: int | None = None
    archive_used_bytes: int | None = None
    observed_at: str


class OverviewMetrics(BaseModel):
    group_count: int = Field(..., ge=0, le=5)
    max_groups: Literal[5] = 5
    running_router_count: int = 0
    online_fc_count: int = 0
    needs_verification_count: int = 0


class RouterServiceStatus(BaseModel):
    state: Literal["stopped", "starting", "running", "error", "needs_verification"]
    unit_name: str | None = None
    pid: int | None = None
    uptime_seconds: int | None = None
    bound_endpoints: list[str] = Field(default_factory=list)
    verified_listening: bool = False
    observed_at: str | None = None
    stale: bool = False
    detail: str | None = None


class MavproxyServiceStatus(BaseModel):
    state: Literal["disconnected", "waiting", "connected", "error"]
    unit_name: str | None = None
    pid: int | None = None
    uptime_seconds: int | None = None
    observed_at: str | None = None
    stale: bool = False
    detail: str | None = None


class SecurityGroupRuleView(BaseModel):
    rule_id: str | None = None
    role: Literal["dtu", "gcs"]
    protocol: Literal["udp", "tcp"]
    port: int
    source_cidr: str
    status: Literal["present", "pending_create", "pending_delete", "drift", "unknown"]


class SecurityGroupServiceStatus(BaseModel):
    state: Literal["synced", "syncing", "drift", "needs_verification", "unconfigured", "error"]
    security_group_id: str | None = None
    owned_rule_count: int = 0
    rules: list[SecurityGroupRuleView] = Field(default_factory=list)
    observed_at: str | None = None
    stale: bool = False
    detail: str | None = None


class GroupSummary(BaseModel):
    group_id: str
    name: str
    dtu_protocol: Literal["udp"] = "udp"
    dtu_port: int = Field(..., ge=1, le=65535)
    gcs_protocol: GcsProtocol
    gcs_port: int = Field(..., ge=1, le=65535)
    config_version: int = Field(..., ge=1)
    candidate_version: int | None = None
    desired_state: DesiredState
    lifecycle_state: GroupLifecycleState
    operation_generation: int = Field(..., ge=1)
    active_operation_id: str | None = None
    verification_reason: str | None = None
    sysid: int | None = None
    fc_state: Literal["offline", "online"] = "offline"
    dtu_peer: str | None = None
    gcs_state: Literal["disconnected", "idle", "connected"] = "disconnected"
    gcs_peer: str | None = None
    link_quality_pct: int | None = Field(default=None, ge=0, le=100)
    rtt_ms: int | None = Field(default=None, ge=0)
    last_activity_at: str | None = None
    router: RouterServiceStatus
    mavproxy: MavproxyServiceStatus
    security_group: SecurityGroupServiceStatus
    observed_at: str
    stale: bool = False
    created_at: str
    updated_at: str


class TelemetrySnapshot(BaseModel):
    """12 telemetry metrics; strictly null when no reliable real source exists."""

    flight_mode: str | None = None
    armed: bool | None = None
    battery_remaining_pct: float | None = None
    battery_voltage_v: float | None = None
    gps_satellites: int | None = None
    gps_fix_type: str | None = None
    gps_hdop: float | None = None
    altitude_msl_m: float | None = None
    relative_altitude_m: float | None = None
    ground_speed_mps: float | None = None
    rx_rate_kbps: float | None = None
    tx_rate_kbps: float | None = None
    packets_lost: int | None = None
    packets_received: int | None = None
    packet_loss_pct: float | None = None
    observed_at: str | None = None
    stale: bool = False


class ObservedGcsCommand(BaseModel):
    id: str
    observed_at: str
    command: str
    detail: str | None = None
    status: Literal["ACCEPTED", "DENIED", "UNSUPPORTED", "FAILED", "IN_PROGRESS", "UNKNOWN"]
    source_sysid: int | None = None
    source_compid: int | None = None
    source_label: str | None = None


class GroupDetail(GroupSummary):
    telemetry: TelemetrySnapshot
    recent_gcs_commands: list[ObservedGcsCommand] = Field(default_factory=list)


class GroupListResponse(BaseModel):
    items: list[GroupSummary]
    total: int
    max_groups: Literal[5] = 5
    observed_at: str


class OverviewResponse(BaseModel):
    ecs_instance_id: str
    observed_at: str
    stale: bool
    stale_after_seconds: int
    components: list[ComponentHealth]
    metrics: OverviewMetrics
    disk: DiskQuotaStatus
    groups: list[GroupSummary]


class CreateGroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=24)
    dtu_port: int = Field(..., ge=1, le=65535)
    gcs_protocol: GcsProtocol
    gcs_port: int = Field(..., ge=1, le=65535)

    @model_validator(mode="after")
    def _validate_ports_and_name(self) -> CreateGroupRequest:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name must not be empty")
        if len(self.name) > 24:
            raise ValueError("name must be at most 24 characters")
        if self.dtu_port == self.gcs_port:
            raise ValueError("gcs_port must differ from dtu_port")
        return self


class UpdateGroupRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=24)
    dtu_port: int | None = Field(default=None, ge=1, le=65535)
    gcs_protocol: GcsProtocol | None = None
    gcs_port: int | None = Field(default=None, ge=1, le=65535)
    expected_config_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _normalize(self) -> UpdateGroupRequest:
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name must not be empty")
        return self


class GroupUpdateDryRunResponse(BaseModel):
    group_id: str
    valid: bool
    requires_restart: bool
    requires_sg_sync: bool
    planned_steps: list[str]
    diff: dict[str, Any]


class GroupActionRequest(BaseModel):
    expected_config_version: int | None = Field(default=None, ge=1)
