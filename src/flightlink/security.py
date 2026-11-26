"""Password hashing (stdlib scrypt), JWT access/refresh tokens, and revocable API tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError

from flightlink.clock import Clock, SystemClock

JWT_ALGORITHM = "HS256"
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt_b64}${hash_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = stored_hash.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
        return hmac.compare_digest(derived, expected)
    except Exception:
        return False


def sha256_hex(raw: str | bytes) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return hashlib.sha256(data).hexdigest()


def generate_opaque_secret(prefix: str = "flk") -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def create_access_token(
    *,
    secret: str,
    user_id: str,
    username: str,
    role: str,
    ttl_seconds: int,
    clock: Clock | None = None,
) -> tuple[str, datetime]:
    now = (clock or SystemClock()).now()
    expires_at = now + timedelta(seconds=ttl_seconds)
    payload: dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "role": role,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return token, expires_at


class TokenExpiredError(Exception):
    pass


class TokenInvalidError(Exception):
    pass


def decode_access_token(
    token: str,
    *,
    secret: str,
    clock: Clock | None = None,
) -> dict[str, Any]:
    now = (clock or SystemClock()).now()
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            options={"verify_exp": False},
        )
    except InvalidTokenError as exc:
        raise TokenInvalidError("Invalid access token") from exc

    if payload.get("typ") != "access" or not payload.get("sub"):
        raise TokenInvalidError("Invalid token claims")

    exp = payload.get("exp")
    if exp is None:
        raise TokenInvalidError("Missing token expiration")
    if int(now.timestamp()) >= int(exp):
        raise TokenExpiredError("Access token expired")
    return payload
