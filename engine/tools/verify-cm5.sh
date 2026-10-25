#!/usr/bin/env bash
# Kiểm thử chấp nhận phía CM5 (chỉ đọc, không đổi cấu hình). Chạy trên CM5:
#   sudo bash engine/tools/verify-cm5.sh
# Với UAVLINK_EVENTS=1 in thêm một sự kiện "check" cho mỗi kiểm tra và một sự kiện "result" cuối cùng.
# Thoát 0 nếu đạt hết, 3 nếu có kiểm tra hỏng.
set -uo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="$ROOT_DIR"
# shellcheck source=../lib/common.sh
. "$ROOT_DIR/lib/common.sh"
# shellcheck source=../lib/events.sh
. "$ROOT_DIR/lib/events.sh"
load_config
require_root

ROUTER_UNIT=uavlink-router
SYS_NET="${UAVLINK_SYS_NET:-/sys/class/net}"

passed=0; failed=0
# check <id> <human name> <args-json> <test command…>
# Event code is "<id>.ok" or "<id>.fail"; the human name is the English fallback message.
check() {
  local id="$1" name="$2" args="$3"; shift 3
  if "$@"; then
    ok "$name"; passed=$((passed + 1)); ev_check "$id" true "$id.ok" "$name" "$args"
  else
    bad "$name"; failed=$((failed + 1)); ev_check "$id" false "$id.fail" "$name" "$args"
  fi
}

ifc="$CM5_ETH_IFACE"
probe=(python3 "$ENGINE/tools/mav_probe.py" "tcp:127.0.0.1:$CM5_TCP_SERVER_PORT" --param NET_IPADDR3)
export PYTHONPATH="${PYTHONPATH:-$ENGINE/../vendor}"
vps_ip=""
if [ -n "${VPS_PUBLIC_IP:-}" ]; then vps_ip="$(resolve_ipv4 "$VPS_PUBLIC_IP" || true)"; fi

t_link()     { [ "$(cat "$SYS_NET/$ifc/carrier" 2>/dev/null)" = 1 ]; }
t_addr()     { ip -4 -o addr show dev "$ifc" | grep -q " $CM5_LAN_IP/"; }
t_route()    { ! ip -4 route show default | grep -q "dev $ifc "; }
t_ping()     { ping -c 3 -W 2 "$X25_IP" >/dev/null; }
t_router()   { systemctl is-active --quiet "$ROUTER_UNIT"; }
t_udp_in()   { saw_packets "$ifc" "udp and src host $X25_IP and dst port $MAVLINK_LAN_PORT"; }
t_mav()      { "${probe[@]}" | sed 's/^/      /'; }
t_signing()  { "${probe[@]}" --check-signing | tail -1 | sed 's/^/      /'; }
t_inet()     { internet_up; }
t_uplink()   { saw_packets any "udp and dst host $vps_ip and dst port $VPS_UPLINK_PORT"; }
# Không dùng `lệnh | grep -q` với pipefail: grep thoát ở dòng khớp, lệnh còn đang ghi thì chết vì SIGPIPE và phép
# kiểm tra hỏng dù kết quả đúng (sshd -T in hơn 4 KB). Đọc hết đầu ra trước rồi mới tìm.
t_ssh()      { grep -qx 'passwordauthentication no' <<<"$(sshd -T 2>/dev/null)"; }
t_ufw()      { grep -q '^Status: active' <<<"$(ufw status 2>/dev/null)"; }
t_not_root() {
  local pid; pid="$(systemctl show -p MainPID --value "$1")"
  [ -n "$pid" ] && [ "$pid" != 0 ] && [ "$(ps -o user= -p "$pid" | xargs)" != root ]
}

a_ifc="{\"iface\":$(ev_str "$ifc")}"
a_addr="{\"iface\":$(ev_str "$ifc"),\"ip\":$(ev_str "$CM5_LAN_IP")}"
a_x25="{\"ip\":$(ev_str "$X25_IP")}"

step "CM5 $(hostname): mạng LAN nối X25"
echo "      $(ip -br addr show dev "$ifc" 2>/dev/null), $(cat "$SYS_NET/$ifc/speed" 2>/dev/null || echo '?') Mb/s"
check lan.link  "Link vật lý $ifc"                        "$a_ifc"  t_link
check lan.addr  "$ifc có địa chỉ $CM5_LAN_IP"             "$a_addr" t_addr
check lan.route "Đường Internet mặc định không qua $ifc"  "$a_ifc"  t_route
check x25.ping  "Ping X25 $X25_IP"                        "$a_x25"  t_ping

step "MAVLink"
check router.active  "Dịch vụ $ROUTER_UNIT đang chạy"       "{\"unit\":\"$ROUTER_UNIT\"}" t_router
check x25.udp_in     "X25 gửi UDP tới :$MAVLINK_LAN_PORT"   "{\"port\":$MAVLINK_LAN_PORT,\"ip\":$(ev_str "$X25_IP")}" t_udp_in
check mavlink.two_way "HEARTBEAT + đọc tham số qua router (2 chiều)" "" t_mav

step "Internet / VPS"
check internet "Ra được Internet (ping hoặc TCP tới 1.1.1.1)" "" t_inet
if [ -n "$vps_ip" ]; then
  check vps.uplink "Đang gửi lên VPS $vps_ip:$VPS_UPLINK_PORT" "{\"ip\":$(ev_str "$vps_ip"),\"port\":$VPS_UPLINK_PORT}" t_uplink
fi

step "Bảo mật"
if [ "${MAVLINK_SIGNING:-0}" = 1 ]; then check signing.enforced "X25 chỉ nhận lệnh có ký MAVLink2" "" t_signing; fi
if [ "${SSH_HARDEN:-0}" = 1 ]; then check ssh.no_password "SSH không nhận mật khẩu" "" t_ssh; fi
if [ "${CM5_ENABLE_UFW:-0}" = 1 ]; then check ufw.active "Tường lửa UFW đang bật" "" t_ufw; fi
if [ "${SERVICE_SANDBOX:-0}" = 1 ]; then check router.not_root "$ROUTER_UNIT không chạy bằng root" "{\"unit\":\"$ROUTER_UNIT\"}" t_not_root "$ROUTER_UNIT"; fi

echo
if [ "$failed" -eq 0 ]; then ok "CM5: đạt $passed/$passed"; else bad "CM5: hỏng $failed/$((passed + failed))"; fi
ev_emit result "ok:=$([ "$failed" -eq 0 ] && echo true || echo false)" "data:={\"passed\":$passed,\"failed\":$failed}"
[ "$failed" -eq 0 ] || exit 3
