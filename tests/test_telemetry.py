from __future__ import annotations

import unittest
from unittest.mock import patch

from flightlink_backend.services import telemetry


class FakeMessage:
    def __init__(self, message_type: str, system_id: int, sequence: int, fields=None):
        self.message_type = message_type
        self.system_id = system_id
        self.sequence = sequence
        self.fields = fields or {}

    def get_type(self):
        return self.message_type

    def to_dict(self):
        return dict(self.fields)

    def get_srcSystem(self):
        return self.system_id

    def get_srcComponent(self):
        return 1

    def get_seq(self):
        return self.sequence

    def get_msgbuf(self):
        return b"mavlink-frame"


def heartbeat(system_id: int, sequence: int = 0) -> FakeMessage:
    return FakeMessage(
        "HEARTBEAT",
        system_id,
        sequence,
        {"type": 2, "autopilot": 3, "base_mode": 128, "custom_mode": 3},
    )


class TelemetrySequenceTests(unittest.TestCase):
    def test_reordered_and_duplicate_sequences_do_not_create_false_loss(self):
        state = telemetry._ChannelState()

        for now, sequence in enumerate((100, 101, 100, 102), start=1):
            state._record_vehicle_packet(sequence, 20, float(now))

        self.assertEqual(state.packets_received_weighted, 3)
        self.assertEqual(state.packets_lost_weighted, 0)
        self.assertEqual(state.last_sequence, 102)
        self.assertEqual(state.link_quality_percent, 100)

    def test_sequence_wrap_and_real_gap_are_counted(self):
        wrapped = telemetry._ChannelState()
        for now, sequence in enumerate((254, 255, 0), start=1):
            wrapped._record_vehicle_packet(sequence, 20, float(now))
        self.assertEqual(wrapped.packets_lost_weighted, 0)
        self.assertEqual(wrapped.packets_received_weighted, 3)

        missing = telemetry._ChannelState()
        missing._record_vehicle_packet(10, 20, 1.0)
        missing._record_vehicle_packet(13, 20, 2.0)
        self.assertEqual(missing.packets_lost_weighted, 2)
        self.assertEqual(missing.link_quality_percent, 50)


class TelemetryAircraftSessionTests(unittest.TestCase):
    def test_new_system_rebinds_after_old_heartbeat_times_out_and_clears_old_data(self):
        state = telemetry._ChannelState()
        with patch.object(telemetry.time, "monotonic", return_value=100.0):
            state.consume(heartbeat(1, 10))
        with patch.object(telemetry.time, "monotonic", return_value=101.0):
            state.consume(
                FakeMessage("SYS_STATUS", 1, 11, {"battery_remaining": 82, "voltage_battery": 12000})
            )
        with patch.object(telemetry.time, "monotonic", return_value=106.0):
            state.consume(heartbeat(2, 4))
        with patch.object(telemetry.time, "monotonic", return_value=106.1):
            snapshot = state.snapshot(ground_station_protocol="tcp", ground_station_port=14552)

        self.assertEqual(snapshot.system_id, 2)
        self.assertTrue(snapshot.uav_online)
        self.assertIsNone(snapshot.battery_remaining_percent)
        self.assertEqual(state.last_sequence, 4)
        self.assertEqual(state.packets_received_weighted, 1)

    def test_second_system_does_not_replace_an_online_primary(self):
        state = telemetry._ChannelState()
        with patch.object(telemetry.time, "monotonic", return_value=100.0):
            state.consume(heartbeat(1))
        with patch.object(telemetry.time, "monotonic", return_value=102.0):
            state.consume(heartbeat(2))
        with patch.object(telemetry.time, "monotonic", return_value=102.1):
            snapshot = state.snapshot(ground_station_protocol="tcp", ground_station_port=14552)

        self.assertEqual(snapshot.system_id, 1)
        self.assertTrue(snapshot.uav_online)

    def test_same_system_reboot_after_timeout_starts_a_fresh_session(self):
        state = telemetry._ChannelState()
        with patch.object(telemetry.time, "monotonic", return_value=100.0):
            state.consume(heartbeat(1, 80))
        with patch.object(telemetry.time, "monotonic", return_value=101.0):
            state.consume(FakeMessage("SYS_STATUS", 1, 81, {"battery_remaining": 70}))
        with patch.object(telemetry.time, "monotonic", return_value=106.0):
            state.consume(heartbeat(1, 0))

        self.assertIsNone(state.battery_remaining_percent)
        self.assertEqual(state.last_sequence, 0)
        self.assertEqual(state.packets_received_weighted, 1)


if __name__ == "__main__":
    unittest.main()
