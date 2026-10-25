#!/usr/bin/env bash
# Normalise the file modes of a copied tree before a package build: directories and scripts (#! first) 0755,
# every other file 0644. A Windows-mounted tree (/mnt/d) shows every file as executable: lintian flags that, and
# debhelper runs an executable config file (debian/*.install, *.manpages, docs…) as a script instead of reading it.
# Usage: bash tools/normalise-modes.sh DIR…
set -euo pipefail
[ $# -gt 0 ] || { echo "usage: tools/normalise-modes.sh DIR…" >&2; exit 2; }

find "$@" -type d -exec chmod 0755 {} +
find "$@" -type f -exec chmod 0644 {} +
find "$@" -type f -exec sh -c 'for f; do [ "$(head -c 2 "$f")" != "#!" ] || chmod 0755 "$f"; done' sh {} +
