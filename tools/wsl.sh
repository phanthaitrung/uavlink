#!/usr/bin/env bash
# Run a command inside the uavlink-dev WSL distro, in the current directory.
# Usage: tools/wsl.sh [--root] <command> [args…]
# Inside Linux (WSL or native) it just runs the command.
set -euo pipefail

user=pi
if [ "${1:-}" = "--root" ]; then user=root; shift; fi
[ $# -gt 0 ] || { echo "usage: tools/wsl.sh [--root] <command…>" >&2; exit 2; }

if [ -n "${WSL_DISTRO_NAME:-}" ] || ! command -v wsl.exe >/dev/null 2>&1; then
  if [ "$user" = root ] && [ "$(id -u)" -ne 0 ]; then exec sudo -n "$@"; fi
  exec "$@"
fi

distro="${UAVLINK_WSL_DISTRO:-uavlink-dev}"
win="$(pwd -W 2>/dev/null || pwd)"      # Git Bash: D:/dynorch-projects/…
case "$win" in
  [A-Za-z]:/*) drive="$(printf %s "${win:0:1}" | tr '[:upper:]' '[:lower:]')"; lin="/mnt/$drive${win:2}" ;;
  *) lin="$win" ;;
esac
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' exec wsl.exe -d "$distro" -u "$user" --cd "$lin" -- "$@"
