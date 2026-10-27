#!/usr/bin/env python3
"""Mock of the uavlink setup server (ARCHITECTURE.md section 6) for developing and screenshotting share/web/.

Serves share/web/ and fake API answers on 127.0.0.1:8091, without authentication. Uses the real share/schema.json,
share/presets/*.json and share/i18n/engine-*.json when they exist, a small built-in sample otherwise; texts for the
codes the mock emits that the engine files do not have yet come from MOCK_ENGINE. Jobs play a scripted event stream
(stages cm5, x25, net; steps, checks of ARCHITECTURE.md section 4.3 with some warnings and one failure, the x25.status
info event, changes with parameters and a diff, rollback armed). --no-x25 drops the X25 checks and the status event.

    python3 tools/mock-server.py [--port 8091] [--host 127.0.0.1] [--speed 1.0] [--preview-only] [--armed] [--no-x25]
                                 [--fail-apply] [--vps]
"""
import argparse
import glob
import json
import mimetypes
import os
import random
import re
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARE = os.path.join(ROOT, "share")
WEB = os.path.join(SHARE, "web")
with open(os.path.join(ROOT, "py", "uavlink", "__init__.py"), encoding="utf-8") as _f:
    VERSION = re.search(r'VERSION = "([^"]+)"', _f.read()).group(1) + "-mock"


def bi(vi, en):
    return {"vi": vi, "en": en}


# ---------------------------------------------------------------- sample data (used when share/schema.json is absent)

SAMPLE_TABS = [
    {"id": "network", "label": bi("Mạng", "Network")},
    {"id": "x25", "label": bi("Mạch bay X25", "Flight controller X25")},
    {"id": "links", "label": bi("Đường truyền", "Links")},
    {"id": "security", "label": bi("Bảo mật", "Security")},
    {"id": "wifi", "label": bi("Wi-Fi", "Wi-Fi")},
    {"id": "vps", "label": bi("Máy chủ VPS", "VPS")},
    {"id": "web", "label": bi("Web Admin", "Web Admin")},
]


def key(roles, tab, typ, default, vi, en, hvi="", hen="", **kw):
    d = {"roles": roles, "tab": tab, "type": typ, "default": default, "label": bi(vi, en),
         "help": bi(hvi, hen), "advanced": False, "secret": False, "required": True}
    d.update(kw)
    return d


CM5, VPS = ["cm5"], ["vps"]
SAMPLE_KEYS = {
    "CM5_LAN_IP": key(CM5, "network", "ipv4", "192.168.50.2", "IP của CM5 trên cáp Ethernet", "CM5 address on the Ethernet cable",
                      "Địa chỉ tĩnh của CM5 trên mạng nối với mạch bay.", "Static address of the CM5 on the flight controller network."),
    "CM5_ETH_IFACE": key(CM5, "network", "iface", "eth0", "Cổng Ethernet", "Ethernet interface",
                         "Cổng mạng nối tới mạch bay.", "The network port wired to the flight controller."),
    "CM5_MAVLINK_ALLOW_LAN": key(CM5, "network", "bool", "0", "Mở MAVLink cho mạng LAN/Wi-Fi nhà", "Open MAVLink to the home LAN/Wi-Fi",
                                 "Chỉ dùng khi thử nghiệm trên bàn.", "Bench testing only.", advanced=True, required=False),
    "X25_IP": key(CM5, "x25", "ipv4", "192.168.50.1", "IP của mạch bay X25", "Flight controller address",
                  "Ghi vào tham số NET_IPADDR của mạch bay.", "Written to the NET_IPADDR parameters."),
    "X25_NETMASK": key(CM5, "x25", "int", "24", "Độ dài mặt nạ mạng", "Network prefix length", min=8, max=30, advanced=True),
    "X25_SERIAL_PORT": key(CM5, "x25", "path", "", "Cổng USB của mạch bay", "Flight controller USB port",
                           "Để trống để tự tìm.", "Leave empty to detect it.", allow_empty=True, required=False, advanced=True),
    "MAVLINK_LAN_PORT": key(CM5, "links", "port", "14550", "Cổng UDP MAVLink trên LAN", "MAVLink UDP port on the LAN"),
    "CM5_TCP_SERVER_PORT": key(CM5, "links", "port", "5760", "Cổng TCP cho trạm mặt đất", "TCP port for ground stations"),
    "VPS_PUBLIC_IP": key(CM5, "links", "host", "", "Địa chỉ VPS", "VPS address",
                         "Máy chủ chuyển tiếp trên Internet.", "The relay server on the Internet.", allow_empty=True, required=False),
    "VPS_UDP_PORT": key(CM5, "links", "port", "14550", "Cổng UDP tới VPS", "UDP port to the VPS", advanced=True),
    "MAVLINK_SIGNING": key(CM5, "security", "bool", "1", "Ký gói MAVLink 2", "Sign MAVLink 2 packets",
                           "Mạch bay bỏ qua lệnh không có chữ ký đúng.", "The flight controller ignores unsigned commands."),
    "MAVLINK_SIGNING_PASSPHRASE": key(CM5, "security", "passphrase", "", "Passphrase ký", "Signing passphrase",
                                      "Để trống để tự sinh (hoặc giữ cái cũ).", "Leave empty to generate (or keep the old one).",
                                      secret=True, required=False, show_if={"MAVLINK_SIGNING": "1"}),
    "CM5_ALLOWED_SSH": key(CM5, "security", "source_list", "any", "Nguồn được SSH", "Allowed SSH sources",
                           "any hoặc danh sách IP / mạng.", "any, or a list of IPs / networks.", advanced=True),
    "TS_AUTHKEY": key(CM5, "security", "string", "", "Khoá Tailscale", "Tailscale auth key", secret=True, required=False,
                      allow_empty=True, advanced=True),
    "CM5_WIFI_ENABLE": key(CM5, "wifi", "bool", "0", "Phát Wi-Fi", "Run a Wi-Fi hotspot"),
    "CM5_WIFI_SSID": key(CM5, "wifi", "string", "uavlink", "Tên Wi-Fi", "Wi-Fi name", show_if={"CM5_WIFI_ENABLE": "1"}),
    "CM5_WIFI_PSK": key(CM5, "wifi", "string", "", "Mật khẩu Wi-Fi", "Wi-Fi password", secret=True, required=False,
                        show_if={"CM5_WIFI_ENABLE": "1"}),
    "CM5_LOG_LEVEL": key(CM5, "links", "enum", "info", "Mức nhật ký router", "Router log level", advanced=True,
                         options=[{"value": "error", "label": bi("Chỉ lỗi", "Errors only")},
                                  {"value": "info", "label": bi("Thông tin", "Info")},
                                  {"value": "debug", "label": bi("Gỡ lỗi", "Debug")}]),
    "VPS_PUBLIC_HOST": key(VPS, "vps", "host", "", "Tên hoặc IP công khai", "Public name or IP"),
    "VPS_TCP_PORT": key(VPS, "vps", "port", "5760", "Cổng TCP cho GCS", "TCP port for GCS"),
    "WEB_BASIC_AUTH_PASS": key(VPS, "web", "string", "", "Mật khẩu Web Admin", "Web Admin password", secret=True, required=False),
    "VPS_ALLOWED": key(VPS, "security", "source_list", "any", "Nguồn được kết nối", "Allowed sources"),
}

SAMPLE_PRESETS = [
    {"id": "dgp-current", "role": "cm5", "order": 10, "star": True,
     "name": bi("Cấu hình mặc định", "Default configuration"),
     "desc": bi("Cấu hình đang bay thật: CM5 192.168.50.2, X25 192.168.50.1, ký MAVLink bật, đẩy telemetry lên VPS.",
                "The setup that flies today: CM5 192.168.50.2, X25 192.168.50.1, signing on, telemetry to the VPS."),
     "values": {}, "ask": ["VPS_PUBLIC_IP"]},
    {"id": "vps-starlink", "role": "cm5", "order": 20,
     "name": bi("Starlink + VPS", "Starlink + VPS"),
     "desc": bi("Đi qua Starlink, về VPS có IP công khai.", "Over Starlink, to a VPS with a public IP."),
     "values": {"VPS_UDP_PORT": "14551"}, "ask": ["VPS_PUBLIC_IP"]},
    {"id": "tailscale-only", "role": "cm5", "order": 30,
     "name": bi("Chỉ Tailscale", "Tailscale only"),
     "desc": bi("Không mở cổng ra Internet; trạm mặt đất vào qua Tailscale.", "No open ports to the Internet; ground stations join over Tailscale."),
     "values": {"CM5_ALLOWED_SSH": "100.64.0.0/10"}, "ask": ["TS_AUTHKEY"]},
    {"id": "bench-lan", "role": "cm5", "order": 40,
     "name": bi("Thử trên bàn (LAN)", "Bench test (LAN)"),
     "desc": bi("Mở MAVLink cho mạng nhà để thử với Mission Planner.", "Opens MAVLink to the home network for Mission Planner tests."),
     "warning": bi("Không dùng để bay: tắt ký và mở cổng cho cả mạng LAN.", "Not for flight: signing off and ports open to the whole LAN."),
     "values": {"CM5_MAVLINK_ALLOW_LAN": "1", "MAVLINK_SIGNING": "0"}, "ask": []},
    {"id": "ardupilot-default", "role": "cm5", "order": 50,
     "name": bi("Mặc định ArduPilot", "ArduPilot default"),
     "desc": bi("X25 192.168.144.14 ↔ CM5 192.168.144.2, ít thay đổi mạch bay nhất.", "X25 192.168.144.14 ↔ CM5 192.168.144.2, fewest flight controller changes."),
     "values": {"CM5_LAN_IP": "192.168.144.2", "X25_IP": "192.168.144.14"}, "ask": []},
    {"id": "custom", "role": "cm5", "order": 60,
     "name": bi("Tự đặt", "Custom"), "desc": bi("Bắt đầu từ giá trị mặc định rồi tự chỉnh.", "Start from the defaults and adjust yourself."),
     "values": {}, "ask": []},
    {"id": "vps-current", "role": "vps", "order": 10, "star": True,
     "name": bi("VPS hiện trạng", "Current VPS setup"), "desc": bi("Như VPS đang chạy: router + Web Admin.", "Like the running VPS: router + Web Admin."),
     "values": {}, "ask": ["VPS_PUBLIC_HOST"]},
    {"id": "vps-minimal", "role": "vps", "order": 20,
     "name": bi("VPS tối giản", "Minimal VPS"), "desc": bi("Chỉ router, không Web Admin.", "Router only, no Web Admin."), "values": {}, "ask": []},
]

SAMPLE_ENGINE = {
    "eth.default_route": bi("Tuyến mặc định đang đi qua {iface}", "The default route goes via {iface}"),
    "eth.default_route_ok": bi("Tuyến Internet mặc định không đi qua {iface}", "The default Internet route does not use {iface}"),
    "lan.link_up": bi("Cổng {iface} có tín hiệu liên kết", "Port {iface} has a link"),
    "lan.link_down": bi("Cổng {iface} chưa có tín hiệu liên kết", "Port {iface} has no link"),
    "internet.up": bi("Đã có kết nối Internet", "Internet is reachable"),
    "x25.usb_found": bi("Đã nhận mạch bay X25 tại {device}", "X25 flight controller detected at {device}"),
    "x25.disarmed": bi("Mạch bay X25 đã DISARM", "X25 flight controller is disarmed"),
    "x25.armed": bi("Mạch bay X25 đang ARM", "The X25 flight controller is armed"),
    "x25.firmware": bi("Firmware ArduPilot {version}", "ArduPilot firmware {version}"),
    "ufw.active.ok": bi("Tường lửa UFW đang bật", "UFW firewall is active"),
    "signing.enforced.ok": bi("Mạch bay X25 chỉ nhận lệnh có ký MAVLink2", "X25 flight controller accepts only commands with MAVLink2 signing"),
}

# Codes the mock emits for the extended pre-check (T-018 net_probe) and others missing from the engine files; the
# real share/i18n/engine-*.json texts win when they exist.
MOCK_ENGINE = {
    "router.conf_ok": bi("Cấu hình router hợp lệ", "The router configuration is valid"),
}

X25_DEVICE = "/dev/serial/by-id/usb-ArduPilot_X25_3A0042000F51-if00"

# NET_* values of the mock X25 before apply (ArduPilot defaults: 192.168.144.14, UDP client to .255:14550).
X25_NET = {"NET_ENABLE": 1, "NET_DHCP": 0, "NET_IPADDR0": 192, "NET_IPADDR1": 168, "NET_IPADDR2": 144, "NET_IPADDR3": 14,
           "NET_NETMASK": 24, "NET_P1_TYPE": 1, "NET_P1_PROTOCOL": 2, "NET_P1_IP0": 192, "NET_P1_IP1": 168,
           "NET_P1_IP2": 144, "NET_P1_IP3": 255, "NET_P1_PORT": 14550}


def x25_status(armed):
    """`data` of the x25.status info event (ARCHITECTURE.md section 4.3): a bench X25 without GPS fix or RC receiver."""
    sensors = [("gyro", True, True), ("accel", True, True), ("mag", True, True), ("baro", True, True), ("gps", True, True),
               ("rc", False, False), ("ahrs", True, True), ("battery", True, True), ("prearm", True, False)]
    return {"firmware": "ArduCopter V4.7.1 (3d4f2a1b)", "board": "CUAV-X25-EVO", "vehicle": "quadrotor",
            "system_status": "active" if armed else "standby", "armed": armed,
            "sensors": [{"id": sid, "present": p, "enabled": p, "healthy": ok} for sid, p, ok in sensors],
            "gps": {"fix": 1, "sats": 4, "hdop": 9.99},
            "battery": {"voltage": 16.42, "current": 1.3, "remaining": 87},
            "ekf": {"ok": True, "flags": 0x0A7},
            "prearm": ["PreArm: GPS 1: Bad fix", "PreArm: RC not found"],
            "net": dict(X25_NET)}


def chk(cid, ok, code, msg, args=None, warn=False):
    ev = {"t": "check", "id": cid, "ok": ok, "code": code, "msg": msg}
    if args:
        ev["args"] = args
    if warn:
        ev["severity"] = "warn"
    return ev


def load_real():
    schema = presets = engine = None
    p = os.path.join(SHARE, "schema.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            schema = json.load(f)
    files = sorted(glob.glob(os.path.join(SHARE, "presets", "*.json")))
    if files:
        presets = []
        for fn in files:
            with open(fn, encoding="utf-8") as f:
                presets.append(json.load(f))
        presets.sort(key=lambda x: x.get("order", 999))
    emap = {}
    for fn in sorted(glob.glob(os.path.join(SHARE, "i18n", "engine-*.json"))):
        with open(fn, encoding="utf-8") as f:
            emap.update(json.load(f))
    engine = emap or None
    return schema, presets, engine


# ---------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind):
        self.id = "j" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
        self.kind = kind
        self.state = "running"
        self.events = []
        self.started = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.ended = None
        self.cond = threading.Condition()

    def emit(self, ev):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self, state):
        with self.cond:
            self.state = state
            self.ended = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.cond.notify_all()

    def snapshot(self):
        with self.cond:
            return {"id": self.id, "kind": self.kind, "state": self.state, "started": self.started,
                    "ended": self.ended, "events": list(self.events)}


DIFF_ROUTER = """--- /etc/mavlink-router/main.conf
+++ /etc/mavlink-router/main.conf
@@ -1,9 +1,14 @@
 [General]
-TcpServerPort=5760
+TcpServerPort={tcp}
 ReportStats=false
 MavlinkDialect=ardupilotmega
+
+[UdpEndpoint x25]
+Mode=Normal
+Address={x25}
+Port={udp}
"""

DIFF_SECRETS = """--- /etc/uavlink/secrets.env
+++ /etc/uavlink/secrets.env
@@ -0,0 +1,2 @@
+MAVLINK_SIGNING_PASSPHRASE=<redacted>
+WEB_BASIC_AUTH_PASS=<redacted>
"""

DIFF_NM = """--- /etc/NetworkManager/system-connections/uavlink-eth.nmconnection
+++ /etc/NetworkManager/system-connections/uavlink-eth.nmconnection
@@ -0,0 +1,8 @@
+[connection]
+id=uavlink-eth
+type=ethernet
+interface-name={iface}
+[ipv4]
+method=manual
+address1={lan}/{mask}
"""


class Mock:
    def __init__(self, opts):
        self.opts = opts
        schema, presets, engine = load_real()
        self.real = schema is not None
        self.schema = schema or {"version": 1, "tabs": SAMPLE_TABS, "keys": SAMPLE_KEYS}
        self.presets = presets or SAMPLE_PRESETS
        self.engine = dict(engine or SAMPLE_ENGINE)
        for code, text in MOCK_ENGINE.items():
            self.engine.setdefault(code, text)
        # the dynamic "running configuration" preset, as detect.running_preset() returns it
        base = next((p for p in self.presets if p.get("role") == "cm5" and p.get("star")), None) or \
            next((p for p in self.presets if p.get("role") == "cm5"), None)
        self.default_values = dict(base.get("values", {})) if base else {}
        running = {"id": "running", "role": "cm5", "order": 0,
                   "name": bi("Cấu hình đang chạy", "Running configuration"),
                   "desc": bi("Giá trị đọc từ hệ thống đang chạy trên máy này.", "Values read from the system running on this machine."),
                   "values": dict(base.get("values", {})) if base else {}, "ask": []}
        if "CM5_LAN_IP" in running["values"] or not base:
            running["values"]["CM5_LAN_IP"] = "192.168.50.2"
        self.presets = list(self.presets) + [running]
        self.jobs = {}
        self.lock = threading.Lock()
        self.active = None
        self.applied = None
        self.last_values = None
        self.last_verify = None  # the last verify Job
        self.passphrase = None
        self.backups = [
            {"name": "x25-backup-20261012-091500.param", "time": "2026-10-12T09:15:00"},
            {"name": "x25-backup-20261003-174210.param", "time": "2026-10-03T17:42:10"},
        ]
        self.shutdown = None

    # -- state
    def state(self):
        o = self.opts
        return {
            "version": VERSION, "is_wsl": o.preview_only, "preview_only": o.preview_only,
            "role_guess": "vps" if o.vps else "cm5",
            "host": {"hostname": "cm5-mock", "ips": ["192.168.1.57", "100.101.20.3"]},
            "detect": {
                "is_wsl": o.preview_only, "arch": "aarch64", "model": "Raspberry Pi Compute Module 5", "hostname": "cm5-mock",
                "ifaces": [
                    {"name": "eth0", "carrier": True, "ipv4": ["192.168.50.2"], "is_default_route": False, "wireless": False},
                    {"name": "wlan0", "carrier": True, "ipv4": ["192.168.1.57"], "is_default_route": True, "wireless": True},
                    {"name": "tailscale0", "carrier": True, "ipv4": ["100.101.20.3"], "is_default_route": False, "wireless": False},
                ],
                "default_route_iface": "wlan0",
                "x25_ports": [] if o.no_x25 else [X25_DEVICE],
                "tailscale": {"ip": "100.101.20.3", "up": True}, "wifi": {"iface": "wlan0", "ssid": "Nha-5G"},
            },
            "schema": self.schema, "presets": self.presets,
            "current": {"values": {}, "secrets_present": {"MAVLINK_SIGNING_PASSPHRASE": self.applied is not None},
                        "applied_at": self.applied and self.applied["time"], "preset": self.applied and self.applied["preset"]},
            "i18n": {"engine": self.engine},
        }

    def validate(self, role, values):
        errors = {}
        keys = self.schema["keys"]
        for k, d in keys.items():
            if role not in d.get("roles", [role]) or d.get("secret"):
                continue
            sh = d.get("show_if")
            if sh and any(str(values.get(a)) != str(b) for a, b in sh.items()):
                continue
            v = str(values.get(k, ""))
            if v == "":
                if d.get("required") and not d.get("allow_empty"):
                    errors[k] = {"code": "required"}
                continue
            t = d.get("type")
            if t == "ipv4" and not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", v):
                errors[k] = {"code": "type.ipv4"}
            elif t == "port" and not (v.isdigit() and 1 <= int(v) <= 65535):
                errors[k] = {"code": "type.port"}
        if role == "cm5" and values.get("CM5_LAN_IP") and values.get("CM5_LAN_IP") == values.get("X25_IP") and "CM5_LAN_IP" not in errors:
            errors["CM5_LAN_IP"] = {"code": "conflict", "args": {"other": "X25_IP"}}
        return errors

    # -- jobs
    def start_job(self, kind, script):
        with self.lock:
            if self.active and self.active.state == "running":
                return None
            job = Job(kind)
            self.jobs[job.id] = job
            self.active = job
        threading.Thread(target=self._play, args=(job, script), daemon=True).start()
        return job

    def _play(self, job, script):
        d = self.opts.delay
        state = "ok"
        for item in script:
            if item == "FAIL":
                state = "failed"
                continue
            time.sleep(d)
            if callable(item):
                item(job)
            else:
                job.emit(item)
        job.finish(state)

    def scripts(self, kind, values, props_off=False):
        """Events of a precheck or apply job. A failed check fails its stage and the precheck job, as the runner does
        when an engine exits 3; the apply job stays ok (unless --fail-apply) so that the results page can be seen."""
        o = self.opts
        v = values or {}
        lan, x25 = v.get("CM5_LAN_IP", "192.168.50.2"), v.get("X25_IP", "192.168.50.1")
        mask, tcp, udp = v.get("X25_NETMASK", "24"), v.get("CM5_TCP_SERVER_PORT", "5760"), v.get("MAVLINK_LAN_PORT", "14550")
        iface = v.get("CM5_ETH_IFACE", "eth0")
        preview = kind == "precheck"
        s = []
        st = lambda sid, state, **kw: dict({"t": "step", "id": sid, "state": state}, **kw)  # noqa: E731
        stage = lambda sid, state: {"t": "stage", "id": sid, "state": state}  # noqa: E731
        log = lambda line: {"t": "log", "line": line}  # noqa: E731

        s.append(stage("cm5", "start"))
        for sid, line in (("packages", "Reading package lists... Done"), ("usb", "udev rules unchanged"), ("eth", "Checking %s" % iface)):
            s += [st(sid, "start"), log(line)]
            if sid == "eth":
                s += [chk("lan.link", True, "lan.link_up", iface + " has carrier", {"iface": iface}),
                      chk("eth.default_route", True, "eth.default_route_ok", "default route not via " + iface, {"iface": iface}),
                      {"t": "change", "kind": "nm", "target": "uavlink-eth", "action": "create",
                       "diff": DIFF_NM.format(iface=iface, lan=lan, mask=mask)}]
            s.append(st(sid, "ok"))
        s += [chk("internet", True, "internet.up", "internet reachable"),
              st("router", "start"), log("Rendering /etc/mavlink-router/main.conf"),
              {"t": "change", "kind": "file", "target": "/etc/mavlink-router/main.conf", "action": "update",
               "diff": DIFF_ROUTER.format(tcp=tcp, x25=x25, udp=udp)},
              {"t": "change", "kind": "service", "target": "uavlink-router.service", "action": "enable"},
              {"t": "change", "kind": "service", "target": "uavlink-router.service", "action": "restart"},
              chk("router.conf", True, "router.conf_ok", "router config valid"),
              st("router", "ok")]
        s += [st("security", "start"),
              {"t": "change", "kind": "file", "target": "/etc/uavlink/secrets.env", "action": "create", "diff": DIFF_SECRETS},
              {"t": "change", "kind": "firewall", "target": "ufw allow %s/tcp from 192.168.1.0/24" % tcp, "action": "create"},
              {"t": "change", "kind": "firewall", "target": "ufw allow %s/udp from %s" % (udp, x25), "action": "create"}]
        if not preview:
            s += [log("Firewall and SSH rollback timer armed"), {"t": "rollback", "state": "armed", "secs": 180}]
        s += [chk("ufw.active", True, "ufw.active.ok", "ufw active"), st("security", "ok"), stage("cm5", "ok")]

        s.append(stage("x25", "start"))
        if not o.no_x25 and (preview or props_off):
            s += self.x25_events(v, armed=o.armed and preview)
            s.append(stage("x25", "ok"))
        else:
            s.append(stage("x25", "skip"))

        if not preview:
            s += [stage("verify", "start")] + self.verify_checks(v) + [stage("verify", "ok")]
        net, failed = self.net_events(v, live=not preview)
        s += [stage("net", "start")] + net
        if preview:
            s += [stage("net", "fail" if failed else "ok")] + (["FAIL"] if failed else [])
        else:
            s.append(stage("net", "ok"))
            if o.fail_apply:
                s.append({"t": "step", "id": "router", "state": "fail", "code": "eth.default_route", "args": {"iface": "wlan0"}, "msg": "default route"})
                s.append("FAIL")
            else:
                s.append({"t": "result", "ok": True, "data": {}})
        return s

    def x25_events(self, v, armed):
        """x25_configure.py: USB checks, the health snapshot (some warnings) and the NET_* changes."""
        x25, lan = v.get("X25_IP", "192.168.50.1"), v.get("CM5_LAN_IP", "192.168.50.2")
        mask, udp = v.get("X25_NETMASK", "24"), v.get("MAVLINK_LAN_PORT", "14550")
        data = x25_status(armed)
        s = [chk("x25.usb", True, "x25.usb_found", "X25 found", {"device": X25_DEVICE}),
             chk("x25.disarmed", False, "x25.armed", "armed") if armed else chk("x25.disarmed", True, "x25.disarmed", "disarmed"),
             chk("x25.firmware", True, "x25.firmware", "firmware ArduCopter V4.7.1", {"version": "ArduCopter V4.7.1"}),
             chk("x25.sensors", True, "x25.sensors_ok", "all enabled sensors healthy"),
             chk("x25.gps", False, "x25.gps_no_fix", "no 3D fix (4 satellites)", {"sats": 4}, warn=True),
             chk("x25.battery", True, "x25.battery_ok", "battery 16.42 V", {"voltage": "16.42"}),
             chk("x25.ekf", True, "x25.ekf_ok", "EKF healthy"),
             chk("x25.prearm", False, "x25.prearm_failed", "PreArm: GPS 1: Bad fix", {"text": "GPS 1: Bad fix"}, warn=True),
             {"t": "info", "code": "x25.status", "msg": "X25 status: ArduCopter V4.7.1, CUAV-X25-EVO, standby", "data": data}]
        if armed:
            return s
        want = {"NET_IPADDR%d" % i: int(b) for i, b in enumerate(x25.split("."))}
        want.update({"NET_P1_IP%d" % i: int(b) for i, b in enumerate(lan.split("."))})
        want.update({"NET_NETMASK": int(mask), "NET_P1_PORT": int(udp)})
        for name, new in want.items():
            if X25_NET.get(name) != new:
                s.append({"t": "change", "kind": "param", "target": name, "old": str(X25_NET.get(name, "")), "new": str(new)})
        if v.get("MAVLINK_SIGNING", "1") == "1":
            s.append({"t": "change", "kind": "param", "target": "SIGNING_KEY", "old": "-", "new": "<set>"})
        return s

    def verify_checks(self, v):
        """verify-cm5.sh checks, with the codes of share/i18n/engine-verify.json."""
        iface, lan, x25 = v.get("CM5_ETH_IFACE", "eth0"), v.get("CM5_LAN_IP", "192.168.50.2"), v.get("X25_IP", "192.168.50.1")
        unit = "uavlink-router.service"
        has_x25 = not self.opts.no_x25
        checks = [("lan.link", {"iface": iface}), ("lan.addr", {"iface": iface, "ip": lan}), ("lan.route", {"iface": iface})]
        if has_x25:
            checks += [("x25.ping", {"ip": x25}), ("x25.udp_in", {"port": int(v.get("MAVLINK_LAN_PORT", "14550")), "ip": x25}),
                       ("mavlink.two_way", None)]
        checks += [("router.active", {"unit": unit}), ("internet", None)]
        if v.get("VPS_PUBLIC_IP"):
            checks.append(("vps.uplink", {"ip": v["VPS_PUBLIC_IP"], "port": int(v.get("VPS_UPLINK_PORT", "14555"))}))
        if has_x25 and v.get("MAVLINK_SIGNING", "1") == "1":
            checks.append(("signing.enforced", None))
        checks += [("ssh.no_password", None), ("ufw.active", None), ("router.not_root", {"unit": unit})]
        return [chk(cid, True, cid + ".ok", cid, args) for cid, args in checks]

    def net_events(self, v, live):
        """net_probe.py (probe mode in the pre-check, live mode after apply): its step ids, codes and args.
        Returns (events, a check failed); the GCS UDP port always fails here so the page shows one failure."""
        has_x25 = not self.opts.no_x25
        x25, tcp, udp = v.get("X25_IP", "192.168.50.1"), int(v.get("CM5_TCP_SERVER_PORT", "5760")), int(v.get("MAVLINK_LAN_PORT", "14550"))
        host = v.get("VPS_PUBLIC_IP", "")
        up, gtcp, gudp = int(v.get("VPS_UPLINK_PORT", "14555")), int(v.get("VPS_GCS_TCP_PORT", "5760")), int(v.get("VPS_GCS_UDP_PORT", "14550"))
        st = lambda sid, state, **kw: dict({"t": "step", "id": sid, "state": state}, **kw)  # noqa: E731
        s = [{"t": "info", "code": "net.mode.live" if live else "net.mode.probe", "msg": "live" if live else "probe"}]
        if has_x25:
            s += [st("net.lan", "start"),
                  chk("x25.lan_ping", True, "x25.lan_ping.ok", "X25 answers ping", {"ip": x25}),
                  chk("x25.lan_mavlink", True, "x25.lan_mavlink.router", "X25 HEARTBEAT through the router",
                      {"port": tcp, "sysid": 1}),
                  st("net.lan", "ok")]
        else:
            s.append(st("net.lan", "skip", code="net.no_x25_ip", msg="no X25 flight controller"))
        s += [st("net.internet", "start"),
              chk("internet", True, "internet.reachable", "Internet is reachable", {"host": "1.1.1.1:443"}),
              st("net.internet", "ok")]
        if host:
            ip = host if re.match(r"^\d+\.\d+\.\d+\.\d+$", host) else "203.0.113.10"
            s.append(st("net.vps", "start"))
            if ip != host:
                s.append(chk("vps.dns", True, "vps.dns.ok", host, {"host": host, "ip": ip}))
            s += [chk("vps.ping", False, "vps.ping.fail", "no ICMP answer", {"ip": ip}, warn=True),
                  chk("vps.tcp", True, "vps.tcp.ok", "GCS TCP port accepts connections", {"ip": ip, "port": gtcp})]
            if not live:
                link = {"ip": ip, "port": up, "tcp_port": gtcp}
                s += [chk("vps.uplink", True, "vps.uplink.relayed", "uplink relayed", link),
                      chk("vps.downlink", True, "vps.downlink.relayed", "downlink reaches this machine", link)]
            elif has_x25:
                s += [chk("vps.x25_via_vps", True, "vps.x25_via_vps.ok", "X25 heartbeats through the VPS",
                          {"ip": ip, "port": gtcp, "sysid": 1}),
                      chk("vps.cmd_via_vps", True, "vps.cmd_via_vps.ok", "signed command through the VPS answered",
                          {"param": "NET_IPADDR3", "value": "1"})]
            s += [chk("vps.gcs_udp", False, "vps.gcs_udp.fail", "no MAVLink answer on the GCS UDP port",
                      {"ip": ip, "port": gudp}),
                  st("net.vps", "fail")]
        else:
            s.append(st("net.vps", "skip", code="net.no_vps", msg="no VPS configured"))
        targets = []
        for target in v.get("CM5_EXTRA_UDP_TARGETS", "").split():
            name, _, addr = target.partition("=")
            ip = addr.rsplit(":", 1)[0]
            if ip.startswith("100."):
                targets.append(chk("gcs.tailscale." + name, True, "gcs.tailscale.ok", "tailscale ping ok",
                                   {"name": name, "ip": ip}))
        if targets:
            s += [st("net.gcs", "start")] + targets + [st("net.gcs", "ok")]
        else:
            s.append(st("net.gcs", "skip", code="gcs.no_targets", msg="no Tailscale GCS target"))
        s += [st("net.cm5", "start"),
              chk("cm5.listen", True, "cm5.listen.ok", "router listens", {"tcp": tcp, "udp": udp}),
              chk("cm5.firewall", True, "cm5.firewall.ok", "firewall lets MAVLink in",
                  {"iface": v.get("CM5_ETH_IFACE", "eth0"), "port": udp}),
              st("net.cm5", "ok")]
        return s, bool(host)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "uavlink-mock"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    @property
    def m(self):
        return self.server.mock

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/json", "application/javascript")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _err(self, code, error, detail="", **kw):
        self._send(code, dict({"error": error, "detail": detail}, **kw))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return {}

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            return self.api_get(path)
        return self.static(path)

    def do_POST(self):
        path = urlsplit(self.path).path
        return self.api_post(path, self._body())

    def static(self, path):
        if path == "/":
            path = "/index.html"
        full = os.path.realpath(os.path.join(WEB, path.lstrip("/")))
        if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
            return self._err(404, "not_found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "application/javascript"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def api_get(self, path):
        m = self.m
        if path == "/api/state":
            return self._send(200, m.state())
        if path == "/api/status":
            svc = {"uavlink-router": "active", "ufw": "active", "chrony": "active", "tailscaled": "active", "uavlink-setup": "active"}
            lv = m.last_verify
            last = None if lv is None else {"id": lv.id, "state": lv.state, "ended": lv.ended}  # as the real server
            return self._send(200, {"services": svc, "applied": m.applied, "last_verify": last})
        if path == "/api/x25/backups":
            return self._send(200, {"backups": m.backups})
        mt = re.fullmatch(r"/api/jobs/([\w-]+)(/stream)?", path)
        if mt:
            job = m.jobs.get(mt.group(1))
            if not job:
                return self._err(404, "not_found")
            return self.stream(job) if mt.group(2) else self._send(200, job.snapshot())
        mt = re.fullmatch(r"/api/secret/(\w+)", path)
        if mt:
            if mt.group(1) != "MAVLINK_SIGNING_PASSPHRASE" or not m.passphrase:
                return self._err(404, "not_found")
            return self._send(200, {"value": m.passphrase})
        return self._err(404, "not_found")

    def stream(self, job):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        idx = 0
        try:
            while True:
                with job.cond:
                    while idx >= len(job.events) and job.state == "running":
                        job.cond.wait(timeout=15)
                        if idx >= len(job.events) and job.state == "running":
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                    batch = job.events[idx:]
                    idx += len(batch)
                    state = job.state
                for ev in batch:
                    self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                if state != "running" and idx >= len(job.events):
                    self.wfile.write(("event: end\ndata: " + json.dumps({"state": state}) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def api_post(self, path, body):
        m = self.m
        o = m.opts
        if path == "/api/validate":
            errs = m.validate(body.get("role", "cm5"), body.get("values") or {})
            return self._send(200, {"ok": not errs, "errors": errs})
        if path in ("/api/precheck", "/api/apply"):
            role, values = body.get("role", "cm5"), body.get("values") or {}
            errs = m.validate(role, values)
            if errs:
                return self._err(400, "invalid", "invalid values", errors=errs)
            if path == "/api/apply":
                if o.preview_only:
                    return self._err(423, "preview_only", "WSL preview mode")
                if role == "vps":
                    return self._err(400, "role.vps_not_ready", "the VPS engine is not ready")
                if not o.no_x25 and not body.get("props_off"):
                    return self._err(400, "props_off", "confirm that the propellers are removed")
                job = m.start_job("apply", m.scripts("apply", values, bool(body.get("props_off"))))
                if job:
                    m.last_values = dict(values)
                    m.passphrase = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(32))
                    m.applied = {"preset": body.get("preset"), "role": role, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": VERSION}
            else:
                job = m.start_job("precheck", m.scripts("precheck", values))
            if not job:
                return self._err(409, "busy", "another job is running")
            return self._send(200, {"job": job.id})
        if path == "/api/verify":
            # verify-cm5.sh, then net_probe.py --mode live (ARCHITECTURE.md section 4.3)
            v = m.last_values or m.default_values
            net, failed = m.net_events(v, live=True)
            script = ([{"t": "stage", "id": "verify", "state": "start"}] + m.verify_checks(v) +
                      [{"t": "stage", "id": "verify", "state": "ok"}, {"t": "stage", "id": "net", "state": "start"}] + net +
                      [{"t": "stage", "id": "net", "state": "fail" if failed else "ok"}] + (["FAIL"] if failed else []))
            job = m.start_job("verify", script)
            if job:
                m.last_verify = job
            return self._send(200, {"job": job.id}) if job else self._err(409, "busy", "another job is running")
        if path == "/api/x25/restore":
            name = body.get("file")
            script = [{"t": "stage", "id": "x25", "state": "start"},
                      chk("x25.usb", True, "x25.usb_found", "X25 found", {"device": X25_DEVICE}),
                      chk("x25.disarmed", True, "x25.disarmed", "disarmed"),
                      {"t": "change", "kind": "param", "target": "NET_IPADDR3", "old": "1", "new": "14"},
                      {"t": "log", "line": "restored from %s" % name}, {"t": "stage", "id": "x25", "state": "ok"}]
            job = m.start_job("restore", script)
            return self._send(200, {"job": job.id}) if job else self._err(409, "busy", "another job is running")
        if path == "/api/confirm":
            if m.active:
                m.active.emit({"t": "rollback", "state": "confirmed"})
            return self._send(200, {"ok": True})
        if path == "/api/shutdown":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        return self._err(404, "not_found")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--speed", type=float, default=1.0, help="2 = twice as fast")
    ap.add_argument("--preview-only", action="store_true", help="behave like WSL: apply returns 423")
    ap.add_argument("--armed", action="store_true", help="pre-check reports the X25 as armed")
    ap.add_argument("--no-x25", action="store_true", help="no flight controller on USB")
    ap.add_argument("--fail-apply", action="store_true", help="the apply job fails at the end")
    ap.add_argument("--vps", action="store_true", help="role_guess is vps")
    opts = ap.parse_args(argv)
    opts.delay = 0.35 / max(opts.speed, 0.01)
    srv = ThreadingHTTPServer((opts.host, opts.port), Handler)
    srv.daemon_threads = True
    srv.mock = Mock(opts)
    print("mock server (%s data) on http://%s:%d/" % ("real" if srv.mock.real else "sample", opts.host, opts.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
