#!/usr/bin/env bash
# Undo everything uavlink changed on this CM5, as recorded in manifest.tsv (ARCHITECTURE.md section 4.2).
#   uninstall-cm5.sh [--preview] [--purge]
# Self-contained: apply copies it to /var/lib/uavlink/uninstall.sh and `apt purge` runs it after the package files
# are gone, so it uses nothing from the package except events.sh when that is still there.
# Never touches flight-controller (X25) parameters. Safe to run again. No manifest = nothing to do.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$here/events.sh" ]; then
  # shellcheck source=../lib/events.sh
  . "$here/events.sh"
elif [ -f "$here/../lib/events.sh" ]; then
  # shellcheck source=../lib/events.sh
  . "$here/../lib/events.sh"
else
  ev_step() { :; }; ev_check() { :; }; ev_change() { :; }; ev_info() { :; }; ev_rollback() { :; }; ev_emit() { :; }
fi

PREVIEW=0; PURGE=0
for a in "$@"; do
  case "$a" in
    --preview) PREVIEW=1 ;;
    --purge) PURGE=1 ;;
    *) echo "usage: uninstall-cm5.sh [--preview] [--purge]" >&2; exit 2 ;;
  esac
done

STATE_DIR="${UAVLINK_STATE_DIR:-/var/lib/uavlink}"
MANIFEST="$STATE_DIR/manifest.tsv"
BACKUP_DIR="${UAVLINK_BACKUP_DIR:-$STATE_DIR/backup}"
UFW_DIR="${UAVLINK_UFW_DIR:-/etc/ufw}"
NM_DIR="${UAVLINK_NM_DIR:-/etc/NetworkManager/system-connections}"
SSH_DIR="${UAVLINK_SSH_DIR:-/etc/ssh}"
UDEV_CONTROL="${UAVLINK_UDEV_CONTROL:-/run/udev/control}"
NM_WAIT="${UAVLINK_NM_WAIT:-30}"
OWN_UNITS=(uavlink-router.service uavlink-rollback.timer uavlink-rollback.service)

if [ "$PREVIEW" = 0 ] && [ "${UAVLINK_ALLOW_WSL:-0}" != 1 ] \
   && { [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; }; then
  echo "uninstall: refusing to change a WSL system (set UAVLINK_ALLOW_WSL=1 to override)" >&2
  ev_step uninstall fail env.wsl "refused on WSL"
  exit 4
fi

if [ ! -f "$MANIFEST" ]; then
  echo "uninstall: no manifest at $MANIFEST, nothing to do"
  ev_info uninstall.nothing "no manifest, nothing to undo"
  exit 0
fi

failed=0
say() { printf '%s%s\n' "$([ "$PREVIEW" = 1 ] && echo 'would: ')" "$*"; }
# run <command…>: execute unless previewing; a failure is reported and remembered, not fatal.
run() {
  [ "$PREVIEW" = 1 ] && return 0
  "$@" || { echo "uninstall: failed: $*" >&2; failed=1; return 1; }
}
# change <kind> <target> <action> <human text>
change() { ev_change "$1" "$2" "$3"; say "$4"; }

ssh_changed=0
restore_file() {
  local target="$1" prior="$2" backup="$3"
  case "$prior" in
    absent)
      [ -e "$target" ] || [ -L "$target" ] || return 0
      if [ -d "$target" ] && [ ! -L "$target" ]; then
        # A directory uavlink created: removed only when empty (flight logs stay).
        if [ -n "$(ls -A -- "$target" 2>/dev/null)" ]; then echo "uninstall: kept $target (not empty)"; return 0; fi
        change file "$target" delete "remove directory $target"
        run rmdir -- "$target"
        return 0
      fi
      change file "$target" delete "remove $target"
      run rm -f -- "$target" ;;
    present)
      if [ ! -e "$backup" ]; then echo "uninstall: backup $backup of $target is missing" >&2; failed=1; return 0; fi
      cmp -s -- "$backup" "$target" && return 0
      change file "$target" update "restore $target from $backup"
      run cp -p -- "$backup" "$target" ;;
    *) return 0 ;;
  esac
  case "$target" in "$SSH_DIR"/*) ssh_changed=1 ;; esac
}

restore_service() {
  local unit="$1" prior="$2"
  case "$prior" in
    enabled-active)    change service "$unit" enable  "enable and start $unit"; run systemctl enable "$unit"; run systemctl start "$unit" ;;
    enabled-inactive)  change service "$unit" enable  "enable $unit, leave stopped"; run systemctl enable "$unit"; run systemctl stop "$unit" ;;
    disabled-active)   change service "$unit" disable "disable $unit, leave running"; run systemctl disable "$unit"; run systemctl start "$unit" ;;
    disabled-inactive) change service "$unit" disable "disable and stop $unit"; run systemctl disable "$unit"; run systemctl stop "$unit" ;;
    masked)            change service "$unit" mask    "mask $unit"; run systemctl mask "$unit" ;;
    absent)            change service "$unit" disable "stop and disable $unit"; run systemctl stop "$unit"; run systemctl disable "$unit" ;;
  esac
}

# The rule files go back in both cases: an inactive ufw keeps the rules uavlink wrote otherwise.
restore_ufw() {
  local prior="$1" backup="$2" f
  case "$prior" in
    inactive) change firewall ufw disable "disable ufw"; run ufw --force disable ;;
    active) change firewall ufw update "restore ufw rules from $backup and reload" ;;
    *) return 0 ;;
  esac
  if [ -d "$backup" ]; then
    for f in "$backup"/*.rules "$backup"/ufw.conf; do [ -e "$f" ] && run cp -p -- "$f" "$UFW_DIR/"; done
  elif [ "$prior" = active ]; then
    echo "uninstall: ufw backup $backup is missing" >&2; failed=1
  fi
  [ "$prior" = active ] || return 0
  # The rollback timer may have disabled it: enable, else reload the restored rules.
  if grep -q '^Status: active' <<<"$(ufw status 2>/dev/null)"; then run ufw reload; else run ufw --force enable; fi
}

# NetworkManager answers? Wait for it (an upgrade may stop it for a moment); not answering is a failure, never
# "the connection is already gone".
nm_answers() {
  local i
  command -v nmcli >/dev/null 2>&1 || return 1
  for ((i = 0; i < NM_WAIT; i++)); do
    nmcli -t general status >/dev/null 2>&1 && return 0
    [ "$PREVIEW" = 1 ] && return 1
    if [ "$i" = 10 ]; then systemctl start NetworkManager >/dev/null 2>&1 || true; fi
    sleep 1
  done
  return 1
}

restore_nm() {
  local name="$1" prior="$2" backup="$3"
  if ! nm_answers; then
    echo "uninstall: NetworkManager does not answer, connection $name left as is" >&2; failed=1; return 0
  fi
  case "$prior" in
    absent)
      nmcli connection show "$name" >/dev/null 2>&1 || return 0
      change nm "$name" delete "delete NetworkManager connection $name"
      run nmcli connection delete "$name"
      return 0 ;;
    present)
      if [ "$backup" = - ]; then
        # Kept as a netplan file (/etc/netplan/90-NM-<uuid>.yaml, Ubuntu 24.04), put back by its own file line.
        change nm "$name" update "reload NetworkManager connection $name"
        if command -v netplan >/dev/null 2>&1; then run netplan generate; fi
      elif [ ! -e "$backup" ]; then
        echo "uninstall: backup $backup of connection $name is missing" >&2; failed=1; return 0
      else
        change nm "$name" create "restore NetworkManager connection $name"
        run cp -p -- "$backup" "$NM_DIR/$name.nmconnection"
        run chmod 600 "$NM_DIR/$name.nmconnection"
      fi
      run nmcli connection reload
      # Still active with the settings uavlink gave it: activate again with the restored ones.
      if grep -qxF "$name" <<<"$(nmcli -t -f NAME connection show --active 2>/dev/null)"; then
        run nmcli connection up "$name"
      fi ;;
    *) return 0 ;;
  esac
}

# group <user:group> absent: uavlink added the user to the group.
restore_group() {
  local member="$1" prior="$2" user group
  user="${member%%:*}"; group="${member#*:}"
  [ "$prior" = absent ] || return 0
  grep -qxF "$group" <<<"$(id -nG "$user" 2>/dev/null | tr ' ' '\n')" || return 0
  change user "$user" set "remove $user from group $group"
  run gpasswd -d "$user" "$group"
}

restore_user() {
  local user="$1" prior="$2"
  [ "$prior" = absent ] || return 0
  getent passwd "$user" >/dev/null 2>&1 || return 0
  change user "$user" delete "delete system user $user"
  run userdel "$user"
}

ev_step uninstall start
echo "== uavlink uninstall$([ "$PREVIEW" = 1 ] && echo ' (preview)')"

# Our own units first, so nothing runs while files are put back.
for u in "${OWN_UNITS[@]}"; do
  if [ "$PREVIEW" = 1 ] || grep -q "^$u" <<<"$(systemctl list-unit-files "$u" 2>/dev/null)"; then
    change service "$u" disable "stop and disable $u"
    run systemctl stop "$u"; run systemctl disable "$u"
  fi
done

# Only the first line per (kind, target) is the state before uavlink; process newest first.
entries="$(awk -F'\t' 'NF >= 3 && $1 !~ /^#/ && !seen[$1 FS $2]++' "$MANIFEST" | tac)"
for kind in file service ufw nm group user; do
  while IFS=$'\t' read -r k target prior backup; do
    [ "$k" = "$kind" ] || continue
    case "$kind" in
      file) restore_file "$target" "$prior" "${backup:--}" ;;
      service) restore_service "$target" "$prior" ;;
      ufw) restore_ufw "$prior" "${backup:--}" ;;
      nm) restore_nm "$target" "$prior" "${backup:--}" ;;
      group) restore_group "$target" "$prior" ;;
      user) restore_user "$target" "$prior" ;;
    esac
  done <<< "$entries"
done

run systemctl daemon-reload
# udevd does not run in a container: a reload that fails there leaves nothing undone.
if [ "$PREVIEW" != 1 ] && ! udevadm control --reload 2>/dev/null && [ -S "$UDEV_CONTROL" ]; then
  echo "uninstall: failed: udevadm control --reload" >&2; failed=1
fi
# sshd reads its configuration at start: without a reload the SSH hardening stays in force until a reboot.
if [ "$ssh_changed" = 1 ] && systemctl is-active --quiet ssh.service 2>/dev/null; then
  change service ssh.service restart "reload ssh"
  run systemctl reload ssh.service
fi

if [ "$PURGE" = 1 ]; then
  change file "$BACKUP_DIR" delete "delete backups $BACKUP_DIR"
  change file "$MANIFEST" delete "delete $MANIFEST"
  run rm -rf -- "$BACKUP_DIR"
  run rm -f -- "$MANIFEST"
fi

if [ "$failed" = 0 ]; then
  ev_step uninstall ok
  ev_emit result ok:=true
  exit 0
fi
ev_step uninstall fail uninstall.partial "some changes could not be undone"
ev_emit result ok:=false
exit 1
