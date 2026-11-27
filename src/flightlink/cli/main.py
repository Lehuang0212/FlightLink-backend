"""FlightLink ECS 1.0 CLI sharing the exact same domain operations & audit trail as the Web API.

Required command surface:
- flightlink init
- flightlink connect --ram --region <region> --security-group <sg_id>
- flightlink doctor (strictly read-only)
- flightlink run | stop | status
- flightlink group create --name 一号机 --dtu-port 5760 --gcs tcp:14552
- flightlink group start|stop|restart <group_id>
- flightlink group update <group_id> --gcs udp:14554 [--dry-run]
- flightlink group delete <group_id> [--wait]
- flightlink firewall diff | sync <group_id>
- flightlink op show|retry <operation_id>
- flightlink logs <group_id> [--follow]
- flightlink history list [--include-deleted-groups]
- flightlink history export <session_id> --source router|mavproxy|metadata|all
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import click

from flightlink.adapters.aliyun_sg import AliyunRamSecurityGroupClient
from flightlink.adapters.interfaces import FakeInfrastructure
from flightlink.adapters.linux_systemd import LinuxSocketInspector, LinuxSystemdSupervisor
from flightlink.archive.service import ArchiveService
from flightlink.archive.storage import build_session_bundle_tgz
from flightlink.cleanup import clear_environment, scan_legacy_remnants
from flightlink.clock import Clock, SystemClock
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.errors import DomainError
from flightlink.domain.legacy_import import import_legacy_v01_readonly
from flightlink.domain.lifecycle import LifecycleAgent
from flightlink.domain.operations_repo import ControlPlaneRepository
from flightlink.provisioning import (
    DEFAULT_ENV_PATH,
    POLKIT_RULE_PATH,
    SYSTEMD_DIR,
    fix_linux_db_permissions,
    is_linux_root,
    parse_env_file,
    provision_linux_host,
    write_env_file,
)
from flightlink.schemas.groups import CreateGroupRequest, UpdateGroupRequest


@dataclass
class CliContext:
    settings: Settings
    db: Database
    repo: ControlPlaneRepository
    archive: ArchiveService
    agent: LifecycleAgent
    clock: Clock
    actor: str = "cli-admin"


def parse_gcs_spec(spec: str) -> tuple[Literal["tcp", "udp"], int]:
    if ":" not in spec:
        raise click.BadParameter("Expected format 'tcp:<port>' or 'udp:<port>', e.g. tcp:14552")
    proto_raw, port_raw = spec.split(":", 1)
    proto = proto_raw.strip().lower()
    if proto not in ("tcp", "udp"):
        raise click.BadParameter("GCS protocol must be 'tcp' or 'udp'")
    if not port_raw.isdigit() or not (1 <= int(port_raw) <= 65535):
        raise click.BadParameter("GCS port must be an integer between 1 and 65535")
    return proto, int(port_raw)  # type: ignore[return-value]


def _make_context(
    *,
    data_dir: Path | None = None,
    adapter_mode: str | None = None,
    injected_ctx: CliContext | None = None,
    init_db: bool = True,
) -> CliContext:
    if injected_ctx is not None:
        return injected_ctx
    kwargs: dict[str, Any] = {}
    if data_dir is not None:
        kwargs["data_dir"] = data_dir
    if adapter_mode is not None:
        kwargs["adapter_mode"] = adapter_mode
    settings = Settings(**kwargs)
    clock = SystemClock()
    db = Database(settings.resolved_db_path, clock=clock)
    if init_db:
        db.init_schema(ecs_instance_id=settings.ecs_instance_id)
    repo = ControlPlaneRepository(db, settings, clock=clock)
    archive = ArchiveService(db, settings, clock=clock)
    if settings.adapter_mode == "fake":
        fake = FakeInfrastructure(clock=clock)
        agent = LifecycleAgent(db, settings, supervisor=fake, sockets=fake, sg_client=fake, archive_service=archive, clock=clock)
    else:
        sup = LinuxSystemdSupervisor(runtime_dir=settings.resolved_runtime_dir)
        socks = LinuxSocketInspector(sup)
        region = (db.get_meta("aliyun_region") if init_db else None) or settings.aliyun_region or "cn-beijing"
        role = (db.get_meta("aliyun_ram_role_name") if init_db else None) or settings.aliyun_ram_role_name
        sg = AliyunRamSecurityGroupClient(region=region, ram_role_name=role, clock=clock)
        agent = LifecycleAgent(db, settings, supervisor=sup, sockets=socks, sg_client=sg, archive_service=archive, clock=clock)
    return CliContext(settings=settings, db=db, repo=repo, archive=archive, agent=agent, clock=clock)


def _execute_or_wait(c: CliContext, operation_id: str, *, timeout_seconds: float = 30.0) -> Any:
    """Execute pending operation locally, or wait if `flightlink-agent.service` already claimed it."""
    c.agent.execute_operation(operation_id)
    fix_linux_db_permissions(c.settings.data_dir)
    deadline = time.monotonic() + timeout_seconds
    while True:
        op = c.repo.get_operation(operation_id)
        if op.status not in ("pending", "running") or time.monotonic() >= deadline:
            return op
        time.sleep(0.2)


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@click.group(help="FlightLink ECS 1.0 unified management CLI.")
@click.option("--data-dir", type=click.Path(path_type=Path), default=None, help="Override FlightLink data directory.")
@click.option("--adapter-mode", type=click.Choice(["linux", "fake"]), default=None, hidden=True)
@click.pass_context
def cli(ctx: click.Context, data_dir: Path | None, adapter_mode: str | None) -> None:
    injected = ctx.obj if isinstance(ctx.obj, CliContext) else None
    init_db = True
    if ctx.invoked_subcommand == "clear" and injected is None:
        probe_settings = Settings(data_dir=data_dir) if data_dir is not None else Settings()
        init_db = probe_settings.resolved_db_path.exists()
    ctx.obj = _make_context(data_dir=data_dir, adapter_mode=adapter_mode, injected_ctx=injected, init_db=init_db)


def _ensure_api_port_not_reserved(c: CliContext, port: int) -> None:
    """Reject setting API listen port to a port already reserved by a port group."""
    with c.db.read() as conn:
        row = conn.execute(
            """
            SELECT pr.group_id, pg.name, pr.role, pr.protocol
            FROM port_reservations pr
            LEFT JOIN port_groups pg ON pg.group_id = pr.group_id
            WHERE pr.port = ?
            LIMIT 1
            """,
            (port,),
        ).fetchone()
    if row is not None:
        owner = row["name"] or row["group_id"]
        raise click.ClickException(
            f"PORT_CONFLICT: API port {port} conflicts with reserved {row['role']}/{row['protocol']} port of group '{owner}'."
        )


def _resolve_api_listen(c: CliContext, env_file: Path = DEFAULT_ENV_PATH) -> tuple[str, int]:
    env_vals = parse_env_file(env_file) if env_file.exists() else {}
    host = env_vals.get("FLIGHTLINK_API_HOST") or c.settings.api_host
    port_raw = env_vals.get("FLIGHTLINK_API_PORT")
    port = int(port_raw) if (port_raw and port_raw.isdigit()) else c.settings.api_port
    return host, port


@cli.command("init", help="Initialize FlightLink host environment, directories, SQLite DB, and optional admin account.")
@click.option("--admin-user", default=None, help="Create initial admin username.")
@click.option("--admin-password", default=None, help="Initial admin password (>= 8 chars).")
@click.option("--dtu-cidr", multiple=True, help="Explicitly allowed DTU source CIDR (e.g. 0.0.0.0/0).")
@click.option("--gcs-cidr", multiple=True, help="Explicitly allowed GCS source CIDR (e.g. 0.0.0.0/0).")
@click.option("--host", "--api-host", "api_host", default=None, help="API listen host (default: 127.0.0.1; preserves existing env if omitted).")
@click.option("--port", "--api-port", "api_port", type=click.IntRange(1, 65535), default=None, help="API listen port (default: 8000; preserves existing env if omitted).")
@click.option("--system/--no-system", "provision_system", default=None, help="Provision Linux users, systemd units, Polkit, SELinux, and flightlink.env (auto-enabled as Linux root).")
@click.option("--env-file", type=click.Path(path_type=Path), default=DEFAULT_ENV_PATH, hidden=True)
@click.option("--systemd-dir", type=click.Path(path_type=Path), default=SYSTEMD_DIR, hidden=True)
@click.option("--polkit-rule", type=click.Path(path_type=Path), default=POLKIT_RULE_PATH, hidden=True)
@click.pass_obj
def cmd_init(
    c: CliContext,
    admin_user: str | None,
    admin_password: str | None,
    dtu_cidr: tuple[str, ...],
    gcs_cidr: tuple[str, ...],
    api_host: str | None,
    api_port: int | None,
    provision_system: bool | None,
    env_file: Path,
    systemd_dir: Path,
    polkit_rule: Path,
) -> None:
    if api_port is not None:
        _ensure_api_port_not_reserved(c, api_port)

    do_system = is_linux_root() if provision_system is None else provision_system
    system_report = None
    if do_system:
        system_report = provision_linux_host(
            data_dir=c.settings.data_dir,
            env_file=env_file,
            systemd_dir=systemd_dir,
            polkit_rule_path=polkit_rule,
            api_host=api_host,
            api_port=api_port,
            dtu_cidrs=list(dtu_cidr) if dtu_cidr else None,
            gcs_cidrs=list(gcs_cidr) if gcs_cidr else None,
            apply_os_commands=is_linux_root(),
        )
    elif (api_host is not None or api_port is not None) and env_file.exists():
        updates: dict[str, str] = {}
        if api_host is not None:
            updates["FLIGHTLINK_API_HOST"] = api_host
        if api_port is not None:
            updates["FLIGHTLINK_API_PORT"] = str(api_port)
        write_env_file(env_file, updates)

    if api_host is not None:
        c.settings.api_host = api_host
    if api_port is not None:
        c.settings.api_port = api_port

    c.settings.data_dir.mkdir(parents=True, exist_ok=True)
    c.settings.resolved_archive_dir.mkdir(parents=True, exist_ok=True)
    c.settings.resolved_router_config_dir.mkdir(parents=True, exist_ok=True)
    c.settings.resolved_runtime_dir.mkdir(parents=True, exist_ok=True)
    ecs_id = c.db.init_schema(ecs_instance_id=c.settings.ecs_instance_id)

    if not c.db.get_meta("jwt_secret_initialized"):
        c.db.set_meta("jwt_secret_initialized", secrets.token_hex(16))
    if dtu_cidr:
        c.db.set_meta_json("dtu_allowed_cidrs", list(dtu_cidr))
    if gcs_cidr:
        c.db.set_meta_json("gcs_allowed_cidrs", list(gcs_cidr))

    created_admin = None
    if admin_user:
        if not admin_password:
            raise click.ClickException("--admin-password is required when --admin-user is provided.")
        try:
            profile = c.repo.create_user(username=admin_user, password=admin_password, role="admin")
            created_admin = profile.model_dump()
        except DomainError as exc:
            raise click.ClickException(f"{exc.code}: {exc.message}") from exc

    fix_linux_db_permissions(c.settings.data_dir)
    resolved_host, resolved_port = _resolve_api_listen(c, env_file=env_file)

    _echo_json(
        {
            "status": "initialized",
            "ecs_instance_id": ecs_id,
            "api_listen": f"{resolved_host}:{resolved_port}",
            "db_path": str(c.settings.resolved_db_path),
            "archive_dir": str(c.settings.resolved_archive_dir),
            "router_config_dir": str(c.settings.resolved_router_config_dir),
            "admin_created": created_admin,
            "system_provisioned": system_report,
        }
    )


@cli.command("connect", help="Verify and record existing ECS RAM role and target business security group (never creates roles).")
@click.option("--ram", "use_ram", is_flag=True, required=True, help="Authenticate using ECS instance RAM role (no static AccessKey).")
@click.option("--region", required=True, help="Aliyun region ID, e.g. cn-beijing.")
@click.option("--security-group", "security_group_id", required=True, help="Target business security group ID.")
@click.option("--ram-role", default=None, help="Optional expected RAM role name; omit to auto-discover attached ECS instance role.")
@click.option("--env-file", type=click.Path(path_type=Path), default=DEFAULT_ENV_PATH, hidden=True)
@click.pass_obj
def cmd_connect(
    c: CliContext,
    use_ram: bool,
    region: str,
    security_group_id: str,
    ram_role: str | None,
    env_file: Path,
) -> None:
    if not use_ram:
        raise click.ClickException("Only --ram instance role authentication is supported.")
    try:
        verified = c.agent.sg_client.validate_connection(
            region=region,
            security_group_id=security_group_id,
            ram_role=ram_role,
        )
    except Exception as exc:
        raise click.ClickException(f"RAM/SecurityGroup verification failed: {exc}") from exc

    resolved_role = verified.get("ram_role", ram_role or "")
    c.db.set_meta("aliyun_region", region)
    c.db.set_meta("aliyun_security_group_id", security_group_id)
    c.db.set_meta("aliyun_ram_role_name", resolved_role)
    if env_file.exists():
        try:
            write_env_file(
                env_file,
                {
                    "FLIGHTLINK_ALIYUN_REGION": region,
                    "FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID": security_group_id,
                    "FLIGHTLINK_ALIYUN_RAM_ROLE_NAME": resolved_role,
                },
            )
        except OSError:
            pass
    fix_linux_db_permissions(c.settings.data_dir)
    _echo_json({**verified, "status": "connected"})


@cli.command("doctor", help="Strictly read-only health, configuration, binary, disk, legacy remnant, and firewall diagnostic check.")
@click.option("--env-file", type=click.Path(path_type=Path), default=DEFAULT_ENV_PATH, hidden=True)
@click.option("--systemd-dir", type=click.Path(path_type=Path), default=SYSTEMD_DIR, hidden=True)
@click.option("--root-prefix", type=click.Path(path_type=Path), default=None, hidden=True)
@click.pass_obj
def cmd_doctor(c: CliContext, env_file: Path, systemd_dir: Path, root_prefix: Path | None) -> None:
    overview = c.repo.get_overview()
    try:
        fw_diff = c.agent.compute_firewall_diff()
    except Exception as exc:
        fw_diff = {"error": str(exc)}

    venv_mavproxy = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin") / "mavproxy.py"
    mavproxy_path = str(venv_mavproxy) if venv_mavproxy.exists() else shutil.which("mavproxy.py")
    dtu_cidrs = c.settings.dtu_allowed_cidrs or list(c.db.get_meta_json("dtu_allowed_cidrs", []) or [])
    gcs_cidrs = c.settings.gcs_allowed_cidrs or list(c.db.get_meta_json("gcs_allowed_cidrs", []) or [])
    api_host, api_port = _resolve_api_listen(c, env_file=env_file)
    legacy = scan_legacy_remnants(
        systemd_dir=systemd_dir,
        env_file=env_file,
        root_prefix=root_prefix,
        inspect_systemd=c.settings.adapter_mode != "fake" and is_linux_root(),
    )
    checks = {
        "read_only": True,
        "db_ok": c.db.health_check(),
        "ecs_instance_id": overview.ecs_instance_id,
        "api_listen": f"{api_host}:{api_port}",
        "binaries": {
            "mavlink-routerd": shutil.which("mavlink-routerd"),
            "mavproxy.py": mavproxy_path,
            "systemctl": shutil.which("systemctl"),
            "ss": shutil.which("ss"),
        },
        "legacy_remnants": legacy,
        "aliyun": {
            "region": c.db.get_meta("aliyun_region") or c.settings.aliyun_region,
            "security_group_id": c.db.get_meta("aliyun_security_group_id") or c.settings.aliyun_security_group_id,
            "ram_role": c.db.get_meta("aliyun_ram_role_name") or c.settings.aliyun_ram_role_name,
            "dtu_allowed_cidrs": dtu_cidrs,
            "gcs_allowed_cidrs": gcs_cidrs,
        },
        "disk": overview.disk.model_dump(),
        "metrics": overview.metrics.model_dump(),
        "components": [comp.model_dump() for comp in overview.components],
        "firewall_diff": fw_diff,
    }
    _echo_json(checks)


@cli.command("run", help="Start management services (flightlink-api, flightlink-agent, flightlink-archive); NEVER batch-starts aircraft channels.")
@click.option("--host", "--api-host", "api_host", default=None, help="Override API listen host and persist to flightlink.env.")
@click.option("--port", "--api-port", "api_port", type=click.IntRange(1, 65535), default=None, help="Override API listen port (default: 8000) and persist to flightlink.env.")
@click.option("--env-file", type=click.Path(path_type=Path), default=DEFAULT_ENV_PATH, hidden=True)
@click.pass_obj
def cmd_run(c: CliContext, api_host: str | None, api_port: int | None, env_file: Path) -> None:
    if api_port is not None:
        _ensure_api_port_not_reserved(c, api_port)

    env_updated = False
    if api_host is not None or api_port is not None:
        updates: dict[str, str] = {}
        if api_host is not None:
            updates["FLIGHTLINK_API_HOST"] = api_host
            c.settings.api_host = api_host
        if api_port is not None:
            updates["FLIGHTLINK_API_PORT"] = str(api_port)
            c.settings.api_port = api_port
        if env_file.exists() or is_linux_root() or env_file != DEFAULT_ENV_PATH:
            write_env_file(env_file, updates)
            env_updated = True
            if is_linux_root():
                subprocess.run(["chown", "root:flightlink", str(env_file)], check=False)
                subprocess.run(["chmod", "0640", str(env_file)], check=False)

    resolved_host, resolved_port = _resolve_api_listen(c, env_file=env_file)
    units = ["flightlink-api.service", "flightlink-agent.service", "flightlink-archive.service"]
    if c.settings.adapter_mode == "fake":
        c.db.record_component_heartbeat("flightlink-api", "ok", f"Started by flightlink run ({resolved_host}:{resolved_port})")
        c.db.record_component_heartbeat("flightlink-agent", "ok", "Started by flightlink run")
        c.db.record_component_heartbeat("flightlink-archive", "ok", "Started by flightlink run")
        _echo_json(
            {
                "status": "started",
                "services": units,
                "api_listen": f"{resolved_host}:{resolved_port}",
                "env_updated": env_updated,
                "aircraft_channels_auto_started": False,
            }
        )
        return
    subprocess.run(["systemctl", "enable", "--now", *units], check=True)
    if env_updated:
        subprocess.run(["systemctl", "restart", "flightlink-api.service"], check=True)
    _echo_json(
        {
            "status": "started",
            "services": units,
            "api_listen": f"{resolved_host}:{resolved_port}",
            "env_updated": env_updated,
            "aircraft_channels_auto_started": False,
        }
    )


@cli.command("clear", help="Clean legacy v0.1 remnants (default), orphaned 1.0 runtime files (--runtime), or full 1.0 reset (--all --yes).")
@click.option("--legacy", "mode_legacy", is_flag=True, default=False, help="Clean legacy v0.1 remnants only (default, safe for 1.0).")
@click.option("--runtime", "mode_runtime", is_flag=True, default=False, help="Clean orphaned FlightLink 1.0 runtime configs/env/units.")
@click.option("--all", "mode_all", is_flag=True, default=False, help="Full reset of FlightLink 1.0 host state (requires --yes; preserves archives by default).")
@click.option("--dry-run", is_flag=True, default=False, help="Preview actions without stopping services or deleting files.")
@click.option("--include-legacy-app", is_flag=True, default=False, help="Also remove legacy /etc/flightlink/api.env and /opt/flightlink/backend.")
@click.option("--skip-cloud", is_flag=True, default=False, help="In --all mode, skip revoking FlightLink-owned cloud security group rules.")
@click.option("--purge-archives", is_flag=True, default=False, help="In --all mode, also delete archived flight logs (default: preserved).")
@click.option("--yes", "-y", is_flag=True, default=False, help="Confirm destructive --all cleanup.")
@click.option("--env-file", type=click.Path(path_type=Path), default=DEFAULT_ENV_PATH, hidden=True)
@click.option("--systemd-dir", type=click.Path(path_type=Path), default=SYSTEMD_DIR, hidden=True)
@click.option("--polkit-rule", type=click.Path(path_type=Path), default=POLKIT_RULE_PATH, hidden=True)
@click.option("--root-prefix", type=click.Path(path_type=Path), default=None, hidden=True)
@click.pass_obj
def cmd_clear(
    c: CliContext,
    mode_legacy: bool,
    mode_runtime: bool,
    mode_all: bool,
    dry_run: bool,
    include_legacy_app: bool,
    skip_cloud: bool,
    purge_archives: bool,
    yes: bool,
    env_file: Path,
    systemd_dir: Path,
    polkit_rule: Path,
    root_prefix: Path | None,
) -> None:
    selected = [m for m, flag in (("legacy", mode_legacy), ("runtime", mode_runtime), ("all", mode_all)) if flag]
    if len(selected) > 1:
        raise click.ClickException("Choose at most one mode: --legacy, --runtime, or --all.")
    mode: Literal["legacy", "runtime", "all"] = selected[0] if selected else "legacy"  # type: ignore[assignment]

    if mode == "all" and not dry_run and not yes:
        raise click.ClickException(
            "Refusing destructive `--all` cleanup without `--yes` (run with `--dry-run` first to preview)."
        )
    if purge_archives and mode != "all":
        raise click.ClickException("--purge-archives can only be used together with --all --yes.")

    active_ids: set[str] = set()
    ecs_id = c.settings.ecs_instance_id
    sg_id = c.settings.aliyun_security_group_id
    if c.settings.resolved_db_path.exists():
        try:
            active_ids = {g.group_id for g in c.repo.list_groups().items}
            ecs_id = c.db.get_meta("ecs_instance_id") or ecs_id
            sg_id = c.db.get_meta("aliyun_security_group_id") or sg_id
        except Exception:
            pass

    if mode == "all" and not dry_run and c.settings.adapter_mode == "fake":
        for gid in active_ids:
            c.agent.supervisor.stop_mavproxy(gid)
            c.agent.supervisor.stop_router(gid)

    result = clear_environment(
        mode=mode,
        dry_run=dry_run,
        include_legacy_app=include_legacy_app,
        skip_cloud=skip_cloud,
        purge_archives=purge_archives,
        data_dir=c.settings.data_dir,
        active_group_ids=active_ids,
        ecs_instance_id=ecs_id,
        sg_id=sg_id,
        sg_client=c.agent.sg_client,
        env_file=env_file,
        systemd_dir=systemd_dir,
        polkit_rule_path=polkit_rule,
        root_prefix=root_prefix,
        apply_os_commands=c.settings.adapter_mode != "fake" and is_linux_root(),
    )
    _echo_json(result)


@cli.command("stop", help="Stop active channels and management services; NEVER deletes archives.")
@click.pass_obj
def cmd_stop(c: CliContext) -> None:
    stopped_groups: list[str] = []
    for g in c.repo.list_groups().items:
        if g.desired_state == "running" or g.router.state == "running":
            op = c.repo.enqueue_group_action(g.group_id, "stop", requested_by=c.actor)
            _execute_or_wait(c, op.operation_id)
            stopped_groups.append(g.group_id)

    units = ["flightlink-api.service", "flightlink-agent.service", "flightlink-archive.service"]
    if c.settings.adapter_mode == "fake":
        for name in ("flightlink-api", "flightlink-agent", "flightlink-archive"):
            c.db.record_component_heartbeat(name, "stopped", "Stopped by flightlink stop")
    else:
        for unit in units:
            subprocess.run(["systemctl", "stop", unit], check=False)

    _echo_json(
        {
            "status": "stopped",
            "stopped_groups": stopped_groups,
            "services_stopped": units,
            "archives_preserved": True,
        }
    )


@cli.command("status", help="Show read-only status overview of management services and port groups.")
@click.pass_obj
def cmd_status(c: CliContext) -> None:
    _echo_json(c.repo.get_overview().model_dump())


# --- User & Token Management ---
@cli.group("user", help="Manage production user accounts.")
def user_group() -> None:
    pass


@user_group.command("add", help="Create a production admin or viewer account.")
@click.option("--username", required=True)
@click.option("--password", required=True)
@click.option("--role", type=click.Choice(["admin", "viewer"]), default="admin")
@click.option("--display-name", default=None)
@click.pass_obj
def cmd_user_add(c: CliContext, username: str, password: str, role: Literal["admin", "viewer"], display_name: str | None) -> None:
    try:
        profile = c.repo.create_user(username=username, password=password, role=role, display_name=display_name)
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc
    _echo_json(profile.model_dump())


@cli.group("token", help="Manage revocable API tokens for headless sync clients.")
def token_group() -> None:
    pass


@token_group.command("create", help="Issue a revocable API token (`flk_...`).")
@click.option("--username", required=True)
@click.option("--name", required=True)
@click.option("--role", type=click.Choice(["admin", "viewer"]), default="viewer")
@click.option("--ttl-days", type=int, default=None)
@click.pass_obj
def cmd_token_create(c: CliContext, username: str, name: str, role: Literal["admin", "viewer"], ttl_days: int | None) -> None:
    try:
        token_id, raw = c.repo.create_api_token(username=username, name=name, role=role, ttl_days=ttl_days)
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc
    _echo_json({"token_id": token_id, "token": raw, "role": role})


# --- Group Management ---
@cli.group("group", help="Port group lifecycle commands.")
def group_cmd() -> None:
    pass


@group_cmd.command("create", help="Create a new port group (configured_stopped).")
@click.option("--name", required=True)
@click.option("--dtu-port", type=int, required=True)
@click.option("--gcs", required=True, help="GCS endpoint as tcp:<port> or udp:<port>.")
@click.option("--wait/--no-wait", default=True, help="Execute/wait for operation completion.")
@click.pass_obj
def cmd_group_create(c: CliContext, name: str, dtu_port: int, gcs: str, wait: bool) -> None:
    gcs_proto, gcs_port = parse_gcs_spec(gcs)
    try:
        req = CreateGroupRequest(name=name, dtu_port=dtu_port, gcs_protocol=gcs_proto, gcs_port=gcs_port)
        op = c.repo.enqueue_create_group(req, requested_by=c.actor)
    except (DomainError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if wait:
        op = _execute_or_wait(c, op.operation_id)
    else:
        fix_linux_db_permissions(c.settings.data_dir)
    _echo_json(op.model_dump())


def _run_group_action(c: CliContext, group_id: str, action: str, wait: bool) -> None:
    try:
        op = c.repo.enqueue_group_action(group_id, action, requested_by=c.actor)
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc
    if wait:
        op = _execute_or_wait(c, op.operation_id)
    else:
        fix_linux_db_permissions(c.settings.data_dir)
    _echo_json(op.model_dump())


@group_cmd.command("start", help="Start a port group.")
@click.argument("group_id")
@click.option("--wait/--no-wait", default=True)
@click.pass_obj
def cmd_group_start(c: CliContext, group_id: str, wait: bool) -> None:
    _run_group_action(c, group_id, "start", wait)


@group_cmd.command("stop", help="Stop a port group.")
@click.argument("group_id")
@click.option("--wait/--no-wait", default=True)
@click.pass_obj
def cmd_group_stop(c: CliContext, group_id: str, wait: bool) -> None:
    _run_group_action(c, group_id, "stop", wait)


@group_cmd.command("restart", help="Restart a port group.")
@click.argument("group_id")
@click.option("--wait/--no-wait", default=True)
@click.pass_obj
def cmd_group_restart(c: CliContext, group_id: str, wait: bool) -> None:
    _run_group_action(c, group_id, "restart", wait)


@group_cmd.command("update", help="Update group name, DTU port, or GCS protocol/port.")
@click.argument("group_id")
@click.option("--name", default=None)
@click.option("--dtu-port", type=int, default=None)
@click.option("--gcs", default=None, help="GCS endpoint as tcp:<port> or udp:<port>.")
@click.option("--dry-run", is_flag=True, default=False, help="Validate and print change plan without mutating.")
@click.option("--wait/--no-wait", default=True)
@click.pass_obj
def cmd_group_update(
    c: CliContext,
    group_id: str,
    name: str | None,
    dtu_port: int | None,
    gcs: str | None,
    dry_run: bool,
    wait: bool,
) -> None:
    gcs_proto = None
    gcs_port = None
    if gcs is not None:
        gcs_proto, gcs_port = parse_gcs_spec(gcs)
    req = UpdateGroupRequest(name=name, dtu_port=dtu_port, gcs_protocol=gcs_proto, gcs_port=gcs_port)
    try:
        if dry_run:
            plan = c.repo.dry_run_update_group(group_id, req)
            _echo_json(plan.model_dump())
            return
        op = c.repo.enqueue_update_group(group_id, req, requested_by=c.actor)
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc
    if wait:
        op = _execute_or_wait(c, op.operation_id)
    else:
        fix_linux_db_permissions(c.settings.data_dir)
    _echo_json(op.model_dump())


@group_cmd.command("delete", help="Delete a port group while preserving unexpired archives.")
@click.argument("group_id")
@click.option("--wait/--no-wait", default=True)
@click.pass_obj
def cmd_group_delete(c: CliContext, group_id: str, wait: bool) -> None:
    try:
        op = c.repo.enqueue_delete_group(group_id, requested_by=c.actor)
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc
    if wait:
        op = _execute_or_wait(c, op.operation_id)
    else:
        fix_linux_db_permissions(c.settings.data_dir)
    _echo_json(op.model_dump())


# --- Firewall ---
@cli.group("firewall", help="Cloud security group diff and sync commands.")
def firewall_cmd() -> None:
    pass


@firewall_cmd.command("diff", help="Read-only diff between desired FlightLink rules and live cloud SG rules.")
@click.pass_obj
def cmd_firewall_diff(c: CliContext) -> None:
    _echo_json(c.agent.compute_firewall_diff())


@firewall_cmd.command("sync", help="Reconcile cloud security group rules for one group.")
@click.argument("group_id")
@click.option("--wait/--no-wait", default=True)
@click.pass_obj
def cmd_firewall_sync(c: CliContext, group_id: str, wait: bool) -> None:
    _run_group_action(c, group_id, "sync-security-group", wait)


# --- Operations ---
@cli.group("op", help="Inspect and retry async operations.")
def op_cmd() -> None:
    pass


@op_cmd.command("show", help="Show operation status and execution steps.")
@click.argument("operation_id")
@click.pass_obj
def cmd_op_show(c: CliContext, operation_id: str) -> None:
    try:
        op = c.repo.get_operation(operation_id)
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc
    _echo_json(op.model_dump())


@op_cmd.command("retry", help="Retry a retryable failed/needs_verification operation.")
@click.argument("operation_id")
@click.option("--wait/--no-wait", default=True)
@click.pass_obj
def cmd_op_retry(c: CliContext, operation_id: str, wait: bool) -> None:
    try:
        op = c.repo.enqueue_retry_operation(operation_id, requested_by=c.actor)
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc
    if wait:
        op = _execute_or_wait(c, op.operation_id)
    else:
        fix_linux_db_permissions(c.settings.data_dir)
    _echo_json(op.model_dump())


# --- Logs ---
@cli.command("logs", help="Read live logs for a port group.")
@click.argument("group_id")
@click.option("--source", type=click.Choice(["all", "router", "mavproxy"]), default="all")
@click.option("--level", type=click.Choice(["all", "DEBUG", "INFO", "WARN", "ERROR"]), default="all")
@click.option("--follow", is_flag=True, default=False)
@click.option("--limit", type=int, default=100)
@click.pass_obj
def cmd_logs(c: CliContext, group_id: str, source: str, level: str, follow: bool, limit: int) -> None:
    cursor = None
    while True:
        page = c.archive.query_live_logs(group_id=group_id, source=source, level=level, cursor=cursor, limit=limit)
        for item in page.items:
            click.echo(f"{item.ts} [{item.source.upper()}] [{item.level}] {item.text}")
        cursor = page.next_cursor
        if not follow:
            break
        time.sleep(1.0)


# --- History ---
@cli.group("history", help="Browse and export archived flight sessions.")
def history_cmd() -> None:
    pass


@history_cmd.command("list", help="List archived groups and session counts.")
@click.option("--include-deleted-groups/--active-only", default=True)
@click.pass_obj
def cmd_history_list(c: CliContext, include_deleted_groups: bool) -> None:
    res = c.archive.list_archive_groups(include_deleted=include_deleted_groups)
    _echo_json(res.model_dump())


@history_cmd.command("export", help="Export a session archive (router | mavproxy | metadata | all).")
@click.argument("session_id")
@click.option("--source", type=click.Choice(["router", "mavproxy", "metadata", "all"]), default="all")
@click.option("--output", "-o", type=click.Path(path_type=Path), default=None)
@click.pass_obj
def cmd_history_export(
    c: CliContext,
    session_id: str,
    source: Literal["router", "mavproxy", "metadata", "all"],
    output: Path | None,
) -> None:
    try:
        files, _meta = c.archive.resolve_session_download_by_source(session_id=session_id, source=source)
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc

    if source == "all":
        bundle_path, digest, size = build_session_bundle_tgz(session_id=session_id, files=files)
        dest = output or Path(f"flightlink-{session_id}-all.tar.gz")
        shutil.move(str(bundle_path), str(dest))
        _echo_json({"exported": str(dest), "source": "all", "size_bytes": size, "sha256": digest})
        return

    logical_name, src_path = files[0]
    dest = output or Path(f"flightlink-{session_id}-{logical_name}")
    shutil.copyfile(src_path, dest)
    _echo_json({"exported": str(dest), "source": source, "size_bytes": dest.stat().st_size})


@cli.command("import-legacy", help="Read-only import from legacy v0.1 database (imported stopped).")
@click.option("--db", "legacy_db", type=click.Path(path_type=Path, exists=True), required=True)
@click.option("--legacy-active", is_flag=True, default=False, hidden=True)
@click.pass_obj
def cmd_import_legacy(c: CliContext, legacy_db: Path, legacy_active: bool) -> None:
    try:
        res = import_legacy_v01_readonly(
            legacy_db_path=legacy_db,
            repo=c.repo,
            requested_by=c.actor,
            legacy_service_active=legacy_active,
        )
    except DomainError as exc:
        raise click.ClickException(f"{exc.code}: {exc.message}") from exc
    _echo_json(res)


if __name__ == "__main__":
    cli()
