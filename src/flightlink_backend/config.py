from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _read_positive_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    database_path: Path
    router_config_dir: Path
    capture_config_dir: Path
    capture_dir: Path
    router_manager_mode: str
    router_control_helper: Path
    sudo_path: Path
    router_command_timeout_seconds: int
    pcap_rotate_seconds: int
    pcap_retention_seconds: int
    pcap_max_bytes: int
    pcap_cleanup_interval_seconds: int
    session_cookie_name: str
    session_ttl_seconds: int
    session_cookie_secure: bool
    security_group_provider: str
    aliyun_region_id: str | None
    aliyun_security_group_id: str | None
    aliyun_ecs_role_name: str | None
    aliyun_uav_source_cidrs: str
    aliyun_gcs_source_cidrs: str | None
    aliyun_api_timeout_seconds: int

    @classmethod
    def from_environment(cls) -> Settings:
        data_dir = Path(os.getenv("FLIGHTLINK_DATA_DIR", "./data")).expanduser().resolve()
        database_path = Path(
            os.getenv("FLIGHTLINK_DATABASE_PATH", str(data_dir / "flightlink.sqlite3"))
        ).expanduser().resolve()
        router_config_dir = Path(
            os.getenv("FLIGHTLINK_ROUTER_CONFIG_DIR", str(data_dir / "router-configs"))
        ).expanduser().resolve()
        capture_config_dir = Path(
            os.getenv("FLIGHTLINK_CAPTURE_CONFIG_DIR", str(data_dir / "capture-configs"))
        ).expanduser().resolve()
        capture_dir = Path(
            os.getenv("FLIGHTLINK_CAPTURE_DIR", str(data_dir / "captures"))
        ).expanduser().resolve()
        router_manager_mode = os.getenv("FLIGHTLINK_ROUTER_MANAGER", "systemd").strip().lower()
        if router_manager_mode not in {"systemd", "disabled"}:
            raise ValueError("FLIGHTLINK_ROUTER_MANAGER must be 'systemd' or 'disabled'")
        pcap_rotate_seconds = _read_positive_int("FLIGHTLINK_PCAP_ROTATE_SECONDS", 600)
        if pcap_rotate_seconds > 86_400:
            raise ValueError("FLIGHTLINK_PCAP_ROTATE_SECONDS must not exceed 86400")
        security_group_provider = os.getenv(
            "FLIGHTLINK_SECURITY_GROUP_PROVIDER", "disabled"
        ).strip().lower()
        if security_group_provider not in {"disabled", "aliyun"}:
            raise ValueError(
                "FLIGHTLINK_SECURITY_GROUP_PROVIDER must be 'disabled' or 'aliyun'"
            )

        def optional_environment(name: str) -> str | None:
            value = os.getenv(name)
            if value is None:
                return None
            normalized = value.strip()
            return normalized or None

        return cls(
            data_dir=data_dir,
            database_path=database_path,
            router_config_dir=router_config_dir,
            capture_config_dir=capture_config_dir,
            capture_dir=capture_dir,
            router_manager_mode=router_manager_mode,
            router_control_helper=Path(
                os.getenv("FLIGHTLINK_ROUTER_CONTROL_HELPER", "/usr/local/sbin/flightlink-routerctl")
            ).expanduser(),
            sudo_path=Path(os.getenv("FLIGHTLINK_SUDO_PATH", "/usr/bin/sudo")).expanduser(),
            router_command_timeout_seconds=_read_positive_int(
                "FLIGHTLINK_ROUTER_COMMAND_TIMEOUT_SECONDS", 10
            ),
            pcap_rotate_seconds=pcap_rotate_seconds,
            pcap_retention_seconds=_read_positive_int(
                "FLIGHTLINK_PCAP_RETENTION_SECONDS", 86_400
            ),
            pcap_max_bytes=_read_positive_int("FLIGHTLINK_PCAP_MAX_BYTES", 1_073_741_824),
            pcap_cleanup_interval_seconds=_read_positive_int(
                "FLIGHTLINK_PCAP_CLEANUP_INTERVAL_SECONDS", 300
            ),
            session_cookie_name=os.getenv("FLIGHTLINK_SESSION_COOKIE_NAME", "flightlink_session"),
            session_ttl_seconds=_read_positive_int("FLIGHTLINK_SESSION_TTL_SECONDS", 43_200),
            session_cookie_secure=_read_bool("FLIGHTLINK_SESSION_COOKIE_SECURE", True),
            security_group_provider=security_group_provider,
            aliyun_region_id=optional_environment("FLIGHTLINK_ALIYUN_REGION_ID"),
            aliyun_security_group_id=optional_environment(
                "FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID"
            ),
            aliyun_ecs_role_name=optional_environment(
                "FLIGHTLINK_ALIYUN_ECS_ROLE_NAME"
            ),
            aliyun_uav_source_cidrs=(
                optional_environment("FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS")
                or "0.0.0.0/0"
            ),
            aliyun_gcs_source_cidrs=optional_environment(
                "FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS"
            ),
            aliyun_api_timeout_seconds=_read_positive_int(
                "FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS", 10
            ),
        )


settings = Settings.from_environment()
