"""Port groups query and 202-Accepted async write routers."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status
from fastapi.responses import JSONResponse

from flightlink.api.deps import AppContainer, get_container, get_current_user, require_admin
from flightlink.domain.operations_repo import parse_if_match_version
from flightlink.schemas.auth import UserProfile
from flightlink.schemas.groups import (
    CreateGroupRequest,
    GroupActionRequest,
    GroupActionType,
    GroupDetail,
    GroupListResponse,
    GroupUpdateDryRunResponse,
    UpdateGroupRequest,
)
from flightlink.schemas.operations import OperationAcceptedResponse

router = APIRouter(prefix="/groups", tags=["Groups"])


@router.get(
    "",
    operation_id="listGroups",
    response_model=GroupListResponse,
    summary="列出当前未完成删除的端口组",
)
def list_groups(
    q: str | None = Query(default=None, max_length=160),
    filter: Literal["all", "online", "idle", "attention"] = Query(default="all"),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> GroupListResponse:
    return container.repo.list_groups(q=q, filter_mode=filter)


@router.post(
    "",
    operation_id="createGroup",
    response_model=OperationAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="创建端口组（异步受理，落态已配置未启动）",
)
def create_group(
    body: CreateGroupRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128),
    admin: UserProfile = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OperationAcceptedResponse:
    op = container.repo.enqueue_create_group(
        body,
        requested_by=admin.username,
        idempotency_key=idempotency_key,
    )
    response.headers["Location"] = f"/api/v1/operations/{op.operation_id}"
    return OperationAcceptedResponse(operation=op)


@router.get(
    "/{group_id}",
    operation_id="getGroup",
    response_model=GroupDetail,
    summary="获取端口组详情",
)
def get_group(
    response: Response,
    group_id: str = Path(...),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> GroupDetail:
    detail = container.repo.get_group_detail(group_id)
    response.headers["ETag"] = f'"v{detail.config_version}"'
    return detail


@router.patch(
    "/{group_id}",
    operation_id="updateGroup",
    response_model=OperationAcceptedResponse | GroupUpdateDryRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="修改端口组名称、端口或地面站协议（异步受理或 dry-run 预演）",
)
def update_group(
    body: UpdateGroupRequest,
    group_id: str = Path(...),
    dry_run: bool = Query(default=False),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128),
    if_match: str | None = Header(default=None, alias="If-Match"),
    admin: UserProfile = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> Response:
    if_match_ver = parse_if_match_version(if_match)
    if dry_run:
        plan = container.repo.dry_run_update_group(
            group_id,
            body,
            if_match_version=if_match_ver,
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content=plan.model_dump())

    op = container.repo.enqueue_update_group(
        group_id,
        body,
        requested_by=admin.username,
        idempotency_key=idempotency_key,
        if_match_version=if_match_ver,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=OperationAcceptedResponse(operation=op).model_dump(),
        headers={"Location": f"/api/v1/operations/{op.operation_id}"},
    )


@router.delete(
    "/{group_id}",
    operation_id="deleteGroup",
    response_model=OperationAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="删除端口组（异步受理；保留未到期历史归档）",
)
def delete_group(
    response: Response,
    group_id: str = Path(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128),
    if_match: str | None = Header(default=None, alias="If-Match"),
    admin: UserProfile = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OperationAcceptedResponse:
    if_match_ver = parse_if_match_version(if_match)
    op = container.repo.enqueue_delete_group(
        group_id,
        requested_by=admin.username,
        idempotency_key=idempotency_key,
        if_match_version=if_match_ver,
    )
    response.headers["Location"] = f"/api/v1/operations/{op.operation_id}"
    return OperationAcceptedResponse(operation=op)


@router.post(
    "/{group_id}/actions/{action}",
    operation_id="triggerGroupAction",
    response_model=OperationAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="触发端口组生命周期动作",
)
def trigger_group_action(
    response: Response,
    group_id: str = Path(...),
    action: GroupActionType = Path(...),
    body: GroupActionRequest | None = None,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128),
    if_match: str | None = Header(default=None, alias="If-Match"),
    admin: UserProfile = Depends(require_admin),
    container: AppContainer = Depends(get_container),
) -> OperationAcceptedResponse:
    if_match_ver = parse_if_match_version(if_match)
    op = container.repo.enqueue_group_action(
        group_id,
        action,
        requested_by=admin.username,
        idempotency_key=idempotency_key,
        if_match_version=if_match_ver,
        expected_config_version=body.expected_config_version if body else None,
    )
    response.headers["Location"] = f"/api/v1/operations/{op.operation_id}"
    return OperationAcceptedResponse(operation=op)
