"""API contract tests: auth, RBAC, 12 nullable telemetry items, idempotency, 412, and deleted-group 404/200."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from flightlink.adapters.interfaces import FakeInfrastructure
from flightlink.api.app import create_app
from flightlink.archive.service import ArchiveService
from flightlink.clock import ManualClock
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.lifecycle import LifecycleAgent


def _setup(tmp_path: Path):
    clock = ManualClock()
    settings = Settings(
        data_dir=tmp_path,
        aliyun_region="cn-beijing",
        aliyun_security_group_id="sg-test-001",
        dtu_allowed_cidrs=["0.0.0.0/0"],
        gcs_allowed_cidrs=["0.0.0.0/0"],
        adapter_mode="fake",
    )
    db = Database(settings.resolved_db_path, clock=clock)
    db.init_schema(ecs_instance_id="ecs_test_01")
    archive = ArchiveService(db, settings, clock=clock)
    app = create_app(settings=settings, db=db, clock=clock, archive_service=archive)
    fake = FakeInfrastructure(clock=clock)
    agent = LifecycleAgent(
        db, settings, supervisor=fake, sockets=fake, sg_client=fake, archive_service=archive, clock=clock
    )
    repo = app.state.container.repo
    repo.create_user(username="ops_admin", password="AdminPassword!1", role="admin")
    repo.create_user(username="ops_viewer", password="ViewerPassword!1", role="viewer")
    return app, client_headers_fn(app), agent, archive, fake, clock


def client_headers_fn(app):
    def _login(client: TestClient, username: str, password: str) -> dict[str, str]:
        resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    return _login


def test_no_demo_account_and_rbac_enforcement(tmp_path: Path) -> None:
    app, login_fn, _, _, _, clock = _setup(tmp_path)
    with TestClient(app) as client:
        # Preview demo credentials MUST NOT work
        r_demo = client.post("/api/v1/auth/login", json={"username": "admin", "password": "flightlink"})
        assert r_demo.status_code == 401
        assert r_demo.json()["error"]["code"] == "INVALID_CREDENTIALS"

        # Unauthenticated rejected
        assert client.get("/api/v1/groups").status_code == 401
        assert client.get("/api/v1/archive/groups").status_code == 401

        # Viewer can read, cannot write (403)
        viewer_h = login_fn(client, "ops_viewer", "ViewerPassword!1")
        assert client.get("/api/v1/overview", headers=viewer_h).status_code == 200
        r_forbid = client.post(
            "/api/v1/groups",
            headers=viewer_h,
            json={"name": "一号机", "dtu_port": 5760, "gcs_protocol": "tcp", "gcs_port": 14552},
        )
        assert r_forbid.status_code == 403
        assert r_forbid.json()["error"]["code"] == "INSUFFICIENT_ROLE"

        # Token refresh rotation and logout revocation
        r_tok = client.post("/api/v1/auth/login", json={"username": "ops_admin", "password": "AdminPassword!1"})
        rt1 = r_tok.json()["refresh_token"]
        r_ref = client.post("/api/v1/auth/refresh", json={"refresh_token": rt1})
        assert r_ref.status_code == 200
        rt2 = r_ref.json()["refresh_token"]
        # Reusing rotated rt1 must fail
        assert client.post("/api/v1/auth/refresh", json={"refresh_token": rt1}).status_code == 401
        # Logout revokes rt2
        assert client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {r_ref.json()['access_token']}"},
            json={"refresh_token": rt2},
        ).status_code == 204
        assert client.post("/api/v1/auth/refresh", json={"refresh_token": rt2}).status_code == 401

        # Expired access token rejected
        clock.advance(hours=2)
        r_exp = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {r_ref.json()['access_token']}"})
        assert r_exp.status_code == 401
        assert r_exp.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_idempotency_conflict_version_412_telemetry_nulls_and_deleted_archive_200(tmp_path: Path) -> None:
    app, login_fn, agent, archive, _, _ = _setup(tmp_path)
    with TestClient(app) as client:
        admin_h = login_fn(client, "ops_admin", "AdminPassword!1")

        # Validation 422 when dtu_port == gcs_port
        r_422 = client.post(
            "/api/v1/groups",
            headers=admin_h,
            json={"name": "坏组", "dtu_port": 5760, "gcs_protocol": "tcp", "gcs_port": 5760},
        )
        assert r_422.status_code == 422
        assert r_422.json()["error"]["code"] == "VALIDATION_ERROR"

        # Create with Idempotency-Key -> 202 Accepted
        idem_key = "idem-create-group-0001"
        body = {"name": "一号机", "dtu_port": 5760, "gcs_protocol": "tcp", "gcs_port": 14552}
        r1 = client.post("/api/v1/groups", headers={**admin_h, "Idempotency-Key": idem_key}, json=body)
        assert r1.status_code == 202
        op_id = r1.json()["operation"]["operation_id"]
        gid = r1.json()["operation"]["group_id"]
        assert r1.headers["Location"] == f"/api/v1/operations/{op_id}"

        # Exact replay returns same operation
        r2 = client.post("/api/v1/groups", headers={**admin_h, "Idempotency-Key": idem_key}, json=body)
        assert r2.status_code == 202
        assert r2.json()["operation"]["operation_id"] == op_id

        # Same Idempotency-Key with DIFFERENT payload MUST return 409 IDEMPOTENCY_CONFLICT
        r_conflict = client.post(
            "/api/v1/groups",
            headers={**admin_h, "Idempotency-Key": idem_key},
            json={"name": "二号机", "dtu_port": 5761, "gcs_protocol": "tcp", "gcs_port": 14553},
        )
        assert r_conflict.status_code == 409
        assert r_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        agent.process_pending_operations()

        # Check group detail: 12 telemetry metrics strictly null when unobserved!
        r_det = client.get(f"/api/v1/groups/{gid}", headers=admin_h)
        assert r_det.status_code == 200
        det = r_det.json()
        assert det["lifecycle_state"] == "configured_stopped"
        assert det["sysid"] is None
        tele = det["telemetry"]
        for k in (
            "flight_mode", "armed", "battery_remaining_pct", "battery_voltage_v",
            "gps_satellites", "gps_fix_type", "gps_hdop", "altitude_msl_m",
            "relative_altitude_m", "ground_speed_mps", "rx_rate_kbps", "tx_rate_kbps",
            "packets_lost", "packets_received", "packet_loss_pct",
        ):
            assert tele[k] is None, f"Telemetry field {k} must be null without real source"

        # Optimistic concurrency mismatch -> 412 CONFIG_VERSION_MISMATCH
        r_412 = client.patch(
            f"/api/v1/groups/{gid}",
            headers={**admin_h, "If-Match": '"v99"'},
            json={"name": "改名"},
        )
        assert r_412.status_code == 412
        assert r_412.json()["error"]["code"] == "CONFIG_VERSION_MISMATCH"

        # Record a session so archive has history, then delete group
        archive.record_valid_fc_heartbeat(group_id=gid, sysid=1)
        archive.append_log_event(group_id=gid, source="router", level="INFO", text="router started")
        r_del = client.delete(f"/api/v1/groups/{gid}", headers=admin_h)
        assert r_del.status_code == 202
        agent.process_pending_operations()

        # Contract: after deletion, /groups/{id} returns 404, /archive/groups/{id} returns 200 (and requires auth!)
        assert client.get(f"/api/v1/groups/{gid}", headers=admin_h).status_code == 404
        assert client.get(f"/api/v1/archive/groups/{gid}").status_code == 401
        r_arch = client.get(f"/api/v1/archive/groups/{gid}", headers=admin_h)
        assert r_arch.status_code == 200
        assert r_arch.json()["deleted"] is True
        assert r_arch.json()["sealed_session_count"] == 1
