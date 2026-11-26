"""CI contract drift tests for docs/api/openapi.yaml and passive read-only API guarantees."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from flightlink.api.app import create_app
from flightlink.api.openapi_export import check_openapi_drift
from flightlink.clock import ManualClock
from flightlink.config import Settings
from flightlink.db import Database


def test_openapi_yaml_matches_live_fastapi_routes_with_zero_drift() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.yaml"
    assert spec_path.exists()
    drift = check_openapi_drift(spec_path)
    assert drift == [], f"OpenAPI drift found:\n" + "\n".join(drift)


def test_api_startup_health_and_gets_have_zero_side_effects(tmp_path: Path) -> None:
    clock = ManualClock()
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.resolved_db_path, clock=clock)
    app = create_app(settings=settings, db=db, clock=clock)

    app.state.container.repo.create_user(
        username="ops_admin", password="StrongPassword!1", role="admin"
    )

    with TestClient(app) as client:
        r_health = client.get("/api/v1/health")
        assert r_health.status_code == 200
        assert r_health.json()["db_ok"] is True

        tok = client.post(
            "/api/v1/auth/login",
            json={"username": "ops_admin", "password": "StrongPassword!1"},
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {tok}"}

        assert client.get("/api/v1/overview", headers=headers).status_code == 200
        assert client.get("/api/v1/groups", headers=headers).status_code == 200
        assert client.get("/api/v1/logs", headers=headers).status_code == 200
        assert client.get("/api/v1/archive/groups", headers=headers).status_code == 200
        assert client.get("/api/v1/archive/changes", headers=headers).status_code == 200

    # Zero operations queued, zero sessions started, zero router configs written by passive startup/GETs!
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM archive_sessions").fetchone()[0] == 0
    assert not list(settings.resolved_router_config_dir.glob("*.conf"))
