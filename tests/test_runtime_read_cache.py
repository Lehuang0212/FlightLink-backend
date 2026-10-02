from __future__ import annotations

import subprocess
import runpy
import io
from contextlib import redirect_stdout
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from flightlink_backend.services.router_manager import SystemdRouterManager
from flightlink_backend.services.capture_manager import SystemdCaptureManager


class RuntimeReadCacheTests(unittest.TestCase):
    def test_status_reads_are_cached_but_lifecycle_status_remains_fresh(self):
        for manager in (SystemdRouterManager(), SystemdCaptureManager()):
            with self.subTest(manager=type(manager).__name__), patch(
                'subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'ActiveState=active\nMainPID=42\n', '')
            ) as run, patch('flightlink_backend.services.read_cache.time.monotonic', return_value=100) as clock:
                channel = uuid4()
                self.assertEqual(manager.read_status(channel).pid, 42)
                manager.read_status(channel)
                self.assertEqual(run.call_count, 1)
                manager.status(channel)
                self.assertEqual(run.call_count, 2)
                clock.return_value = 104
                manager.read_status(channel)
                self.assertEqual(run.call_count, 3)
                manager.restart(channel)
                manager.read_status(channel)
                self.assertEqual(run.call_count, 5)

    def test_router_log_reads_are_cached_and_invalidated_by_restart(self):
        manager = SystemdRouterManager()
        channel = uuid4()
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'UDP Endpoint [4]uav {\n\tTotal: 1\n}\n', '')) as run:
            manager.logs(channel, 500)
            manager.logs(channel, 500)
            self.assertEqual(run.call_count, 1)
            manager.restart(channel)
            manager.logs(channel, 500)
            self.assertEqual(run.call_count, 3)

    def test_helper_uses_raw_messages_and_current_service_invocation(self):
        helper = (Path(__file__).resolve().parents[1] / 'deploy/sbin/flightlink-routerctl').read_text(encoding='utf-8')
        self.assertIn('--output=cat', helper)
        self.assertIn('_SYSTEMD_INVOCATION_ID=', helper)

    def test_helper_preserves_indentation_and_filters_to_current_run(self):
        helper = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/sbin/flightlink-routerctl'))
        invocation = 'a' * 32
        raw = 'UDP Endpoint [4]uav {\n\tReceived messages {\n\t\tTotal: 1\n\t}\n}\n'
        with patch('subprocess.run', side_effect=[
            subprocess.CompletedProcess([], 0, invocation, ''),
            subprocess.CompletedProcess([], 0, raw, ''),
        ]) as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(helper['main'](['logs', str(uuid4()), '500']), 0)
            self.assertEqual(output.getvalue(), raw)
            command = run.call_args_list[1].args[0]
            self.assertIn(f'_SYSTEMD_INVOCATION_ID={invocation}', command)
            self.assertIn('--output=cat', command)
