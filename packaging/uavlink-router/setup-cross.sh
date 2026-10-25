#!/usr/bin/env bash
# Prepare an Ubuntu build machine (the uavlink-dev WSL distro) for "build.sh --arch ARCH". Safe to run again.
# Usage: bash packaging/uavlink-router/setup-cross.sh [ARCH]   (default arm64; needs network)
#   - crossbuild-essential-ARCH (cross compiler) and qemu-user-static (to run the result)
#   - ARCH as a dpkg foreign architecture with the libraries the router links to (libc6, libgcc-s1, libstdc++6):
#     dpkg-shlibdeps reads their symbols files to write Depends. Ubuntu serves arm64/armhf/riscv64 from
#     ports.ubuntu.com, so the existing archive sources are limited to the native arch.
# From Git Bash it re-runs itself as root inside the uavlink-dev WSL distro.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh --root bash packaging/uavlink-router/setup-cross.sh "$@"
fi
if [ "$(id -u)" -ne 0 ]; then exec sudo -n bash "${BASH_SOURCE[0]}" "$@"; fi

arch="${1:-arm64}"
native="$(dpkg --print-architecture)"
[ "$arch" != "$native" ] || { echo "$arch is the native architecture: nothing to set up" >&2; exit 2; }
. /etc/os-release
export DEBIAN_FRONTEND=noninteractive

main_sources=/etc/apt/sources.list.d/ubuntu.sources
ports_sources=/etc/apt/sources.list.d/uavlink-ports.sources
if [ -f "$main_sources" ] && ! grep -q '^Architectures:' "$main_sources"; then
  cp -a "$main_sources" "$main_sources.uavlink-orig"
  sed -i "/^Types:/a Architectures: $native" "$main_sources"
  echo "limited $main_sources to $native (original kept as $main_sources.uavlink-orig)"
fi
arches="$(sed -n 's/^Architectures: *//p' "$ports_sources" 2>/dev/null || true)"
case " $arches " in *" $arch "*) ;; *) arches="${arches:+$arches }$arch" ;; esac
cat > "$ports_sources" <<EOF
# Added by uavlink packaging/uavlink-router/setup-cross.sh for cross builds.
Types: deb
URIs: http://ports.ubuntu.com/ubuntu-ports/
Suites: $VERSION_CODENAME $VERSION_CODENAME-updates $VERSION_CODENAME-security
Components: main universe
Architectures: $arches
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF

dpkg --add-architecture "$arch"
apt-get update -qq
apt-get install -y -qq "crossbuild-essential-$arch" qemu-user-static \
  "libc6:$arch" "libgcc-s1:$arch" "libstdc++6:$arch"
echo "ready: bash packaging/uavlink-router/build.sh --arch $arch"
