"""Async operation & execution step schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

OperationAction = Literal[
    "create",
    "update",
    "delete",
    "start",
    "stop",
    "restart",
    "sync_security_group",
    "verify",
]

OperationStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "needs_verification",
    "cancelled",
]

OperationStepStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "needs_verification",
    "skipped",
]


class OperationStep(BaseModel):
    step_name: str
    status: OperationStepStatus
    started_at: str | None = None
    finished_at: str | None = None
    detail: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class OperationDetail(BaseModel):
    operation_id: str
    group_id: str
    action: OperationAction
    status: OperationStatus
    generation: int
    idempotency_key: str | None = None
    requested_by: str
    retryable: bool = False
    retry_hint: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    steps: list[OperationStep] = Field(default_factory=list)
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str


class OperationAcceptedResponse(BaseModel):
    operation: OperationDetail


class OperationListResponse(BaseModel):
    items: list[OperationDetail]
    next_cursor: str | None = None
    has_more: bool = False
