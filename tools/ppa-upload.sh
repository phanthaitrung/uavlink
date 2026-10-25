#!/usr/bin/env bash
# Build signed source packages for Launchpad (one per Ubuntu series) and upload them with dput.
# Usage: bash tools/ppa-upload.sh [--dry-run | --upload] [--series "noble ..."] [--ppa ppa:uavlink/stable]
#                                 [--throwaway-key]
#   --dry-run        (default) build the source packages, print the dput commands, upload nothing
#   --upload         dput every <series> .changes file; needs a signing key
#   --series         space separated Ubuntu series (default: noble)
#   --throwaway-key  sign with a temporary key in a temporary GNUPGHOME (tests the signing path; Launchpad rejects it)
# Signing key: PPA_GPG_KEY_ID (fingerprint, already in the keyring) and optionally PPA_GPG_PASSPHRASE.
# Without a key the packages are built unsigned (dry run only). The passphrase and key are never printed.
# Result: out/ppa/<series>/{uavlink,uavlink-router}_*_source.changes and the files they list.
# From Git Bash it re-runs itself inside the uavlink-dev WSL distro.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh bash tools/ppa-upload.sh "$@"
fi
cd "$here"

mode=dry-run
series_list=noble
ppa=ppa:uavlink/stable
throwaway=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) mode=dry-run ;;
    --upload) mode=upload ;;
    --series) series_list="${2:?--series needs a value}"; shift ;;
    --ppa) ppa="${2:?--ppa needs a value}"; shift ;;
    --throwaway-key) throwaway=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
for s in $series_list; do
  case "$s" in *[!a-z]*|'') echo "bad series name: $s" >&2; exit 2 ;; esac
done

for tool in dpkg-buildpackage dpkg-parsechangelog xz tar; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing tool: $tool (apt-get install devscripts dpkg-dev xz-utils)" >&2; exit 1; }
done
if [ "$mode" = upload ]; then
  command -v dput >/dev/null 2>&1 || { echo "missing tool: dput (apt-get install dput)" >&2; exit 1; }
fi

work="$(mktemp -d /var/tmp/uavlink-ppa.XXXXXX)"
tmp_gnupg=
cleanup() {
  if [ -n "$tmp_gnupg" ]; then gpgconf --homedir "$tmp_gnupg" --kill gpg-agent 2>/dev/null || true; fi
  rm -rf "$work"
}
trap cleanup EXIT

# ---- signing -----------------------------------------------------------------------------------
sign_args=(-us -uc)
key="${PPA_GPG_KEY_ID:-}"
if [ "$throwaway" = 1 ]; then
  [ "$mode" = dry-run ] || { echo "--throwaway-key only works with --dry-run" >&2; exit 2; }
  tmp_gnupg="$work/gnupg"; mkdir -m 700 "$tmp_gnupg"
  export GNUPGHOME="$tmp_gnupg"
  maint="$(dpkg-parsechangelog -S Maintainer)"
  gpg --batch --quiet --passphrase '' --quick-generate-key "$maint" ed25519 sign never 2>/dev/null
  key="$(gpg --batch --list-secret-keys --with-colons | awk -F: '/^fpr:/ {print $10; exit}')"
  echo "signing with a throwaway key (${key: -16}), Launchpad would reject it"
fi
if [ -n "$key" ]; then
  # dpkg-buildpackage -p takes a command name only, so the options go into a wrapper.
  gpg_wrap="$work/gpg-sign"
  {
    echo '#!/bin/sh'
    if [ -n "${PPA_GPG_PASSPHRASE:-}" ]; then
      pass_file="$work/pass"; ( umask 077; printf '%s' "$PPA_GPG_PASSPHRASE" > "$pass_file" )
      echo "exec gpg --batch --yes --pinentry-mode loopback --passphrase-file '$pass_file' \"\$@\""
    else
      echo 'exec gpg --batch --yes "$@"'
    fi
  } > "$gpg_wrap"
  chmod 700 "$gpg_wrap"
  sign_args=("-k$key" "-p$gpg_wrap")
elif [ "$mode" = upload ]; then
  echo "--upload needs a signing key: set PPA_GPG_KEY_ID (and PPA_GPG_PASSPHRASE)" >&2
  exit 1
else
  echo "no signing key: source packages are built unsigned"
fi

# ---- helpers -----------------------------------------------------------------------------------
# Rewrite the first changelog line: new version and distribution, the rest of the entry stays.
retarget() {  # <changelog> <package> <version> <series>
  local urgency; urgency="$(dpkg-parsechangelog -l "$1" -S Urgency)"
  sed -i "1s/^.*\$/$2 ($3) $4; urgency=$urgency/" "$1"
}

is_signed() { grep -q '^-----BEGIN PGP SIGNATURE-----' "$1"; }

out="$here/out/ppa"
rm -rf "$out"; mkdir -p "$out"

# ---- uavlink-router orig tarball (must be part of the first upload) -----------------------------
bash packaging/uavlink-router/fetch-orig.sh
orig="$(ls "$here"/out/uavlink-router_*.orig.tar.xz | head -n1)"
router_upstream="$(basename "$orig" .orig.tar.xz)"; router_upstream="${router_upstream#uavlink-router_}"
router_rev="$(dpkg-parsechangelog -l packaging/uavlink-router/debian/changelog -S Version)"   # <upstream>-<revision>
router_rev="${router_rev#"$router_upstream"-}"
uavlink_version="$(dpkg-parsechangelog -S Version)"

for series in $series_list; do
  dest="$out/$series"; mkdir -p "$dest" "$work/$series"

  echo "== $series: uavlink ${uavlink_version}~${series}1"
  src="$work/$series/uavlink"; mkdir -p "$src"
  tar -c --exclude=./.git --exclude=./out --exclude=__pycache__ --exclude=./packaging/uavlink-router/upstream . | tar -x -C "$src"
  bash tools/normalise-modes.sh "$src"
  retarget "$src/debian/changelog" uavlink "${uavlink_version}~${series}1" "$series"
  ( cd "$src" && dpkg-buildpackage -S -d -sa "${sign_args[@]}" ) >"$work/$series/uavlink.log" 2>&1 \
    || { cat "$work/$series/uavlink.log" >&2; exit 1; }
  cp -f "$work/$series"/uavlink_* "$dest/"

  echo "== $series: uavlink-router ${router_upstream}-${router_rev}~${series}1"
  rsrc="$work/$series/uavlink-router-$router_upstream"
  cp "$orig" "$work/$series/"
  tar -C "$work/$series" -xf "$orig"
  cp -a packaging/uavlink-router/debian "$rsrc/debian"
  bash tools/normalise-modes.sh "$rsrc/debian"
  retarget "$rsrc/debian/changelog" uavlink-router "${router_upstream}-${router_rev}~${series}1" "$series"
  ( cd "$rsrc" && dpkg-buildpackage -S -d -sa "${sign_args[@]}" ) >"$work/$series/router.log" 2>&1 \
    || { cat "$work/$series/router.log" >&2; exit 1; }
  cp -f "$work/$series"/uavlink-router_* "$dest/"
done

changes_files=()
for c in "$out"/*/*_source.changes; do changes_files+=("$c"); done

echo "== source packages"
for c in "${changes_files[@]}"; do
  state=unsigned; if is_signed "$c"; then state=signed; fi
  echo "${c#"$here"/} ($state)"
done

echo "== dput"
for c in "${changes_files[@]}"; do
  if [ "$mode" = upload ]; then
    is_signed "$c" || { echo "$c is not signed" >&2; exit 1; }
    dput "$ppa" "$c"
  else
    echo "would run: dput $ppa ${c#"$here"/}"
  fi
done
if [ "$mode" != upload ]; then echo "dry run: nothing uploaded"; fi
