"""Entry point for `flightlink-api` service."""

from __future__ import annotations

import argparse

import uvicorn

from flightlink.api.app import create_app
from flightlink.config import Settings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="FlightLink ECS 1.0 Control & Read API")
    parser.add_argument("--host", default=None, help="Override API listen host (default: FLIGHTLINK_API_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Override API listen port (default: FLIGHTLINK_API_PORT or 8000)")
    args = parser.parse_args(argv)

    overrides: dict[str, object] = {}
    if args.host is not None:
        overrides["api_host"] = args.host
    if args.port is not None:
        if not (1 <= args.port <= 65535):
            parser.error("--port must be between 1 and 65535")
        overrides["api_port"] = args.port

    settings = Settings(**overrides)  # type: ignore[arg-type]
    app = create_app(settings=settings)
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


if __name__ == "__main__":
    main()
