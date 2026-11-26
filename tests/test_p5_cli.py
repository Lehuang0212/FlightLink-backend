"""P5 CLI end-to-end tests covering all required `flightlink` subcommands."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from click.testing import CliRunner

from flightlink.adapters.interfaces import FakeInfrastructure
from flightlink.archive.service import ArchiveService
from flightlink.cli.main import CliContext, cli
from flightlink.clock import ManualClock
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.lifecycle import LifecycleAgent
from flightlink.domain.operations_repo import ControlPlaneRepository


def _make_cli_ctx(tmp_path: Path) -> CliContext:
    clock = ManualClock()
    settings = Settings(
        data_dir=tmp_path,
        dtu_allowed_cidrs=["0.0.0.0/0"],
        gcs_allowed_cidrs=["0.0.0.0/0"],
        adapter_mode="fake",
    )
    db = Database(settings.resolved_db_path, clock=clock)
    db.init_schema(ecs_instance_id="ecs_cli_01")
    repo = ControlPlaneRepository(db, settings, clock=clock)
    archive = ArchiveService(db, settings, clock=clock)
    fake = FakeInfrastructure(clock=clock)
    agent = LifecycleAgent(
        db, settings, supervisor=fake, sockets=fake, sg_client=fake, archive_service=archive, clock=clock
    )
    return CliContext(settings=settings, db=db, repo=repo, archive=archive, agent=agent, clock=clock)


def test_full_cli_command_surface(tmp_path: Path) -> None:
    runner = CliRunner()
    ctx = _make_cli_ctx(tmp_path)

    def invoke(*args: str):
        res = runner.invoke(cli, list(args), obj=ctx)
        assert res.exit_code == 0, f"Command {args} failed ({res.exit_code}): {res.output}"
        return res

    # init & connect --ram & doctor & run & status
    r_init = json.loads(invoke("init", "--admin-user", "ops_admin", "--admin-password", "AdminPass!123").output)
    assert r_init["status"] == "initialized"

    r_conn = json.loads(
        invoke("connect", "--ram", "--region", "cn-beijing", "--security-group", "sg-2ze93ogksv8g4i7tc8vd").output
    )
    assert r_conn["status"] == "connected"

    r_doc = json.loads(invoke("doctor").output)
    assert r_doc["read_only"] is True
    assert r_doc["db_ok"] is True

    r_run = json.loads(invoke("run").output)
    assert r_run["aircraft_channels_auto_started"] is False

    # group create -> start -> update --dry-run -> update -> firewall diff/sync -> op show -> logs -> history
    op_create = json.loads(
        invoke("group", "create", "--name", "一号机", "--dtu-port", "5760", "--gcs", "tcp:14552").output
    )
    assert op_create["status"] == "succeeded"
    gid = op_create["group_id"]

    op_start = json.loads(invoke("group", "start", gid).output)
    assert op_start["status"] == "succeeded"

    dry = json.loads(invoke("group", "update", gid, "--gcs", "udp:14554", "--dry-run").output)
    assert dry["valid"] is True and dry["requires_restart"] is True

    op_upd = json.loads(invoke("group", "update", gid, "--gcs", "udp:14554").output)
    assert op_upd["status"] == "succeeded"

    fw_diff = json.loads(invoke("firewall", "diff").output)
    assert fw_diff["in_sync"] is True
    json.loads(invoke("firewall", "sync", gid).output)

    op_shown = json.loads(invoke("op", "show", op_upd["operation_id"]).output)
    assert op_shown["operation_id"] == op_upd["operation_id"]

    # Record session logs & verify logs + history list + history export
    sid = ctx.archive.record_valid_fc_heartbeat(group_id=gid, sysid=1)
    ctx.archive.append_log_event(group_id=gid, source="router", level="INFO", text="cli router log line")
    ctx.archive.append_log_event(group_id=gid, source="mavproxy", level="INFO", text="cli mavproxy log line")
    assert "cli router log line" in invoke("logs", gid).output

    # Delete group --wait (seals session, preserves archive)
    op_del = json.loads(invoke("group", "delete", gid, "--wait").output)
    assert op_del["status"] == "succeeded"

    hist = json.loads(invoke("history", "list", "--include-deleted-groups").output)
    assert any(item["group_id"] == gid and item["deleted"] is True for item in hist["items"])

    export_tgz = tmp_path / "exported.tar.gz"
    exp = json.loads(invoke("history", "export", sid, "--source", "all", "-o", str(export_tgz)).output)
    assert export_tgz.exists()
    with tarfile.open(export_tgz, "r:gz") as tar:
        names = tar.getnames()
        assert any(n.endswith("router.log") for n in names)
        assert any(n.endswith("mavproxy.log") for n in names)
        assert any(n.endswith("metadata.json") for n in names)

    # stop stops services but NEVER deletes archives!
    stop_res = json.loads(invoke("stop").output)
    assert stop_res["archives_preserved"] is True
    assert ctx.archive.get_session_detail(sid).session_id == sid
