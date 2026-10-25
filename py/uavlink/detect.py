"""Reads the live system: interfaces, X25 on USB, Tailscale, Wi-Fi, router config, WSL (ARCHITECTURE.md section 5).

Every probe has a timeout and never raises. `root` is a filesystem prefix (default `/`) so tests can point at fixture
trees; external commands go through `_run`, which tests stub.
"""

import glob
import json
import os
import platform
import re
import socket
import subprocess

CMD_TIMEOUT = 5
ROUTER_SERVICES = ("uavlink-router", "mavlink-router")
ETH_CON_NAME = "x25-link"
_SESSION_TYPES = ("x11", "wayland", "mir")


def _run(cmd, timeout=CMD_TIMEOUT):
    """Run a command and return (exit code, stdout). 127 = not found, 124 = timeout, 126 = other failure."""
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, env=dict(os.environ, LC_ALL="C"))
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    except Exception:
        return 126, ""
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _safe(fn, default, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return default


def _p(root, path):
    """`path` (absolute) below the prefix `root`."""
    root = str(root)
    if root in ("", "/"):
        return path
    return os.path.join(root, path.lstrip("/"))


def _read(root, path, default=None):
    try:
        with open(_p(root, path), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def _json(text, default=None):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _is_real_root(root):
    return str(root) in ("", "/")


# ---- simple facts ---------------------------------------------------------------------------------------------

def is_wsl(root="/"):
    release = (_read(root, "/proc/sys/kernel/osrelease") or "").lower()
    if "microsoft" in release or "wsl" in release:
        return True
    return _is_real_root(root) and bool(os.environ.get("WSL_DISTRO_NAME"))


def _model(root):
    text = _read(root, "/proc/device-tree/model")
    return text.replace("\x00", "").strip() if text else ""


def _hostname(root):
    if _is_real_root(root):
        return socket.gethostname()
    return (_read(root, "/etc/hostname") or "").strip()


def x25_ports(root="/"):
    """USB serial ports of ArduPilot boards, as paths without the `root` prefix."""
    found = sorted(glob.glob(_p(root, "/dev/serial/by-id/*ArduPilot*")))
    prefix = "" if _is_real_root(root) else str(root).rstrip("/")
    return [p[len(prefix):] if prefix and p.startswith(prefix) else p for p in found]


def role_guess(root="/"):
    if _safe(x25_ports, [], root) or "raspberry pi" in _safe(_model, "", root).lower():
        return "cm5"
    return "vps"


# ---- network --------------------------------------------------------------------------------------------------

def _sys_int(root, path):
    text = _read(root, path)
    try:
        return int(text.strip())
    except (AttributeError, ValueError):
        return None


def _default_route_iface():
    rc, out = _run(["ip", "-j", "-4", "route", "show", "default"])
    routes = _json(out, []) if rc == 0 else []
    for route in routes if isinstance(routes, list) else []:
        if route.get("dev"):
            return route["dev"]
    return None


def _ifaces(root, default_iface):
    rc, out = _run(["ip", "-j", "addr", "show"])
    links = _json(out, []) if rc == 0 else []
    result = []
    for link in links if isinstance(links, list) else []:
        name = link.get("ifname")
        if not name or name == "lo":
            continue
        sysdir = f"/sys/class/net/{name}"
        carrier = _sys_int(root, f"{sysdir}/carrier")
        speed = _sys_int(root, f"{sysdir}/speed")
        wireless = os.path.isdir(_p(root, f"{sysdir}/wireless")) or os.path.isdir(_p(root, f"{sysdir}/phy80211"))
        ipv4 = [f"{a['local']}/{a['prefixlen']}" for a in link.get("addr_info", [])
                if a.get("family") == "inet" and a.get("local")]
        result.append({
            "name": name,
            "mac": link.get("address") or "",
            "carrier": None if carrier is None else carrier == 1,
            "speed": speed if speed and speed > 0 else None,
            "ipv4": ipv4,
            "is_default_route": name == default_iface,
            "wireless": wireless,
        })
    return result


def _tailscale():
    rc, out = _run(["tailscale", "ip", "-4"])
    ip = out.split()[0] if rc == 0 and out.split() else None
    up = bool(ip)
    rc, out = _run(["tailscale", "status", "--json"])
    state = (_json(out, {}) or {}).get("BackendState") if rc == 0 else None
    if state:
        up = state == "Running"
    return {"ip": ip, "up": up}


def _split_nmcli(line):
    """Fields of a `nmcli -t` line (`:` separated, `\\:` and `\\\\` escaped)."""
    fields, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            fields.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    fields.append(cur)
    return fields


def _wifi(ifaces):
    wifi_ifaces = [i["name"] for i in ifaces if i["wireless"]]
    iface, ssid = (wifi_ifaces[0] if wifi_ifaces else None), None
    rc, out = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,DEVICE", "device", "wifi", "list"])
    if rc == 0:
        for line in out.splitlines():
            fields = _split_nmcli(line)
            if len(fields) >= 3 and fields[0] == "yes":
                ssid, iface = fields[1] or None, fields[2] or iface
                break
    return {"iface": iface, "ssid": ssid}


# ---- services and session -------------------------------------------------------------------------------------

def _service_state(name):
    rc, out = _run(["systemctl", "is-active", name])
    state = out.strip()
    return state if state else "unknown"


def _router(root):
    conf = "/etc/mavlink-router/main.conf"
    return {
        "conf_path": conf,
        "exists": os.path.isfile(_p(root, conf)),
        "service": {name: _service_state(name) for name in ROUTER_SERVICES},
    }


def _desktop_user():
    """User owning the active local graphical session, else None."""
    rc, out = _run(["loginctl", "list-sessions", "--no-legend"])
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        rc, info = _run(["loginctl", "show-session", parts[0], "-p", "Name", "-p", "Type", "-p", "Active",
                         "-p", "Remote"])
        props = dict(l.split("=", 1) for l in info.splitlines() if "=" in l)
        if (rc == 0 and props.get("Active") == "yes" and props.get("Remote") != "yes"
                and props.get("Type") in _SESSION_TYPES and props.get("Name")):
            return props["Name"]
    return None


def _ssh_session():
    conn = os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT") or ""
    if not conn and not os.environ.get("SSH_TTY"):
        return None
    return {"client": conn.split()[0] if conn.split() else None}


def detect(root="/"):
    ifaces_default = _safe(_default_route_iface, None)
    ifaces = _safe(_ifaces, [], root, ifaces_default)
    return {
        "is_wsl": _safe(is_wsl, False, root),
        "arch": platform.machine(),
        "model": _safe(_model, "", root),
        "hostname": _safe(_hostname, "", root),
        "ifaces": ifaces,
        "default_route_iface": ifaces_default,
        "x25_ports": _safe(x25_ports, [], root),
        "tailscale": _safe(_tailscale, {"ip": None, "up": False}),
        "wifi": _safe(_wifi, {"iface": None, "ssid": None}, ifaces),
        "router": _safe(_router, {"conf_path": "/etc/mavlink-router/main.conf", "exists": False,
                                  "service": {n: "unknown" for n in ROUTER_SERVICES}}, root),
        "desktop_user": _safe(_desktop_user, None),
        "ssh_session": _safe(_ssh_session, None),
    }


# ---- running configuration ------------------------------------------------------------------------------------

def parse_router_conf(text):
    """mavlink-router ini: {"General": {k: v}, "UdpEndpoint name": {k: v}, …} (ordered)."""
    sections, cur = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            cur = sections.setdefault(m.group(1).strip(), {})
            continue
        if "=" in line and cur is not None:
            key, value = line.split("=", 1)
            cur[key.strip()] = value.strip()
    return sections


def _router_values(text):
    values = {}
    sections = parse_router_conf(text)
    general = sections.get("General", {})
    for key, name in (("TcpServerPort", "CM5_TCP_SERVER_PORT"), ("ReportStats", "ROUTER_REPORT_STATS"),
                      ("Log", "CM5_ROUTER_LOG_DIR"), ("LogMode", "CM5_ROUTER_LOG_MODE")):
        if key in general:
            values[name] = general[key]
    if "General" in sections and "Log" not in general:
        values["CM5_ROUTER_LOG_DIR"] = ""
    extra = []
    for title, body in sections.items():
        kind, _, name = title.partition(" ")
        if kind != "UdpEndpoint" or not name:
            continue
        mode = body.get("Mode", "").lower()
        if name == "x25_ethernet" and mode == "server":
            if "Port" in body:
                values["MAVLINK_LAN_PORT"] = body["Port"]
        elif name == "vps_mavlink" and mode == "normal":
            if "Address" in body:
                values["VPS_PUBLIC_IP"] = body["Address"]
            if "Port" in body:
                values["VPS_UPLINK_PORT"] = body["Port"]
        elif mode == "normal" and "Address" in body and "Port" in body:
            extra.append(f"{name}={body['Address']}:{body['Port']}")
    if sections:
        if "vps_mavlink" not in {t.partition(" ")[2] for t in sections}:
            values["VPS_PUBLIC_IP"] = ""
        values["CM5_EXTRA_UDP_TARGETS"] = " ".join(extra)
    return values


def _apply_addresses(values, addrs):
    """addrs: list of 'ip/prefix'. The first is the LAN address, the others are extras."""
    if not addrs:
        return
    ip, _, prefix = addrs[0].partition("/")
    values["CM5_LAN_IP"] = ip
    if prefix.isdigit():
        values["X25_NETMASK"] = prefix
    values["CM5_ETH_EXTRA_ADDRS"] = " ".join(addrs[1:])


def _nm_values(root, con_name=ETH_CON_NAME):
    """Keyfile /etc/NetworkManager/system-connections/<name>.nmconnection."""
    text = _read(root, f"/etc/NetworkManager/system-connections/{con_name}.nmconnection")
    if text is None:
        return None
    values, section, addrs = {"CM5_ETH_CON_NAME": con_name}, "", []
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            section = m.group(1)
            continue
        if "=" not in line:
            continue
        key, value = (s.strip() for s in line.split("=", 1))
        if section == "connection" and key == "interface-name":
            values["CM5_ETH_IFACE"] = value
        elif section == "ipv4" and re.match(r"^address\d+$", key):
            addrs.append((int(key[7:]), value.split(",")[0].strip()))
    _apply_addresses(values, [a for _, a in sorted(addrs)])
    return values


def _netplan_values(root):
    text = _read(root, "/etc/netplan/60-x25-link.yaml")
    if text is None:
        return None
    values, addrs, in_addrs, base_indent = {}, [], False, 0
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if in_addrs:
            if line.startswith("- ") and indent >= base_indent:
                addrs.append(line[2:].strip().strip("'\""))
                continue
            in_addrs = False
        if line == "addresses:":
            in_addrs, base_indent = True, indent
        elif line.endswith(":") and indent == 4 and "CM5_ETH_IFACE" not in values:
            values["CM5_ETH_IFACE"] = line[:-1]
    _apply_addresses(values, addrs)
    return values


def running_preset(root="/"):
    """A preset-shaped dict with the values found on this machine, or None when there is nothing to read."""
    values = {}
    conf = _safe(_read, None, root, "/etc/mavlink-router/main.conf")
    if conf:
        values.update(_safe(_router_values, {}, conf))
    net = _safe(_nm_values, None, root)
    if net is None:
        net = _safe(_netplan_values, None, root)
    if net:
        values.update(net)
    if not values:
        return None
    return {
        "id": "running",
        "role": "cm5",
        "name": {"vi": "Cấu hình đang chạy", "en": "Running configuration"},
        "desc": {"vi": "Giá trị đọc từ máy này (main.conf của mavlink-router và kết nối mạng x25-link).",
                 "en": "Values read from this machine (the mavlink-router main.conf and the x25-link network connection)."},
        "values": values,
    }
