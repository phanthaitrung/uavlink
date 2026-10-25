#!/usr/bin/env bash
# A disposable Ubuntu 24.04 system container standing in for the CM5 (systemd-nspawn inside uavlink-dev), for real
# applies. Never apply on a WSL distro itself: all WSL2 distros share one network namespace, so the container
# gets its own (--private-network) and nothing is ever configured in the shared one.
#
#   container.sh up                create (from the pristine image) and boot the container, wait until SSH works
#   container.sh ssh [-o OPT]… [cmd]   SSH into it as pi, through its Internet NIC (a NEW connection every time)
#   container.sh exec <cmd…>      run a command as root on its "local console" (a service in the container, no SSH)
#   container.sh env              print its settings (ports, interfaces, paths) as shell assignments
#   container.sh save|load <name> keep / restore a copy of the stopped root file system (development)
#   container.sh down             power it off;   container.sh destroy   power off and delete its root file system
#
# Runs as root in uavlink-dev (Git Bash: re-runs itself there). Needs systemd-container, slirp4netns and
# libarchive-tools. In a container ModemManager never starts (ConditionVirtualization=!container) and udevd does not
# run; the CM5 has both.
# Root file system /var/lib/machines/uavlink-cm5 (from D:\wsl\images\ubuntu-2404-pristine.tar); keys and runtime
# files in /run/uavlink-ct (the SSH key: /root/.cache/uavlink-ct).
#
# Network, like the CM5 (Internet on Wi-Fi, a dedicated cable to the X25):
#   tap0  Internet and the default route, from slirp4netns (user-mode NAT inside the container's netns, as an
#         unprivileged process would get). The container is 10.0.2.100; 10.0.2.2 is the loopback of uavlink-dev,
#         so a fake VPS on 127.0.0.1 there is reached at 10.0.2.2. SSH: 127.0.0.1:$SSH_PORT is forwarded to port 22.
#   eth0  the X25 cable: a veth pair created inside the container's netns, its peer "x25" in a separate netns
#         ($X25_NETNS) with the X25 address 192.168.50.1/24, where the fake X25 runs (ip netns exec).
#   NetworkManager manages eth0 (as on the CM5); tap0 belongs to slirp4netns.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh --root bash tests/integration/container.sh "$@"
fi
if [ "$(id -u)" -ne 0 ]; then exec sudo -n bash "${BASH_SOURCE[0]}" "$@"; fi

M="${UAVLINK_CT_NAME:-uavlink-cm5}"
ROOT="/var/lib/machines/$M"
IMAGE="${UAVLINK_CT_IMAGE:-/mnt/d/wsl/images/ubuntu-2404-pristine.tar}"
RUN="/run/uavlink-ct/$M"
KEY_DIR=/root/.cache/uavlink-ct
KEY="$KEY_DIR/id_ed25519"
X25_NETNS="$M-x25"
PASSWORD="${UAVLINK_CT_PASSWORD:-uavlink-sandbox}"   # password login exists so the SSH hardening can be seen
UNIT="uavlink-ct-$M"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes
  -o ConnectTimeout=10 -o ServerAliveInterval=15)

log() { printf '[ct] %s\n' "$*" >&2; }

running() { machinectl show "$M" -p State --value 2>/dev/null | grep -qx running; }
leader() { machinectl show "$M" -p Leader --value; }

load_env() {
  [ -f "$RUN/ct.env" ] || { echo "container $M is not running (container.sh up)" >&2; exit 1; }
  # shellcheck disable=SC1091
  . "$RUN/ct.env"
}

free_port() {
  python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}

ct_ssh() {   # ct_ssh [-o OPTION]… [command]; ssh takes the first value of an option, so these come first
  load_env
  local opts=()
  while [ "${1:-}" = -o ]; do opts+=(-o "$2"); shift 2; done
  ssh "${opts[@]}" "${SSH_OPTS[@]}" -i "$KEY" -p "$SSH_PORT" pi@127.0.0.1 "$@"
}

# A command as root inside the container, as a transient service (no login session, no SSH): the local console.
ct_exec() {
  systemd-run -M "$M" -q --wait --pipe --collect --service-type=exec -- "$@"
}

create_rootfs() {
  [ -d "$ROOT/etc" ] && return 0
  [ -f "$IMAGE" ] || { echo "missing pristine image $IMAGE" >&2; exit 1; }
  log "extracting $IMAGE into $ROOT"
  mkdir -p "$ROOT"
  # bsdtar: the image comes from `wsl --export` (libarchive), with file capabilities (ping) as libarchive xattrs.
  bsdtar -xpf "$IMAGE" -C "$ROOT" --numeric-owner --xattrs
  # WSL leftovers: resolv.conf points into /mnt/wsl (slirp4netns serves DNS on 10.0.2.3), hosts is generated.
  rm -f "$ROOT/etc/resolv.conf" "$ROOT/etc/wsl.conf"
  printf 'nameserver 10.0.2.3\n' > "$ROOT/etc/resolv.conf"
  printf '127.0.0.1\tlocalhost\n127.0.1.1\t%s\n\n::1\tip6-localhost ip6-loopback\nff02::1\tip6-allnodes\nff02::2\tip6-allrouters\n' \
    "$M" > "$ROOT/etc/hosts"
  echo "$M" > "$ROOT/etc/hostname"
  rm -f "$ROOT/.provisioned"
}

boot() {
  local i
  rm -rf "$RUN"; mkdir -p "$RUN"
  systemctl reset-failed "$UNIT.service" "$UNIT-net.service" >/dev/null 2>&1 || true
  systemd-run -q --unit="$UNIT" --slice=machine.slice -p Delegate=yes -p KillMode=mixed -p Type=notify \
    -p NotifyAccess=all -- systemd-nspawn --quiet --boot --notify-ready=yes --directory="$ROOT" --machine="$M" \
    --private-network --resolv-conf=off --timezone=off --link-journal=no --console=read-only
  for ((i = 0; i < 60; i++)); do running && break; sleep 1; done
  running || { journalctl -u "$UNIT" -n 30 --no-pager >&2; echo "container did not start" >&2; exit 1; }
  local pid; pid="$(leader)"

  # Internet: slirp4netns puts tap0 (10.0.2.100/24, default route via 10.0.2.2) into the container's netns.
  systemd-run -q --unit="$UNIT-net" -- slirp4netns --configure --mtu=65520 --api-socket="$RUN/slirp.sock" "$pid" tap0
  for ((i = 0; i < 50; i++)); do [ -S "$RUN/slirp.sock" ] && break; sleep 0.2; done
  local port; port="$(free_port)"
  python3 - "$RUN/slirp.sock" "$port" <<'EOF'
import json, socket, sys
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
s.sendall(json.dumps({"execute": "add_hostfwd", "arguments": {"proto": "tcp", "host_addr": "127.0.0.1",
                     "host_port": int(sys.argv[2]), "guest_port": 22}}).encode())
answer = s.recv(4096).decode()
if "error" in answer:
    sys.exit("add_hostfwd: " + answer)
EOF

  # The X25 cable: both ends are created in private namespaces, never in the shared one.
  ip netns del "$X25_NETNS" 2>/dev/null || true
  ip netns add "$X25_NETNS"
  nsenter -t "$pid" -n ip link add eth0 type veth peer name x25 netns "$X25_NETNS"
  ip -n "$X25_NETNS" link set lo up
  ip -n "$X25_NETNS" addr add 192.168.50.1/24 dev x25
  ip -n "$X25_NETNS" link set x25 up

  {
    echo "SSH_PORT=$port"
    echo "LEADER=$pid"
    echo "IFACE_INET=tap0"
    echo "IFACE_X25=eth0"
    echo "X25_NETNS=$X25_NETNS"
    echo "CT_PASSWORD='$PASSWORD'"
    echo "ROOT=$ROOT"
    echo "KEY=$KEY"
  } > "$RUN/ct.env"
  for ((i = 0; i < 60; i++)); do
    case "$(systemctl -M "$M" is-system-running 2>/dev/null || true)" in running | degraded) break ;; esac
    sleep 1
  done
}

# First boot only: what the CM5 has and the pristine image lacks (SSH server, NetworkManager, ModemManager).
provision() {
  [ -f "$ROOT/.provisioned" ] && return 0
  log "provisioning (apt upgrade, openssh-server, network-manager, modemmanager)"
  mkdir -p "$KEY_DIR"; chmod 700 "$KEY_DIR"
  [ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N '' -C uavlink-ct -f "$KEY"
  install -d -m 700 -o 1000 -g 1000 "$ROOT/home/pi/.ssh"
  install -m 600 -o 1000 -g 1000 "$KEY.pub" "$ROOT/home/pi/.ssh/authorized_keys"
  # Upgraded first: an unattended upgrade during a test restarts systemd and NetworkManager under its feet.
  ct_exec bash -c 'export DEBIAN_FRONTEND=noninteractive
    apt-get -q update >/dev/null
    apt-get -q -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade >/dev/null
    apt-get -q install -y openssh-server network-manager modemmanager >/dev/null
    printf "network:\n  version: 2\n  renderer: NetworkManager\n" > /etc/netplan/01-network-manager-all.yaml
    chmod 600 /etc/netplan/01-network-manager-all.yaml
    netplan generate
    systemctl restart NetworkManager
    systemctl enable --now ssh.socket ModemManager.service >/dev/null 2>&1 || true
    echo "pi:'"$PASSWORD"'" | chpasswd'
  touch "$ROOT/.provisioned"
}

wait_ssh() {
  local i
  for ((i = 0; i < 60; i++)); do ct_ssh true 2>/dev/null && return 0; sleep 1; done
  echo "no SSH into the container" >&2; exit 1
}

up() {
  command -v systemd-nspawn >/dev/null && command -v slirp4netns >/dev/null && command -v bsdtar >/dev/null || {
    echo "missing tools: apt-get install systemd-container slirp4netns libarchive-tools" >&2; exit 1; }
  if running; then log "already running"; return 0; fi
  create_rootfs
  boot
  provision
  wait_ssh
}

down() {
  if running; then
    machinectl poweroff "$M" 2>/dev/null || true
    local i
    for ((i = 0; i < 60; i++)); do running || break; sleep 1; done
    running && machinectl terminate "$M" 2>/dev/null || true
  fi
  systemctl stop "$UNIT-net.service" "$UNIT.service" >/dev/null 2>&1 || true
  systemctl reset-failed "$UNIT.service" "$UNIT-net.service" >/dev/null 2>&1 || true
  ip netns del "$X25_NETNS" 2>/dev/null || true
  rm -rf "$RUN"
}

case "${1:-}" in
  up) up ;;
  ssh) shift; ct_ssh "$@" ;;
  exec) shift; ct_exec "$@" ;;
  env) load_env; cat "$RUN/ct.env" ;;
  save) [ -n "${2:-}" ] || exit 2; down; rm -rf "$ROOT.$2"; cp -a "$ROOT" "$ROOT.$2" ;;
  load) [ -n "${2:-}" ] && [ -d "$ROOT.$2" ] || { echo "no saved copy $ROOT.${2:-}" >&2; exit 2; }
        down; rm -rf "$ROOT"; cp -a "$ROOT.$2" "$ROOT"; up ;;
  down) down ;;
  destroy) down; rm -rf "$ROOT" ;;
  *) sed -n '2,13p' "$0" >&2; exit 2 ;;
esac
