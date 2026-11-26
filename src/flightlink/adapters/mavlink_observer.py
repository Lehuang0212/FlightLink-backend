"""MAVLink v1/v2 frame parser, strict FC heartbeat filter, 12-item telemetry extractor,
read-only GCS command observer, and real-socket loopback relay for P3 verification.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from flightlink.clock import Clock, SystemClock

# MAVLink CRC_EXTRA bytes per official common.xml
CRC_EXTRA: dict[int, int] = {
    0: 50,   # HEARTBEAT
    1: 124,  # SYS_STATUS
    24: 24,  # GPS_RAW_INT
    33: 104, # GLOBAL_POSITION_INT
    74: 20,  # VFR_HUD
    76: 152, # COMMAND_LONG
    77: 143, # COMMAND_ACK
}

MAV_TYPE_GCS = 6
MAV_TYPE_ONBOARD_CONTROLLER = 18
MAV_COMP_ID_MISSIONPLANNER = 190
MAV_MODE_FLAG_SAFETY_ARMED = 0x80

GPS_FIX_NAMES: dict[int, str] = {
    0: "No GPS",
    1: "No Fix",
    2: "2D Fix",
    3: "3D Fix",
    4: "DGPS",
    5: "RTK Float",
    6: "RTK Fixed",
}

CMD_RESULT_NAMES: dict[int, str] = {
    0: "ACCEPTED",
    1: "DENIED",
    2: "DENIED",
    3: "UNSUPPORTED",
    4: "FAILED",
    5: "IN_PROGRESS",
}

CMD_NAMES: dict[int, str] = {
    176: "MAV_CMD_DO_SET_MODE",
    400: "MAV_CMD_COMPONENT_ARM_DISARM",
    511: "MAV_CMD_SET_MESSAGE_INTERVAL",
    512: "MAV_CMD_REQUEST_MESSAGE",
}


def x25_crc(data: bytes, crc_extra: int | None = None) -> int:
    crc = 0xFFFF
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp ^= (tmp << 4) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    if crc_extra is not None:
        tmp = crc_extra ^ (crc & 0xFF)
        tmp ^= (tmp << 4) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def build_mavlink_v1_frame(
    *,
    msgid: int,
    payload: bytes,
    sysid: int,
    compid: int = 1,
    seq: int = 0,
) -> bytes:
    header = struct.pack("<BBBBBB", 0xFE, len(payload), seq & 0xFF, sysid & 0xFF, compid & 0xFF, msgid & 0xFF)
    crc = x25_crc(header[1:] + payload, CRC_EXTRA[msgid])
    return header + payload + struct.pack("<H", crc)


def build_heartbeat_frame(
    *,
    sysid: int,
    compid: int = 1,
    mav_type: int = 2,  # MAV_TYPE_QUADROTOR
    autopilot: int = 3,  # MAV_AUTOPILOT_ARDUPILOTMEGA
    base_mode: int = 0x51,
    custom_mode: int = 0,
    system_status: int = 4,
    seq: int = 0,
) -> bytes:
    payload = struct.pack("<IBBBBB", custom_mode, mav_type, autopilot, base_mode, system_status, 3)
    return build_mavlink_v1_frame(msgid=0, payload=payload, sysid=sysid, compid=compid, seq=seq)


def build_sys_status_frame(
    *,
    sysid: int,
    voltage_mv: int,
    battery_remaining: int,
    seq: int = 0,
) -> bytes:
    # SYS_STATUS (msgid=1), 31 bytes: sensors(3I), load(H), voltage_battery(H), current(h), drop_comm(H), errors(5H), battery_remaining(b)
    payload = struct.pack(
        "<IIIHHhHHHHHHb",
        0, 0, 0,
        250,
        voltage_mv,
        -1,
        0, 0, 0, 0, 0, 0,
        battery_remaining,
    )
    return build_mavlink_v1_frame(msgid=1, payload=payload, sysid=sysid, compid=1, seq=seq)


def build_gps_raw_int_frame(
    *,
    sysid: int,
    fix_type: int,
    satellites_visible: int,
    eph_cm: int,
    alt_mm: int,
    seq: int = 0,
) -> bytes:
    # GPS_RAW_INT (msgid=24), 30 bytes: time_usec(Q), lat(i), lon(i), alt(i), eph(H), epv(H), vel(H), cog(H), fix_type(B), satellites_visible(B)
    payload = struct.pack("<QiiiHHHHBB", 0, 0, 0, alt_mm, eph_cm, 0, 0, 0, fix_type, satellites_visible)
    return build_mavlink_v1_frame(msgid=24, payload=payload, sysid=sysid, compid=1, seq=seq)


def build_global_position_int_frame(
    *,
    sysid: int,
    alt_mm: int,
    relative_alt_mm: int,
    seq: int = 0,
) -> bytes:
    # GLOBAL_POSITION_INT (msgid=33), 28 bytes
    payload = struct.pack("<IiiiihhhH", 0, 0, 0, alt_mm, relative_alt_mm, 0, 0, 0, 0)
    return build_mavlink_v1_frame(msgid=33, payload=payload, sysid=sysid, compid=1, seq=seq)


def build_vfr_hud_frame(
    *,
    sysid: int,
    groundspeed_mps: float,
    alt_m: float,
    seq: int = 0,
) -> bytes:
    # VFR_HUD (msgid=74), 20 bytes: airspeed(f), groundspeed(f), alt(f), climb(f), heading(h), throttle(H)
    payload = struct.pack("<ffffhH", 0.0, groundspeed_mps, alt_m, 0.0, 0, 0)
    return build_mavlink_v1_frame(msgid=74, payload=payload, sysid=sysid, compid=1, seq=seq)


def build_command_long_frame(
    *,
    sysid: int = 255,
    compid: int = MAV_COMP_ID_MISSIONPLANNER,
    target_system: int = 1,
    target_component: int = 1,
    command: int = 176,
    param1: float = 0.0,
    seq: int = 0,
) -> bytes:
    # COMMAND_LONG (msgid=76), 33 bytes: 7 floats + command(H) + target_sys(B) + target_comp(B) + confirmation(B)
    payload = struct.pack("<fffffffHBBB", param1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, command, target_system, target_component, 0)
    return build_mavlink_v1_frame(msgid=76, payload=payload, sysid=sysid, compid=compid, seq=seq)


@dataclass(frozen=True)
class ParsedMavlinkFrame:
    version: int
    seq: int
    sysid: int
    compid: int
    msgid: int
    payload: bytes


def parse_mavlink_frames(data: bytes) -> list[ParsedMavlinkFrame]:
    """Extract all CRC-verified MAVLink v1/v2 frames from raw bytes (ignoring IMEI/text noise)."""
    frames: list[ParsedMavlinkFrame] = []
    idx = 0
    n = len(data)
    while idx < n:
        magic = data[idx]
        if magic == 0xFE and idx + 8 <= n:
            plen = data[idx + 1]
            total = 6 + plen + 2
            if idx + total <= n:
                seq = data[idx + 2]
                sysid = data[idx + 3]
                compid = data[idx + 4]
                msgid = data[idx + 5]
                payload = data[idx + 6 : idx + 6 + plen]
                crc_got = struct.unpack_from("<H", data, idx + 6 + plen)[0]
                if msgid in CRC_EXTRA:
                    crc_exp = x25_crc(data[idx + 1 : idx + 6 + plen], CRC_EXTRA[msgid])
                    if crc_got == crc_exp:
                        frames.append(ParsedMavlinkFrame(1, seq, sysid, compid, msgid, payload))
                        idx += total
                        continue
        elif magic == 0xFD and idx + 12 <= n:
            plen = data[idx + 1]
            incompat = data[idx + 2]
            sig_len = 13 if (incompat & 0x01) else 0
            total = 10 + plen + 2 + sig_len
            if idx + total <= n:
                seq = data[idx + 4]
                sysid = data[idx + 5]
                compid = data[idx + 6]
                msgid = data[idx + 7] | (data[idx + 8] << 8) | (data[idx + 9] << 16)
                payload = data[idx + 10 : idx + 10 + plen]
                crc_got = struct.unpack_from("<H", data, idx + 10 + plen)[0]
                if msgid in CRC_EXTRA:
                    crc_exp = x25_crc(data[idx + 1 : idx + 10 + plen], CRC_EXTRA[msgid])
                    if crc_got == crc_exp:
                        frames.append(ParsedMavlinkFrame(2, seq, sysid, compid, msgid, payload))
                        idx += total
                        continue
        idx += 1
    return frames


def is_valid_fc_heartbeat(
    frame: ParsedMavlinkFrame,
    *,
    observer_sysids: set[int] | None = None,
) -> bool:
    """Return True ONLY for genuine flight controller HEARTBEAT frames.

    Excludes GCS heartbeats, MAVProxy/companion heartbeats, and invalid payloads.
    """
    if frame.msgid != 0 or len(frame.payload) < 9:
        return False
    if frame.sysid <= 0 or frame.sysid >= 255:
        return False
    if observer_sysids and frame.sysid in observer_sysids:
        return False
    if frame.compid == MAV_COMP_ID_MISSIONPLANNER:
        return False
    _custom_mode, mav_type, autopilot, _base_mode, _sys_status, _mav_ver = struct.unpack_from("<IBBBBB", frame.payload, 0)
    if mav_type in (MAV_TYPE_GCS, MAV_TYPE_ONBOARD_CONTROLLER):
        return False
    if autopilot == 8:  # MAV_AUTOPILOT_INVALID (non-FC component)
        return False
    return True


@dataclass
class TelemetryObserverState:
    """Tracks strictly observed telemetry & GCS commands for a single group.

    Any field without a real observed source stays `None` (never fabricated!).
    """

    sysid: int | None = None
    flight_mode: str | None = None
    armed: bool | None = None
    battery_remaining_pct: float | None = None
    battery_voltage_v: float | None = None
    gps_satellites: int | None = None
    gps_fix_type: str | None = None
    gps_hdop: float | None = None
    altitude_msl_m: float | None = None
    relative_altitude_m: float | None = None
    ground_speed_mps: float | None = None
    rx_rate_kbps: float | None = None
    tx_rate_kbps: float | None = None
    packets_lost: int | None = None
    packets_received: int | None = None
    observed_at: str | None = None
    recent_gcs_commands: list[dict[str, object]] = field(default_factory=list)
    _last_seq_by_sysid: dict[int, int] = field(default_factory=dict)

    def feed_bytes(
        self,
        data: bytes,
        *,
        from_dtu: bool,
        clock: Clock | None = None,
        observer_sysids: set[int] | None = None,
    ) -> list[ParsedMavlinkFrame]:
        clk = clock or SystemClock()
        frames = parse_mavlink_frames(data)
        valid_fc_hbs: list[ParsedMavlinkFrame] = []

        for frame in frames:
            now_iso = clk.now_iso()
            if from_dtu and is_valid_fc_heartbeat(frame, observer_sysids=observer_sysids):
                self.sysid = frame.sysid
                custom_mode, _mtype, _ap, base_mode, _st, _ver = struct.unpack_from("<IBBBBB", frame.payload, 0)
                self.armed = bool(base_mode & MAV_MODE_FLAG_SAFETY_ARMED)
                self.flight_mode = f"MODE_{custom_mode}"
                self.observed_at = now_iso
                valid_fc_hbs.append(frame)

            # Packet loss / count tracking for FC stream
            if from_dtu and frame.sysid == self.sysid:
                self.packets_received = (self.packets_received or 0) + 1
                self.packets_lost = self.packets_lost or 0
                prev_seq = self._last_seq_by_sysid.get(frame.sysid)
                if prev_seq is not None:
                    expected = (prev_seq + 1) & 0xFF
                    if frame.seq != expected:
                        gap = (frame.seq - expected) & 0xFF
                        if gap < 64:
                            self.packets_lost += gap
                self._last_seq_by_sysid[frame.sysid] = frame.seq
                self.observed_at = now_iso

                if frame.msgid == 1 and len(frame.payload) >= 31:
                    _, _, _, _, voltage_mv, _, _, _, _, _, _, _, batt_rem = struct.unpack_from("<IIIHHhHHHHHHb", frame.payload, 0)
                    if voltage_mv != 0xFFFF:
                        self.battery_voltage_v = round(voltage_mv / 1000.0, 2)
                    if batt_rem >= 0:
                        self.battery_remaining_pct = float(batt_rem)
                elif frame.msgid == 24 and len(frame.payload) >= 30:
                    _, _, _, alt_mm, eph_cm, _, _, _, fix_type, sats = struct.unpack_from("<QiiiHHHHBB", frame.payload, 0)
                    self.gps_fix_type = GPS_FIX_NAMES.get(fix_type, f"Fix {fix_type}")
                    if sats != 255:
                        self.gps_satellites = int(sats)
                    if eph_cm != 0xFFFF:
                        self.gps_hdop = round(eph_cm / 100.0, 2)
                    self.altitude_msl_m = round(alt_mm / 1000.0, 2)
                elif frame.msgid == 33 and len(frame.payload) >= 28:
                    _, _, _, alt_mm, rel_alt_mm, _, _, _, _ = struct.unpack_from("<IiiiihhhH", frame.payload, 0)
                    self.altitude_msl_m = round(alt_mm / 1000.0, 2)
                    self.relative_altitude_m = round(rel_alt_mm / 1000.0, 2)
                elif frame.msgid == 74 and len(frame.payload) >= 20:
                    _, groundspeed, alt_m, _, _, _ = struct.unpack_from("<ffffhH", frame.payload, 0)
                    self.ground_speed_mps = round(float(groundspeed), 2)
                    if self.altitude_msl_m is None:
                        self.altitude_msl_m = round(float(alt_m), 2)

            if frame.msgid == 76 and len(frame.payload) >= 33:
                p1, *_rest, cmd_id, _t_sys, _t_comp, _conf = struct.unpack_from("<fffffffHBBB", frame.payload, 0)
                self.recent_gcs_commands.insert(
                    0,
                    {
                        "id": f"cmd_{frame.sysid}_{frame.seq}_{cmd_id}",
                        "observed_at": now_iso,
                        "command": CMD_NAMES.get(cmd_id, f"MAV_CMD_{cmd_id}"),
                        "detail": f"param1={p1:g}",
                        "status": "UNKNOWN",
                        "source_sysid": frame.sysid,
                        "source_compid": frame.compid,
                        "source_label": f"GCS sysid={frame.sysid}",
                    },
                )
                self.recent_gcs_commands = self.recent_gcs_commands[:10]

        return valid_fc_hbs

    def to_telemetry_dict(self) -> dict[str, object]:
        loss_pct = None
        if self.packets_received is not None and self.packets_received > 0 and self.packets_lost is not None:
            loss_pct = round(self.packets_lost / (self.packets_received + self.packets_lost) * 100.0, 2)
        return {
            "flight_mode": self.flight_mode,
            "armed": self.armed,
            "battery_remaining_pct": self.battery_remaining_pct,
            "battery_voltage_v": self.battery_voltage_v,
            "gps_satellites": self.gps_satellites,
            "gps_fix_type": self.gps_fix_type,
            "gps_hdop": self.gps_hdop,
            "altitude_msl_m": self.altitude_msl_m,
            "relative_altitude_m": self.relative_altitude_m,
            "ground_speed_mps": self.ground_speed_mps,
            "rx_rate_kbps": self.rx_rate_kbps,
            "tx_rate_kbps": self.tx_rate_kbps,
            "packets_lost": self.packets_lost,
            "packets_received": self.packets_received,
            "packet_loss_pct": loss_pct,
        }


class LocalBypassMavlinkRouter:
    """Real OS-socket isolated router with passive non-blocking bypass tap (for P3 topology tests).

    Guarantees:
    - DTU <-> GCS forwarding runs directly on real UDP/TCP sockets.
    - Bypass tap (MAVProxy observer) is strictly out-of-band (non-blocking UDP copy);
      killing observer, pausing archive, or backpressuring logs NEVER stalls or drops DTU <-> GCS!
    - UDP GCS mode opens ONLY UDP sockets (zero TCP listening sockets!).
    """

    def __init__(
        self,
        *,
        dtu_port: int,
        gcs_protocol: Literal["tcp", "udp"],
        gcs_port: int,
        bypass_tap_port: int | None = None,
        on_bypass_packet: Callable[[bytes, bool], None] | None = None,
    ) -> None:
        self.dtu_port = dtu_port
        self.gcs_protocol = gcs_protocol
        self.gcs_port = gcs_port
        self.bypass_tap_port = bypass_tap_port
        self.on_bypass_packet = on_bypass_packet
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._dtu_sock: socket.socket | None = None
        self._gcs_udp_sock: socket.socket | None = None
        self._gcs_tcp_server: socket.socket | None = None
        self._gcs_tcp_client: socket.socket | None = None
        self._tap_sock: socket.socket | None = None
        self._last_dtu_addr: tuple[str, int] | None = None
        self._last_gcs_udp_addr: tuple[str, int] | None = None
        self._lock = threading.Lock()
        self.bound_dtu_port: int = 0
        self.bound_gcs_port: int = 0

    def start(self) -> None:
        self._dtu_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._dtu_sock.bind(("127.0.0.1", self.dtu_port))
        self._dtu_sock.settimeout(0.1)
        self.bound_dtu_port = self._dtu_sock.getsockname()[1]

        self._tap_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._tap_sock.setblocking(False)

        if self.gcs_protocol == "udp":
            self._gcs_udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._gcs_udp_sock.bind(("127.0.0.1", self.gcs_port))
            self._gcs_udp_sock.settimeout(0.1)
            self.bound_gcs_port = self._gcs_udp_sock.getsockname()[1]
            t_gcs = threading.Thread(target=self._gcs_udp_loop, daemon=True)
            t_gcs.start()
            self._threads.append(t_gcs)
        else:
            self._gcs_tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._gcs_tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._gcs_tcp_server.bind(("127.0.0.1", self.gcs_port))
            self._gcs_tcp_server.listen(2)
            self._gcs_tcp_server.settimeout(0.1)
            self.bound_gcs_port = self._gcs_tcp_server.getsockname()[1]
            t_tcp = threading.Thread(target=self._gcs_tcp_loop, daemon=True)
            t_tcp.start()
            self._threads.append(t_tcp)

        t_dtu = threading.Thread(target=self._dtu_loop, daemon=True)
        t_dtu.start()
        self._threads.append(t_dtu)

    def _emit_bypass(self, data: bytes, from_dtu: bool) -> None:
        # Non-blocking best-effort copy; observer failures NEVER affect forwarding!
        if self.bypass_tap_port and self._tap_sock is not None:
            try:
                self._tap_sock.sendto(data, ("127.0.0.1", self.bypass_tap_port))
            except OSError:
                pass
        if self.on_bypass_packet is not None:
            try:
                self.on_bypass_packet(data, from_dtu)
            except Exception:
                pass

    def _dtu_loop(self) -> None:
        assert self._dtu_sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._dtu_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._last_dtu_addr = addr
                gcs_udp = self._last_gcs_udp_addr
                gcs_tcp = self._gcs_tcp_client
            if self.gcs_protocol == "udp" and self._gcs_udp_sock and gcs_udp:
                try:
                    self._gcs_udp_sock.sendto(data, gcs_udp)
                except OSError:
                    pass
            elif self.gcs_protocol == "tcp" and gcs_tcp is not None:
                try:
                    gcs_tcp.sendall(data)
                except OSError:
                    with self._lock:
                        self._gcs_tcp_client = None
            self._emit_bypass(data, True)

    def _gcs_udp_loop(self) -> None:
        assert self._gcs_udp_sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._gcs_udp_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self._last_gcs_udp_addr = addr
                dtu_addr = self._last_dtu_addr
            if self._dtu_sock and dtu_addr:
                try:
                    self._dtu_sock.sendto(data, dtu_addr)
                except OSError:
                    pass
            self._emit_bypass(data, False)

    def _gcs_tcp_loop(self) -> None:
        assert self._gcs_tcp_server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._gcs_tcp_server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.1)
            with self._lock:
                if self._gcs_tcp_client is not None:
                    try:
                        self._gcs_tcp_client.close()
                    except OSError:
                        pass
                self._gcs_tcp_client = conn
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                with self._lock:
                    dtu_addr = self._last_dtu_addr
                if self._dtu_sock and dtu_addr:
                    try:
                        self._dtu_sock.sendto(data, dtu_addr)
                    except OSError:
                        pass
                self._emit_bypass(data, False)
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                if self._gcs_tcp_client is conn:
                    self._gcs_tcp_client = None

    def stop(self) -> None:
        self._stop.set()
        for s in (self._dtu_sock, self._gcs_udp_sock, self._gcs_tcp_client, self._gcs_tcp_server, self._tap_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        for t in self._threads:
            t.join(timeout=1.0)
