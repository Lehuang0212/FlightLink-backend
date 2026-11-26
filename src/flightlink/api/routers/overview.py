"""Overview router (strictly read-only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flightlink.api.deps import AppContainer, get_container, get_current_user
from flightlink.schemas.auth import UserProfile
from flightlink.schemas.groups import OverviewResponse

router = APIRouter(tags=["Overview"])


@router.get(
    "/overview",
    operation_id="getOverview",
    response_model=OverviewResponse,
    summary="获取总览、三组件状态、磁盘水位与端口组摘要",
)
def get_overview(
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> OverviewResponse:
    return container.repo.get_overview()
