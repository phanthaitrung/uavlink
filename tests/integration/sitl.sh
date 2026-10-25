#!/usr/bin/env bash
# The real X25 path end to end, with ArduPilot SITL (ArduCopter 4.7.1) standing in for the X25 (TESTING.md).
# Usage (repository root, Git Bash or inside the uavlink-dev distro): bash tests/integration/sitl.sh [--keep]
#   --keep  leave SITL and both routers running at the end; prints the ports and how to stop them
#
#   x25_configure.py ── TCP (SITL SERIAL0, plays the USB link) ──> SITL
#   SITL NET_P1 (UDP client) ──> local mavlink-routerd, config from install-cm5.sh --print-config (the CM5)
#        ──> fake VPS mavlink-routerd (UDP Server uplink, TCP server, UDP Server GCS port)
#        ──> outside TCP client (net_probe.py --mode live, mav_probe.py)
# Values: tests/fixtures/cm5-dgp/config.env (dgp-current) moved to loopback addresses and free ports. Nothing outside
# 127.0.0.0/8 is used: no interface, route or firewall change.
#
# SITL is built once outside the repository: $ARDUPILOT_DIR (default /var/tmp/ardupilot) at $ARDUPILOT_TAG (default
# Copter-4.7.1), build tools in $ARDUPILOT_DIR-venv. mavlink-routerd: $UAVLINK_TEST_ROUTERD, PATH,
# /usr/lib/uavlink-router, out/uavlink-router_*.deb, else built from out/uavlink-router_*.orig.tar.xz into
# /var/tmp/uavlink-sitl-routerd. Result: "SITL: ALL CHECKS PASSED" and exit 0, or the failed checks and exit 1.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here" || exit 2
  exec bash tools/wsl.sh bash tests/integration/sitl.sh "$@"
fi

keep=0
for a in "$@"; do
  case "$a" in
    --keep) keep=1 ;;
    *) echo "usage: sitl.sh [--keep]" >&2; exit 2 ;;
  esac
done

AP="${ARDUPILOT_DIR:-/var/tmp/ardupilot}"
TAG="${ARDUPILOT_TAG:-Copter-4.7.1}"
VENV="$AP-venv"
SITL_BIN="$AP/build/sitl/bin/arducopter"
X25_IP=127.0.50.1
# Not 127.0.50.2: SITL's NET_P1 socket is connected to CM5_LAN_IP and drops what comes from another address, and the
# router answers from 127.0.0.1 on loopback (on the CM5 it answers from 192.168.50.2), so commands would be lost.
CM5_LAN_IP=127.0.0.1
PASSPHRASE="sitl-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$here/vendor"
CHECK="$here/tests/integration/sitl_check.py"

WORK="$(mktemp -d /var/tmp/uavlink-sitl.XXXXXX)"
FAILS=0
PIDS=()
finished=0

ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
check() { local name="$1"; shift; if "$@" >"$WORK/last" 2>&1; then ok "$name"; else bad "$name"; sed 's/^/        | /' "$WORK/last" | tail -15; fi; }
step() { printf '\n== %s\n' "$*"; }
# has FILE LINE...: each LINE is a whole line of FILE. lacks REGEX FILE: no line matches (prints the ones that do).
has() {
  local f="$1" l; shift
  for l in "$@"; do grep -Fxq -- "$l" "$f" || { echo "missing: $l"; cat "$f"; return 1; }; done
}
lacks() { ! grep -E -- "$1" "$2"; }

stop_all() {
  local p
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
  for p in "${PIDS[@]}"; do
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || break; sleep 0.2; done
    kill -9 "$p" 2>/dev/null
  done
}

finish() {
  finished=1
  step "result"
  if [ "$keep" = 1 ] && [ "${#PIDS[@]}" -gt 0 ]; then
    echo "  kept running (pids ${PIDS[*]}), work dir $WORK"
    echo "  X25 USB link (SITL SERIAL0) : tcp:127.0.0.1:$USB_PORT"
    echo "  CM5 router TCP              : tcp:127.0.0.1:$CM5_TCP"
    echo "  fake VPS GCS TCP            : tcp:127.0.0.1:$VPS_TCP   (signed, passphrase in $WORK/etc/secrets.env)"
    echo "  stop: kill ${PIDS[*]}; rm -rf $WORK"
  else
    stop_all
    if [ "$FAILS" -eq 0 ]; then rm -rf "$WORK"; else echo "  logs kept in $WORK"; fi
  fi
  if [ "$FAILS" -eq 0 ]; then echo "SITL: ALL CHECKS PASSED"; exit 0; fi
  echo "SITL: $FAILS CHECK(S) FAILED"
  exit 1
}
die() { bad "$*"; finish; }
trap 'echo; bad "interrupted"; keep=0; finish' INT TERM
trap '[ "$finished" = 1 ] || { bad "stopped early"; keep=0; finish; }' EXIT

# A background process in its own session, under a shell that waits for it: on WSL, SITL ends when its parent exits
# with the wsl.exe session, so with --keep it needs a parent that stays. Its own pid goes to PIDS.
spawn() {
  local log="$1" i; shift
  rm -f "$log.pid"
  setsid bash -c '"$@" & echo "$!" > "$0.pid"; wait' "$log" "$@" > "$log" 2>&1 < /dev/null &
  for ((i = 0; i < 50; i++)); do [ -s "$log.pid" ] && break; sleep 0.1; done
  PIDS+=("$(cat "$log.pid")")
}

wait_tcp() {
  local port="$1" secs="$2" i
  for ((i = 0; i < secs * 10; i++)); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then return 0; fi
    sleep 0.1
  done
  return 1
}

# x25_configure.py with the events on; output in $WORK/$1.out, event lines in $WORK/$1.ev, exit code in $WORK/$1.rc.
run_x25() {
  local name="$1"; shift
  UAVLINK_EVENTS=1 UAVLINK_ALLOW_WSL=1 timeout 300 python3 "$here/engine/x25/x25_configure.py" "$@" \
    > "$WORK/$name.out" 2>&1
  echo "$?" > "$WORK/$name.rc"
  python3 "$CHECK" events "$WORK/$name.out" > "$WORK/$name.ev"
}
rc_is() { [ "$(cat "$WORK/$1.rc")" = "$2" ] || { tail -25 "$WORK/$1.out"; return 1; }; }

# ------------------------------------------------------------------ 0. SITL and mavlink-routerd
step "0. ArduPilot SITL $TAG and mavlink-routerd"
build_sitl() {
  if [ ! -d "$AP/.git" ]; then
    git clone --depth 1 --branch "$TAG" https://github.com/ArduPilot/ardupilot "$AP" || return 1
  fi
  git -C "$AP" submodule update --init --recursive --depth 1 --jobs 8 || return 1
  if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV" || return 1
    "$VENV/bin/pip" install -q "empy==3.3.4" pexpect future lxml setuptools pyserial dronecan || return 1
  fi
  (cd "$AP" && export PATH="$VENV/bin:$PATH" && ./waf configure --board sitl && ./waf copter)
}
if [ ! -x "$SITL_BIN" ]; then
  echo "  building SITL in $AP (once, a few minutes)"
  build_sitl > "$WORK/build.log" 2>&1 || { tail -30 "$WORK/build.log"; die "SITL build"; }
fi
check "ArduPilot checkout is at $TAG" bash -c "git -C '$AP' tag --points-at HEAD | grep -Fx '$TAG'"

find_routerd() {
  local c deb orig version dir
  for c in "${UAVLINK_TEST_ROUTERD:-}" "$(command -v mavlink-routerd 2>/dev/null)" /usr/lib/uavlink-router/mavlink-routerd; do
    if [ -n "$c" ] && [ -x "$c" ]; then echo "$c"; return 0; fi
  done
  deb="$(find "$here/out" -maxdepth 1 -name "uavlink-router_*_$(dpkg --print-architecture).deb" ! -name '*dbgsym*' \
    2>/dev/null | sort | tail -n1)"
  if [ -n "$deb" ] && dpkg-deb -x "$deb" "$WORK/routerd" 2>/dev/null; then
    c="$WORK/routerd/usr/lib/uavlink-router/mavlink-routerd"
    if [ -x "$c" ]; then echo "$c"; return 0; fi
  fi
  bash "$here/packaging/uavlink-router/fetch-orig.sh" >&2 || return 1
  orig="$(find "$here/out" -maxdepth 1 -name 'uavlink-router_*.orig.tar.xz' | sort | head -n1)"
  version="$(basename "$orig" .orig.tar.xz)"; version="${version#uavlink-router_}"
  dir="/var/tmp/uavlink-sitl-routerd/$version"
  if [ ! -x "$dir/build/src/mavlink-routerd" ]; then
    rm -rf "$dir"; mkdir -p "$dir"
    tar -C "$dir" -xf "$orig" && meson setup "$dir/build" "$dir/uavlink-router-$version" >&2 \
      && ninja -C "$dir/build" >&2 || return 1
  fi
  echo "$dir/build/src/mavlink-routerd"
}
ROUTERD="$(find_routerd)" || die "no mavlink-routerd (set UAVLINK_TEST_ROUTERD or run bash packaging/uavlink-router/build.sh)"
ok "mavlink-routerd: $ROUTERD"

ports="$(python3 "$CHECK" ports)" || die "no free ports"
eval "$ports"
ok "ports: USB $USB_PORT, CM5 TCP $CM5_TCP, LAN UDP $LAN_PORT, VPS TCP $VPS_TCP / uplink $UPLINK_PORT / GCS UDP $GCS_UDP_PORT, SITL -I $SITL_INSTANCE"

# ------------------------------------------------------------------ 1. configuration (dgp-current on loopback)
step "1. configuration: dgp-current on loopback"
mkdir -p "$WORK/etc" "$WORK/sitl" "$WORK/vps.d" "$WORK/cm5.d"
{
  cat "$here/tests/fixtures/cm5-dgp/config.env"
  echo "# tests/integration/sitl.sh: loopback addresses and free ports (later lines win)"
  echo "X25_SERIAL='tcp:127.0.0.1:$USB_PORT'"
  echo "X25_IP='$X25_IP'"
  echo "CM5_LAN_IP='$CM5_LAN_IP'"
  echo "CM5_ETH_EXTRA_ADDRS=''"
  echo "MAVLINK_LAN_PORT='$LAN_PORT'"
  echo "CM5_TCP_SERVER_PORT='$CM5_TCP'"
  echo "CM5_EXTRA_UDP_TARGETS=''"
  echo "CM5_INSTALL_TAILSCALE='0'"
  echo "CM5_ROUTER_LOG_DIR=''"
  echo "X25_BACKUP_DIR='$WORK/backups'"
  echo "VPS_PUBLIC_IP='127.0.0.1'"
  echo "VPS_UPLINK_PORT='$UPLINK_PORT'"
  echo "VPS_GCS_TCP_PORT='$VPS_TCP'"
  echo "VPS_GCS_UDP_PORT='$GCS_UDP_PORT'"
} > "$WORK/etc/config.env"
printf "MAVLINK_SIGNING_PASSPHRASE='%s'\n" "$PASSPHRASE" > "$WORK/etc/secrets.env"
chmod 600 "$WORK/etc/secrets.env"
export UAVLINK_CONFIG_DIR="$WORK/etc" UAVLINK_STATE_DIR="$WORK/state"

check "install-cm5.sh --print-config renders the CM5 router config" \
  bash -c "UAVLINK_EVENTS=0 bash '$here/engine/cm5/install-cm5.sh' --print-config > '$WORK/cm5.conf'"
check "CM5 router config: X25 UDP Server on $LAN_PORT, uplink to the fake VPS 127.0.0.1:$UPLINK_PORT" bash -c "
  grep -A3 '^\[UdpEndpoint x25_ethernet\]' '$WORK/cm5.conf' | grep -qx 'Port = $LAN_PORT' &&
  grep -A3 '^\[UdpEndpoint vps_mavlink\]' '$WORK/cm5.conf' | grep -qx 'Port = $UPLINK_PORT'"
cat > "$WORK/vps.conf" <<EOF
# Fake VPS relay: the endpoint kinds of the real one (tests/fixtures/net-fake-vps/vps.conf), other ports
[General]
TcpServerPort = $VPS_TCP
ReportStats = false
MavlinkDialect = ardupilotmega

[UdpEndpoint uplink]
Mode = Server
Address = 127.0.0.1
Port = $UPLINK_PORT

[UdpEndpoint gcs]
Mode = Server
Address = 127.0.0.1
Port = $GCS_UDP_PORT
EOF

# ------------------------------------------------------------------ 2. start
step "2. start the fake VPS, the CM5 router and SITL"
spawn "$WORK/vps.log" "$ROUTERD" -c "$WORK/vps.conf" -d "$WORK/vps.d"
spawn "$WORK/cm5.log" "$ROUTERD" -c "$WORK/cm5.conf" -d "$WORK/cm5.d"
check "fake VPS listens on TCP $VPS_TCP" wait_tcp "$VPS_TCP" 10
check "CM5 router listens on TCP $CM5_TCP" wait_tcp "$CM5_TCP" 10
# Fresh work dir = fresh eeprom.bin. SERIAL0 without ":wait": SITL boots at once, like a powered X25.
spawn "$WORK/sitl.log" bash -c "cd '$WORK/sitl' && exec '$SITL_BIN' --model quad --speedup 1 -I '$SITL_INSTANCE' \
  --defaults '$AP/Tools/autotest/default_params/copter.parm' --serial0 'tcp:$USB_PORT'"
SITL_PID="${PIDS[-1]}"
check "SITL SERIAL0 (the USB link) listens on TCP $USB_PORT" wait_tcp "$USB_PORT" 30
check "SITL starts with networking off (NET_ENABLE=0, NET_P1_TYPE=0)" \
  python3 "$CHECK" params "tcp:127.0.0.1:$USB_PORT" NET_ENABLE=0 NET_P1_TYPE=0

# ------------------------------------------------------------------ 3. x25_configure.py for real
step "3. x25_configure.py: backup, param rounds with reboots, signing, Ethernet check"
run_x25 apply
check "x25_configure.py exits 0" rc_is apply 0
check "connected over the USB link" has "$WORK/apply.ev" "check x25.usb ok x25.usb_found"
check "firmware reported as 4.7.1" has "$WORK/apply.ev" "check x25.firmware ok x25.firmware"
check "SITL runs ArduCopter V4.7.1" grep -q "ArduCopter V4.7.1" "$WORK/apply.out"
check "backup step ok" has "$WORK/apply.ev" "step x25.backup ok"
check "SITL has no NET_IPADDR*: info x25.ip_by_os" has "$WORK/apply.ev" "info x25.ip_by_os"
check "round 1 writes NET_ENABLE and NET_P1_TYPE" has "$WORK/apply.ev" "change NET_ENABLE 0 1" "change NET_P1_TYPE 0 1"
check "round 2 writes NET_P1_IP*, NET_P1_PORT, NET_P1_PROTOCOL" has "$WORK/apply.ev" "change NET_P1_IP0 0 127" \
  "change NET_P1_IP3 0 1" "change NET_P1_PORT 0 $LAN_PORT" "change NET_P1_PROTOCOL 0 2"
check "two reboots, both reconnected" bash -c "[ \"\$(grep -cFx 'step x25.reboot ok' '$WORK/apply.ev')\" = 2 ]"
check "params step ok, without 'no write needed'" has "$WORK/apply.ev" "step x25.params ok"
check "signing key loaded" has "$WORK/apply.ev" "step x25.signing ok x25.signing_set"
check "Ethernet check ok (signed read through the CM5 router, unsigned refused)" \
  has "$WORK/apply.ev" "step x25.ethcheck ok"
check "no failed step" lacks "^step [^ ]+ fail" "$WORK/apply.ev"
check "the passphrase is not in the output" lacks "$PASSPHRASE" "$WORK/apply.out"

step "4. state after the run"
check "one backup file, taken before any write (NET_ENABLE,0.0)" bash -c "
  [ \"\$(find '$WORK/backups' -name 'x25_params_*.parm' | wc -l)\" = 1 ] &&
  grep -qx 'NET_ENABLE,0.0' '$WORK'/backups/x25_params_*.parm"
check "params on SITL after the reboots (read unsigned over the USB link)" \
  python3 "$CHECK" params "tcp:127.0.0.1:$USB_PORT" NET_ENABLE=1 NET_P1_TYPE=1 NET_P1_PROTOCOL=2 \
  NET_P1_PORT="$LAN_PORT" NET_P1_IP0=127 NET_P1_IP1=0 NET_P1_IP2=0 NET_P1_IP3=1
check "SITL NET_P1 UDP client is connected to the CM5 router ($CM5_LAN_IP:$LAN_PORT)" bash -c "
  ss -Hunp | grep -F '$CM5_LAN_IP:$LAN_PORT' | grep -q 'pid=$SITL_PID,'"
check "CM5 router TCP: signed read answered, unsigned refused" \
  env MAVLINK_SIGNING_PASSPHRASE="$PASSPHRASE" python3 "$here/engine/tools/mav_probe.py" "tcp:127.0.0.1:$CM5_TCP" \
  --param NET_P1_PORT --check-signing
check "CM5 router TCP: wrong key refused" bash -c "! MAVLINK_SIGNING_PASSPHRASE=not-the-key python3 \
  '$here/engine/tools/mav_probe.py' 'tcp:127.0.0.1:$CM5_TCP' --param NET_P1_PORT --timeout 8"

# ------------------------------------------------------------------ 5. through the fake VPS
step "5. net_probe.py --mode live: X25 through the fake VPS, signed command back"
UAVLINK_EVENTS=1 UAVLINK_ROUTER_CONF="$WORK/cm5.conf" UAVLINK_INTERNET_HOSTS="127.0.0.1:$VPS_TCP" \
  UAVLINK_FALLBACK_SECRETS=/nonexistent python3 "$here/engine/tools/net_probe.py" --mode live > "$WORK/net.out" 2>&1
echo "$?" > "$WORK/net.rc"
python3 "$CHECK" events "$WORK/net.out" > "$WORK/net.ev"
check "net_probe.py exits 0" rc_is net 0
check "x25.lan_mavlink: X25 heartbeat through the CM5 router" has "$WORK/net.ev" "check x25.lan_mavlink ok x25.lan_mavlink.router"
check "vps.tcp ok" has "$WORK/net.ev" "check vps.tcp ok vps.tcp.ok"
check "vps.x25_via_vps ok" has "$WORK/net.ev" "check vps.x25_via_vps ok vps.x25_via_vps.ok"
check "vps.cmd_via_vps ok (signed NET_ENABLE read)" has "$WORK/net.ev" "check vps.cmd_via_vps ok vps.cmd_via_vps.ok"
check "cm5.listen ok" has "$WORK/net.ev" "check cm5.listen ok cm5.listen.ok"
check "no failed check" lacks "^check [^ ]+ fail" "$WORK/net.ev"
check "VPS TCP: signed read answered, unsigned refused" \
  env MAVLINK_SIGNING_PASSPHRASE="$PASSPHRASE" python3 "$here/engine/tools/mav_probe.py" "tcp:127.0.0.1:$VPS_TCP" \
  --param NET_P1_PORT --check-signing

# ------------------------------------------------------------------ 6. second run
step "6. x25_configure.py again: nothing to write, no reboot"
run_x25 again
check "second run exits 0" rc_is again 0
check "params ok, no write needed" has "$WORK/again.ev" "step x25.params ok x25.params_ok"
check "no param written, no reboot" lacks "^(change |step x25\.reboot)" "$WORK/again.ev"
check "Ethernet check ok again" has "$WORK/again.ev" "step x25.ethcheck ok"
check "SITL is still the same process (reboots are re-execs)" kill -0 "$SITL_PID"

finish
