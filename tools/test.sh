#!/usr/bin/env bash
# All automatic tests. On Windows it re-runs itself inside the uavlink-dev WSL distro.
# Usage: bash tools/test.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${WSL_DISTRO_NAME:-}" ] && command -v wsl.exe >/dev/null 2>&1; then
  cd "$here"
  exec bash tools/wsl.sh bash tools/test.sh "$@"
fi
cd "$here"
export PYTHONDONTWRITEBYTECODE=1
fail=0

# Files under version control (git cannot read Windows worktrees from WSL, so use find).
mapfile -t files < <(find . -path ./.git -prune -o -path ./out -prune -o -name node_modules -prune -o -name __pycache__ -prune \
  -o -path ./packaging/uavlink-router/upstream -prune -o -type f -print | sed 's|^\./||' | sort)

echo "== CRLF"
crlf=()
for f in "${files[@]}"; do
  case "$f" in *.png|*.jpg|*.gz|*.xz|*.tar) continue ;; esac
  if grep -qIU $'\r' "$f" 2>/dev/null; then crlf+=("$f"); fi
done
if [ ${#crlf[@]} -gt 0 ]; then printf 'CRLF line endings: %s\n' "${crlf[@]}"; fail=1; else echo ok; fi

echo "== python unittest"
python3 -m unittest discover -s tests -t . -p 'test_*.py' || fail=1

echo "== shellcheck"
sh_files=()
for f in "${files[@]}"; do
  case "$f" in
    *.sh) sh_files+=("$f") ;;
    debian/*postinst|debian/*postrm|debian/*prerm|debian/*preinst|*/debian/*postinst|*/debian/*postrm|*/debian/*prerm|*/debian/*preinst) sh_files+=("$f") ;;
  esac
done
if [ ${#sh_files[@]} -gt 0 ]; then shellcheck -S warning "${sh_files[@]}" || fail=1; fi
echo "${#sh_files[@]} files checked"

[ "$fail" -eq 0 ] && echo "ALL TESTS PASSED" || echo "TESTS FAILED"
exit "$fail"
