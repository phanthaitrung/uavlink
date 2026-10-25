#!/usr/bin/env bash
# Fake uninstall for runner tests.
echo "fake uninstall-cm5 args: $*"
echo '@@UAVLINK {"t":"step","id":"undo","state":"ok"}'
exit "${FAKE_UNINSTALL_EXIT:-0}"
