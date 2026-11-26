"""Archive browsing, incremental sync manifest, and artifact descriptor schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from flightlink.schemas.groups import GcsProtocol
from flightlink.schemas.logs import LogEntry

ArchiveSourceKind = Literal["router", "mavproxy", "metadata"]
ArchiveSourceAvailability = Literal[
    "available",
    "missing_on_server",
    "empty",
    "degraded_quota_exceeded",
    "in_progress",
]
SessionCloseReason = Literal[
    "fc_heartbeat_timeout",
    "admin_group_deleted",
    "admin_group_stopped",
    "port_reconfigured",
    "service_shutdown",
    "recovery_closed",
]


class ArchiveGap(BaseModel):
    source: Literal["router", "mavproxy", "all"]
    started_at: str
    ended_at: str | None = None
    reason: Literal[
        "observer_disconnected",
        "archive_restarted",
        "disk_quota_exceeded",
        "journal_gap",
    ]
    detail: str | None = None


class ArchiveArtifactDescriptor(BaseModel):
    source: ArchiveSourceKind
    availability: ArchiveSourceAvailability
    file_id: str | None = None
    logical_name: str
    content_type: str
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    line_count: int | None = Field(default=None, ge=0)
    missing_reason: str | None = None
    read_url: str | None = None


class ArchiveSessionSummary(BaseModel):
    ecs_instance_id: str
    session_id: str
    group_id: str
    group_name_snapshot: str
    dtu_port_snapshot: int
    gcs_protocol_snapshot: GcsProtocol
    gcs_port_snapshot: int
    group_deleted: bool = False
    status: Literal["active", "sealing", "sealed"]
    sysid: int | None = None
    first_observed_at: str
    last_heartbeat_at: str
    ended_at: str | None = None
    sealed_at: str | None = None
    expires_at: str | None = None
    close_reason: SessionCloseReason | None = None
    has_gaps: bool = False
    has_missing_sources: bool = False
    files: list[ArchiveArtifactDescriptor] = Field(default_factory=list)


class ArchiveSessionDetail(ArchiveSessionSummary):
    gaps: list[ArchiveGap] = Field(default_factory=list)


class ArchiveGroupSummary(BaseModel):
    group_id: str
    name_snapshot: str
    dtu_protocol: Literal["udp"] = "udp"
    dtu_port_snapshot: int
    gcs_protocol_snapshot: GcsProtocol
    gcs_port_snapshot: int
    deleted: bool = False
    deleted_at: str | None = None
    active_session_count: int = 0
    sealed_session_count: int = 0
    latest_session: ArchiveSessionSummary | None = None


class ArchiveGroupListResponse(BaseModel):
    ecs_instance_id: str
    items: list[ArchiveGroupSummary]
    observed_at: str


class ArchiveSessionListResponse(BaseModel):
    group_id: str
    items: list[ArchiveSessionSummary]
    next_cursor: str | None = None
    has_more: bool = False


class ArchiveEntryPageResponse(BaseModel):
    session_id: str
    source: Literal["router", "mavproxy"]
    source_availability: ArchiveSourceAvailability
    missing_reason: str | None = None
    items: list[LogEntry]
    gaps: list[ArchiveGap] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class ArchiveChangeItem(BaseModel):
    change_seq: int = Field(..., ge=1)
    ecs_instance_id: str
    group_id: str
    group_name_snapshot: str
    dtu_port_snapshot: int
    gcs_protocol_snapshot: GcsProtocol
    gcs_port_snapshot: int
    group_deleted: bool
    session_id: str
    sysid: int | None = None
    first_observed_at: str
    last_heartbeat_at: str
    ended_at: str
    sealed_at: str
    expires_at: str
    close_reason: SessionCloseReason
    files: list[ArchiveArtifactDescriptor]
    gaps: list[ArchiveGap] = Field(default_factory=list)


class ArchiveChangesResponse(BaseModel):
    ecs_instance_id: str
    retention_seconds: int
    oldest_available_sealed_at: str | None = None
    items: list[ArchiveChangeItem]
    next_cursor: str | None = None
    has_more: bool = False
    observed_at: str
