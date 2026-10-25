"""Runs engine commands as jobs and parses their progress events (ARCHITECTURE.md sections 4.1 and 5).

The runner neither reads nor writes the configuration: callers save it first. It only reads `secrets.env` of the
config dir to keep those values out of the log and the events.
"""

import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from . import VERSION, detect, paths

EVENT_PREFIX = "@@UAVLINK "
MIN_SECRET_LEN = 4          # shorter values would mangle ordinary text when redacted
KEEP_JOBS = 20

_registry = []              # newest last
_registry_lock = threading.Lock()


class Busy(Exception):
    """Another job is running."""


def _utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- secrets --------------------------------------------------------------------------------------------------

def _unquote(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("'\\''", "'")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def read_secret_values(config_dir):
    """Values in `<config_dir>/secrets.env` (small local reader, never executes shell)."""
    values = set()
    try:
        with open(os.path.join(str(config_dir), "secrets.env"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        raw = line.split("=", 1)[1].strip()
        for value in (_unquote(raw), raw):
            if len(value) >= MIN_SECRET_LEN:
                values.add(value)
    return values


def _redactor(secret_values):
    ordered = sorted(secret_values, key=len, reverse=True)

    def text(s):
        for v in ordered:
            if v in s:
                s = s.replace(v, "***")
        return s

    def deep(o):
        if isinstance(o, str):
            return text(o)
        if isinstance(o, list):
            return [deep(x) for x in o]
        if isinstance(o, dict):
            return {deep(k): deep(v) for k, v in o.items()}
        return o

    return text, deep


# ---- job ------------------------------------------------------------------------------------------------------

class Stage:
    def __init__(self, id, cmd=None, skip=None, then=()):
        self.id = id
        self.cmd = cmd              # argv list
        self.then = list(then)      # more argv lists, run after `cmd` even when it failed
        self.skip = skip            # None or {"code": …, "msg": …}


class Job:
    """Runs `stages` in sequence. States: `new`, `running`, then `ok` or `failed`."""

    def __init__(self, kind, stages, config_dir=None, stop_on_fail=True, fail=None, on_ok=None):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.state = "new"
        self.events = []
        self.started = None
        self.ended = None
        self.stages = list(stages)
        self.config_dir = config_dir
        self.stop_on_fail = stop_on_fail
        self._fail = fail           # {"code", "msg"}: fail at once
        self._on_ok = on_ok
        self._lock = threading.RLock()
        self._subs = []
        self._done = threading.Event()
        self._proc = None
        self._cancelled = False
        self._lock_fd = None
        self._log = None
        self._thread = None
        self._redact_text = self._redact_deep = lambda x: x

    # -- public --

    def start(self):
        with self._lock:
            if self.state != "new":
                raise RuntimeError("job already started")
            self.state = "running"
            self.started = _utc_iso()
        if self._fail:
            self._register()
            self._open_log()
            self._finish_failed_at_once()
            return self
        self._acquire_lock()
        try:
            self._redact_text, self._redact_deep = _redactor(
                read_secret_values(self.config_dir or paths.CONFIG_DIR))
            self._open_log()
            self._register()
            self._thread = threading.Thread(target=self._run, name=f"job-{self.id}", daemon=True)
            self._thread.start()
        except BaseException:
            self._release_lock()
            raise
        return self

    def wait(self, timeout=None):
        """Block until the job ends. True when it ended, False on timeout."""
        return self._done.wait(timeout)

    def subscribe(self, callback):
        """Calls `callback(event)` for every past event, then for each new one."""
        with self._lock:
            for ev in list(self.events):
                self._call(callback, ev)
            self._subs.append(callback)

    def cancel(self):
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill_group(proc, signal.SIGTERM)
            killer = threading.Timer(5, _kill_group, (proc, signal.SIGKILL))
            killer.daemon = True
            killer.start()

    def to_dict(self):
        with self._lock:
            return {"id": self.id, "kind": self.kind, "state": self.state, "started": self.started,
                    "ended": self.ended, "events": list(self.events)}

    # -- internals --

    def _register(self):
        with _registry_lock:
            _registry.append(self)
            del _registry[:-KEEP_JOBS]

    @staticmethod
    def _call(callback, ev):
        try:
            callback(ev)
        except Exception:
            pass

    def _acquire_lock(self):
        run_dir = str(paths.RUN_DIR)
        os.makedirs(run_dir, exist_ok=True)
        fd = os.open(os.path.join(run_dir, "job.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            with self._lock:
                self.state = "new"
                self.started = None
            raise Busy("another job is running")
        self._lock_fd = fd

    def _release_lock(self):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def _open_log(self):
        try:
            log_dir = os.path.join(str(paths.STATE_DIR), "logs")
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self._log = open(os.path.join(log_dir, f"{stamp}-{self.kind}.log"), "a", encoding="utf-8")
        except OSError:
            self._log = None

    def _write_log(self, line):
        if self._log:
            try:
                self._log.write(line + "\n")
                self._log.flush()
            except OSError:
                pass

    def _emit(self, ev):
        ev = self._redact_deep(ev)
        with self._lock:
            self.events.append(ev)
            for cb in list(self._subs):
                self._call(cb, ev)

    def _stage_event(self, stage, state, **extra):
        self._write_log(f"== stage {stage.id} {state}" + (f" ({extra['code']})" if extra.get("code") else ""))
        self._emit({"t": "stage", "id": stage.id, "state": state, **extra})

    def _finish(self, ok, **extra):
        state = "ok" if ok else "failed"
        if ok and self._on_ok:
            try:
                self._on_ok()
            except Exception as e:           # a failed bookkeeping write must not hide a good apply
                self._emit({"t": "log", "line": f"warning: {e}"})
        self._emit({"t": "end", "state": state, **extra})
        with self._lock:
            self.state = state
            self.ended = _utc_iso()
        if self._log:
            self._log.close()
            self._log = None
        self._release_lock()
        self._done.set()

    def _finish_failed_at_once(self):
        stage = Stage("role")
        self._stage_event(stage, "fail", code=self._fail["code"], msg=self._fail["msg"])
        self._finish(False)

    def _run(self):
        ok = True
        try:
            for stage in self.stages:
                if self._cancelled:
                    break
                if stage.skip:
                    self._stage_event(stage, "skip", **stage.skip)
                    continue
                if not ok and self.stop_on_fail:
                    self._stage_event(stage, "skip", code="stage.not_run", msg="an earlier stage failed")
                    continue
                self._stage_event(stage, "start")
                code = self._run_stage(stage)
                if code == 0 and not self._cancelled:
                    self._stage_event(stage, "ok")
                else:
                    ok = False
                    self._stage_event(stage, "fail", exit=code)
                    if self._cancelled:
                        break
        except Exception as e:
            ok = False
            self._emit({"t": "log", "line": f"runner error: {e}"})
        if self._cancelled:
            self._finish(False, reason="cancelled")
        else:
            self._finish(ok)

    def _engine_env(self):
        env = dict(os.environ)
        env.update({
            "UAVLINK_EVENTS": "1",
            "UAVLINK_CONFIG_DIR": str(self.config_dir or paths.CONFIG_DIR),
            "UAVLINK_STATE_DIR": str(paths.STATE_DIR),
            "PYTHONPATH": str(paths.VENDOR_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LC_ALL": "C.UTF-8",
        })
        return env

    def _run_stage(self, stage):
        """Runs the stage's commands in order; the exit code is the first non-zero one."""
        result = 0
        for cmd in [stage.cmd] + stage.then:
            if self._cancelled:
                break
            code = self._run_cmd(cmd)
            result = result or code
        return result

    def _run_cmd(self, cmd):
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=self._engine_env(), start_new_session=True)
        except OSError as e:
            self._emit({"t": "log", "line": f"cannot start {cmd[0]}: {e}"})
            return 127
        self._proc = proc
        if self._cancelled:                  # cancel() raced with the start
            _kill_group(proc, signal.SIGTERM)
        with proc.stdout:
            for raw in iter(proc.stdout.readline, b""):
                self._handle_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
        code = proc.wait()
        self._proc = None
        return code

    def _handle_line(self, line):
        line = self._redact_text(line)
        self._write_log(line)
        if line.startswith(EVENT_PREFIX):
            try:
                ev = json.loads(line[len(EVENT_PREFIX):])
            except ValueError:
                ev = None
            if isinstance(ev, dict) and isinstance(ev.get("t"), str):
                self._emit(ev)
                return
        self._emit({"t": "log", "line": line})


def _kill_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


# ---- pipelines ------------------------------------------------------------------------------------------------

def _vps_not_ready():
    return {"code": "role.vps_not_ready", "msg": "the vps role is not available in this version"}


def _sh(*parts):
    return ["bash", str(paths.ENGINE_DIR.joinpath(*parts))]


def _py(*parts):
    return [sys.executable, str(paths.ENGINE_DIR.joinpath(*parts))]


def _start(job):
    return job.start()


def precheck(role, config_dir):
    if role == "vps":
        return _start(Job("precheck", [], config_dir, fail=_vps_not_ready()))
    stages = [Stage("cm5", _sh("cm5", "install-cm5.sh") + ["--preview"]),
              Stage("x25", _py("x25", "x25_configure.py") + ["--preview"]),
              Stage("net", _py("tools", "net_probe.py") + ["--mode", "auto"])]
    return _start(Job("precheck", stages, config_dir, stop_on_fail=False))


def _verify_stage():
    return Stage("verify", _sh("tools", "verify-cm5.sh"), then=[_py("tools", "net_probe.py") + ["--mode", "live"]])


def _applied_writer(role, config_dir):
    def write():
        values = {}
        try:
            with open(os.path.join(str(config_dir or paths.CONFIG_DIR), "config.env"),
                      encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("UAVLINK_PRESET="):
                        values["preset"] = _unquote(line.split("=", 1)[1])
        except OSError:
            pass
        data = {"preset": values.get("preset", ""), "role": role, "time": _utc_iso(), "version": VERSION}
        os.makedirs(str(paths.STATE_DIR), exist_ok=True)
        target = os.path.join(str(paths.STATE_DIR), "applied.json")
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, target)
    return write


def apply(role, config_dir=None, props_off=False):
    if role == "vps":
        return _start(Job("apply", [], config_dir, fail=_vps_not_ready()))
    skip = None
    if not props_off:
        skip = {"code": "x25.props_not_off", "msg": "propellers not confirmed removed, flight controller untouched"}
    elif not detect.x25_ports():
        skip = {"code": "x25.not_connected", "msg": "no X25 on USB, flight controller untouched"}
    stages = [Stage("cm5", _sh("cm5", "install-cm5.sh")),
              Stage("x25", _py("x25", "x25_configure.py") + ["--yes"], skip=skip),
              _verify_stage()]
    return _start(Job("apply", stages, config_dir, on_ok=_applied_writer(role, config_dir)))


def verify(role):
    if role == "vps":
        return _start(Job("verify", [], None, fail=_vps_not_ready()))
    return _start(Job("verify", [_verify_stage()]))


def x25_restore(backup):
    return _start(Job("x25-restore", [Stage("x25", _py("x25", "x25_configure.py") + ["--restore", str(backup), "--yes"])]))


def uninstall(preview=False):
    saved = paths.STATE_DIR / "uninstall.sh"
    cmd = ["bash", str(saved)] if saved.is_file() else _sh("cm5", "uninstall-cm5.sh")
    if preview:
        cmd = cmd + ["--preview"]
    return _start(Job("uninstall-preview" if preview else "uninstall", [Stage("uninstall", cmd)]))


def current_job():
    """The running job, else None."""
    with _registry_lock:
        for job in reversed(_registry):
            if job.state == "running":
                return job
    return None


def get_job(job_id):
    """A job started in this process (one of the last few), else None."""
    with _registry_lock:
        for job in _registry:
            if job.id == job_id:
                return job
    return None
