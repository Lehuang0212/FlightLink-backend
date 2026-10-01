from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from flightlink_backend.config import settings
from flightlink_backend.services import capture_files


class CaptureRetentionLoggingTests(unittest.TestCase):
    def test_deployment_grants_only_capture_group_write_access_for_cleanup(self):
        backend_root = Path(__file__).resolve().parents[1]
        installer = (backend_root / "deploy" / "flightlink-run").read_text(encoding="utf-8")
        api_service = (
            backend_root / "deploy" / "systemd" / "flightlink-api.service"
        ).read_text(encoding="utf-8")

        self.assertIn('install -d -o flightlink-capture -g flightlink-capture -m 2770 "$capture_dir"', installer)
        self.assertIn('chmod 2770 "$capture_dir"', installer)
        self.assertIn("SupplementaryGroups=flightlink-router flightlink-capture", api_service)
        self.assertNotIn('chmod 2777 "$capture_dir"', installer)

    def test_failed_file_unlink_is_logged_with_path_and_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            channel_id = uuid4()
            path = directory / f"channel-{channel_id}-12345678-20200101-000000-1577836800.pcap"
            path.write_bytes(b"pcap")
            os.utime(path, (1, 1))
            config = replace(
                settings,
                capture_dir=directory,
                pcap_retention_seconds=1,
                pcap_max_bytes=1024,
            )
            original_unlink = Path.unlink

            def deny_capture_unlink(target, *args, **kwargs):
                if target == path:
                    raise PermissionError("permission denied")
                return original_unlink(target, *args, **kwargs)

            with patch.object(capture_files, "settings", config), patch.object(
                Path, "unlink", deny_capture_unlink
            ), self.assertLogs(capture_files.logger, level="WARNING") as captured:
                self.assertEqual(capture_files.clean_capture_files(), (0, 0))

            self.assertTrue(path.exists())
            self.assertIn(str(path), captured.output[0])
            self.assertIn("permission denied", captured.output[0])


if __name__ == "__main__":
    unittest.main()
