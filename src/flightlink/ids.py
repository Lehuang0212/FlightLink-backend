"""Opaque identifier generators."""

from __future__ import annotations

import secrets
import time


def _opaque(prefix: str) -> str:
    ts_ms = int(time.time() * 1000)
    rand_hex = secrets.token_hex(6)
    return f"{prefix}_{ts_ms:012x}{rand_hex}"


def new_group_id() -> str:
    return _opaque("grp")


def new_operation_id() -> str:
    return _opaque("op")


def new_session_id() -> str:
    return _opaque("sess")


def new_file_id(source: str) -> str:
    return f"file_{secrets.token_hex(8)}_{source}"


def new_user_id() -> str:
    return _opaque("usr")


def new_token_id() -> str:
    return _opaque("tok")


def new_event_id() -> str:
    return _opaque("evt")


def new_request_id() -> str:
    return _opaque("req")


def new_instance_id() -> str:
    return f"ecs_{secrets.token_hex(8)}"
