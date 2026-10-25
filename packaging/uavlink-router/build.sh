#!/usr/bin/env bash
# Build the uavlink-router packages with the network cut off, check the binary runs, then run lintian.
# Usage: bash packaging/uavlink-router/build.sh [--arch ARCH]
#   no --arch (or the build machine's arch): source + binary packages,
#     out/uavlink-router_*.{dsc,orig.tar.xz,debian.tar.xz} and *_<arch>.{deb,changes,buildinfo}
#   --arch arm64 (any other Debian arch): cross build of the binary package only (-B -Pcross,nocheck),
#     out/uavlink-router_*_arm64.{deb,changes,buildinfo}; prepare the machine once with setup-cross.sh arm64
# From Git Bash it re-runs itself as root inside the uavlink-dev WSL distro.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh --root bash packaging/uavlink-router/build.sh "$@"
fi
if [ "$(id -u)" -ne 0 ]; then exec sudo -n bash "${BASH_SOURCE[0]}" "$@"; fi

build_arch="$(dpkg --print-architecture)"
arch="$build_arch"
while [ $# -gt 0 ]; do
  case "$1" in
    --arch) arch="${2:?--arch needs a value}"; shift 2 ;;
    --arch=*) arch="${1#--arch=}"; shift ;;
    *) echo "usage: build.sh [--arch ARCH]" >&2; exit 2 ;;
  esac
done
gnu_type="$(dpkg-architecture -a"$arch" -qDEB_HOST_GNU_TYPE 2>/dev/null)"
if [ "$arch" = "$build_arch" ]; then
  check_opts=()
  build_opts=(-sa)
  missing="debhelper meson ninja-build pkgconf g++"
else
  check_opts=(-a"$arch" "-Pcross,nocheck")
  # Binary only: the source package from the native build stays the one in out/.
  build_opts=(-B "${check_opts[@]}")
  missing="debhelper meson ninja-build, then bash packaging/uavlink-router/setup-cross.sh $arch"
fi

pkg="$here/packaging/uavlink-router"
out="$here/out"
bash "$pkg/fetch-orig.sh"       # no-op when the tarball exists

orig="$(ls "$out"/uavlink-router_*.orig.tar.xz | head -n1)"
version="$(basename "$orig" .orig.tar.xz)"; version="${version#uavlink-router_}"
work="$(mktemp -d /var/tmp/uavlink-router-build.XXXXXX)"
trap 'rm -rf "$work"' EXIT

cp "$orig" "$work/"
tar -C "$work" -xf "$orig"
src="$work/uavlink-router-$version"
cp -a "$pkg/debian" "$src/debian"
bash "$here/tools/normalise-modes.sh" "$src/debian"    # /mnt/d marks every file executable

if ! (cd "$src" && dpkg-checkbuilddeps "${check_opts[@]}"); then
  echo "Missing build dependencies: apt-get install $missing" >&2
  exit 1
fi
if [ "$arch" != "$build_arch" ] && ! { command -v "$gnu_type-g++" >/dev/null &&
     dpkg-query -W "libc6:$arch" "libgcc-s1:$arch" "libstdc++6:$arch" >/dev/null 2>&1; }; then
  echo "Not set up for $arch (cross g++, $arch libraries for dpkg-shlibdeps):" \
       "bash packaging/uavlink-router/setup-cross.sh $arch" >&2
  exit 1
fi

# Fresh network namespace: only a loopback interface, nothing routable.
echo "== network inside the build ($arch)"
unshare -n bash -c '
  ip link set lo up
  ip -o link show | cut -d: -f2 | tr -d " " | paste -sd" " | sed "s/^/interfaces: /"
  if timeout 3 bash -c "exec 3<>/dev/tcp/1.1.1.1/53" 2>/dev/null; then echo "network is reachable, aborting" >&2; exit 1; fi
  echo "no network (connect to 1.1.1.1:53 failed, as required)"
  cd "$0" && dpkg-buildpackage -us -uc -j"$(nproc)" "$@"
' "$src" "${build_opts[@]}"

mkdir -p "$out"
for f in "$work"/uavlink-router_"$version"-*; do
  [ -f "$f" ] && cp -f "$f" "$out/"
done
if [ -n "${SUDO_UID:-}" ]; then chown "$SUDO_UID:${SUDO_GID:-$SUDO_UID}" "$out"/uavlink-router_* || true; fi
deb="$(ls "$out"/uavlink-router_"$version"-*_"$arch".deb)"

echo "== binary"
dpkg-deb -x "$deb" "$work/root"
bin="$work/root/usr/lib/uavlink-router/mavlink-routerd"
file -b "$bin"
if [ "$arch" = "$build_arch" ]; then
  "$bin" --version
else
  qemu="qemu-$(dpkg-architecture -a"$arch" -qDEB_HOST_GNU_CPU 2>/dev/null)-static"
  if command -v "$qemu" >/dev/null; then
    # The cross libc (libc6-$arch-cross, pulled in by crossbuild-essential) stands in for the target's root.
    echo "\$ $qemu -L /usr/$gnu_type mavlink-routerd --version"
    "$qemu" -L "/usr/$gnu_type" "$bin" --version
  else
    echo "$qemu not found (apt-get install qemu-user-static): binary not run"
  fi
fi

echo "== lintian"
lint=0
lintian --info --display-info --pedantic "$out/uavlink-router_${version}-1_$arch.changes" || lint=$?
echo "== result"
ls -l "$out"/uavlink-router_*
exit "$lint"
