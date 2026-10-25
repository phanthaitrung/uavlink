# shellcheck shell=bash
# Progress events for the setup page and the CLI (ARCHITECTURE.md section 4.1). Source it, do not run it.
# Every function prints one "@@UAVLINK {json}" line on stdout, only when UAVLINK_EVENTS=1.
#
#   ev_step   <id> <start|ok|skip|warn|fail> [code] [msg] [args-json]
#   ev_check  <id> <true|false> [code] [msg] [args-json]
#   ev_change <kind> <target> <action> [key=value | key:=raw-json]…   e.g. diff="$d" old=14 new=1
#   ev_info   <code> <msg> [args-json]
#   ev_rollback <armed|confirmed|fired> [secs]
#   ev_emit   <type> [key=value | key:=raw-json]…                      any other event
# args-json is a JSON object such as '{"iface":"eth0"}'; build string values with ev_str.

# JSON string literal (with quotes) for $1. Drops control characters other than \n \t \r.
ev_str() {
  local s="$1" b='\'
  case "$s" in *[$'\001'-$'\010\013\014\016'-$'\037\177']*) s="$(printf '%s' "$s" | tr -d '\001-\010\013\014\016-\037\177')" ;; esac
  # Quoted replacements are literal in every bash version (5.2 treats a bare \ in a replacement as an escape).
  s="${s//"$b"/"$b$b"}"; s="${s//\"/"$b\""}"
  s="${s//$'\n'/"${b}n"}"; s="${s//$'\t'/"${b}t"}"; s="${s//$'\r'/"${b}r"}"
  printf '"%s"' "$s"
}

ev_emit() {
  [ "${UAVLINK_EVENTS:-0}" = 1 ] || return 0
  local json kv key
  json="{\"t\":$(ev_str "$1")"; shift
  for kv in "$@"; do
    case "$kv" in
      *:=*) key="${kv%%:=*}"; json+=",$(ev_str "$key"):${kv#*:=}" ;;
      *=*)  key="${kv%%=*}";  json+=",$(ev_str "$key"):$(ev_str "${kv#*=}")" ;;
      *)    printf 'ev_emit: bad argument %s\n' "$kv" >&2; return 1 ;;
    esac
  done
  printf '@@UAVLINK %s}\n' "$json"
}

# Adds code/msg/args to an event when they are not empty.
_ev_with() {
  local type="$1" code="$4" msg="$5" args="$6"
  local extra=("$2" "$3")
  [ -n "$code" ] && extra+=("code=$code")
  [ -n "$msg" ] && extra+=("msg=$msg")
  [ -n "$args" ] && extra+=("args:=$args")
  ev_emit "$type" "${extra[@]}"
}

ev_step()     { _ev_with step "id=$1" "state=$2" "${3:-}" "${4:-}" "${5:-}"; }
ev_check()    { _ev_with check "id=$1" "ok:=$2" "${3:-}" "${4:-}" "${5:-}"; }
ev_change()   { local kind="$1" target="$2" action="$3"; shift 3; ev_emit change "kind=$kind" "target=$target" "action=$action" "$@"; }
ev_info()     { local args="${3:-}"; if [ -n "$args" ]; then ev_emit info "code=$1" "msg=$2" "args:=$args"; else ev_emit info "code=$1" "msg=$2"; fi; }
ev_rollback() { if [ -n "${2:-}" ]; then ev_emit rollback "state=$1" "secs:=$2"; else ev_emit rollback "state=$1"; fi; }

# True on WSL (used to refuse real changes there, see ARCHITECTURE.md section 4).
uavlink_is_wsl() {
  [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null
}
