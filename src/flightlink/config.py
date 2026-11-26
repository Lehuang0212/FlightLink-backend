"""FlightLink runtime settings.

Explicitly enforces safe defaults:
- Management API binds to 127.0.0.1 by default (remote access must sit behind HTTPS).
- Business DTU/GCS allowed CIDRs default to empty (never implicitly opens 0.0.0.0/0
  unless explicitly configured).
- No demo accounts are ever seeded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLIGHTLINK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path("/var/lib/flightlink"))
    db_path: Path | None = None
    archive_dir: Path | None = None
    router_config_dir: Path | None = None
    runtime_dir: Path | None = None

    ecs_instance_id: str | None = None

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=list)

    jwt_secret: str = Field(default="change-me-in-production-via-flightlink-init")
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 7 * 24 * 3600

    max_groups: int = 5
    stale_after_seconds: int = 30
    fc_heartbeat_grace_seconds: int = 15
    archive_retention_seconds: int = 7 * 24 * 3600
    live_log_buffer_max_rows: int = 20000

    disk_warn_free_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GiB
    disk_min_free_bytes: int = 512 * 1024 * 1024  # 512 MiB
    archive_max_bytes: int = 20 * 1024 * 1024 * 1024  # 20 GiB

    # Business security group source CIDRs: NEVER open 0.0.0.0/0 by default.
    # Operators must explicitly set e.g. ["0.0.0.0/0"] or specific CIDRs.
    dtu_allowed_cidrs: list[str] = Field(default_factory=list)
    gcs_allowed_cidrs: list[str] = Field(default_factory=list)

    aliyun_region: str | None = None
    aliyun_security_group_id: str | None = None
    aliyun_ram_role_name: str | None = None

    adapter_mode: Literal["linux", "fake"] = "linux"

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "control.sqlite3")

    @property
    def resolved_archive_dir(self) -> Path:
        return self.archive_dir or (self.data_dir / "archive")

    @property
    def resolved_router_config_dir(self) -> Path:
        return self.router_config_dir or (self.data_dir / "router.d")

    @property
    def resolved_runtime_dir(self) -> Path:
        return self.runtime_dir or (self.data_dir / "run")
