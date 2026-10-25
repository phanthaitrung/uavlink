#!/usr/bin/env bash
# End-to-end test of the whole package in a fresh WSL distro, as the user will run it (see TESTING.md).
# Usage (Git Bash, repository root): bash tests/integration/run.sh [--keep] [--skip-build] [--shots-only]
#   --keep        keep the distro uavlink-it at the end (default: unregister it)
#   --skip-build  reuse the .deb files already in out/
#   --shots-only  only take the screenshots of the real page (needs a distro left by --keep)
# Result: "INTEGRATION: ALL CHECKS PASSED" and exit 0, or the failed checks and exit 1.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$here"
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

DISTRO="${UAVLINK_IT_DISTRO:-uavlink-it}"
IMAGE="${UAVLINK_IT_IMAGE:-D:\\wsl\\images\\ubuntu-2404-pristine.tar}"
DISTRO_DIR="${UAVLINK_IT_DIR:-D:\\wsl\\$DISTRO}"
keep=0; build=1; shots_only=0
for a in "$@"; do
  case "$a" in
    --keep) keep=1 ;;
    --skip-build) build=0 ;;
    --shots-only) shots_only=1 ;;
    *) echo "usage: run.sh [--keep] [--skip-build] [--shots-only]" >&2; exit 2 ;;
  esac
done

if [ -n "${WSL_DISTRO_NAME:-}" ] || ! command -v wsl.exe >/dev/null 2>&1; then
  echo "run.sh must be started from Git Bash on Windows" >&2; exit 2
fi

step() { printf '\n== %s\n' "$*"; }

# Linux path of a Windows-style path under /mnt.
lin_path() {
  local win; win="$(cd "$1" && pwd -W)"
  printf '/mnt/%s%s' "$(printf %s "${win:0:1}" | tr '[:upper:]' '[:lower:]')" "${win:2}"
}
in_distro() { wsl.exe -d "$DISTRO" -u root --cd /root -- "$@"; }
in_dev() { bash tools/wsl.sh "$@"; }

REPO_LIN="$(lin_path "$here")"
IT="$REPO_LIN/tests/integration"

register_distro() {
  if wsl.exe -l -q 2>/dev/null | tr -d '\0\r' | grep -qx "$DISTRO"; then
    echo "distro $DISTRO exists: unregistering it first"
    wsl.exe --unregister "$DISTRO" >/dev/null
  fi
  [ -f "$(cygpath -u "$IMAGE")" ] || { echo "missing pristine image $IMAGE" >&2; exit 1; }
  mkdir -p "$(cygpath -u "$DISTRO_DIR")"
  wsl.exe --import "$DISTRO" "$DISTRO_DIR" "$IMAGE" --version 2
  # Restart so that systemd (enabled in the image) is PID 1, then wait for it.
  wsl.exe --terminate "$DISTRO" >/dev/null 2>&1 || true
  in_distro bash "$IT/wait-systemd.sh"
}

KEEPALIVE_PID=""
cleanup() {
  [ -z "$KEEPALIVE_PID" ] || kill "$KEEPALIVE_PID" 2>/dev/null || true
  if [ "$keep" = 1 ]; then
    echo "distro $DISTRO kept (wsl -d $DISTRO -u root). Remove it: wsl --unregister $DISTRO"
  else
    wsl.exe --unregister "$DISTRO" >/dev/null 2>&1 || true
    echo "distro $DISTRO unregistered"
  fi
}

if [ "$shots_only" = 1 ]; then
  bash tests/integration/shots.sh "$DISTRO"
  exit $?
fi

if [ "$build" = 1 ]; then
  step "build uavlink"
  bash tools/build-deb.sh
  step "build uavlink-router"
  bash packaging/uavlink-router/build.sh
fi

step "local apt repository (out/repo)"
bash tests/integration/make-repo.sh

step "fresh distro $DISTRO"
register_distro
trap cleanup EXIT
# WSL stops a distro (and the services in it) about 15 s after the last attached session ends: hold one open.
wsl.exe -d "$DISTRO" -u root -- sleep 3600 >/dev/null 2>&1 &
KEEPALIVE_PID=$!

rc=0
in_distro bash "$IT/in-distro.sh" "$REPO_LIN" install || rc=$?

step "screenshots of the real page"
bash tests/integration/shots.sh "$DISTRO" || { echo "screenshots failed"; rc=1; }

in_distro bash "$IT/in-distro.sh" "$REPO_LIN" purge || rc=$?

if [ "$rc" = 0 ]; then
  echo; echo "INTEGRATION: ALL CHECKS PASSED"
else
  echo; echo "INTEGRATION: FAILED"
fi
exit "$rc"
