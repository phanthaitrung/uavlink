#!/usr/bin/env python3
"""
Cấu hình cổng Ethernet của CUAV X25 EVO (ArduCopter) qua cáp USB. Chạy trên CM5.

Trình tự (giống thao tác tay trong CHAT_SESSION_LOG.md, lượt 4):
  1. Sao lưu toàn bộ tham số ra tệp .parm.
  2. Ghi NET_ENABLE=1, NET_P1_TYPE=1 rồi khởi động lại (cây NET_* mới hiện ra).
  3. Ghi IP tĩnh cho X25, cổng 1 = UDP Client gửi MAVLink2 tới CM5, rồi khởi động lại.
  4. Đọc lại để xác nhận.
  5. (MAVLINK_SIGNING=1) Nạp khóa ký MAVLink2 qua USB: từ đó cổng mạng chỉ nhận lệnh có chữ ký.
  6. Kiểm tra MAVLink đi qua Ethernet (mavlink-router trên CM5), lệnh không ký bị từ chối.
Tham số nào đã đúng thì không ghi; không có gì phải ghi thì không khởi động lại.
Ngay sau khi kết nối (cả khi --preview) đọc tình trạng mạch bay, chỉ đọc: cảm biến, GPS, pin, EKF, PreArm, firmware.

Mặc định lấy từ $UAVLINK_CONFIG_DIR/config.env và secrets.env (mặc định /etc/uavlink); biến môi trường ghi đè tệp.
  --preview              kết nối, đọc, in các tham số sẽ đổi (cũ -> mới); không ghi, không khởi động lại,
                         không nạp khóa ký, không sao lưu (--dry-run là tên cũ)
  --list-backups         in danh sách bản sao lưu (JSON) rồi thoát
  --restore TỆP --yes    ghi lại các tham số NET_* từ bản sao lưu (rollback)
  --signing off          xoá khóa ký trên mạch bay (tắt ký)
Có UAVLINK_EVENTS=1 thì in thêm các dòng sự kiện "@@UAVLINK {json}" (ARCHITECTURE.md mục 4.1).
Mã thoát: 0 xong, 1 lỗi, 2 sai tham số, 3 kiểm tra Ethernet không đạt, 4 từ chối (đang ARM, WSL).
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import OrderedDict

# Dùng MAVLink2 ngay từ đầu: cần cho SETUP_SIGNING (id 256), và mặc định pymavlink bắt đầu bằng
# MAVLink1 rồi tự chuyển khi nhận gói MAVLink2 đầu tiên, lúc chuyển thì mất cấu hình ký đã đặt.
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")
try:
    from pymavlink import mavutil  # noqa: E402
except ImportError:  # người gọi chưa đặt PYTHONPATH: dùng bản đóng gói kèm <engine>/../vendor
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "vendor"))
    from pymavlink import mavutil  # noqa: E402

MAV = mavutil.mavlink
MAX_ROUNDS = 4
HOST_IP_PARAMS = ("NET_IPADDR0", "NET_IPADDR1", "NET_IPADDR2", "NET_IPADDR3", "NET_NETMASK")  # IP của chính X25
PROBE_PARAM = "NET_ENABLE"  # đọc thử qua Ethernet: có trên mọi firmware có mạng, NET_IPADDR* thì không (SITL, Linux)
SIGNING_EPOCH = 1420070400  # 2015-01-01 UTC, gốc thời gian của MAVLink signing
# Không bao giờ ghi lại khi khôi phục: thống kê và ID cảm biến do firmware tự đặt
RESTORE_SKIP_PREFIXES = ("STAT_", "FORMAT_VERSION")
RESTORE_SKIP_MARKERS = ("_DEV_ID", "DEVID", "_ACC_ID", "_GYR_ID", "_ACC2_ID", "_GYR2_ID", "_ACC3_ID", "_GYR3_ID")
BACKUP_GLOB = "x25_params_*.parm"
CONFIG = {}  # config.env + secrets.env; main() nạp

# ---- tình trạng mạch bay (x25.status, ARCHITECTURE.md 4.3) ----
HEALTH_SECS = 5  # thời gian chờ tối đa; thường xong sau ~1 s
HEALTH_MESSAGES = ("SYS_STATUS", "GPS_RAW_INT", "EKF_STATUS_REPORT", "AUTOPILOT_VERSION")
# Chỉ các lệnh này được gửi khi đọc tình trạng: không lệnh nào ghi gì lên mạch bay (không REQUEST_DATA_STREAM,
# vì ArduPilot lưu tốc độ luồng vào tham số SRx_)
HEALTH_COMMANDS = (MAV.MAV_CMD_REQUEST_MESSAGE, MAV.MAV_CMD_RUN_PREARM_CHECKS, MAV.MAV_CMD_DO_SEND_BANNER)
SENSOR_BITS = OrderedDict([
    ("gyro", MAV.MAV_SYS_STATUS_SENSOR_3D_GYRO), ("accel", MAV.MAV_SYS_STATUS_SENSOR_3D_ACCEL),
    ("mag", MAV.MAV_SYS_STATUS_SENSOR_3D_MAG), ("baro", MAV.MAV_SYS_STATUS_SENSOR_ABSOLUTE_PRESSURE),
    ("gps", MAV.MAV_SYS_STATUS_SENSOR_GPS), ("rc", MAV.MAV_SYS_STATUS_SENSOR_RC_RECEIVER),
    ("ahrs", MAV.MAV_SYS_STATUS_AHRS), ("battery", MAV.MAV_SYS_STATUS_SENSOR_BATTERY),
    ("prearm", MAV.MAV_SYS_STATUS_PREARM_CHECK),
])
SENSORS_OWN_CHECK = ("gps", "battery", "prearm")  # có kiểm tra riêng, không tính vào x25.sensors
SENSORS_WARN_ONLY = ("rc", "ahrs")  # chưa bật tay điều khiển RC, EKF đang khởi tạo: chỉ cảnh báo
EKF_VARIANCE_BAD = 0.8  # như Mission Planner: phương sai từ 0.8 trở lên là EKF có vấn đề


# ---- sự kiện (ARCHITECTURE.md 4.1) -------------------------------------------------------------------------------

_open_steps = []  # các bước đang chạy, trong cùng ở cuối (x25.reboot chạy trong x25.params), để fail() đóng chúng


def ev(**obj):
    if os.environ.get("UAVLINK_EVENTS") != "1":
        return
    print("@@UAVLINK " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")), flush=True)


def _with(obj, code, args, msg):
    if code:
        obj["code"] = code
    if args:
        obj["args"] = args
    if msg:
        obj["msg"] = msg
    return obj


def ev_step(sid, state, code=None, args=None, msg=None):
    if state == "start":
        _open_steps.append(sid)
    elif sid in _open_steps:
        _open_steps.remove(sid)
    ev(**_with({"t": "step", "id": sid, "state": state}, code, args, msg))


def ev_check(cid, ok, code=None, args=None, msg=None, severity=None):
    obj = _with({"t": "check", "id": cid, "ok": bool(ok)}, code, args, msg)
    if severity and not ok:
        obj["severity"] = severity
    ev(**obj)


def ev_info(code, msg, args=None, data=None):
    obj = _with({"t": "info"}, code, args, msg)
    if data is not None:
        obj["data"] = data
    ev(**obj)


def ev_change(target, old, new):
    ev(t="change", kind="param", target=target, action="set", old=old, new=new)


# ---- cấu hình ------------------------------------------------------------------------------------------------------

def _parse_value(raw):
    """Giá trị sau dấu '=': không quote (bỏ '# ghi chú' cuối dòng), "…", '…', ghép được ('a'\\''b'). Không chạy shell."""
    out, i, n = [], 0, len(raw)
    while i < n:
        c = raw[i]
        if c == "'":
            j = raw.find("'", i + 1)
            if j < 0:
                out.append(raw[i + 1:])
                break
            out.append(raw[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and raw[i] != '"':
                if raw[i] == "\\" and i + 1 < n and raw[i + 1] in '"\\$`':
                    i += 1
                out.append(raw[i])
                i += 1
            i += 1
        elif c == "\\" and i + 1 < n:
            out.append(raw[i + 1])
            i += 2
        elif c in " \t":
            rest = raw[i:].lstrip(" \t")
            if not rest or rest.startswith("#"):
                break
            out.append(raw[i:n - len(rest)])
            i = n - len(rest)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def read_env_file(path):
    values = {}
    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return values
    with f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, raw = line.partition("=")
            key = key.strip()
            if sep and key.replace("_", "a").isalnum() and not key[0].isdigit():
                values[key] = _parse_value(raw.lstrip())
    return values


def load_config(config_dir=None):
    """config.env rồi secrets.env (secrets thắng)."""
    config_dir = config_dir or os.environ.get("UAVLINK_CONFIG_DIR") or "/etc/uavlink"
    values = read_env_file(os.path.join(config_dir, "config.env"))
    values.update(read_env_file(os.path.join(config_dir, "secrets.env")))
    return values


def env(name, default=""):
    """Biến môi trường thắng tệp cấu hình; rỗng coi như chưa đặt."""
    return os.environ.get(name) or CONFIG.get(name) or default


def default_backup_dir():
    state = os.environ.get("UAVLINK_STATE_DIR") or "/var/lib/uavlink"
    return os.path.join(state, "x25-backups")


def is_wsl():
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/sys/kernel/osrelease", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


# ---- tiện ích -------------------------------------------------------------------------------------------------------

def log(msg=""):
    print(msg, flush=True)


def fail(msg, code=1, ecode=None, args=None):
    """In lỗi, ghi "step fail" cho mọi bước đang mở (bước trong cùng mang mã lỗi) rồi thoát."""
    for i, sid in enumerate(reversed(list(_open_steps))):
        ev_step(sid, "fail", *((ecode, args) if i == 0 else ()))
    print(f"[LỖI] {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def ipv4(text):
    parts = text.strip().split(".")
    if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        raise argparse.ArgumentTypeError(f"IPv4 không hợp lệ: {text}")
    return [int(p) for p in parts]


def same(a, b):
    return abs(float(a) - float(b)) < 1e-4


def fmt(v):
    return f"{float(v):g}"


def param_name(msg):
    pid = msg.param_id
    if isinstance(pid, bytes):
        pid = pid.decode("ascii", "ignore")
    return pid.rstrip("\x00")


def signing_key(passphrase):
    # Mission Planner, QGC, MAVProxy cùng lấy SHA-256 của passphrase làm khóa
    return hashlib.sha256(passphrase.encode("utf-8")).digest()


def signing_timestamp():
    return int((max(time.time(), SIGNING_EPOCH) - SIGNING_EPOCH) * 100000)  # đơn vị 10 µs


def accept_unsigned(_mav, _msg_id):
    return True


def read_param(conn, sysid, compid, name, tries=3):
    """Đọc một tham số; trả về giá trị hoặc None nếu mạch bay không trả lời."""
    for _ in range(tries):
        conn.mav.param_request_read_send(sysid, compid, name.encode("ascii"), -1)
        end = time.time() + 2
        while time.time() < end:
            pv = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
            if pv is not None and param_name(pv) == name:
                return pv.param_value
    return None


def find_device():
    for pattern in ("/dev/serial/by-id/*X25*-if00", "/dev/serial/by-id/*ArduPilot*-if00", "/dev/ttyACM0"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def is_net_device(device):
    """X25_SERIAL dạng địa chỉ mạng của pymavlink (tcp:host:cổng, udpin:…), vd SERIAL0 của SITL: không phải tệp."""
    return re.match(r"(tcp|tcpin|udp|udpin|udpout):[^:]+:\d+$", device or "") is not None


def device_present(device):
    return bool(device) and (is_net_device(device) or os.path.exists(device))


class FlightController:
    """Kết nối MAVLink qua USB tới mạch bay, tự nối lại sau khi khởi động lại."""

    def __init__(self, device, baud):
        self.device = device
        self.baud = baud
        self.conn = None
        self.sysid = None
        self.compid = None
        self.heartbeat = None
        self.status = None  # dữ liệu x25.status đọc lúc kết nối (chưa có "net")

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.conn = None

    def connect(self, timeout=60):
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            if device_present(self.device):
                try:
                    self.conn = mavutil.mavlink_connection(
                        self.device, baud=self.baud, source_system=255, source_component=190
                    )
                    if self.wait_heartbeat(10):
                        return
                    last_err = "không có HEARTBEAT"
                except Exception as e:  # cổng đang bận, USB vừa rút/cắm lại...
                    last_err = e
                self.close()
            time.sleep(1)
        fail(f"Không kết nối được mạch bay qua {self.device} sau {timeout:.0f}s ({last_err})", 1,
             "x25.connect_failed", {"device": self.device, "secs": int(timeout)})

    def wait_heartbeat(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            msg = self.conn.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
            if msg is None or msg.autopilot == MAV.MAV_AUTOPILOT_INVALID:
                continue
            self.sysid, self.compid = msg.get_srcSystem(), msg.get_srcComponent()
            self.heartbeat = msg
            return msg
        return None

    @property
    def armed(self):
        return bool(self.heartbeat.base_mode & MAV.MAV_MODE_FLAG_SAFETY_ARMED)

    def ensure_disarmed(self):
        self.wait_heartbeat(3)
        if self.armed:
            fail("Mạch bay đang ARM. Disarm (và tháo cánh quạt) rồi chạy lại.", 4, "x25.armed")

    def command(self, command, *params):
        self.conn.mav.command_long_send(self.sysid, self.compid, command, 0, *(list(params) + [0] * 7)[:7])

    def health_snapshot(self, timeout=HEALTH_SECS):
        """Đọc tình trạng mạch bay, chỉ đọc (HEALTH_COMMANDS): xin từng gói HEALTH_MESSAGES bằng
        MAV_CMD_REQUEST_MESSAGE, gom STATUSTEXT sau MAV_CMD_RUN_PREARM_CHECKS (dòng "PreArm: …") và
        MAV_CMD_DO_SEND_BANNER (firmware, tên mạch). Trả về {tên gói: gói hoặc None, "texts": [dòng]}."""
        got, texts = {}, OrderedDict()
        self.command(MAV.MAV_CMD_RUN_PREARM_CHECKS)
        self.command(MAV.MAV_CMD_DO_SEND_BANNER)
        types = ["HEARTBEAT", "COMMAND_ACK", "STATUSTEXT"] + list(HEALTH_MESSAGES)
        end = time.time() + timeout
        next_ask, acked = 0, None
        while time.time() < end:
            now = time.time()
            if now >= next_ask:
                for name in HEALTH_MESSAGES:
                    if name not in got:
                        self.command(MAV.MAV_CMD_REQUEST_MESSAGE, getattr(MAV, "MAVLINK_MSG_ID_" + name))
                next_ask = now + 1
            # các dòng PreArm có thể tới sau COMMAND_ACK: chờ thêm 1 s
            if len(got) == len(HEALTH_MESSAGES) and acked is not None and now - acked >= 1:
                break
            msg = self.conn.recv_match(type=types, blocking=True, timeout=0.2)
            if msg is None or (msg.get_srcSystem(), msg.get_srcComponent()) != (self.sysid, self.compid):
                continue
            mtype = msg.get_type()
            if mtype == "HEARTBEAT":
                if msg.autopilot != MAV.MAV_AUTOPILOT_INVALID:
                    self.heartbeat = msg
            elif mtype == "COMMAND_ACK":
                if msg.command == MAV.MAV_CMD_RUN_PREARM_CHECKS:
                    acked = time.time()
            elif mtype == "STATUSTEXT":
                text = msg.text.decode("utf-8", "replace") if isinstance(msg.text, bytes) else msg.text
                key = ("id", msg.id) if getattr(msg, "id", 0) else ("n", len(texts))  # mảnh của một dòng dài: cùng id
                texts[key] = texts.get(key, "") + text.rstrip("\x00")
            else:
                got[mtype] = msg
        snap = {name: got.get(name) for name in HEALTH_MESSAGES}
        snap["texts"] = list(texts.values())
        return snap

    def fetch_all(self, timeout=120):
        """Tải toàn bộ tham số. Trả về {tên: (giá trị, kiểu)}."""
        params, seen = {}, set()
        total = None
        list_requests = 1
        self.conn.mav.param_request_list_send(self.sysid, self.compid)
        start = last_rx = time.time()
        while total is None or len(seen) < total:
            if time.time() - start > timeout:
                fail(f"Hết thời gian tải tham số ({len(seen)}/{total})", 1, "x25.params_timeout",
                     {"got": len(seen), "total": total if total is not None else 0})
            msg = self.conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
            if msg is not None and msg.get_srcSystem() == self.sysid:
                params[param_name(msg)] = (msg.param_value, msg.param_type)
                total = msg.param_count
                if msg.param_index < total:
                    seen.add(msg.param_index)
                last_rx = time.time()
                continue
            if time.time() - last_rx < 2:
                continue
            # Luồng tham số dừng: xin lại cả danh sách, hoặc xin riêng các chỉ số còn thiếu
            if total is None:
                if list_requests >= 3:
                    fail("Mạch bay không trả lời PARAM_REQUEST_LIST", 1, "x25.no_param_list")
                self.conn.mav.param_request_list_send(self.sysid, self.compid)
                list_requests += 1
            else:
                missing = [i for i in range(total) if i not in seen]
                for i in missing[:40]:
                    self.conn.mav.param_request_read_send(self.sysid, self.compid, b"", i)
            last_rx = time.time()
        return params

    def set_param(self, name, value, ptype, retries=3):
        for _ in range(retries):
            self.conn.mav.param_set_send(self.sysid, self.compid, name.encode("ascii"), float(value), ptype)
            end = time.time() + 3
            while time.time() < end:
                msg = self.conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
                if msg is not None and param_name(msg) == name:
                    if same(msg.param_value, value):
                        return True
                    break
        return False

    def setup_signing(self, key):
        """Nạp khóa ký qua USB (cổng USB luôn được nhận gói không ký). Khóa toàn số 0 = tắt ký.
        Mạch bay không trả lời SETUP_SIGNING nên gửi 2 lần; kết quả được kiểm qua Ethernet sau đó."""
        for _ in range(2):
            self.conn.mav.setup_signing_send(self.sysid, self.compid, list(key), signing_timestamp())
            time.sleep(0.5)

    def reboot(self):
        self.ensure_disarmed()
        ev_step("x25.reboot", "start")
        log("  Khởi động lại mạch bay...")
        self.conn.mav.command_long_send(
            self.sysid, self.compid, MAV.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN, 0, 1, 0, 0, 0, 0, 0, 0
        )
        end = time.time() + 3
        while time.time() < end:
            ack = self.conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.5)
            if ack is not None and ack.command == MAV.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN:
                if ack.result != MAV.MAV_RESULT_ACCEPTED:
                    fail(f"Mạch bay từ chối khởi động lại (MAV_RESULT={ack.result})", 1, "x25.reboot_refused",
                         {"result": int(ack.result)})
                break
        self.close()
        time.sleep(8)  # chờ cổng USB cũ biến mất và bootloader chạy xong
        self.connect(timeout=90)
        log(f"  Đã kết nối lại (sysid {self.sysid}).")
        ev_step("x25.reboot", "ok")


# ---- tình trạng mạch bay ------------------------------------------------------------------------------------------

def sw_version(v):
    return f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}"


def enum_name(enum, value):
    """("MAV_TYPE", 2) -> "quadrotor"; giá trị lạ -> số dạng chuỗi."""
    entry = MAV.enums.get(enum, {}).get(value)
    return entry.name[len(enum) + 1:].lower() if entry is not None else str(value)


def banner_info(texts):
    """(firmware, board) từ banner: "ArduCopter V4.7.1 (3d4f2a1b)", "CUAV-X25-EVO 003A002D 3133510B 37363134"."""
    firmware = board = None
    for t in texts:
        if firmware is None and re.match(r"\w+ V\d+\.\d+", t):
            firmware = t
        m = re.match(r"(\S+)(?: [0-9A-Fa-f]{8}){3}$", t)
        if board is None and m:
            board = m.group(1)
    return firmware, board


def ekf_ok(m):
    variances = (m.velocity_variance, m.pos_horiz_variance, m.pos_vert_variance, m.compass_variance)
    return (bool(m.flags & MAV.EKF_ATTITUDE) and not m.flags & (MAV.EKF_UNINITIALIZED | MAV.EKF_GPS_GLITCHING)
            and max(variances) < EKF_VARIANCE_BAD)


def status_data(fc, snap):
    """Dữ liệu x25.status (ARCHITECTURE.md 4.3) trừ "net"; giá trị chưa biết là None."""
    ss, gps, ekf, ver = (snap[n] for n in HEALTH_MESSAGES)
    firmware, board = banner_info(snap["texts"])
    if firmware is None and ver is not None:
        firmware = sw_version(ver.flight_sw_version)
    if board is None and ver is not None and ver.board_version:
        board = f"board id {ver.board_version >> 16}"
    hb = fc.heartbeat
    return OrderedDict([
        ("firmware", firmware), ("board", board),
        ("vehicle", enum_name("MAV_TYPE", hb.type)), ("system_status", enum_name("MAV_STATE", hb.system_status)),
        ("armed", fc.armed),
        ("sensors", [] if ss is None else [
            {"id": sid, "present": bool(ss.onboard_control_sensors_present & bit),
             "enabled": bool(ss.onboard_control_sensors_enabled & bit),
             "healthy": bool(ss.onboard_control_sensors_health & bit)} for sid, bit in SENSOR_BITS.items()]),
        ("gps", {"fix": None, "sats": None, "hdop": None} if gps is None else {
            "fix": int(gps.fix_type), "sats": None if gps.satellites_visible == 255 else int(gps.satellites_visible),
            "hdop": None if gps.eph == 0xFFFF else round(gps.eph / 100, 2)}),
        ("battery", {"voltage": None, "current": None, "remaining": None} if ss is None else {
            "voltage": None if ss.voltage_battery == 0xFFFF else round(ss.voltage_battery / 1000, 2),
            "current": None if ss.current_battery < 0 else round(ss.current_battery / 100, 2),
            "remaining": None if ss.battery_remaining < 0 else int(ss.battery_remaining)}),
        ("ekf", {"ok": None, "flags": None} if ekf is None else {"ok": ekf_ok(ekf), "flags": int(ekf.flags)}),
        ("prearm", list(OrderedDict.fromkeys(t for t in snap["texts"] if t.startswith("PreArm:")))),
    ])


def _unhealthy(sensors, sid):
    return any(s["id"] == sid and s["present"] and s["enabled"] and not s["healthy"] for s in sensors)


def health_checks(d):
    """Các kiểm tra x25.sensors, x25.gps, x25.battery, x25.ekf, x25.prearm từ dữ liệu x25.status.
    Chỉ cảm biến phần cứng lỗi là không đạt; còn lại là cảnh báo (severity warn), vì trên bàn thử thường chưa có GPS,
    pin, tay điều khiển RC."""
    sensors = d["sensors"]
    bad = [s["id"] for s in sensors if s["id"] not in SENSORS_OWN_CHECK and _unhealthy(sensors, s["id"])]
    if not sensors:
        log("Cảm biến : mạch bay không gửi SYS_STATUS")
        ev_check("x25.sensors", False, "x25.no_sys_status", msg="no SYS_STATUS from the flight controller",
                 severity="warn")
    elif bad:
        log(f"Cảm biến : lỗi {', '.join(bad)}")
        ev_check("x25.sensors", False, "x25.sensors_unhealthy", {"names": ", ".join(bad)},
                 f"unhealthy sensors: {', '.join(bad)}", "warn" if set(bad) <= set(SENSORS_WARN_ONLY) else None)
    else:
        log("Cảm biến : bình thường")
        ev_check("x25.sensors", True, "x25.sensors_ok")

    gps = d["gps"]
    sats = gps["sats"] or 0
    log(f"GPS      : fix {gps['fix']}, {sats} vệ tinh, HDOP {gps['hdop']}")
    if not gps["fix"]:  # None: không có GPS_RAW_INT; 0: GPS_FIX_TYPE_NO_GPS
        ev_check("x25.gps", False, "x25.gps_none", msg="no GPS reported", severity="warn")
    elif gps["fix"] < MAV.GPS_FIX_TYPE_3D_FIX:
        ev_check("x25.gps", False, "x25.gps_no_fix", {"sats": sats}, f"no 3D fix ({sats} satellites)", "warn")
    else:
        ev_check("x25.gps", True, "x25.gps_fix", {"sats": sats})

    v = d["battery"]["voltage"]
    log(f"Pin      : {'không có' if v is None else f'{v:g} V'}")
    if v is None or v < 1:
        ev_check("x25.battery", False, "x25.battery_absent", msg="no battery voltage reported", severity="warn")
    elif _unhealthy(sensors, "battery"):
        ev_check("x25.battery", False, "x25.battery_low", {"voltage": f"{v:g}"},
                 f"battery low or battery monitor fault ({v:g} V)", "warn")
    else:
        ev_check("x25.battery", True, "x25.battery_ok", {"voltage": f"{v:g}"})

    ekf = d["ekf"]
    log(f"EKF      : {'không có EKF_STATUS_REPORT' if ekf['ok'] is None else 'ổn định' if ekf['ok'] else 'chưa ổn định'}")
    if ekf["ok"] is None:
        ev_check("x25.ekf", False, "x25.ekf_missing", msg="no EKF_STATUS_REPORT", severity="warn")
    elif not ekf["ok"]:
        ev_check("x25.ekf", False, "x25.ekf_bad", msg="EKF not healthy", severity="warn")
    else:
        ev_check("x25.ekf", True, "x25.ekf_ok")

    for t in d["prearm"]:
        log(f"  {t}")
    if d["prearm"]:
        text = d["prearm"][0][len("PreArm:"):].strip()
        ev_check("x25.prearm", False, "x25.prearm_failed", {"text": text}, d["prearm"][0], "warn")
    elif _unhealthy(sensors, "prearm"):
        ev_check("x25.prearm", False, "x25.prearm_unhealthy", msg="pre-arm checks do not pass", severity="warn")
    else:
        ev_check("x25.prearm", True, "x25.prearm_ok")


def ev_status(fc, params):
    """Sự kiện info x25.status: tình trạng đọc lúc kết nối và giá trị NET_* hiện có trên mạch bay."""
    if fc.status is None:
        return
    net = OrderedDict()
    for n in sorted(params):
        if n.startswith("NET_"):
            v = round(float(params[n][0]), 6)
            net[n] = int(v) if v.is_integer() else v
    d = OrderedDict(fc.status, net=net)
    ev_info("x25.status", f"{d['firmware'] or 'unknown firmware'} on {d['board'] or 'unknown board'}, "
            f"{d['vehicle']}, {'armed' if d['armed'] else 'disarmed'}", data=d)


def build_targets(a):
    """Tham số bắt buộc (phải có và đúng) và tùy chọn (chỉnh nếu firmware có)."""
    required = OrderedDict()
    required["NET_ENABLE"] = 1
    required["NET_P1_TYPE"] = 1  # UDP Client
    for i in range(4):
        required[f"NET_IPADDR{i}"] = a.x25_ip[i]
    required["NET_NETMASK"] = a.netmask
    for i in range(4):
        required[f"NET_P1_IP{i}"] = a.cm5_ip[i]
    required["NET_P1_PORT"] = a.port
    required["NET_P1_PROTOCOL"] = 2  # MAVLink2
    optional = OrderedDict()
    optional["NET_DHCP"] = 0
    for i in range(4):
        optional[f"NET_GWADDR{i}"] = a.gateway[i]
    return required, optional


def ip_set_by_os(params):
    """Nhánh NET_ đã hiện (có NET_OPTIONS) mà không có NET_IPADDR*: firmware không tự đặt IP của mạch, hệ điều hành
    đặt (SITL, mạch chạy Linux; ArduPilot chỉ có NET_IPADDR*, NET_NETMASK trên ChibiOS có Ethernet). NET_P1_* vẫn dùng."""
    return "NET_OPTIONS" in params and "NET_IPADDR0" not in params


def write_backup(params, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, time.strftime("x25_params_%Y%m%d-%H%M%S.parm"))
    with open(path, "w") as f:
        for name in sorted(params):
            f.write(f"{name},{float(params[name][0])!r}\n")
    log(f"  Đã sao lưu {len(params)} tham số vào {path}")
    ev_info("x25.backup_written", f"saved {len(params)} parameters to {path}", {"file": path, "count": len(params)})
    return path


def load_parm(path):
    values = OrderedDict()
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.replace("\t", ",").replace(" ", ",").split(",")
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                values[parts[0]] = float(parts[1])
    return values


def list_backups(backup_dir):
    """[{"file", "time", "params"}], mới nhất trước. Thư mục chưa có thì danh sách rỗng."""
    items = []
    for path in glob.glob(os.path.join(backup_dir, BACKUP_GLOB)):
        try:
            mtime = os.path.getmtime(path)
            with open(path, encoding="utf-8", errors="replace") as f:
                count = sum(1 for line in f if line.split("#", 1)[0].strip())
        except OSError:
            continue
        stamp = datetime.datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
        items.append({"file": path, "time": stamp, "params": count, "_m": mtime})
    items.sort(key=lambda it: (it["_m"], it["file"]), reverse=True)
    for it in items:
        del it["_m"]
    return items


def configure(fc, a):
    required, optional = build_targets(a)
    targets = OrderedDict(required)
    targets.update(optional)
    last_missing = None
    os_ip = False
    for rnd in range(1, MAX_ROUNDS + 1):
        log(f"\n== Vòng {rnd}: tải tham số")
        if rnd == 1:
            if a.preview or a.no_backup:
                ev_step("x25.backup", "skip")
            else:
                ev_step("x25.backup", "start")
        params = fc.fetch_all()
        log(f"  Nhận {len(params)} tham số")
        if rnd == 1:
            ev_status(fc, params)
            if not (a.preview or a.no_backup):
                write_backup(params, a.backup_dir)
                ev_step("x25.backup", "ok")
            ev_step("x25.params", "start")
        if "NET_ENABLE" not in params:
            fail("Firmware không có NET_ENABLE: bản ArduCopter này không hỗ trợ Ethernet", 1, "x25.no_ethernet")
        if not os_ip and ip_set_by_os(params):
            os_ip = True
            names = ", ".join(HOST_IP_PARAMS)
            log(f"  Firmware không tự đặt IP của mạch (SITL / Linux), bỏ qua: {names}")
            ev_info("x25.ip_by_os", "the firmware does not set its own IP address (SITL or Linux board), "
                    f"skipped: {names}", {"names": names})

        diff = [(n, params[n][0], v) for n, v in targets.items() if n in params and not same(params[n][0], v)]
        missing = [n for n in required if n not in params and not (os_ip and n in HOST_IP_PARAMS)]
        if not diff and not missing:
            log("  Mọi tham số mạng đã đúng.")
            # x25.params_ok nghĩa là "không cần ghi": chỉ khi xong ngay ở vòng 1 (các vòng sau là sau khi đã ghi)
            ev_step("x25.params", "ok", "x25.params_ok" if rnd == 1 else None)
            return
        for n, old, new in diff:
            log(f"  {n:16s} {fmt(old):>8s} -> {fmt(new)}")
            if a.preview:
                ev_change(n, fmt(old), fmt(new))
        if missing:
            log(f"  Chưa xuất hiện (cần khởi động lại): {', '.join(missing)}")
            if a.preview:
                for n in missing:  # chưa có trên mạch bay: sẽ được ghi sau lần khởi động lại đầu tiên
                    ev_change(n, "", fmt(required[n]))
        if a.preview:
            log("\n--preview: không ghi gì.")
            ev_step("x25.params", "ok")
            return

        fc.ensure_disarmed()
        for n, old, new in diff:
            if not fc.set_param(n, new, params[n][1]):
                fail(f"Ghi {n}={fmt(new)} không được xác nhận", 1, "x25.set_failed", {"name": n, "value": fmt(new)})
            ev_change(n, fmt(old), fmt(new))
        if diff:
            log(f"  Đã ghi {len(diff)} tham số.")
        elif last_missing == set(missing):
            fail(f"Đã khởi động lại mà vẫn thiếu: {', '.join(missing)}", 1, "x25.still_missing",
                 {"names": ", ".join(missing)})
        last_missing = set(missing)
        fc.reboot()
    fail(f"Chưa cấu hình xong sau {MAX_ROUNDS} vòng", 1, "x25.not_configured", {"rounds": MAX_ROUNDS})


def restore(fc, a):
    if not os.path.isfile(a.restore):
        fail(f"Không thấy tệp sao lưu {a.restore}", 2, "x25.backup_missing", {"file": a.restore})
    wanted = load_parm(a.restore)
    ev_step("x25.params", "start")
    params = fc.fetch_all()
    ev_status(fc, params)
    log(f"  Bản sao lưu: {len(wanted)} tham số, mạch bay: {len(params)} tham số, lọc tiền tố '{a.prefix}'")

    def restorable(n):
        return (n.startswith(a.prefix) and not n.startswith(RESTORE_SKIP_PREFIXES)
                and not any(m in n for m in RESTORE_SKIP_MARKERS))

    diff = [(n, params[n][0], v) for n, v in wanted.items()
            if restorable(n) and n in params and not same(params[n][0], v)]
    for n, old, new in diff:
        log(f"  {n:16s} {fmt(old):>8s} -> {fmt(new)}")
        if a.preview or not a.yes:
            ev_change(n, fmt(old), fmt(new))
    if not diff:
        log("  Không có gì khác bản sao lưu.")
        ev_step("x25.params", "ok", "x25.params_ok")
        return
    if a.preview or not a.yes:
        log(f"\n{len(diff)} tham số sẽ được ghi. Chạy lại kèm --yes để ghi và khởi động lại mạch bay.")
        ev_step("x25.params", "ok")
        return
    fc.ensure_disarmed()
    failed = []
    for n, old, new in diff:
        if fc.set_param(n, new, params[n][1]):
            ev_change(n, fmt(old), fmt(new))
        else:
            failed.append(n)
    if failed:
        log(f"  [CẢNH BÁO] Không ghi được: {', '.join(failed)}")
        ev_step("x25.params", "warn", "x25.restore_partial", {"names": ", ".join(failed)})
    else:
        ev_step("x25.params", "ok")
    log(f"  Đã ghi {len(diff) - len(failed)} tham số.")
    fc.reboot()


def check_ethernet(a, key):
    """Ping X25 rồi chờ HEARTBEAT qua mavlink-router (đường Ethernet), đọc thử 1 tham số.
    Khi bật ký: lệnh có chữ ký phải được trả lời, lệnh không ký phải bị bỏ qua."""
    ip = ".".join(map(str, a.x25_ip))
    log(f"\n== Kiểm tra MAVLink qua Ethernet ({ip})")
    pinged = False
    for _ in range(15):
        if subprocess.run(["ping", "-c", "1", "-W", "1", ip], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            pinged = True
            break
        time.sleep(2)
    log(f"  ping {ip}: {'OK' if pinged else 'KHÔNG trả lời'}")
    try:
        conn = mavutil.mavlink_connection(a.router, source_system=253, source_component=191)
    except Exception as e:
        log(f"  Không mở được {a.router} ({e}): mavlink-router có đang chạy không?")
        return False
    if key:
        conn.setup_signing(key, sign_outgoing=True, allow_unsigned_callback=accept_unsigned)
    hb = None
    end = time.time() + 30
    while hb is None and time.time() < end:
        msg = conn.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        if msg is not None and msg.autopilot != MAV.MAV_AUTOPILOT_INVALID:
            hb = msg
    if hb is None:
        conn.close()
        log("  Không nhận được MAVLink qua Ethernet.")
        log("  Kiểm tra: cáp LAN, `ip -br addr show eth0`, `sudo tcpdump -ni eth0 udp port 14550`.")
        return False
    sysid, compid = hb.get_srcSystem(), hb.get_srcComponent()
    log(f"  HEARTBEAT qua {a.router}: sysid={sysid}, mode={mavutil.mode_string_v10(hb)}")

    value = read_param(conn, sysid, compid, PROBE_PARAM)
    if value is None:
        conn.close()
        log(f"  Mạch bay không trả lời lệnh{' có chữ ký' if key else ''} qua Ethernet.")
        if key:
            log("  Khóa ký trên mạch bay khác MAVLINK_SIGNING_PASSPHRASE? Chạy lại bước x25 để nạp lại khóa.")
        return False
    log(f"  Đọc {PROBE_PARAM} qua Ethernet{' bằng lệnh có ký' if key else ''} = {fmt(value)}  -> 2 chiều OK")

    ok = True
    if a.signing == "on":
        conn.mav.signing.sign_outgoing = False
        if read_param(conn, sysid, compid, PROBE_PARAM, tries=2) is None:
            log("  Lệnh KHÔNG ký bị từ chối -> ký MAVLink2 đang được bắt buộc")
        else:
            log("  [CẢNH BÁO] Mạch bay vẫn nhận lệnh không ký qua Ethernet: ký MAVLink2 chưa có hiệu lực")
            ok = False
    conn.close()
    return ok


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Cấu hình Ethernet cho CUAV X25 EVO qua USB")
    ap.add_argument("--device", default=env("X25_SERIAL", "auto"),
                    help="cổng serial (hoặc tcp:host:cổng, vd SITL), mặc định tự tìm")
    ap.add_argument("--baud", type=int, default=int(env("X25_BAUD", "115200")))
    ap.add_argument("--x25-ip", type=ipv4, default=env("X25_IP", "192.168.50.1"))
    ap.add_argument("--netmask", type=int, default=int(env("X25_NETMASK", "24")))
    ap.add_argument("--gateway", type=ipv4, default=env("X25_GATEWAY", "0.0.0.0"))
    ap.add_argument("--cm5-ip", type=ipv4, default=env("CM5_LAN_IP", "192.168.50.2"),
                    help="IP của CM5 trên dây LAN (đích NET_P1_IP*)")
    ap.add_argument("--port", type=int, default=int(env("MAVLINK_LAN_PORT", "14550")))
    ap.add_argument("--backup-dir", default=os.path.expanduser(env("X25_BACKUP_DIR", default_backup_dir())))
    ap.add_argument("--router", default=f"tcp:127.0.0.1:{env('CM5_TCP_SERVER_PORT', '5760')}",
                    help="mavlink-router trên CM5, để kiểm tra đường Ethernet")
    ap.add_argument("--preview", "--dry-run", dest="preview", action="store_true",
                    help="chỉ kết nối, đọc và in khác biệt; không ghi, không khởi động lại, không sao lưu")
    ap.add_argument("--list-backups", action="store_true", help="in danh sách bản sao lưu (JSON) rồi thoát")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--skip-eth-check", action="store_true")
    ap.add_argument("--restore", metavar="TỆP", help="ghi lại tham số từ tệp .parm sao lưu")
    ap.add_argument("--prefix", default="NET_", help="chỉ khôi phục tham số có tiền tố này ('' = tất cả)")
    ap.add_argument("--yes", action="store_true", help="xác nhận ghi khi --restore")
    ap.add_argument("--signing", choices=["on", "keep", "off"],
                    default="on" if env("MAVLINK_SIGNING", "0") == "1" else "keep",
                    help="on: nạp khóa từ MAVLINK_SIGNING_PASSPHRASE; keep: không đụng tới; off: xoá khóa")
    return ap.parse_args(argv)


def main(argv=None):
    CONFIG.clear()
    CONFIG.update(load_config())
    _open_steps.clear()
    a = parse_args(argv)

    if a.list_backups:
        print(json.dumps(list_backups(a.backup_dir), ensure_ascii=False))
        return 0

    ev_step("x25.connect", "start")
    if is_wsl() and not a.preview and os.environ.get("UAVLINK_ALLOW_WSL") != "1":
        fail("Đang chạy trong WSL: chỉ --preview được phép (đặt UAVLINK_ALLOW_WSL=1 để bỏ qua).", 4, "env.wsl")

    passphrase = env("MAVLINK_SIGNING_PASSPHRASE")
    if a.signing == "on" and not passphrase and not a.preview:
        fail("Bật ký MAVLink nhưng thiếu MAVLINK_SIGNING_PASSPHRASE (secrets.env)", 2, "x25.no_passphrase")
    key = signing_key(passphrase) if passphrase and a.signing != "off" else None

    device = a.device
    if device == "auto":
        device = find_device()
    if not device_present(device):
        ev_check("x25.usb", False, "x25.not_found", msg="no X25 on USB")
        log("Không thấy X25 qua USB (/dev/serial/by-id/*ArduPilot*). Cắm cáp USB và kiểm tra `ls /dev/serial/by-id/`.")
        if a.preview:
            ev_step("x25.connect", "skip", "x25.not_found")
            return 0
        fail("Không thấy X25 qua USB.", 1, "x25.not_found")
    a.device = device
    ev_check("x25.usb", True, "x25.usb_found", {"device": device})
    log(f"Cổng USB : {a.device}")
    log("AN TOÀN  : tháo cánh quạt, không ARM; script sẽ khởi động lại mạch bay khi cần.")

    fc = FlightController(a.device, a.baud)
    fc.connect(timeout=30)
    if fc.heartbeat.autopilot != MAV.MAV_AUTOPILOT_ARDUPILOTMEGA:
        fail(f"Mạch bay không chạy ArduPilot (autopilot={fc.heartbeat.autopilot})", 1, "x25.not_ardupilot",
             {"autopilot": int(fc.heartbeat.autopilot)})
    snap = fc.health_snapshot()
    fc.status = status_data(fc, snap)
    ver = snap["AUTOPILOT_VERSION"]
    version = sw_version(ver.flight_sw_version) if ver is not None else None
    armed = fc.armed
    log(f"Mạch bay : sysid {fc.sysid}, firmware {fc.status['firmware'] or 'không rõ'}, "
        f"mạch {fc.status['board'] or 'không rõ'}, mode {mavutil.mode_string_v10(fc.heartbeat)}, "
        f"{'ARMED' if armed else 'disarmed'}")
    ev_check("x25.firmware", True, "x25.firmware", {"version": version or "unknown"})
    ev_check("x25.disarmed", not armed, None if not armed else "x25.armed")
    health_checks(fc.status)
    if armed and not a.preview:
        fail("Mạch bay đang ARM. Disarm (và tháo cánh quạt) rồi chạy lại.", 4, "x25.armed")
    ev_step("x25.connect", "ok")

    if a.restore:
        restore(fc, a)
        fc.close()
        return 0

    configure(fc, a)
    if a.preview:
        ev_step("x25.reboot", "skip")
    if a.signing == "on":
        ev_step("x25.signing", "start")
        if a.preview:
            log("\n--preview: sẽ nạp khóa ký MAVLink2 (MAVLINK_SIGNING_PASSPHRASE) xuống mạch bay.")
            ev_info("x25.signing_pending", "the MAVLink 2 signing key would be loaded onto the flight controller")
            ev_step("x25.signing", "skip")
        else:
            fc.ensure_disarmed()
            fc.setup_signing(key)
            log("\n== Đã nạp khóa ký MAVLink2: cổng mạng chỉ còn nhận lệnh có chữ ký.")
            ev_step("x25.signing", "ok", "x25.signing_set")
    elif a.signing == "off" and not a.preview:
        ev_step("x25.signing", "start")
        fc.ensure_disarmed()
        fc.setup_signing(bytes(32))
        log("\n== Đã xoá khóa ký MAVLink2 trên mạch bay (tắt ký).")
        ev_step("x25.signing", "ok", "x25.signing_cleared")
    else:
        ev_step("x25.signing", "skip")
    fc.close()
    if a.preview or a.skip_eth_check:
        ev_step("x25.ethcheck", "skip")
        return 0
    ev_step("x25.ethcheck", "start")
    if not check_ethernet(a, key):
        ev_step("x25.ethcheck", "fail", "x25.ethcheck_failed", msg="MAVLink over Ethernet did not pass the check")
        return 3
    ev_step("x25.ethcheck", "ok")
    log("\nHoàn tất: X25 gửi MAVLink2 tới "
        f"{'.'.join(map(str, a.cm5_ip))}:{a.port} qua Ethernet"
        f"{', chỉ nhận lệnh có chữ ký' if a.signing == 'on' else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
