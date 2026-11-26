"""Async operations inspection and safe retry router."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status

from flightlink.api.deps import AppContainer, get_container, get_current_user, require_admin
from flightlink.schemas.auth import UserProfile
from flightlink.schemas.operations import (
    OperationAcceptedResponse,
    OperationDetail,
    OperationListResponse,
    OperationStatus,
)

router = APIRouter(prefix="/operations", tags=["Operations"])


@router.get(
    "",
    operation_id="listOperations",
    response_model=OperationListResponse,
    summary="查询最近异步操作记录",
)
def list_operations(
    group_id: str | None = Query(default=None),
    status_filter: OperationStatus | None = Query(default=None, alias="status"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> OperationListResponse:
    return container.repo.list_operations(
        group_id=group_id,
        status=status_filter,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/{operation_id}",
    operation_id="getOperation",
    response_model=OperationDetail,
    summary="查询单个异步操作状态与步骤明细",
)
def get_operation(
    operation_id: str = Path(...),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> OperationDetail:
    return container.repo.get_operation(operation_id)


@router.post(
    "/{operation_id}/retry",
    operation_id="retryOperation",
    response_model=OperationAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="重试可重试的失败或待核实操作",
)
def retry_operation(
    response: Response,
    operation_id: str = Path(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128),
    admin: UserProfile = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OperationAcceptedResponse:
    op = container.repo.enqueue_retry_operation(
        operation_id,
        requested_by=admin.username,
        idempotency_key=idempotency_key,
    )
    response.headers["Location"] = f"/api/v1/operations/{op.operation_id}"
    return OperationAcceptedResponse(operation=op)
