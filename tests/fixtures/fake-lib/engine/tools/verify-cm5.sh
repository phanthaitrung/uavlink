#!/usr/bin/env bash
# Fake verify for runner tests: FAKE_VERIFY_EXIT (default 0; 3 = failed checks), FAKE_VERIFY_SLEEP seconds.
echo "args: $*"
echo '@@UAVLINK {"t":"check","id":"lan.link","ok":true,"code":"lan.link_up","args":{"iface":"eth0"}}'
if [ -n "${FAKE_VERIFY_SLEEP:-}" ]; then sleep "$FAKE_VERIFY_SLEEP"; fi
exit "${FAKE_VERIFY_EXIT:-0}"
