# uavlink — architecture and contracts

This file is the contract between the parts of the package. Each part is built by a separate ticket, in parallel, so
**names, paths, formats and function signatures below are binding**. If a ticket needs to change one, it asks first
(`vixtab ticket ask`) instead of changing it silently.

Plan and background (Vietnamese): `D:\dynorch-projects\mavlink\KE_HOACH_GOI_CAI_DAT.md`.
The scripts in `engine/` started as verbatim copies of the tested installer in `D:\dynorch-projects\mavlink\cm5-x25-install`
(operator-driven `deploy.sh` flow, see its `README.md`). Their logic and bug fixes were run on the real hardware: keep them.

Scope of version 0.1.x: role **cm5** end to end (CLI + setup page). Role **vps** appears in presets and in the UI but its
engine is phase 4: applying it returns the error code `role.vps_not_ready`.

## 1. Packages

| Package | Arch | Source | Contents |
|---|---|---|---|
| `uavlink` | all | `debian/` at repo root, format `3.0 (native)`, version `0.1.0` | CLI, setup server + page, engine, presets, vendored pymavlink |
| `uavlink-router` | any | `packaging/uavlink-router/`, its own source package, `3.0 (quilt)` | mavlink-router at commit `2362c620f483cef1edd574fb962a373a288e4b9e` |

- Both must build **without network access** (Launchpad PPA builders have none). Upstream sources are fetched into the
  orig tarball by a script, never at build time.
- `uavlink` Depends on `uavlink-router`, `python3 (>= 3.10)`, `python3-serial`, `iproute2`, `iputils-ping`, `ufw`,
  `chrony | time-daemon` (an installed systemd-timesyncd satisfies it: apt will not swap it for chrony on install, the engine does that on apply), `tcpdump`, `curl`. Target: Ubuntu 24.04 (noble), arm64 (CM5) and amd64 (VPS, WSL tests).
- Installing or upgrading a package never changes the running system (network, firewall, router, X25). Only
  `uavlink apply` (CLI) or the Apply button (setup page) does.

## 2. Installed layout

| Path | Owner | Contents |
|---|---|---|
| `/usr/bin/uavlink` | pkg | launcher: adds `/usr/lib/uavlink/py` to `sys.path`, calls `uavlink.cli.main()` |
| `/usr/lib/uavlink/py/uavlink/` | pkg | Python package (stdlib only) |
| `/usr/lib/uavlink/vendor/pymavlink/` | pkg | vendored pure-Python pymavlink 2.4.50 (LGPL-3) |
| `/usr/lib/uavlink/engine/` | pkg | `lib/`, `cm5/`, `x25/`, `tools/` (same tree as `engine/` in the repo) |
| `/usr/share/uavlink/` | pkg | `schema.json`, `presets/*.json`, `i18n/*.json`, `web/` |
| `/usr/lib/systemd/system/uavlink-setup.service` | pkg | temporary setup server, never enabled |
| `/usr/lib/uavlink-router/mavlink-routerd` | uavlink-router | router binary (not `/usr/bin`, so a hand-built router there stays untouched) |
| `/usr/lib/systemd/system/uavlink-router.service` | uavlink-router | not enabled or started by the package; the engine enables it |
| `/etc/uavlink/config.env` | `uavlink apply` | 0644 root, non-secret settings (not a conffile) |
| `/etc/uavlink/secrets.env` | `uavlink apply` | 0600 root, secrets only |
| `/var/lib/uavlink/` | engine | `manifest.tsv`, `uninstall.sh`, `x25-backups/`, `logs/`, `applied.json` |
| `/run/uavlink/` | setup server | `setup.json` (0600), `serve.env`, `job.lock`, temporary preview config dirs |

Repo tree mirrors it: `bin/`, `py/uavlink/`, `vendor/`, `engine/`, `share/`, `systemd/`, `debian/`,
`packaging/uavlink-router/`, `tests/`, `tools/`.

`py/uavlink/paths.py` resolves every path above, with environment overrides for tests: `UAVLINK_LIB_DIR`,
`UAVLINK_SHARE_DIR`, `UAVLINK_CONFIG_DIR`, `UAVLINK_STATE_DIR`, `UAVLINK_RUN_DIR`. Running from the source tree works
without any override (paths point into the repo, except config/state/run which stay system paths). Always use `paths`,
never hard-code these paths in Python.

## 3. Configuration

- Format: one `KEY=VALUE` per line. **Writers** (`config.write_env`) always single-quote values (`KEY='a b'`, a `'` is
  written as `'\''`). **Readers** (`config.read_env`) accept the legacy format of `cm5-x25-install/config.env` too:
  unquoted (inline `# comment` stripped), `"double"`, `'single'`. Readers never execute shell.
- The engine loads both files with `set -a; . config.env; . secrets.env; set +a` (directory `UAVLINK_CONFIG_DIR`,
  default `/etc/uavlink`). Values in `secrets.env` win.
- Keys are those of `cm5-x25-install/config.env`, minus operator-only keys (`CM5_SSH`, `VPS_SSH`, `DO_*`, `MAV_VENV`,
  `MAVLINK_ROUTER_REPO`, `MAVLINK_ROUTER_REF`, `PYMAVLINK_VERSION`, `PYSERIAL_VERSION`, `WEB_PIP_PACKAGES`,
  `CM5_SUDO_NOPASSWD`), plus:

| New key | Values | Meaning |
|---|---|---|
| `UAVLINK_ROLE` | `cm5` \| `vps` | role of this machine |
| `UAVLINK_PRESET` | preset id | preset the values came from (informational) |
| `CM5_MAVLINK_ALLOW_LAN` | `0` \| `1` | ufw also opens TCP `CM5_TCP_SERVER_PORT` and UDP `MAVLINK_LAN_PORT` to the home LAN/Wi-Fi (bench preset) |

- `X25_BACKUP_DIR` default becomes `/var/lib/uavlink/x25-backups`. `CM5_USER` empty = auto (the user owning the active
  desktop session, else `SUDO_USER`, else uid 1000).
- Secrets (only in `secrets.env`, never logged, never in events): `MAVLINK_SIGNING_PASSPHRASE`, `WEB_BASIC_AUTH_PASS`,
  `TS_AUTHKEY`, `CM5_WIFI_PSK`. A missing `MAVLINK_SIGNING_PASSPHRASE` with `MAVLINK_SIGNING=1` is generated on save
  (32 chars `[A-Za-z0-9]`).

### 3.1 `share/schema.json`

```json
{
  "version": 1,
  "tabs": [{"id": "network", "label": {"vi": "Mạng", "en": "Network"}}],
  "keys": {
    "CM5_LAN_IP": {
      "roles": ["cm5"], "tab": "network", "type": "ipv4", "default": "192.168.50.2",
      "label": {"vi": "…", "en": "…"}, "help": {"vi": "…", "en": "…"},
      "advanced": false, "secret": false, "required": true
    }
  }
}
```

- Tabs (ids): `network`, `x25`, `links`, `security`, `wifi` (cm5); `vps`, `web`, `security` (vps).
- `type`: `string`, `int` (`min`, `max`), `port`, `bool` (`"0"`/`"1"`), `enum` (`options: [{value, label}]`), `ipv4`,
  `host` (IPv4 or DNS name), `iface`, `path`, `cidr_list` (space-separated `a.b.c.d/n`), `udp_targets`
  (space-separated `name=ip:port`), `source_list` (`any` or space-separated IP/CIDR), `passphrase`
  (`^[A-Za-z0-9._-]{12,}$`).
- Optional: `allow_empty` (empty string is valid), `show_if: {"KEY": "value"}`, `placeholder`.
- Every value is a string, as in the env file.

### 3.2 `share/presets/<id>.json`

```json
{
  "id": "dgp-current", "role": "cm5", "order": 10, "star": true,
  "name": {"vi": "Cấu hình mặc định", "en": "Default configuration"},
  "desc": {"vi": "…", "en": "…"},
  "warning": {"vi": "…", "en": "…"},
  "values": {"CM5_LAN_IP": "192.168.50.2"},
  "ask": ["VPS_PUBLIC_IP"]
}
```

`values` overlay the schema defaults; `ask` lists keys the user must confirm in step ③ (highlighted); `warning` is
optional. Preset ids: `dgp-current`, `vps-starlink`, `tailscale-only`, `bench-lan`, `ardupilot-default`, `custom`
(cm5); `vps-current`, `vps-minimal`, `vps-restricted` (vps). The dynamic preset `running` (values read from the live
system) comes from `detect.running_preset()`.

### 3.3 Text and languages

- Bilingual objects `{"vi": "…", "en": "…"}` live in `schema.json` and presets.
- UI strings: `share/web/i18n/vi.json` and `en.json`, flat keys (`"step.role.title": "…"`), identical key sets.
- Engine message codes: `share/i18n/engine-<part>.json` (`engine-cm5.json`, `engine-verify.json`, `engine-x25.json`):
  `{"<code>": {"vi": "…", "en": "…"}}`. Placeholders `{name}` are filled from the event's `args`.
- A test fails when any bilingual object misses a language or the two UI files differ in keys.

## 4. Engine

Entry points (run as root, read the config from `UAVLINK_CONFIG_DIR`):

| Command | Does |
|---|---|
| `engine/cm5/install-cm5.sh [--preview] [--steps a,b]` | steps `packages`, `usb`, `eth`, `wifi`, `tailscale`, `router`, `security` |
| `engine/x25/x25_configure.py [--preview] [--restore FILE] [--list-backups] [--yes] [--signing on\|keep\|off]` | flight controller params over USB |
| `engine/tools/verify-cm5.sh` | acceptance checks |
| `engine/tools/net_probe.py [--mode auto\|live\|probe] [--timeout S]` | network path checks (section 4.3): X25 over LAN, CM5 ↔ VPS both ways, GCS ports on the VPS, Tailscale GCS, CM5 listeners; read-only, never changes configuration |
| `engine/cm5/uninstall-cm5.sh [--preview] [--purge]` | undo everything recorded in `manifest.tsv` |

- `--preview`: no writes at all (files, services, firewall, X25 params). Prints what would change as `change` events,
  and runs read-only checks.
- On WSL (`/proc/sys/kernel/osrelease` contains `microsoft` or `WSL_DISTRO_NAME` is set), everything except `--preview`
  refuses with exit 4 and code `env.wsl`, unless `UAVLINK_ALLOW_WSL=1`.
- Python engine parts import pymavlink from `paths.VENDOR_DIR`; the runner sets `PYTHONPATH` to it. No venv, no pip.
- The router is the packaged `uavlink-router.service`. On first apply the engine disables a hand-built
  `mavlink-router.service` if one is enabled, and records that in the manifest so uninstall can re-enable it. The
  sandbox drop-in goes to `/etc/systemd/system/uavlink-router.service.d/`.
- Firewall/SSH rollback timer is named `uavlink-rollback` (`systemctl stop uavlink-rollback.timer` confirms).
- Exit codes: `0` ok, `1` failed, `2` usage, `3` a check failed, `4` refused (armed, WSL, unsafe).

### 4.1 Progress events

When `UAVLINK_EVENTS=1`, engine commands print machine-readable lines on **stdout**, mixed with the human log:

```text
@@UAVLINK {"t":"step","id":"eth","state":"start"}
@@UAVLINK {"t":"step","id":"eth","state":"ok"}
@@UAVLINK {"t":"step","id":"eth","state":"fail","code":"eth.default_route","args":{"iface":"eth0"},"msg":"default route goes via eth0"}
@@UAVLINK {"t":"change","kind":"file","target":"/etc/mavlink-router/main.conf","action":"update","diff":"--- …"}
@@UAVLINK {"t":"change","kind":"param","target":"NET_IPADDR3","old":"14","new":"1"}
@@UAVLINK {"t":"check","id":"lan.link","ok":true,"code":"lan.link_up","args":{"iface":"eth0"},"msg":"eth0 has carrier"}
@@UAVLINK {"t":"info","code":"x25.firmware","args":{"version":"ArduCopter V4.7.1"},"msg":"…"}
@@UAVLINK {"t":"rollback","state":"armed","secs":180}
```

- Shell scripts emit them with `engine/lib/events.sh` (`ev_step`, `ev_check`, `ev_change`, `ev_info`, `ev_rollback`,
  `ev_emit`, `ev_str`, `uavlink_is_wsl`; usage in its header, tests in `tests/test_events_sh.py`). Python engine
  parts print the same format themselves.
- One JSON object per line, UTF-8, after the exact prefix `@@UAVLINK ` (with one space).
- `t`: `step` (`state`: `start` `ok` `skip` `warn` `fail`), `change`, `check` (`ok`: bool), `info`, `rollback`
  (`state`: `armed` `confirmed` `fired`), `result` (`ok`, `data`).
- `change.kind`: `file` `service` `param` `firewall` `nm` `package` `user` `udev`; `action`: `create` `update`
  `delete` `enable` `disable` `start` `restart` `mask` `unmask` `install` `set`.
- `code`/`args` are optional; `msg` is the English fallback. Secrets never appear in any event or log line; diffs of
  files that hold secrets show `<redacted>` for the value.
- Step ids are listed in the table above; check ids are dotted (`lan.link`, `x25.usb`, `x25.disarmed`, `vps.reach`…).
  The page groups checks by the first part of the id: `x25` (X25 flight controller), `lan`/`eth` (LAN port),
  `internet`/`vps` (Internet and VPS), `gcs` (ground stations), `cm5`/`tailscale`/`router`/`ssh`/`ufw`/`signing`
  (this machine); anything else goes to "other".
- `check` events may carry `"severity": "warn"`: a failed check that is only a warning (e.g. no GPS fix on the bench,
  ICMP blocked); the page shows it amber and does not count it as a failure.
- `info` events may carry `data` (any JSON object) for the page to render as a table. Defined so far:
  `{"t":"info","code":"x25.status","data":{…}}` (X25 health snapshot, fields in section 4.3).

### 4.3 Pre-check coverage

| Area | Check ids | Source |
|---|---|---|
| X25 over USB | `x25.usb`, `x25.disarmed`, `x25.firmware`, `x25.sensors`, `x25.gps`, `x25.battery`, `x25.ekf`, `x25.prearm`; `info x25.status` | `x25_configure.py --preview` |
| X25 over LAN | `lan.link`, `eth.default_route` (install-cm5), `x25.lan_ping`, `x25.lan_mavlink` (heartbeat from `X25_IP` on `MAVLINK_LAN_PORT`, or seen through the running router) | `install-cm5.sh --preview`, `net_probe.py` |
| CM5 → VPS → outside | `internet`, `vps.dns`, `vps.ping` (warn only), `vps.tcp` (GCS TCP port connects and speaks MAVLink), `vps.uplink` (a heartbeat sent by this machine to `VPS_UPLINK_PORT` comes out on the GCS TCP port), `vps.x25_via_vps` (X25 heartbeats visible to an outside client, live mode) | `net_probe.py` |
| outside → VPS → CM5 | `vps.downlink` (a message sent by an outside client on the GCS TCP port reaches this machine), `vps.cmd_via_vps` (signed `PARAM_REQUEST_READ` through the VPS answered by the X25, live mode) | `net_probe.py` |
| GCS clients | `vps.gcs_udp` (GCS UDP port answers), `gcs.tailscale.<name>` (`tailscale ping` to each Tailscale GCS target), `cm5.listen` (router listens on TCP `CM5_TCP_SERVER_PORT` / UDP `MAVLINK_LAN_PORT`), `cm5.firewall` (ufw lets MAVLink in on the X25 port and `tailscale0`) | `net_probe.py` |

- `net_probe.py --mode auto`: **live** when a local router with a VPS endpoint is running (use it; only TCP clients
  to the VPS, nothing that could take over the VPS's UDP endpoints), else **probe** (own UDP socket to the VPS). In
  probe mode, if vehicle traffic already flows through the VPS (another CM5 is live), the UDP tests are skipped with
  a warning so a live system is never disturbed.
- Connections *into* this machine from the Internet/tailnet cannot be proven from the machine itself: the checks show
  that the router listens, the firewall allows it and the GCS peers answer `tailscale ping`.
- `x25.status` data: `firmware`, `board`, `vehicle`, `system_status`, `armed`, `sensors` (list of
  `{id, present, enabled, healthy}`), `gps` (`{fix, sats, hdop}`), `battery` (`{voltage, current, remaining}`),
  `ekf` (`{ok, flags}`), `prearm` (list of strings), `net` (current `NET_*` values).

### 4.2 Manifest (`/var/lib/uavlink/manifest.tsv`)

Written by the engine before it changes anything for the first time; read by `uninstall-cm5.sh`. Tab-separated:

```text
<kind>	<target>	<prior state>	<backup path or ->
```

| kind | target | prior state |
|---|---|---|
| `file` | absolute path (also a directory the engine created: uninstall removes it only when empty) | `absent` \| `present` (backup = copy of the original) |
| `service` | unit name | `enabled-active`, `enabled-inactive`, `disabled-active`, `disabled-inactive`, `masked`, `absent` (read from systemd, never guessed) |
| `ufw` | `ufw` | `active` \| `inactive` (backup = directory with the original `/etc/ufw/*.rules` and `ufw.conf`) |
| `nm` | connection name | `absent` \| `present` (backup = the original keyfile, or `-` when the connection is a netplan file `/etc/netplan/90-NM-<uuid>.yaml`, recorded as its own `file` line) |
| `group` | `user:group` | `absent` (the user was not a member; written only then, so an existing membership is never removed) |
| `user` | user name | `absent` |

Only the **first** line per (kind, target) counts: it is the state before uavlink. Backups live in
`/var/lib/uavlink/backup/`. Apply also copies `uninstall-cm5.sh` to `/var/lib/uavlink/uninstall.sh`, so `apt purge`
can undo the changes after the package files are gone.

## 5. Python package `py/uavlink/` (stdlib only)

| Module | Public API |
|---|---|
| `paths` | constants `LIB_DIR`, `ENGINE_DIR`, `VENDOR_DIR`, `SHARE_DIR`, `WEB_DIR`, `CONFIG_DIR`, `STATE_DIR`, `RUN_DIR`, `CONFIG_FILE`, `SECRETS_FILE`; `is_source_tree()` |
| `config` | `load_schema()`, `defaults(role)`, `read_env(path)`, `write_env(path, values, mode)`, `validate(role, values) -> {KEY: {"code", "args"}}`, `load_current() -> (values, secrets_present: {KEY: bool})`, `save(role, preset_id, values, secrets, config_dir=None)`, `secret_keys()`, `redact(text, secrets)` |
| `presets` | `load_presets(role=None) -> list`, `get(preset_id)`, `resolve(role, preset_id, overrides) -> values` |
| `detect` | `detect() -> dict`, `is_wsl()`, `role_guess()`, `running_preset() -> dict \| None` |
| `runner` | `Job`, `precheck(role, config_dir)`, `apply(role, config_dir=None, props_off=False)`, `verify(role)`, `x25_restore(backup)`, `uninstall(preview=False)`, `current_job()` |
| `cli` | `main(argv=None) -> int` |
| `server` | `main(argv=None) -> int` (setup server, foreground) |
| `launch` | `main(argv=None) -> int` (`uavlink setup`: start the service, print URLs, open the desktop browser) |

- `config.validate` error codes: `required`, `type.<type>` (e.g. `type.ipv4`), `range` (`args: {min,max}`),
  `enum`, `conflict` (`args: {other}`). The UI translates `validate.<code>`.
- `runner` does not read or write the config: callers save it first (`config.save`) or write a temporary config dir
  for `precheck`. A `Job` runs engine commands in sequence with `UAVLINK_EVENTS=1`, parses events, keeps them in
  memory, writes the redacted log to `STATE_DIR/logs/<UTC time>-<kind>.log`, and lets callers subscribe.
  `Job` attributes: `id`, `kind`, `state` (`running` `ok` `failed`), `events` (list of dicts; human lines are
  `{"t": "log", "line": "…"}`), `started`, `ended`; methods `start()`, `wait(timeout=None)`, `subscribe(callback)`
  (callback gets each new event, after replaying the past ones), `cancel()`. Only one job at a time
  (`RUN_DIR/job.lock`).
- Runner stages (event `{"t":"stage","id":…,"state":…}` emitted by the runner itself): precheck = `cm5` preview +
  `x25` preview + `net` (`net_probe.py --mode auto`); apply = `cm5`, `x25` (skipped when no X25 is connected or
  `props_off` is false), `verify` (`verify-cm5.sh` then `net_probe.py --mode live`).

### 5.1 CLI

`uavlink setup [--lan] [--port N] [--foreground] [--no-browser]` (→ `launch.main`), `uavlink serve […]`
(→ `server.main`), `uavlink apply --preset ID [--set KEY=VALUE]… [--preview] [--yes] [--props-off]`,
`uavlink presets [--role R]`, `uavlink config show|get KEY|set KEY=VALUE…`, `uavlink status`, `uavlink verify`,
`uavlink confirm`, `uavlink x25 backups|restore FILE`, `uavlink uninstall [--preview]`, `uavlink --version`.
`cli.main` imports `launch` and `server` lazily.

## 6. Setup server and page

- `uavlink-setup.service` runs `/usr/bin/uavlink serve` as root, `EnvironmentFile=-/run/uavlink/serve.env`
  (`UAVLINK_LISTEN`, default `127.0.0.1`; `UAVLINK_PORT`, default `8090`; `UAVLINK_IDLE_SECS`, default `1800`).
  `Restart=no`. Never enabled; `uavlink setup` starts it.
- On start the server writes `RUN_DIR/setup.json` (0600): `{"listen", "port", "token", "pid", "started"}`. Token:
  `secrets.token_urlsafe(18)`. `launch` reads this file to print the URLs.
- Auth: `GET /?t=<token>` sets cookie `uavlink=<token>; HttpOnly; SameSite=Strict; Path=/` and redirects to `/`.
  Every `/api/*` request needs the cookie (constant-time compare) → else `401 {"error": "auth"}`. Every non-GET request
  also needs `Origin` equal to `http://<Host header>` and `Content-Type: application/json` → else `403`/`415`. The
  `Host` header must be an IP literal, `localhost`, or this machine's hostname (anti DNS rebinding) → else `421`.
- Exits after `UAVLINK_IDLE_SECS` without requests, or on `POST /api/shutdown`. Static files from `paths.WEB_DIR`.

API (JSON; errors are `{"error": "<code>", "detail": "…"}`):

| Method, path | Body → response |
|---|---|
| `GET /api/state` | → `{version, is_wsl, preview_only, role_guess, host: {hostname, ips}, detect, schema, presets, current: {values, secrets_present, applied_at, preset}, i18n: {engine: {…merged engine-*.json…}}}` |
| `POST /api/validate` | `{role, values}` → `{ok, errors: {KEY: {code, args}}}` |
| `POST /api/precheck` | `{role, values}` → `{job}` |
| `POST /api/apply` | `{role, preset, values, secrets: {KEY: "value"}, props_off}` → `{job}`; `409 busy`, `423 preview_only`, `400 invalid` (with `errors`) |
| `GET /api/jobs/<id>` | → `{id, kind, state, started, ended, events}` |
| `GET /api/jobs/<id>/stream` | Server-Sent Events: `data: <event JSON>` per event (replay first), final `event: end` with `{state}` |
| `POST /api/confirm` | → `{ok}` (stops `uavlink-rollback.timer`) |
| `POST /api/verify` | → `{job}` |
| `GET /api/status` | → `{services: {name: state}, applied, last_verify}` |
| `GET /api/secret/<KEY>` | → `{value}`; only `MAVLINK_SIGNING_PASSPHRASE`, only after an apply |
| `GET /api/x25/backups`, `POST /api/x25/restore` `{file}` | → `{backups: [...]}`, `{job}` |
| `POST /api/shutdown` | → `{ok}` then exit |

- `preview_only` is true on WSL unless `UAVLINK_ALLOW_WSL=1`.
- Page: `share/web/index.html` + `app.js` + `style.css` + `i18n/`, no external requests, no build step, no framework.
  Five steps (Role → Preset → Settings → Pre-check → Apply & results) and a Status page. `?lang=vi|en` and
  `#step=N` deep links (for screenshots). Language: `?lang`, else saved choice (`localStorage`), else browser
  language, else `vi`.

## 7. Development

- Windows host, Git Bash. All Linux work runs in the WSL distro **`uavlink-dev`** (Ubuntu 24.04, systemd on, user
  `pi` with passwordless sudo, build tools installed). `tools/wsl.sh [--root] <cmd…>` runs a command there in the
  current directory. Python is not installed on Windows: run Python only through `tools/wsl.sh`.
- Tests: `bash tools/test.sh` (runs in WSL: CRLF check, `python3 -m unittest`, `shellcheck -S warning`). Python tests
  are `tests/test_*.py`, stdlib `unittest`, no root, no network, no real hardware; tests that need root or systemd use
  `tools/wsl.sh --root` from a separate script and are skipped by default.
- A pristine Ubuntu 24.04 image (systemd on, user `pi`) for clean install tests: `D:\wsl\images\ubuntu-2404-pristine.tar`
  (`wsl --import <name> D:\wsl\<name> D:\wsl\images\ubuntu-2404-pristine.tar`; `wsl --unregister <name>` afterwards).
- Executable files: set the bit in git (`git update-index --chmod=+x <file>`), Windows does not keep it.
- Line endings are LF (`.gitattributes`). Never commit secrets, `.deb` files or build output (`out/`).
- Create and change files with the Write/Edit tools, not with shell heredocs: backslashes in heredocs sent through the
  Bash tool can get lost.
- Do not touch the real CM5, VPS or X25, and do not push anywhere except `origin` (a local bare repo).
