from contextlib import redirect_stdout
from io import StringIO
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from flightlink_backend import maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'example.sqlite3'
        self.config = NS(database_path=self.path, security_group_provider='aliyun', aliyun_region_id='region', aliyun_security_group_id='group')
        self.enterContext(patch.object(maintenance, 'settings', self.config))
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE port_channels(runtime_intent, security_group_state);
            INSERT INTO port_channels VALUES('applied','synced');
            CREATE TABLE security_group_rules(id, channel_id, role, protocol, port, source_cidr, rule_id);
            INSERT INTO security_group_rules VALUES(1,'chan','uav','udp',5700,'192.0.2.0/24','rule1');
        """)
        self.rule = NS(rule_id='rule1', description='FlightLink flightlink:channel:chan:role:uav managed ingress rule', direction='ingress', policy='accept', protocol='UDP', source_cidr='192.0.2.0/24', port_range='5700/5700')
        self.provider = self.enterContext(patch('flightlink_backend.services.aliyun_security_group.get_security_group_provider')).return_value
        self.provider.describe_security_group.return_value = NS(rules=[self.rule])

    def test_preview_does_not_change_cloud_or_intent(self):
        with redirect_stdout(StringIO()) as output:
            maintenance.main('rules')
        self.assertIn('region group', output.getvalue())
        self.provider.revoke_rule.assert_not_called()
        self.assertEqual(self.db.execute('SELECT runtime_intent FROM port_channels').fetchone()[0], 'applied')

    def test_no_revoke_when_ownership_changed(self):
        self.rule.description = 'manually managed'
        with self.assertRaises(RuntimeError):
            maintenance.main('clean')
        self.provider.revoke_rule.assert_not_called()
        self.assertEqual(self.db.execute('SELECT count(*) FROM security_group_rules').fetchone()[0], 1)

    def test_failed_verification_retains_mapping_for_retry(self):
        with self.assertRaises(RuntimeError):
            maintenance.main('clean')
        self.assertEqual(self.db.execute('SELECT count(*) FROM security_group_rules').fetchone()[0], 1)
        self.provider.describe_security_group.side_effect = [NS(rules=[self.rule]), NS(rules=[])]
        maintenance.main('clean')
        self.assertEqual(self.db.execute('SELECT count(*) FROM security_group_rules').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT runtime_intent FROM port_channels').fetchone()[0], 'blocked')
