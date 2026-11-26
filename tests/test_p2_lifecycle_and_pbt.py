"""P2 Channel Lifecycle, Crash Recovery, Deterministic Fault Injection, and Hypothesis PBT suite."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal

from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

from flightlink.adapters.interfaces import FakeInfrastructure
from flightlink.archive.service import ArchiveService
from flightlink.clock import ManualClock
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.errors import ConflictError, DomainError
from flightlink.domain.lifecycle import LifecycleAgent
from flightlink.domain.operations_repo import ControlPlaneRepository
from flightlink.schemas.groups import CreateGroupRequest, UpdateGroupRequest


def _env(tmp_path: Path):
    clock = ManualClock()
    settings = Settings(
        data_dir=tmp_path,
        aliyun_region="cn-beijing",
        aliyun_security_group_id="sg-p2-test",
        dtu_allowed_cidrs=["0.0.0.0/0"],
        gcs_allowed_cidrs=["0.0.0.0/0"],
        adapter_mode="fake",
    )
    db = Database(settings.resolved_db_path, clock=clock)
    db.init_schema(ecs_instance_id="ecs_p2")
    repo = ControlPlaneRepository(db, settings, clock=clock)
    archive = ArchiveService(db, settings, clock=clock)
    fake = FakeInfrastructure(clock=clock)
    agent = LifecycleAgent(
        db, settings, supervisor=fake, sockets=fake, sg_client=fake, archive_service=archive, clock=clock
    )
    return db, repo, agent, archive, fake, clock


# =====================================================================
# 1. Hypothesis Stateful Property-Based Testing (P2 core invariants)
# =====================================================================
class ChannelLifecycleStateMachine(RuleBasedStateMachine):
    """Drives arbitrary sequences of create/start/stop/rename/rebind/delete/fault/restart
    and asserts all P2 safety invariants after EVERY single step.
    """

    groups = Bundle("groups")

    def __init__(self) -> None:
        super().__init__()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="fl-pbt-")
        self.db, self.repo, self.agent, self.archive, self.fake, self.clock = _env(Path(self._tmpdir.name))
        self.seen_group_ids: set[str] = set()
        self.deleted_group_ids: set[str] = set()
        self.name_seq = 0

    def teardown(self) -> None:
        self._tmpdir.cleanup()

    @rule(
        target=groups,
        dtu_port=st.integers(min_value=5760, max_value=5775),
        gcs_port=st.integers(min_value=14550, max_value=14565),
        gcs_proto=st.sampled_from(["tcp", "udp"]),
        cloud_timeout=st.booleans(),
    )
    def create_group(
        self,
        dtu_port: int,
        gcs_port: int,
        gcs_proto: Literal["tcp", "udp"],
        cloud_timeout: bool,
    ) -> str:
        self.name_seq += 1
        name = f"机-{self.name_seq}"
        if cloud_timeout:
            self.fake.fail_authorize_timeout_unapplied.add(("udp", dtu_port))
        try:
            op = self.repo.enqueue_create_group(
                CreateGroupRequest(name=name, dtu_port=dtu_port, gcs_protocol=gcs_proto, gcs_port=gcs_port),
                requested_by="pbt",
            )
        except (DomainError, ValueError):
            self.fake.fail_authorize_timeout_unapplied.clear()
            return ""
        # Every new group MUST get a brand-new unique group_id (never reusing deleted IDs)
        assert op.group_id not in self.seen_group_ids
        self.seen_group_ids.add(op.group_id)
        self.agent.execute_operation(op.operation_id)
        self.fake.fail_authorize_timeout_unapplied.clear()
        return op.group_id

    @rule(gid=groups)
    def start_group(self, gid: str) -> None:
        if not gid:
            return
        try:
            op = self.repo.enqueue_group_action(gid, "start", requested_by="pbt")
        except DomainError:
            return
        self.agent.execute_operation(op.operation_id)

    @rule(gid=groups)
    def stop_group(self, gid: str) -> None:
        if not gid:
            return
        try:
            op = self.repo.enqueue_group_action(gid, "stop", requested_by="pbt")
        except DomainError:
            return
        self.agent.execute_operation(op.operation_id)

    @rule(
        gid=groups,
        new_dtu=st.integers(min_value=5760, max_value=5775),
        new_gcs=st.integers(min_value=14550, max_value=14565),
        new_proto=st.sampled_from(["tcp", "udp"]),
        swap_own_ports=st.booleans(),
    )
    def update_group(
        self,
        gid: str,
        new_dtu: int,
        new_gcs: int,
        new_proto: Literal["tcp", "udp"],
        swap_own_ports: bool,
    ) -> None:
        if not gid:
            return
        try:
            detail = self.repo.get_group_detail(gid)
        except DomainError:
            return
        was_stopped = detail.lifecycle_state == "configured_stopped"
        if swap_own_ports:
            new_dtu, new_gcs = detail.gcs_port, detail.dtu_port
        try:
            op = self.repo.enqueue_update_group(
                gid,
                UpdateGroupRequest(
                    dtu_port=new_dtu,
                    gcs_protocol=new_proto,
                    gcs_port=new_gcs,
                    expected_config_version=detail.config_version,
                ),
                requested_by="pbt",
            )
        except (DomainError, ValueError):
            return
        self.agent.execute_operation(op.operation_id)
        # Invariant specific to stopped-group port update: NEVER spawns listening socket!
        if was_stopped:
            assert not self.fake.inspect_router(gid).active

    @rule(gid=groups, timeout_revoke=st.booleans())
    def delete_group(self, gid: str, timeout_revoke: bool) -> None:
        if not gid:
            return
        if timeout_revoke:
            self.fake.fail_revoke_timeout_unapplied.add("*")
        try:
            op = self.repo.enqueue_delete_group(gid, requested_by="pbt")
        except DomainError:
            self.fake.fail_revoke_timeout_unapplied.clear()
            return
        self.agent.execute_operation(op.operation_id)
        self.fake.fail_revoke_timeout_unapplied.clear()
        final_op = self.repo.get_operation(op.operation_id)
        if final_op.status == "succeeded":
            self.deleted_group_ids.add(gid)

    @rule(ecs_reboot=st.booleans())
    def restart_agent(self, ecs_reboot: bool) -> None:
        if ecs_reboot:
            # Simulate host reboot clearing all running processes
            for gid in list(self.fake.list_running_router_groups()):
                self.fake.stop_router(gid)
                self.fake.stop_mavproxy(gid)
        self.agent.reconcile_on_startup(ecs_reboot=ecs_reboot)

    @invariant()
    def no_two_groups_share_business_port(self) -> None:
        with self.db.read() as conn:
            rows = conn.execute("SELECT port, group_id FROM port_reservations").fetchall()
        port_owner: dict[int, str] = {}
        for r in rows:
            p = int(r["port"])
            gid = str(r["group_id"])
            if p in port_owner:
                assert port_owner[p] == gid, f"Port {p} double-owned by {port_owner[p]} and {gid}"
            port_owner[p] = gid

        # Also check live listening sockets
        sock_owners: dict[int, str] = {}
        for s in self.fake.list_listening_sockets():
            if s.group_id is None:
                continue
            if s.port in sock_owners:
                assert sock_owners[s.port] == s.group_id, f"Socket port {s.port} double-bound!"
            sock_owners[s.port] = s.group_id

    @invariant()
    def deleted_groups_never_resurrect_and_max_five(self) -> None:
        with self.db.read() as conn:
            active_rows = conn.execute(
                "SELECT group_id, lifecycle_state, desired_state FROM port_groups WHERE deleted_at IS NULL"
            ).fetchall()
        assert len(active_rows) <= 5
        active_map = {str(r["group_id"]): r for r in active_rows}
        for dgid in self.deleted_group_ids:
            assert dgid not in active_map, f"Deleted group {dgid} resurrected!"
            assert not self.fake.inspect_router(dgid).active, f"Deleted group {dgid} router still active!"

        for gid, row in active_map.items():
            if row["lifecycle_state"] in ("configured_stopped", "deleting", "cleanup_pending"):
                assert not self.fake.inspect_router(gid).active, f"Group {gid} in {row['lifecycle_state']} has active router!"


TestChannelLifecyclePBT = ChannelLifecycleStateMachine.TestCase
TestChannelLifecyclePBT.settings = hyp_settings(max_examples=30, stateful_step_count=20, deadline=None)


# =====================================================================
# 2. Deterministic Edge-Case & Fault-Injection Tests (P2 Checklist)
# =====================================================================
def test_same_port_protocol_switch_and_port_swap_within_group(tmp_path: Path) -> None:
    _, repo, agent, _, fake, _ = _env(tmp_path)
    op = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    gid = op.group_id
    agent.execute_operation(op.operation_id)
    op_start = repo.enqueue_group_action(gid, "start", requested_by="admin")
    agent.execute_operation(op_start.operation_id)

    # 1. Same port, switch protocol tcp:14552 -> udp:14552
    op_proto = repo.enqueue_update_group(
        gid,
        UpdateGroupRequest(gcs_protocol="udp", gcs_port=14552, expected_config_version=1),
        requested_by="admin",
    )
    agent.execute_operation(op_proto.operation_id)
    assert repo.get_operation(op_proto.operation_id).status == "succeeded"
    det = repo.get_group_detail(gid)
    assert det.gcs_protocol == "udp"
    assert det.gcs_port == 14552
    assert det.config_version == 2
    assert ("udp", 14552) in {(s.protocol, s.port) for s in fake.list_listening_sockets() if s.group_id == gid}

    # 2. Swap dtu_port and gcs_port within the same group (5760 <-> 14552)
    op_swap = repo.enqueue_update_group(
        gid,
        UpdateGroupRequest(dtu_port=14552, gcs_port=5760, expected_config_version=2),
        requested_by="admin",
    )
    agent.execute_operation(op_swap.operation_id)
    assert repo.get_operation(op_swap.operation_id).status == "succeeded"
    det2 = repo.get_group_detail(gid)
    assert det2.dtu_port == 14552
    assert det2.gcs_port == 5760
    assert det2.lifecycle_state == "running"


def test_rename_does_not_restart_and_sibling_isolated_on_failure(tmp_path: Path) -> None:
    _, repo, agent, _, fake, _ = _env(tmp_path)
    op1 = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    op2 = repo.enqueue_create_group(
        CreateGroupRequest(name="二号机", dtu_port=5761, gcs_protocol="udp", gcs_port=14553),
        requested_by="admin",
    )
    agent.process_pending_operations()
    g1, g2 = op1.group_id, op2.group_id

    for g in (g1, g2):
        repo.enqueue_group_action(g, "start", requested_by="admin")
    agent.process_pending_operations()
    g1_starts_before = fake.router_start_counts[g1]
    g2_starts_before = fake.router_start_counts[g2]

    # Rename g1 -> ZERO restart!
    op_ren = repo.enqueue_update_group(g1, UpdateGroupRequest(name="一号机-改名"), requested_by="admin")
    agent.execute_operation(op_ren.operation_id)
    assert fake.router_start_counts[g1] == g1_starts_before

    # External process preempts candidate port 14599 -> g1 update fails & recovers old config; g2 NEVER restarts!
    fake.add_external_socket("tcp", 14599, process_name="other-system-daemon", pid=7777)
    op_bad = repo.enqueue_update_group(g1, UpdateGroupRequest(gcs_port=14599), requested_by="admin")
    agent.execute_operation(op_bad.operation_id)
    assert repo.get_operation(op_bad.operation_id).status == "failed"
    assert repo.get_operation(op_bad.operation_id).error_code == "PORT_PREEMPTED"
    assert repo.get_group_detail(g1).gcs_port == 14552
    assert repo.get_group_detail(g1).lifecycle_state == "running"
    assert fake.router_start_counts[g2] == g2_starts_before  # Sibling completely untouched!


def test_running_update_new_link_ok_but_old_sg_revoke_fails_does_not_rollback(tmp_path: Path) -> None:
    _, repo, agent, _, fake, _ = _env(tmp_path)
    op = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    gid = op.group_id
    agent.execute_operation(op.operation_id)
    agent.execute_operation(repo.enqueue_group_action(gid, "start", requested_by="admin").operation_id)

    # Inject revoke timeout on old rules when switching port 14552 -> 14554
    fake.fail_revoke_timeout_unapplied.add("*")
    op_upd = repo.enqueue_update_group(gid, UpdateGroupRequest(gcs_port=14554), requested_by="admin")
    agent.execute_operation(op_upd.operation_id)
    fake.fail_revoke_timeout_unapplied.clear()

    res = repo.get_operation(op_upd.operation_id)
    assert res.status == "needs_verification"
    assert res.retryable is True
    # New link MUST stay committed & running (NO rollback!)
    det = repo.get_group_detail(gid)
    assert det.gcs_port == 14554
    assert det.lifecycle_state == "running"
    assert det.router.verified_listening is True

    # Retry ONLY cleans up old SG rule & releases old port 14552
    op_retry = repo.enqueue_retry_operation(op_upd.operation_id, requested_by="admin")
    agent.execute_operation(op_retry.operation_id)
    assert repo.get_operation(op_retry.operation_id).status == "succeeded"
    assert repo.get_group_detail(gid).security_group.state == "synced"


def test_stale_start_fenced_by_delete_and_cleanup_pending_cannot_resurrect(tmp_path: Path) -> None:
    _, repo, agent, _, fake, _ = _env(tmp_path)
    op_c = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    gid = op_c.group_id
    agent.execute_operation(op_c.operation_id)

    # Enqueue start, THEN enqueue delete BEFORE start executes!
    op_start = repo.enqueue_group_action(gid, "start", requested_by="admin")
    fake.fail_revoke_timeout_unapplied.add("*")
    op_del = repo.enqueue_delete_group(gid, requested_by="admin")

    # Old start task MUST be cancelled and MUST NOT start router!
    agent.execute_operation(op_start.operation_id)
    assert repo.get_operation(op_start.operation_id).status == "cancelled"
    assert not fake.inspect_router(gid).active

    # Delete enters cleanup_pending due to cloud revoke timeout
    agent.execute_operation(op_del.operation_id)
    fake.fail_revoke_timeout_unapplied.clear()
    assert repo.get_group_detail(gid).lifecycle_state == "cleanup_pending"

    # Trying to start a cleanup_pending group MUST fail with 409 GROUP_DELETING!
    try:
        repo.enqueue_group_action(gid, "start", requested_by="admin")
        assert False, "Expected ConflictError"
    except ConflictError as exc:
        assert exc.code == "GROUP_DELETING"

    # Port 5760 is STILL reserved until cleanup completes (another group cannot steal it!)
    try:
        repo.enqueue_create_group(
            CreateGroupRequest(name="抢端口", dtu_port=5760, gcs_protocol="tcp", gcs_port=14560),
            requested_by="admin",
        )
        assert False, "Expected PORT_CONFLICT while cleanup_pending"
    except ConflictError as exc:
        assert exc.code == "PORT_CONFLICT"

    # Retry delete completes cleanup and releases ports -> recreate gets NEW group_id!
    op_retry = repo.enqueue_retry_operation(op_del.operation_id, requested_by="admin")
    agent.execute_operation(op_retry.operation_id)
    assert repo.get_operation(op_retry.operation_id).status == "succeeded"

    op_recreate = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    assert op_recreate.group_id != gid


def test_ecs_reboot_does_not_auto_start_groups(tmp_path: Path) -> None:
    _, repo, agent, _, fake, _ = _env(tmp_path)
    op = repo.enqueue_create_group(
        CreateGroupRequest(name="一号机", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552),
        requested_by="admin",
    )
    gid = op.group_id
    agent.execute_operation(op.operation_id)
    agent.execute_operation(repo.enqueue_group_action(gid, "start", requested_by="admin").operation_id)
    assert repo.get_group_detail(gid).lifecycle_state == "running"

    # Simulate host reboot (all processes gone)
    fake.stop_router(gid)
    fake.stop_mavproxy(gid)
    agent.reconcile_on_startup(ecs_reboot=True)
    assert not fake.inspect_router(gid).active
    assert repo.get_group_detail(gid).lifecycle_state == "configured_stopped"
    assert repo.get_group_detail(gid).desired_state == "stopped"
