#!/usr/bin/env bash
# Snapshot of the system state that uavlink apply/uninstall may touch, one "section<TAB>key<TAB>value" line each.
# Runs as root inside the sandbox container (apply-container.sh), compared with sandbox_state_diff.py.
set -uo pipefail
export LC_ALL=C

units=(uavlink-router.service mavlink-router.service uavlink-setup.service uavlink-rollback.timer
  uavlink-rollback.service ModemManager.service chrony.service systemd-timesyncd.service
  unattended-upgrades.service ufw.service ssh.service ssh.socket NetworkManager.service tailscaled.service)

# Files under /etc (and the uavlink state dirs): type, mode, owner, content hash or link target.
# Account databases are compared as entries below (their backups and lock files change on every useradd).
find /etc /var/lib/uavlink /var/log/mavlink-router -xdev \
  \( -path /etc/ld.so.cache -o -path /etc/.pwd.lock -o -name 'passwd-' -o -name 'group-' -o -name 'shadow-' \
     -o -name 'gshadow-' -o -path /etc/passwd -o -path /etc/group -o -path /etc/shadow -o -path /etc/gshadow \
     -o -path /etc/subuid -o -path /etc/subgid -o -path '/etc/subuid-' -o -path '/etc/subgid-' \) -prune \
  -o -printf '%p\t%y\t%m\t%u:%g\t%l\n' 2>/dev/null | sort | while IFS=$'\t' read -r p y m o l; do
  case "$y" in
    f) h="$(sha256sum "$p" 2>/dev/null | cut -c1-16)" ;;
    l) h="-> $l" ;;
    *) h="" ;;
  esac
  printf 'file\t%s\t%s %s %s %s\n' "$p" "$y" "$m" "$o" "$h"
done

for u in "${units[@]}"; do
  printf 'unit\t%s\t%s %s\n' "$u" "$(systemctl is-enabled "$u" 2>/dev/null | head -n1 || true)" \
    "$(systemctl is-active "$u" 2>/dev/null | head -n1 || true)"
done
systemctl list-unit-files --no-legend --type=service,timer,socket 2>/dev/null \
  | awk '$2 != "transient" {print "unitfile\t" $1 "\t" $2}'

ufw status verbose 2>/dev/null | awk 'NF {print "ufw\t" NR "\t" $0}'

nmcli -t -f NAME,TYPE,AUTOCONNECT connection show 2>/dev/null | sort | awk -F: '{print "nm\t" $1 "\t" $2 " " $3}'

getent passwd | awk -F: '{print "user\t" $1 "\t" $3 ":" $4 " " $6 " " $7}'
getent group | awk -F: '{print "group\t" $1 "\t" $3 " " $4}'

dpkg-query -W -f '${Package}\t${Version} ${db:Status-Abbrev}\n' 2>/dev/null | awk '{print "pkg\t" $0}'

sshd -T 2>/dev/null | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin|x11forwarding|logingracetime) ' \
  | awk '{print "sshd\t" $1 "\t" $2}'

ip -4 -o addr show 2>/dev/null | awk '{print "addr\t" $2 " " $4 "\t"}'
ip -4 route show default 2>/dev/null | awk '{print "route\tdefault\t" $0}'
