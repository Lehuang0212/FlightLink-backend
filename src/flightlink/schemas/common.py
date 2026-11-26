"""Common API schemas (errors, health)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: list[ErrorDetail] = Field(default_factory=list)
    request_id: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    ecs_instance_id: str
    observed_at: str
    db_ok: bool
