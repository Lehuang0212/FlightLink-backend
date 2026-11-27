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


def test_cli_init_system_provisioning_and_db_cidr_fallback(tmp_path: Path) -> None:
    """Verify `flightlink init --system` writes flightlink.env, 5 systemd units with UMask=0007,
    narrow Polkit rule, and DB-stored CIDRs work in LifecycleAgent even when env CIDRs are empty.
    """
    runner = CliRunner()
    clock = ManualClock()
    # Explicitly leave settings CIDRs empty to test DB fallback!
    settings = Settings(data_dir=tmp_path / "data", adapter_mode="fake")
    db = Database(settings.resolved_db_path, clock=clock)
    db.init_schema(ecs_instance_id="ecs_prov_01")
    repo = ControlPlaneRepository(db, settings, clock=clock)
    archive = ArchiveService(db, settings, clock=clock)
    fake = FakeInfrastructure(clock=clock)
    agent = LifecycleAgent(
        db, settings, supervisor=fake, sockets=fake, sg_client=fake, archive_service=archive, clock=clock
    )
    ctx = CliContext(settings=settings, db=db, repo=repo, archive=archive, agent=agent, clock=clock)

    env_file = tmp_path / "etc" / "flightlink.env"
    systemd_dir = tmp_path / "systemd"
    polkit_rule = tmp_path / "polkit" / "50-flightlink-agent.rules"

    r_init = runner.invoke(
        cli,
        [
            "init",
            "--system",
            "--env-file", str(env_file),
            "--systemd-dir", str(systemd_dir),
            "--polkit-rule", str(polkit_rule),
            "--admin-user", "ops_admin",
            "--admin-password", "AdminPass!123",
            "--dtu-cidr", "0.0.0.0/0",
            "--gcs-cidr", "0.0.0.0/0",
        ],
        obj=ctx,
    )
    assert r_init.exit_code == 0, r_init.output
    data = json.loads(r_init.output)
    assert data["system_provisioned"] is not None
    assert env_file.exists()
    env_text = env_file.read_text(encoding="utf-8")
    assert "FLIGHTLINK_JWT_SECRET=" in env_text
    assert 'FLIGHTLINK_DTU_ALLOWED_CIDRS=["0.0.0.0/0"]' in env_text
    assert (systemd_dir / "flightlink-api.service").exists()
    api_unit = (systemd_dir / "flightlink-api.service").read_text(encoding="utf-8")
    assert "UMask=0007" in api_unit
    # ExecStart must point to the active venv (sys.prefix), never a dereferenced /usr/bin symlink!
    import sys
    assert str(Path(sys.prefix)) in api_unit
    assert "Restart=no" in (systemd_dir / "flightlink-router@.service").read_text(encoding="utf-8")
    assert polkit_rule.exists()
    assert "flightlink-router@" in polkit_rule.read_text(encoding="utf-8")

    # connect --ram with explicit --ram-role also updates env_file!
    r_conn = runner.invoke(
        cli,
        [
            "connect", "--ram",
            "--region", "cn-beijing",
            "--security-group", "sg-custom-001",
            "--ram-role", "MyCustomRole",
            "--env-file", str(env_file),
        ],
        obj=ctx,
    )
    assert r_conn.exit_code == 0, r_conn.output
    assert json.loads(r_conn.output)["ram_role"] == "MyCustomRole"
    assert "FLIGHTLINK_ALIYUN_RAM_ROLE_NAME=MyCustomRole" in env_file.read_text(encoding="utf-8")

    # Re-running `flightlink init --system` without --admin-user re-renders units while preserving secrets & SG config!
    secret_before = [line for line in env_file.read_text(encoding="utf-8").splitlines() if line.startswith("FLIGHTLINK_JWT_SECRET=")][0]
    r_reinit = runner.invoke(
        cli,
        [
            "init",
            "--system",
            "--env-file", str(env_file),
            "--systemd-dir", str(systemd_dir),
            "--polkit-rule", str(polkit_rule),
        ],
        obj=ctx,
    )
    assert r_reinit.exit_code == 0, r_reinit.output
    reinit_env = env_file.read_text(encoding="utf-8")
    assert secret_before in reinit_env
    assert "FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID=sg-custom-001" in reinit_env
    assert 'FLIGHTLINK_DTU_ALLOWED_CIDRS=["0.0.0.0/0"]' in reinit_env

    # Group create works using DB-stored CIDRs even though Settings had empty CIDRs!
    r_grp = runner.invoke(
        cli,
        ["group", "create", "--name", "一号机", "--dtu-port", "5760", "--gcs", "tcp:14552"],
        obj=ctx,
    )
    assert r_grp.exit_code == 0, r_grp.output
    assert json.loads(r_grp.output)["status"] == "succeeded"


def test_cli_custom_api_port_and_conflict_guard(tmp_path: Path) -> None:
    """Verify `--port` / `--host` on `init` and `run`, env preservation, and API<->group port conflict rejection."""
    runner = CliRunner()
    ctx = _make_cli_ctx(tmp_path / "data")
    env_file = tmp_path / "etc" / "flightlink.env"
    systemd_dir = tmp_path / "systemd"
    polkit_rule = tmp_path / "polkit" / "50-flightlink-agent.rules"

    # 1. init --system --port 18080 persists custom port
    r1 = runner.invoke(
        cli,
        [
            "init", "--system",
            "--port", "18080",
            "--host", "127.0.0.1",
            "--env-file", str(env_file),
            "--systemd-dir", str(systemd_dir),
            "--polkit-rule", str(polkit_rule),
        ],
        obj=ctx,
    )
    assert r1.exit_code == 0, r1.output
    assert json.loads(r1.output)["api_listen"] == "127.0.0.1:18080"
    assert "FLIGHTLINK_API_PORT=18080" in env_file.read_text(encoding="utf-8")

    # 2. Re-running init without --port preserves 18080 (never resets back to 8000)
    r2 = runner.invoke(
        cli,
        [
            "init", "--system",
            "--env-file", str(env_file),
            "--systemd-dir", str(systemd_dir),
            "--polkit-rule", str(polkit_rule),
        ],
        obj=ctx,
    )
    assert r2.exit_code == 0, r2.output
    assert json.loads(r2.output)["api_listen"] == "127.0.0.1:18080"
    assert "FLIGHTLINK_API_PORT=18080" in env_file.read_text(encoding="utf-8")

    # 3. Creating a group using the API port (18080) MUST be rejected with PORT_CONFLICT
    r_conflict_grp = runner.invoke(
        cli,
        ["group", "create", "--name", "撞口机", "--dtu-port", "5760", "--gcs", "tcp:18080"],
        obj=ctx,
    )
    assert r_conflict_grp.exit_code != 0
    assert "18080" in r_conflict_grp.output

    # 4. Create valid group (5760 / 14552); trying `run --port 14552` MUST be rejected
    r_ok_grp = runner.invoke(
        cli,
        ["group", "create", "--name", "正常机", "--dtu-port", "5760", "--gcs", "tcp:14552"],
        obj=ctx,
    )
    assert r_ok_grp.exit_code == 0, r_ok_grp.output

    r_conflict_run = runner.invoke(
        cli,
        ["run", "--port", "14552", "--env-file", str(env_file)],
        obj=ctx,
    )
    assert r_conflict_run.exit_code != 0
    assert "PORT_CONFLICT" in r_conflict_run.output

    # 5. `run --port 19090` updates env_file and shows up in doctor
    r_run = runner.invoke(
        cli,
        ["run", "--port", "19090", "--env-file", str(env_file)],
        obj=ctx,
    )
    assert r_run.exit_code == 0, r_run.output
    run_data = json.loads(r_run.output)
    assert run_data["api_listen"] == "127.0.0.1:19090"
    assert run_data["env_updated"] is True
    assert "FLIGHTLINK_API_PORT=19090" in env_file.read_text(encoding="utf-8")

    r_doc = runner.invoke(cli, ["doctor", "--env-file", str(env_file)], obj=ctx)
    assert r_doc.exit_code == 0, r_doc.output
    assert json.loads(r_doc.output)["api_listen"] == "127.0.0.1:19090"

    # 6. `run --port` on an uninitialized/cleared linux host gives a clean Error (no traceback, no half-baked env)
    uninit_env = tmp_path / "uninit" / "flightlink.env"
    uninit_systemd = tmp_path / "uninit" / "systemd"
    ctx.settings.adapter_mode = "linux"
    r_uninit = runner.invoke(
        cli,
        ["run", "--port", "18080", "--env-file", str(uninit_env), "--systemd-dir", str(uninit_systemd)],
        obj=ctx,
    )
    assert r_uninit.exit_code != 0
    assert "flightlink init" in r_uninit.output
    assert "--port 18080" in r_uninit.output
    assert "Traceback" not in r_uninit.output
    assert not uninit_env.exists()


def test_cli_clear_legacy_runtime_and_all_modes(tmp_path: Path) -> None:
    """Verify `flightlink clear` safely cleans v0.1 remnants (default), 1.0 orphaned runtime files,
    and requires `--yes` for `--all` while preserving archives & manual cloud SG rules by default.
    """
    runner = CliRunner()
    ctx = _make_cli_ctx(tmp_path / "data")
    root_prefix = tmp_path / "root"
    env_file = tmp_path / "etc" / "flightlink.env"
    systemd_dir = tmp_path / "systemd"
    polkit_rule = tmp_path / "polkit" / "50-flightlink-agent.rules"

    # Provision 1.0 host files + connect SG + create active group + seed archive + manual SG rule
    assert runner.invoke(
        cli,
        [
            "init", "--system",
            "--env-file", str(env_file),
            "--systemd-dir", str(systemd_dir),
            "--polkit-rule", str(polkit_rule),
        ],
        obj=ctx,
    ).exit_code == 0
    assert runner.invoke(
        cli,
        ["connect", "--ram", "--region", "cn-beijing", "--security-group", "sg-clear-01", "--env-file", str(env_file)],
        obj=ctx,
    ).exit_code == 0
    ctx.agent.sg_client.authorize_rule(
        security_group_id="sg-clear-01",
        protocol="tcp",
        port=22,
        source_cidr="10.0.0.0/8",
        description="manual-ssh-keep",
    )
    r_grp = runner.invoke(
        cli,
        ["group", "create", "--name", "留存机", "--dtu-port", "5760", "--gcs", "tcp:14552"],
        obj=ctx,
    )
    assert r_grp.exit_code == 0
    active_gid = json.loads(r_grp.output)["group_id"]

    # Seed archive file to verify preservation
    arch_file = ctx.settings.resolved_archive_dir / active_gid / "sess_keep" / "router.log"
    arch_file.parent.mkdir(parents=True, exist_ok=True)
    arch_file.write_text("precious flight log\n", encoding="utf-8")

    # Plant legacy v0.1 remnants
    legacy_capture = systemd_dir / "flightlink-capture@.service"
    legacy_dropin = systemd_dir / "flightlink-api.service.d" / "service-control.conf"
    legacy_routerctl = root_prefix / "usr/local/sbin/flightlink-routerctl"
    legacy_sudoers = root_prefix / "etc/sudoers.d/flightlink-router-control"
    legacy_api_env = env_file.parent / "api.env"
    for f in (legacy_capture, legacy_dropin, legacy_routerctl, legacy_sudoers, legacy_api_env):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("legacy", encoding="utf-8")

    # 1. doctor detects legacy remnants read-only
    doc = json.loads(
        runner.invoke(
            cli,
            ["doctor", "--env-file", str(env_file), "--systemd-dir", str(systemd_dir), "--root-prefix", str(root_prefix)],
            obj=ctx,
        ).output
    )
    assert doc["legacy_remnants"]["detected"] is True
    assert str(legacy_capture) in doc["legacy_remnants"]["paths"]

    # 2. `clear --dry-run` previews without deleting anything
    dry = json.loads(
        runner.invoke(
            cli,
            ["clear", "--dry-run", "--env-file", str(env_file), "--systemd-dir", str(systemd_dir), "--polkit-rule", str(polkit_rule), "--root-prefix", str(root_prefix)],
            obj=ctx,
        ).output
    )
    assert dry["dry_run"] is True and dry["mode"] == "legacy"
    assert legacy_capture.exists() and legacy_routerctl.exists()

    # 3. Default `clear` removes core legacy remnants only, keeping 1.0 units/env/DB/archive and noting api.env
    res_legacy = json.loads(
        runner.invoke(
            cli,
            ["clear", "--env-file", str(env_file), "--systemd-dir", str(systemd_dir), "--polkit-rule", str(polkit_rule), "--root-prefix", str(root_prefix)],
            obj=ctx,
        ).output
    )
    assert res_legacy["mode"] == "legacy" and res_legacy["archives_preserved"] is True
    assert not legacy_capture.exists()
    assert not legacy_dropin.parent.exists()
    assert not legacy_routerctl.exists()
    assert not legacy_sudoers.exists()
    assert legacy_api_env.exists()  # kept unless --include-legacy-app
    assert (systemd_dir / "flightlink-api.service").exists()
    assert env_file.exists() and arch_file.exists()

    # 4. `clear --runtime` removes orphaned router.d/run artifacts without touching active group
    cand_conf = ctx.settings.resolved_router_config_dir / f"{active_gid}.candidate.conf"
    orphan_conf = ctx.settings.resolved_router_config_dir / "grp_orphan.conf"
    orphan_env = ctx.settings.resolved_runtime_dir / "mavproxy-grp_orphan.env"
    active_env = ctx.settings.resolved_runtime_dir / f"mavproxy-{active_gid}.env"
    for f in (cand_conf, orphan_conf, orphan_env, active_env):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("tmp", encoding="utf-8")

    res_rt = json.loads(
        runner.invoke(
            cli,
            ["clear", "--runtime", "--env-file", str(env_file), "--systemd-dir", str(systemd_dir), "--polkit-rule", str(polkit_rule), "--root-prefix", str(root_prefix)],
            obj=ctx,
        ).output
    )
    assert res_rt["mode"] == "runtime"
    assert not cand_conf.exists() and not orphan_conf.exists() and not orphan_env.exists()
    assert active_env.exists() and (ctx.settings.resolved_router_config_dir / f"{active_gid}.conf").exists()

    # 5. `clear --all` without `--yes` MUST fail
    r_no_yes = runner.invoke(
        cli,
        ["clear", "--all", "--env-file", str(env_file), "--systemd-dir", str(systemd_dir), "--polkit-rule", str(polkit_rule), "--root-prefix", str(root_prefix)],
        obj=ctx,
    )
    assert r_no_yes.exit_code != 0
    assert "--yes" in r_no_yes.output

    # 6. `clear --all --yes` revokes ONLY owned cloud SG rules, removes 1.0 files, preserves archive & manual SSH rule!
    res_all = json.loads(
        runner.invoke(
            cli,
            ["clear", "--all", "--yes", "--env-file", str(env_file), "--systemd-dir", str(systemd_dir), "--polkit-rule", str(polkit_rule), "--root-prefix", str(root_prefix)],
            obj=ctx,
        ).output
    )
    assert res_all["mode"] == "all" and res_all["archives_preserved"] is True
    assert len(res_all["cloud_rules_revoked"]) == 2
    assert res_all["cloud_manual_preserved"] == 1
    remaining_rules = ctx.agent.sg_client.list_rules(security_group_id="sg-clear-01")
    assert [r.description for r in remaining_rules] == ["manual-ssh-keep"]
    assert not env_file.exists()
    assert not (systemd_dir / "flightlink-api.service").exists()
    assert not polkit_rule.exists()
    assert arch_file.exists()  # Archives preserved by default!

    # 7. `clear --all --yes --purge-archives` removes archives when explicitly requested
    res_purge = json.loads(
        runner.invoke(
            cli,
            ["clear", "--all", "--yes", "--purge-archives", "--skip-cloud", "--env-file", str(env_file), "--systemd-dir", str(systemd_dir), "--polkit-rule", str(polkit_rule), "--root-prefix", str(root_prefix)],
            obj=ctx,
        ).output
    )
    assert res_purge["archives_preserved"] is False
    assert not arch_file.exists()
