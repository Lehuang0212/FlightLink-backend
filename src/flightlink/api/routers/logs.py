"""Live cursor-based logs and explicit CLI/diagnostics export router."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse

from flightlink.api.deps import AppContainer, get_container, get_current_user
from flightlink.schemas.auth import UserProfile
from flightlink.schemas.logs import LiveLogPageResponse

router = APIRouter(prefix="/logs", tags=["Logs"])


@router.get(
    "",
    operation_id="queryLiveLogs",
    response_model=LiveLogPageResponse,
    summary="按组、来源、级别、关键词与游标读取实时日志",
)
def query_live_logs(
    group_id: str | None = Query(default=None),
    source: Literal["all", "router", "mavproxy"] = Query(default="all"),
    level: Literal["all", "DEBUG", "INFO", "WARN", "ERROR"] = Query(default="all"),
    q: str | None = Query(default=None, max_length=160),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> LiveLogPageResponse:
    return container.archive.query_live_logs(
        group_id=group_id,
        source=source,
        level=level,
        q=q,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/export",
    operation_id="exportLiveLogs",
    response_class=PlainTextResponse,
    summary="显式导出实时日志文本（CLI / 排障专用）",
)
def export_live_logs(
    group_id: str | None = Query(default=None),
    source: Literal["all", "router", "mavproxy"] = Query(default="all"),
    level: Literal["all", "DEBUG", "INFO", "WARN", "ERROR"] = Query(default="all"),
    q: str | None = Query(default=None, max_length=160),
    limit: int = Query(default=1000, ge=1, le=5000),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> PlainTextResponse:
    page = container.archive.query_live_logs(
        group_id=group_id,
        source=source,
        level=level,
        q=q,
        cursor=None,
        limit=limit,
    )
    lines = [
        f"{item.ts} [{item.group_id}] [{item.source.upper()}] [{item.level}] {item.text}"
        for item in page.items
    ]
    body = ("\n".join(lines) + "\n") if lines else ""
    filename = f"flightlink-live-{group_id or 'all'}-{source}.log"
    return PlainTextResponse(
        content=body,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
