"""FastAPI dependency injection for auth, RBAC, and repository/archive access."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from flightlink.archive.service import ArchiveService
from flightlink.clock import Clock, SystemClock
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.errors import ForbiddenError, UnauthorizedError
from flightlink.domain.operations_repo import ControlPlaneRepository
from flightlink.schemas.auth import UserProfile

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="bearerAuth")


@dataclass
class AppContainer:
    settings: Settings
    db: Database
    clock: Clock
    repo: ControlPlaneRepository
    archive: ArchiveService


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    container: AppContainer = Depends(get_container),
) -> UserProfile:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("AUTH_REQUIRED", "Authentication required.")
    return container.repo.authenticate_bearer(credentials.credentials)


def require_admin(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    if user.role != "admin":
        raise ForbiddenError("Admin role is required for write operations.")
    return user
