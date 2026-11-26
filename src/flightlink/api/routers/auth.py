"""Authentication router (production accounts only; no demo accounts)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from flightlink.api.deps import AppContainer, get_container, get_current_user
from flightlink.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshTokenRequest,
    TokenResponse,
    UserProfile,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post(
    "/login",
    operation_id="login",
    response_model=TokenResponse,
    summary="账号密码登录",
)
def login(
    body: LoginRequest,
    container: AppContainer = Depends(get_container),
) -> TokenResponse:
    return container.repo.login(username=body.username, password=body.password)


@router.post(
    "/refresh",
    operation_id="refreshToken",
    response_model=TokenResponse,
    summary="刷新访问令牌",
)
def refresh_token(
    body: RefreshTokenRequest,
    container: AppContainer = Depends(get_container),
) -> TokenResponse:
    return container.repo.refresh_session(refresh_token=body.refresh_token)


@router.post(
    "/logout",
    operation_id="logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="退出登录并撤销当前刷新令牌",
)
def logout(
    body: LogoutRequest | None = None,
    user: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> Response:
    container.repo.logout(
        user_id=user.user_id,
        refresh_token=body.refresh_token if body else None,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    operation_id="getCurrentUser",
    response_model=UserProfile,
    summary="获取当前已认证用户信息",
)
def get_me(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    return user
