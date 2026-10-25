import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "vendor"))

spec = importlib.util.spec_from_file_location("x25_configure", REPO / "engine" / "x25" / "x25_configure.py")
x25 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(x25)
MAV = x25.MAV

PASSPHRASE = "correct-horse-battery-staple-1234"
# What a freshly flashed X25 reports: only NET_ENABLE / NET_P1_TYPE exist until it is rebooted with NET_ENABLE=1.
INITIAL = {"NET_ENABLE": 0.0, "NET_P1_TYPE": 0.0, "GPS_TYPE": 1.0, "STAT_RUNTIME": 1234.0}
HIDDEN = {
    "NET_IPADDR0": 0.0, "NET_IPADDR1": 0.0, "NET_IPADDR2": 0.0, "NET_IPADDR3": 14.0, "NET_NETMASK": 24.0,
    "NET_P1_IP0": 0.0, "NET_P1_IP1": 0.0, "NET_P1_IP2": 0.0, "NET_P1_IP3": 0.0, "NET_P1_PORT": 14550.0,
    "NET_P1_PROTOCOL": 1.0, "NET_DHCP": 1.0, "NET_OPTIONS": 0.0,
    "NET_GWADDR0": 0.0, "NET_GWADDR1": 0.0, "NET_GWADDR2": 0.0, "NET_GWADDR3": 0.0,
}
# ArduPilot SITL 4.7.1 (and Linux boards): the OS owns the IP address, so no NET_IPADDR*, NET_NETMASK, NET_DHCP,
# NET_GWADDR* at all, only the NET_P1_* port and NET_OPTIONS appear after the reboot.
SITL_HIDDEN = {
    "NET_P1_IP0": 0.0, "NET_P1_IP1": 0.0, "NET_P1_IP2": 0.0, "NET_P1_IP3": 0.0, "NET_P1_PORT": 0.0,
    "NET_P1_PROTOCOL": 0.0, "NET_OPTIONS": 0.0,
}
ALL_SENSORS = sum(x25.SENSOR_BITS.values())
EKF_GOOD = (MAV.EKF_ATTITUDE | MAV.EKF_VELOCITY_HORIZ | MAV.EKF_VELOCITY_VERT | MAV.EKF_POS_HORIZ_REL
            | MAV.EKF_POS_HORIZ_ABS | MAV.EKF_POS_VERT_ABS)
# What a healthy X25 on the bench with GPS and battery answers to MAV_CMD_REQUEST_MESSAGE
HEALTHY = {
    "SYS_STATUS": {"onboard_control_sensors_present": ALL_SENSORS, "onboard_control_sensors_enabled": ALL_SENSORS,
                   "onboard_control_sensors_health": ALL_SENSORS, "voltage_battery": 16400, "current_battery": 120,
                   "battery_remaining": 87},
    "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 12, "eph": 80},
    "EKF_STATUS_REPORT": {"flags": EKF_GOOD, "velocity_variance": 0.1, "pos_horiz_variance": 0.1,
                          "pos_vert_variance": 0.1, "compass_variance": 0.1},
    "AUTOPILOT_VERSION": {"flight_sw_version": (4 << 24) | (7 << 16) | (1 << 8), "board_version": 1234 << 16},
}
BANNER = ["ArduCopter V4.7.1 (3d4f2a1b)", "ChibiOS: 6c2e6e5d", "CUAV-X25-EVO 003A002D 3133510B 37363134",
          "Frame: QUAD/X"]
MSG_NAMES = {getattr(MAV, "MAVLINK_MSG_ID_" + n): n for n in x25.HEALTH_MESSAGES}


def health(**changes):
    """HEALTHY with some fields changed; a message set to None is never answered."""
    out = {name: dict(fields) for name, fields in HEALTHY.items()}
    for name, fields in changes.items():
        out[name] = None if fields is None else dict(out[name], **fields)
    return out


def without(*sensors):
    return ALL_SENSORS & ~sum(x25.SENSOR_BITS[s] for s in sensors)


def fake_msg(mtype, **fields):
    return SimpleNamespace(get_type=lambda: mtype, get_srcSystem=lambda: 1, get_srcComponent=lambda: 1, **fields)


class FakeMav:
    def __init__(self, conn):
        self.conn = conn
        self.signing = SimpleNamespace(sign_outgoing=False)

    def request_data_stream_send(self, *args):
        self.conn.stream_requests.append(args)

    def param_request_list_send(self, sysid, compid):
        visible = [n for n in self.conn.params if n not in self.conn.hidden]
        for i, name in enumerate(visible):
            self.conn.queue_param(name, index=i, count=len(visible))

    def param_request_read_send(self, sysid, compid, name, index):
        name = name.decode("ascii")
        # like ArduPilot: a name it does not have gets no answer
        if name in self.conn.params and name not in self.conn.hidden and self.conn.accepts_command():
            self.conn.queue_param(name)

    def param_set_send(self, sysid, compid, name, value, ptype):
        name = name.decode("ascii")
        if name in self.conn.refuse:            # answers with the value it keeps
            self.conn.queue_param(name)
            return
        self.conn.sets.append((name, value))
        self.conn.params[name] = value
        self.conn.queue_param(name)

    def command_long_send(self, sysid, compid, command, confirmation, *params):
        self.conn.commands.append((command, params[0]))
        if command == MAV.MAV_CMD_REQUEST_MESSAGE:
            name = MSG_NAMES.get(int(params[0]))
            if self.conn.health.get(name) is not None:
                self.conn.queue.append((name, fake_msg(name, **self.conn.health[name])))
        elif command == MAV.MAV_CMD_RUN_PREARM_CHECKS:
            # like ArduPilot: the ACK comes first, the PreArm texts follow
            ack = fake_msg("COMMAND_ACK", command=command, result=MAV.MAV_RESULT_ACCEPTED)
            self.conn.queue.append(("COMMAND_ACK", ack))
            self.conn.send_texts(self.conn.prearm)
        elif command == MAV.MAV_CMD_DO_SEND_BANNER:
            self.conn.send_texts(self.conn.banner)
        elif command == MAV.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN:
            self.conn.reboots += 1
            if self.conn.params["NET_ENABLE"] == 1 and self.conn.reveal_on_reboot:
                self.conn.hidden.clear()
            ack = SimpleNamespace(command=command, result=MAV.MAV_RESULT_ACCEPTED)
            self.conn.queue = [("COMMAND_ACK", ack)]

    def setup_signing_send(self, sysid, compid, key, timestamp):
        self.conn.signing.append(bytes(key))


class FakeConn:
    """Stands in for a pymavlink serial connection to an X25; no hardware, no sleeping.
    `skew` is virtual time: waiting for a message that never comes, or for the next 1 Hz HEARTBEAT, advances it."""

    def __init__(self, params=None, hidden=None, armed=False, health=None, prearm=(), banner=BANNER):
        self.params = dict(INITIAL if params is None else params)
        self.params.update(HIDDEN if hidden is None else hidden)
        self.hidden = set(HIDDEN if hidden is None else hidden)
        self.armed = armed
        self.health = HEALTHY if health is None else health
        self.prearm = list(prearm)
        self.banner = list(banner)
        self.queue = []
        self.sets = []
        self.signing = []
        self.commands = []
        self.stream_requests = []
        self.reboots = 0
        self.skew = 0.0
        self.refuse = set()         # PARAM_SET of these names is not applied
        self.reveal_on_reboot = True    # a reboot with NET_ENABLE=1 shows the hidden NET_* parameters
        self.need_key = None        # a network link with signing on: only commands signed with this key are answered
        self.key = None
        self.mav = FakeMav(self)

    def setup_signing(self, key, sign_outgoing=True, allow_unsigned_callback=None):
        self.key = key
        self.mav.signing.sign_outgoing = sign_outgoing

    def accepts_command(self):
        return self.need_key is None or (self.mav.signing.sign_outgoing and self.key == self.need_key)

    def queue_param(self, name, index=0, count=1):
        msg = SimpleNamespace(param_id=name, param_value=self.params[name], param_type=9, param_count=count,
                              param_index=index, get_srcSystem=lambda: 1)
        self.queue.append(("PARAM_VALUE", msg))

    def send_texts(self, texts):
        """STATUSTEXT lines; an item is a text, or the fields of one chunk ({"text", "id", "chunk_seq"})."""
        for t in texts:
            fields = dict(t) if isinstance(t, dict) else {"text": t, "id": 0, "chunk_seq": 0}
            self.queue.append(("STATUSTEXT", fake_msg("STATUSTEXT", severity=MAV.MAV_SEVERITY_CRITICAL, **fields)))

    def recv_match(self, type=None, blocking=False, timeout=None):
        types = type if isinstance(type, (list, tuple)) else [type]
        for i, (mtype, msg) in enumerate(self.queue):
            if mtype in types:
                del self.queue[i]
                return msg
        if "HEARTBEAT" in types:
            self.skew += 1
            return fake_msg("HEARTBEAT", autopilot=MAV.MAV_AUTOPILOT_ARDUPILOTMEGA, type=MAV.MAV_TYPE_QUADROTOR,
                            base_mode=128 if self.armed else 0, custom_mode=0, system_status=MAV.MAV_STATE_STANDBY)
        self.skew += timeout or 0
        return None

    def close(self):
        pass


class Run:
    def __init__(self, rc, events, out, conn, tmp):
        self.rc, self.events, self.out, self.conn, self.tmp = rc, events, out, conn, tmp

    def of(self, t, **match):
        return [e for e in self.events if e["t"] == t and all(e.get(k) == v for k, v in match.items())]

    def steps(self, sid):
        return [e["state"] for e in self.events if e["t"] == "step" and e["id"] == sid]


class X25Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config_dir = self.tmp / "etc"
        self.config_dir.mkdir()
        self.state_dir = self.tmp / "state"
        self.device = self.tmp / "ttyACM0"
        self.device.write_text("")

    def run_main(self, argv, conn=None, env=None, config=None, device=True, router=None):
        """`conn` is the USB link; `router` (if given) the link through mavlink-router used by the Ethernet check."""
        conn = conn or FakeConn()

        def connect(url, *a, **k):
            return router if router is not None and url.startswith("tcp:") else conn

        if config is not None:
            (self.config_dir / "config.env").write_text(config)
        environ = {"UAVLINK_EVENTS": "1", "UAVLINK_CONFIG_DIR": str(self.config_dir),
                   "UAVLINK_STATE_DIR": str(self.state_dir), "UAVLINK_ALLOW_WSL": "1"}
        environ.update(env or {})
        out = io.StringIO()
        real_time = time.time
        with mock.patch.dict(os.environ, environ), \
                mock.patch.object(x25.mavutil, "mavlink_connection", connect), \
                mock.patch.object(x25, "find_device", lambda: str(self.device) if device else None), \
                mock.patch.object(x25.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)), \
                mock.patch.object(x25.time, "sleep", lambda s: None), \
                mock.patch.object(x25.time, "time", lambda: real_time() + conn.skew + (router.skew if router else 0)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = x25.main(argv)
            except SystemExit as e:
                rc = e.code
        events = [json.loads(line[len("@@UAVLINK "):]) for line in out.getvalue().splitlines()
                  if line.startswith("@@UAVLINK ")]
        return Run(rc, events, out.getvalue(), conn, self.tmp)


class EnvFileTest(unittest.TestCase):
    def parse(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.env"
            p.write_text(text, encoding="utf-8")
            return x25.read_env_file(str(p))

    def test_quoting_rules(self):
        values = self.parse(
            "# comment\n\n"
            "A=plain   # trailing comment\n"
            "B=\"two words\" # c\n"
            "C='it'\\''s'\n"
            "D=\"q\\\"uote\\\\ \\$HOME\"\n"
            "export E=1\n"
            "F=\n"
            "G=a#b\n"
            "H=$(touch /nonexistent/x) `id`\n"
            "bad line\n"
            "1X=nope\n"
        )
        self.assertEqual(values["A"], "plain")
        self.assertEqual(values["B"], "two words")
        self.assertEqual(values["C"], "it's")
        self.assertEqual(values["D"], 'q"uote\\ $HOME')
        self.assertEqual(values["E"], "1")
        self.assertEqual(values["F"], "")
        self.assertEqual(values["G"], "a#b")
        self.assertEqual(values["H"], "$(touch /nonexistent/x) `id`")  # never executed
        self.assertNotIn("1X", values)
        self.assertEqual(len(values), 8)

    def test_missing_file_is_empty(self):
        self.assertEqual(x25.read_env_file("/nonexistent/config.env"), {})

    def test_secrets_win_and_environment_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.env").write_text("X25_IP=10.0.0.1\nMAVLINK_SIGNING_PASSPHRASE='from-config'\n")
            (Path(d) / "secrets.env").write_text("MAVLINK_SIGNING_PASSPHRASE='from-secrets'\n")
            cfg = x25.load_config(d)
            self.assertEqual(cfg["X25_IP"], "10.0.0.1")
            self.assertEqual(cfg["MAVLINK_SIGNING_PASSPHRASE"], "from-secrets")
            with mock.patch.dict(x25.CONFIG, cfg, clear=True), mock.patch.dict(os.environ, {"X25_IP": "10.9.9.9"}):
                self.assertEqual(x25.env("X25_IP", "1.1.1.1"), "10.9.9.9")
                self.assertEqual(x25.env("MAVLINK_SIGNING_PASSPHRASE"), "from-secrets")
                self.assertEqual(x25.env("MISSING", "dflt"), "dflt")


class PreviewTest(X25Case):
    def test_preview_reports_changes_and_never_writes(self):
        r = self.run_main(["--preview", "--signing", "on"], env={"MAVLINK_SIGNING_PASSPHRASE": PASSPHRASE})
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.conn.sets, [])
        self.assertEqual(r.conn.reboots, 0)
        self.assertEqual(r.conn.signing, [])
        self.assertFalse(self.state_dir.exists(), "preview must not write a backup")
        changes = {e["target"]: e for e in r.of("change")}
        self.assertEqual(changes["NET_ENABLE"], {"t": "change", "kind": "param", "target": "NET_ENABLE",
                                                 "action": "set", "old": "0", "new": "1"})
        self.assertEqual(changes["NET_P1_TYPE"]["new"], "1")
        # not on the board yet: reported with an empty old value, new value from the config defaults
        self.assertEqual((changes["NET_IPADDR3"]["old"], changes["NET_IPADDR3"]["new"]), ("", "1"))
        self.assertEqual(changes["NET_P1_PORT"]["new"], "14550")
        self.assertEqual(changes["NET_P1_IP3"]["new"], "2")
        self.assertTrue(r.of("info", code="x25.signing_pending"))
        self.assertEqual(r.steps("x25.backup"), ["skip"])
        self.assertEqual(r.steps("x25.signing"), ["start", "skip"])
        self.assertEqual(r.steps("x25.ethcheck"), ["skip"])
        self.assertEqual(r.of("check", id="x25.usb")[0]["ok"], True)
        self.assertEqual(r.of("check", id="x25.disarmed")[0]["ok"], True)
        self.assertEqual(r.of("check", id="x25.firmware")[0]["args"], {"version": "4.7.1"})
        self.assertNotIn(PASSPHRASE, r.out)
        self.assertNotIn(x25.signing_key(PASSPHRASE).hex(), r.out)

    def test_dry_run_is_an_alias(self):
        r = self.run_main(["--dry-run"])
        self.assertEqual((r.rc, r.conn.sets, r.conn.reboots), (0, [], 0))
        self.assertTrue(r.of("change", target="NET_ENABLE"))

    def test_preview_when_already_configured(self):
        conn = FakeConn()
        conn.params.update({"NET_ENABLE": 1.0, "NET_P1_TYPE": 1.0, "NET_IPADDR0": 192.0, "NET_IPADDR1": 168.0,
                            "NET_IPADDR2": 50.0, "NET_IPADDR3": 1.0, "NET_NETMASK": 24.0, "NET_P1_IP0": 192.0,
                            "NET_P1_IP1": 168.0, "NET_P1_IP2": 50.0, "NET_P1_IP3": 2.0, "NET_P1_PORT": 14550.0,
                            "NET_P1_PROTOCOL": 2.0, "NET_DHCP": 0.0})
        conn.hidden.clear()
        r = self.run_main(["--preview"], conn=conn)
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.of("change"), [])
        self.assertEqual(r.steps("x25.params"), ["start", "ok"])

    def test_preview_uses_config_file_values(self):
        r = self.run_main(["--preview"], config="CM5_LAN_IP=10.1.2.3  # cm5\nMAVLINK_LAN_PORT='14551'\nX25_IP=\"10.1.2.1\"\n")
        changes = {e["target"]: e["new"] for e in r.of("change")}
        self.assertEqual([changes[f"NET_P1_IP{i}"] for i in range(4)], ["10", "1", "2", "3"])
        self.assertEqual(changes["NET_P1_PORT"], "14551")
        self.assertEqual(changes["NET_IPADDR3"], "1")

    def test_preview_reports_armed_but_exits_zero(self):
        r = self.run_main(["--preview"], conn=FakeConn(armed=True))
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.of("check", id="x25.disarmed")[0]["ok"], False)
        self.assertEqual(r.conn.sets, [])

    def test_no_x25_in_preview(self):
        r = self.run_main(["--preview"], device=False)
        self.assertEqual(r.rc, 0)
        usb = r.of("check", id="x25.usb")[0]
        self.assertEqual((usb["ok"], usb["code"]), (False, "x25.not_found"))
        self.assertEqual([e["id"] for e in r.of("check")], ["x25.usb"])
        self.assertEqual(r.of("info"), [])


HEALTH_CHECKS = ("x25.sensors", "x25.gps", "x25.battery", "x25.ekf", "x25.prearm")


class HealthTest(X25Case):
    def checks(self, r):
        return {e["id"]: e for e in r.of("check")}

    def status(self, r):
        infos = r.of("info", code="x25.status")
        self.assertEqual(len(infos), 1)
        return infos[0]

    def snapshot(self, conn):
        """health_snapshot() alone; returns (snapshot, virtual seconds it took)."""
        fc = x25.FlightController("fake", 115200)
        fc.conn, fc.sysid, fc.compid = conn, 1, 1
        real_time = time.time
        with mock.patch.object(x25.time, "time", lambda: real_time() + conn.skew):
            start = x25.time.time()
            snap = fc.health_snapshot()
            return snap, x25.time.time() - start

    def test_healthy_board_passes_and_reports_status(self):
        r = self.run_main(["--preview"])
        self.assertEqual(r.rc, 0)
        checks = self.checks(r)
        for cid in HEALTH_CHECKS:
            self.assertTrue(checks[cid]["ok"], cid)
            self.assertNotIn("severity", checks[cid])
        self.assertEqual(checks["x25.gps"]["args"], {"sats": 12})
        self.assertEqual(checks["x25.battery"]["args"], {"voltage": "16.4"})
        self.assertEqual(checks["x25.firmware"]["args"], {"version": "4.7.1"})
        info = self.status(r)
        self.assertEqual(info["msg"], "ArduCopter V4.7.1 (3d4f2a1b) on CUAV-X25-EVO, quadrotor, disarmed")
        d = info["data"]
        self.assertEqual(list(d), ["firmware", "board", "vehicle", "system_status", "armed", "sensors", "gps",
                                   "battery", "ekf", "prearm", "net"])
        self.assertEqual((d["firmware"], d["board"], d["vehicle"], d["system_status"], d["armed"]),
                         ("ArduCopter V4.7.1 (3d4f2a1b)", "CUAV-X25-EVO", "quadrotor", "standby", False))
        self.assertEqual([s["id"] for s in d["sensors"]],
                         ["gyro", "accel", "mag", "baro", "gps", "rc", "ahrs", "battery", "prearm"])
        self.assertTrue(all(s == {"id": s["id"], "present": True, "enabled": True, "healthy": True}
                            for s in d["sensors"]))
        self.assertEqual(d["gps"], {"fix": 3, "sats": 12, "hdop": 0.8})
        self.assertEqual(d["battery"], {"voltage": 16.4, "current": 1.2, "remaining": 87})
        self.assertEqual(d["ekf"], {"ok": True, "flags": EKF_GOOD})
        self.assertEqual(d["prearm"], [])
        # a fresh board shows only NET_ENABLE / NET_P1_TYPE until it is rebooted with NET_ENABLE=1
        self.assertEqual(d["net"], {"NET_ENABLE": 0, "NET_P1_TYPE": 0})

    def test_bench_without_gps_and_battery_only_warns(self):
        bits = without("gps", "battery", "prearm")
        conn = FakeConn(health=health(
            SYS_STATUS={"onboard_control_sensors_health": bits, "voltage_battery": 0, "current_battery": -1,
                        "battery_remaining": -1},
            GPS_RAW_INT={"fix_type": 1, "satellites_visible": 3, "eph": 0xFFFF},
            EKF_STATUS_REPORT={"flags": MAV.EKF_ATTITUDE | MAV.EKF_CONST_POS_MODE, "pos_horiz_variance": 0.9}),
            prearm=["PreArm: Compass not calibrated", "PreArm: GPS: no fix", "PreArm: Compass not calibrated"])
        r = self.run_main(["--preview"], conn=conn)
        self.assertEqual(r.rc, 0)
        checks = self.checks(r)
        self.assertTrue(checks["x25.sensors"]["ok"])  # GPS, battery and pre-arm have their own checks
        for cid, code in (("x25.gps", "x25.gps_no_fix"), ("x25.battery", "x25.battery_absent"),
                          ("x25.ekf", "x25.ekf_bad"), ("x25.prearm", "x25.prearm_failed")):
            self.assertEqual((checks[cid]["ok"], checks[cid]["code"], checks[cid]["severity"]), (False, code, "warn"))
        self.assertEqual(checks["x25.gps"]["args"], {"sats": 3})
        self.assertEqual(checks["x25.prearm"]["args"], {"text": "Compass not calibrated"})
        d = self.status(r)["data"]
        self.assertEqual(d["prearm"], ["PreArm: Compass not calibrated", "PreArm: GPS: no fix"])
        self.assertEqual(d["gps"], {"fix": 1, "sats": 3, "hdop": None})
        self.assertEqual(d["battery"], {"voltage": 0.0, "current": None, "remaining": None})
        self.assertEqual(d["ekf"]["ok"], False)
        self.assertEqual([s["id"] for s in d["sensors"] if not s["healthy"]], ["gps", "battery", "prearm"])

    def test_hardware_sensor_fault_fails(self):
        conn = FakeConn(health=health(SYS_STATUS={"onboard_control_sensors_health": without("mag", "rc")}))
        sensors = self.checks(self.run_main(["--preview"], conn=conn))["x25.sensors"]
        self.assertEqual((sensors["ok"], sensors["code"], sensors["args"]), (False, "x25.sensors_unhealthy",
                                                                             {"names": "mag, rc"}))
        self.assertNotIn("severity", sensors)

    def test_rc_or_ahrs_fault_only_warns_and_absent_sensors_do_not_count(self):
        conn = FakeConn(health=health(SYS_STATUS={"onboard_control_sensors_present": without("baro"),
                                                  "onboard_control_sensors_health": without("baro", "rc", "ahrs")}))
        sensors = self.checks(self.run_main(["--preview"], conn=conn))["x25.sensors"]
        self.assertEqual((sensors["ok"], sensors["args"], sensors["severity"]), (False, {"names": "rc, ahrs"}, "warn"))

    def test_low_battery_and_prearm_bit_without_text(self):
        conn = FakeConn(health=health(SYS_STATUS={"onboard_control_sensors_health": without("battery", "prearm"),
                                                  "voltage_battery": 13100}))
        checks = self.checks(self.run_main(["--preview"], conn=conn))
        bat, pre = checks["x25.battery"], checks["x25.prearm"]
        self.assertEqual((bat["ok"], bat["code"], bat["args"], bat["severity"]),
                         (False, "x25.battery_low", {"voltage": "13.1"}, "warn"))
        self.assertEqual((pre["ok"], pre["code"], pre["severity"]), (False, "x25.prearm_unhealthy", "warn"))

    def test_long_statustext_chunks_are_joined(self):
        conn = FakeConn(prearm=[{"text": "PreArm: Battery 1 below minimum arming", "id": 7, "chunk_seq": 0},
                                {"text": " voltage", "id": 7, "chunk_seq": 1}])
        snap, _ = self.snapshot(conn)
        self.assertIn("PreArm: Battery 1 below minimum arming voltage", snap["texts"])

    def test_silent_board_warns_and_is_bounded_in_time(self):
        silent = {name: None for name in HEALTHY}
        conn = FakeConn(health=silent, banner=[])
        r = self.run_main(["--preview"], conn=conn)
        self.assertEqual(r.rc, 0)
        checks = self.checks(r)
        self.assertEqual(checks["x25.firmware"]["args"], {"version": "unknown"})
        for cid, code in (("x25.sensors", "x25.no_sys_status"), ("x25.gps", "x25.gps_none"),
                          ("x25.battery", "x25.battery_absent"), ("x25.ekf", "x25.ekf_missing")):
            self.assertEqual((checks[cid]["ok"], checks[cid]["code"], checks[cid]["severity"]), (False, code, "warn"))
        d = self.status(r)["data"]
        self.assertEqual((d["firmware"], d["board"], d["sensors"], d["prearm"]), (None, None, [], []))
        self.assertEqual(d["gps"], {"fix": None, "sats": None, "hdop": None})
        self.assertEqual(d["ekf"], {"ok": None, "flags": None})
        self.assertEqual(self.status(r)["msg"], "unknown firmware on unknown board, quadrotor, disarmed")
        # a board that answers nothing costs at most HEALTH_SECS (+1 for the last wait), within the 8 s budget
        _, secs = self.snapshot(FakeConn(health=silent, banner=[]))
        self.assertLessEqual(secs, x25.HEALTH_SECS + 1)
        self.assertLessEqual(x25.HEALTH_SECS + 1, 8)
        # a board that answers everything is done about 1 s after the pre-arm ACK
        _, secs = self.snapshot(FakeConn())
        self.assertLess(secs, 3)

    def test_board_name_falls_back_to_board_id(self):
        conn = FakeConn(banner=[])
        d = self.status(self.run_main(["--preview"], conn=conn))["data"]
        self.assertEqual((d["firmware"], d["board"]), ("4.7.1", "board id 1234"))

    def test_snapshot_is_read_only(self):
        r = self.run_main(["--preview"], conn=FakeConn(prearm=["PreArm: RC not calibrated"]))
        conn = r.conn
        self.assertEqual((conn.sets, conn.reboots, conn.signing, conn.stream_requests), ([], 0, [], []))
        self.assertLessEqual({c for c, _ in conn.commands}, set(x25.HEALTH_COMMANDS))
        requested = {int(p) for c, p in conn.commands if c == MAV.MAV_CMD_REQUEST_MESSAGE}
        self.assertEqual(requested, set(MSG_NAMES))
        self.assertFalse(self.state_dir.exists())


class ApplyTest(X25Case):
    def test_full_run_writes_reboots_signs_and_backs_up(self):
        r = self.run_main(["--skip-eth-check", "--signing", "on"], env={"MAVLINK_SIGNING_PASSPHRASE": PASSPHRASE})
        self.assertEqual(r.rc, 0, r.out)
        conn = r.conn
        self.assertEqual(conn.reboots, 2)
        self.assertEqual(conn.signing, [x25.signing_key(PASSPHRASE)] * 2)
        self.assertEqual(conn.params["NET_ENABLE"], 1)
        self.assertEqual(conn.params["NET_IPADDR3"], 1)
        self.assertEqual(conn.params["NET_P1_IP3"], 2)
        self.assertEqual(conn.params["NET_P1_PROTOCOL"], 2)
        self.assertEqual(conn.params["NET_DHCP"], 0)
        # backup holds the values before anything was written
        backups = list((self.state_dir / "x25-backups").glob("x25_params_*.parm"))
        self.assertEqual(len(backups), 1)
        saved = x25.load_parm(str(backups[0]))
        self.assertEqual(saved["NET_ENABLE"], 0)
        info = r.of("info", code="x25.backup_written")[0]
        self.assertEqual(info["args"]["file"], str(backups[0]))
        # events
        self.assertEqual(r.steps("x25.connect"), ["start", "ok"])
        self.assertEqual(r.steps("x25.backup"), ["start", "ok"])
        self.assertEqual(r.steps("x25.params"), ["start", "ok"])
        self.assertEqual(r.steps("x25.reboot"), ["start", "ok", "start", "ok"])
        self.assertEqual(r.steps("x25.signing"), ["start", "ok"])
        self.assertEqual(r.steps("x25.ethcheck"), ["skip"])
        changed = {e["target"]: (e["old"], e["new"]) for e in r.of("change")}
        self.assertEqual(changed["NET_ENABLE"], ("0", "1"))
        self.assertEqual(changed["NET_IPADDR3"], ("14", "1"))
        self.assertEqual(len(r.of("change")), len(conn.sets))
        self.assertNotIn(PASSPHRASE, r.out)
        self.assertNotIn(x25.signing_key(PASSPHRASE).hex(), r.out)
        self.assertTrue(all(e["t"] in {"step", "check", "change", "info"} for e in r.events))
        # the health snapshot also runs at the start of a real run, before anything is written
        status = r.of("info", code="x25.status")
        self.assertEqual(len(status), 1)
        self.assertEqual(status[0]["data"]["net"], {"NET_ENABLE": 0, "NET_P1_TYPE": 0})
        self.assertTrue(all(r.of("check", id=cid) for cid in HEALTH_CHECKS))
        self.assertEqual(conn.stream_requests, [])
        self.assertNotIn(MAV.MAV_CMD_SET_MESSAGE_INTERVAL, [c for c, _ in conn.commands])

    def test_signing_off_sends_zero_key(self):
        r = self.run_main(["--skip-eth-check", "--no-backup", "--signing", "off"])
        self.assertEqual(r.rc, 0)
        self.assertEqual(r.conn.signing, [bytes(32)] * 2)
        self.assertFalse(self.state_dir.exists())

    def test_signing_on_without_passphrase_is_refused(self):
        r = self.run_main(["--signing", "on"])
        self.assertEqual(r.rc, 2)
        self.assertEqual(r.conn.sets, [])
        self.assertEqual(r.of("step", state="fail")[0]["code"], "x25.no_passphrase")

    def test_armed_is_refused(self):
        r = self.run_main(["--skip-eth-check"], conn=FakeConn(armed=True))
        self.assertEqual(r.rc, 4)
        self.assertEqual((r.conn.sets, r.conn.reboots, r.conn.signing), ([], 0, []))
        self.assertEqual(r.of("step", state="fail")[0]["code"], "x25.armed")
        self.assertFalse(self.state_dir.exists())

    def test_no_x25_fails(self):
        r = self.run_main([], device=False)
        self.assertEqual(r.rc, 1)
        self.assertEqual(r.of("check", id="x25.usb")[0]["ok"], False)
        self.assertEqual(r.of("step", state="fail")[0]["code"], "x25.not_found")

    def test_wsl_refuses_everything_but_preview(self):
        with mock.patch.object(x25, "is_wsl", lambda: True):
            r = self.run_main([], env={"UAVLINK_ALLOW_WSL": ""})
            self.assertEqual(r.rc, 4)
            self.assertEqual(r.of("step", state="fail")[0]["code"], "env.wsl")
            self.assertEqual(self.run_main(["--preview"], env={"UAVLINK_ALLOW_WSL": ""}).rc, 0)

    def test_restore_needs_yes_and_skips_ids(self):
        parm = self.tmp / "old.parm"
        parm.write_text("NET_ENABLE,1\nNET_DHCP,1\nSTAT_RUNTIME,5\nGPS_TYPE,9\n")
        conn = FakeConn()
        conn.params.update({"NET_ENABLE": 1.0, "NET_DHCP": 0.0})
        conn.hidden.clear()
        r = self.run_main(["--restore", str(parm)], conn=conn)
        self.assertEqual((r.rc, conn.sets, conn.reboots), (0, [], 0))
        self.assertEqual([e["target"] for e in r.of("change")], ["NET_DHCP"])
        self.assertEqual(r.of("info", code="x25.status")[0]["data"]["net"]["NET_DHCP"], 0)
        r = self.run_main(["--restore", str(parm), "--yes", "--preview"], conn=conn)
        self.assertEqual((conn.sets, conn.reboots), ([], 0))
        r = self.run_main(["--restore", str(parm), "--yes"], conn=conn)
        self.assertEqual(r.rc, 0)
        self.assertEqual(conn.sets, [("NET_DHCP", 1.0)])
        self.assertEqual(conn.reboots, 1)


class RealFirmwareTest(X25Case):
    """Bugs found running x25_configure.py against ArduPilot SITL 4.7.1 (tests/integration/sitl.sh)."""

    def configured(self):
        conn = FakeConn()
        conn.params.update({"NET_ENABLE": 1.0, "NET_P1_TYPE": 1.0, "NET_IPADDR0": 192.0, "NET_IPADDR1": 168.0,
                            "NET_IPADDR2": 50.0, "NET_IPADDR3": 1.0, "NET_NETMASK": 24.0, "NET_P1_IP0": 192.0,
                            "NET_P1_IP1": 168.0, "NET_P1_IP2": 50.0, "NET_P1_IP3": 2.0, "NET_P1_PORT": 14550.0,
                            "NET_P1_PROTOCOL": 2.0, "NET_DHCP": 0.0})
        conn.hidden.clear()
        return conn

    def test_network_address_as_the_usb_device(self):
        # X25_SERIAL=tcp:host:port (SITL SERIAL0) was rejected: the device had to be an existing file
        r = self.run_main(["--preview"], config="X25_SERIAL='tcp:127.0.0.1:5760'\n", device=False)
        self.assertEqual(r.rc, 0, r.out)
        usb = r.of("check", id="x25.usb")[0]
        self.assertEqual((usb["ok"], usb["args"]), (True, {"device": "tcp:127.0.0.1:5760"}))
        self.assertTrue(r.of("info", code="x25.status"))
        for dev in ("tcp:127.0.0.1:5760", "udpin:0.0.0.0:14550", "udpout:10.0.0.1:14550"):
            self.assertTrue(x25.is_net_device(dev), dev)
        for dev in ("/dev/ttyACM0", "tcp:127.0.0.1", "serial:/dev/ttyACM0:57600", "", None):
            self.assertFalse(x25.is_net_device(dev), dev)

    def test_board_that_has_no_host_ip_params(self):
        # SITL / Linux boards never show NET_IPADDR*, NET_NETMASK: this failed with x25.still_missing after 2 reboots
        conn = FakeConn(hidden=SITL_HIDDEN)
        r = self.run_main(["--skip-eth-check", "--no-backup"], conn=conn)
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(conn.reboots, 2)
        info = r.of("info", code="x25.ip_by_os")
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["args"], {"names": ", ".join(x25.HOST_IP_PARAMS)})
        self.assertEqual([n for n, _ in conn.sets],
                         ["NET_ENABLE", "NET_P1_TYPE", "NET_P1_IP0", "NET_P1_IP1", "NET_P1_IP2", "NET_P1_IP3",
                          "NET_P1_PORT", "NET_P1_PROTOCOL"])
        self.assertEqual(r.steps("x25.params"), ["start", "ok"])

    def test_x25_with_host_ip_params_still_needs_them(self):
        r = self.run_main(["--skip-eth-check", "--no-backup"])
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(r.of("info", code="x25.ip_by_os"), [])
        self.assertIn(("NET_IPADDR3", 1), r.conn.sets)
        # a board whose NET_ tree never appears after the reboot still fails, as before
        conn = FakeConn()
        conn.reveal_on_reboot = False
        r = self.run_main(["--skip-eth-check", "--no-backup"], conn=conn)
        self.assertEqual(r.rc, 1)
        self.assertEqual(r.of("step", state="fail")[0]["code"], "x25.still_missing")

    def test_failure_after_a_reboot_closes_the_params_step(self):
        # x25.reboot "ok" cleared the open step, so a failure in round 2 sent no "step fail" and no error code
        conn = FakeConn()
        conn.refuse.add("NET_P1_IP3")
        r = self.run_main(["--skip-eth-check", "--no-backup"], conn=conn)
        self.assertEqual(r.rc, 1)
        self.assertEqual(r.steps("x25.reboot"), ["start", "ok"])
        self.assertEqual(r.steps("x25.params"), ["start", "fail"])
        failed = r.of("step", id="x25.params", state="fail")[0]
        self.assertEqual((failed["code"], failed["args"]), ("x25.set_failed", {"name": "NET_P1_IP3", "value": "2"}))

    def test_failure_inside_a_nested_step_closes_both(self):
        x25._open_steps[:] = []
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"UAVLINK_EVENTS": "1"}):
            x25.ev_step("x25.params", "start")
            x25.ev_step("x25.reboot", "start")
            with self.assertRaises(SystemExit):
                x25.fail("gone", 1, "x25.connect_failed", {"device": "d", "secs": 90})
        evs = [json.loads(line[len("@@UAVLINK "):]) for line in out.getvalue().splitlines()]
        self.assertEqual(evs[2:], [
            {"t": "step", "id": "x25.reboot", "state": "fail", "code": "x25.connect_failed",
             "args": {"device": "d", "secs": 90}},
            {"t": "step", "id": "x25.params", "state": "fail"}])
        self.assertEqual(x25._open_steps, [])

    def test_params_ok_only_when_nothing_was_written(self):
        # "x25.params_ok" says "no write is needed": it was also sent after writing in earlier rounds
        r = self.run_main(["--skip-eth-check", "--no-backup"])
        self.assertTrue(r.conn.sets)
        self.assertEqual(r.of("step", id="x25.params", state="ok")[0].get("code"), None)
        r = self.run_main(["--skip-eth-check", "--no-backup"], conn=self.configured())
        self.assertEqual((r.conn.sets, r.conn.reboots), ([], 0))
        self.assertEqual(r.of("step", id="x25.params", state="ok")[0].get("code"), "x25.params_ok")

    def test_ethernet_check_on_a_board_without_net_ipaddr(self):
        # the check read NET_IPADDR3, which SITL / Linux boards do not have: no answer, exit 3
        key = x25.signing_key(PASSPHRASE)
        router = FakeConn(params={"NET_ENABLE": 1.0, "NET_OPTIONS": 0.0, "NET_P1_PORT": 14550.0}, hidden={})
        router.need_key = key
        r = self.run_main(["--no-backup", "--signing", "on"], conn=FakeConn(hidden=SITL_HIDDEN), router=router,
                          env={"MAVLINK_SIGNING_PASSPHRASE": PASSPHRASE})
        self.assertEqual(r.rc, 0, r.out)
        self.assertEqual(r.steps("x25.ethcheck"), ["start", "ok"])
        self.assertIn(f"{x25.PROBE_PARAM} qua Ethernet", r.out)

    def test_ethernet_check_failures(self):
        env = {"MAVLINK_SIGNING_PASSPHRASE": PASSPHRASE}
        for need_key in (x25.signing_key("another passphrase"), None):    # wrong key on the board; signing not enforced
            router = FakeConn(params={"NET_ENABLE": 1.0}, hidden={})
            router.need_key = need_key
            r = self.run_main(["--signing", "on"], conn=self.configured(), router=router, env=env)
            self.assertEqual(r.rc, 3, r.out)
            failed = r.of("step", id="x25.ethcheck", state="fail")[0]
            self.assertEqual(failed["code"], "x25.ethcheck_failed")


class BackupListTest(X25Case):
    def test_list_backups_json(self):
        d = self.tmp / "bk"
        d.mkdir()
        old, new = d / "x25_params_20260101-000000.parm", d / "x25_params_20260202-000000.parm"
        old.write_text("A,1.0\nB,2.0\n")
        new.write_text("A,1.0\n# note\nB,2.0\nC,3.0\n")
        (d / "unrelated.txt").write_text("x")
        os.utime(old, (1_000_000_000, 1_000_000_000))
        os.utime(new, (1_100_000_000, 1_100_000_000))
        r = self.run_main(["--list-backups", "--backup-dir", str(d)], conn=FakeConn())
        self.assertEqual(r.rc, 0)
        items = json.loads(r.out)  # the whole stdout is the JSON list
        self.assertEqual([i["file"] for i in items], [str(new), str(old)])
        self.assertEqual([i["params"] for i in items], [3, 2])
        self.assertRegex(items[0]["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(set(items[0]), {"file", "time", "params"})

    def test_missing_dir_is_empty_list(self):
        r = self.run_main(["--list-backups"])
        self.assertEqual(json.loads(r.out), [])
        self.assertEqual(r.events, [])


class VendorTest(unittest.TestCase):
    def test_isolated_import_from_vendor(self):
        code = ("import sys; sys.path.insert(0, %r); from pymavlink import mavutil; print(mavutil.mavlink.WIRE_PROTOCOL_VERSION)"
                % str(REPO / "vendor"))
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "MAVLINK20", "MAVLINK_DIALECT")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        out = subprocess.run([sys.executable, "-I", "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "2.0")

    def test_no_compiled_modules(self):
        compiled = [p for p in (REPO / "vendor").rglob("*") if p.suffix in (".so", ".pyd", ".dll")]
        self.assertEqual(compiled, [])

    def test_mav_probe_falls_back_to_vendor(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        out = subprocess.run([sys.executable, "-I", str(REPO / "engine" / "tools" / "mav_probe.py"), "--help"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--check-signing", out.stdout)

    def test_x25_configure_falls_back_to_vendor(self):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "UAVLINK_EVENTS")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        out = subprocess.run([sys.executable, "-I", str(REPO / "engine" / "x25" / "x25_configure.py"), "--help"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--list-backups", out.stdout)


class I18nTest(unittest.TestCase):
    def test_every_emitted_id_is_translated(self):
        data = json.loads((REPO / "share" / "i18n" / "engine-x25.json").read_text(encoding="utf-8"))
        for key, text in data.items():
            self.assertEqual(sorted(text), ["en", "vi"], key)
            self.assertTrue(text["vi"] and text["en"], key)
        source = (REPO / "engine" / "x25" / "x25_configure.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'"((?:x25|env)\.[a-z_]+)"', source))
        self.assertTrue(emitted)
        self.assertEqual(sorted(emitted - set(data)), [])

    def test_placeholders_are_in_the_args_the_code_sends(self):
        data = json.loads((REPO / "share" / "i18n" / "engine-x25.json").read_text(encoding="utf-8"))
        known = {"version", "device", "secs", "autopilot", "got", "total", "file", "count", "name", "value", "names",
                 "rounds", "result", "sats", "voltage", "text"}
        for key, text in data.items():
            for lang, s in text.items():
                self.assertLessEqual(set(re.findall(r"\{(\w+)\}", s)), known, f"{key}/{lang}")


if __name__ == "__main__":
    unittest.main()
