#!/usr/bin/env bash
# Real apply of role cm5 end to end in a disposable Ubuntu 24.04 system container (systemd-nspawn in uavlink-dev,
# see container.sh and TESTING.md): install from a local apt repository, apply over SSH, confirm from a new SSH
# connection, verify, apply again (no changes), an unconfirmed rollback that must undo a firewall lockout, an apply
# from the local console, uninstall, apt purge, and the container back to its prior state.
#
# Usage (Git Bash or uavlink-dev, repository root): bash tests/integration/apply-container.sh [--keep] [--skip-build]
#   --keep            keep the container at the end (bash tests/integration/container.sh ssh | destroy)
#   --skip-build      reuse the .deb files in out/
#   --reuse NAME      development: start from the saved copy NAME (container.sh save NAME)
# Result: "APPLY-CONTAINER: ALL CHECKS PASSED" and exit 0, or the failed checks and exit 1. Logs: out/apply-container/.
#
# Sandbox: only the container changes, in its own network namespace. The X25 is a fake on the container's eth0
# (fake-x25.py), the VPS a local mavlink-routerd at 10.0.2.2 as seen from the container. Never the real CM5, VPS
# (209.97.167.92, mav.dgp.vn) or X25. No X25 on USB, so the apply skips stage x25 (T-021 covers it with SITL).
# The container runs the WSL kernel, so the engine sees WSL: UAVLINK_ALLOW_WSL=1 is set for it (safe in a private
# network namespace). ModemManager cannot start in a container (it is enabled but inactive), udevd does not run.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh --root bash tests/integration/apply-container.sh "$@"
fi
if [ "$(id -u)" -ne 0 ]; then exec sudo -n bash "${BASH_SOURCE[0]}" "$@"; fi
cd "$here"

keep=0; build=1; reuse=""
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) keep=1 ;;
    --skip-build) build=0 ;;
    --reuse) shift; reuse="${1:?--reuse needs a name}" ;;
    *) echo "usage: apply-container.sh [--keep] [--skip-build] [--reuse NAME]" >&2; exit 2 ;;
  esac
  shift
done

IT="$here/tests/integration"
OUT="$here/out/apply-container"
LTMP="$(mktemp -d /var/tmp/uavlink-applyct.XXXXXX)"
rm -rf "$OUT"; mkdir -p "$OUT"
FAILS=0
PIDS=()

ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
note() { printf '  note  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
check() {
  local name="$1"; shift
  if "$@" >"$LTMP/last" 2>&1; then ok "$name"; else bad "$name"; sed 's/^/        | /' "$LTMP/last" | tail -20; fi
}

ct() { bash "$IT/container.sh" "$@"; }
cssh() { ct ssh "$@"; }              # as pi over SSH through tap0 and ufw: every call is a NEW connection
cexec() { ct exec bash -c "$1"; }    # as root on the local console (no SSH)
free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
events() { python3 "$IT/sandbox_events.py" "$@"; }

finish() {
  local p
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  if [ "$keep" = 1 ]; then
    echo "container kept: bash tests/integration/container.sh ssh   (remove: bash tests/integration/container.sh destroy)"
  else
    ct destroy || true
    echo "container deleted"
  fi
  rm -rf "$LTMP"
  if [ "$FAILS" -eq 0 ]; then echo; echo "APPLY-CONTAINER: ALL CHECKS PASSED"; exit 0; fi
  echo; echo "APPLY-CONTAINER: $FAILS CHECK(S) FAILED (logs: out/apply-container/)"; exit 1
}
die() { bad "$*"; finish; }

# Methods sshd offers to a client without a key ("publickey,password" or "publickey").
auth_methods() {
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=10 \
    -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive -p "$SSH_PORT" \
    pi@127.0.0.1 true 2>&1 | sed -n 's/.*Permission denied (\(.*\)).*/\1/p'
}

snapshot() { ct exec bash -s < "$IT/sandbox-state.sh" > "$OUT/$1.state"; }

# state_diff <before> <after> [--allow REGEX]…: differences, without the files of packages installed or removed
# (uninstall does not remove packages).
state_diff() {
  local before="$OUT/$1.state" after="$OUT/$2.state" pkgs rc=0; shift 2
  pkgs="$(python3 "$IT/sandbox_state_diff.py" --changed-pkgs "$before" "$after" | tr '\n' ' ')"
  : > "$LTMP/pkgfiles"
  if [ -n "${pkgs// /}" ]; then cexec "dpkg -L $pkgs 2>/dev/null; true" > "$LTMP/pkgfiles"; fi
  {
    echo "packages changed: ${pkgs:-none}"
    python3 "$IT/sandbox_state_diff.py" "$before" "$after" --pkg-files "$LTMP/pkgfiles" "$@" || rc=$?
  } > "$OUT/diff-$(basename "$after" .state).txt"
  cat "$OUT/diff-$(basename "$after" .state).txt"
  return "$rc"
}

# ------------------------------------------------------------------ 0. packages and the container
if [ "$build" = 1 ]; then
  step "0. build the packages and the local apt repository"
  bash tools/build-deb.sh > "$OUT/build-uavlink.log" 2>&1 || { tail -30 "$OUT/build-uavlink.log"; die "build uavlink"; }
  bash packaging/uavlink-router/build.sh > "$OUT/build-router.log" 2>&1 || { tail -30 "$OUT/build-router.log"; die "build uavlink-router"; }
fi
if [ "$build" = 1 ] || [ ! -f out/repo/Packages ]; then
  bash tests/integration/make-repo.sh > "$OUT/make-repo.log" 2>&1 || { tail -30 "$OUT/make-repo.log"; die "make-repo"; }
fi
router_deb=""
for f in out/repo/uavlink-router_*.deb; do
  case "$f" in *dbgsym*) ;; *) router_deb="$f" ;; esac
done
[ -f "$router_deb" ] || die "no uavlink-router .deb in out/repo"

step "1. the container"
trap finish EXIT
if [ -n "$reuse" ]; then
  ct load "$reuse" > "$OUT/ct-up.log" 2>&1 || { tail -30 "$OUT/ct-up.log"; die "start from the saved copy $reuse"; }
else
  ct destroy
  ct up > "$OUT/ct-up.log" 2>&1 || { tail -30 "$OUT/ct-up.log"; die "create the container"; }
fi
ok "container booted, SSH works"
eval "$(ct env)"
# (systemd-detect-virt says "wsl" in there: the WSL kernel shows through, /run/systemd/container tells nspawn.)
check "this is a container with its own network namespace" cexec "[ \"\$(cat /run/systemd/container)\" = systemd-nspawn ] && [ \"\$(readlink /proc/1/ns/net)\" != '$(readlink /proc/1/ns/net)' ]"
check "NetworkManager manages the X25 port ($IFACE_X25), no address yet" cexec "nmcli -t -f DEVICE,STATE device | grep -q '^$IFACE_X25:' && ! nmcli -t -f DEVICE,STATE device | grep -q '^$IFACE_X25:unmanaged' && ! ip -4 -o addr show dev $IFACE_X25 | grep -q inet"
check "default route via $IFACE_INET" cexec "ip -4 route show default | grep -q 'dev $IFACE_INET'"
check "ModemManager enabled (it cannot start in a container)" cexec 'systemctl is-enabled --quiet ModemManager'

# ------------------------------------------------------------------ 2. the prior state
step "2. prior state: local apt repository, hand-built mavlink-router and x25-link (old deploy.sh)"
VPS_UPLINK="$(free_port)"; VPS_TCP="$(free_port)"; VPS_GCS_UDP="$(free_port)"
PASS="$(python3 -c 'import secrets, string; print("".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(32)))')"
rm -rf "$ROOT/srv/uavlink-repo"
cp -r out/repo "$ROOT/srv/uavlink-repo"
cp "$router_deb" "$ROOT/var/tmp/uavlink-router.deb"
ct exec bash -s "$VPS_UPLINK" > "$OUT/prep.log" 2>&1 <<'EOF' || bad "prepare the prior state (out/apply-container/prep.log)"
set -euo pipefail
uplink="$1"
# No unattended upgrade in the middle of the test (it restarts systemd and NetworkManager): stop the timers for
# this boot only (their enablement, which the snapshots compare, stays) and let a running one finish.
systemctl stop apt-daily.timer apt-daily-upgrade.timer
for _ in $(seq 1 600); do
  systemctl is-active --quiet apt-daily.service apt-daily-upgrade.service || break
  sleep 1
done
echo 'deb [trusted=yes] file:/srv/uavlink-repo ./' > /etc/apt/sources.list.d/uavlink-local.list
apt-get update -qq
# Ubuntu server ships ufw (inactive) and tcpdump; the WSL image does not.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw tcpdump >/dev/null
# The X25 cable as the old deploy.sh set it up: NetworkManager connection x25-link (a netplan file on 24.04).
nmcli connection add type ethernet con-name x25-link ifname eth0 ipv4.method manual ipv4.addresses 192.168.144.20/24 \
  ipv6.method disabled >/dev/null
nmcli connection up x25-link >/dev/null
# The router built by hand with the old deploy.sh: binary in /usr/bin, its own unit and main.conf.
rm -rf /var/tmp/router-x
dpkg-deb -x /var/tmp/uavlink-router.deb /var/tmp/router-x
install -m 755 /var/tmp/router-x/usr/lib/uavlink-router/mavlink-routerd /usr/bin/mavlink-routerd
rm -rf /var/tmp/router-x /var/tmp/uavlink-router.deb
mkdir -p /etc/mavlink-router
cat > /etc/mavlink-router/main.conf <<CONF
# hand-built (cm5-x25-install deploy.sh)
[General]
TcpServerPort = 5760
ReportStats = false
MavlinkDialect = ardupilotmega

[UdpEndpoint x25_ethernet]
Mode = Server
Address = 0.0.0.0
Port = 14550

[UdpEndpoint vps_mavlink]
Mode = Normal
Address = 10.0.2.2
Port = $uplink
CONF
cat > /etc/systemd/system/mavlink-router.service <<'UNIT'
[Unit]
Description=MAVLink Router (hand-built)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/mavlink-routerd -c /etc/mavlink-router/main.conf
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now mavlink-router.service
EOF
check "x25-link of deploy.sh active with 192.168.144.20/24, stored as a netplan file" cexec "nmcli -t -f NAME,DEVICE connection show --active | grep -qx 'x25-link:$IFACE_X25' && ip -4 -o addr show dev $IFACE_X25 | grep -q ' 192.168.144.20/24 ' && ls /etc/netplan/90-NM-*.yaml"
check "hand-built mavlink-router.service enabled and running" cexec 'systemctl is-enabled --quiet mavlink-router && systemctl is-active --quiet mavlink-router'
check "sshd accepts passwords before uavlink" test "$(auth_methods)" = "publickey,password"
snapshot prior

# ------------------------------------------------------------------ 3. install
step "3. install from the local apt repository"
if cssh 'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y uavlink' > "$OUT/install.log" 2>&1; then
  ok "apt-get install -y uavlink"
else
  bad "apt-get install -y uavlink"; tail -20 "$OUT/install.log"
fi
check "uavlink-router came as a dependency" cexec 'dpkg -s uavlink-router | grep -q "Status: install ok installed"'
snapshot installed
check "installing changed nothing (beyond package files and its own empty directories)" state_diff prior installed \
  --allow '^file	/etc/uavlink$' --allow '^file	/var/lib/uavlink$'

# ------------------------------------------------------------------ 4. fake VPS and fake X25
step "4. fake VPS (10.0.2.2 from the container) and fake X25 (192.168.50.1 on $IFACE_X25)"
dpkg-deb -x "$router_deb" "$LTMP/routerd"
cat > "$LTMP/vps.conf" <<EOF
[General]
TcpServerPort = $VPS_TCP
ReportStats = false
MavlinkDialect = ardupilotmega

[UdpEndpoint uplink]
Mode = Server
Address = 127.0.0.1
Port = $VPS_UPLINK

[UdpEndpoint gcs]
Mode = Server
Address = 127.0.0.1
Port = $VPS_GCS_UDP
EOF
mkdir -p "$LTMP/vps.d"
"$LTMP/routerd/usr/lib/uavlink-router/mavlink-routerd" -c "$LTMP/vps.conf" -d "$LTMP/vps.d" > "$OUT/fake-vps.log" 2>&1 &
PIDS+=($!)
FAKE_X25_PASSPHRASE="$PASS" ip netns exec "$X25_NETNS" python3 -u "$IT/fake-x25.py" 192.168.50.2:14550 > "$OUT/fake-x25.log" 2>&1 &
PIDS+=($!)
check "fake VPS listens on TCP $VPS_TCP" bash -c "for i in \$(seq 1 50); do (exec 3<>/dev/tcp/127.0.0.1/$VPS_TCP) 2>/dev/null && exit 0; sleep 0.2; done; exit 1"
echo "  uplink UDP $VPS_UPLINK, GCS TCP $VPS_TCP, GCS UDP $VPS_GCS_UDP"

# The overrides of the sandbox. The GCS ports of the VPS are vps-role keys, so net_probe takes them from the
# environment (sudo keeps variables given on its command line).
SETS="--set CM5_ETH_IFACE=$IFACE_X25 --set VPS_PUBLIC_IP=10.0.2.2 --set VPS_UPLINK_PORT=$VPS_UPLINK --set CM5_EXTRA_UDP_TARGETS= --set CM5_INSTALL_TAILSCALE=0"
ENGINE_ENV="UAVLINK_ALLOW_WSL=1 VPS_GCS_TCP_PORT=$VPS_TCP VPS_GCS_UDP_PORT=$VPS_GCS_UDP"
# The X25 already enforces signing with this passphrase (configuring it is the x25 stage, not run here).
printf "MAVLINK_SIGNING_PASSPHRASE='%s'\n" "$PASS" | cexec 'install -D -m 600 /dev/stdin /etc/uavlink/secrets.env'

# apply <name> [--set …]: `sudo uavlink apply` over a new SSH connection; events in out/apply-container/<name>.jsonl.
apply() {
  local name="$1" rc=0; shift
  cssh "sudo $ENGINE_ENV uavlink apply --preset dgp-current --yes $SETS $* --json" > "$OUT/$name.jsonl" 2> "$OUT/$name.err" || rc=$?
  echo "$rc" > "$OUT/$name.rc"
  events summary "$OUT/$name.jsonl" > "$OUT/$name.summary"
  sed 's/^/        /' "$OUT/$name.summary"
}
rc_of() { cat "$OUT/$1.rc"; }
no_failed_checks() { local f; f="$(events checks "$OUT/$1.jsonl")"; [ -z "$f" ] || { echo "$f"; return 1; }; }
no_changes() { local c; c="$(events changes "$OUT/$1.jsonl")"; [ -z "$c" ] || { echo "$c"; return 1; }; }
manifest_has() { cexec "awk -F'\t' -v k='$1' -v t='$2' -v s='$3' '\$1 == k && \$2 == t && \$3 == s {f = 1} END {exit !f}' /var/lib/uavlink/manifest.tsv"; }
timer_active() { cexec 'systemctl is-active --quiet uavlink-rollback.timer'; }
router_user() { cexec 'stat -c %U /proc/$(systemctl show -p MainPID --value uavlink-router)'; }

# ------------------------------------------------------------------ 5. first apply
step "5. first apply (over SSH)"
apply apply1
check "apply exits 0" test "$(rc_of apply1)" = 0
check "stage cm5 ok" test "$(events stage "$OUT/apply1.jsonl" cm5)" = ok
check "stage x25 skipped (props not confirmed, no X25 on USB)" test "$(events stage "$OUT/apply1.jsonl" x25)" = skip
check "stage verify ok" test "$(events stage "$OUT/apply1.jsonl" verify)" = ok
check "no failed check" no_failed_checks apply1
check "rollback armed (180 s)" test "$(events rollback "$OUT/apply1.jsonl")" = 180
check "rollback timer still running after an apply over SSH (only a new connection may confirm)" timer_active
check "a NEW SSH connection works (ufw + SSH hardening)" cssh true
check "uavlink confirm from that new connection" cssh 'sudo uavlink confirm'
check "rollback timer stopped" bash -c "! bash '$IT/container.sh' exec systemctl is-active --quiet uavlink-rollback.timer"
check "sshd refuses passwords now" test "$(auth_methods)" = "publickey"
check "ufw active: SSH, the X25 port, tailscale" cexec "ufw status verbose | grep -q '^Status: active' && ufw status verbose | grep -Eq '^22/tcp +ALLOW IN' && ufw status verbose | grep -Eq 'Anywhere on $IFACE_X25 +ALLOW IN' && ufw status verbose | grep -Eq 'Anywhere on tailscale0 +ALLOW IN'"
check "uavlink-router running as mavlink-router" test "$(router_user)" = mavlink-router
check "hand-built mavlink-router disabled and stopped" cexec '! systemctl is-enabled --quiet mavlink-router && ! systemctl is-active --quiet mavlink-router'
check "ModemManager disabled" cexec '! systemctl is-enabled --quiet ModemManager'
check "NetworkManager connection x25-link active on $IFACE_X25" cexec "nmcli -t -f NAME,DEVICE connection show --active | grep -qx 'x25-link:$IFACE_X25'"
check "$IFACE_X25 has 192.168.50.2/24 and 192.168.144.2/24" cexec "ip -4 -o addr show dev $IFACE_X25 | grep -q ' 192.168.50.2/24 ' && ip -4 -o addr show dev $IFACE_X25 | grep -q ' 192.168.144.2/24 '"
check "default route still via $IFACE_INET only" cexec "ip -4 route show default | grep -q 'dev $IFACE_INET' && ! ip -4 route show default | grep -q 'dev $IFACE_X25'"
check "router config points at the fake VPS" cexec "grep -q 'Address = 10.0.2.2' /etc/mavlink-router/main.conf && grep -q 'Port = $VPS_UPLINK' /etc/mavlink-router/main.conf"
check "manifest: main.conf was present (backup kept)" manifest_has file /etc/mavlink-router/main.conf present
check "manifest: hand-built router was enabled-active" manifest_has service mavlink-router.service enabled-active
check "manifest: ModemManager was enabled-inactive" manifest_has service ModemManager.service enabled-inactive
check "manifest: ufw was inactive" manifest_has ufw ufw inactive
check "manifest: x25-link was present" manifest_has nm x25-link present
check "manifest: its netplan file /etc/netplan/90-NM-<uuid>.yaml backed up" cexec "grep -Eq '^file	/etc/netplan/90-NM-[0-9a-f-]+\.yaml	present	/' /var/lib/uavlink/manifest.tsv"
check "manifest: CM5_USER added to dialout" manifest_has group pi:dialout absent
check "manifest: user mavlink-router was absent" manifest_has user mavlink-router absent
check "uninstall.sh copied for apt purge" cexec 'test -x /var/lib/uavlink/uninstall.sh && test -f /var/lib/uavlink/events.sh'
check "applied.json written" cexec 'grep -q dgp-current /var/lib/uavlink/applied.json'
check "fake X25 answered signed parameter reads" grep -Eq 'answered=[1-9]' "$OUT/fake-x25.log"
cexec 'cat /var/lib/uavlink/manifest.tsv' > "$OUT/manifest.apply1.tsv"

step "6. uavlink verify (new SSH connection)"
rc=0; cssh "sudo $ENGINE_ENV uavlink verify --json" > "$OUT/verify.jsonl" 2>&1 || rc=$?
events summary "$OUT/verify.jsonl" | sed 's/^/        /'
check "verify exits 0" test "$rc" = 0
check "verify: no failed check" no_failed_checks verify
check "verify ran the acceptance and network checks" bash -c "grep -q '\"id\": \"mavlink.two_way\"' '$OUT/verify.jsonl' && grep -q '\"id\": \"vps.x25_via_vps\"' '$OUT/verify.jsonl'"

# ------------------------------------------------------------------ 7. apply again: nothing changes
step "7. second apply: no changes"
pid_before="$(cexec 'systemctl show -p MainPID --value uavlink-router')"
con_before="$(cexec "nmcli -g GENERAL.CON-PATH device show $IFACE_X25")"
apply apply2
check "apply exits 0" test "$(rc_of apply2)" = 0
check "no change events" no_changes apply2
check "no rollback armed" test -z "$(events rollback "$OUT/apply2.jsonl")"
check "no failed check" no_failed_checks apply2
check "router not restarted" test "$(cexec 'systemctl show -p MainPID --value uavlink-router')" = "$pid_before"
check "x25-link not re-activated" test "$(cexec "nmcli -g GENERAL.CON-PATH device show $IFACE_X25")" = "$con_before"
cexec 'cat /var/lib/uavlink/manifest.tsv' > "$OUT/manifest.apply2.tsv"
check "manifest unchanged" cmp "$OUT/manifest.apply1.tsv" "$OUT/manifest.apply2.tsv"

# ------------------------------------------------------------------ 8. unconfirmed rollback
step "8. unconfirmed rollback undoes a firewall change that locks SSH out"
# Safety net of the sandbox only: if the uavlink rollback never fires, this one opens SSH again after 10 min.
cexec "systemd-run --quiet --unit=sandbox-guard --on-active=600 /bin/sh -c 'ufw --force disable; rm -f /etc/ssh/sshd_config.d/10-uavlink-hardening.conf; systemctl reload ssh'"
secs=120
t0="$(date +%s)"
apply rollback --set FIREWALL_ROLLBACK_SECS=$secs --set CM5_SSH_ALLOW_LAN=0
t1="$(date +%s)"
check "apply exits 0" test "$(rc_of rollback)" = 0
check "rollback armed ($secs s)" test "$(events rollback "$OUT/rollback.jsonl")" = "$secs"
if [ $((t1 - t0)) -lt $((secs - 15)) ]; then
  check "a new SSH connection is now blocked (port 22 closed by the new rules)" bash -c "! bash '$IT/container.sh' ssh -o ConnectTimeout=5 true"
else
  note "the apply took $((t1 - t0)) s, too long to see the lockout before the rollback"
fi
back=""
for _ in $(seq 1 60); do
  if cssh -o ConnectTimeout=5 true 2>/dev/null; then back="$(date +%s)"; break; fi
  sleep 5
done
if [ -n "$back" ]; then
  ok "SSH reachable again $((back - t0)) s after the apply started"
  check "it was the uavlink rollback, not the sandbox guard" test $((back - t0)) -lt 500
else
  bad "SSH did not come back"
fi
check "ufw disabled by the rollback" cexec "ufw status | grep -q '^Status: inactive'"
check "SSH hardening removed by the rollback" test "$(auth_methods)" = "publickey,password"
check "rollback timer gone" bash -c "! bash '$IT/container.sh' exec systemctl is-active --quiet uavlink-rollback.timer"
cexec 'systemctl stop sandbox-guard.timer 2>/dev/null; systemctl reset-failed sandbox-guard.service 2>/dev/null; true'

step "9. apply again from the local console (no SSH): it confirms itself"
rc=0
cexec "env $ENGINE_ENV uavlink apply --preset dgp-current --yes $SETS --json" > "$OUT/apply3.jsonl" 2> "$OUT/apply3.err" || rc=$?
events summary "$OUT/apply3.jsonl" | sed 's/^/        /'
check "apply exits 0" test "$rc" = 0
check "rollback armed again" test "$(events rollback "$OUT/apply3.jsonl")" = 180
check "rollback confirmed by the CLI (local console)" bash -c "! bash '$IT/container.sh' exec systemctl is-active --quiet uavlink-rollback.timer"
check "a NEW SSH connection works" cssh true
check "ufw active again" cexec "ufw status | grep -q '^Status: active'"
check "sshd refuses passwords again" test "$(auth_methods)" = "publickey"
cexec 'cat /var/lib/uavlink/manifest.tsv' > "$OUT/manifest.apply3.tsv"
check "manifest still starts with the states before uavlink" bash -c "head -n \$(wc -l < '$OUT/manifest.apply1.tsv') '$OUT/manifest.apply3.tsv' | cmp - '$OUT/manifest.apply1.tsv'"
snapshot applied

step "10. no secret in logs"
check "passphrase not in /var/lib/uavlink/logs, /var/log or the journal" bash -c "printf '%s' '$PASS' | bash '$IT/container.sh' exec bash -c 'p=\$(cat); ! grep -rqF -- \"\$p\" /var/lib/uavlink/logs /var/log && ! journalctl --no-pager -q | grep -qF -- \"\$p\"'"
check "passphrase not in the command output" bash -c "! grep -rqF -- '$PASS' '$OUT'"

# ------------------------------------------------------------------ 11. uninstall
step "11. uavlink uninstall (over SSH)"
rc=0; cssh "sudo UAVLINK_ALLOW_WSL=1 uavlink uninstall --yes" > "$OUT/uninstall.log" 2>&1 || rc=$?
check "uninstall exits 0" test "$rc" = 0
[ "$rc" = 0 ] || tail -20 "$OUT/uninstall.log"
check "hand-built mavlink-router enabled and running again" cexec 'systemctl is-enabled --quiet mavlink-router && systemctl is-active --quiet mavlink-router'
check "its main.conf is back" cexec "grep -q '^# hand-built' /etc/mavlink-router/main.conf"
check "uavlink-router stopped and disabled" cexec '! systemctl is-active --quiet uavlink-router && ! systemctl is-enabled --quiet uavlink-router'
check "ModemManager enabled again" cexec 'systemctl is-enabled --quiet ModemManager'
check "ufw inactive" cexec "ufw status | grep -q '^Status: inactive'"
check "x25-link back to the deploy.sh settings" cexec "[ \"\$(nmcli -g ipv4.addresses connection show x25-link)\" = 192.168.144.20/24 ] && ip -4 -o addr show dev $IFACE_X25 | grep -q ' 192.168.144.20/24 ' && ! ip -4 -o addr show dev $IFACE_X25 | grep -q ' 192.168.50.2/'"
check "pi no longer in dialout" cexec "! id -nG pi | grep -qw dialout"
check "user mavlink-router deleted" cexec '! getent passwd mavlink-router'
check "sshd accepts passwords again (as before)" test "$(auth_methods)" = "publickey,password"
snapshot uninstalled
# Apply swaps systemd-timesyncd for chrony (apt removes one when it installs the other) and uninstall leaves
# packages alone (ARCHITECTURE.md 4.2). What the chrony package scripts create and what the removed timesyncd leaves
# differ by design; the files dpkg lists are left out by state_diff already.
PKG_ALLOW=(--allow '^(user|group)	_chrony$' --allow '^file	/etc/chrony/' --allow '^file	/etc/systemd/system/chronyd\.service$'
  --allow '^unitfile	chronyd\.service$' --allow '^(unit|unitfile)	systemd-timesyncd\.service$'
  --allow '^unitfile	dbus-org\.freedesktop\.timesync1\.service$')
# Left until apt purge: the configuration and the state directory (manifest, backups, logs).
check "back to the prior state (except config and state, kept until purge)" state_diff prior uninstalled \
  --allow '^file	/etc/uavlink(/|$)' --allow '^file	/var/lib/uavlink(/|$)' "${PKG_ALLOW[@]}"

# ------------------------------------------------------------------ 12. apt purge
step "12. apt purge"
rc=0; cssh 'sudo UAVLINK_ALLOW_WSL=1 DEBIAN_FRONTEND=noninteractive apt-get purge -y uavlink uavlink-router' > "$OUT/purge.log" 2>&1 || rc=$?
check "apt-get purge exits 0" test "$rc" = 0
check "/etc/uavlink and /var/lib/uavlink gone" cexec '! test -e /etc/uavlink && ! test -e /var/lib/uavlink'
check "hand-built mavlink-router still running" cexec 'systemctl is-active --quiet mavlink-router'
snapshot purged
check "back to the prior state" state_diff prior purged "${PKG_ALLOW[@]}"
