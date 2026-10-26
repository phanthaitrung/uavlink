#!/usr/bin/env bash
# The checks that run inside the fresh test distro, as root. Started by run.sh:
#   in-distro.sh <repository root as seen from the distro, /mnt/…>
# Needs out/repo and out/repo-upgrade (make-repo.sh). Prints "ok" / "FAIL" per check, exits 1 if any failed.
set -uo pipefail

REPO="${1:?usage: in-distro.sh <repo root> <install|purge>}"
PHASE="${2:?usage: in-distro.sh <repo root> <install|purge>}"
LOCAL=/srv/uavlink-repo
PORT=8090
BASE="http://127.0.0.1:$PORT"
FAILS=0
TMP="$(mktemp -d /var/tmp/uavlink-it.XXXXXX)"
VER="$(sed -n '1s/^uavlink (\([^)]*\)).*/\1/p' "$REPO/debian/changelog")"   # no dpkg-dev in the fresh distro
UP="$VER+test1"                                                              # the bumped build of make-repo.sh

ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
check() { local name="$1"; shift; if "$@" >"$TMP/last" 2>&1; then ok "$name"; else bad "$name"; sed 's/^/        | /' "$TMP/last" | tail -15; fi; }
step() { printf '\n== %s\n' "$*"; }
has() { grep -Eq -- "$1" "$2"; }

# Run a command on a pseudo terminal, like a person typing it; output in $TMP/tty.out.
on_tty() { script -qec "$*" /dev/null >"$TMP/tty.out" 2>&1; }

is_candidate() { [ "$(apt-cache policy uavlink | awk '/Candidate:/ {print $2}')" = "$1" ]; }
installed_version() { dpkg-query -W -f='${Version}' uavlink; }

secret_a='Qx7mLp2vNc9RtYb4WsKd8HfJ3GzA6eUo'      # MAVLINK_SIGNING_PASSPHRASE
secret_b='tskey-auth-it-5f3a9c1e7b2d4a60'         # TS_AUTHKEY

# A fresh distro starts unattended-upgrades soon after boot; apt waits for its dpkg lock instead of failing at once.
echo 'DPkg::Lock::Timeout "900";' > /etc/apt/apt.conf.d/99uavlink-it-lock

if [ "$PHASE" = install ]; then
# ------------------------------------------------------------------ 0. the local repository
step "0. local apt repository"
rm -rf "$LOCAL"; mkdir -p "$LOCAL"
cp -r "$REPO/out/repo/." "$LOCAL/"
echo "deb [trusted=yes] file:$LOCAL ./" > /etc/apt/sources.list.d/uavlink-local.list
check "apt update" apt-get update -qq
check "apt sees uavlink $VER" is_candidate "$VER"
check "pristine: uavlink not installed" bash -c '! dpkg -s uavlink >/dev/null 2>&1'
check "systemd is running" bash -c 'test -d /run/systemd/system'
check "this is WSL" bash -c 'grep -qi microsoft /proc/sys/kernel/osrelease'

# ------------------------------------------------------------------ 1. install
step "1. install with apt (one line, on a terminal)"
if env -u DEBIAN_FRONTEND TERM=xterm bash -c 'script -qec "apt-get install -y uavlink" /dev/null' >"$TMP/install.out" 2>&1; then
  ok "apt-get install -y uavlink"
else
  bad "apt-get install -y uavlink"; tail -30 "$TMP/install.out"
fi
check "uavlink-router came as a dependency" bash -c 'dpkg -s uavlink-router | grep -q "Status: install ok installed"'
check "router binary at /usr/lib/uavlink-router/mavlink-routerd" test -x /usr/lib/uavlink-router/mavlink-routerd
check "uavlink --version says $VER" bash -c "uavlink --version | grep -qxF 'uavlink $VER'"
check "install changed nothing: router service not enabled" bash -c '! systemctl is-enabled uavlink-router.service 2>/dev/null | grep -q "^enabled"'
check "install changed nothing: no config written" bash -c '! test -e /etc/uavlink/config.env && ! test -e /var/lib/uavlink/manifest.tsv'
check "postinst printed the setup URL with a token" has 'http://127\.0\.0\.1:8090/\?t=[A-Za-z0-9_-]+' "$TMP/install.out"
token="$(grep -Eo '127\.0\.0\.1:8090/\?t=[A-Za-z0-9_-]+' "$TMP/install.out" | head -n1 | sed 's/.*t=//')"
if [ -z "$token" ]; then bad "no token found, the HTTP checks cannot run"; fi
check "setup.json holds the same token" bash -c "grep -q '$token' /run/uavlink/setup.json"
check "setup.json is mode 600" bash -c '[ "$(stat -c %a /run/uavlink/setup.json)" = 600 ]'
check "setup service is active" systemctl is-active --quiet uavlink-setup.service

JAR="$TMP/cookies"
curl -s -o /dev/null -D "$TMP/h1" -c "$JAR" "$BASE/?t=$token"
check "GET /?t=TOKEN redirects (302)" has '^HTTP/1\.[01] 302' "$TMP/h1"
check "GET /?t=TOKEN sets the HttpOnly cookie" has 'Set-Cookie: uavlink=.*HttpOnly' "$TMP/h1"
code="$(curl -s -o "$TMP/index.html" -w '%{http_code}' -b "$JAR" "$BASE/")"
check "GET / with the cookie -> 200" test "$code" = 200
check "page is the uavlink page" has 'uavlink' "$TMP/index.html"
code="$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/state")"
check "GET /api/state without cookie -> 401" test "$code" = 401
code="$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" "$BASE/?t=wrong")"
check "wrong token does not give a session (no 200 with data)" bash -c "[ '$code' != 200 ] || ! curl -s -b /dev/null '$BASE/api/state' | grep -q version"
curl -s -b "$JAR" "$BASE/api/state" > "$TMP/state.json"
check "/api/state says WSL + preview_only" bash -c "python3 - '$TMP/state.json' <<'EOF'
import json, sys
s = json.load(open(sys.argv[1]))
assert s['is_wsl'] is True and s['preview_only'] is True, (s['is_wsl'], s['preview_only'])
assert s['version'] == '$VER', s['version']
assert any(p['id'] == 'dgp-current' for p in s['presets'])
EOF"

# ------------------------------------------------------------------ 2. CLI and API previews
step "2. presets and previews (CLI and API)"
check "uavlink presets lists dgp-current" bash -c 'uavlink presets | grep -q dgp-current'
check "uavlink presets --role vps lists vps presets" bash -c 'uavlink presets --role vps | grep -q vps-'
uavlink apply --preset dgp-current --preview --json > "$TMP/cli.jsonl" 2> "$TMP/cli.err"; rc=$?
# WSL has one interface, eth0, and it carries the default route: the engine rightly flags it and the preview ends "failed" (exit 1).
check "apply --preview ends with exit 1 (eth.default_route, WSL has only eth0)" test "$rc" = 1
check "eth.default_route is the failed check" has "\"id\": \"eth\", \"state\": \"fail\", \"code\": \"eth.default_route\"" "$TMP/cli.jsonl"
uavlink apply --preset dgp-current --preview > "$TMP/cli.txt" 2>&1
check "apply --preview prints a change table" bash -c "grep -q 'mavlink-router' '$TMP/cli.txt'"
check "x25.usb is false on WSL (no X25)" bash -c "python3 - '$TMP/cli.jsonl' <<'EOF'
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1]) if l.startswith('{')]
usb = [e for e in evs if e.get('t') == 'check' and e.get('id') == 'x25.usb']
assert usb and usb[0]['ok'] is False, usb
EOF"
check "preview wrote nothing (no config, no manifest, no unit enabled)" bash -c '! test -e /etc/uavlink/config.env && ! test -e /var/lib/uavlink/manifest.tsv && ! test -e /etc/mavlink-router/main.conf && ! systemctl is-enabled uavlink-router.service 2>/dev/null | grep -q "^enabled"'

uavlink apply --preset dgp-current --yes --json > "$TMP/refuse.jsonl" 2> "$TMP/refuse.err"; rc=$?
check "apply without --preview is refused on WSL (exit 4)" test "$rc" = 4
check "refusal carries env.wsl" bash -c "cat '$TMP/refuse.jsonl' '$TMP/refuse.err' | grep -q 'env.wsl'"
check "refused apply left no manifest" bash -c '! test -e /var/lib/uavlink/manifest.tsv'
# Known: the CLI saves /etc/uavlink/config.env before the engine refuses on WSL (see the T-012 Answer). Not asserted; undone here.
if [ -e /etc/uavlink/config.env ]; then echo "  note  refused apply left /etc/uavlink/config.env (known, removed)"; rm -f /etc/uavlink/config.env /etc/uavlink/secrets.env; fi
check "expected change events: router config, router unit drop-ins, packages" bash -c "python3 - '$TMP/cli.jsonl' <<'EOF'
import json, sys
evs = [json.loads(l) for l in open(sys.argv[1]) if l.startswith('{')]
ch = {(e['kind'], e['target']) for e in evs if e.get('t') == 'change'}
for want in [('file', '/etc/mavlink-router/main.conf'), ('service', 'uavlink-router.service'), ('package', 'chrony')]:
    assert want in ch, (want, sorted(ch))
EOF"

# the same through the API
PYPATH=/usr/lib/uavlink/py
python3 - "$PYPATH" > "$TMP/precheck-body.json" <<'EOF'
import json, sys
sys.path.insert(0, sys.argv[1])
from uavlink import presets
print(json.dumps({"role": "cm5", "values": presets.resolve("cm5", "dgp-current", {})}))
EOF
api() {   # api <method> <path> [body-file]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -s -b "$JAR" -X "$method" -H "Origin: $BASE" -H 'Content-Type: application/json' --data @"$body" "$BASE$path"
  else
    curl -s -b "$JAR" -X "$method" -H "Origin: $BASE" -H 'Content-Type: application/json' "$BASE$path"
  fi
}
api POST /api/precheck "$TMP/precheck-body.json" > "$TMP/precheck.json"
job="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['job'])" "$TMP/precheck.json" 2>/dev/null || true)"
check "POST /api/precheck returns a job" test -n "$job"
curl -s -N --max-time 240 -b "$JAR" "$BASE/api/jobs/$job/stream" > "$TMP/stream.txt"
check "SSE stream ends with event: end (state failed, same reason as the CLI)" bash -c "grep -A1 '^event: end' '$TMP/stream.txt' | grep -q '\"state\": *\"failed\"'"
check "API and CLI report the same changes and checks" bash -c "python3 - '$TMP/cli.jsonl' '$TMP/stream.txt' <<'EOF'
import json, sys
def keys(evs):
    out = set()
    for e in evs:
        if e.get('t') == 'change':
            out.add(('change', e.get('kind'), e.get('target')))
        elif e.get('t') == 'check':
            out.add(('check', e.get('id'), e.get('ok')))
    return out
cli = [json.loads(l) for l in open(sys.argv[1]) if l.startswith('{')]
sse = [json.loads(l[6:]) for l in open(sys.argv[2]) if l.startswith('data: ')]
a, b = keys(cli), keys(sse)
assert any(k[0] == 'change' for k in a), 'no change events from the CLI'
assert a == b, 'differ: only CLI %s, only API %s' % (sorted(a - b, key=str), sorted(b - a, key=str))
assert ('check', 'x25.usb', False) in b
EOF"
api POST /api/apply "$TMP/precheck-body.json" > "$TMP/apply-api.json"
code="$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" -X POST -H "Origin: $BASE" -H 'Content-Type: application/json' --data '{"role":"cm5","values":{},"secrets":{}}' "$BASE/api/apply")"
check "POST /api/apply is refused on WSL (423 preview_only)" test "$code" = 423

# a real secret in the saved config, so the log scan in step 5 means something
uavlink config set "MAVLINK_SIGNING_PASSPHRASE=$secret_a" "TS_AUTHKEY=$secret_b" > "$TMP/cfgset.out" 2>&1 || true
if ! grep -q "$secret_a" /etc/uavlink/secrets.env 2>/dev/null; then
  install -d -m 0755 /etc/uavlink
  printf "MAVLINK_SIGNING_PASSPHRASE='%s'\nTS_AUTHKEY='%s'\n" "$secret_a" "$secret_b" > /etc/uavlink/secrets.env
fi
chmod 600 /etc/uavlink/secrets.env
uavlink apply --preset dgp-current --preview > /dev/null 2>&1
uavlink status > "$TMP/status.out" 2>&1 || true

# shut the setup server down through the API
code="$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" -X POST -H "Origin: $BASE" -H 'Content-Type: application/json' --data '{}' "$BASE/api/shutdown")"
check "POST /api/shutdown -> 200" test "$code" = 200
check "setup server stopped" bash -c 'for i in $(seq 1 20); do systemctl is-active --quiet uavlink-setup.service || exit 0; sleep 1; done; exit 1'
check "nothing listens on 8090" bash -c '! curl -s -m 3 -o /dev/null http://127.0.0.1:8090/'

# ------------------------------------------------------------------ 3. upgrade
step "3. upgrade to $UP"
echo "keep-me" > /etc/uavlink/marker
echo "OPTION='kept'" >> /etc/uavlink/config.env
cp /etc/uavlink/config.env "$TMP/config.env.before"
states() { for u in uavlink-router.service uavlink-setup.service uavlink-rollback.timer; do echo "$u $(systemctl is-enabled $u 2>&1 | head -n1) $(systemctl is-active $u 2>&1 | head -n1)"; done; }
states > "$TMP/states.before"
cp -r "$REPO/out/repo-upgrade/." "$LOCAL/"
apt-get update -qq
check "$UP is the candidate" is_candidate "$UP"
if env -u DEBIAN_FRONTEND TERM=xterm bash -c 'script -qec "apt-get install -y --only-upgrade uavlink" /dev/null' >"$TMP/upgrade.out" 2>&1; then
  ok "apt-get install --only-upgrade uavlink"
else
  bad "upgrade"; tail -30 "$TMP/upgrade.out"
fi
check "version is now $UP" test "$(installed_version)" = "$UP"
check "upgrade did not print a setup URL (no browser/setup reopen)" bash -c "! grep -q '?t=' '$TMP/upgrade.out'"
check "setup service did not start again" bash -c '! systemctl is-active --quiet uavlink-setup.service && ! test -e /run/uavlink/setup.json'
check "/etc/uavlink kept" bash -c "test \"\$(cat /etc/uavlink/marker)\" = keep-me && cmp /etc/uavlink/config.env '$TMP/config.env.before'"
states > "$TMP/states.after"
check "services unchanged" diff "$TMP/states.before" "$TMP/states.after"

# ------------------------------------------------------------------ 5. secrets (before the purge, which deletes the logs)
step "5. no secret value in any log"
check "secrets.env holds the planted secrets (so the scan means something)" bash -c "grep -q '$secret_a' /etc/uavlink/secrets.env && grep -q '$secret_b' /etc/uavlink/secrets.env"
check "there are job logs to scan" bash -c 'ls /var/lib/uavlink/logs/*.log >/dev/null 2>&1'
check "no secret in /var/lib/uavlink/logs" bash -c "! grep -rF -e '$secret_a' -e '$secret_b' /var/lib/uavlink/logs"
check "no secret in the journal" bash -c "! journalctl --no-pager -q 2>/dev/null | grep -F -e '$secret_a' -e '$secret_b'"
check "no secret in the command output" bash -c "! grep -F -e '$secret_a' -e '$secret_b' '$TMP'/*.out '$TMP'/*.txt '$TMP'/*.jsonl '$TMP'/*.json '$TMP'/*.err"

fi
if [ "$PHASE" = purge ]; then
# ------------------------------------------------------------------ 4. purge + manifest restore
step "4. purge restores what the manifest recorded"
# apply is refused on WSL, so a fixture manifest stands in for a real apply (ARCHITECTURE.md 4.2).
fx=/etc/uavlink-fixture; bk=/var/lib/uavlink/backup
mkdir -p "$fx" "$bk"
echo "original content" > "$fx/present.conf"; cp -p "$fx/present.conf" "$bk/present.conf"
echo "changed by uavlink" > "$fx/present.conf"
echo "created by uavlink" > "$fx/created.conf"
printf 'file\t%s\tpresent\t%s\nfile\t%s\tabsent\t-\n' "$fx/present.conf" "$bk/present.conf" "$fx/created.conf" > /var/lib/uavlink/manifest.tsv
cp /usr/lib/uavlink/engine/cm5/uninstall-cm5.sh /var/lib/uavlink/uninstall.sh
# The uninstall script refuses on WSL; the override is only for this fixture.
if UAVLINK_ALLOW_WSL=1 apt-get purge -y uavlink uavlink-router > "$TMP/purge.out" 2>&1; then
  ok "apt-get purge -y uavlink uavlink-router"
else
  bad "purge"; tail -30 "$TMP/purge.out"
fi
check "/etc/uavlink gone" bash -c '! test -e /etc/uavlink'
check "/var/lib/uavlink gone" bash -c '! test -e /var/lib/uavlink'
check "fixture file restored from backup" bash -c "test \"\$(cat $fx/present.conf)\" = 'original content'"
check "fixture file created by uavlink removed" bash -c "! test -e $fx/created.conf"
check "packages removed" bash -c '! dpkg -s uavlink 2>/dev/null | grep -q "^Status: install" && ! dpkg -s uavlink-router 2>/dev/null | grep -q "^Status: install"'
check "program files gone" bash -c '! test -e /usr/bin/uavlink && ! test -e /usr/lib/uavlink && ! test -e /usr/lib/uavlink-router'
rm -rf "$fx"

fi

[ "$FAILS" -eq 0 ] && echo "IN-DISTRO: all checks passed" || echo "IN-DISTRO: $FAILS check(s) failed"
rm -rf "$TMP"
[ "$FAILS" -eq 0 ]
