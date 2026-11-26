"""Entry point for `flightlink-api` service."""

from __future__ import annotations

import uvicorn

from flightlink.api.app import create_app
from flightlink.config import Settings


def main() -> None:
    settings = Settings()
    app = create_app(settings=settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


if __name__ == "__main__":
    main()
