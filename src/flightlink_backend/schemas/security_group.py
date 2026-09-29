from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SecurityGroupChannelPublic(BaseModel):
    state: Literal["disabled", "pending", "synced", "cleanup_pending", "error"] = (
        "disabled"
    )
    error: str | None = None


class SecurityGroupRulePublic(BaseModel):
    rule_id: str
    protocol: str
    port_range: str
    source_cidr: str | None
    policy: str
    description: str | None
    direction: str


class SecurityGroupPreflightPublic(BaseModel):
    state: Literal["disabled", "not_configured", "ready", "error"]
    read_access_verified: bool = False
    write_access_checked: bool = False
    region_id: str | None = None
    security_group_id: str | None = None
    security_group_name: str | None = None
    missing_configuration: list[str] = Field(default_factory=list)
    ingress_rules: list[SecurityGroupRulePublic] = Field(default_factory=list)
    error: str | None = None
