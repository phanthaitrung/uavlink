"""Configuration model (ARCHITECTURE.md section 3): schema, env files, validation, save."""

import ipaddress
import json
import os
import re
import secrets as _secrets
import string
import tempfile

from . import paths

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PASSPHRASE_RE = re.compile(r"^[A-Za-z0-9._-]{12,}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
DNS_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

SIGNING_KEY = "MAVLINK_SIGNING_PASSPHRASE"


class ConfigError(ValueError):
    """Raised by `save` when the values do not validate or a key is unknown. `errors` is the validate() result."""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or {}


# ------------------------------------------------------------------ schema

def load_schema():
    with open(paths.SHARE_DIR / "schema.json", encoding="utf-8") as f:
        return json.load(f)


def role_keys(role, schema=None):
    """Keys of the role, in schema order: {KEY: spec}."""
    schema = schema or load_schema()
    return {k: v for k, v in schema["keys"].items() if role in v["roles"]}


def defaults(role):
    return {k: str(v["default"]) for k, v in role_keys(role).items()}


def secret_keys():
    return [k for k, v in load_schema()["keys"].items() if v.get("secret")]


# ------------------------------------------------------------------ env files

def read_env(path):
    """Parse a KEY=VALUE file. Understands the legacy cm5-x25-install/config.env format. Never executes anything:
    `$VAR` and backticks stay literal. A missing file gives {}."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    out = {}
    n = len(text)
    i = 0
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        if text[i] == "#":
            i = _eol(text, i)
            continue
        m = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=").match(text, i)
        if not m:
            i = _eol(text, i)
            continue
        value, i = _parse_value(text, m.end())
        out[m.group(1)] = value
        i = _eol(text, i)
    return out


def _eol(text, i):
    j = text.find("\n", i)
    return len(text) if j < 0 else j + 1


def _parse_value(text, i):
    """Shell-like word: adjacent '..', "..", \\x and bare parts are joined. Ends at unquoted whitespace."""
    n = len(text)
    buf = []
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            break
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                buf.append(text[i + 1:])
                i = n
                break
            buf.append(text[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\$`\n':
                    if text[i + 1] != "\n":
                        buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1
        elif c == "\\" and i + 1 < n:
            if text[i + 1] != "\n":
                buf.append(text[i + 1])
            i += 2
        else:
            buf.append(c)
            i += 1
    return "".join(buf), i


def _quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def write_env(path, values, mode=0o644):
    """Atomically write `KEY='value'` lines (temp file in the same directory, fsync, rename)."""
    path = os.fspath(path)
    lines = []
    for k, v in values.items():
        if not KEY_RE.match(k):
            raise ValueError("invalid key name: %r" % (k,))
        v = "" if v is None else str(v)
        if "\0" in v:
            raise ValueError("NUL in value of %s" % k)
        lines.append("%s=%s\n" % (k, _quote(v)))
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".uavlink-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(lines))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _fsync_dir(directory):
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ------------------------------------------------------------------ validation

def _err(code, **args):
    return {"code": code, "args": args}


def _is_ipv4(s):
    try:
        ipaddress.IPv4Address(s)
    except ValueError:
        return False
    return bool(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", s))


def _is_cidr(s):
    ip, sep, n = s.partition("/")
    return bool(sep) and _is_ipv4(ip) and n.isdigit() and 0 <= int(n) <= 32


def _port_ok(s):
    return s.isdigit() and 1 <= int(s) <= 65535


def _check_value(spec, value):
    """Return None when valid, else an error dict."""
    typ = spec["type"]
    if "\n" in value or "\r" in value or "\0" in value:
        return _err("type." + typ)
    if typ == "string":
        return None
    if typ in ("int", "port"):
        if not re.fullmatch(r"-?\d+", value):
            return _err("type." + typ)
        lo = 1 if typ == "port" else spec.get("min")
        hi = 65535 if typ == "port" else spec.get("max")
        num = int(value)
        if (lo is not None and num < lo) or (hi is not None and num > hi):
            return _err("range", min=lo, max=hi)
        return None
    if typ == "bool":
        return None if value in ("0", "1") else _err("type.bool")
    if typ == "enum":
        allowed = [o["value"] for o in spec.get("options", [])]
        return None if value in allowed else _err("enum", options=allowed)
    if typ == "ipv4":
        return None if _is_ipv4(value) else _err("type.ipv4")
    if typ == "host":
        return None if _is_ipv4(value) or (DNS_RE.match(value) and not re.fullmatch(r"[\d.]+", value)) else _err("type.host")
    if typ == "iface":
        return None if IFACE_RE.match(value) else _err("type.iface")
    if typ == "path":
        return None if value[:1] in ("/", "~") else _err("type.path")
    if typ == "cidr_list":
        return None if all(_is_cidr(t) for t in value.split()) else _err("type.cidr_list")
    if typ == "udp_targets":
        for t in value.split():
            name, eq, rest = t.partition("=")
            ip, colon, port = rest.rpartition(":")
            if not (eq and colon and TARGET_NAME_RE.match(name) and _is_ipv4(ip) and _port_ok(port)):
                return _err("type.udp_targets")
        return None
    if typ == "source_list":
        if value == "any":
            return None
        return None if value.split() and all(_is_ipv4(t) or _is_cidr(t) for t in value.split()) else _err("type.source_list")
    if typ == "passphrase":
        return None if PASSPHRASE_RE.match(value) else _err("type.passphrase")
    return _err("type." + typ)


def _visible(spec, values):
    cond = spec.get("show_if")
    return not cond or all(str(values.get(k, "")) == str(v) for k, v in cond.items())


def validate(role, values):
    """Check the values of a role. Returns {KEY: {"code", "args"}}; empty when everything is valid.
    Keys hidden by `show_if` are skipped. Missing keys count as empty."""
    errors = {}
    specs = role_keys(role)
    for key, spec in specs.items():
        if not _visible(spec, values):
            continue
        raw = values.get(key, "")
        value = "" if raw is None else str(raw)
        if value == "":
            if not spec.get("allow_empty"):
                errors[key] = _err("required")
            continue
        e = _check_value(spec, value)
        if e:
            errors[key] = e
    if role == "cm5" and not any(k in errors for k in ("CM5_LAN_IP", "X25_IP", "X25_NETMASK")):
        _check_subnet(values, errors)
    return errors


def _check_subnet(values, errors):
    cm5, x25 = str(values.get("CM5_LAN_IP", "")), str(values.get("X25_IP", ""))
    prefix = str(values.get("X25_NETMASK", ""))
    if not (cm5 and x25 and prefix):
        return
    net = ipaddress.ip_network("%s/%s" % (x25, prefix), strict=False)
    if cm5 == x25 or ipaddress.ip_address(cm5) not in net:
        errors["CM5_LAN_IP"] = _err("conflict", other="X25_IP")
        errors["X25_IP"] = _err("conflict", other="CM5_LAN_IP")


# ------------------------------------------------------------------ current config and save

def _config_dir(config_dir):
    return os.fspath(config_dir) if config_dir else os.fspath(paths.CONFIG_DIR)


def load_current(config_dir=None):
    """Read config.env and secrets.env. Returns (values, secrets_present): `values` holds every non-secret KEY in
    config.env; `secrets_present` maps each secret key of the schema to True when a value is stored."""
    d = _config_dir(config_dir)
    cfg = read_env(os.path.join(d, "config.env"))
    sec = read_env(os.path.join(d, "secrets.env"))
    skeys = secret_keys()
    merged = dict(cfg)
    merged.update(sec)
    present = {k: bool(merged.get(k)) for k in skeys}
    values = {k: v for k, v in cfg.items() if k not in skeys}
    return values, present


def _generate_passphrase():
    alphabet = string.ascii_letters + string.digits
    return "".join(_secrets.choice(alphabet) for _ in range(32))


def save(role, preset_id, values, secrets, config_dir=None):
    """Write config.env (0644, non-secret keys of the role + UAVLINK_ROLE/UAVLINK_PRESET) and secrets.env (0600).
    Missing keys take the schema default. Empty or absent secrets keep the stored ones. Raises ConfigError for
    unknown keys or invalid values. Returns {"config": path, "secrets": path, "generated": [secret keys]}."""
    specs = role_keys(role)
    allkeys = load_schema()["keys"]
    skeys = {k for k, v in allkeys.items() if v.get("secret")}
    d = _config_dir(config_dir)

    plain = dict(values or {})
    given = dict(secrets or {})
    for k in [k for k in plain if k in skeys]:       # secrets mixed into `values` are treated as secrets
        given.setdefault(k, plain.pop(k))
    unknown = sorted((set(plain) | set(given)) - set(specs))
    if unknown:
        raise ConfigError("unknown keys for role %s: %s" % (role, ", ".join(unknown)))

    stored = read_env(os.path.join(d, "secrets.env"))
    stored = {k: v for k, v in stored.items() if v != ""}
    new_secrets = dict(stored)
    for k, v in given.items():
        if v not in (None, ""):
            new_secrets[k] = str(v)

    full = defaults(role)
    full.update({k: str(v) for k, v in plain.items()})
    generated = []
    if SIGNING_KEY in specs and full.get("MAVLINK_SIGNING") == "1" and not new_secrets.get(SIGNING_KEY):
        new_secrets[SIGNING_KEY] = _generate_passphrase()
        generated.append(SIGNING_KEY)

    check = dict(full)
    check.update({k: v for k, v in new_secrets.items() if k in specs})
    errors = validate(role, check)
    if errors:
        raise ConfigError("invalid configuration: %s" % ", ".join(sorted(errors)), errors)

    out = {"UAVLINK_ROLE": role, "UAVLINK_PRESET": preset_id or ""}
    out.update({k: v for k, v in full.items() if k not in skeys})
    os.makedirs(d, mode=0o755, exist_ok=True)
    secrets_path = os.path.join(d, "secrets.env")
    config_path = os.path.join(d, "config.env")
    order = {k: i for i, k in enumerate(allkeys)}
    write_env(secrets_path, dict(sorted(new_secrets.items(), key=lambda kv: order.get(kv[0], 10**6))), 0o600)
    write_env(config_path, out, 0o644)
    return {"config": config_path, "secrets": secrets_path, "generated": generated}


def redact(text, secrets):
    """Replace every secret value in `text` with ***. `secrets` is a {KEY: value} dict or an iterable of values."""
    values = secrets.values() if isinstance(secrets, dict) else secrets
    for v in sorted({str(v) for v in values if v}, key=len, reverse=True):
        text = text.replace(v, "***")
    return text
