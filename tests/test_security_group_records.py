from __future__ import annotations

import importlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from flightlink_backend import database
from flightlink_backend.schemas.channels import ChannelPublic


class SecurityGroupRecordTests(unittest.TestCase):
    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self, path: Path, *, schema_version: int | None = None) -> None:
        with patch.object(
            database, "connect", side_effect=lambda: self._connect(path)
        ):
            if schema_version is None:
                database.initialize_database()
            else:
                with patch.object(database, "SCHEMA_VERSION", schema_version):
                    database.initialize_database()

    def _load_records_service(self):
        try:
            return importlib.import_module(
                "flightlink_backend.services.security_group_records"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Security-group record service is missing: {exc.name}")

    def _insert_channel(self, connection: sqlite3.Connection, channel_id: UUID) -> None:
        connection.execute(
            """
            INSERT INTO port_channels(
                id, uav_udp_port, ground_station_port, ground_station_protocol,
                enabled, created_at, updated_at, monitor_udp_port
            ) VALUES (?, 5761, 14553, 'tcp', 1, 100, 200, 40001)
            """,
            (str(channel_id),),
        )

    def test_schema_v3_migrates_channels_to_pending_security_group_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "schema-v3.sqlite3"
            self._initialize(path, schema_version=3)

            channel_id = uuid4()
            connection = self._connect(path)
            self._insert_channel(connection, channel_id)
            connection.execute(
                "INSERT INTO channel_listeners(transport, port, channel_id, role) "
                "VALUES ('udp', 5761, ?, 'uav')",
                (str(channel_id),),
            )
            connection.execute(
                "INSERT INTO channel_listeners(transport, port, channel_id, role) "
                "VALUES ('tcp', 14553, ?, 'ground_station')",
                (str(channel_id),),
            )
            connection.close()

            self._initialize(path)
            connection = self._connect(path)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                channel = connection.execute(
                    "SELECT * FROM port_channels WHERE id = ?", (str(channel_id),)
                ).fetchone()
                listeners = connection.execute(
                    "SELECT transport, port, role FROM channel_listeners "
                    "WHERE channel_id = ? ORDER BY role",
                    (str(channel_id),),
                ).fetchall()
                rule_table = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'security_group_rules'"
                ).fetchone()

                self.assertEqual(version, 4)
                self.assertEqual(channel["uav_udp_port"], 5761)
                self.assertEqual(channel["ground_station_port"], 14553)
                self.assertEqual(channel["security_group_state"], "pending")
                self.assertIsNone(channel["security_group_error"])
                self.assertEqual(len(listeners), 2)
                self.assertIsNotNone(rule_table)
            finally:
                connection.close()

    def test_managed_rule_rows_preserve_channel_until_rules_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "records.sqlite3"
            self._initialize(path)
            connection = self._connect(path)
            try:
                channel_id = uuid4()
                self._insert_channel(connection, channel_id)
                records = self._load_records_service()
                record_type = getattr(records, "SecurityGroupRuleRecord", None)
                self.assertTrue(callable(record_type))

                record = record_type(
                    id=None,
                    rule_id="sgr-managed-1",
                    channel_id=channel_id,
                    role="uav",
                    protocol="udp",
                    port=5761,
                    source_cidr="0.0.0.0/0",
                    description=f"FlightLink channel {channel_id} uav",
                    state="active",
                )
                record_id = records.save_rule_record(connection, record)

                self.assertGreater(record_id, 0)
                self.assertEqual(
                    records.list_rule_records(connection, channel_id),
                    [replace(record, id=record_id)],
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "DELETE FROM port_channels WHERE id = ?", (str(channel_id),)
                    )

                records.remove_rule_record(connection, record_id)
                self.assertEqual(records.list_rule_records(connection, channel_id), [])
                connection.execute(
                    "DELETE FROM port_channels WHERE id = ?", (str(channel_id),)
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT id FROM port_channels WHERE id = ?", (str(channel_id),)
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_channel_public_includes_security_group_state(self) -> None:
        self.assertIn("security_group", ChannelPublic.model_fields)

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "public-state.sqlite3"
            self._initialize(path)
            connection = self._connect(path)
            try:
                channel_id = uuid4()
                self._insert_channel(connection, channel_id)
                records = self._load_records_service()
                set_state = getattr(
                    records, "set_channel_security_group_state", None
                )
                self.assertTrue(callable(set_state))
                set_state(
                    connection,
                    channel_id,
                    "cleanup_pending",
                    "old rule cleanup failed",
                )

                from flightlink_backend.services.channels import get_channel

                public_channel = get_channel(connection, channel_id)
                self.assertEqual(
                    public_channel.security_group.state, "cleanup_pending"
                )
                self.assertEqual(
                    public_channel.security_group.error,
                    "old rule cleanup failed",
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
