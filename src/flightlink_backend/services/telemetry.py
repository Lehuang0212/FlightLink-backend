from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging
import re
import socket
import threading
import time
from typing import Any
from uuid import UUID

from pymavlink.dialects.v20 import ardupilotmega as mavlink2

from ..schemas.channels import (
    ChannelTelemetryPublic,
    FlightMessagePublic,
    PacketSummaryPublic,
    RadioStatusPublic,
)

logger = logging.getLogger(__name__)

UAV_HEARTBEAT_TIMEOUT_SECONDS = 5.0
GCS_HEARTBEAT_TIMEOUT_SECONDS = 10.0
QUALITY_DECAY_SECONDS = 5.0
QUALITY_IDLE_DECAY_SECONDS = 1.0
MESSAGE_HISTORY_LIMIT = 100
PACKET_HISTORY_LIMIT = 500
_PACKET_SUMMARY_PREFIX = b"FLIGHTLINK_PACKET:"
_SUMMARY_ENDPOINTS = re.compile(r"(?P<source>\S+)\s+>\s+(?P<destination>\S+):")
_SUMMARY_LENGTH = re.compile(r"\blength\s+(\d+)")

_SEVERITY_NAMES = {
    0: "EMERGENCY",
    1: "ALERT",
    2: "CRITICAL",
    3: "ERROR",
    4: "WARNING",
    5: "NOTICE",
    6: "INFO",
    7: "DEBUG",
}
_VEHICLE_TYPES = {
    1: "FIXED_WING",
    2: "QUADROTOR",
    3: "COAXIAL",
    4: "HELICOPTER",
    10: "ROVER",
    11: "BOAT",
    12: "SUBMARINE",
    13: "HEXAROTOR",
    14: "OCTOROTOR",
    15: "TRICOPTER",
    19: "VTOL_TILTROTOR",
    20: "VTOL_FIXEDROTOR",
    21: "VTOL_TAILSITTER",
    22: "VTOL_TILTWING",
    26: "DODECAROTOR",
}
_COPTER_MODES = {
    0: "STABILIZE",
    1: "ACRO",
    2: "ALT_HOLD",
    3: "AUTO",
    4: "GUIDED",
    5: "LOITER",
    6: "RTL",
    7: "CIRCLE",
    9: "LAND",
    11: "DRIFT",
    13: "SPORT",
    14: "FLIP",
    15: "AUTOTUNE",
    16: "POSHOLD",
    17: "BRAKE",
    18: "THROW",
    19: "AVOID_ADSB",
    20: "GUIDED_NOGPS",
    21: "SMART_RTL",
    22: "FLOWHOLD",
    23: "FOLLOW",
    24: "ZIGZAG",
    25: "SYSTEMID",
    26: "AUTOROTATE",
    27: "AUTO_RTL",
}
_PLANE_MODES = {
    0: "MANUAL",
    1: "CIRCLE",
    2: "STABILIZE",
    3: "TRAINING",
    4: "ACRO",
    5: "FBWA",
    6: "FBWB",
    7: "CRUISE",
    8: "AUTOTUNE",
    10: "AUTO",
    11: "RTL",
    12: "LOITER",
    13: "TAKEOFF",
    15: "GUIDED",
    16: "INITIALISING",
    17: "QLAND",
    18: "VTOL_TAKEOFF",
}
_ROVER_MODES = {
    0: "MANUAL",
    1: "ACRO",
    3: "STEERING",
    4: "HOLD",
    10: "AUTO",
    11: "RTL",
    12: "SMART_RTL",
    15: "GUIDED",
    16: "INITIALISING",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _decode_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()
    return str(value).split("\0", 1)[0].strip()


def _mode_name(vehicle_type: int, autopilot: int, custom_mode: int) -> str:
    if autopilot == 3:
        modes = (
            _COPTER_MODES
            if vehicle_type in {2, 3, 4, 13, 14, 15, 26}
            else _PLANE_MODES
            if vehicle_type in {1, 19, 20, 21, 22}
            else _ROVER_MODES
            if vehicle_type in {10, 11}
            else {}
        )
        return modes.get(custom_mode, f"MODE_{custom_mode}")
    return f"MODE_{custom_mode}"


def _autopilot_name(value: int) -> str:
    return {3: "ArduPilot", 12: "PX4"}.get(value, f"AUTOPILOT_{value}")


def _decode_address(raw: str, ipv6: bool) -> str:
    address_hex = raw.split(":", 1)[0]
    address_bytes = bytes.fromhex(address_hex)
    if ipv6:
        address_bytes = b"".join(
            address_bytes[index : index + 4][::-1]
            for index in range(0, len(address_bytes), 4)
        )
        return socket.inet_ntop(socket.AF_INET6, address_bytes)
    return socket.inet_ntop(socket.AF_INET, address_bytes[::-1])


def _parse_tcpdump_endpoint(value: str) -> tuple[str, int] | None:
    endpoint = value.strip().strip("[]").rstrip(":")
    host, separator, raw_port = endpoint.rpartition(".")
    if not separator or not host:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        return None
    if not 0 <= port <= 65535:
        return None
    return host, port


def _parse_public_endpoint(value: str) -> tuple[str, int] | None:
    endpoint = value.strip()
    if endpoint.startswith("[") and "]:" in endpoint:
        host, raw_port = endpoint[1:].rsplit("]:", 1)
    else:
        host, separator, raw_port = endpoint.rpartition(":")
        if not separator:
            return None
    try:
        port = int(raw_port)
    except ValueError:
        return None
    if not host or not 0 <= port <= 65535:
        return None
    return host, port


def _parse_packet_summary(raw: bytes) -> PacketSummaryPublic:
    summary = raw.decode("utf-8", errors="replace").strip()[:1000]
    timestamp_match = re.match(r"^(\d{9,}(?:\.\d+)?)\s+", summary)
    captured_at = _utc_now().isoformat()
    if timestamp_match is not None:
        try:
            captured_at = datetime.fromtimestamp(
                float(timestamp_match.group(1)), tz=timezone.utc
            ).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    endpoints_match = _SUMMARY_ENDPOINTS.search(summary)
    source: str | None = None
    destination: str | None = None
    if endpoints_match is not None:
        source_endpoint = _parse_tcpdump_endpoint(endpoints_match.group("source"))
        destination_endpoint = _parse_tcpdump_endpoint(endpoints_match.group("destination"))
        if source_endpoint is not None:
            host, port = source_endpoint
            source = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        if destination_endpoint is not None:
            host, port = destination_endpoint
            destination = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    protocol_match = re.search(r"\b(UDP|TCP)\b", summary, re.IGNORECASE)
    length_match = _SUMMARY_LENGTH.search(summary)
    return PacketSummaryPublic(
        captured_at=captured_at,
        protocol=protocol_match.group(1).upper() if protocol_match else None,
        source=source,
        destination=destination,
        length_bytes=int(length_match.group(1)) if length_match else None,
        summary=summary,
    )


def _tcp_peers_for_port(port: int) -> list[str]:
    """Read established TCP peers from procfs without requiring root or shell tools."""
    peers: list[str] = []
    for table, ipv6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        try:
            with open(table, encoding="ascii") as stream:
                next(stream, None)
                for line in stream:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != "01":
                        continue
                    local_address, remote_address = fields[1], fields[2]
                    local_port = int(local_address.rsplit(":", 1)[1], 16)
                    if local_port != port:
                        continue
                    remote_port = int(remote_address.rsplit(":", 1)[1], 16)
                    remote_ip = _decode_address(remote_address, ipv6)
                    peer = f"[{remote_ip}]:{remote_port}" if ipv6 else f"{remote_ip}:{remote_port}"
                    if peer not in peers:
                        peers.append(peer)
        except (OSError, ValueError, IndexError):
            continue
    return peers


class _ChannelState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.monitor_error: str | None = None
        self.started_at = time.monotonic()
        self.primary_system_id: int | None = None
        self.primary_component_id: int | None = None
        self.vehicle_type: int | None = None
        self.autopilot_id: int | None = None
        self.flight_mode: str | None = None
        self.armed: bool | None = None
        self.battery_remaining_percent: int | None = None
        self.battery_voltage_v: float | None = None
        self.gps_fix_type: int | None = None
        self.gps_satellites: int | None = None
        self.altitude_m: float | None = None
        self.relative_altitude_m: float | None = None
        self.ground_speed_m_s: float | None = None
        self.radio_status: RadioStatusPublic | None = None
        self.last_packet_monotonic: float | None = None
        self.last_packet_at: str | None = None
        self.uav_peer: str | None = None
        self.last_heartbeat_monotonic: float | None = None
        self.last_gcs_packet_monotonic: float | None = None
        self.last_gcs_packet_at: str | None = None
        self.ground_station_udp_peer: str | None = None
        self.gcs_sources: set[tuple[int, int]] = set()
        self.last_sequence: int | None = None
        self.packets_received_weighted = 0.0
        self.packets_lost_weighted = 0.0
        self.last_counter_decay = time.monotonic()
        self.link_quality_percent: float | None = None
        self.last_quality_decay = time.monotonic()
        self.uav_bytes: deque[tuple[float, int]] = deque()
        self.gcs_bytes: deque[tuple[float, int]] = deque()
        self.messages: deque[FlightMessagePublic] = deque(maxlen=MESSAGE_HISTORY_LIMIT)
        self.packet_summaries: deque[PacketSummaryPublic] = deque(maxlen=PACKET_HISTORY_LIMIT)

    def consume_packet_summary(
        self,
        summary: PacketSummaryPublic,
        *,
        uav_udp_port: int,
        ground_station_port: int,
    ) -> None:
        source_endpoint = _parse_public_endpoint(summary.source or "")
        destination_endpoint = _parse_public_endpoint(summary.destination or "")
        now = time.monotonic()
        with self.lock:
            self.packet_summaries.append(summary)
            if (
                summary.protocol == "UDP"
                and source_endpoint is not None
                and destination_endpoint is not None
                and destination_endpoint[1] == uav_udp_port
            ):
                host, port = source_endpoint
                self.uav_peer = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
            if (
                summary.protocol == "UDP"
                and source_endpoint is not None
                and destination_endpoint is not None
                and destination_endpoint[1] == ground_station_port
            ):
                host, port = source_endpoint
                self.ground_station_udp_peer = (
                    f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                )
                self.last_gcs_packet_monotonic = now
                self.last_gcs_packet_at = summary.captured_at

    def _decay_packet_counters(self, now: float) -> None:
        if now - self.last_counter_decay >= QUALITY_DECAY_SECONDS:
            self.packets_received_weighted *= 0.8
            self.packets_lost_weighted *= 0.8
            self.last_counter_decay = now

    def _record_vehicle_packet(self, sequence: int, frame_bytes: int, now: float) -> None:
        self._decay_packet_counters(now)
        if self.last_sequence is not None:
            gap = (sequence - self.last_sequence) & 0xFF
            if gap > 1:
                self.packets_lost_weighted += gap - 1
        self.packets_received_weighted += 1
        self.last_sequence = sequence
        denominator = self.packets_received_weighted + self.packets_lost_weighted
        if denominator > 0:
            self.link_quality_percent = 100.0 * self.packets_received_weighted / denominator
        self.last_quality_decay = now
        self.last_packet_monotonic = now
        self.last_packet_at = _utc_now().isoformat()
        self.uav_bytes.append((now, frame_bytes))
        while self.uav_bytes and self.uav_bytes[0][0] < now - 5.0:
            self.uav_bytes.popleft()

    def _fade_idle_quality(self, now: float) -> None:
        if self.link_quality_percent is None or self.last_packet_monotonic is None:
            return
        if now - self.last_packet_monotonic < QUALITY_IDLE_DECAY_SECONDS:
            return
        elapsed_ticks = int(now - self.last_quality_decay)
        if elapsed_ticks > 0:
            self.link_quality_percent *= 0.8**elapsed_ticks
            self.last_quality_decay = now

    def consume(self, message: Any) -> None:
        message_type = message.get_type()
        if message_type == "BAD_DATA":
            return
        fields = message.to_dict()
        source_system = int(message.get_srcSystem())
        source_component = int(message.get_srcComponent())
        sequence = int(message.get_seq())
        now = time.monotonic()
        frame_bytes = len(message.get_msgbuf())

        with self.lock:
            if message_type == "HEARTBEAT":
                vehicle_type = int(fields.get("type", 0))
                autopilot = int(fields.get("autopilot", 0))
                if vehicle_type == 6:
                    self.last_gcs_packet_monotonic = now
                    self.last_gcs_packet_at = _utc_now().isoformat()
                    if len(self.gcs_sources) < 32:
                        self.gcs_sources.add((source_system, source_component))
                    self.gcs_bytes.append((now, frame_bytes))
                    while self.gcs_bytes and self.gcs_bytes[0][0] < now - 5.0:
                        self.gcs_bytes.popleft()
                    return
                if autopilot != 0 and source_component == 1:
                    if self.primary_system_id is None:
                        self.primary_system_id = source_system
                        self.primary_component_id = source_component
                    if (
                        source_system == self.primary_system_id
                        and source_component == self.primary_component_id
                    ):
                        self.vehicle_type = vehicle_type
                        self.autopilot_id = autopilot
                        self.flight_mode = _mode_name(
                            vehicle_type, autopilot, int(fields.get("custom_mode", 0))
                        )
                        base_mode = int(fields.get("base_mode", 0))
                        self.armed = bool(base_mode & 128)
                        self.last_heartbeat_monotonic = now

            is_primary_vehicle = (
                self.primary_system_id is not None
                and source_system == self.primary_system_id
                and source_component == self.primary_component_id
            )
            if not is_primary_vehicle:
                if (source_system, source_component) in self.gcs_sources:
                    self.gcs_bytes.append((now, frame_bytes))
                    while self.gcs_bytes and self.gcs_bytes[0][0] < now - 5.0:
                        self.gcs_bytes.popleft()
                return

            self._record_vehicle_packet(sequence, frame_bytes, now)
            if message_type == "SYS_STATUS":
                remaining = int(fields.get("battery_remaining", -1))
                voltage_mv = int(fields.get("voltage_battery", 65535))
                if remaining >= 0:
                    self.battery_remaining_percent = remaining
                if voltage_mv != 65535:
                    self.battery_voltage_v = voltage_mv / 1000.0
            elif message_type == "BATTERY_STATUS":
                remaining = int(fields.get("battery_remaining", -1))
                if remaining >= 0:
                    self.battery_remaining_percent = remaining
                voltages = fields.get("voltages") or []
                known_voltages = [int(value) for value in voltages if int(value) != 65535]
                if known_voltages:
                    self.battery_voltage_v = sum(known_voltages) / 1000.0
            elif message_type in {"GPS_RAW_INT", "GPS2_RAW"}:
                self.gps_fix_type = int(fields.get("fix_type", 0))
                satellites = int(fields.get("satellites_visible", 255))
                self.gps_satellites = satellites if satellites != 255 else None
            elif message_type == "GLOBAL_POSITION_INT":
                self.altitude_m = int(fields.get("alt", 0)) / 1000.0
                self.relative_altitude_m = int(fields.get("relative_alt", 0)) / 1000.0
                vx = int(fields.get("vx", 0)) / 100.0
                vy = int(fields.get("vy", 0)) / 100.0
                self.ground_speed_m_s = (vx * vx + vy * vy) ** 0.5
            elif message_type == "VFR_HUD":
                self.ground_speed_m_s = float(fields.get("groundspeed", 0.0))
                self.altitude_m = float(fields.get("alt", 0.0))
            elif message_type == "RADIO_STATUS":
                self.radio_status = RadioStatusPublic(
                    rssi=int(fields.get("rssi", 0)),
                    remote_rssi=int(fields.get("remrssi", 0)),
                    noise=int(fields.get("noise", 0)),
                    remote_noise=int(fields.get("remnoise", 0)),
                    tx_buffer_percent=int(fields.get("txbuf", 0)),
                    receive_errors=int(fields.get("rxerrors", 0)),
                )
            elif message_type == "STATUSTEXT":
                severity_value = int(fields.get("severity", -1))
                message_text = _decode_text(fields.get("text", ""))
                if message_text:
                    self.messages.append(
                        FlightMessagePublic(
                            received_at=_utc_now().isoformat(),
                            severity=severity_value if severity_value >= 0 else None,
                            severity_name=_SEVERITY_NAMES.get(severity_value, "UNKNOWN"),
                            text=message_text,
                        )
                    )

    def snapshot(
        self,
        *,
        ground_station_protocol: str,
        ground_station_port: int,
    ) -> ChannelTelemetryPublic:
        now = time.monotonic()
        with self.lock:
            self._fade_idle_quality(now)
            cutoff = now - 5.0
            while self.uav_bytes and self.uav_bytes[0][0] < cutoff:
                self.uav_bytes.popleft()
            while self.gcs_bytes and self.gcs_bytes[0][0] < cutoff:
                self.gcs_bytes.popleft()
            span = min(5.0, max(1.0, now - self.started_at))
            uav_rate = sum(size for _, size in self.uav_bytes) / span
            gcs_rate = sum(size for _, size in self.gcs_bytes) / span
            packet_age = (
                max(0.0, now - self.last_packet_monotonic)
                if self.last_packet_monotonic is not None
                else None
            )
            heartbeat_age = (
                max(0.0, now - self.last_heartbeat_monotonic)
                if self.last_heartbeat_monotonic is not None
                else None
            )
            gcs_age = (
                max(0.0, now - self.last_gcs_packet_monotonic)
                if self.last_gcs_packet_monotonic is not None
                else None
            )
            quality = self.link_quality_percent
            if quality is not None:
                quality = min(100.0, max(0.0, quality))
            if ground_station_protocol == "tcp":
                peers = _tcp_peers_for_port(ground_station_port)
                gcs_connected = bool(peers)
                gcs_peer = peers[0] if peers else None
            else:
                gcs_connected = gcs_age is not None and gcs_age <= GCS_HEARTBEAT_TIMEOUT_SECONDS
                gcs_peer = self.ground_station_udp_peer
            return ChannelTelemetryPublic(
                monitor_state="listening" if self.monitor_error is None else "unavailable",
                monitor_error=self.monitor_error,
                uav_online=heartbeat_age is not None and heartbeat_age <= UAV_HEARTBEAT_TIMEOUT_SECONDS,
                uav_peer=self.uav_peer,
                system_id=self.primary_system_id,
                component_id=self.primary_component_id,
                vehicle_type=(
                    _VEHICLE_TYPES.get(self.vehicle_type, f"TYPE_{self.vehicle_type}")
                    if self.vehicle_type is not None
                    else None
                ),
                autopilot=(
                    _autopilot_name(self.autopilot_id)
                    if self.autopilot_id is not None
                    else None
                ),
                flight_mode=self.flight_mode,
                armed=self.armed,
                battery_remaining_percent=self.battery_remaining_percent,
                battery_voltage_v=self.battery_voltage_v,
                gps_fix_type=self.gps_fix_type,
                gps_satellites=self.gps_satellites,
                altitude_m=self.altitude_m,
                relative_altitude_m=self.relative_altitude_m,
                ground_speed_m_s=self.ground_speed_m_s,
                link_quality_percent=quality,
                packets_received_window=round(self.packets_received_weighted),
                packets_lost_window=round(self.packets_lost_weighted),
                last_packet_at=self.last_packet_at,
                last_packet_age_seconds=(
                    round(packet_age, 3) if packet_age is not None else None
                ),
                uav_rx_bytes_per_second=round(uav_rate, 1),
                gcs_tx_bytes_per_second=round(gcs_rate, 1),
                ground_station_connected=gcs_connected,
                ground_station_peer=gcs_peer,
                ground_station_last_packet_at=self.last_gcs_packet_at,
                radio_status=self.radio_status,
            )


class _MonitorListener:
    def __init__(
        self,
        channel_id: UUID,
        port: int,
        uav_udp_port: int,
        ground_station_port: int,
    ) -> None:
        self.channel_id = channel_id
        self.port = port
        self.uav_udp_port = uav_udp_port
        self.ground_station_port = ground_station_port
        self.state = _ChannelState()
        self.stop_event = threading.Event()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(1.0)
        self.socket.bind(("127.0.0.1", port))
        self.thread = threading.Thread(
            target=self._receive_loop,
            name=f"flightlink-mavlink-{channel_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def _receive_loop(self) -> None:
        parser = mavlink2.MAVLink(None)
        while not self.stop_event.is_set():
            try:
                datagram, _ = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if datagram.startswith(_PACKET_SUMMARY_PREFIX):
                summary = _parse_packet_summary(datagram[len(_PACKET_SUMMARY_PREFIX) :])
                self.state.consume_packet_summary(
                    summary,
                    uav_udp_port=self.uav_udp_port,
                    ground_station_port=self.ground_station_port,
                )
                continue
            for octet in datagram:
                try:
                    message = parser.parse_char(bytes((octet,)))
                except Exception:
                    logger.debug("Dropped an invalid MAVLink byte on channel %s", self.channel_id)
                    continue
                if message is not None:
                    try:
                        self.state.consume(message)
                    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                        logger.debug(
                            "Unable to decode a MAVLink message on channel %s",
                            self.channel_id,
                            exc_info=True,
                        )

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.socket.close()
        except OSError:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)


class MavlinkTelemetryMonitor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: dict[UUID, _MonitorListener] = {}
        self._errors: dict[UUID, str] = {}

    def start(
        self,
        channel_id: UUID,
        port: int,
        uav_udp_port: int,
        ground_station_port: int,
    ) -> None:
        with self._lock:
            existing = self._listeners.get(channel_id)
            if (
                existing is not None
                and existing.port == port
                and existing.uav_udp_port == uav_udp_port
                and existing.ground_station_port == ground_station_port
                and existing.thread.is_alive()
            ):
                return
            self.stop(channel_id)
            try:
                listener = _MonitorListener(
                    channel_id, port, uav_udp_port, ground_station_port
                )
                listener.start()
            except OSError as exc:
                message = f"Cannot listen on telemetry monitor UDP port {port}: {exc}"
                self._errors[channel_id] = message
                raise RuntimeError(message) from exc
            self._listeners[channel_id] = listener
            self._errors.pop(channel_id, None)

    def stop(self, channel_id: UUID) -> None:
        with self._lock:
            listener = self._listeners.pop(channel_id, None)
            self._errors.pop(channel_id, None)
        if listener is not None:
            listener.stop()

    def stop_all(self) -> None:
        with self._lock:
            channel_ids = list(self._listeners)
        for channel_id in channel_ids:
            self.stop(channel_id)

    def snapshot(
        self,
        channel_id: UUID,
        *,
        ground_station_protocol: str,
        ground_station_port: int,
    ) -> ChannelTelemetryPublic:
        with self._lock:
            listener = self._listeners.get(channel_id)
            error = self._errors.get(channel_id)
        if listener is None:
            return ChannelTelemetryPublic(
                monitor_state="unavailable",
                monitor_error=error or "Telemetry monitor is not running",
            )
        return listener.state.snapshot(
            ground_station_protocol=ground_station_protocol,
            ground_station_port=ground_station_port,
        )

    def messages(self, channel_id: UUID, limit: int) -> list[FlightMessagePublic]:
        with self._lock:
            listener = self._listeners.get(channel_id)
        if listener is None:
            return []
        with listener.state.lock:
            return list(listener.state.messages)[-limit:]

    def packets(self, channel_id: UUID, limit: int) -> list[PacketSummaryPublic]:
        with self._lock:
            listener = self._listeners.get(channel_id)
        if listener is None:
            return []
        with listener.state.lock:
            return list(listener.state.packet_summaries)[-limit:]


_monitor: MavlinkTelemetryMonitor | None = None


def get_mavlink_telemetry_monitor() -> MavlinkTelemetryMonitor:
    global _monitor
    if _monitor is None:
        _monitor = MavlinkTelemetryMonitor()
    return _monitor


def set_mavlink_telemetry_monitor_for_process(
    monitor: MavlinkTelemetryMonitor | None,
) -> None:
    global _monitor
    _monitor = monitor
