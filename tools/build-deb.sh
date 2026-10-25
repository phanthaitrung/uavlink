#!/usr/bin/env bash
# Build the uavlink source + binary package into out/ and run lintian.
# Usage: bash tools/build-deb.sh   (Git Bash or Linux; on Windows it re-runs itself inside the uavlink-dev WSL distro)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh bash tools/build-deb.sh "$@"
fi
cd "$here"

work="$(mktemp -d /var/tmp/uavlink-build.XXXXXX)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/uavlink"

# Copy the working tree without .git and out/.
tar -c --exclude=./.git --exclude=./out --exclude=__pycache__ . | tar -x -C "$work/uavlink"
# The copy becomes the source package: scripts 0755, the rest 0644 (debian/rules does the same for installed files).
bash tools/normalise-modes.sh "$work/uavlink"

( cd "$work/uavlink" && dpkg-buildpackage -us -uc )

mkdir -p out
cp -f "$work"/uavlink_* out/
echo "== built"
ls -l out/uavlink_*

echo "== lintian"
if command -v lintian >/dev/null 2>&1; then
  lintian --pedantic --tag-display-limit 0 out/uavlink_*.changes || true
else
  echo "lintian not installed"
fi
