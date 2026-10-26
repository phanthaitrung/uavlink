#!/usr/bin/env bash
# Make the local apt repositories used instead of the PPA (needs out/*.deb from the two build scripts).
#   out/repo          uavlink <changelog version> + uavlink-router
#   out/repo-upgrade  the same plus a locally bumped uavlink <version>+test1 (built from a temporary copy, nothing committed)
# Usage: bash tests/integration/make-repo.sh   (Git Bash or Linux; on Windows it re-runs itself inside uavlink-dev)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh bash tests/integration/make-repo.sh "$@"
fi
cd "$here"

ver="$(dpkg-parsechangelog -S Version)"
up="$ver+test1"
main_deb=""; router_deb=""
[ -f "out/uavlink_${ver}_all.deb" ] && main_deb="out/uavlink_${ver}_all.deb"
for f in out/uavlink-router_*_"$(dpkg --print-architecture)".deb; do    # out/ may also hold a cross-built arm64 deb
  case "$f" in *dbgsym*) ;; *) [ -f "$f" ] && router_deb="$f" ;; esac
done
[ -n "$main_deb" ] && [ -n "$router_deb" ] || { echo "missing out/uavlink_${ver}_all.deb or out/uavlink-router_*.deb: build first" >&2; exit 1; }

# $up: a copy of the tree with only debian/changelog changed.
work="$(mktemp -d /var/tmp/uavlink-bump.XXXXXX)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/uavlink"
tar -c --exclude=./.git --exclude=./out --exclude=__pycache__ . | tar -x -C "$work/uavlink"
bash tools/normalise-modes.sh "$work/uavlink"
{
  printf 'uavlink (%s) noble; urgency=medium\n\n  * Local test build for the upgrade check, never published.\n\n' "$up"
  printf ' -- tomphan <phanthaitrung@gmail.com>  Sat, 24 Oct 2026 13:00:00 +0700\n\n'
  cat "$work/uavlink/debian/changelog"
} > "$work/changelog.new"
mv "$work/changelog.new" "$work/uavlink/debian/changelog"
( cd "$work/uavlink" && dpkg-buildpackage -us -uc -b >"$work/build.log" 2>&1 ) || { tail -30 "$work/build.log" >&2; exit 1; }
bumped="$work/uavlink_${up}_all.deb"
[ -f "$bumped" ] || { echo "bumped build produced no deb" >&2; exit 1; }

index() {   # index <dir>: flat repository metadata
  ( cd "$1" && apt-ftparchive packages . > Packages && gzip -9kf Packages && apt-ftparchive release . > Release )
}

rm -rf out/repo out/repo-upgrade
mkdir -p out/repo out/repo-upgrade
cp "$main_deb" "$router_deb" out/repo/
cp "$main_deb" "$router_deb" "$bumped" out/repo-upgrade/
index out/repo
index out/repo-upgrade
echo "== out/repo";         ls out/repo
echo "== out/repo-upgrade"; ls out/repo-upgrade
