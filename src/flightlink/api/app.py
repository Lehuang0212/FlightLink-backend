"""FastAPI application factory (`flightlink-api`).

Strictly enforces:
- Starting the API, calling `/health`, or calling any `GET` endpoint NEVER starts
  listeners, capture collectors, or cloud security group syncs.
- Explicit CORS origins (never wildcard with credentials).
- Unified `ErrorEnvelope` for both `DomainError` and FastAPI validation errors.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from flightlink import __version__
from flightlink.api.deps import AppContainer
from flightlink.api.routers import archive, auth, groups, health, logs, operations, overview
from flightlink.archive.service import ArchiveService
from flightlink.clock import Clock, SystemClock
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.errors import DomainError
from flightlink.domain.operations_repo import ControlPlaneRepository
from flightlink.ids import new_request_id
from flightlink.schemas.common import ErrorBody, ErrorDetail, ErrorEnvelope


def create_app(
    *,
    settings: Settings | None = None,
    db: Database | None = None,
    clock: Clock | None = None,
    archive_service: ArchiveService | None = None,
) -> FastAPI:
    cfg = settings or Settings()
    clk = clock or (db.clock if db else SystemClock())
    database = db or Database(cfg.resolved_db_path, clock=clk)
    database.init_schema(ecs_instance_id=cfg.ecs_instance_id)

    repo = ControlPlaneRepository(database, cfg, clock=clk)
    arch = archive_service or ArchiveService(database, cfg, clock=clk)
    container = AppContainer(settings=cfg, db=database, clock=clk, repo=repo, archive=arch)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Strictly passive: record API heartbeat only; NEVER start listeners, capture, or cloud sync!
        database.record_component_heartbeat("flightlink-api", "ok", "API process started (passive)")
        yield

    app = FastAPI(
        title="FlightLink ECS Backend API",
        version=__version__,
        description="FlightLink ECS 1.0 control-plane, live observation, and session archive API.",
        lifespan=lifespan,
    )
    app.state.container = container

    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        req_id = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        req_id = getattr(request.state, "request_id", new_request_id())
        envelope = ErrorEnvelope(
            error=ErrorBody(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                details=[ErrorDetail(field=d.field, message=d.message) for d in exc.details],
                request_id=req_id,
            )
        )
        return JSONResponse(status_code=exc.status_code, content=envelope.model_dump())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        req_id = getattr(request.state, "request_id", new_request_id())
        details = [
            ErrorDetail(
                field=".".join(str(part) for part in err.get("loc", []) if part != "body") or "request",
                message=str(err.get("msg", "Invalid value")),
            )
            for err in exc.errors()
        ]
        envelope = ErrorEnvelope(
            error=ErrorBody(
                code="VALIDATION_ERROR",
                message="Request validation failed.",
                retryable=False,
                details=details,
                request_id=req_id,
            )
        )
        return JSONResponse(status_code=422, content=envelope.model_dump())

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(health.router)
    v1.include_router(auth.router)
    v1.include_router(overview.router)
    v1.include_router(groups.router)
    v1.include_router(operations.router)
    v1.include_router(logs.router)
    v1.include_router(archive.router)
    app.include_router(v1)
    return app
