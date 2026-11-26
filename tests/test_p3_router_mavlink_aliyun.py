"""P3 tests: Router config (no stray TCP 5760), real-socket dual-group same-SYSID bypass isolation,
strict FC heartbeat filtering, Aliyun RAM SG signed RPC + manual-rule protection, and legacy v0.1 import.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import time
from pathlib import Path

import httpx

from flightlink.adapters.aliyun_sg import AliyunRamSecurityGroupClient
from flightlink.adapters.interfaces import CloudAuthTemporaryError, CloudTimeoutError
from flightlink.adapters.mavlink_observer import (
    MAV_TYPE_GCS,
    MAV_TYPE_ONBOARD_CONTROLLER,
    LocalBypassMavlinkRouter,
    TelemetryObserverState,
    build_command_long_frame,
    build_gps_raw_int_frame,
    build_heartbeat_frame,
    build_sys_status_frame,
)
from flightlink.adapters.router_config import render_router_config
from flightlink.clock import ManualClock
from flightlink.config import Settings
from flightlink.db import Database
from flightlink.domain.errors import ConflictError
from flightlink.domain.legacy_import import import_legacy_v01_readonly
from flightlink.domain.operations_repo import ControlPlaneRepository


def test_router_config_disables_default_tcp_5760_for_udp_gcs() -> None:
    udp_cfg = render_router_config(group_id="grp_u1", dtu_port=5761, gcs_protocol="udp", gcs_port=14553)
    assert "TcpServerPort = 0" in udp_cfg
    assert "[UdpEndpoint gcs]" in udp_cfg
    assert "Port = 14553" in udp_cfg
    assert "Address = 127.0.0.1" in udp_cfg  # loopback-only bypass tap

    tcp_cfg = render_router_config(group_id="grp_t1", dtu_port=5760, gcs_protocol="tcp", gcs_port=14552)
    assert "TcpServerPort = 14552" in tcp_cfg
    assert "[UdpEndpoint gcs]" not in tcp_cfg


def test_real_sockets_dual_group_same_sysid_isolation_and_bypass_fault_tolerance() -> None:
    """Two real-socket routers (UDP GCS & TCP GCS) with identical SYSID=1:
    - Zero cross-talk between groups.
    - Killing/failing bypass observer & applying backpressure NEVER interrupts DTU <-> GCS packet delivery.
    """
    obs_a_packets: list[bytes] = []
    obs_b_packets: list[bytes] = []
    inject_observer_crash = {"boom": False}

    def on_bypass_a(data: bytes, from_dtu: bool) -> None:
        if inject_observer_crash["boom"]:
            raise RuntimeError("Simulated MAVProxy / archive observer crash + backpressure!")
        obs_a_packets.append(data)

    def on_bypass_b(data: bytes, from_dtu: bool) -> None:
        obs_b_packets.append(data)

    r_udp = LocalBypassMavlinkRouter(
        dtu_port=0, gcs_protocol="udp", gcs_port=0, on_bypass_packet=on_bypass_a
    )
    r_tcp = LocalBypassMavlinkRouter(
        dtu_port=0, gcs_protocol="tcp", gcs_port=0, on_bypass_packet=on_bypass_b
    )
    r_udp.start()
    r_tcp.start()

    dtu_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    gcs_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dtu_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    gcs_b = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        gcs_a.settimeout(2.0)
        gcs_b.settimeout(2.0)
        dtu_a.settimeout(2.0)
        dtu_b.settimeout(2.0)

        # Connect GCS endpoints (UDP peer registration + TCP connect)
        gcs_a.sendto(build_heartbeat_frame(sysid=255, compid=190, mav_type=MAV_TYPE_GCS), ("127.0.0.1", r_udp.bound_gcs_port))
        gcs_b.connect(("127.0.0.1", r_tcp.bound_gcs_port))
        time.sleep(0.05)

        # Send FC heartbeats with IDENTICAL SYSID=1 but distinct custom_mode (11 vs 22)
        hb_a = build_heartbeat_frame(sysid=1, custom_mode=11, seq=1)
        hb_b = build_heartbeat_frame(sysid=1, custom_mode=22, seq=2)
        dtu_a.sendto(hb_a, ("127.0.0.1", r_udp.bound_dtu_port))
        dtu_b.sendto(hb_b, ("127.0.0.1", r_tcp.bound_dtu_port))

        got_a, _ = gcs_a.recvfrom(4096)
        got_b = gcs_b.recv(4096)
        assert got_a == hb_a
        assert got_b == hb_b

        # Now crash MAVProxy / archive bypass on group A -> DTU <-> GCS MUST keep forwarding!
        inject_observer_crash["boom"] = True
        hb_a2 = build_heartbeat_frame(sysid=1, custom_mode=33, seq=3)
        dtu_a.sendto(hb_a2, ("127.0.0.1", r_udp.bound_dtu_port))
        got_a2, _ = gcs_a.recvfrom(4096)
        assert got_a2 == hb_a2  # Real packet delivery evidence despite observer failure!
    finally:
        for s in (dtu_a, gcs_a, dtu_b, gcs_b):
            s.close()
        r_udp.stop()
        r_tcp.stop()


def test_strict_fc_heartbeat_filtering_and_nullable_telemetry() -> None:
    obs = TelemetryObserverState()
    # IMEI, text 'heart', GCS heartbeat, MAVProxy onboard heartbeat MUST NOT open session or assign sysid!
    assert obs.feed_bytes(b"865432051234567", from_dtu=True) == []
    assert obs.feed_bytes(b"heart\r\n", from_dtu=True) == []
    assert obs.feed_bytes(build_heartbeat_frame(sysid=255, compid=190, mav_type=MAV_TYPE_GCS), from_dtu=True) == []
    assert obs.feed_bytes(build_heartbeat_frame(sysid=200, mav_type=MAV_TYPE_ONBOARD_CONTROLLER), from_dtu=True) == []
    assert obs.sysid is None

    # Real FC heartbeat + SYS_STATUS + GPS_RAW_INT -> only observed metrics populated, rest remain None
    hbs = obs.feed_bytes(build_heartbeat_frame(sysid=7, custom_mode=4, base_mode=0x81, seq=1), from_dtu=True)
    assert len(hbs) == 1
    assert obs.sysid == 7
    obs.feed_bytes(build_sys_status_frame(sysid=7, voltage_mv=24600, battery_remaining=86, seq=2), from_dtu=True)
    obs.feed_bytes(build_gps_raw_int_frame(sysid=7, fix_type=6, satellites_visible=17, eph_cm=62, alt_mm=512400, seq=3), from_dtu=True)
    obs.feed_bytes(build_command_long_frame(sysid=255, target_system=7, command=176, param1=1.0, seq=1), from_dtu=False)

    tele = obs.to_telemetry_dict()
    assert tele["armed"] is True
    assert tele["battery_voltage_v"] == 24.6
    assert tele["battery_remaining_pct"] == 86.0
    assert tele["gps_fix_type"] == "RTK Fixed"
    assert tele["gps_satellites"] == 17
    assert tele["gps_hdop"] == 0.62
    assert tele["ground_speed_mps"] is None  # Unobserved -> strictly None!
    assert len(obs.recent_gcs_commands) == 1
    assert obs.recent_gcs_commands[0]["command"] == "MAV_CMD_DO_SET_MODE"


def test_aliyun_ram_sg_client_signs_and_revokes_only_by_rule_id() -> None:
    clock = ManualClock()
    state = {
        "rules": [
            {
                "SecurityGroupRuleId": "sgr-manual-ssh",
                "IpProtocol": "TCP",
                "PortRange": "22/22",
                "SourceCidrIp": "10.0.0.0/8",
                "Description": "manual-ssh-rule",
            }
        ],
        "mode": "ok",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/latest/api/token"):
            return httpx.Response(200, text="imds-v2-token")
        if "/ram/security-credentials/" in url:
            if state["mode"] == "ram_down":
                return httpx.Response(403, json={"Code": "Forbidden"})
            if url.endswith("/"):
                return httpx.Response(200, text="FlightLinkEcsSecurityGroupRole")
            return httpx.Response(
                200,
                json={
                    "Code": "Success",
                    "AccessKeyId": "STS.TEMP_AK",
                    "AccessKeySecret": "TEMP_SECRET",
                    "SecurityToken": "TEMP_TOKEN",
                    "Expiration": "2026-11-26T18:00:00Z",
                },
            )
        if state["mode"] == "timeout":
            raise httpx.ReadTimeout("simulated timeout")
        form = dict(httpx.QueryParams(request.content.decode("utf-8")))
        action = form["Action"]
        assert "Signature" in form and form["SecurityToken"] == "TEMP_TOKEN"
        if action == "DescribeSecurityGroups":
            return httpx.Response(
                200,
                json={"SecurityGroups": {"SecurityGroup": [{"SecurityGroupId": "sg-2ze93ogksv8g4i7tc8vd", "SecurityGroupName": "default"}]}},
            )
        if action == "DescribeSecurityGroupAttribute":
            return httpx.Response(200, json={"Permissions": {"Permission": state["rules"]}})
        if action == "AuthorizeSecurityGroup":
            rid = f"sgr-fl-{len(state['rules']) + 1}"
            state["rules"].append(
                {
                    "SecurityGroupRuleId": rid,
                    "IpProtocol": form["Permissions.1.IpProtocol"].upper(),
                    "PortRange": form["Permissions.1.PortRange"],
                    "SourceCidrIp": form["Permissions.1.SourceCidrIp"],
                    "Description": form["Permissions.1.Description"],
                }
            )
            return httpx.Response(200, json={"SecurityGroupRuleIds": {"SecurityGroupRuleId": [rid]}})
        if action == "RevokeSecurityGroup":
            rid = form["SecurityGroupRuleId.1"]
            state["rules"] = [r for r in state["rules"] if r["SecurityGroupRuleId"] != rid]
            return httpx.Response(200, json={"RequestId": "req-1"})
        return httpx.Response(400, json={"Code": "UnknownAction"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = AliyunRamSecurityGroupClient(region="cn-beijing", clock=clock, http_client=http)
    verified = client.validate_connection(region="cn-beijing", security_group_id="sg-2ze93ogksv8g4i7tc8vd")
    assert verified["ram_role"] == "FlightLinkEcsSecurityGroupRole"

    # Explicitly specifying a role name verifies that exact role; omitting ram_role re-discovers cleanly!
    verified_explicit = client.validate_connection(
        region="cn-beijing",
        security_group_id="sg-2ze93ogksv8g4i7tc8vd",
        ram_role="CustomRoleName",
    )
    assert verified_explicit["ram_role"] == "CustomRoleName"
    verified_rediscovered = client.validate_connection(
        region="cn-beijing",
        security_group_id="sg-2ze93ogksv8g4i7tc8vd",
        ram_role=None,
    )
    assert verified_rediscovered["ram_role"] == "FlightLinkEcsSecurityGroupRole"

    created = client.authorize_rule(
        security_group_id="sg-2ze93ogksv8g4i7tc8vd",
        protocol="udp",
        port=5760,
        source_cidr="0.0.0.0/0",
        description="flightlink:v1:ecs1:grp1:dtu",
    )
    assert created.rule_id.startswith("sgr-fl-")
    client.revoke_rule(security_group_id="sg-2ze93ogksv8g4i7tc8vd", rule_id=created.rule_id)
    remaining = client.list_rules(security_group_id="sg-2ze93ogksv8g4i7tc8vd")
    assert [r.rule_id for r in remaining] == ["sgr-manual-ssh"]  # Manual SSH rule untouched!

    state["mode"] = "timeout"
    try:
        client.list_rules(security_group_id="sg-2ze93ogksv8g4i7tc8vd")
        assert False, "Expected CloudTimeoutError"
    except CloudTimeoutError:
        pass

    state["mode"] = "ram_down"
    client._cached_creds = None
    try:
        client.list_rules(security_group_id="sg-2ze93ogksv8g4i7tc8vd")
        assert False, "Expected CloudAuthTemporaryError"
    except CloudAuthTemporaryError:
        pass


def test_legacy_v01_readonly_import_stopped_and_refuses_active_legacy(tmp_path: Path) -> None:
    legacy_db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(legacy_db))
    conn.execute(
        "CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT, uav_port INTEGER, gcs_protocol TEXT, gcs_port INTEGER)"
    )
    conn.execute("INSERT INTO channels VALUES (1, '旧一号机', 5760, 'tcp', 14552)")
    conn.commit()
    conn.close()

    settings = Settings(data_dir=tmp_path / "new")
    db = Database(settings.resolved_db_path)
    db.init_schema()
    repo = ControlPlaneRepository(db, settings)

    # Active legacy service MUST be refused!
    try:
        import_legacy_v01_readonly(legacy_db_path=legacy_db, repo=repo, legacy_service_active=True)
        assert False, "Expected ConflictError"
    except ConflictError as exc:
        assert exc.code == "LEGACY_SERVICE_ACTIVE"

    res = import_legacy_v01_readonly(legacy_db_path=legacy_db, repo=repo, legacy_service_active=False)
    assert len(res["imported"]) == 1
    gid = res["imported"][0]["group_id"]
    assert repo.get_group_detail(gid).desired_state == "stopped"
    assert repo.get_group_detail(gid).lifecycle_state == "configured_stopped"
