"""Health check router (strictly read-only, zero side effects)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flightlink import __version__
from flightlink.api.deps import AppContainer, get_container
from flightlink.schemas.common import HealthResponse

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    operation_id="getHealth",
    response_model=HealthResponse,
    summary="服务健康检查",
)
def get_health(container: AppContainer = Depends(get_container)) -> HealthResponse:
    db_ok = container.db.health_check()
    ecs_id = container.db.get_meta("ecs_instance_id", "ecs_unknown") or "ecs_unknown"
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        version=__version__,
        ecs_instance_id=ecs_id,
        observed_at=container.clock.now_iso(),
        db_ok=db_ok,
    )
