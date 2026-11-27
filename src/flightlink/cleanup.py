"""Host environment scanning and safe layered cleanup (`flightlink clear`).

Layers:
- `legacy` (default): removes ONLY known legacy v0.1 remnants (strict whitelist);
  never touches FlightLink 1.0 DB, archives, services, or cloud security group rules.
- `runtime`: removes orphaned FlightLink 1.0 runtime artifacts (`.candidate.conf`,
  orphaned `router.d/*.conf` / `run/mavproxy-*.env`, and unmanaged router/mavproxy units).
- `all`: full reset of FlightLink 1.0 host state (stops channels & management services,
  revokes ONLY `flightlink:v1:<ecs_id>:` owned cloud SG rules unless `--skip-cloud`,
  removes 1.0 units/Polkit/env/DB/runtime files, and PRESERVES `archive/` by default
  unless `--purge-archives` is explicitly passed).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from flightlink.provisioning import (
    DEFAULT_ENV_PATH,
    POLKIT_RULE_PATH,
    SYSTEMD_DIR,
    is_linux_root,
)

V1_SYSTEMD_UNITS = (
    "flightlink-api.service",
    "flightlink-agent.service",
    "flightlink-archive.service",
    "flightlink-router@.service",
    "flightlink-mavproxy@.service",
)


def _run_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=20)


def _list_systemd_units(pattern: str) -> list[str]:
    if not shutil.which("systemctl"):
        return []
    res = _run_cmd(["systemctl", "list-units", "--all", "--no-legend", "--plain", "--no-pager", pattern])
    units: list[str] = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if parts and parts[0].endswith(".service"):
            units.append(parts[0])
    return units


def _legacy_paths(
    *,
    systemd_dir: Path = SYSTEMD_DIR,
    env_file: Path = DEFAULT_ENV_PATH,
    root_prefix: Path | None = None,
) -> tuple[list[Path], list[Path]]:
    """Return (core_legacy_paths, optional_legacy_app_paths)."""
    base = root_prefix or Path("/")
    core = [
        systemd_dir / "flightlink-capture@.service",
        systemd_dir / "flightlink-api.service.d",
        base / "usr/local/sbin/flightlink-capture-runner",
        base / "usr/local/sbin/flightlink-routerctl",
        base / "usr/local/sbin/flightlink",
        base / "usr/local/lib/flightlink",
        base / "etc/sudoers.d/flightlink-router-control",
        base / "run/flightlink-maintenance.lock",
    ]
    app = [
        env_file.parent / "api.env",
        base / "opt/flightlink/backend",
    ]
    return core, app


def scan_legacy_remnants(
    *,
    systemd_dir: Path = SYSTEMD_DIR,
    env_file: Path = DEFAULT_ENV_PATH,
    root_prefix: Path | None = None,
    inspect_systemd: bool | None = None,
) -> dict[str, Any]:
    """Strictly read-only scan for legacy v0.1 host remnants."""
    core_candidates, app_candidates = _legacy_paths(
        systemd_dir=systemd_dir,
        env_file=env_file,
        root_prefix=root_prefix,
    )
    existing_core = [str(p) for p in core_candidates if p.exists() or p.is_symlink()]
    existing_app = [str(p) for p in app_candidates if p.exists() or p.is_symlink()]
    check_sys = is_linux_root() if inspect_systemd is None else inspect_systemd
    capture_units = _list_systemd_units("flightlink-capture@*.service") if check_sys else []
    has_remnants = bool(existing_core or existing_app or capture_units)
    return {
        "detected": has_remnants,
        "paths": existing_core,
        "legacy_app_paths": existing_app,
        "capture_units": capture_units,
        "hint": "Run `sudo flightlink clear` to clean legacy v0.1 remnants." if has_remnants else None,
    }


def _remove_path(path: Path) -> bool:
    if not (path.exists() or path.is_symlink()):
        return False
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    if path.is_dir():
        shutil.rmtree(path)
        return True
    return False


def clear_environment(
    *,
    mode: Literal["legacy", "runtime", "all"] = "legacy",
    dry_run: bool = False,
    include_legacy_app: bool = False,
    skip_cloud: bool = False,
    purge_archives: bool = False,
    data_dir: Path,
    active_group_ids: set[str],
    ecs_instance_id: str | None = None,
    sg_id: str | None = None,
    sg_client: Any | None = None,
    env_file: Path = DEFAULT_ENV_PATH,
    systemd_dir: Path = SYSTEMD_DIR,
    polkit_rule_path: Path = POLKIT_RULE_PATH,
    root_prefix: Path | None = None,
    apply_os_commands: bool | None = None,
) -> dict[str, Any]:
    """Execute or preview layered FlightLink host cleanup."""
    do_os = is_linux_root() if apply_os_commands is None else apply_os_commands

    units_to_stop: list[str] = []
    units_to_disable: list[str] = []
    paths_to_remove: list[Path] = []
    cloud_rules_revoked: list[dict[str, Any]] = []
    cloud_manual_preserved = 0
    cloud_warnings: list[str] = []
    notes: list[str] = []

    core_legacy, app_legacy = _legacy_paths(
        systemd_dir=systemd_dir,
        env_file=env_file,
        root_prefix=root_prefix,
    )

    if mode in ("legacy", "all"):
        if do_os:
            units_to_stop.extend(_list_systemd_units("flightlink-capture@*.service"))
        for p in core_legacy:
            if p.exists() or p.is_symlink():
                paths_to_remove.append(p)
        for p in app_legacy:
            if p.exists() or p.is_symlink():
                if include_legacy_app or mode == "all":
                    paths_to_remove.append(p)
                else:
                    notes.append(f"Legacy app path detected (pass --include-legacy-app to remove): {p}")

    if mode == "runtime":
        router_dir = data_dir / "router.d"
        if router_dir.is_dir():
            for child in sorted(router_dir.iterdir()):
                if child.name.endswith(".candidate.conf"):
                    paths_to_remove.append(child)
                elif child.name.endswith(".conf"):
                    gid = child.name[: -len(".conf")]
                    if gid not in active_group_ids:
                        paths_to_remove.append(child)
        run_dir = data_dir / "run"
        if run_dir.is_dir():
            for child in sorted(run_dir.iterdir()):
                if child.name.startswith("mavproxy-") and child.name.endswith(".env"):
                    gid = child.name[len("mavproxy-") : -len(".env")]
                    if gid not in active_group_ids:
                        paths_to_remove.append(child)
        if do_os:
            for pattern, prefix in (
                ("flightlink-router@*.service", "flightlink-router@"),
                ("flightlink-mavproxy@*.service", "flightlink-mavproxy@"),
            ):
                for unit in _list_systemd_units(pattern):
                    gid = unit[len(prefix) : -len(".service")]
                    if gid not in active_group_ids:
                        units_to_stop.append(unit)

    if mode == "all":
        if do_os:
            units_to_stop.extend(_list_systemd_units("flightlink-mavproxy@*.service"))
            units_to_stop.extend(_list_systemd_units("flightlink-router@*.service"))
            units_to_stop.extend(
                ["flightlink-api.service", "flightlink-agent.service", "flightlink-archive.service"]
            )
            units_to_disable.extend(
                ["flightlink-api.service", "flightlink-agent.service", "flightlink-archive.service"]
            )

        for unit_file in V1_SYSTEMD_UNITS:
            p = systemd_dir / unit_file
            if p.exists() or p.is_symlink():
                paths_to_remove.append(p)
        if polkit_rule_path.exists() or polkit_rule_path.is_symlink():
            paths_to_remove.append(polkit_rule_path)
        if env_file.exists() or env_file.is_symlink():
            paths_to_remove.append(env_file)

        for db_name in ("control.sqlite3", "control.sqlite3-wal", "control.sqlite3-shm"):
            p = data_dir / db_name
            if p.exists():
                paths_to_remove.append(p)
        for sub in ("router.d", "run"):
            d = data_dir / sub
            if d.is_dir():
                for child in sorted(d.iterdir()):
                    paths_to_remove.append(child)
        if purge_archives:
            arch = data_dir / "archive"
            if arch.is_dir():
                for child in sorted(arch.iterdir()):
                    paths_to_remove.append(child)

        # Revoke ONLY this instance's FlightLink-owned cloud SG rules (never touch manual rules!)
        if not skip_cloud and sg_id and sg_client is not None and ecs_instance_id:
            prefix = f"flightlink:v1:{ecs_instance_id}:"
            try:
                live_rules = sg_client.list_rules(security_group_id=sg_id)
                for lr in live_rules:
                    if lr.description.startswith(prefix):
                        entry = {
                            "rule_id": lr.rule_id,
                            "protocol": lr.protocol,
                            "port": lr.port,
                            "source_cidr": lr.source_cidr,
                            "description": lr.description,
                        }
                        if not dry_run:
                            sg_client.revoke_rule(security_group_id=sg_id, rule_id=lr.rule_id)
                        cloud_rules_revoked.append(entry)
                    else:
                        cloud_manual_preserved += 1
            except Exception as exc:
                cloud_warnings.append(f"Cloud SG cleanup skipped/failed: {exc}")

    # Deduplicate while preserving order
    units_to_stop = list(dict.fromkeys(units_to_stop))
    units_to_disable = list(dict.fromkeys(units_to_disable))
    dedup_paths: list[Path] = []
    seen_paths: set[str] = set()
    for p in paths_to_remove:
        sp = str(p)
        if sp not in seen_paths:
            seen_paths.add(sp)
            dedup_paths.append(p)

    removed_paths: list[str] = []
    stopped_units: list[str] = []
    daemon_reloaded = False

    if not dry_run:
        if do_os and shutil.which("systemctl"):
            for unit in units_to_stop:
                _run_cmd(["systemctl", "stop", unit])
                stopped_units.append(unit)
            for unit in units_to_disable:
                _run_cmd(["systemctl", "disable", unit])
        for p in dedup_paths:
            if _remove_path(p):
                removed_paths.append(str(p))
        if do_os and shutil.which("systemctl") and (stopped_units or removed_paths):
            _run_cmd(["systemctl", "daemon-reload"])
            daemon_reloaded = True

    return {
        "mode": mode,
        "dry_run": dry_run,
        "status": "dry_run" if dry_run else "cleaned",
        "units_stopped": units_to_stop if dry_run else stopped_units,
        "paths_removed": [str(p) for p in dedup_paths] if dry_run else removed_paths,
        "daemon_reloaded": daemon_reloaded,
        "archives_preserved": not (mode == "all" and purge_archives),
        "cloud_rules_revoked": cloud_rules_revoked,
        "cloud_manual_preserved": cloud_manual_preserved,
        "cloud_warnings": cloud_warnings,
        "notes": notes,
    }
