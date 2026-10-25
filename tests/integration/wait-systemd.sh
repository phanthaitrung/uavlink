#!/usr/bin/env bash
# Wait until systemd in the test distro has finished booting (running or degraded). Runs inside the distro.
for _ in $(seq 1 90); do
  s="$(systemctl is-system-running 2>/dev/null || true)"
  case "$s" in running|degraded) exit 0 ;; esac
  sleep 1
done
echo "systemd not ready: ${s:-no answer}" >&2
exit 1
