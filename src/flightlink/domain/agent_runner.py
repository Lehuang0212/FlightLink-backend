"""Daemon entrypoint for `flightlink-agent`."""

from __future__ import annotations

import signal
import time

from flightlink.adapters.aliyun_sg import AliyunRamSecurityGroupClient
from flightlink.adapters.interfaces import FakeInfrastructure
from flightlink.adapters.linux_systemd import LinuxSocketInspector, LinuxSystemdSupervisor
from flightlink.archive.service import ArchiveService
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.lifecycle import LifecycleAgent


def build_agent(settings: Settings) -> LifecycleAgent:
    db = Database(settings.resolved_db_path)
    db.init_schema(ecs_instance_id=settings.ecs_instance_id)
    archive = ArchiveService(db, settings)

    if settings.adapter_mode == "fake":
        fake = FakeInfrastructure()
        return LifecycleAgent(
            db,
            settings,
            supervisor=fake,
            sockets=fake,
            sg_client=fake,
            archive_service=archive,
        )

    supervisor = LinuxSystemdSupervisor(runtime_dir=settings.resolved_runtime_dir)
    sockets = LinuxSocketInspector(supervisor)
    region = db.get_meta("aliyun_region") or settings.aliyun_region or "cn-beijing"
    role = db.get_meta("aliyun_ram_role_name") or settings.aliyun_ram_role_name
    sg_client = AliyunRamSecurityGroupClient(region=region, ram_role_name=role)
    return LifecycleAgent(
        db,
        settings,
        supervisor=supervisor,
        sockets=sockets,
        sg_client=sg_client,
        archive_service=archive,
    )


def main() -> None:
    settings = Settings()
    agent = build_agent(settings)
    agent.reconcile_on_startup(ecs_reboot=False)

    stop = False

    def _handle(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    while not stop:
        agent.process_pending_operations()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
