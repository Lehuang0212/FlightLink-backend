import unittest
from types import SimpleNamespace
from uuid import uuid4

from flightlink_backend.services.telemetry import MavlinkTelemetryMonitor, _ChannelState
from flightlink_backend.schemas.channels import PacketSummaryPublic


class CursorTests(unittest.TestCase):
    def test_increment_reset_and_expiry(self):
        monitor, channel, state = MavlinkTelemetryMonitor(), uuid4(), _ChannelState()
        monitor._listeners[channel] = SimpleNamespace(state=state)

        def read(cursor=None):
            return monitor.history(channel, 'packets', 2, cursor)

        def append(text):
            state.consume_packet_summary(PacketSummaryPublic(summary=text, captured_at='now'), uav_udp_port=5000, ground_station_port=5001)

        initial = read()['cursor']
        append('one')
        update = read(initial)
        self.assertFalse(update['reset'])
        self.assertEqual([p.summary for p in update['packets']], ['one'])
        self.assertEqual(read(update['cursor'])['packets'], [])
        append('two')
        append('three')
        self.assertTrue(read(initial)['reset'])
        monitor._listeners[channel] = SimpleNamespace(state=_ChannelState())
        self.assertTrue(read(update['cursor'])['reset'])
