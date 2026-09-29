from __future__ import annotations

import importlib
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from flightlink_backend.schemas.channels import ChannelWrite
from flightlink_backend.services.aliyun_security_group import (
    SecurityGroupRule,
    SecurityGroupSnapshot,
)


class FakeSecurityGroupProvider:
    enabled = True

    def __init__(
        self,
        rules=None,
        *,
        fail_authorize_protocol=None,
        error_after_authorize_protocol=None,
        hide_authorized=False,
        fail_revoke=False,
    ):
        self.rules = list(rules or [])
        self.fail_authorize_protocol = fail_authorize_protocol
        self.error_after_authorize_protocol = error_after_authorize_protocol
        self.hide_authorized = hide_authorized
        self.fail_revoke = fail_revoke
        self.authorized = []
        self.revoked = []
        self.next_rule_id = 1
        self.fail_describe = False

    def describe_security_group(self):
        if self.fail_describe:
            raise RuntimeError("sensitive SDK response")
        return SecurityGroupSnapshot(
            region_id="cn-hangzhou",
            security_group_id="sg-test",
            security_group_name="test",
            rules=list(self.rules),
        )

    def authorize_rule(self, rule):
        if rule.protocol.lower() == self.fail_authorize_protocol:
            raise RuntimeError("sensitive SDK response")
        self.authorized.append(rule)
        if not self.hide_authorized:
            rule_id = f"sgr-created-{self.next_rule_id}"
            self.next_rule_id += 1
            self.rules.append(
                SecurityGroupRule(
                    rule_id=rule_id,
                    protocol=rule.protocol.upper(),
                    port_range=f"{rule.port}/{rule.port}",
                    source_cidr=rule.source_cidr,
                    policy="Accept",
                    description=rule.description,
                    direction="ingress",
                )
            )
        if rule.protocol.lower() == self.error_after_authorize_protocol:
            raise RuntimeError("sensitive SDK response after successful mutation")

    def revoke_rule(self, rule_id):
        self.revoked.append(rule_id)
        if self.fail_revoke:
            raise RuntimeError("sensitive SDK response")
        for index, rule in enumerate(self.rules):
            if rule.rule_id == rule_id:
                del self.rules[index]
                return
        raise RuntimeError("InvalidSecurityGroupRuleId.NotFound")


class SecurityGroupRuleReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE port_channels (
                id TEXT PRIMARY KEY,
                security_group_state TEXT NOT NULL DEFAULT 'pending',
                security_group_error TEXT
            );
            CREATE TABLE security_group_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id TEXT UNIQUE,
                channel_id TEXT NOT NULL REFERENCES port_channels(id) ON DELETE RESTRICT,
                role TEXT NOT NULL,
                protocol TEXT NOT NULL,
                port INTEGER NOT NULL,
                source_cidr TEXT NOT NULL,
                description TEXT NOT NULL,
                state TEXT NOT NULL
            );
            """
        )
        self.channel_id = uuid4()
        self.connection.execute(
            "INSERT INTO port_channels(id) VALUES (?)", (str(self.channel_id),)
        )
        self.service = importlib.import_module(
            "flightlink_backend.services.security_group_rules"
        )
        from flightlink_backend.config import settings

        self.config = replace(
            settings,
            security_group_provider="aliyun",
            aliyun_uav_source_cidrs="0.0.0.0/0",
            aliyun_gcs_source_cidrs="198.51.100.7/32, 203.0.113.0/24",
        )
        self.payload = ChannelWrite(
            uav_udp_port=5761,
            ground_station_port=14553,
            ground_station_protocol="tcp",
        )

    def tearDown(self):
        self.connection.close()

    def _ensure(self, provider, payload=None):
        with patch.object(self.service, "settings", self.config), patch.object(
            self.service, "get_security_group_provider", return_value=provider
        ):
            return self.service.ensure_channel_rules(
                self.connection, self.channel_id, payload or self.payload
            )

    def _revoke(self, provider):
        with patch.object(self.service, "settings", self.config), patch.object(
            self.service, "get_security_group_provider", return_value=provider
        ):
            return self.service.revoke_channel_rules(self.connection, self.channel_id)

    def _records(self):
        return self.service.list_rule_records(self.connection, self.channel_id)

    def test_ensure_opens_only_exact_uav_and_gcs_ports_for_configured_cidrs(self):
        provider = FakeSecurityGroupProvider()

        result = self._ensure(provider)

        self.assertEqual(result.state, "synced")
        self.assertEqual(
            [
                (rule.protocol.lower(), rule.port, rule.source_cidr)
                for rule in provider.authorized
            ],
            [
                ("udp", 5761, "0.0.0.0/0"),
                ("tcp", 14553, "198.51.100.7/32"),
                ("tcp", 14553, "203.0.113.0/24"),
            ],
        )
        self.assertTrue(
            all("flightlink:channel:" in rule.description for rule in provider.authorized)
        )
        self.assertEqual(
            {record.role for record in self._records()}, {"uav", "ground_station"}
        )

    def test_matching_external_rule_is_reused_but_never_recorded_as_owned(self):
        provider = FakeSecurityGroupProvider(
            [
                SecurityGroupRule(
                    rule_id="sgr-external",
                    protocol="UDP",
                    port_range="5761/5761",
                    source_cidr="0.0.0.0/0",
                    policy="Accept",
                    description="manually managed by operator",
                    direction="ingress",
                )
            ]
        )

        result = self._ensure(provider)

        self.assertEqual(result.state, "synced")
        self.assertEqual(provider.authorized[0].port, 14553)
        self.assertNotIn("sgr-external", [record.rule_id for record in self._records()])

    def test_managed_rule_id_is_read_back_and_persisted_after_authorize(self):
        provider = FakeSecurityGroupProvider()

        result = self._ensure(provider)

        records = self._records()
        self.assertEqual(result.state, "synced")
        self.assertEqual(len(records), 3)  # One UAV rule and two configured GCS CIDRs.
        self.assertTrue(all(record.rule_id for record in records))
        self.assertEqual({record.state for record in records}, {"active"})
        self.assertEqual(set(result.managed_rule_ids), {record.rule_id for record in records})

    def test_authorize_success_without_readback_is_saved_as_pending_error(self):
        provider = FakeSecurityGroupProvider(hide_authorized=True)

        result = self._ensure(provider)

        records = self._records()
        self.assertEqual(result.state, "error")
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].rule_id)
        self.assertEqual(records[0].state, "pending_discovery")
        self.assertNotIn("sensitive", result.error or "")

    def test_authorize_error_is_reconciled_when_rule_is_visible_afterward(self):
        provider = FakeSecurityGroupProvider(error_after_authorize_protocol="udp")

        result = self._ensure(provider)

        uav_record = next(record for record in self._records() if record.role == "uav")
        self.assertEqual(result.state, "synced")
        self.assertEqual(uav_record.state, "active")
        self.assertEqual(uav_record.rule_id, "sgr-created-1")

    def test_partial_authorize_failure_keeps_successful_rule_for_retry(self):
        provider = FakeSecurityGroupProvider(fail_authorize_protocol="tcp")

        result = self._ensure(provider)

        records = self._records()
        self.assertEqual(result.state, "error")
        self.assertEqual(len(records), 2)
        by_role = {record.role: record for record in records}
        self.assertEqual(by_role["uav"].state, "active")
        self.assertIsNotNone(by_role["uav"].rule_id)
        self.assertEqual(by_role["ground_station"].state, "pending_discovery")
        self.assertIsNone(by_role["ground_station"].rule_id)

    def test_revoke_uses_only_persisted_rule_ids(self):
        provider = FakeSecurityGroupProvider(
            [
                SecurityGroupRule(
                    rule_id="sgr-external",
                    protocol="UDP",
                    port_range="5000/5000",
                    source_cidr="0.0.0.0/0",
                    policy="Accept",
                    description="external rule",
                    direction="ingress",
                )
            ]
        )
        record = self.service.SecurityGroupRuleRecord(
            id=None,
            rule_id="sgr-owned",
            channel_id=self.channel_id,
            role="uav",
            protocol="udp",
            port=5761,
            source_cidr="0.0.0.0/0",
            description=f"FlightLink channel {self.channel_id} uav",
            state="active",
        )
        self.service.save_rule_record(self.connection, record)
        provider.rules.append(
            SecurityGroupRule(
                rule_id="sgr-owned",
                protocol="UDP",
                port_range="5761/5761",
                source_cidr="0.0.0.0/0",
                policy="Accept",
                description=record.description,
                direction="ingress",
            )
        )

        result = self._revoke(provider)

        self.assertEqual(result.state, "synced")
        self.assertEqual(provider.revoked, ["sgr-owned"])
        self.assertEqual([rule.rule_id for rule in provider.rules], ["sgr-external"])
        self.assertEqual(self._records(), [])

    def test_stale_rule_id_is_reconciled_after_describe_confirms_absent(self):
        provider = FakeSecurityGroupProvider()
        record = self.service.SecurityGroupRuleRecord(
            id=None,
            rule_id="sgr-already-removed",
            channel_id=self.channel_id,
            role="uav",
            protocol="udp",
            port=5761,
            source_cidr="0.0.0.0/0",
            description=f"FlightLink channel {self.channel_id} uav",
            state="active",
        )
        self.service.save_rule_record(self.connection, record)

        result = self._revoke(provider)

        self.assertEqual(result.state, "synced")
        self.assertEqual(provider.revoked, ["sgr-already-removed"])
        self.assertEqual(self._records(), [])

    def test_failed_revoke_keeps_rule_mapping_for_retry(self):
        provider = FakeSecurityGroupProvider(fail_revoke=True)
        record = self.service.SecurityGroupRuleRecord(
            id=None,
            rule_id="sgr-still-active",
            channel_id=self.channel_id,
            role="uav",
            protocol="udp",
            port=5761,
            source_cidr="0.0.0.0/0",
            description=f"FlightLink channel {self.channel_id} uav",
            state="active",
        )
        self.service.save_rule_record(self.connection, record)
        provider.rules.append(
            SecurityGroupRule(
                rule_id="sgr-still-active",
                protocol="UDP",
                port_range="5761/5761",
                source_cidr="0.0.0.0/0",
                policy="Accept",
                description=record.description,
                direction="ingress",
            )
        )

        result = self._revoke(provider)

        saved = self._records()
        self.assertEqual(result.state, "cleanup_pending")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].rule_id, "sgr-still-active")
        self.assertEqual(saved[0].state, "pending_revoke")


if __name__ == "__main__":
    unittest.main()
