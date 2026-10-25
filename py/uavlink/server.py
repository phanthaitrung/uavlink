"""Temporary setup server (ARCHITECTURE.md section 6): token/cookie auth, JSON API, Server-Sent Events, static page.

It applies system configuration as root, so every request is checked: Host allow-list (DNS rebinding), cookie
(constant-time compare), Origin and content type for anything that is not a GET, and a small body limit.
"""

import argparse
import collections
import hmac
import ipaddress
import json
import mimetypes
import os
import queue
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs

from . import VERSION, config, detect, paths, presets, runner

DEFAULT_PORT = 8090
DEFAULT_IDLE_SECS = 1800
MAX_BODY = 64 * 1024
KEEPALIVE_SECS = 15
COOKIE_NAME = "uavlink"
SECRET_KEY = "MAVLINK_SIGNING_PASSPHRASE"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
TIMER = "uavlink-rollback.timer"


def _utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd, timeout=10):
    try:
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, text=True, errors="replace")
        return done.returncode, done.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


# ---- ufw --------------------------------------------------------------------------------------------------------

def ufw_active():
    rc, out = _run(["ufw", "status"])
    return rc == 0 and "Status: active" in out


def ufw_allow(port):
    _run(["ufw", "allow", "%d/tcp" % port, "comment", "uavlink-setup"])


def ufw_delete(port):
    _run(["ufw", "delete", "allow", "%d/tcp" % port])


# ---- helpers ----------------------------------------------------------------------------------------------------

def _roles():
    found = set()
    for spec in config.load_schema()["keys"].values():
        found.update(spec["roles"])
    return found


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _applied():
    return _read_json_file(os.path.join(str(paths.STATE_DIR), "applied.json"))


def _engine_i18n():
    merged = {}
    folder = paths.SHARE_DIR / "i18n"
    for p in sorted(folder.glob("engine-*.json")) if folder.is_dir() else []:
        data = _read_json_file(p)
        if data:
            merged.update(data)
    return merged


def _host_info():
    hostname = socket.gethostname()
    ips = []
    for iface in detect.detect().get("ifaces", []):
        for cidr in iface.get("ipv4", []):
            ip = cidr.split("/")[0]
            if ip not in ips:
                ips.append(ip)
    return {"hostname": hostname, "ips": ips}


def _host_name(header):
    """Host header without the port; None when empty."""
    header = (header or "").strip()
    if not header:
        return None
    if header.startswith("["):
        end = header.find("]")
        return header[1:end] if end > 0 else None
    if header.count(":") == 1:
        header = header.split(":", 1)[0]
    return header or None


def host_allowed(header):
    name = _host_name(header)
    if not name:
        return False
    if name.lower() == "localhost" or name.lower() == socket.gethostname().lower():
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _x25_backup_dir():
    return os.path.join(str(paths.STATE_DIR), "x25-backups")


def _x25_backups():
    folder = _x25_backup_dir()
    found = []
    try:
        names = sorted(os.listdir(folder), reverse=True)
    except OSError:
        return found
    for name in names:
        full = os.path.join(folder, name)
        if name.startswith("x25_params_") and name.endswith(".parm") and os.path.isfile(full):
            st = os.stat(full)
            found.append({"name": name, "file": full, "size": st.st_size,
                          "time": datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return found


class ApiError(Exception):
    def __init__(self, status, code, detail="", **extra):
        super().__init__(code)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra


# ---- server -----------------------------------------------------------------------------------------------------

class SetupServer:
    """The HTTP server plus its state. `serve()` blocks until idle, shutdown request or `stop()`."""

    def __init__(self, listen="127.0.0.1", port=DEFAULT_PORT, idle_secs=DEFAULT_IDLE_SECS):
        self.listen = listen
        self.idle_secs = idle_secs
        self.token = secrets.token_urlsafe(18)
        self.started = _utc_iso()
        self.stopped = threading.Event()
        self.last_activity = time.monotonic()
        self.streams = 0
        self.jobs = collections.deque(maxlen=runner.KEEP_JOBS)
        self.lock = threading.Lock()
        self.allow_wsl = os.environ.get("UAVLINK_ALLOW_WSL") == "1"
        self.open_firewall = listen not in LOOPBACK
        self.fw_open = False
        family = socket.AF_INET6 if ":" in listen else socket.AF_INET
        cls = type("SetupHTTPServer", (ThreadingHTTPServer,), {"address_family": family, "daemon_threads": True})
        self.httpd = cls((listen, port), _make_handler(self))
        self.port = self.httpd.server_address[1]

    # -- state --

    def touch(self):
        self.last_activity = time.monotonic()

    def preview_only(self):
        return detect.is_wsl() and not self.allow_wsl

    def track(self, job):
        """Remember the job, remove its temporary config dir and re-open the firewall port when it ends."""
        with self.lock:
            self.jobs.append(job)
        return job

    def after_job(self, job, cleanup=None):
        def watch():
            job.wait()
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)
            if self.fw_open:
                ufw_allow(self.port)           # the engine may have reset ufw
        threading.Thread(target=watch, name="after-" + job.id, daemon=True).start()

    def find_job(self, job_id):
        with self.lock:
            for job in self.jobs:
                if job.id == job_id:
                    return job
        return None

    def last_job(self, kind):
        with self.lock:
            for job in reversed(self.jobs):
                if job.kind == kind:
                    return job
        return None

    # -- lifecycle --

    def write_setup_json(self):
        os.makedirs(str(paths.RUN_DIR), exist_ok=True)
        target = os.path.join(str(paths.RUN_DIR), "setup.json")
        tmp = target + ".tmp"
        data = {"listen": self.listen, "port": self.port, "token": self.token, "pid": os.getpid(),
                "started": self.started}
        try:
            os.unlink(tmp)
        except OSError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.write("\n")
        os.replace(tmp, target)

    def remove_setup_json(self):
        try:
            os.unlink(os.path.join(str(paths.RUN_DIR), "setup.json"))
        except OSError:
            pass

    def stop(self):
        self.stopped.set()

    def serve(self):
        self.write_setup_json()
        if self.open_firewall and ufw_active():
            ufw_allow(self.port)
            self.fw_open = True
        thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                  name="http", daemon=True)
        thread.start()
        try:
            while not self.stopped.wait(0.2):
                if self.idle_secs is not None and self._idle():
                    break
        finally:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.remove_setup_json()
            if self.fw_open:
                ufw_delete(self.port)
                self.fw_open = False

    def _idle(self):
        if self.streams or runner.current_job() is not None:
            self.touch()
            return False
        return time.monotonic() - self.last_activity > self.idle_secs


# ---- request handler --------------------------------------------------------------------------------------------

def _make_handler(srv):
    class Handler(BaseHTTPRequestHandler):
        server_version = "uavlink-setup"
        sys_version = ""
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):           # the URL can carry the token
            pass

        # -- output --

        def _headers(self, status, ctype, length, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if self.path.startswith("/api/"):
                self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _send(self, status, body, ctype, extra=None):
            self._headers(status, ctype, len(body), extra)
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status, obj, extra=None):
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _error(self, status, code, detail="", **extra):
            self._json(status, {"error": code, "detail": detail, **extra})

        # -- checks --

        def _cookie_ok(self):
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                return False
            morsel = jar.get(COOKIE_NAME)
            return bool(morsel) and hmac.compare_digest(morsel.value.encode("utf-8"), srv.token.encode("utf-8"))

        def _body(self):
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                raise ApiError(411, "length_required")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError(400, "bad_request", "bad Content-Length")
            if length < 0:
                raise ApiError(400, "bad_request", "bad Content-Length")
            if length > MAX_BODY:
                raise ApiError(413, "too_large", "body over %d bytes" % MAX_BODY)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                raise ApiError(400, "bad_request", "body is not JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "bad_request", "body must be a JSON object")
            return data

        # -- dispatch --

        def do_GET(self):
            self._handle(False)

        def do_POST(self):
            self._handle(True)

        def do_HEAD(self):
            self._handle(False)

        def do_PUT(self):
            self._handle(True)

        do_DELETE = do_PATCH = do_PUT

        def _handle(self, mutating):
            srv.touch()
            try:
                if not host_allowed(self.headers.get("Host")):
                    raise ApiError(421, "host", "unexpected Host header")
                url = urlsplit(self.path)
                path = url.path
                if path.startswith("/api/") or path == "/api":
                    self._api(path, mutating)
                elif mutating:
                    raise ApiError(405, "method", "only GET is allowed here")
                else:
                    self._page(path, parse_qs(url.query))
            except ApiError as e:
                self._error(e.status, e.code, e.detail, **e.extra)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:                                    # never leave the client hanging
                try:
                    self._error(500, "internal", str(e))
                except OSError:
                    pass
            finally:
                srv.touch()

        # -- static page --

        def _page(self, path, query):
            if path == "/" and "t" in query:
                if hmac.compare_digest(query["t"][0].encode("utf-8"), srv.token.encode("utf-8")):
                    self._headers(302, "text/plain", 0, {
                        "Location": "/",
                        "Set-Cookie": "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (COOKIE_NAME, srv.token)})
                    return
                raise ApiError(401, "auth", "bad token")
            rel = unquote(path).lstrip("/") or "index.html"
            if "\x00" in rel or "\\" in rel or ".." in rel.split("/"):
                raise ApiError(404, "not_found")
            root = os.path.realpath(str(paths.WEB_DIR))
            full = os.path.realpath(os.path.join(root, rel))
            if os.path.commonpath([root, full]) != root or not os.path.isfile(full):
                raise ApiError(404, "not_found")
            with open(full, "rb") as f:
                body = f.read()
            ext = os.path.splitext(full)[1].lower()
            self._send(200, body, CONTENT_TYPES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream")

        # -- API --

        def _api(self, path, mutating):
            if not self._cookie_ok():
                raise ApiError(401, "auth")
            if mutating:
                if self.headers.get("Origin") != "http://" + (self.headers.get("Host") or ""):
                    raise ApiError(403, "origin", "bad Origin")
                ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    raise ApiError(415, "content_type", "Content-Type must be application/json")
            parts = [p for p in path.split("/") if p][1:]
            method = self.command
            route = (method, parts[0] if parts else "")
            if method == "GET" and parts[:1] == ["jobs"] and len(parts) in (2, 3):
                if len(parts) == 3 and parts[2] != "stream":
                    raise ApiError(404, "not_found")
                return self._job(parts[1], stream=len(parts) == 3)
            if method == "GET" and parts[:1] == ["secret"] and len(parts) == 2:
                return self._secret(parts[1])
            if method == "GET" and parts == ["x25", "backups"]:
                return self._json(200, {"backups": _x25_backups()})
            if method == "POST" and parts == ["x25", "restore"]:
                return self._x25_restore(self._body())
            handlers = {("GET", "state"): self._state, ("GET", "status"): self._status}
            posts = {"validate": self._validate, "precheck": self._precheck, "apply": self._apply,
                     "confirm": self._confirm, "verify": self._verify, "shutdown": self._shutdown}
            if len(parts) == 1 and route in handlers:
                return handlers[route]()
            if len(parts) == 1 and method == "POST" and parts[0] in posts:
                return posts[parts[0]](self._body())
            raise ApiError(404, "not_found")

        def _state(self):
            values, present = config.load_current()
            applied = _applied() or {}
            offered = presets.load_presets()
            running = detect.running_preset()
            if running:
                offered = [running] + [p for p in offered if p.get("id") != running["id"]]
            info = detect.detect()
            self._json(200, {
                "version": VERSION, "is_wsl": detect.is_wsl(), "preview_only": srv.preview_only(),
                "role_guess": detect.role_guess(), "host": _host_info(), "detect": info,
                "schema": config.load_schema(), "presets": offered,
                "current": {"values": values, "secrets_present": present,
                            "applied_at": applied.get("time"),
                            "preset": applied.get("preset") or values.get("UAVLINK_PRESET", "")},
                "i18n": {"engine": _engine_i18n()}})

        def _role(self, data):
            role = data.get("role")
            if role not in _roles():
                raise ApiError(400, "bad_request", "unknown role")
            return role

        @staticmethod
        def _str_dict(data, key):
            value = data.get(key) or {}
            if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
                raise ApiError(400, "bad_request", "%s must be an object" % key)
            return {k: ("" if v is None else str(v)) for k, v in value.items()}

        def _validate(self, data):
            role = self._role(data)
            values = config.defaults(role)
            values.update(self._str_dict(data, "values"))
            errors = config.validate(role, values)
            self._json(200, {"ok": not errors, "errors": errors})

        def _start_job(self, starter):
            try:
                job = starter()
            except runner.Busy:
                raise ApiError(409, "busy", "another job is running")
            srv.track(job)
            return job

        def _precheck(self, data):
            role = self._role(data)
            values = self._str_dict(data, "values")
            if runner.current_job() is not None:
                raise ApiError(409, "busy", "another job is running")
            tmp = tempfile.mkdtemp(prefix="uavlink-precheck-")
            os.chmod(tmp, 0o700)
            try:
                try:
                    config.save(role, data.get("preset") if isinstance(data.get("preset"), str) else "",
                                values, {}, config_dir=tmp)
                except config.ConfigError as e:
                    raise ApiError(400, "invalid", str(e), errors=e.errors)
                job = self._start_job(lambda: runner.precheck(role, tmp))
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            srv.after_job(job, cleanup=tmp)
            self._json(200, {"job": job.id})

        def _apply(self, data):
            role = self._role(data)
            values = self._str_dict(data, "values")
            secret_values = self._str_dict(data, "secrets")
            preset = data.get("preset") if isinstance(data.get("preset"), str) else ""
            if srv.preview_only():
                raise ApiError(423, "preview_only", "this machine only supports a preview")
            if runner.current_job() is not None:
                raise ApiError(409, "busy", "another job is running")
            try:
                config.save(role, preset, values, secret_values)
            except config.ConfigError as e:
                raise ApiError(400, "invalid", str(e), errors=e.errors)
            job = self._start_job(lambda: runner.apply(role, props_off=data.get("props_off") is True))
            srv.after_job(job)
            self._json(200, {"job": job.id})

        def _verify(self, data):
            job = self._start_job(lambda: runner.verify(data.get("role") if data.get("role") in _roles() else "cm5"))
            srv.after_job(job)
            self._json(200, {"job": job.id})

        def _x25_restore(self, data):
            if srv.preview_only():
                raise ApiError(423, "preview_only", "this machine only supports a preview")
            wanted = data.get("file")
            if not isinstance(wanted, str):
                raise ApiError(400, "bad_request", "file is required")
            match = [b for b in _x25_backups() if wanted in (b["name"], b["file"])]
            if not match:
                raise ApiError(404, "not_found", "no such backup")
            job = self._start_job(lambda: runner.x25_restore(match[0]["file"]))
            srv.after_job(job)
            self._json(200, {"job": job.id})

        def _confirm(self, data):
            if srv.preview_only():
                raise ApiError(423, "preview_only", "this machine only supports a preview")
            rc, _ = _run(["systemctl", "stop", TIMER])
            self._json(200, {"ok": rc == 0})

        def _status(self):
            names = list(getattr(detect, "ROUTER_SERVICES", ())) + [TIMER]
            services = {}
            for name in names:
                rc, out = _run(["systemctl", "is-active", name], timeout=5)
                services[name] = out.strip() or "unknown"
            verify = srv.last_job("verify")
            last = None if verify is None else {"id": verify.id, "state": verify.state, "ended": verify.ended}
            self._json(200, {"services": services, "applied": _applied(), "last_verify": last})

        def _secret(self, key):
            if key != SECRET_KEY or _applied() is None:
                raise ApiError(404, "not_found")
            value = config.read_env(os.path.join(str(paths.CONFIG_DIR), "secrets.env")).get(key)
            if not value:
                raise ApiError(404, "not_found")
            self._json(200, {"value": value})

        def _shutdown(self, data):
            self._json(200, {"ok": True})
            srv.stop()

        def _job(self, job_id, stream):
            job = srv.find_job(job_id)
            if job is None:
                raise ApiError(404, "not_found", "unknown job")
            if not stream:
                return self._json(200, job.to_dict())
            self._stream(job)

        def _stream(self, job):
            events = queue.Queue()
            job.subscribe(events.put)
            self._headers(200, "text/event-stream; charset=utf-8", None,
                          {"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
            with srv.lock:
                srv.streams += 1
            try:
                while True:
                    try:
                        ev = events.get(timeout=KEEPALIVE_SECS)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        srv.touch()
                        continue
                    self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    if ev.get("t") == "end":
                        state = json.dumps({"state": ev.get("state")})
                        self.wfile.write(("event: end\ndata: %s\n\n" % state).encode("utf-8"))
                        self.wfile.flush()
                        return
                    self.wfile.flush()
                    srv.touch()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with srv.lock:
                    srv.streams -= 1

    return Handler


# ---- entry point ------------------------------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="uavlink serve", description="Temporary setup server.")
    parser.add_argument("--listen", default=os.environ.get("UAVLINK_LISTEN") or "127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("UAVLINK_PORT") or DEFAULT_PORT))
    parser.add_argument("--idle", type=int, default=int(os.environ.get("UAVLINK_IDLE_SECS") or DEFAULT_IDLE_SECS),
                        help="seconds without requests before the server exits")
    args = parser.parse_args(argv)
    try:
        srv = SetupServer(args.listen, args.port, args.idle)
    except OSError as e:
        print("uavlink serve: cannot listen on %s:%d: %s" % (args.listen, args.port, e), file=sys.stderr)
        return 1
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: srv.stop())
        signal.signal(signal.SIGINT, lambda *_: srv.stop())
    srv.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
