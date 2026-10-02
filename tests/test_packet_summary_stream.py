from __future__ import annotations

import runpy
import unittest
from pathlib import Path
from unittest.mock import patch

from flightlink_backend.services.telemetry import _parse_packet_summary


class PacketSummaryStreamTests(unittest.TestCase):
    def assembler(self):
        helper = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/sbin/flightlink-capture-runner'))
        return helper['PacketSummaryAssembler']()

    def test_verbose_udp_lines_form_one_packet_with_ip_ports_and_timestamp(self):
        stream = self.assembler()
        self.assertIsNone(stream.feed(b'1790910000.123456 IP (tos 0x0, ttl 64, proto UDP (17), length 70)'))
        self.assertIsNone(stream.feed(b'    117.1.2.3.38211 > 10.0.0.5.5760: UDP, length 42'))
        raw = stream.flush()
        packet = _parse_packet_summary(raw)
        self.assertEqual(packet.source, '117.1.2.3:38211')
        self.assertEqual(packet.destination, '10.0.0.5:5760')
        self.assertEqual(packet.protocol, 'UDP')
        self.assertEqual(packet.length_bytes, 70)
        self.assertEqual(packet.captured_at, '2026-10-02T03:00:00.123456+00:00')
        self.assertIsNone(stream.flush())

    def test_tcp_ipv6_and_adjacent_packets_do_not_mix(self):
        stream = self.assembler()
        stream.feed(b'1790910000.1 IP6 (hlim 64, next-header TCP (6) payload length: 20)')
        stream.feed(b'    2001:db8::1.59231 > 2001:db8::2.14552: Flags [S], seq 1, length 0')
        first = stream.feed(b'1790910001.2 IP 117.1.2.3.38211 > 10.0.0.5.5760: UDP, length 42')
        packet = _parse_packet_summary(first)
        self.assertEqual(packet.source, '[2001:db8::1]:59231')
        self.assertEqual(packet.destination, '[2001:db8::2]:14552')
        self.assertEqual(packet.protocol, 'TCP')
        self.assertNotIn('117.1.2.3', packet.summary)
        self.assertEqual(_parse_packet_summary(stream.flush()).source, '117.1.2.3:38211')

    def test_orphan_lines_are_not_emitted_as_packets_and_buffer_is_bounded(self):
        stream = self.assembler()
        self.assertIsNone(stream.feed(b'    117.1.2.3.1234 > 10.0.0.5.5760: UDP, length 42'))
        self.assertIsNone(stream.flush())
        stream.feed(b'1790910000.1 IP 117.1.2.3.1234 > 10.0.0.5.5760: UDP, length 42')
        for _ in range(20):
            stream.feed(b'    ' + b'x' * 500)
        self.assertLessEqual(len(stream.flush()), 1000)

    def test_quiet_stream_emits_last_packet_once_after_short_idle(self):
        stream = self.assembler()
        with patch('time.monotonic', return_value=100.0) as clock:
            stream.feed(b'1790910000.1 IP (ttl 64, proto UDP (17), length 70)')
            stream.feed(b'    117.1.2.3.38211 > 10.0.0.5.5760: UDP, length 42')
            clock.return_value = 100.1
            self.assertIsNone(stream.flush_if_idle())
            clock.return_value = 100.21
            packet = _parse_packet_summary(stream.flush_if_idle())
            self.assertEqual(packet.source, '117.1.2.3:38211')
            self.assertIsNone(stream.flush_if_idle())
