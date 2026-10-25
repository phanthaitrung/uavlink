#!/usr/bin/env bash
# Screenshots of the real setup page (server inside the test distro, preview only), Vietnamese and English,
# with headless Edge on Windows -> out/screenshots/real/<lang>-step<N>.png and <lang>-status.png
# Usage: bash tests/integration/shots.sh [distro]   (Git Bash; the distro needs uavlink installed)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$here"
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
DISTRO="${1:-uavlink-it}"
PORT=8090
OUT="$here/out/screenshots/real"
mkdir -p "$OUT"

edge=""
for c in "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" "/c/Program Files/Microsoft/Edge/Application/msedge.exe"; do
  [ -x "$c" ] && edge="$c" && break
done
[ -n "$edge" ] || { echo "Microsoft Edge not found" >&2; exit 1; }

in_distro() { wsl.exe -d "$DISTRO" -u root --cd /root -- "$@"; }

# WSL stops a distro (and the setup service in it) about 15 s after its last attached session ends: hold one open.
in_distro sleep 600 >/dev/null 2>&1 &
keepalive=$!
in_distro uavlink setup --no-browser > /dev/null
token="$(in_distro python3 -c 'import json; print(json.load(open("/run/uavlink/setup.json"))["token"])' | tr -d '\r')"
[ -n "$token" ] || { echo "no setup token" >&2; exit 1; }
stop_server() {
  in_distro curl -s -m 5 -b "uavlink=$token" -X POST -H "Origin: http://127.0.0.1:$PORT" -H 'Content-Type: application/json' \
    --data '{}' "http://127.0.0.1:$PORT/api/shutdown" >/dev/null 2>&1 || true
  kill "$keepalive" 2>/dev/null || true
}
trap stop_server EXIT

# Node drives Edge through the DevTools protocol: it clicks through the five steps and saves a PNG for each.
command -v node >/dev/null 2>&1 || { echo "node (22 or newer) is needed for the screenshots" >&2; exit 1; }
node tests/integration/shots.mjs "$(cygpath -w "$edge")" "$PORT" "$token" "$(cygpath -w "$OUT")"
echo "screenshots in $OUT"
