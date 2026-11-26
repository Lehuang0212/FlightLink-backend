"""P4 Archive Engine, Approved Incremental Auto-Save Sync Client, and Final Release Acceptance Scenario."""

from __future__ import annotations

import errno
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from flightlink.adapters.interfaces import FakeInfrastructure
from flightlink.api.app import create_app
from flightlink.archive.service import ArchiveService
from flightlink.clock import ManualClock
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.lifecycle import LifecycleAgent
from flightlink.schemas.groups import CreateGroupRequest

# Load runnable example client directly from docs/examples/python_archive_sync.py
_SYNC_PATH = Path(__file__).resolve().parents[1] / "docs" / "examples" / "python_archive_sync.py"
_spec = importlib.util.spec_from_file_location("python_archive_sync", _SYNC_PATH)
assert _spec and _spec.loader
_sync_mod = importlib.util.module_from_spec(_spec)
sys.modules["python_archive_sync"] = _sync_mod
_spec.loader.exec_module(_sync_mod)
ArchiveAutoSaveClient = _sync_mod.ArchiveAutoSaveClient


class CountingTransport(httpx.BaseTransport):
    """Wraps FastAPI TestClient to count per-URL requests and inject deterministic network outages."""

    def __init__(self, app: Any) -> None:
        self._inner = TestClient(app, base_url="http://test")
        self.request_log: list[tuple[str, str]] = []
        self.fail_paths_containing: set[str] = set()
        self.fail_once_containing: set[str] = set()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.request_log.append((request.method, path))
        for needle in list(self.fail_once_containing):
            if needle in path:
                self.fail_once_containing.discard(needle)
                raise httpx.ConnectError(f"Injected one-shot network failure on {path}")
        for needle in self.fail_paths_containing:
            if needle in path:
                raise httpx.ConnectError(f"Injected network outage on {path}")
        resp = self._inner.request(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            status_code=resp.status_code,
            headers=dict(resp.headers),
            content=resp.content,
            request=request,
        )


def _setup_stack(tmp_path: Path):
    clock = ManualClock()
    settings = Settings(
        data_dir=tmp_path / "ecs",
        aliyun_region="cn-beijing",
        aliyun_security_group_id="sg-p4-test",
        dtu_allowed_cidrs=["0.0.0.0/0"],
        gcs_allowed_cidrs=["0.0.0.0/0"],
        adapter_mode="fake",
        fc_heartbeat_grace_seconds=15,
    )
    db = Database(settings.resolved_db_path, clock=clock)
    db.init_schema(ecs_instance_id="ecs_prod_01")
    archive = ArchiveService(db, settings, clock=clock)
    app = create_app(settings=settings, db=db, clock=clock, archive_service=archive)
    fake = FakeInfrastructure(clock=clock)
    agent = LifecycleAgent(
        db, settings, supervisor=fake, sockets=fake, sg_client=fake, archive_service=archive, clock=clock
    )
    repo = app.state.container.repo
    repo.create_user(username="ops_admin", password="AdminPassword!1", role="admin")
    _, viewer_token = repo.create_api_token(username="ops_admin", name="desktop-sync", role="viewer")
    transport = CountingTransport(app)
    http = httpx.Client(transport=transport, base_url="http://test")
    return app, repo, agent, archive, fake, clock, viewer_token, transport, http


# =====================================================================
# Incremental Acceptance #1, #2, #3:
# Default auto-save, network outage -> manual retry only missing files,
# non-blocking cursor, restart after partial success does not re-download!
# =====================================================================
def test_incremental_auto_save_partial_restart_and_manual_retry(tmp_path: Path) -> None:
    _, repo, agent, archive, _, clock, token, transport, http = _setup_stack(tmp_path)

    op = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    gid = op.group_id
    agent.execute_operation(op.operation_id)

    # Session 1: both router and mavproxy logs exist
    s1 = archive.record_valid_fc_heartbeat(group_id=gid, sysid=1)
    archive.append_log_event(group_id=gid, source="router", level="INFO", text="s1 router line 1", event_id="e1")
    archive.append_log_event(group_id=gid, source="router", level="INFO", text="s1 router line 1", event_id="e1")  # dedup!
    archive.append_log_event(group_id=gid, source="mavproxy", level="INFO", text="s1 mavproxy line 1", event_id="e2")

    # While active, /archive/changes MUST NOT expose unsealed session!
    r_ch = http.get("/api/v1/archive/changes", headers={"Authorization": f"Bearer {token}"})
    assert r_ch.status_code == 200
    assert r_ch.json()["items"] == []

    # Seal s1 via 15s heartbeat timeout
    clock.advance(seconds=16)
    assert archive.check_heartbeat_timeouts() == [s1]

    # Session 2: sealed right after s1
    s2 = archive.record_valid_fc_heartbeat(group_id=gid, sysid=1)
    archive.append_log_event(group_id=gid, source="router", level="INFO", text="s2 router line", event_id="e3")
    archive.append_log_event(group_id=gid, source="mavproxy", level="INFO", text="s2 mavproxy line", event_id="e4")
    clock.advance(seconds=16)
    assert archive.check_heartbeat_timeouts() == [s2]

    # Inject network failure ONLY on s1's mavproxy file so s1 partially succeeds (router + metadata verified, mavproxy fails),
    # while s2 MUST NOT be blocked and MUST finish saving completely!
    s1_detail = archive.get_session_detail(s1)
    s1_mav_fid = next(f.file_id for f in s1_detail.files if f.source == "mavproxy")
    s1_router_fid = next(f.file_id for f in s1_detail.files if f.source == "router")
    assert s1_mav_fid and s1_router_fid
    transport.fail_paths_containing.add(s1_mav_fid)

    logs_root = tmp_path / "client_docs" / "FlightLink" / "Logs"
    sync1 = ArchiveAutoSaveClient(
        base_url="http://test",
        bearer_token=token,
        logs_root=logs_root,
        http_client=http,
        max_auto_attempts=2,
    )
    res1 = sync1.sync_once()
    assert res1.discovered_sessions == 2

    # s2 saved completely (router.log, mavproxy.log, metadata.json) -> NOT blocked by s1!
    st_s2 = sync1.get_session_state(s2)
    assert st_s2 is not None and st_s2["local_status"] == "saved"
    assert st_s2["show_retry_action"] is False
    for fname in ("router.log", "mavproxy.log", "metadata.json"):
        assert (logs_root / "ecs_prod_01" / gid / s2 / fname).exists()

    # s1 failed on mavproxy -> shows failure + 「重新下载日志」 jumping to slice, router & metadata already verified!
    st_s1 = sync1.get_session_state(s1)
    assert st_s1 is not None and st_s1["local_status"] == "failed"
    assert st_s1["show_retry_action"] is True
    assert st_s1["last_error_category"] == "network_error"
    assert st_s1["retry_target_route"] == f"#/history/{gid}?slice={s1}"
    assert (logs_root / "ecs_prod_01" / gid / s1 / "router.log").exists()
    assert (logs_root / "ecs_prod_01" / gid / s1 / "metadata.json").exists()
    assert not (logs_root / "ecs_prod_01" / gid / s1 / "mavproxy.log").exists()

    # Simulate client RESTART + network recovery -> manual retry MUST NOT re-download already verified router/metadata!
    transport.fail_paths_containing.clear()
    transport.request_log.clear()
    sync2 = ArchiveAutoSaveClient(
        base_url="http://test",
        bearer_token=token,
        logs_root=logs_root,
        http_client=http,
        max_auto_attempts=2,
    )
    retry_res = sync2.retry_session(s1)
    assert retry_res.files_downloaded == 1  # ONLY mavproxy.log downloaded!
    assert retry_res.files_skipped_verified == 2  # router.log + metadata.json skipped!
    fetched_paths = [p for _, p in transport.request_log]
    assert all(s1_router_fid not in p for p in fetched_paths)
    assert any(s1_mav_fid in p for p in fetched_paths)

    st_s1_after = sync2.get_session_state(s1)
    assert st_s1_after is not None and st_s1_after["local_status"] == "saved"
    assert st_s1_after["show_retry_action"] is False


# =====================================================================
# Cloud source missing vs client download failure vs disk permission / ENOSPC
# =====================================================================
def test_distinguish_missing_on_server_disk_permission_and_disk_full(tmp_path: Path) -> None:
    _, repo, agent, archive, _, clock, token, transport, http = _setup_stack(tmp_path)
    op = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    gid = op.group_id
    agent.execute_operation(op.operation_id)

    # Session with ONLY router log (cloud itself lacks mavproxy output!)
    s_gap = archive.record_valid_fc_heartbeat(group_id=gid, sysid=1)
    archive.append_log_event(group_id=gid, source="router", level="INFO", text="router only")
    clock.advance(seconds=16)
    archive.check_heartbeat_timeouts()

    logs_root = tmp_path / "logs_gap"
    sync = ArchiveAutoSaveClient(base_url="http://test", bearer_token=token, logs_root=logs_root, http_client=http)
    sync.sync_once()
    st = sync.get_session_state(s_gap)
    assert st is not None
    # MUST be `saved_with_source_gaps` (NOT `saved` and NOT `failed` / NO retry prompt!)
    assert st["local_status"] == "saved_with_source_gaps"
    assert st["show_retry_action"] is False
    assert st["last_error_category"] == "cloud_source_missing"

    # Disk permission error classification
    s_perm = archive.record_valid_fc_heartbeat(group_id=gid, sysid=1)
    archive.append_log_event(group_id=gid, source="router", level="INFO", text="perm test")
    clock.advance(seconds=16)
    archive.check_heartbeat_timeouts()

    def raise_perm(_p: Path, _b: bytes) -> None:
        raise PermissionError(errno.EACCES, "Permission denied")

    sync_perm = ArchiveAutoSaveClient(
        base_url="http://test", bearer_token=token, logs_root=tmp_path / "logs_perm",
        http_client=http, write_hook=raise_perm,
    )
    sync_perm.sync_once()
    st_perm = sync_perm.get_session_state(s_perm)
    assert st_perm is not None and st_perm["local_status"] == "failed"
    assert st_perm["last_error_category"] == "disk_permission_denied"

    # Disk full (ENOSPC) classification
    def raise_enospc(_p: Path, _b: bytes) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    sync_full = ArchiveAutoSaveClient(
        base_url="http://test", bearer_token=token, logs_root=tmp_path / "logs_full",
        http_client=http, write_hook=raise_enospc,
    )
    sync_full.sync_once()
    st_full = sync_full.get_session_state(s_perm)
    assert st_full is not None and st_full["local_status"] == "failed"
    assert st_full["last_error_category"] == "disk_full"


# =====================================================================
# Single-disk quota degradation & active download lease protection
# =====================================================================
def test_disk_quota_degrades_with_gap_and_download_lease_blocks_purge(tmp_path: Path) -> None:
    _, repo, agent, archive, _, clock, _, _, _ = _setup_stack(tmp_path)
    op = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    gid = op.group_id
    agent.execute_operation(op.operation_id)

    s1 = archive.record_valid_fc_heartbeat(group_id=gid, sysid=1)
    archive.append_log_event(group_id=gid, source="router", level="INFO", text="normal line")
    # Simulate disk running below min_free_bytes -> degrade writes & record gap, do NOT delete archives!
    archive.free_bytes_override = 1024
    written = archive.append_log_event(group_id=gid, source="router", level="INFO", text="dropped line")
    assert written is False
    archive.free_bytes_override = None

    clock.advance(seconds=16)
    archive.check_heartbeat_timeouts()
    detail = archive.get_session_detail(s1)
    assert any(g.reason == "disk_quota_exceeded" for g in detail.gaps)

    # Advance to 1 minute before 7-day boundary, start an active download (acquiring a 600s lease),
    # then cross the 7-day boundary while download is still in progress -> MUST NOT purge!
    clock.advance(days=6, hours=23, minutes=59)
    lease = archive.acquire_download_lease(s1, ttl_seconds=600)
    clock.advance(minutes=2)
    assert archive.purge_expired_sessions() == []

    # Release lease -> now purge removes expired session
    archive.release_download_lease(lease)
    assert archive.purge_expired_sessions() == [s1]


# =====================================================================
# FINAL RELEASE ACCEPTANCE SCENARIO + Deleted-Group Retry + 7-Day Boundary
# =====================================================================
def test_final_release_acceptance_scenario_end_to_end(tmp_path: Path) -> None:
    """Covers BOTH the original P5 final release gate AND incremental acceptance #4 & #5:
    1. Create & start "一号机" and sibling "二号机"; produce real FC session on "一号机".
    2. Delete "一号机" while injecting cloud revoke timeout; restart API and agent.
    3. Verify old "一号机" did NOT resurrect, "二号机" never interrupted, old history STILL downloadable/retryable.
    4. Finish cleanup verification; recreate "一号机" with SAME name & SAME ports -> new group_id, logs never mix.
    5. Python sync client restarts without re-downloading verified files.
    6. Advance controllable clock past 7-day boundary -> cloud purges ONLY expired old slice,
       local saved copy remains intact, expired un-downloaded file stops retrying (410),
       and offline client past retention reports retention gap (410 CURSOR_EXPIRED).
    """
    app, repo, agent, archive, fake, clock, token, transport, http = _setup_stack(tmp_path)

    # Step 1: Create & start 一号机 + 二号机
    op1 = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    op2 = repo.enqueue_create_group(
        CreateGroupRequest(name="二号机", dtu_port=5761, gcs_protocol="udp", gcs_port=14553),
        requested_by="admin",
    )
    g1_old, g2 = op1.group_id, op2.group_id
    agent.process_pending_operations()
    repo.enqueue_group_action(g1_old, "start", requested_by="admin")
    repo.enqueue_group_action(g2, "start", requested_by="admin")
    agent.process_pending_operations()
    g2_starts_initial = fake.router_start_counts[g2]

    # Real simulated FC session on 一号机 (active when delete happens!)
    s_old = archive.record_valid_fc_heartbeat(group_id=g1_old, sysid=1)
    archive.append_log_event(group_id=g1_old, source="router", level="INFO", text="old group 1 router log")
    archive.append_log_event(group_id=g1_old, source="mavproxy", level="INFO", text="old group 1 mavproxy log")

    # Also create a second session that will remain un-downloaded until expired to test 410 stop-retry
    clock.advance(seconds=16)
    archive.check_heartbeat_timeouts()
    s_del_active = archive.record_valid_fc_heartbeat(group_id=g1_old, sysid=1)
    archive.append_log_event(group_id=g1_old, source="router", level="INFO", text="active during delete")
    archive.append_log_event(group_id=g1_old, source="mavproxy", level="INFO", text="active during delete mp")

    # Step 2: Delete 一号机 with cloud revoke timeout!
    fake.fail_revoke_timeout_unapplied.add("*")
    op_del = repo.enqueue_delete_group(g1_old, requested_by="admin")
    agent.execute_operation(op_del.operation_id)
    fake.fail_revoke_timeout_unapplied.clear()
    assert repo.get_operation(op_del.operation_id).status == "needs_verification"
    assert repo.get_group_detail(g1_old).lifecycle_state == "cleanup_pending"

    # Restart API & agent!
    app2 = create_app(settings=app.state.container.settings, db=app.state.container.db, clock=clock, archive_service=archive)
    agent2 = LifecycleAgent(
        app2.state.container.db, app2.state.container.settings,
        supervisor=fake, sockets=fake, sg_client=fake, archive_service=archive, clock=clock,
    )
    agent2.reconcile_on_startup(ecs_reboot=False)

    # Step 3: Verify old group did NOT resurrect, 二号机 NEVER restarted, old history downloadable!
    assert not fake.inspect_router(g1_old).active
    assert fake.inspect_router(g2).active
    assert fake.router_start_counts[g2] == g2_starts_initial

    # Finish delete cleanup
    op_del_retry = repo.enqueue_retry_operation(op_del.operation_id, requested_by="admin")
    agent2.execute_operation(op_del_retry.operation_id)
    assert repo.get_operation(op_del_retry.operation_id).status == "succeeded"

    # Verify deleted group: /groups/{id} -> 404, /archive/groups/{id} -> 200, and sync client can download & retry!
    assert http.get(f"/api/v1/groups/{g1_old}", headers={"Authorization": f"Bearer {token}"}).status_code == 404
    assert http.get(f"/api/v1/archive/groups/{g1_old}", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert archive.get_session_detail(s_del_active).close_reason == "admin_group_deleted"

    # Sync client downloads s_old & s_del_active even though g1_old is DELETED (with outage + manual retry!)
    logs_root = tmp_path / "desktop_logs"
    transport.fail_paths_containing.add(s_del_active)
    sync_client = ArchiveAutoSaveClient(base_url="http://test", bearer_token=token, logs_root=logs_root, http_client=http)
    sync_client.sync_once()
    assert sync_client.get_session_state(s_old)["local_status"] == "saved"
    assert sync_client.get_session_state(s_del_active)["local_status"] == "failed"

    # Network recovers -> manual retry on DELETED group's session succeeds!
    transport.fail_paths_containing.clear()
    sync_client.retry_session(s_del_active)
    assert sync_client.get_session_state(s_del_active)["local_status"] == "saved"
    old_local_router = logs_root / "ecs_prod_01" / g1_old / s_old / "router.log"
    assert old_local_router.exists()
    old_local_bytes = old_local_router.read_bytes()

    # Seed a second offline client BEFORE s_new is created: it discovers s_old/s_del_active while file downloads fail,
    # then goes completely offline for > 7 days while s_new is created and s_old/s_del_active expire!
    offline_client = ArchiveAutoSaveClient(
        base_url="http://test", bearer_token=token, logs_root=tmp_path / "offline_client", http_client=http
    )
    transport.fail_paths_containing.add("/files/")
    offline_client.sync_once()
    transport.fail_paths_containing.clear()
    # Pin offline_client cursor to chg_1 (simulating client went offline right after chg_1)
    conn = offline_client._connect()
    conn.execute("UPDATE sync_cursors SET cursor = 'chg_1'")
    conn.close()

    # Step 4: Recreate 一号机 with SAME name & SAME ports -> new group_id, logs NEVER mixed!
    op_new = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    g1_new = op_new.group_id
    assert g1_new != g1_old
    agent2.execute_operation(op_new.operation_id)
    agent2.execute_operation(repo.enqueue_group_action(g1_new, "start", requested_by="admin").operation_id)

    # Advance 2 days so new session is inside 7-day window when old sessions expire
    clock.advance(days=2)
    s_new = archive.record_valid_fc_heartbeat(group_id=g1_new, sysid=1)
    archive.append_log_event(group_id=g1_new, source="router", level="INFO", text="NEW group 1 router log")
    archive.append_log_event(group_id=g1_new, source="mavproxy", level="INFO", text="NEW group 1 mavproxy log")
    clock.advance(seconds=16)
    archive.check_heartbeat_timeouts()

    # Step 5: Restart sync client -> syncs ONLY s_new, does NOT re-download s_old or s_del_active!
    transport.request_log.clear()
    sync_restarted = ArchiveAutoSaveClient(base_url="http://test", bearer_token=token, logs_root=logs_root, http_client=http)
    r_sync2 = sync_restarted.sync_once()
    assert r_sync2.discovered_sessions == 1
    assert r_sync2.files_downloaded == 3  # router, mavproxy, metadata of s_new ONLY
    assert all(s_old not in p for _, p in transport.request_log)
    # Old & new group logs live under distinct immutable group_ids and never mix
    new_local_router = logs_root / "ecs_prod_01" / g1_new / s_new / "router.log"
    assert "NEW group 1 router log" in new_local_router.read_text(encoding="utf-8")
    assert "old group 1" not in new_local_router.read_text(encoding="utf-8")

    # Step 6: Cross 7-day boundary for old sessions (advance 5 days + 1 hour => old = 7d1h expired, new = 5d1h valid!)
    clock.advance(days=5, hours=1)
    purged = archive.purge_expired_sessions()
    assert set(purged) == {s_old, s_del_active}
    # s_new is NOT expired yet -> still present on cloud!
    assert archive.get_session_detail(s_new).session_id == s_new

    # Local saved copy of expired s_old MUST remain completely intact!
    assert old_local_router.exists()
    assert old_local_router.read_bytes() == old_local_bytes

    # Offline client retrying expired s_old receives 410 ARCHIVE_EXPIRED -> marks expired_on_server & stops retrying!
    r_exp = offline_client.retry_session(s_old)
    assert r_exp.files_expired >= 1
    st_exp = offline_client.get_session_state(s_old)
    assert st_exp is not None and st_exp["local_status"] == "expired_on_server"
    assert st_exp["show_retry_action"] is False  # Never loops retrying expired files!

    # Offline client wakes up with stale cursor chg_1 -> gets 410 CURSOR_EXPIRED -> reports gap & reconciles s_new!
    r_reconcile = offline_client.sync_once()
    assert r_reconcile.retention_gap_detected is True
    assert offline_client.get_session_state(s_new)["local_status"] == "saved"
