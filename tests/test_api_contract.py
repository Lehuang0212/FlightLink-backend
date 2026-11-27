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


def test_sync_security_group_and_verify_actions_succeed_and_guard_busy(tmp_path: Path) -> None:
    app, login, agent, _archive, _fake, _clock = _setup(tmp_path)
    with TestClient(app) as client:
        admin_h = login(client, "ops_admin", "AdminPassword!1")

        r_create = client.post(
            "/api/v1/groups",
            headers=admin_h,
            json={"name": "一号机", "dtu_port": 5760, "gcs_protocol": "tcp", "gcs_port": 14552},
        )
        assert r_create.status_code == 202
        gid = r_create.json()["operation"]["group_id"]
        agent.process_pending_operations()

        # 1. sync-security-group MUST succeed (not cancelled with STALE_GENERATION!)
        r_sync = client.post(f"/api/v1/groups/{gid}/actions/sync-security-group", headers=admin_h)
        assert r_sync.status_code == 202
        sync_op_id = r_sync.json()["operation"]["operation_id"]
        agent.process_pending_operations()

        sync_op = client.get(f"/api/v1/operations/{sync_op_id}", headers=admin_h).json()
        assert sync_op["status"] == "succeeded", sync_op
        assert sync_op["error_code"] is None
        step_map = {s["step_name"]: s["status"] for s in sync_op["steps"]}
        assert step_map["reconcile_security_group"] == "succeeded"
        assert step_map["verify_security_group"] == "succeeded"

        # 2. verify MUST succeed when group is idle
        r_ver = client.post(f"/api/v1/groups/{gid}/actions/verify", headers=admin_h)
        assert r_ver.status_code == 202
        ver_op_id = r_ver.json()["operation"]["operation_id"]
        agent.process_pending_operations()

        ver_op = client.get(f"/api/v1/operations/{ver_op_id}", headers=admin_h).json()
        assert ver_op["status"] == "succeeded", ver_op
        ver_steps = {s["step_name"]: s["status"] for s in ver_op["steps"]}
        assert ver_steps["inspect_actual_runtime"] == "succeeded"
        assert ver_steps["reconcile_state"] == "succeeded"

        # 3. verify while another operation (`start`) is pending MUST return 409 GROUP_BUSY
        #    and MUST NOT cancel/kill the active `start` operation!
        r_start = client.post(f"/api/v1/groups/{gid}/actions/start", headers=admin_h)
        assert r_start.status_code == 202
        start_op_id = r_start.json()["operation"]["operation_id"]

        r_ver_busy = client.post(f"/api/v1/groups/{gid}/actions/verify", headers=admin_h)
        assert r_ver_busy.status_code == 409
        assert r_ver_busy.json()["error"]["code"] == "GROUP_BUSY"

        agent.process_pending_operations()
        start_op = client.get(f"/api/v1/operations/{start_op_id}", headers=admin_h).json()
        assert start_op["status"] == "succeeded", start_op


def test_periodic_reconcile_keeps_fresh_preserves_connected_mavproxy_and_detects_staleness(tmp_path: Path) -> None:
    from flightlink.adapters.interfaces import CloudTimeoutError

    app, login, agent, _archive, fake, clock = _setup(tmp_path)
    with TestClient(app) as client:
        admin_h = login(client, "ops_admin", "AdminPassword!1")

        # Create one stopped group and one running group
        gid_stopped = client.post(
            "/api/v1/groups",
            headers=admin_h,
            json={"name": "停机组", "dtu_port": 5760, "gcs_protocol": "tcp", "gcs_port": 14552},
        ).json()["operation"]["group_id"]
        gid_running = client.post(
            "/api/v1/groups",
            headers=admin_h,
            json={"name": "运行组", "dtu_port": 5761, "gcs_protocol": "udp", "gcs_port": 14553},
        ).json()["operation"]["group_id"]
        agent.process_pending_operations()

        assert client.post(f"/api/v1/groups/{gid_running}/actions/start", headers=admin_h).status_code == 202
        agent.process_pending_operations()

        # Simulate live FC heartbeat having promoted MAVProxy to 'connected'
        with agent.db.write_transaction() as conn:
            conn.execute(
                "UPDATE group_runtime_status SET mavproxy_state = 'connected', fc_state = 'online' WHERE group_id = ?",
                (gid_running,),
            )

        # Advance > 30s and trigger periodic reconciliation
        clock.advance(seconds=45)
        agent.run_periodic_reconcile_if_due()

        # Check both list and detail endpoints: ZERO stale flags, stopped group NOT auto-started, connected MAVProxy preserved!
        listed = {item["group_id"]: item for item in client.get("/api/v1/groups", headers=admin_h).json()["items"]}
        for gid in (gid_stopped, gid_running):
            assert listed[gid]["stale"] is False
            assert listed[gid]["router"]["stale"] is False
            assert listed[gid]["mavproxy"]["stale"] is False
            assert listed[gid]["security_group"]["stale"] is False

        det_stopped = client.get(f"/api/v1/groups/{gid_stopped}", headers=admin_h).json()
        assert det_stopped["lifecycle_state"] == "configured_stopped"
        assert det_stopped["router"]["state"] == "stopped"
        assert det_stopped["mavproxy"]["state"] == "disconnected"
        assert det_stopped["stale"] is False

        det_running = client.get(f"/api/v1/groups/{gid_running}", headers=admin_h).json()
        assert det_running["router"]["state"] == "running"
        assert det_running["mavproxy"]["state"] == "connected"
        assert det_running["stale"] is False
        assert det_running["security_group"]["stale"] is False

        # Advance past sg_reconcile_interval_seconds (120s): cloud SG read-only poll refreshes sg_observed_at
        clock.advance(seconds=130)
        agent.run_periodic_reconcile_if_due()
        det_running = client.get(f"/api/v1/groups/{gid_running}", headers=admin_h).json()
        assert det_running["security_group"]["state"] == "synced"
        assert det_running["security_group"]["stale"] is False

        # Temporary cloud failure during periodic SG poll MUST NOT restart or stop healthy running Router!
        orig_list_rules = fake.list_rules
        orig_pid = det_running["router"]["pid"]

        def _boom(*args, **kwargs):
            raise CloudTimeoutError("simulated Aliyun timeout")

        fake.list_rules = _boom  # type: ignore[method-assign]
        try:
            clock.advance(seconds=130)
            agent.run_periodic_reconcile_if_due()
        finally:
            fake.list_rules = orig_list_rules  # type: ignore[method-assign]

        det_after_err = client.get(f"/api/v1/groups/{gid_running}", headers=admin_h).json()
        assert det_after_err["router"]["state"] == "running"
        assert det_after_err["router"]["pid"] == orig_pid

        # External cloud rule deletion -> periodic SG poll detects 'drift' with fresh sg_observed_at
        sg_rules = fake.list_rules(security_group_id="sg-test-001")
        for r in sg_rules:
            if gid_running in r.description and r.port == 14553:
                fake.revoke_rule(security_group_id="sg-test-001", rule_id=r.rule_id)
                break
        clock.advance(seconds=130)
        agent.run_periodic_reconcile_if_due()
        det_drift = client.get(f"/api/v1/groups/{gid_running}", headers=admin_h).json()
        assert det_drift["security_group"]["state"] == "drift"
        assert det_drift["security_group"]["stale"] is False

        # If agent stops running past thresholds, stale MUST truthfully become True!
        clock.advance(seconds=45)
        det_local_stale = client.get(f"/api/v1/groups/{gid_running}", headers=admin_h).json()
        assert det_local_stale["stale"] is True
        assert det_local_stale["router"]["stale"] is True
        assert det_local_stale["mavproxy"]["stale"] is True
        assert det_local_stale["security_group"]["stale"] is False  # within 360s SG threshold

        clock.advance(seconds=350)
        det_all_stale = client.get(f"/api/v1/groups/{gid_running}", headers=admin_h).json()
        assert det_all_stale["security_group"]["stale"] is True
