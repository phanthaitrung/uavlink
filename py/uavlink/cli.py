"""The `uavlink` command (ARCHITECTURE.md section 5.1) on top of config, presets, runner and detect.

Exit codes: 0 ok, 1 failed, 2 usage or validation error, 4 refused (not root, busy, unsafe).
Messages are Vietnamese when LC_ALL / LC_MESSAGES / LANG starts with `vi`, else English.
"""

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

from . import VERSION, config, detect, paths, presets, runner

OK, FAILED, USAGE, REFUSED = 0, 1, 2, 4
MASK = "********"
ROLLBACK_TIMER = "uavlink-rollback.timer"
STATUS_UNITS = ("uavlink-router.service", "uavlink-setup.service", ROLLBACK_TIMER)

# ---- text -----------------------------------------------------------------------------------------------------

_S = {
    "need_root": {"vi": "Cần quyền root. Chạy: sudo {cmd}", "en": "Root is required. Run: sudo {cmd}"},
    "env_wsl": {"vi": "[env.wsl] Đang chạy trên WSL nên không áp dụng thay đổi thật. Dùng --preview, hoặc đặt UAVLINK_ALLOW_WSL=1.",
                "en": "[env.wsl] Running on WSL, so real changes are refused. Use --preview, or set UAVLINK_ALLOW_WSL=1."},
    "unknown_preset": {"vi": "Không có preset '{id}'. Xem: uavlink presets", "en": "Unknown preset '{id}'. See: uavlink presets"},
    "bad_set": {"vi": "--set cần dạng KEY=VALUE, nhận được '{item}'", "en": "--set needs KEY=VALUE, got '{item}'"},
    "invalid": {"vi": "Cấu hình không hợp lệ:", "en": "Invalid configuration:"},
    "no_role": {"vi": "Chưa có cấu hình (thiếu UAVLINK_ROLE). Hãy chạy: sudo uavlink apply --preset ID",
                "en": "No configuration yet (UAVLINK_ROLE is missing). Run: sudo uavlink apply --preset ID"},
    "busy": {"vi": "Đang có một tác vụ khác chạy. Thử lại sau.", "en": "Another job is running. Try again later."},
    "unset": {"vi": "(chưa đặt)", "en": "(unset)"},
    "no_key": {"vi": "Không có khóa '{key}'", "en": "No such key '{key}'"},
    "preview_title": {"vi": "Sẽ thay đổi (xem trước, chưa ghi gì):", "en": "Will change (preview, nothing written):"},
    "no_changes": {"vi": "Không có thay đổi nào.", "en": "No changes."},
    "checks": {"vi": "Kiểm tra:", "en": "Checks:"},
    "summary": {"vi": "Sắp áp dụng preset '{preset}' cho vai trò {role}.", "en": "About to apply preset '{preset}' for role {role}."},
    "will_set": {"vi": "Giá trị khác cấu hình hiện tại:", "en": "Values that differ from the current config:"},
    "confirm": {"vi": "Tiếp tục? [y/N] ", "en": "Continue? [y/N] "},
    "props": {"vi": "Đã THÁO CÁNH QUẠT chưa? Cấu hình X25 sẽ ghi tham số bay. [y/N] ",
              "en": "Are the PROPELLERS REMOVED? The X25 flight parameters will be written. [y/N] "},
    "cancelled": {"vi": "Đã hủy.", "en": "Cancelled."},
    "applied": {"vi": "Áp dụng xong.", "en": "Apply finished."},
    "failed": {"vi": "Không thành công, xem nhật ký trong {dir}", "en": "Failed, see the logs in {dir}"},
    "gcs": {"vi": "Kết nối trạm mặt đất: UDP {udp} hoặc TCP {ip}:{tcp}", "en": "Ground station: UDP {udp} or TCP {ip}:{tcp}"},
    "passphrase": {"vi": "Mật khẩu ký MAVLink: sudo uavlink config get MAVLINK_SIGNING_PASSPHRASE",
                   "en": "MAVLink signing passphrase: sudo uavlink config get MAVLINK_SIGNING_PASSPHRASE"},
    "rollback_ssh": {
        "vi": "Quay lui tự động sau {secs}s nếu không xác nhận. Mở một phiên SSH MỚI và chạy: sudo uavlink confirm",
        "en": "Automatic rollback in {secs}s unless confirmed. Open a NEW SSH session and run: sudo uavlink confirm"},
    "rollback_local": {"vi": "Đã xác nhận tự động (console cục bộ).", "en": "Confirmed automatically (local console)."},
    "rollback_fired": {"vi": "Đã quay lui thay đổi tường lửa/SSH.", "en": "Firewall/SSH changes were rolled back."},
    "confirmed": {"vi": "Đã xác nhận, hủy bộ đếm quay lui.", "en": "Confirmed, rollback timer stopped."},
    "confirm_failed": {"vi": "Không dừng được {unit}.", "en": "Could not stop {unit}."},
    "no_backups": {"vi": "Chưa có bản sao lưu X25.", "en": "No X25 backups."},
    "not_found": {"vi": "Không thấy tệp: {file}", "en": "File not found: {file}"},
    "status_applied": {"vi": "Lần áp dụng cuối", "en": "Last apply"},
    "status_verify": {"vi": "Lần kiểm tra cuối", "en": "Last verify"},
    "never": {"vi": "chưa có", "en": "none"},
    "stage.role": {"vi": "Vai trò", "en": "Role"},
    "stage.cm5": {"vi": "Cài đặt CM5", "en": "CM5 install"},
    "stage.x25": {"vi": "Cấu hình X25", "en": "X25 setup"},
    "stage.verify": {"vi": "Kiểm tra nghiệm thu", "en": "Acceptance checks"},
    "stage.uninstall": {"vi": "Gỡ cài đặt", "en": "Uninstall"},
}


def lang():
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name)
        if value:
            return "vi" if value.lower().startswith("vi") else "en"
    return "en"


def t(_key, **kw):
    text = _S[_key][lang()]
    return text.format(**kw) if kw else text


class _Args(dict):
    def __missing__(self, key):
        return "{" + key + "}"


_engine_cache = None


def _engine_messages():
    global _engine_cache
    if _engine_cache is None:
        merged = {}
        folder = paths.SHARE_DIR / "i18n"
        for p in sorted(folder.glob("engine-*.json")) if folder.is_dir() else []:
            try:
                with open(p, encoding="utf-8") as f:
                    merged.update(json.load(f))
            except (OSError, ValueError):
                pass
        _engine_cache = merged
    return _engine_cache


def engine_text(code, args=None, msg=None):
    """Text of an engine message code in the current language, else `msg`, else the code."""
    entry = _engine_messages().get(code) if code else None
    if isinstance(entry, dict) and entry.get(lang()):
        try:
            return entry[lang()].format_map(args or {})
        except (KeyError, ValueError, IndexError, AttributeError):
            pass
    return msg or code or ""


def step_name(step_id):
    return engine_text("step." + step_id, None, step_id)


def stage_name(stage_id):
    key = "stage." + stage_id
    return _S[key][lang()] if key in _S else stage_id


# ---- small helpers --------------------------------------------------------------------------------------------

def _is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


PROC = "/proc"
SSH_DAEMONS = ("sshd", "sshd-session")


def _session_is_remote(session):
    """logind says the login session came over the network (Remote=yes) or from sshd."""
    try:
        r = subprocess.run(["loginctl", "show-session", session, "-p", "Remote", "-p", "Service"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    return props.get("Remote") == "yes" or props.get("Service", "").startswith("sshd")


def _parent(proc, pid):
    """(command name, parent pid) of a process from <proc>/<pid>/stat, else (None, 0)."""
    try:
        with open(os.path.join(proc, str(pid), "stat"), encoding="utf-8", errors="replace") as f:
            stat = f.read()
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        return comm, int(stat[stat.rindex(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return None, 0


def over_ssh():
    """True when this command runs in an SSH session. `sudo` drops SSH_CONNECTION, so the logind session of this
    process (from its cgroup) and its parent processes count too."""
    if any(os.environ.get(k) for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return True
    try:
        with open(os.path.join(PROC, "self", "cgroup"), encoding="utf-8", errors="replace") as f:
            m = re.search(r"/session-([^/.]+)\.scope", f.read())
    except OSError:
        m = None
    if m and _session_is_remote(m.group(1)):
        return True
    _comm, pid = _parent(PROC, "self")
    for _ in range(64):
        if pid <= 1:
            break
        comm, pid = _parent(PROC, pid)
        if comm in SSH_DAEMONS:
            return True
    return False


def _out(text=""):
    print(text, flush=True)


def _err(text):
    print(text, file=sys.stderr, flush=True)


def _ask(prompt):
    """Prompt on stderr (stdout may carry --json), answer from stdin. EOF counts as no."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        return input().strip().lower() in ("y", "yes", "c", "có", "co")
    except EOFError:
        return False


def _current_role():
    return config.read_env(str(paths.CONFIG_DIR / "config.env")).get("UAVLINK_ROLE") or None


def _parse_sets(items):
    out = {}
    for item in items or []:
        key, eq, value = item.partition("=")
        if not eq or not config.KEY_RE.match(key):
            raise ValueError(t("bad_set", item=item))
        out[key] = value
    return out


def _print_errors(errors, as_json=False):
    if as_json:
        _out(json.dumps({"t": "validation", "errors": errors}, ensure_ascii=False))
        return
    _err(t("invalid"))
    for key, e in errors.items():
        args = e.get("args") or {}
        text = _validation_text(e["code"], args)
        _err("  %s: %s" % (key, text))


_VALIDATION = {
    "required": {"vi": "bắt buộc", "en": "required"},
    "range": {"vi": "ngoài khoảng {min}..{max}", "en": "out of range {min}..{max}"},
    "enum": {"vi": "phải là một trong: {options}", "en": "must be one of: {options}"},
    "conflict": {"vi": "xung đột với {other}", "en": "conflicts with {other}"},
}


def _validation_text(code, args):
    if code in _VALIDATION:
        shown = dict(args)
        if isinstance(shown.get("options"), list):
            shown["options"] = ", ".join(shown["options"])
        return _VALIDATION[code][lang()].format_map(_Args(shown))
    kind = code.split(".", 1)[-1]
    return ("giá trị không hợp lệ (%s)" if lang() == "vi" else "invalid value (%s)") % kind


# ---- live output ----------------------------------------------------------------------------------------------

class Progress:
    """Subscribes to a job and prints one line per stage/step; remembers changes, checks and the rollback state."""

    def __init__(self, as_json=False, verbose=False):
        self.as_json = as_json
        self.verbose = verbose
        self.changes = []
        self.checks = []
        self.rollback_secs = None
        self.rollback_fired = False
        self._lock = threading.Lock()

    def __call__(self, ev):
        with self._lock:
            if self.as_json:
                _out(json.dumps(ev, ensure_ascii=False))
            self._remember(ev)
            if not self.as_json:
                self._show(ev)

    def _remember(self, ev):
        kind = ev.get("t")
        if kind == "change":
            self.changes.append(ev)
        elif kind == "check":
            self.checks.append(ev)
        elif kind == "rollback":
            if ev.get("state") == "armed":
                self.rollback_secs = ev.get("secs")
            elif ev.get("state") == "fired":
                self.rollback_fired = True

    def _show(self, ev):
        kind = ev.get("t")
        text = engine_text(ev.get("code"), ev.get("args"), ev.get("msg"))
        if kind == "stage":
            name = stage_name(ev["id"])
            state = ev.get("state")
            if state == "start":
                _out("▶ " + name)
            elif state == "ok":
                _out("✔ " + name)
            elif state == "skip":
                _out("⚠ %s: %s" % (name, text or "skip"))
            elif state == "fail":
                _out("✖ %s%s" % (name, ": " + text if text else ""))
        elif kind == "step":
            state = ev.get("state")
            mark = {"ok": "✔", "skip": "⚠", "warn": "⚠", "fail": "✖"}.get(state)
            if mark:
                _out("  %s %s%s" % (mark, step_name(ev["id"]), ": " + text if text else ""))
        elif kind == "info":
            if text:
                _out("  ℹ " + text)
        elif kind == "rollback":
            self._show_rollback(ev)
        elif kind == "log" and self.verbose:
            _out("    " + ev.get("line", ""))

    def _show_rollback(self, ev):
        state = ev.get("state")
        if state == "armed" and over_ssh():
            _out("⚠ " + t("rollback_ssh", secs=ev.get("secs", "?")))
        elif state == "fired":
            _out("✖ " + t("rollback_fired"))


def run_job(job, progress):
    """Stream a started job to the console. Returns the exit code."""
    job.subscribe(progress)
    try:
        job.wait()
    except KeyboardInterrupt:
        job.cancel()
        job.wait(10)
        return FAILED
    if job.state == "ok":
        return OK
    for ev in job.events:
        if ev.get("t") == "stage" and ev.get("state") == "fail" and ev.get("exit") == REFUSED:
            return REFUSED
    return FAILED


def _print_checks(progress):
    if not progress.checks or progress.as_json:
        return
    _out(t("checks"))
    for ev in progress.checks:
        text = engine_text(ev.get("code"), ev.get("args"), ev.get("msg")) or ev.get("id", "")
        _out("  %s %s — %s" % ("✔" if ev.get("ok") else "✖", ev.get("id", ""), text))


def _print_change_table(progress):
    if not progress.changes:
        _out(t("no_changes"))
        return
    rows = []
    for ev in progress.changes:
        if ev.get("kind") == "param" or "old" in ev:
            detail = "%s → %s" % (ev.get("old", ""), ev.get("new", ""))
        else:
            detail = ev.get("action", "")
        rows.append((ev.get("kind", ""), ev.get("target", ""), detail, ev.get("diff") if progress.verbose else None))
    w1 = max(len(r[0]) for r in rows)
    w2 = max(len(r[1]) for r in rows)
    for kind, target, detail, diff in rows:
        _out("  %-*s  %-*s  %s" % (w1, kind, w2, target, detail))
        if diff:
            for line in diff.splitlines():
                _out("      " + line)


# ---- subcommands ----------------------------------------------------------------------------------------------

def cmd_apply(args):
    preset = presets.get(args.preset)
    if preset is None:
        _err(t("unknown_preset", id=args.preset))
        return USAGE
    role = args.role or preset["role"]
    try:
        overrides = _parse_sets(args.set)
        values = presets.resolve(role, args.preset, overrides)
    except (ValueError, presets.PresetError) as e:       # PresetError is a ValueError
        _err(str(e))
        return USAGE
    errors = config.validate(role, values)
    if errors:
        _print_errors(errors, args.json)
        return USAGE
    if args.preview:
        return _apply_preview(args, role, values)
    return _apply_real(args, role, values)


def _apply_preview(args, role, values):
    run_dir = str(paths.RUN_DIR)
    try:
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix="preview-", dir=run_dir)
    except OSError:
        _err(t("need_root", cmd="uavlink apply --preset %s --preview" % args.preset))
        return REFUSED
    try:
        os.chmod(tmp, 0o700)
        try:
            config.save(role, args.preset, values, {}, config_dir=tmp)
        except config.ConfigError as e:
            _print_errors(e.errors, args.json)
            return USAGE
        try:
            job = runner.precheck(role, tmp)
        except runner.Busy:
            _err(t("busy"))
            return REFUSED
        progress = Progress(args.json, args.verbose)
        code = run_job(job, progress)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not args.json:
        _out()
        _out(t("preview_title"))
        _print_change_table(progress)
        _print_checks(progress)
    return code


def _differences(role, values):
    current, _present = config.load_current()
    skeys = set(config.secret_keys())
    diff = []
    for key, new in values.items():
        old = current.get(key)
        if old is not None and old == new:
            continue
        if key in skeys:
            diff.append((key, MASK if old else t("unset"), MASK if new else t("unset")))
        else:
            diff.append((key, t("unset") if old is None else old, new))
    return diff


def _apply_real(args, role, values):
    if detect.is_wsl() and os.environ.get("UAVLINK_ALLOW_WSL") != "1":
        _err(t("env_wsl"))
        return REFUSED
    if not _is_root():
        _err(t("need_root", cmd="uavlink apply --preset " + args.preset))
        return REFUSED
    diff = _differences(role, values)
    if not args.yes:
        _err(t("summary", preset=args.preset, role=role))
        if diff:
            _err(t("will_set"))
            for key, old, new in diff:
                _err("  %s: %s → %s" % (key, old, new))
        if not _ask(t("confirm")):
            _err(t("cancelled"))
            return FAILED
    props_off = bool(args.props_off)
    if not props_off and role == "cm5" and detect.x25_ports():
        props_off = _ask(t("props"))
    try:
        saved = config.save(role, args.preset, values, {})
    except config.ConfigError as e:
        _print_errors(e.errors or {"": {"code": "invalid", "args": {}}}, args.json)
        return USAGE
    try:
        job = runner.apply(role, None, props_off)
    except runner.Busy:
        _err(t("busy"))
        return REFUSED
    progress = Progress(args.json, args.verbose)
    code = run_job(job, progress)
    _finish_apply(args, role, values, progress, code, saved)
    return code


def _finish_apply(args, role, values, progress, code, saved):
    # The rollback timer keeps a firewall/SSH change from locking a remote session out. On the local console it is
    # confirmed at once, also when a later stage (X25, verify) failed: that says nothing about SSH access.
    remote = over_ssh()
    confirmed = bool(progress.rollback_secs) and not remote and _confirm_quiet()
    if args.json:
        return
    _print_checks(progress)
    if code != OK:
        _out("✖ " + t("failed", dir=paths.STATE_DIR / "logs"))
    else:
        _out("✔ " + t("applied"))
    if confirmed:
        _out("  " + t("rollback_local"))
    elif progress.rollback_secs and remote:
        _out("⚠ " + t("rollback_ssh", secs=progress.rollback_secs))
    if code != OK:
        return
    if role == "cm5":
        _out("  " + t("gcs", udp=values.get("MAVLINK_LAN_PORT", ""), ip=values.get("CM5_LAN_IP", ""),
                       tcp=values.get("CM5_TCP_SERVER_PORT", "")))
    if values.get("MAVLINK_SIGNING") == "1":
        _out("  " + t("passphrase"))


def _confirm_quiet():
    try:
        return subprocess.run(["systemctl", "stop", ROLLBACK_TIMER], check=False).returncode == 0
    except OSError:
        return False


def cmd_presets(args):
    items = presets.load_presets(args.role)
    width = max([len(p["id"]) for p in items] or [0])
    for p in items:
        _out("%s %-*s  %-3s  %s" % ("★" if p.get("star") else " ", width, p["id"], p.get("role", ""),
                                    p["name"].get(lang()) or p["name"].get("en", "")))
    return OK


def cmd_config(args):
    if args.action == "show":
        return _config_show()
    if args.action == "get":
        return _config_get(args.items)
    return _config_set(args.items)


def _config_show():
    values, present = config.load_current()
    order = list(config.load_schema()["keys"])
    ranked = sorted(values, key=lambda k: (order.index(k) if k in order else -1, k))
    for key in ranked:
        _out("%s=%s" % (key, values[key]))
    for key, there in present.items():
        if there:
            _out("%s=%s" % (key, MASK))
    return OK


def _config_get(items):
    if len(items) != 1:
        _err("usage: uavlink config get KEY")
        return USAGE
    key = items[0]
    if key in config.secret_keys():
        if not _is_root():
            _err(t("need_root", cmd="uavlink config get " + key))
            return REFUSED
        value = config.read_env(str(paths.CONFIG_DIR / "secrets.env")).get(key)
    else:
        value = config.read_env(str(paths.CONFIG_DIR / "config.env")).get(key)
    if value is None or (value == "" and key in config.secret_keys()):
        _err(t("no_key", key=key))
        return FAILED
    _out(value)
    return OK


def _config_set(items):
    if not items:
        _err("usage: uavlink config set KEY=VALUE…")
        return USAGE
    try:
        sets = _parse_sets(items)
    except ValueError as e:
        _err(str(e))
        return USAGE
    role = _current_role()
    if role is None:
        _err(t("no_role"))
        return FAILED
    skeys = set(config.secret_keys())
    if any(k in skeys for k in sets) and not _is_root():
        _err(t("need_root", cmd="uavlink config set …"))
        return REFUSED
    current, _present = config.load_current()
    values = config.defaults(role)
    values.update({k: v for k, v in current.items() if k in values})
    values.update({k: v for k, v in sets.items() if k not in skeys})
    secrets = {k: v for k, v in sets.items() if k in skeys}
    try:
        config.save(role, current.get("UAVLINK_PRESET", ""), values, secrets)
    except config.ConfigError as e:
        _print_errors(e.errors or {"": {"code": "invalid", "args": {}}})
        if not e.errors:
            _err(str(e))
        return USAGE
    except OSError as e:
        _err(str(e))
        return REFUSED
    return OK


def _unit_state(unit):
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return r.stdout.strip() or "unknown"


def _last_log(kind):
    folder = paths.STATE_DIR / "logs"
    found = sorted(folder.glob("*-%s.log" % kind)) if folder.is_dir() else []
    return found[-1] if found else None


def cmd_status(args):
    for unit in STATUS_UNITS:
        _out("%-28s %s" % (unit, _unit_state(unit)))
    applied = paths.STATE_DIR / "applied.json"
    try:
        with open(applied, encoding="utf-8") as f:
            data = json.load(f)
        _out("%-28s %s  preset=%s role=%s version=%s" % (t("status_applied"), data.get("time", ""),
                                                        data.get("preset", ""), data.get("role", ""), data.get("version", "")))
    except (OSError, ValueError):
        _out("%-28s %s" % (t("status_applied"), t("never")))
    log = _last_log("verify")
    if log is None:
        _out("%-28s %s" % (t("status_verify"), t("never")))
    else:
        when = datetime.fromtimestamp(log.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        _out("%-28s %s  %s" % (t("status_verify"), when, log))
    return OK


def cmd_verify(args):
    role = _current_role() or detect.role_guess() or "cm5"
    try:
        job = runner.verify(role)
    except runner.Busy:
        _err(t("busy"))
        return REFUSED
    progress = Progress(args.json, args.verbose)
    code = run_job(job, progress)
    _print_checks(progress)
    return code


def cmd_confirm(args):
    if not _is_root():
        _err(t("need_root", cmd="uavlink confirm"))
        return REFUSED
    if _confirm_quiet():
        _out(t("confirmed"))
        return OK
    _err(t("confirm_failed", unit=ROLLBACK_TIMER))
    return FAILED


def _backup_dir():
    configured = config.read_env(str(paths.CONFIG_DIR / "config.env")).get("X25_BACKUP_DIR")
    return configured or str(paths.STATE_DIR / "x25-backups")


def cmd_x25(args):
    folder = _backup_dir()
    if args.action == "backups":
        files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
        if not files:
            _out(t("no_backups"))
        for name in files:
            _out(os.path.join(folder, name))
        return OK
    if not args.file:
        _err("usage: uavlink x25 restore FILE")
        return USAGE
    target = args.file if os.path.exists(args.file) else os.path.join(folder, args.file)
    if not os.path.isfile(target):
        _err(t("not_found", file=args.file))
        return FAILED
    if not _is_root():
        _err(t("need_root", cmd="uavlink x25 restore " + args.file))
        return REFUSED
    if not args.props_off and not _ask(t("props")):
        _err(t("cancelled"))
        return FAILED
    try:
        job = runner.x25_restore(target)
    except runner.Busy:
        _err(t("busy"))
        return REFUSED
    progress = Progress(False, args.verbose)
    return run_job(job, progress)


def cmd_uninstall(args):
    if not args.preview:
        if not _is_root():
            _err(t("need_root", cmd="uavlink uninstall"))
            return REFUSED
        if not args.yes and not _ask(t("confirm")):
            _err(t("cancelled"))
            return FAILED
    try:
        job = runner.uninstall(preview=args.preview)
    except runner.Busy:
        _err(t("busy"))
        return REFUSED
    progress = Progress(False, args.verbose)
    code = run_job(job, progress)
    if args.preview:
        _out()
        _out(t("preview_title"))
        _print_change_table(progress)
    return code


# ---- parser ---------------------------------------------------------------------------------------------------

def _bi(en, vi):
    return "%s / %s" % (en, vi)


def build_parser():
    p = argparse.ArgumentParser(
        prog="uavlink", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=_bi("Configure the CM5 + X25 MAVLink link.", "Cấu hình đường MAVLink CM5 + X25.") +
                    "\n\n  sudo uavlink apply --preset dgp-current\n  sudo uavlink setup")
    p.add_argument("--version", action="version", version="uavlink " + VERSION)
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    def add(name, en, vi, **kw):
        return sub.add_parser(name, help=_bi(en, vi), description=_bi(en, vi), **kw)

    add("setup", "open the setup page (options: --lan --port N --foreground --no-browser)",
        "mở trang cài đặt (tùy chọn: --lan --port N --foreground --no-browser)", add_help=False)
    add("serve", "run the setup server in the foreground", "chạy máy chủ cài đặt ở foreground", add_help=False)

    a = add("apply", "apply a preset to this machine", "áp dụng một preset cho máy này")
    a.add_argument("--preset", required=True, metavar="ID", help=_bi("preset id", "mã preset"))
    a.add_argument("--role", choices=("cm5", "vps"), help=_bi("role (default: the preset's)", "vai trò (mặc định: của preset)"))
    a.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help=_bi("override a value (repeatable)", "ghi đè một giá trị (lặp lại được)"))
    a.add_argument("--preview", action="store_true", help=_bi("show what would change, write nothing", "chỉ xem trước, không ghi gì"))
    a.add_argument("--yes", action="store_true", help=_bi("do not ask for confirmation", "không hỏi xác nhận"))
    a.add_argument("--props-off", action="store_true", help=_bi("propellers are removed (X25 may be configured)",
                                                                  "đã tháo cánh quạt (được cấu hình X25)"))
    a.add_argument("--json", action="store_true", help=_bi("print raw events, one per line", "in sự kiện thô, mỗi dòng một sự kiện"))
    a.add_argument("--verbose", action="store_true", help=_bi("show file diffs and engine output", "hiện diff tệp và log của engine"))

    pr = add("presets", "list presets", "liệt kê preset")
    pr.add_argument("--role", choices=("cm5", "vps"))

    c = add("config", "show or change the saved configuration", "xem hoặc đổi cấu hình đã lưu")
    c.add_argument("action", choices=("show", "get", "set"))
    c.add_argument("items", nargs="*", metavar="KEY|KEY=VALUE")

    add("status", "state of services, last apply and last verify", "trạng thái dịch vụ, lần áp dụng và kiểm tra cuối")
    v = add("verify", "run the acceptance checks", "chạy kiểm tra nghiệm thu")
    v.add_argument("--json", action="store_true")
    v.add_argument("--verbose", action="store_true")
    add("confirm", "confirm the firewall/SSH change (stops the rollback timer)",
        "xác nhận thay đổi tường lửa/SSH (dừng bộ đếm quay lui)")

    x = add("x25", "flight controller parameter backups", "sao lưu tham số bộ điều khiển bay")
    x.add_argument("action", choices=("backups", "restore"))
    x.add_argument("file", nargs="?")
    x.add_argument("--props-off", action="store_true", help=_bi("propellers are removed", "đã tháo cánh quạt"))
    x.add_argument("--verbose", action="store_true")

    u = add("uninstall", "undo everything uavlink changed", "hoàn tác mọi thay đổi của uavlink")
    u.add_argument("--preview", action="store_true", help=_bi("show what would change", "chỉ xem trước"))
    u.add_argument("--yes", action="store_true", help=_bi("do not ask for confirmation", "không hỏi xác nhận"))
    u.add_argument("--verbose", action="store_true")
    return p


COMMANDS = {"apply": cmd_apply, "presets": cmd_presets, "config": cmd_config, "status": cmd_status,
            "verify": cmd_verify, "confirm": cmd_confirm, "x25": cmd_x25, "uninstall": cmd_uninstall}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if argv and argv[0] in ("setup", "serve"):
        module = importlib.import_module("." + ("launch" if argv[0] == "setup" else "server"), __package__)
        return module.main(argv[1:])
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else USAGE
    if not args.cmd:
        parser.print_help()
        return USAGE
    return COMMANDS[args.cmd](args)
