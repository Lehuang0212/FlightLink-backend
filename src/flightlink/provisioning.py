"""Linux host system provisioning executed by `flightlink init` (when running as root or `--system`).

Automates:
- Creating least-privilege group `flightlink` and users `flightlink-api`, `flightlink-agent`, `flightlink-archive`
- Creating `/var/lib/flightlink{,/archive,/router.d,/run}` (mode 2770 setgid) and `/etc/flightlink` (mode 0750)
- Generating/updating `/etc/flightlink/flightlink.env` (preserving existing JWT secret)
- Installing systemd unit templates (auto-resolving current venv bin & mavlink-routerd paths, UMask=0007)
- Installing minimal Polkit rule allowing ONLY `flightlink-agent` to manage `flightlink-router@*.service`
  and `flightlink-mavproxy@*.service` (keeping `flightlink-api` at zero systemd privileges)
- Applying SELinux `bin_t` label on Rocky/RHEL if SELinux is active
- Running `systemctl daemon-reload`
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_ENV_PATH = Path("/etc/flightlink/flightlink.env")
POLKIT_RULE_PATH = Path("/etc/polkit-1/rules.d/50-flightlink-agent.rules")
SYSTEMD_DIR = Path("/etc/systemd/system")


POLKIT_RULE_CONTENT = """\
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        subject.user == "flightlink-agent") {
        var unit = action.lookup("unit");
        if (unit && (unit.indexOf("flightlink-router@") === 0 ||
                     unit.indexOf("flightlink-mavproxy@") === 0)) {
            return polkit.Result.YES;
        }
    }
});
"""


def render_systemd_units(
    *,
    venv_bin: Path,
    data_dir: Path,
    env_file: Path,
    mavlink_routerd_bin: str,
    mavproxy_bin: str,
) -> dict[str, str]:
    return {
        "flightlink-api.service": f"""\
[Unit]
Description=FlightLink ECS 1.0 Control & Read API (passive, no systemd/SG write privileges)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=flightlink-api
Group=flightlink
WorkingDirectory={data_dir}
EnvironmentFile=-{env_file}
ExecStart={venv_bin / "flightlink-api"}
UMask=0007
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={data_dir}
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
""",
        "flightlink-agent.service": f"""\
[Unit]
Description=FlightLink ECS 1.0 Channel Lifecycle & Security Group Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=flightlink-agent
Group=flightlink
WorkingDirectory={data_dir}
EnvironmentFile=-{env_file}
ExecStart={venv_bin / "flightlink-agent"}
UMask=0007
Restart=on-failure
RestartSec=3
PrivateTmp=true

[Install]
WantedBy=multi-user.target
""",
        "flightlink-archive.service": f"""\
[Unit]
Description=FlightLink ECS 1.0 Continuous Session Archive Collector & 7-Day Retention Cleaner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=flightlink-archive
Group=flightlink
WorkingDirectory={data_dir}
EnvironmentFile=-{env_file}
ExecStart={venv_bin / "flightlink-archive"}
UMask=0007
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={data_dir}
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
""",
        "flightlink-router@.service": f"""\
[Unit]
Description=FlightLink Per-Group Isolated mavlink-routerd (%i)
After=network-online.target
# Intentionally NOT WantedBy=multi-user.target:
# ECS host reboot MUST NOT automatically bring up aircraft port groups.

[Service]
Type=simple
User=flightlink-agent
Group=flightlink
ExecStart={mavlink_routerd_bin} -c {data_dir / "router.d"}/%i.conf
Restart=no
TimeoutStopSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=flightlink-router-%i
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadOnlyPaths={data_dir / "router.d"}
""",
        "flightlink-mavproxy@.service": f"""\
[Unit]
Description=FlightLink Passive Bypass MAVProxy Observer (%i)
After=flightlink-router@%i.service
PartOf=flightlink-router@%i.service

[Service]
Type=simple
User=flightlink-archive
Group=flightlink
Environment=HOME=/tmp
EnvironmentFile=-{data_dir / "run"}/mavproxy-%i.env
ExecStart={mavproxy_bin} --master=${{INTERNAL_TAP_ENDPOINT}} --daemon --non-interactive --no-state --target-system=0
Restart=on-failure
RestartSec=5
TimeoutStopSec=3
StandardOutput=journal
StandardError=journal
SyslogIdentifier=flightlink-mavproxy-%i
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
""",
    }


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    return values


def write_env_file(path: Path, updates: dict[str, str]) -> dict[str, str]:
    merged = parse_env_file(path)
    merged.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Managed by `flightlink init` / `flightlink connect`",
        *[f"{k}={v}" for k, v in sorted(merged.items())],
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return merged


def _run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def is_linux_root() -> bool:
    return sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0


def _resolve_venv_bin() -> Path:
    """Return the active virtualenv's bin directory WITHOUT following python symlinks to /usr/bin."""
    candidate = Path(sys.prefix) / ("Scripts" if sys.platform == "win32" else "bin")
    if candidate.is_dir():
        return candidate
    return Path(sys.executable).parent


def provision_linux_host(
    *,
    data_dir: Path,
    env_file: Path = DEFAULT_ENV_PATH,
    systemd_dir: Path = SYSTEMD_DIR,
    polkit_rule_path: Path = POLKIT_RULE_PATH,
    api_host: str | None = None,
    api_port: int | None = None,
    dtu_cidrs: list[str] | None = None,
    gcs_cidrs: list[str] | None = None,
    apply_os_commands: bool = True,
) -> dict[str, Any]:
    """Provision Linux host users, directories, flightlink.env, systemd units, Polkit, and SELinux."""
    venv_bin = _resolve_venv_bin()
    routerd_bin = (
        shutil.which("mavlink-routerd")
        or ("/usr/local/bin/mavlink-routerd" if Path("/usr/local/bin/mavlink-routerd").exists() else "/usr/bin/mavlink-routerd")
    )
    venv_mavproxy = venv_bin / "mavproxy.py"
    mavproxy_bin = str(venv_mavproxy) if venv_mavproxy.exists() else (shutil.which("mavproxy.py") or str(venv_mavproxy))

    actions: list[str] = []

    if apply_os_commands:
        _run_cmd(["groupadd", "--system", "flightlink"], check=False)
        for user in ("flightlink-api", "flightlink-agent", "flightlink-archive"):
            _run_cmd(
                ["useradd", "--system", "--gid", "flightlink", "--no-create-home", "--shell", "/sbin/nologin", user],
                check=False,
            )
        actions.append("system_users_ready")

    for d in (data_dir, data_dir / "archive", data_dir / "router.d", data_dir / "run"):
        d.mkdir(parents=True, exist_ok=True)
    env_file.parent.mkdir(parents=True, exist_ok=True)

    existing_env = parse_env_file(env_file)
    jwt_secret = existing_env.get("FLIGHTLINK_JWT_SECRET") or secrets.token_hex(32)
    resolved_api_host = api_host if api_host is not None else existing_env.get("FLIGHTLINK_API_HOST", "127.0.0.1")
    resolved_api_port = str(api_port) if api_port is not None else existing_env.get("FLIGHTLINK_API_PORT", "8000")
    env_updates: dict[str, str] = {
        "FLIGHTLINK_DATA_DIR": str(data_dir),
        "FLIGHTLINK_API_HOST": resolved_api_host,
        "FLIGHTLINK_API_PORT": resolved_api_port,
        "FLIGHTLINK_JWT_SECRET": jwt_secret,
        "FLIGHTLINK_ADAPTER_MODE": existing_env.get("FLIGHTLINK_ADAPTER_MODE", "linux"),
    }
    if dtu_cidrs:
        env_updates["FLIGHTLINK_DTU_ALLOWED_CIDRS"] = json.dumps(dtu_cidrs)
    if gcs_cidrs:
        env_updates["FLIGHTLINK_GCS_ALLOWED_CIDRS"] = json.dumps(gcs_cidrs)
    write_env_file(env_file, env_updates)
    actions.append(f"env_file_written:{env_file}")

    systemd_dir.mkdir(parents=True, exist_ok=True)
    units = render_systemd_units(
        venv_bin=venv_bin,
        data_dir=data_dir,
        env_file=env_file,
        mavlink_routerd_bin=routerd_bin,
        mavproxy_bin=mavproxy_bin,
    )
    for filename, content in units.items():
        (systemd_dir / filename).write_text(content, encoding="utf-8", newline="\n")
    actions.append(f"systemd_units_installed:{len(units)}")

    polkit_rule_path.parent.mkdir(parents=True, exist_ok=True)
    polkit_rule_path.write_text(POLKIT_RULE_CONTENT, encoding="utf-8", newline="\n")
    actions.append(f"polkit_rule_installed:{polkit_rule_path}")

    if apply_os_commands:
        _run_cmd(["chown", "-R", "flightlink-agent:flightlink", str(data_dir)], check=False)
        for d in (data_dir, data_dir / "archive", data_dir / "router.d", data_dir / "run"):
            _run_cmd(["chmod", "2770", str(d)], check=False)
        _run_cmd(["chown", "root:flightlink", str(env_file.parent), str(env_file)], check=False)
        _run_cmd(["chmod", "0750", str(env_file.parent)], check=False)
        _run_cmd(["chmod", "0640", str(env_file)], check=False)

        if shutil.which("getenforce"):
            sel = _run_cmd(["getenforce"], check=False)
            if sel.stdout.strip().lower() in ("enforcing", "permissive") and shutil.which("chcon"):
                _run_cmd(["chcon", "-R", "-t", "bin_t", str(venv_bin)], check=False)
                actions.append("selinux_bin_t_applied")

        if shutil.which("systemctl"):
            _run_cmd(["systemctl", "daemon-reload"], check=False)
            actions.append("systemd_daemon_reloaded")

    return {
        "env_file": str(env_file),
        "systemd_dir": str(systemd_dir),
        "polkit_rule": str(polkit_rule_path),
        "api_listen": f"{resolved_api_host}:{resolved_api_port}",
        "mavlink_routerd": routerd_bin,
        "mavproxy": mavproxy_bin,
        "actions": actions,
    }


def fix_linux_db_permissions(data_dir: Path) -> None:
    """Ensure SQLite DB, WAL, and router config files remain accessible to `flightlink` group after root CLI operations."""
    if not is_linux_root():
        return
    for name in ("control.sqlite3", "control.sqlite3-wal", "control.sqlite3-shm"):
        p = data_dir / name
        if p.exists():
            _run_cmd(["chown", "flightlink-agent:flightlink", str(p)], check=False)
            _run_cmd(["chmod", "0660", str(p)], check=False)
    for subdir in ("router.d", "run"):
        sub = data_dir / subdir
        if sub.is_dir():
            for child in sub.iterdir():
                if child.is_file():
                    _run_cmd(["chown", "flightlink-agent:flightlink", str(child)], check=False)
                    _run_cmd(["chmod", "0640", str(child)], check=False)
