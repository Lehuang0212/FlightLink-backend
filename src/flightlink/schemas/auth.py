"""Authentication & user profile schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

UserRole = Literal["admin", "viewer"]


class UserProfile(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: UserRole
    created_at: str
    last_login_at: str | None = None


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., min_length=16)


class LogoutRequest(BaseModel):
    refresh_token: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: str
    refresh_expires_at: str
    user: UserProfile
