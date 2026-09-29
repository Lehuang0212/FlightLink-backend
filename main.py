"""Compatibility entry point for ``uvicorn main:app``."""

from flightlink_backend.main import app

__all__ = ["app"]
