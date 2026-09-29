from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..config import settings
from ..database import get_db
from ..security import (
    hash_session_token,
    new_session_token,
    verify_password,
    verify_unknown_user_password,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class AdminPublic(BaseModel):
    id: int
    username: str


def get_current_admin(
    request: Request,
    connection: sqlite3.Connection = Depends(get_db),
) -> AdminPublic:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    now = int(time.time())
    row = connection.execute(
        """
        SELECT users.id, users.username
        FROM admin_sessions AS sessions
        JOIN admin_users AS users ON users.id = sessions.user_id
        WHERE sessions.token_hash = ?
          AND sessions.expires_at > ?
          AND users.is_active = 1
        """,
        (hash_session_token(token), now),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return AdminPublic(id=row["id"], username=row["username"])


@router.post("/login", response_model=AdminPublic)
def login(
    payload: LoginRequest,
    response: Response,
    connection: sqlite3.Connection = Depends(get_db),
) -> AdminPublic:
    username = payload.username.strip()
    row = connection.execute(
        "SELECT id, username, password_hash, is_active FROM admin_users WHERE username = ?",
        (username,),
    ).fetchone()

    if row is None:
        verify_unknown_user_password(payload.password)
        valid = False
    else:
        valid = verify_password(payload.password, row["password_hash"])

    if row is None or not valid or not row["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    now = int(time.time())
    expires_at = now + settings.session_ttl_seconds
    raw_token = new_session_token()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now,))
        connection.execute(
            "INSERT INTO admin_sessions(token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (hash_session_token(raw_token), row["id"], now, expires_at),
        )
        connection.execute("UPDATE admin_users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/api/v1",
    )
    return AdminPublic(id=row["id"], username=row["username"])


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    request: Request,
    connection: sqlite3.Connection = Depends(get_db),
) -> Response:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        connection.execute(
            "DELETE FROM admin_sessions WHERE token_hash = ?",
            (hash_session_token(token),),
        )
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/api/v1",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=AdminPublic)
def me(admin: AdminPublic = Depends(get_current_admin)) -> AdminPublic:
    return admin
