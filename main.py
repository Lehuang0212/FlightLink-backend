"""ASGI entrypoint for `uvicorn main:app`."""

from __future__ import annotations

from pathlib import Path

from flightlink.api.app import create_app
from flightlink.config import Settings

_default_settings = Settings(data_dir=Path("./data"))
app = create_app(settings=_default_settings)
