#!/usr/bin/env python3
"""
Kiểm tra đường mạng (ARCHITECTURE.md mục 4.3). Chỉ đọc: không đổi cấu hình, không ghi tham số.

  net_probe.py [--mode auto|live|probe] [--timeout S]

  - X25 qua LAN: ping X25_IP (chỉ cảnh báo), HEARTBEAT của X25 trên UDP MAVLINK_LAN_PORT hoặc qua router đang chạy.
  - Internet, VPS: DNS, ping (chỉ cảnh báo), cổng TCP GCS của VPS.
  - live  (router trên máy này đang gửi lên VPS): chỉ mở kết nối TCP tới VPS như một GCS bên ngoài; thấy HEARTBEAT
          của X25 và đọc được NET_ENABLE (lệnh có ký) qua VPS.
  - probe (chưa có router gửi lên VPS): tự gửi HEARTBEAT loại GCS (sysid 250) lên VPS_UPLINK_PORT, xem nó ra cổng
          TCP; gửi từ phía TCP, xem nó về; cổng UDP GCS phải trả lời. Endpoint UDP Server của mavlink-router chỉ nhớ
          một địa chỉ (người gửi cuối), nên nếu đang có máy bay (hoặc GCS) khác đi qua VPS thì bỏ qua phần UDP.
  - auto  = live nếu uavlink-router (hoặc mavlink-router) đang chạy và main.conf có endpoint tới VPS, ngược lại probe.
  - GCS qua Tailscale: tailscale ping từng đích 100.64.0.0/10 trong CM5_EXTRA_UDP_TARGETS (chỉ cảnh báo).
  - CM5: router nghe TCP CM5_TCP_SERVER_PORT / UDP MAVLINK_LAN_PORT; tường lửa cho MAVLink vào từ X25 và tailscale0.

Cấu hình từ $UAVLINK_CONFIG_DIR/config.env và secrets.env (mặc định /etc/uavlink); biến môi trường ghi đè tệp.
Passphrase ký lấy từ secrets.env đó, không có thì từ /etc/uavlink/secrets.env.
--timeout là tổng thời gian cho cả lần chạy (mặc định 25 s); mỗi lần chờ được một phần cố định của nó.
Có UAVLINK_EVENTS=1 thì in thêm các dòng sự kiện "@@UAVLINK {json}" (ARCHITECTURE.md mục 4.1).
Mã thoát: 0 đạt (có thể có cảnh báo), 2 sai tham số, 3 có kiểm tra hỏng.

Biến môi trường cho kiểm thử: UAVLINK_ROUTER_CONF (main.conf), UAVLINK_FALLBACK_SECRETS, UAVLINK_INTERNET_HOSTS
("ip:cổng ip:cổng"), UAVLINK_PROC_NET (/proc/net).
"""
import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

# Dùng MAVLink2 ngay từ đầu (giống mav_probe.py): lệnh có ký cần MAVLink2.
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")
try:
    from pymavlink import mavutil  # noqa: E402
except ImportError:  # người gọi chưa đặt PYTHONPATH: dùng bản đóng gói kèm <engine>/../vendor
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "vendor"))
    from pymavlink import mavutil  # noqa: E402

MAV = mavutil.mavlink
SIGNING_EPOCH = 1420070400  # 2015-01-01 UTC, gốc thời gian của MAVLink signing

# Máy kiểm tra xưng là một GCS (không bao giờ là loại máy bay), sysid 250. Mỗi socket một compid riêng: router bỏ
# một gói khi endpoint nhận đã từng gửi chính sysid/compid đó (chống vòng lặp), nên hai đầu không được trùng.
PROBE_SYSID = 250
COMP_UPLINK = 191           # socket UDP gửi lên VPS_UPLINK_PORT (đóng vai CM5)
COMP_TCP = 192              # kết nối TCP tới cổng GCS của VPS (đóng vai GCS bên ngoài)
COMP_GCS_UDP = 193          # socket UDP gửi tới VPS_GCS_UDP_PORT
# Tham số đọc thử qua VPS: có trên mọi firmware có Ethernet (NET_IPADDR* thì không: SITL, mạch chạy Linux không có)
PARAM = "NET_ENABLE"

ROUTER_UNITS = ("uavlink-router.service", "mavlink-router.service")
DEFAULT_CONFIG_DIR = "/etc/uavlink"
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

# Thời gian chờ (giây) khi --timeout 25; tổng trường hợp xấu nhất dưới 25 s (các lệnh tailscale ping chạy song song).
DEFAULT_TIMEOUT = 25.0
WAITS = {"ping": 1, "lan": 2.5, "internet": 2, "dns": 2, "connect": 2.5, "listen": 2.5, "uplink": 2.5,
         "downlink": 2, "gcs_udp": 2, "x25_via_vps": 3, "cmd": 4, "tailscale": 2}
RESEND = 0.5                # chu kỳ gửi lại HEARTBEAT khi chờ

CONFIG = {}                 # config.env + secrets.env; main() nạp
_scale = 1.0                # --timeout / 25


def wait(name):
    return WAITS[name] * _scale


# ---- sự kiện (ARCHITECTURE.md 4.1) -------------------------------------------------------------------------------

_out_lock = threading.Lock()


def ev(**obj):
    if os.environ.get("UAVLINK_EVENTS") != "1":
        return
    with _out_lock:
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
    ev(**_with({"t": "step", "id": sid, "state": state}, code, args, msg))


def ev_info(code, msg, args=None):
    ev(**_with({"t": "info"}, code, args, msg))


def log(msg=""):
    with _out_lock:
        print(msg, flush=True)


class Report:
    """Đếm kết quả, in dòng cho người đọc và sự kiện "check"."""

    def __init__(self):
        self.passed = self.failed = self.warned = 0
        self.step = None

    def begin(self, sid, title):
        log(f"\n== {title}")
        ev_step(sid, "start")
        self.step = {"id": sid, "failed": 0, "warned": 0}

    def end(self):
        st = self.step
        ev_step(st["id"], "fail" if st["failed"] else "warn" if st["warned"] else "ok")
        self.step = None

    def skip(self, sid, title, code, msg, args=None):
        log(f"\n== {title}\n   skipped: {msg}")
        ev_step(sid, "skip", code, args, msg)

    def check(self, cid, ok, code, msg, args=None, warn=False):
        obj = _with({"t": "check", "id": cid, "ok": bool(ok)}, code, args, msg)
        if ok:
            self.passed += 1
            mark = "[ok]  "
        elif warn:
            obj["severity"] = "warn"
            self.warned += 1
            mark = "[warn]"
            if self.step:
                self.step["warned"] += 1
        else:
            self.failed += 1
            mark = "[FAIL]"
            if self.step:
                self.step["failed"] += 1
        log(f"   {mark} {msg}")
        ev(**obj)


# ---- cấu hình (bộ đọc nhỏ, giống x25_configure.py; không chạy shell) --------------------------------------------

def _parse_value(raw):
    """Giá trị sau dấu '=': không quote (bỏ '# ghi chú' cuối dòng), "…", '…', ghép được ('a'\\''b')."""
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
    config_dir = config_dir or os.environ.get("UAVLINK_CONFIG_DIR") or DEFAULT_CONFIG_DIR
    values = read_env_file(os.path.join(config_dir, "config.env"))
    values.update(read_env_file(os.path.join(config_dir, "secrets.env")))
    return values


def env(name, default=""):
    """Biến môi trường thắng tệp cấu hình; rỗng coi như chưa đặt."""
    return os.environ.get(name) or CONFIG.get(name) or default


def env_port(name, default):
    try:
        port = int(env(name, str(default)))
    except ValueError:
        return default
    return port if 0 < port < 65536 else default


def signing_passphrase():
    """Passphrase ký: môi trường / secrets.env của thư mục cấu hình, không có thì secrets.env mặc định."""
    value = env("MAVLINK_SIGNING_PASSPHRASE")
    if not value:
        fallback = os.environ.get("UAVLINK_FALLBACK_SECRETS") or os.path.join(DEFAULT_CONFIG_DIR, "secrets.env")
        value = read_env_file(fallback).get("MAVLINK_SIGNING_PASSPHRASE", "")
    return value


def tailscale_targets(text):
    """[(tên, ip)] của các đích name=ip:port trong dải Tailscale 100.64.0.0/10."""
    out = []
    for item in text.split():
        name, sep, addr = item.partition("=")
        host = addr.rsplit(":", 1)[0] if sep else ""
        try:
            if ipaddress.ip_address(host) in TAILSCALE_NET:
                out.append((name, host))
        except ValueError:
            continue
    return out


# ---- router trên máy này -----------------------------------------------------------------------------------------

def read_router_conf(path):
    """Các mục của main.conf: [{"type": "udpendpoint", "name": …, "mode": …, "address": …, "port": …}, …]."""
    sections, cur = [], None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return sections
    for raw in lines:
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            head = line[1:-1].split(None, 1)
            cur = {"type": head[0].lower() if head else "", "name": head[1].strip() if len(head) > 1 else ""}
            sections.append(cur)
        elif cur is not None and "=" in line:
            key, _, value = line.partition("=")
            cur[key.strip().lower()] = value.strip()
    return sections


def vps_endpoint(sections, hosts):
    """Tên endpoint UDP (chế độ client) gửi tới một trong `hosts`, hoặc None."""
    for s in sections:
        if s["type"] == "udpendpoint" and s.get("mode", "normal").lower() not in ("server", "eavesdropping") \
                and s.get("address") in hosts:
            return s["name"] or "vps"
    return None


def router_unit():
    """Tên dịch vụ router đang chạy, hoặc None."""
    if not shutil.which("systemctl"):
        return None
    for unit in ROUTER_UNITS:
        try:
            if subprocess.run(["systemctl", "is-active", "--quiet", unit], timeout=5,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                return unit
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def listening(proto, port):
    """Có socket nghe `port` trong /proc/net (tcp: trạng thái LISTEN; udp: đã bind)."""
    base = os.environ.get("UAVLINK_PROC_NET") or "/proc/net"
    for name in (proto, proto + "6"):
        try:
            with open(os.path.join(base, name), encoding="ascii", errors="replace") as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if len(fields) < 4 or ":" not in fields[1]:
                continue
            try:
                local_port = int(fields[1].rsplit(":", 1)[1], 16)
            except ValueError:
                continue
            if local_port == port and (proto == "udp" or fields[3] == "0A"):
                return True
    return False


# ---- MAVLink trên socket -----------------------------------------------------------------------------------------

class _Out:
    def __init__(self, write):
        self.write = write


class Link:
    """Một socket với bộ mã hóa riêng (sysid 250, `compid`) và bộ giải mã không kiểm chữ ký (chỉ quan sát)."""

    def __init__(self, sock, compid, addr=None):
        self.sock, self.addr = sock, addr
        self.tx = MAV.MAVLink(_Out(self._write), srcSystem=PROBE_SYSID, srcComponent=compid)
        self.rx = MAV.MAVLink(None)
        self.rx.robust_parsing = True
        self.good = self.bad = 0
        self.closed = False

    @classmethod
    def udp(cls, compid, addr):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        return cls(sock, compid, addr)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _write(self, data):
        try:
            if self.addr:
                self.sock.sendto(data, self.addr)
            else:
                self.sock.sendall(data)
        except OSError:
            self.closed = self.addr is None

    def sign(self, passphrase):
        s = self.tx.signing
        s.secret_key = hashlib.sha256(passphrase.encode("utf-8")).digest()   # như Mission Planner, QGC
        s.sign_outgoing = True
        s.link_id = 0
        s.timestamp = int((max(time.time(), SIGNING_EPOCH) - SIGNING_EPOCH) * 100000)   # đơn vị 10 µs

    def heartbeat(self):
        self.tx.heartbeat_send(MAV.MAV_TYPE_GCS, MAV.MAV_AUTOPILOT_INVALID, 0, 0, 0)

    def recv(self, secs, match=None, tick=None, source=None):
        """Đọc tới khi `match(msg)` đúng (trả về msg) hoặc hết `secs` giây (None). `tick()` chạy mỗi RESEND giây;
        với UDP chỉ nhận gói từ `source` ((ip, cổng), hoặc chỉ ip)."""
        end = time.time() + secs
        next_tick = time.time()
        while not self.closed:
            now = time.time()
            if tick and now >= next_tick:
                tick()
                next_tick = now + RESEND
            left = end - now
            if left <= 0:
                return None
            self.sock.settimeout(max(0.01, min(left, next_tick - now) if tick else left))
            try:
                data, src = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionRefusedError:      # ICMP "port unreachable" của gói vừa gửi
                continue
            except OSError:
                self.closed = True
                return None
            if self.addr is None and not data:
                self.closed = True
                return None
            if source and (src if isinstance(source, tuple) else src[0]) != source:
                continue
            for m in self.rx.parse_buffer(data) or []:
                if m.get_type() == "BAD_DATA":
                    self.bad += 1
                    continue
                self.good += 1
                if match and match(m):
                    return m
        return None


def is_vehicle_heartbeat(m):
    return m.get_type() == "HEARTBEAT" and m.type != MAV.MAV_TYPE_GCS and m.autopilot != MAV.MAV_AUTOPILOT_INVALID


def is_foreign_gcs_heartbeat(m):
    return m.get_type() == "HEARTBEAT" and m.type == MAV.MAV_TYPE_GCS and m.get_srcSystem() != PROBE_SYSID


def from_probe(compid):
    return lambda m: m.get_srcSystem() == PROBE_SYSID and m.get_srcComponent() == compid


def tcp_connect(ip, port, secs):
    """(Link, None) hoặc (None, "refused" | "timeout" | "error")."""
    try:
        sock = socket.create_connection((ip, port), timeout=secs)
    except ConnectionRefusedError:
        return None, "refused"
    except socket.timeout:
        return None, "timeout"
    except OSError:
        return None, "error"
    return Link(sock, COMP_TCP), None


def resolve_ipv4(host, secs):
    """IPv4 của `host` (IP giữ nguyên), None nếu không phân giải được trong `secs` giây."""
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        pass
    box = []

    def work():
        try:
            box.append(socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)[0][4][0])
        except (OSError, IndexError, UnicodeError):
            pass

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(secs)
    return box[0] if box else None


def ping(ip, secs):
    secs = max(1, int(round(secs)))
    try:
        return subprocess.run(["ping", "-n", "-q", "-c", "2", "-i", "0.2", "-W", str(secs), ip], timeout=secs + 2,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def read_param(link, sysid, compid, secs):
    """Gửi PARAM_REQUEST_READ (mỗi giây một lần) tới khi có PARAM_VALUE của PARAM; trả về giá trị hoặc None."""
    def ask():
        link.tx.param_request_read_send(sysid, compid, PARAM.encode("ascii"), -1)

    def match(m):
        if m.get_type() != "PARAM_VALUE" or m.get_srcSystem() != sysid:
            return False
        pid = m.param_id.decode("ascii", "ignore") if isinstance(m.param_id, bytes) else m.param_id
        return pid.rstrip("\x00") == PARAM

    end = time.time() + secs
    while time.time() < end:
        ask()
        m = link.recv(min(1.0, end - time.time()), match)
        if m is not None:
            return m.param_value
    return None


# ---- các nhóm kiểm tra -------------------------------------------------------------------------------------------

def check_lan(rep, s):
    """X25 qua LAN; trả về sysid của X25 nếu thấy HEARTBEAT."""
    if not s["x25_ip"]:
        rep.skip("net.lan", "X25 over LAN", "net.no_x25_ip", "X25_IP is not set, X25 LAN checks skipped")
        return None
    rep.begin("net.lan", "X25 over LAN")
    ip, port = s["x25_ip"], s["lan_port"]
    ok = ping(ip, wait("ping"))
    rep.check("x25.lan_ping", ok, "x25.lan_ping.ok" if ok else "x25.lan_ping.fail",
              f"X25 {ip} {'answers' if ok else 'does not answer'} ping", {"ip": ip}, warn=True)

    hb, via_router = None, s["router"] or s["mode"] == "live"
    if via_router:
        tcp = s["cm5_tcp_port"]
        link, _ = tcp_connect("127.0.0.1", tcp, wait("connect"))
        if link:
            hb = link.recv(wait("lan"), is_vehicle_heartbeat)
            link.close()
        if hb is not None:
            rep.check("x25.lan_mavlink", True, "x25.lan_mavlink.router",
                      f"X25 HEARTBEAT seen through the router (TCP {tcp}, sysid {hb.get_srcSystem()})",
                      {"port": tcp, "sysid": hb.get_srcSystem()})
        else:
            rep.check("x25.lan_mavlink", False, "x25.lan_mavlink.router_fail",
                      f"no X25 HEARTBEAT through the router (TCP {tcp})", {"port": tcp})
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            sock.close()
            rep.check("x25.lan_mavlink", False, "x25.lan_mavlink.port_busy",
                      f"UDP port {port} is used by another program, X25 HEARTBEAT not checked", {"port": port},
                      warn=True)
            rep.end()
            return None
        link = Link(sock, COMP_UPLINK)
        hb = link.recv(wait("lan"), is_vehicle_heartbeat, source=ip)
        link.close()
        if hb is not None:
            rep.check("x25.lan_mavlink", True, "x25.lan_mavlink.ok",
                      f"X25 {ip} sends HEARTBEAT to UDP port {port} (sysid {hb.get_srcSystem()})",
                      {"ip": ip, "port": port, "sysid": hb.get_srcSystem()})
        else:
            rep.check("x25.lan_mavlink", False, "x25.lan_mavlink.fail",
                      f"no HEARTBEAT from X25 {ip} on UDP port {port}", {"ip": ip, "port": port})
    rep.end()
    return hb.get_srcSystem() if hb is not None else None


def check_internet(rep, s):
    rep.begin("net.internet", "Internet")
    end = time.time() + wait("internet")
    reached = None
    for host in s["internet_hosts"]:
        addr, _, port = host.rpartition(":")
        left = end - time.time()
        if left <= 0:
            break
        try:
            socket.create_connection((addr, int(port)), timeout=left).close()
            reached = host
            break
        except (OSError, ValueError):
            continue
    if reached:
        rep.check("internet", True, "internet.reachable", f"Internet is reachable ({reached})", {"host": reached})
    else:
        rep.check("internet", False, "internet.unreachable", "Internet is not reachable", warn=not s["vps_host"])
    rep.end()


def check_vps(rep, s, x25_sysid):
    host = s["vps_host"]
    if not host:
        rep.skip("net.vps", "Relay server (VPS)", "net.no_vps", "VPS_PUBLIC_IP is not set, VPS checks skipped")
        return
    rep.begin("net.vps", f"Relay server (VPS) {host}")
    ip = s["vps_ip"]
    if ip != host:
        rep.check("vps.dns", ip is not None, "vps.dns.ok" if ip else "vps.dns.fail",
                  f"{host} resolves to {ip}" if ip else f"{host} does not resolve",
                  {"host": host, "ip": ip} if ip else {"host": host})
    if ip is None:
        rep.end()
        return
    ok = ping(ip, wait("ping"))
    rep.check("vps.ping", ok, "vps.ping.ok" if ok else "vps.ping.fail",
              f"VPS {ip} {'answers' if ok else 'does not answer'} ping", {"ip": ip}, warn=True)

    tcp_port = s["gcs_tcp_port"]
    link, err = tcp_connect(ip, tcp_port, wait("connect"))
    targs = {"ip": ip, "port": tcp_port}
    if link is None:
        rep.check("vps.tcp", False, f"vps.tcp.{err}", f"GCS TCP port {ip}:{tcp_port}: connection {err}", targs)
        rep.end()
        return
    try:
        if s["mode"] == "live":
            _vps_live(rep, s, ip, link, x25_sysid)
        else:
            _vps_probe(rep, s, ip, link)
    finally:
        link.close()
    rep.end()


def _tcp_check(rep, link, ip, port):
    """vps.tcp sau khi đã đọc một lúc; False nếu kết nối không dùng được."""
    targs = {"ip": ip, "port": port}
    if link.closed:
        rep.check("vps.tcp", False, "vps.tcp.closed", f"GCS TCP port {ip}:{port} closed the connection", targs)
        return False
    if link.bad and not link.good:
        rep.check("vps.tcp", False, "vps.tcp.not_mavlink", f"GCS TCP port {ip}:{port} does not speak MAVLink", targs)
        return False
    rep.check("vps.tcp", True, "vps.tcp.ok", f"GCS TCP port {ip}:{port} accepts connections", targs)
    return True


def _vps_live(rep, s, ip, link, x25_sysid):
    tcp_port = s["gcs_tcp_port"]

    def x25(m):
        return is_vehicle_heartbeat(m) and (x25_sysid is None or m.get_srcSystem() == x25_sysid)

    hb = link.recv(wait("x25_via_vps"), x25)
    if not _tcp_check(rep, link, ip, tcp_port):
        return
    if hb is None:
        rep.check("vps.x25_via_vps", False, "vps.x25_via_vps.fail",
                  f"no X25 HEARTBEAT through the VPS (TCP {ip}:{tcp_port})", {"ip": ip, "port": tcp_port})
        return
    sysid, compid = hb.get_srcSystem(), hb.get_srcComponent()
    rep.check("vps.x25_via_vps", True, "vps.x25_via_vps.ok",
              f"X25 HEARTBEAT seen through the VPS (TCP {ip}:{tcp_port}, sysid {sysid})",
              {"ip": ip, "port": tcp_port, "sysid": sysid})

    passphrase = signing_passphrase()
    if passphrase:
        link.sign(passphrase)
    elif s["signing"]:
        rep.check("vps.cmd_via_vps", False, "vps.cmd_no_passphrase",
                  "no MAVLink2 signing passphrase, command through the VPS not checked", warn=True)
        return
    link.heartbeat()                    # router của VPS biết sysid 250 ở kết nối này
    value = read_param(link, sysid, compid, wait("cmd"))
    signed = "signed " if passphrase else ""
    if value is not None:
        rep.check("vps.cmd_via_vps", True, "vps.cmd_via_vps.ok",
                  f"X25 answers a {signed}{PARAM} read through the VPS ({value:g})",
                  {"param": PARAM, "value": f"{value:g}"})
    else:
        rep.check("vps.cmd_via_vps", False, "vps.cmd_via_vps.fail",
                  f"X25 does not answer a {signed}{PARAM} read through the VPS", {"param": PARAM})


def _vps_probe(rep, s, ip, link):
    tcp_port, up_port, gcs_port = s["gcs_tcp_port"], s["uplink_port"], s["gcs_udp_port"]
    # Nghe trước: có máy bay đi qua VPS thì không đụng tới endpoint UDP nào của VPS.
    seen = {}

    def live(m):
        if is_foreign_gcs_heartbeat(m):
            seen.setdefault("gcs", m.get_srcSystem())
        return is_vehicle_heartbeat(m)

    vehicle = link.recv(wait("listen"), live)
    if not _tcp_check(rep, link, ip, tcp_port):
        return
    if vehicle is not None:
        sysid = vehicle.get_srcSystem()
        rep.check("vps.udp", False, "vps.udp_skipped_live",
                  f"vehicle sysid {sysid} is already live through the VPS, UDP tests skipped", {"sysid": sysid},
                  warn=True)
        return

    up = Link.udp(COMP_UPLINK, (ip, up_port))
    gcs = Link.udp(COMP_GCS_UDP, (ip, gcs_port))
    try:
        uargs = {"ip": ip, "port": up_port, "tcp_port": tcp_port}
        got = link.recv(wait("uplink"), from_probe(COMP_UPLINK), tick=up.heartbeat)
        rep.check("vps.uplink", got is not None, "vps.uplink.relayed" if got else "vps.uplink.lost",
                  f"HEARTBEAT sent to {ip}:{up_port} {'comes' if got else 'does not come'} out on TCP {tcp_port}",
                  uargs)
        # Router của VPS đã nhớ địa chỉ socket `up` (người gửi cuối) nên trả gói từ phía TCP về đó.
        back = up.recv(wait("downlink"), from_probe(COMP_TCP), tick=link.heartbeat, source=(ip, up_port))
        rep.check("vps.downlink", back is not None, "vps.downlink.relayed" if back else "vps.downlink.lost",
                  f"HEARTBEAT sent on TCP {tcp_port} {'reaches' if back else 'does not reach'} this machine "
                  f"through {ip}:{up_port}", uargs)
        gargs = {"ip": ip, "port": gcs_port}
        if "gcs" in seen:
            rep.check("vps.gcs_udp", False, "vps.gcs_udp_skipped_live",
                      f"GCS sysid {seen['gcs']} is connected to the VPS, GCS UDP port not tested",
                      {"sysid": seen["gcs"]}, warn=True)
            return

        def both():
            gcs.heartbeat()
            link.heartbeat()

        reply = gcs.recv(wait("gcs_udp"), lambda m: not from_probe(COMP_GCS_UDP)(m), tick=both,
                         source=(ip, gcs_port))
        rep.check("vps.gcs_udp", reply is not None, "vps.gcs_udp.ok" if reply else "vps.gcs_udp.fail",
                  f"GCS UDP port {ip}:{gcs_port} {'answers' if reply else 'does not answer'} with MAVLink", gargs)
    finally:
        up.close()
        gcs.close()


def check_gcs(rep, s):
    targets = s["ts_targets"]
    if not targets:
        rep.skip("net.gcs", "Tailscale GCS", "gcs.no_targets", "no Tailscale GCS target in CM5_EXTRA_UDP_TARGETS")
        return
    if not shutil.which("tailscale"):
        rep.skip("net.gcs", "Tailscale GCS", "gcs.no_tailscale", "Tailscale is not installed, GCS checks skipped")
        return
    rep.begin("net.gcs", "Tailscale GCS")
    secs = wait("tailscale")
    results = {}

    def one(name, ip):
        cmd = ["tailscale", "ping", "--c", "1", "--until-direct=false", "--timeout", f"{max(secs, 0.5):.1f}s", ip]
        try:
            results[name] = subprocess.run(cmd, timeout=secs + 1, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            results[name] = False

    threads = [threading.Thread(target=one, args=t, daemon=True) for t in targets]
    for t in threads:
        t.start()
    end = time.time() + secs + 1.5
    for t in threads:
        t.join(max(0, end - time.time()))
    for name, ip in targets:
        ok = results.get(name, False)
        rep.check(f"gcs.tailscale.{name}", ok, "gcs.tailscale.ok" if ok else "gcs.tailscale.fail",
                  f"Tailscale GCS {name} ({ip}) {'answers' if ok else 'does not answer'} tailscale ping",
                  {"name": name, "ip": ip}, warn=True)
    rep.end()


def ufw_rules(text):
    """(trạng thái active, mặc định cho vào, [(to, action, from)]) từ `ufw status verbose`."""
    active = default_allow = False
    rules, table = [], False
    for line in text.splitlines():
        if line.startswith("Status:"):
            active = line.split(":", 1)[1].strip() == "active"
        elif line.startswith("Default:"):
            default_allow = "allow (incoming)" in line
        elif line.startswith("--"):
            table = True
        elif table and line.strip():
            cols = [c.strip() for c in line.split("  ") if c.strip()]
            if len(cols) >= 3:
                rules.append((cols[0].replace(" (v6)", ""), cols[1], cols[2].replace(" (v6)", "")))
    return active, default_allow, rules


def _ufw_allows(rules, iface, port=None, src=None):
    wanted = {f"Anywhere on {iface}"}
    if port:
        wanted |= {f"{port}/udp on {iface}", f"{port} on {iface}", f"{port}/udp", str(port), "Anywhere"}
    for to, action, frm in rules:
        if to not in wanted or action not in ("ALLOW", "ALLOW IN"):
            continue
        if frm == "Anywhere" or src is None:
            return True
        try:
            if ipaddress.ip_address(src) in ipaddress.ip_network(frm, strict=False):
                return True
        except ValueError:
            continue
    return False


def check_cm5(rep, s):
    rep.begin("net.cm5", "This machine: listeners and firewall")
    tcp, udp = s["cm5_tcp_port"], s["lan_port"]
    if s["router"] or s["mode"] == "live":
        has_tcp, has_udp = listening("tcp", tcp), listening("udp", udp)
        largs = {"tcp": tcp, "udp": udp}
        if has_tcp and has_udp:
            rep.check("cm5.listen", True, "cm5.listen.ok", f"the router listens on TCP {tcp} and UDP {udp}", largs)
        elif has_udp:
            rep.check("cm5.listen", False, "cm5.listen.no_tcp", f"nothing listens on TCP {tcp}", {"port": tcp})
        elif has_tcp:
            rep.check("cm5.listen", False, "cm5.listen.no_udp", f"nothing listens on UDP {udp}", {"port": udp})
        else:
            rep.check("cm5.listen", False, "cm5.listen.none", f"nothing listens on TCP {tcp} or UDP {udp}", largs)
    else:
        log("   the router is not running, listeners not checked")
        ev_info("cm5.router_stopped", "the MAVLink router is not running, its ports are checked after apply")

    iface, x25_ip = s["eth_iface"], s["x25_ip"]
    fargs = {"iface": iface, "port": udp}
    if not shutil.which("ufw"):
        rep.check("cm5.firewall", True, "cm5.firewall.absent", "ufw is not installed, nothing blocks MAVLink")
        rep.end()
        return
    try:
        p = subprocess.run(["ufw", "status", "verbose"], timeout=10, capture_output=True, text=True)
        text = p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        text = None
    if text is None:
        rep.check("cm5.firewall", False, "cm5.firewall.unknown", "the ufw firewall state cannot be read", warn=True)
        rep.end()
        return
    active, default_allow, rules = ufw_rules(text)
    if not active:
        rep.check("cm5.firewall", True, "cm5.firewall.inactive", "the ufw firewall is off, nothing blocks MAVLink")
    elif default_allow:
        rep.check("cm5.firewall", True, "cm5.firewall.ok", "the ufw firewall allows incoming traffic", fargs)
    elif not _ufw_allows(rules, iface, udp, x25_ip or None):
        rep.check("cm5.firewall", False, "cm5.firewall.no_x25",
                  f"the ufw firewall blocks MAVLink from the X25 on {iface} UDP {udp}", fargs)
    elif s["tailscale"] and not _ufw_allows(rules, "tailscale0"):
        rep.check("cm5.firewall", False, "cm5.firewall.no_tailscale", "the ufw firewall blocks tailscale0")
    else:
        rep.check("cm5.firewall", True, "cm5.firewall.ok",
                  f"the ufw firewall lets MAVLink in on {iface} UDP {udp}" + (" and tailscale0" if s["tailscale"] else ""),
                  fargs)
    rep.end()


# ---- chạy ----------------------------------------------------------------------------------------------------------

def settings(mode):
    vps_host = env("VPS_PUBLIC_IP").strip()
    targets = tailscale_targets(env("CM5_EXTRA_UDP_TARGETS"))
    unit = router_unit()
    conf_path = os.environ.get("UAVLINK_ROUTER_CONF") or "/etc/mavlink-router/main.conf"
    vps_ip = resolve_ipv4(vps_host, wait("dns")) if vps_host else None
    endpoint = vps_endpoint(read_router_conf(conf_path), {vps_host, vps_ip}) if vps_host else None
    if mode == "auto":
        mode = "live" if unit and endpoint else "probe"
    return {
        "mode": mode, "router": unit, "vps_endpoint": endpoint, "vps_host": vps_host, "vps_ip": vps_ip,
        "x25_ip": env("X25_IP").strip(),
        "lan_port": env_port("MAVLINK_LAN_PORT", 14550),
        "cm5_tcp_port": env_port("CM5_TCP_SERVER_PORT", 5760),
        "eth_iface": env("CM5_ETH_IFACE", "eth0"),
        "uplink_port": env_port("VPS_UPLINK_PORT", 14555),
        "gcs_tcp_port": env_port("VPS_GCS_TCP_PORT", 5760),
        "gcs_udp_port": env_port("VPS_GCS_UDP_PORT", 14550),
        "signing": env("MAVLINK_SIGNING", "0") == "1",
        "ts_targets": targets,
        "tailscale": env("CM5_INSTALL_TAILSCALE", "0") == "1" or bool(targets),
        "internet_hosts": (os.environ.get("UAVLINK_INTERNET_HOSTS") or "1.1.1.1:443 8.8.8.8:443").split(),
    }


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Kiểm tra đường mạng X25 / CM5 / VPS / GCS (chỉ đọc)")
    ap.add_argument("--mode", choices=["auto", "live", "probe"], default="auto")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="S",
                    help="tổng thời gian cho cả lần chạy, giây (mặc định 25)")
    a = ap.parse_args(argv)
    if not 1 <= a.timeout <= 600:
        ap.error("--timeout phải trong khoảng 1..600")
    return a


def main(argv=None):
    global _scale
    a = parse_args(argv)
    _scale = a.timeout / DEFAULT_TIMEOUT
    CONFIG.clear()
    CONFIG.update(load_config())

    s = settings(a.mode)
    rep = Report()
    if s["mode"] == "live":
        log(f"Mode: live (router {s['router'] or 'not running'}, VPS endpoint {s['vps_endpoint'] or 'none'})")
        ev_info("net.mode.live", "live mode: only TCP connections to the VPS", {"unit": s["router"] or "-"})
    else:
        log("Mode: probe (this machine sends its own test packets to the VPS)")
        ev_info("net.mode.probe", "probe mode: this machine sends its own test packets to the VPS")

    x25_sysid = check_lan(rep, s)
    check_internet(rep, s)
    check_vps(rep, s, x25_sysid)
    check_gcs(rep, s)
    check_cm5(rep, s)

    log(f"\nNetwork: {rep.passed} passed, {rep.warned} warnings, {rep.failed} failed")
    ev(t="result", ok=rep.failed == 0,
       data={"passed": rep.passed, "warned": rep.warned, "failed": rep.failed, "mode": s["mode"]})
    return 3 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
