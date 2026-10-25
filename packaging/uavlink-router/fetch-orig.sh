#!/usr/bin/env bash
# Create the reproducible upstream tarball of mavlink-router (needs network, not used at package build time).
# Usage: bash packaging/uavlink-router/fetch-orig.sh [--force]
# Writes out/uavlink-router_<version>.orig.tar.xz at the repository root. Runs inside the WSL distro when started on Windows.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh bash packaging/uavlink-router/fetch-orig.sh "$@"
fi

repo=https://github.com/mavlink-router/mavlink-router.git
commit=2362c620f483cef1edd574fb962a373a288e4b9e
version="4+git16.${commit:0:7}"      # v4-16-g2362c62
# Submodules the build needs. googletest is a system package and the tests are off, so it is not listed.
submodules=(modules/mavlink_c_library_v2)

out="$here/out"
orig="$out/uavlink-router_${version}.orig.tar.xz"
if [ -f "$orig" ] && [ "${1:-}" != --force ]; then
  echo "exists: $orig"
  exit 0
fi

work="$(mktemp -d /var/tmp/uavlink-router-orig.XXXXXX)"
trap 'rm -rf "$work"' EXIT
top="uavlink-router-$version"

git clone --quiet "$repo" "$work/clone"
git -C "$work/clone" -c advice.detachedHead=false checkout --quiet "$commit"
git -C "$work/clone" submodule update --init --quiet "${submodules[@]}"
mtime="$(git -C "$work/clone" log -1 --format=%ct "$commit")"

mkdir -p "$out"
mv "$work/clone" "$work/$top"
tar -C "$work" --exclude=.git --exclude=.github \
    --sort=name --mtime="@$mtime" --owner=0 --group=0 --numeric-owner \
    --mode='u+rw,go+r,go-w' --format=gnu -cf - "$top" | xz -9e -T1 > "$orig.tmp"
mv "$orig.tmp" "$orig"
echo "created: $orig"
sha256sum "$orig"
