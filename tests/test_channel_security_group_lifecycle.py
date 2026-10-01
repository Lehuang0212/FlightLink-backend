from __future__ import annotations

import asyncio
import importlib
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from flightlink_backend import database
from flightlink_backend.config import settings
from flightlink_backend.schemas.channels import (
    ChannelTelemetryPublic,
    ChannelWrite,
    RouterRuntimePublic,
)


class FakeSecurityGroupProvider:
    enabled = True
    missing_configuration: list[str] = []

    def __init__(self, events: list[tuple], *, fail_protocol: str | None = None):
        self.events = events
        self.fail_protocol = fail_protocol
        self.rules = []
        self.next_id = 1
        self.fail_revoke_ids: set[str] = set()

    def describe_security_group(self):
        from flightlink_backend.services.aliyun_security_group import SecurityGroupSnapshot

        return SecurityGroupSnapshot(
            region_id="cn-hangzhou",
            security_group_id="sg-test",
            security_group_name="FlightLink test",
            rules=list(self.rules),
        )

    def authorize_rule(self, ingress):
        self.events.append(("authorize", ingress.protocol, ingress.port))
        if ingress.protocol.lower() == self.fail_protocol:
            raise RuntimeError("simulated authorization failure")
        from flightlink_backend.services.aliyun_security_group import SecurityGroupRule

        self.rules.append(
            SecurityGroupRule(
                rule_id=f"sgr-{self.next_id}",
                protocol=ingress.protocol.upper(),
                port_range=f"{ingress.port}/{ingress.port}",
                source_cidr=ingress.source_cidr,
                policy="Accept",
                description=ingress.description,
                direction="ingress",
            )
        )
        self.next_id += 1

    def revoke_rule(self, rule_id: str):
        self.events.append(("revoke", rule_id))
        if rule_id in self.fail_revoke_ids:
            raise RuntimeError("simulated revocation failure")
        for index, rule in enumerate(self.rules):
            if rule.rule_id == rule_id:
                del self.rules[index]
                return
        raise RuntimeError("InvalidSecurityGroupRuleId.NotFound")


class FakeProcessManager:
    def __init__(self, name: str, events: list[tuple]):
        self.name = name
        self.events = events
        self.state = "inactive"
        self.fail_start = False
        self.fail_restart = False
        self.fail_stop = False

    def start(self, channel_id):
        self.events.append((self.name, "start"))
        if self.fail_start:
            from flightlink_backend.services.router_manager import RouterManagerError
            from flightlink_backend.services.capture_manager import CaptureManagerError

            error_type = RouterManagerError if self.name == "router" else CaptureManagerError
            raise error_type("simulated start failure")
        self.state = "active"

    def restart(self, channel_id):
        self.events.append((self.name, "restart"))
        if self.fail_restart:
            from flightlink_backend.services.router_manager import RouterManagerError
            from flightlink_backend.services.capture_manager import CaptureManagerError

            error_type = RouterManagerError if self.name == "router" else CaptureManagerError
            raise error_type("simulated restart failure")
        self.state = "active"

    def stop(self, channel_id):
        self.events.append((self.name, "stop"))
        if self.fail_stop:
            from flightlink_backend.services.router_manager import RouterManagerError
            from flightlink_backend.services.capture_manager import CaptureManagerError

            error_type = RouterManagerError if self.name == "router" else CaptureManagerError
            raise error_type("simulated stop failure")
        self.state = "inactive"

    def status(self, channel_id):
        return RouterRuntimePublic(state=self.state)

    def logs(self, channel_id, limit):
        return SimpleNamespace(lines=[])


class FakeTelemetryMonitor:
    def start(self, *args):
        return None

    def snapshot(self, *args, **kwargs):
        return ChannelTelemetryPublic(monitor_state="listening")

    def stop(self, channel_id):
        return None

    def stop_all(self):
        return None


class ChannelSecurityGroupLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.config = replace(
            settings,
            data_dir=root,
            database_path=root / "flightlink.sqlite3",
            router_config_dir=root / "router-configs",
            capture_config_dir=root / "capture-configs",
            capture_dir=root / "captures",
            router_manager_mode="systemd",
            security_group_provider="aliyun",
            aliyun_uav_source_cidrs="0.0.0.0/0",
            aliyun_gcs_source_cidrs="198.51.100.8/32",
        )
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        for migration in (
            database._migration_1,
            database._migration_2,
            database._migration_3,
            database._migration_4,
        ):
            migration(self.connection)

        self.events: list[tuple] = []
        self.provider = FakeSecurityGroupProvider(self.events)
        self.router = FakeProcessManager("router", self.events)
        self.capture = FakeProcessManager("capture", self.events)
        self.telemetry = FakeTelemetryMonitor()
        self.channel_services = importlib.import_module("flightlink_backend.services.channels")
        self.router_config = importlib.import_module("flightlink_backend.services.router_config")
        self.capture_files = importlib.import_module("flightlink_backend.services.capture_files")
        self.router_manager = importlib.import_module("flightlink_backend.services.router_manager")
        self.capture_manager = importlib.import_module("flightlink_backend.services.capture_manager")
        self.channel_runtime = importlib.import_module("flightlink_backend.services.channel_runtime")
        self.security_group_rules = importlib.import_module(
            "flightlink_backend.services.security_group_rules"
        )
        self.service = importlib.import_module(
            "flightlink_backend.services.channel_security_group"
        )
        self.patches = [
            patch.object(self.router_config, "settings", self.config),
            patch.object(self.capture_files, "settings", self.config),
            patch.object(self.router_manager, "settings", self.config),
            patch.object(self.capture_manager, "settings", self.config),
            patch.object(self.channel_runtime, "settings", self.config),
            patch.object(self.channel_runtime, "get_mavlink_telemetry_monitor", return_value=self.telemetry),
            patch.object(self.security_group_rules, "settings", self.config),
            patch.object(
                self.security_group_rules,
                "get_security_group_provider",
                return_value=self.provider,
            ),
        ]
        for patcher in self.patches:
            patcher.start()
        self.router_manager.set_router_manager_for_process(self.router)
        self.capture_manager.set_capture_manager_for_process(self.capture)
        self.default_payload = ChannelWrite(
            uav_udp_port=5761,
            ground_station_port=14553,
            ground_station_protocol="tcp",
        )

    def tearDown(self):
        self.router_manager.set_router_manager_for_process(None)
        self.capture_manager.set_capture_manager_for_process(None)
        for patcher in reversed(self.patches):
            patcher.stop()
        self.connection.close()
        self.temporary_directory.cleanup()

    def _create(self, payload: ChannelWrite | None = None):
        return self.service.create_managed_channel(
            self.connection, payload or self.default_payload
        )

    def _records(self, channel_id: UUID):
        return self.security_group_rules.list_rule_records(self.connection, channel_id)

    def test_create_syncs_security_group_before_starting_router(self):
        channel = self._create()

        first_start = next(index for index, event in enumerate(self.events) if event == ("router", "start"))
        last_authorize = max(index for index, event in enumerate(self.events) if event[0] == "authorize")
        self.assertLess(last_authorize, first_start)
        self.assertEqual(channel.security_group.state, "synced")
        self.assertEqual(channel.runtime.state, "active")
        self.assertEqual(channel.capture_runtime.state, "active")

    def test_monitor_port_allocator_skips_requested_uav_udp_port(self):
        with patch.object(self.channel_services.socket, "socket"):
            channel = self.channel_services.create_channel(
                self.connection,
                ChannelWrite(
                    uav_udp_port=40000,
                    ground_station_port=14552,
                    ground_station_protocol="tcp",
                ),
            )

        self.assertNotEqual(channel.monitor_udp_port, channel.uav_udp_port)

    def test_monitor_port_allocator_skips_requested_udp_ground_station_port(self):
        with patch.object(self.channel_services.socket, "socket"):
            channel = self.channel_services.create_channel(
                self.connection,
                ChannelWrite(
                    uav_udp_port=5760,
                    ground_station_port=40000,
                    ground_station_protocol="udp",
                ),
            )

        self.assertNotEqual(channel.monitor_udp_port, channel.ground_station_port)

    def test_create_preserves_local_behavior_when_provider_disabled(self):
        from flightlink_backend.services.aliyun_security_group import DisabledSecurityGroupProvider

        with patch.object(
            self.security_group_rules,
            "get_security_group_provider",
            return_value=DisabledSecurityGroupProvider(),
        ):
            channel = self._create()

        self.assertEqual(channel.security_group.state, "disabled")
        self.assertIn(("router", "start"), self.events)
        self.assertFalse(any(event[0] == "authorize" for event in self.events))

    def test_create_failure_keeps_channel_error_and_does_not_start_router(self):
        self.provider.fail_protocol = "tcp"

        channel = self._create()

        self.assertEqual(channel.security_group.state, "error")
        self.assertIsNotNone(channel.security_group.error)
        self.assertFalse(any(event in {("router", "start"), ("capture", "start")} for event in self.events))
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT id FROM port_channels WHERE id = ?", (str(channel.id),)
            ).fetchone()
        )

    def test_update_authorizes_new_ports_before_local_config_switch(self):
        old_channel = self._create()
        self.events.clear()
        new_payload = ChannelWrite(
            uav_udp_port=5762,
            ground_station_port=14554,
            ground_station_protocol="tcp",
        )

        updated = self.service.update_managed_channel(
            self.connection, old_channel.id, new_payload
        )

        event_names = [event[0] for event in self.events]
        first_authorize = event_names.index("authorize")
        router_restart = self.events.index(("router", "restart"))
        first_revoke = event_names.index("revoke")
        self.assertLess(first_authorize, router_restart)
        self.assertLess(router_restart, first_revoke)
        self.assertEqual((updated.uav_udp_port, updated.ground_station_port), (5762, 14554))
        self.assertEqual(updated.security_group.state, "synced")

    def test_same_channel_updates_are_serialized_through_cloud_cleanup(self):
        database_path = Path(self.temporary_directory.name) / "concurrent.sqlite3"
        setup = sqlite3.connect(database_path, isolation_level=None)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        for migration in (
            database._migration_1,
            database._migration_2,
            database._migration_3,
            database._migration_4,
        ):
            migration(setup)
        channel = self.service.create_managed_channel(setup, self.default_payload)
        setup.close()

        first_at_cleanup = threading.Event()
        release_first = threading.Event()
        second_started_cloud_sync = threading.Event()
        results: dict[str, object] = {}
        failures: list[BaseException] = []
        original_cleanup = self.service.cleanup_stale_channel_rules
        original_ensure = self.service.ensure_channel_rules

        def pause_first_cleanup(connection, channel_id, payload):
            if payload.uav_udp_port == 5762:
                first_at_cleanup.set()
                if not release_first.wait(5):
                    raise TimeoutError("test did not release first update")
            return original_cleanup(connection, channel_id, payload)

        def observe_second_sync(connection, channel_id, payload):
            if payload.uav_udp_port == 5763:
                second_started_cloud_sync.set()
            return original_ensure(connection, channel_id, payload)

        def update(name: str, udp_port: int, tcp_port: int) -> None:
            connection = sqlite3.connect(database_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                results[name] = self.service.update_managed_channel(
                    connection,
                    channel.id,
                    ChannelWrite(
                        uav_udp_port=udp_port,
                        ground_station_port=tcp_port,
                        ground_station_protocol="tcp",
                    ),
                )
            except BaseException as exc:
                failures.append(exc)
            finally:
                connection.close()

        with patch.object(self.service, "cleanup_stale_channel_rules", pause_first_cleanup), patch.object(
            self.service, "ensure_channel_rules", observe_second_sync
        ):
            first = threading.Thread(target=update, args=("first", 5762, 14554))
            second = threading.Thread(target=update, args=("second", 5763, 14555))
            first.start()
            self.assertTrue(first_at_cleanup.wait(5), "first update did not reach cleanup")
            second.start()
            entered_without_lock = second_started_cloud_sync.wait(0.5)
            release_first.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(failures, failures)
        self.assertFalse(
            entered_without_lock,
            "second update entered cloud sync before the first update finished cleanup",
        )
        connection = sqlite3.connect(database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        stored = self.channel_services.get_channel(connection, channel.id)
        records = self.security_group_rules.list_rule_records(connection, channel.id)
        cloud_ports = {rule.port_range for rule in self.provider.rules}
        mapped_ports = {f"{record.port}/{record.port}" for record in records}
        self.assertEqual((stored.uav_udp_port, stored.ground_station_port), (5763, 14555))
        self.assertEqual(cloud_ports, mapped_ports)
        self.assertEqual(cloud_ports, {"5763/5763", "14555/14555"})
        self.assertEqual(stored.security_group.state, "synced")
        connection.close()

    def test_delete_waits_until_same_channel_update_cleanup_finishes(self):
        database_path = Path(self.temporary_directory.name) / "update-delete.sqlite3"
        setup = sqlite3.connect(database_path, isolation_level=None)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        for migration in (
            database._migration_1,
            database._migration_2,
            database._migration_3,
            database._migration_4,
        ):
            migration(setup)
        channel = self.service.create_managed_channel(setup, self.default_payload)
        setup.close()

        first_at_cleanup = threading.Event()
        release_first = threading.Event()
        delete_started = threading.Event()
        failures: list[BaseException] = []
        original_cleanup = self.service.cleanup_stale_channel_rules
        original_stop_capture = self.service.stop_capture_service

        def pause_update_cleanup(connection, channel_id, payload):
            if payload.uav_udp_port == 5762:
                first_at_cleanup.set()
                if not release_first.wait(5):
                    raise TimeoutError("test did not release update cleanup")
            return original_cleanup(connection, channel_id, payload)

        def observe_delete_stop(channel_id):
            delete_started.set()
            return original_stop_capture(channel_id)

        def with_connection(action):
            connection = sqlite3.connect(database_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                action(connection)
            except BaseException as exc:
                failures.append(exc)
            finally:
                connection.close()

        def update(connection):
            self.service.update_managed_channel(
                connection,
                channel.id,
                ChannelWrite(uav_udp_port=5762, ground_station_port=14554),
            )

        def delete(connection):
            self.service.delete_managed_channel(connection, channel.id)

        with patch.object(self.service, "cleanup_stale_channel_rules", pause_update_cleanup), patch.object(
            self.service, "stop_capture_service", observe_delete_stop
        ):
            first = threading.Thread(target=with_connection, args=(update,))
            second = threading.Thread(target=with_connection, args=(delete,))
            first.start()
            self.assertTrue(first_at_cleanup.wait(5), "update did not reach cleanup")
            second.start()
            delete_entered_without_lock = delete_started.wait(0.5)
            release_first.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(failures, failures)
        self.assertFalse(
            delete_entered_without_lock,
            "delete began stopping services before the update finished cleanup",
        )
        connection = sqlite3.connect(database_path, isolation_level=None)
        self.assertIsNone(
            connection.execute("SELECT id FROM port_channels WHERE id = ?", (str(channel.id),)).fetchone()
        )
        connection.close()

    def test_update_waits_until_managed_create_finishes_runtime_setup(self):
        database_path = Path(self.temporary_directory.name) / "create-update.sqlite3"
        setup = sqlite3.connect(database_path, isolation_level=None)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        for migration in (
            database._migration_1,
            database._migration_2,
            database._migration_3,
            database._migration_4,
        ):
            migration(setup)
        setup.close()

        create_at_runtime = threading.Event()
        release_create = threading.Event()
        update_started_cloud_sync = threading.Event()
        created_channel: dict[str, object] = {}
        failures: list[BaseException] = []
        original_runtime = self.service.reconcile_channel_runtime
        original_ensure = self.service.ensure_channel_rules

        def pause_create_runtime(channel, **kwargs):
            if channel.uav_udp_port == self.default_payload.uav_udp_port:
                created_channel["id"] = channel.id
                create_at_runtime.set()
                if not release_create.wait(5):
                    raise TimeoutError("test did not release managed create")
            return original_runtime(channel, **kwargs)

        def observe_update_sync(connection, channel_id, payload):
            if payload.uav_udp_port == 5763:
                update_started_cloud_sync.set()
            return original_ensure(connection, channel_id, payload)

        def with_connection(action):
            connection = sqlite3.connect(database_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                action(connection)
            except BaseException as exc:
                failures.append(exc)
            finally:
                connection.close()

        def create(connection):
            self.service.create_managed_channel(connection, self.default_payload)

        def update(connection):
            self.service.update_managed_channel(
                connection,
                UUID(str(created_channel["id"])),
                ChannelWrite(uav_udp_port=5763, ground_station_port=14555),
            )

        with patch.object(self.service, "reconcile_channel_runtime", pause_create_runtime), patch.object(
            self.service, "ensure_channel_rules", observe_update_sync
        ):
            first = threading.Thread(target=with_connection, args=(create,))
            second = threading.Thread(target=with_connection, args=(update,))
            first.start()
            self.assertTrue(create_at_runtime.wait(5), "managed create did not reach runtime setup")
            second.start()
            update_entered_without_lock = update_started_cloud_sync.wait(0.5)
            release_create.set()
            first.join(5)
            second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(failures, failures)
        self.assertFalse(
            update_entered_without_lock,
            "update entered cloud sync before managed create finished runtime setup",
        )
        connection = sqlite3.connect(database_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        channel_id = UUID(str(created_channel["id"]))
        stored = self.channel_services.get_channel(connection, channel_id)
        capture_config = self.capture_files.capture_config_path(channel_id)
        self.assertEqual((stored.uav_udp_port, stored.ground_station_port), (5763, 14555))
        self.assertIn('"uav_udp_port":5763', capture_config.read_text(encoding="utf-8"))
        self.assertIn("Port = 5763", self.router_config.config_file_path(channel_id).read_text(encoding="utf-8"))
        self.assertEqual(stored.security_group.state, "synced")
        connection.close()

    def test_get_runtime_recovery_cannot_restart_a_stale_monitor_after_update(self):
        database_path = Path(self.temporary_directory.name) / "get-update.sqlite3"
        setup = sqlite3.connect(database_path, isolation_level=None)
        setup.row_factory = sqlite3.Row
        setup.execute("PRAGMA foreign_keys = ON")
        for migration in (
            database._migration_1,
            database._migration_2,
            database._migration_3,
            database._migration_4,
        ):
            migration(setup)
        channel = self.service.create_managed_channel(setup, self.default_payload)
        setup.close()

        api_channels = importlib.import_module("flightlink_backend.api.channels")
        get_at_monitor_start = threading.Event()
        release_get = threading.Event()
        update_started_cloud_sync = threading.Event()
        current_monitor_ports: dict[str, tuple[int, int, int]] = {}
        failures: list[BaseException] = []
        original_start = self.telemetry.start
        original_ensure = self.service.ensure_channel_rules

        def pause_old_monitor_start(channel_id, monitor_port, uav_port, ground_port):
            if uav_port == self.default_payload.uav_udp_port:
                get_at_monitor_start.set()
                if not release_get.wait(5):
                    raise TimeoutError("test did not release runtime read")
            current_monitor_ports[str(channel_id)] = (monitor_port, uav_port, ground_port)
            return original_start(channel_id, monitor_port, uav_port, ground_port)

        def observe_update_sync(connection, channel_id, payload):
            if payload.uav_udp_port == 5763:
                update_started_cloud_sync.set()
            return original_ensure(connection, channel_id, payload)

        def read_runtime():
            connection = sqlite3.connect(database_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                api_channels.read_channel(channel.id, None, connection)
            except BaseException as exc:
                failures.append(exc)
            finally:
                connection.close()

        def update_channel():
            connection = sqlite3.connect(database_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                self.service.update_managed_channel(
                    connection,
                    channel.id,
                    ChannelWrite(uav_udp_port=5763, ground_station_port=14555),
                )
            except BaseException as exc:
                failures.append(exc)
            finally:
                connection.close()

        with patch.object(self.telemetry, "start", pause_old_monitor_start), patch.object(
            self.service, "ensure_channel_rules", observe_update_sync
        ), patch.object(api_channels, "get_router_manager", return_value=self.router), patch.object(
            api_channels, "get_capture_manager", return_value=self.capture
        ), patch.object(
            api_channels, "get_mavlink_telemetry_monitor", return_value=self.telemetry
        ):
            reader = threading.Thread(target=read_runtime)
            updater = threading.Thread(target=update_channel)
            reader.start()
            self.assertTrue(get_at_monitor_start.wait(5), "runtime read did not start the monitor")
            updater.start()
            update_entered_without_lock = update_started_cloud_sync.wait(0.5)
            release_get.set()
            reader.join(5)
            updater.join(5)

        self.assertFalse(reader.is_alive())
        self.assertFalse(updater.is_alive())
        self.assertFalse(failures, failures)
        self.assertFalse(
            update_entered_without_lock,
            "update ran while GET was still using an old channel snapshot",
        )
        self.assertEqual(
            current_monitor_ports[str(channel.id)][1:],
            (5763, 14555),
        )

    def test_update_cloud_failure_keeps_old_ports_and_compensates_new_rules(self):
        old_channel = self._create()
        old_rule_ids = {record.rule_id for record in self._records(old_channel.id)}
        self.provider.fail_protocol = "tcp"

        returned = self.service.update_managed_channel(
            self.connection,
            old_channel.id,
            ChannelWrite(uav_udp_port=5762, ground_station_port=14554),
        )

        stored = self.channel_services.get_channel(self.connection, old_channel.id)
        self.assertEqual((stored.uav_udp_port, stored.ground_station_port), (5761, 14553))
        self.assertEqual({record.rule_id for record in self._records(old_channel.id)}, old_rule_ids)
        self.assertEqual({rule.port_range for rule in self.provider.rules}, {"5761/5761", "14553/14553"})
        self.assertEqual(returned.security_group.state, "error")
        self.assertFalse(any(event in {("router", "restart"), ("capture", "restart")} for event in self.events))

    def test_update_keeps_old_rule_when_router_restart_fails(self):
        old_channel = self._create()
        old_rule_ids = {record.rule_id for record in self._records(old_channel.id)}
        self.router.fail_restart = True

        returned = self.service.update_managed_channel(
            self.connection,
            old_channel.id,
            ChannelWrite(uav_udp_port=5762, ground_station_port=14554),
        )

        stored = self.channel_services.get_channel(self.connection, old_channel.id)
        self.assertEqual((stored.uav_udp_port, stored.ground_station_port), (5761, 14553))
        self.assertTrue(old_rule_ids.issubset({record.rule_id for record in self._records(old_channel.id)}))
        self.assertEqual({rule.port_range for rule in self.provider.rules}, {"5761/5761", "14553/14553"})
        self.assertEqual(returned.security_group.state, "error")

    def test_update_reports_cleanup_pending_when_old_rule_revoke_fails(self):
        old_channel = self._create()
        old_rule_ids = {record.rule_id for record in self._records(old_channel.id)}
        self.provider.fail_revoke_ids = set(old_rule_ids)

        updated = self.service.update_managed_channel(
            self.connection,
            old_channel.id,
            ChannelWrite(uav_udp_port=5762, ground_station_port=14554),
        )

        self.assertEqual((updated.uav_udp_port, updated.ground_station_port), (5762, 14554))
        self.assertEqual(updated.security_group.state, "cleanup_pending")
        stale_records = [record for record in self._records(old_channel.id) if record.rule_id in old_rule_ids]
        self.assertEqual(len(stale_records), len(old_rule_ids))
        self.assertTrue(all(record.state == "pending_revoke" for record in stale_records))

    def test_disabling_channel_stops_runtime_before_revoking_ingress(self):
        channel = self._create()
        self.events.clear()

        updated = self.service.update_managed_channel(
            self.connection,
            channel.id,
            ChannelWrite(
                uav_udp_port=5761,
                ground_station_port=14553,
                ground_station_protocol="tcp",
                enabled=False,
            ),
        )

        capture_stop = self.events.index(("capture", "stop"))
        router_stop = self.events.index(("router", "stop"))
        first_revoke = next(index for index, event in enumerate(self.events) if event[0] == "revoke")
        self.assertLess(capture_stop, first_revoke)
        self.assertLess(router_stop, first_revoke)
        self.assertFalse(updated.enabled)
        self.assertEqual(updated.security_group.state, "synced")

    def test_delete_stops_services_before_revoke_and_preserves_records_on_failure(self):
        channel = self._create()
        record_count = len(self._records(channel.id))
        self.provider.fail_revoke_ids = {self._records(channel.id)[0].rule_id}

        with self.assertRaises(self.service.ChannelSecurityGroupFailure):
            self.service.delete_managed_channel(self.connection, channel.id)

        capture_stop = self.events.index(("capture", "stop"))
        router_stop = self.events.index(("router", "stop"))
        revoke = next(index for index, event in enumerate(self.events) if event[0] == "revoke")
        self.assertLess(capture_stop, router_stop)
        self.assertLess(router_stop, revoke)
        remaining = self._records(channel.id)
        self.assertEqual(len(remaining), record_count - 1)
        self.assertEqual(remaining[0].state, "pending_revoke")
        self.assertIn(remaining[0].rule_id, self.provider.fail_revoke_ids)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM port_channels WHERE id = ?", (str(channel.id),)
            ).fetchone()[0],
            1,
        )

    def test_delete_keeps_rules_when_capture_stop_fails(self):
        channel = self._create()
        self.events.clear()
        self.capture.fail_stop = True

        with self.assertRaises(self.capture_manager.CaptureManagerError):
            self.service.delete_managed_channel(self.connection, channel.id)

        self.assertEqual(self.events, [("capture", "stop")])
        self.assertEqual(len(self._records(channel.id)), 2)

    def test_delete_retries_after_rule_was_already_removed_in_cloud(self):
        channel = self._create()
        stale_id = self._records(channel.id)[0].rule_id
        self.provider.rules = [rule for rule in self.provider.rules if rule.rule_id != stale_id]

        self.service.delete_managed_channel(self.connection, channel.id)

        self.assertIsNone(
            self.connection.execute(
                "SELECT id FROM port_channels WHERE id = ?", (str(channel.id),)
            ).fetchone()
        )
        self.assertEqual(self.provider.rules, [])

    def test_startup_reconciliation_never_mutates_security_group(self):
        from flightlink_backend import main

        provider = self.provider
        class FakeConnection:
            def close(self):
                return None

        class FakeRetentionWorker:
            def __init__(self, interval):
                return None
            def start(self):
                return None
            def stop(self):
                return None

        async def run_lifespan():
            async with main.lifespan(main.app):
                return None

        with (
            patch.object(main, "initialize_database"),
            patch.object(main, "connect", return_value=FakeConnection()),
            patch.object(main, "synchronize_channel_configs"),
            patch.object(main, "list_channels", return_value=[]),
            patch.object(main, "get_mavlink_telemetry_monitor", return_value=self.telemetry),
            patch.object(main, "CaptureRetentionWorker", FakeRetentionWorker),
            patch.object(self.security_group_rules, "get_security_group_provider", side_effect=AssertionError("startup must not call cloud APIs")),
        ):
            asyncio.run(run_lifespan())

        self.assertEqual(provider.rules, [])
        self.assertFalse(any(event[0] in {"authorize", "revoke"} for event in self.events))


if __name__ == "__main__":
    unittest.main()
