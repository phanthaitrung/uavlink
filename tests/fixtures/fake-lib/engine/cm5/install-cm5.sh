#!/usr/bin/env bash
# Fake engine command for runner tests. Behaviour comes from the environment:
#   FAKE_CM5_EXIT (default 0), FAKE_CM5_SLEEP seconds, FAKE_ECHO text printed as a log line and inside an event.
echo "args: $*"
echo "env: events=${UAVLINK_EVENTS:-} config=${UAVLINK_CONFIG_DIR:-} state=${UAVLINK_STATE_DIR:-} pythonpath=${PYTHONPATH:-} lc=${LC_ALL:-} bytecode=${PYTHONDONTWRITEBYTECODE:-}"
echo '@@UAVLINK {"t":"step","id":"eth","state":"start"}'
echo '@@UAVLINK not json'
if [ -n "${FAKE_ECHO:-}" ]; then
  echo "leak: $FAKE_ECHO"
  echo "@@UAVLINK {\"t\":\"info\",\"msg\":\"$FAKE_ECHO\",\"args\":{\"v\":\"$FAKE_ECHO\"}}"
fi
if [ -n "${FAKE_CM5_SLEEP:-}" ]; then sleep "$FAKE_CM5_SLEEP"; fi
echo '@@UAVLINK {"t":"step","id":"eth","state":"ok"}'
exit "${FAKE_CM5_EXIT:-0}"
