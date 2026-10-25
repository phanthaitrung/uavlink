"""`uavlink setup`: start the temporary setup server, print its URLs, open the desktop browser."""

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time

from . import paths

DEFAULT_PORT = 8090
SERVICE = "uavlink-setup.service"
WAIT_SECS = 15
IDLE_MINUTES = 30


# ---------------------------------------------------------------- pure helpers (unit tested)

def serve_env_text(listen, port):
    return "UAVLINK_LISTEN=%s\nUAVLINK_PORT=%d\n" % (listen, port)


def write_serve_env(listen, port, run_dir=None):
    run_dir = run_dir or paths.RUN_DIR
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "serve.env"
    tmp = run_dir / "serve.env.tmp"
    tmp.write_text(serve_env_text(listen, port), encoding="utf-8")
    os.replace(tmp, target)
    return target


def parse_ip_addr(text):
    """IPv4 addresses from `ip -4 -o addr show` output, in order, without duplicates."""
    found = []
    for line in text.splitlines():
        fields = line.split()
        if "inet" in fields:
            addr = fields[fields.index("inet") + 1].split("/")[0]
            if addr not in found:
                found.append(addr)
    return found


def is_loopback(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def is_tailscale(addr):
    try:
        return ipaddress.ip_address(addr) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def build_urls(port, token, lan, ips=(), ssh_connection=None, user="USER"):
    """Return the text lines of the access hint (Vietnamese first, then English)."""
    local = "http://127.0.0.1:%d/?t=%s" % (port, token)
    lines = ["", "uavlink — trang cài đặt / setup page", "-" * 38]
    lines.append("Mở trang cài đặt / Open the setup page:")
    lines.append("  " + local)
    if lan:
        for ip in ips:
            if is_loopback(ip):
                continue
            mark = "   (Tailscale)" if is_tailscale(ip) else ""
            lines.append("  http://%s:%d/?t=%s%s" % (ip, port, token, mark))
    else:
        parts = (ssh_connection or "").split()
        if len(parts) >= 3:
            lines.append("")
            lines.append("Từ máy khác qua SSH / From another computer over SSH:")
            lines.append("  ssh -L %d:127.0.0.1:%d %s@%s" % (port, port, user, parts[2]))
            lines.append("  rồi mở / then open  " + local)
    lines.append("")
    lines.append("Trang tự tắt sau %d phút không dùng. Mở lại: sudo uavlink setup" % IDLE_MINUTES)
    lines.append("The page stops by itself after %d minutes idle. Reopen: sudo uavlink setup" % IDLE_MINUTES)
    lines.append("")
    return lines


def parse_session_list(text):
    """Session ids from `loginctl list-sessions --no-legend`."""
    return [line.split()[0] for line in text.splitlines() if line.split()]


def parse_session_show(text):
    props = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def is_local_graphical(props):
    return (props.get("Type") in ("x11", "wayland") and props.get("Active") == "yes"
            and props.get("Remote") == "no")


def pick_graphical_session(show_outputs):
    """First active local graphical session among the `loginctl show-session` outputs, as a props dict."""
    for text in show_outputs:
        props = parse_session_show(text)
        if is_local_graphical(props):
            return props
    return None


# ---------------------------------------------------------------- system access

def _run(argv, timeout=10):
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return res


def _out(argv):
    res = _run(argv)
    return res.stdout if res is not None and res.returncode == 0 else ""


def local_ips():
    return parse_ip_addr(_out(["ip", "-4", "-o", "addr", "show"]))


def graphical_session():
    ids = parse_session_list(_out(["loginctl", "list-sessions", "--no-legend"]))
    shows = [_out(["loginctl", "show-session", sid, "-p", "Type", "-p", "Active", "-p", "Remote",
                   "-p", "Name", "-p", "User", "-p", "Display"]) for sid in ids]
    return pick_graphical_session(shows)


def open_in_desktop(url):
    """Open `url` in the active local graphical session. Returns (ok, note)."""
    session = graphical_session()
    if not session or not session.get("Name"):
        return False, "không có phiên đồ họa tại chỗ / no local graphical session"
    user, uid = session["Name"], session.get("User", "")
    res = _run(["systemd-run", "--user", "--machine=%s@" % user, "--quiet", "--collect", "xdg-open", url], 20)
    if res is not None and res.returncode == 0:
        return True, ""
    env = ["XDG_RUNTIME_DIR=/run/user/%s" % uid, "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%s/bus" % uid]
    if session.get("Display"):
        env.append("DISPLAY=" + session["Display"])
    res = _run(["runuser", "-u", user, "--", "env"] + env + ["xdg-open", url], 20)
    if res is not None and res.returncode == 0:
        return True, ""
    return False, "không mở được trình duyệt / could not open the browser"


def read_setup_json(run_dir=None):
    try:
        return json.loads((run_dir or paths.RUN_DIR).joinpath("setup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def wait_for_setup(expected_pid=None, timeout=WAIT_SECS, run_dir=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = read_setup_json(run_dir)
        if data and data.get("token") and (expected_pid is None or data.get("pid") == expected_pid):
            return data
        time.sleep(0.25)
    return None


def systemd_running():
    return os.path.isdir("/run/systemd/system")


def _main_pid():
    out = _out(["systemctl", "show", "-p", "MainPID", "--value", SERVICE]).strip()
    return int(out) if out.isdigit() and int(out) > 0 else None


def _ssh_user():
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "USER"


def report(data, lan, no_browser):
    port = int(data.get("port", DEFAULT_PORT))
    for line in build_urls(port, data["token"], lan, local_ips() if lan else (),
                           os.environ.get("SSH_CONNECTION"), _ssh_user()):
        print(line)
    if not no_browser:
        ok, note = open_in_desktop("http://127.0.0.1:%d/?t=%s" % (port, data["token"]))
        if not ok:
            print("Lưu ý / Note: " + note)


# ---------------------------------------------------------------- entry point

def main(argv=None):
    parser = argparse.ArgumentParser(prog="uavlink setup", description="Open the uavlink setup page.")
    parser.add_argument("--lan", action="store_true", help="listen on all interfaces / mở cho mạng LAN")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--foreground", action="store_true", help="run the server in this process")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--postinst", action="store_true", help="called from the package postinst")
    args = parser.parse_args(argv)

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("Cần quyền root. Chạy: sudo uavlink setup\nRoot is required. Run: sudo uavlink setup", file=sys.stderr)
        return 1

    listen = "0.0.0.0" if args.lan else "127.0.0.1"

    if args.foreground or not systemd_running():
        from . import server  # lazy: not needed for the service path
        os.environ["UAVLINK_LISTEN"] = listen
        os.environ["UAVLINK_PORT"] = str(args.port)
        try:
            (paths.RUN_DIR / "setup.json").unlink()
        except OSError:
            pass

        def announce():
            data = wait_for_setup()
            if data:
                report(data, args.lan, args.no_browser)
        threading.Thread(target=announce, daemon=True).start()
        return server.main([])

    write_serve_env(listen, args.port)
    try:
        (paths.RUN_DIR / "setup.json").unlink()
    except OSError:
        pass
    res = _run(["systemctl", "restart", SERVICE], 60)
    if res is None or res.returncode != 0:
        print("Không khởi động được dịch vụ / Could not start %s" % SERVICE, file=sys.stderr)
        return 1
    data = wait_for_setup(_main_pid())
    if not data:
        print("Dịch vụ chưa sẵn sàng sau %d giây / Service not ready after %d s. "
              "journalctl -u %s" % (WAIT_SECS, WAIT_SECS, SERVICE), file=sys.stderr)
        return 1
    report(data, args.lan, args.no_browser)
    return 0
