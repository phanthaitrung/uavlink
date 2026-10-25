#!/usr/bin/env bash
# Cài đặt phía Raspberry Pi CM5. Chạy lại nhiều lần được, bước nào đã xong thì bỏ qua.
#   packages   Gói hệ thống, chrony, quyền dialout
#   usb        Cổng USB của X25: ModemManager không được chiếm
#   eth        Cổng LAN nối X25: IP tĩnh, không làm đường Internet mặc định
#   wifi       Nối Wi-Fi Internet (chỉ khi có CM5_WIFI_SSID)
#   tailscale  Cài và đăng nhập Tailscale (chỉ khi CM5_INSTALL_TAILSCALE=1)
#   router     uavlink-router: nhận X25 qua LAN, đẩy lên VPS và các đích UDP khác
#   security   SSH chỉ bằng khóa, tường lửa UFW, tự cài bản vá. Thay đổi tường lửa/SSH tự hoàn
#              tác sau FIREWALL_ROLLBACK_SECS giây nếu không được xác nhận.
#
# Dùng:  install-cm5.sh [--preview] [--steps a,b] [--print-config]
# Cấu hình đọc từ $UAVLINK_CONFIG_DIR (mặc định /etc/uavlink). Với UAVLINK_EVENTS=1 in thêm sự kiện
# `@@UAVLINK {json}` (ARCHITECTURE.md mục 4.1). Mã thoát: 0 xong, 1 lỗi, 2 sai cú pháp, 3 xem trước thấy
# điểm hỏng, 4 từ chối (WSL, không phải root).
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/common.sh
. "$ROOT_DIR/lib/common.sh"

ALL_STEPS=(packages usb eth wifi tailscale router security)

usage() {
  echo "Dùng: install-cm5.sh [--preview] [--steps $(IFS=,; echo "${ALL_STEPS[*]}")] [--print-config]" >&2
  exit 2
}

# Mã lỗi render_router_conf in ra stderr dạng "mã<TAB>chi tiết" rồi thoát 11 (xem step_router).
render_fail() {
  printf '%s\t%s\n' "$1" "$2" >&2
  exit 11
}

render_router_conf() {
  local vps_ip="" t name hostport host port
  if [ -n "$VPS_PUBLIC_IP" ]; then
    vps_ip="$(resolve_ipv4 "$VPS_PUBLIC_IP")" || render_fail router.vps_resolve "$VPS_PUBLIC_IP"
  fi
  cat <<EOF
# Tạo bởi uavlink (engine/cm5/install-cm5.sh). Đừng sửa tay: đổi cấu hình trong uavlink rồi áp dụng lại.
[General]
TcpServerPort = $CM5_TCP_SERVER_PORT
MavlinkDialect = ardupilotmega
ReportStats = $ROUTER_REPORT_STATS
EOF
  if [ -n "$CM5_ROUTER_LOG_DIR" ]; then
    printf 'Log = %s\nLogMode = %s\n' "$CM5_ROUTER_LOG_DIR" "$CM5_ROUTER_LOG_MODE"
  fi
  cat <<EOF

# X25 EVO (NET_P1_TYPE = UDP Client) gửi tới $CM5_LAN_IP:$MAVLINK_LAN_PORT
[UdpEndpoint x25_ethernet]
Mode = Server
Address = 0.0.0.0
Port = $MAVLINK_LAN_PORT
EOF
  if [ -n "$vps_ip" ]; then
    cat <<EOF

# Đẩy lên VPS relay
[UdpEndpoint vps_mavlink]
Mode = Normal
Address = $vps_ip
Port = $VPS_UPLINK_PORT
EOF
  fi
  for t in $CM5_EXTRA_UDP_TARGETS; do
    name="${t%%=*}"; hostport="${t#*=}"; host="${hostport%:*}"; port="${hostport##*:}"
    if [ "$name" = "$t" ] || [ "$host" = "$hostport" ]; then
      render_fail router.target_format "$t"
    fi
    host="$(resolve_ipv4 "$host")" || render_fail router.target_resolve "$t"
    cat <<EOF

[UdpEndpoint $name]
Mode = Normal
Address = $host
Port = $port
EOF
  done
}

# Render vào $ROUTER_CONF (không qua stdout: `fail` phát sự kiện trên stdout). Lỗi của render đi qua stderr, vì nó chạy trong $(...).
render_router_conf_or_fail() {
  ROUTER_CONF=""
  local errf conf rc=0 code detail
  errf="$(mktemp)"
  conf="$(render_router_conf 2>"$errf")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    IFS=$'\t' read -r code detail < "$errf" || true
    rm -f "$errf"
    case "$code" in
      router.vps_resolve)
        fail router.vps_resolve "Không phân giải được VPS_PUBLIC_IP=$detail" "cannot resolve VPS_PUBLIC_IP=$detail" "$(jobj host "$detail")" ;;
      router.target_format)
        fail router.target_format "CM5_EXTRA_UDP_TARGETS sai dạng ở '$detail' (cần ten=ip:cong)" \
          "CM5_EXTRA_UDP_TARGETS has a bad entry '$detail' (need name=ip:port)" "$(jobj target "$detail")" ;;
      router.target_resolve)
        fail router.target_resolve "Không phân giải được địa chỉ trong '$detail'" "cannot resolve the address in '$detail'" "$(jobj target "$detail")" ;;
      *) fail step.failed "Không tạo được cấu hình router (mã $rc)" "cannot render the router config (exit $rc)" ;;
    esac
  fi
  rm -f "$errf"
  ROUTER_CONF="$conf"
}

cm5_user_ok() { [ -n "$CM5_USER" ] && id "$CM5_USER" >/dev/null 2>&1; }

# Báo user thiếu: lỗi khi áp dụng, cảnh báo khi xem trước. Trả 1 nếu thiếu.
require_cm5_user() {
  cm5_user_ok && return 0
  if [ "$PREVIEW" = 1 ]; then
    warn_code user.missing "Không có user '$CM5_USER' trên máy này (sửa CM5_USER)" \
      "user '$CM5_USER' does not exist on this machine (set CM5_USER)" "$(jobj user "$CM5_USER")"
    return 1
  fi
  fail user.missing "Không có user '$CM5_USER' trên máy này (sửa CM5_USER)" \
    "user '$CM5_USER' does not exist on this machine (set CM5_USER)" "$(jobj user "$CM5_USER")"
}

# ---- Các bước ---------------------------------------------------------------------------------

step_packages() {
  if internet_up; then
    check_ev internet true internet.up "Có Internet" "Internet is reachable"
  else
    check_ev internet false internet.down "Không ra được Internet (ping và TCP tới 1.1.1.1)" "no Internet (ping and TCP to 1.1.1.1 failed)"
  fi
  apt_install tcpdump chrony iproute2 iputils-ping curl ufw python3
  if ! svc_is_enabled chrony.service || ! svc_is_active chrony.service; then
    svc_do enable-now chrony.service || warn "Không bật được chrony"
  fi
  if require_cm5_user; then
    if ! grep -qx dialout <<<"$(id -nG "$CM5_USER" | tr ' ' '\n')"; then
      ev_change user "$CM5_USER" set group=dialout
      if [ "$PREVIEW" = 1 ]; then
        info "Sẽ thêm $CM5_USER vào nhóm dialout"
      else
        record_group "$CM5_USER" dialout
        usermod -aG dialout "$CM5_USER"
        info "Đã thêm $CM5_USER vào nhóm dialout"
      fi
    fi
  fi
}

step_usb() {
  local rule dev
  rule='# Tạo bởi uavlink: ModemManager không dò cổng USB của mạch bay ArduPilot
ACTION=="add|change", SUBSYSTEM=="tty", ENV{ID_VENDOR}=="ArduPilot", ENV{ID_MM_DEVICE_IGNORE}="1"'
  if write_if_changed /etc/udev/rules.d/99-ardupilot-no-modemmanager.rules 644 <<<"$rule"; then
    if [ "$PREVIEW" != 1 ]; then udevadm control --reload-rules || true; fi
  fi
  if [ "$CM5_DISABLE_MODEMMANAGER" = 1 ] && systemctl cat ModemManager.service >/dev/null 2>&1; then
    if svc_is_enabled ModemManager.service || svc_is_active ModemManager.service; then
      svc_do disable-now ModemManager.service || true
      ok "Đã tắt ModemManager"
    fi
  fi
  dev="$(ls /dev/serial/by-id/*ArduPilot*-if00 2>/dev/null | head -1 || true)"
  if [ -n "$dev" ]; then
    ok "Thấy X25 qua USB: $dev"
    ev_info usb.x25_found "X25 found over USB: $dev" "$(jobj dev "$dev")"
  else
    warn_code usb.no_x25 "Chưa thấy X25 qua USB (/dev/serial/by-id/*ArduPilot*). Bước x25 cần cắm cáp USB." \
      "X25 not seen over USB (/dev/serial/by-id/*ArduPilot*). The x25 step needs the USB cable."
  fi
}

# NetworkManager quản lý mạng của máy này? Đang chạy, hoặc được bật mà tạm dừng (vd unattended-upgrades đang nâng
# cấp nó): khi đó chờ nó chạy lại, rồi tự khởi động; không chuyển sang netplan, máy dùng NetworkManager.
# NM_WAIT_SECS: thời gian chờ (mặc định 90 s).
nm_manages_network() {
  local i secs="${NM_WAIT_SECS:-90}"
  command -v nmcli >/dev/null 2>&1 || return 1
  systemctl is-active --quiet NetworkManager && return 0
  systemctl is-enabled --quiet NetworkManager 2>/dev/null || return 1
  [ "$PREVIEW" != 1 ] || return 0
  warn "NetworkManager đang dừng, chờ tối đa ${secs}s"
  for ((i = 0; i < secs; i++)); do
    if [ "$i" = $((secs * 2 / 3)) ]; then systemctl start NetworkManager >/dev/null 2>&1 || true; fi
    if systemctl is-active --quiet NetworkManager; then
      nm-online -s -q -t 30 >/dev/null 2>&1 || true
      return 0
    fi
    sleep 1
  done
  fail eth.nm_not_running "NetworkManager được bật nhưng không chạy, không đặt được địa chỉ cho $CM5_ETH_IFACE" \
    "NetworkManager is enabled but does not run, the address of $CM5_ETH_IFACE cannot be set" "$1"
}

# nm_con_has <tên> <thuộc tính> <giá trị>…: kết nối NetworkManager đã có đúng các giá trị này chưa?
# (nmcli -g in mỗi giá trị một dòng; danh sách địa chỉ cách nhau bằng ", ".)
nm_con_has() {
  local name="$1" keys=() wanted=() got=() i; shift
  while [ $# -ge 2 ]; do keys+=("$1"); wanted+=("$2"); shift 2; done
  mapfile -t got < <(nmcli -g "$(IFS=,; echo "${keys[*]}")" connection show "$name" 2>/dev/null)
  [ "${#got[@]}" -eq "${#keys[@]}" ] || return 1
  for ((i = 0; i < ${#keys[@]}; i++)); do
    [ "${got[$i]//, /,}" = "${wanted[$i]}" ] || return 1
  done
}

nm_con_active_on() {
  grep -qxF "$1:$2" <<<"$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null)"
}

# Đủ các địa chỉ cấu hình trên cổng LAN chưa?
eth_has_addrs() {
  local a cur
  cur="$(ip -4 -o addr show dev "$CM5_ETH_IFACE" | awk '{print $4}')"
  for a in "$CM5_LAN_IP/$X25_NETMASK" $CM5_ETH_EXTRA_ADDRS; do
    grep -qxF "$a" <<<"$cur" || return 1
  done
}

step_eth() {
  local ifc="$CM5_ETH_IFACE" defdev addrs a carrier route_bad=0 cons act args
  args="$(jobj iface "$ifc")"
  ip link show "$ifc" >/dev/null 2>&1 || fail eth.no_iface \
    "Không có cổng $ifc (xem 'ip -br link'), sửa CM5_ETH_IFACE" "no interface $ifc (see 'ip -br link'), set CM5_ETH_IFACE" "$args"

  carrier="$(cat "/sys/class/net/$ifc/carrier" 2>/dev/null || echo 0)"
  if [ "$carrier" = 1 ]; then
    check_ev lan.link true lan.link_up "$ifc có link" "$ifc has carrier" "$args"
  else
    check_ev lan.link false lan.link_down "Chưa có link trên $ifc (X25 chưa cấp nguồn hoặc chưa cắm cáp)" \
      "no carrier on $ifc (X25 not powered or cable unplugged)" "$args"
  fi

  # Cổng LAN này dành riêng cho X25. Nếu Internet đang đi qua nó thì cấu hình sẽ cắt mạng.
  defdev="$(ip -4 route show default | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
  if [ "$defdev" = "$ifc" ] && [ "${FORCE:-0}" != 1 ]; then
    route_bad=1
    check_ev eth.default_route false eth.default_route "Đường Internet mặc định đang đi qua $ifc" \
      "the default route goes via $ifc" "$args"
    if [ "$PREVIEW" != 1 ]; then
      fail eth.default_route "Đường Internet mặc định đang đi qua $ifc. Hãy đưa Internet sang Wi-Fi trước (hoặc chạy với FORCE=1 nếu chắc chắn)." \
        "the default route goes via $ifc; move Internet to Wi-Fi first (or run with FORCE=1 if sure)" "$args"
    fi
  else
    check_ev eth.default_route true eth.default_route_ok "Đường Internet mặc định không qua $ifc" \
      "the default route does not use $ifc" "$args"
  fi

  addrs="$CM5_LAN_IP/$X25_NETMASK"
  for a in $CM5_ETH_EXTRA_ADDRS; do addrs="$addrs,$a"; done

  if nm_manages_network "$args"; then
    local props=(connection.interface-name "$ifc" connection.autoconnect yes connection.autoconnect-priority 100
      ipv4.method manual ipv4.addresses "$addrs" ipv4.gateway "" ipv4.dns "" ipv4.never-default yes
      ipv4.may-fail yes ipv6.method disabled)
    cons="$(nmcli -t -f NAME connection show 2>/dev/null || true)"
    if grep -qxF "$CM5_ETH_CON_NAME" <<<"$cons"; then act=update; else act=create; fi
    # Đã đúng mà vẫn sửa thì NetworkManager kích hoạt lại kết nối: đường X25 rớt một nhịp mỗi lần áp dụng.
    if [ "$act" = update ] && nm_con_has "$CM5_ETH_CON_NAME" "${props[@]}"; then
      if nm_con_active_on "$CM5_ETH_CON_NAME" "$ifc" || [ "$carrier" != 1 ]; then
        ok "Kết nối NetworkManager '$CM5_ETH_CON_NAME' đã đúng"
        act=keep
      else
        act=up
      fi
    fi
    if [ "$act" = keep ]; then
      :
    elif [ "$act" = up ]; then
      ev_change nm "$CM5_ETH_CON_NAME" start "iface=$ifc"
      if [ "$PREVIEW" != 1 ]; then
        if nmcli connection up "$CM5_ETH_CON_NAME" >/dev/null; then
          ok "Đã kích hoạt '$CM5_ETH_CON_NAME' trên $ifc"
        else
          warn "Không kích hoạt được '$CM5_ETH_CON_NAME', xem 'nmcli connection show $CM5_ETH_CON_NAME'"
        fi
      fi
    elif [ "$PREVIEW" = 1 ]; then
      ev_change nm "$CM5_ETH_CON_NAME" "$act" "iface=$ifc" "addresses=$addrs"
      info "Sẽ $act kết nối NetworkManager '$CM5_ETH_CON_NAME' ($addrs trên $ifc)"
    else
      ev_change nm "$CM5_ETH_CON_NAME" "$act" "iface=$ifc" "addresses=$addrs"
      record_nm "$CM5_ETH_CON_NAME"
      if [ "$act" = update ]; then
        nmcli connection modify "$CM5_ETH_CON_NAME" "${props[@]}"
        info "Đã cập nhật kết nối NetworkManager '$CM5_ETH_CON_NAME'"
      else
        nmcli connection add type ethernet con-name "$CM5_ETH_CON_NAME" ifname "$ifc" "${props[@]}" >/dev/null
        info "Đã tạo kết nối NetworkManager '$CM5_ETH_CON_NAME'"
      fi
      if [ "$carrier" = 1 ]; then
        if nmcli connection up "$CM5_ETH_CON_NAME" >/dev/null; then
          ok "Đã kích hoạt '$CM5_ETH_CON_NAME' trên $ifc"
        else
          warn "Không kích hoạt được '$CM5_ETH_CON_NAME', xem 'nmcli connection show $CM5_ETH_CON_NAME'"
        fi
      else
        warn "NetworkManager sẽ tự bật '$CM5_ETH_CON_NAME' khi có link trên $ifc."
      fi
    fi
  elif command -v netplan >/dev/null 2>&1; then
    local yaml
    yaml="# Tạo bởi uavlink: cổng LAN nối X25, không gateway
network:
  version: 2
  ethernets:
    $ifc:
      dhcp4: false
      dhcp6: false
      optional: true
      addresses:"
    for a in ${addrs//,/ }; do yaml="$yaml
        - $a"; done
    if write_if_changed /etc/netplan/60-x25-link.yaml 600 <<<"$yaml"; then
      if [ "$PREVIEW" != 1 ]; then
        netplan generate
        netplan apply
        ok "Đã cấu hình $ifc bằng netplan"
      fi
    else
      ok "$ifc đã cấu hình bằng netplan"
    fi
  else
    fail eth.no_netmgr "Không có NetworkManager hay netplan. Hãy tự đặt $addrs cho $ifc (không gateway) rồi chạy lại." \
      "neither NetworkManager nor netplan found; set $addrs on $ifc yourself (no gateway) and rerun" "$args"
  fi

  if [ "$PREVIEW" != 1 ] && eth_has_addrs; then
    ok "$(ip -br addr show dev "$ifc")"
  fi
  if [ "$route_bad" = 1 ]; then
    fail eth.default_route "Đường Internet mặc định đang đi qua $ifc" "the default route goes via $ifc" "$args"
  fi
}

step_wifi() {
  local active cons act ssid="$CM5_WIFI_SSID"
  if [ -z "$ssid" ]; then
    info "CM5_WIFI_SSID trống, giữ nguyên Wi-Fi hiện tại"
    step_end skip wifi.disabled "CM5_WIFI_SSID is empty, Wi-Fi left as is"
    return 0
  fi
  command -v nmcli >/dev/null 2>&1 || fail wifi.no_nm "Không có nmcli (NetworkManager) để cấu hình Wi-Fi" \
    "nmcli (NetworkManager) is required to configure Wi-Fi"
  active="$(nmcli -t -f ACTIVE,SSID device wifi 2>/dev/null || true)"
  if grep -qxF "yes:$ssid" <<<"$active"; then
    ok "Đang nối Wi-Fi '$ssid'"
    step_end skip wifi.already "already connected to '$ssid'" "$(jobj ssid "$ssid")"
    return 0
  fi
  [ -n "$CM5_WIFI_PSK" ] || fail wifi.no_psk "Thiếu CM5_WIFI_PSK trong secrets.env" "CM5_WIFI_PSK is missing in secrets.env"
  cons="$(nmcli -t -f NAME connection show 2>/dev/null || true)"
  if grep -qxF "$ssid" <<<"$cons"; then act=update; else act=create; fi
  ev_change nm "$ssid" "$act" "iface=$CM5_WIFI_IFACE" "ssid=$ssid"
  if [ "$PREVIEW" = 1 ]; then
    info "Sẽ nối Wi-Fi '$ssid' trên $CM5_WIFI_IFACE"
    return 0
  fi
  record_nm "$ssid"
  nmcli device wifi connect "$ssid" password "$CM5_WIFI_PSK" ifname "$CM5_WIFI_IFACE" >/dev/null \
    || fail wifi.connect_failed "Không nối được Wi-Fi '$ssid' trên $CM5_WIFI_IFACE" \
      "could not connect to Wi-Fi '$ssid' on $CM5_WIFI_IFACE" "$(jobj ssid "$ssid" iface "$CM5_WIFI_IFACE")"
  ok "Đã nối Wi-Fi '$ssid'"
}

step_tailscale() {
  local ts_args up=0 ip
  if [ "$CM5_INSTALL_TAILSCALE" != 1 ]; then
    info "CM5_INSTALL_TAILSCALE không bật, bỏ qua"
    step_end skip tailscale.disabled "Tailscale is not enabled"
    return 0
  fi
  if command -v tailscale >/dev/null 2>&1 && tailscale ip -4 >/dev/null 2>&1; then up=1; fi
  if [ "$up" = 1 ]; then
    ip="$(tailscale ip -4 | head -1)"
    check_ev tailscale.up true tailscale.up "Tailscale đã đăng nhập: $ip" "Tailscale is logged in: $ip" "$(jobj ip "$ip")"
    step_end skip tailscale.already_up "Tailscale already logged in" "$(jobj ip "$ip")"
    return 0
  fi
  check_ev tailscale.up false tailscale.down "Tailscale chưa đăng nhập" "Tailscale is not logged in"
  if ! command -v tailscale >/dev/null 2>&1; then
    ev_change package tailscale install
    if [ "$PREVIEW" != 1 ]; then curl -fsSL https://tailscale.com/install.sh | sh; fi
  fi
  if [ "$PREVIEW" = 1 ]; then
    ev_change service tailscaled.service start
    info "Sẽ chạy tailscale up --hostname=$CM5_TS_HOSTNAME"
    if [ -z "$TS_AUTHKEY" ]; then
      ev_info tailscale.login_needed "no TS_AUTHKEY: open the Tailscale login link when applying"
    fi
    return 0
  fi
  record_service tailscaled.service
  # Không dùng --ssh: Tailscale SSH bắt xác thực lại định kỳ, agent không tự vào được.
  ts_args=(up --hostname="$CM5_TS_HOSTNAME")
  if [ -n "$TS_AUTHKEY" ]; then
    ts_args+=(--authkey="$TS_AUTHKEY")
  else
    info "Không có TS_AUTHKEY: mở đường link Tailscale in ra dưới đây để đăng nhập (chờ tối đa 5 phút)"
    ev_info tailscale.login_needed "no TS_AUTHKEY: open the Tailscale login link printed in the log"
  fi
  ev_change service tailscaled.service start
  if ! timeout 300 tailscale "${ts_args[@]}"; then
    warn_code tailscale.login_pending "Tailscale chưa đăng nhập xong; chạy lại bước này sau khi mở link" \
      "Tailscale login is not finished; rerun this step after opening the link"
    return 0
  fi
  ok "Tailscale: $(tailscale ip -4 | head -1)"
  warn "Nhớ tắt hết hạn khóa: https://login.tailscale.com/admin/machines -> ... cạnh $CM5_TS_HOSTNAME -> Disable key expiry"
}

step_router() {
  local conf router_changed=0 vps_ip=""
  if [ ! -x "$ROUTER_BIN" ]; then
    if [ "$PREVIEW" = 1 ]; then
      warn_code router.not_installed "Chưa có $ROUTER_BIN (gói uavlink-router)" \
        "$ROUTER_BIN is missing (package uavlink-router)" "$(jobj path "$ROUTER_BIN")"
    else
      fail router.not_installed "Chưa có $ROUTER_BIN: cài gói uavlink-router" \
        "$ROUTER_BIN is missing: install the uavlink-router package" "$(jobj path "$ROUTER_BIN")"
    fi
  fi
  if [ -n "$VPS_PUBLIC_IP" ]; then
    vps_ip="$(resolve_ipv4 "$VPS_PUBLIC_IP" || true)"
    if [ -n "$vps_ip" ] && ping -c 2 -W 2 "$vps_ip" >/dev/null 2>&1; then
      check_ev vps.reach true vps.reach_ok "VPS $VPS_PUBLIC_IP trả lời ping" "VPS $VPS_PUBLIC_IP answers ping" "$(jobj host "$VPS_PUBLIC_IP")"
    else
      # ICMP có thể bị chặn nên đây chỉ là cảnh báo
      check_ev vps.reach false vps.unreachable "VPS $VPS_PUBLIC_IP không trả lời ping (ICMP có thể bị chặn)" \
        "VPS $VPS_PUBLIC_IP does not answer ping (ICMP may be blocked)" "$(jobj host "$VPS_PUBLIC_IP")"
      step_warn vps.unreachable "VPS does not answer ping"
    fi
  fi

  # Router tự build cũ (mavlink-router.service) tranh cùng cổng: ghi lại trạng thái rồi tắt.
  if systemctl cat mavlink-router.service >/dev/null 2>&1; then
    if svc_is_enabled mavlink-router.service || svc_is_active mavlink-router.service; then
      ev_info router.hand_built_disabled "disabling the hand-built mavlink-router.service"
      svc_do disable-now mavlink-router.service || true
      info "Đã tắt mavlink-router.service tự build (thay bằng $ROUTER_UNIT)"
    fi
  fi

  if [ -n "$CM5_ROUTER_LOG_DIR" ] && [ "$PREVIEW" != 1 ]; then make_dir "$CM5_ROUTER_LOG_DIR" -m 755; fi
  render_router_conf_or_fail
  conf="$ROUTER_CONF"
  if write_if_changed /etc/mavlink-router/main.conf 644 <<<"$conf"; then router_changed=1; fi
  if setup_router_service "$CM5_ROUTER_LOG_DIR"; then router_changed=1; fi
  restart_router "$router_changed"
}

# Giao diện không phải CM5_ETH_IFACE nối LAN/Wi-Fi nhà (en*, eth*, wl*), dùng cho CM5_MAVLINK_ALLOW_LAN.
lan_ifaces() {
  local n
  { ip -o link show 2>/dev/null | awk -F': ' '{sub(/@.*/, "", $2); print $2}'; echo "$CM5_WIFI_IFACE"; } \
    | sort -u | while read -r n; do
    case "$n" in en* | eth* | wl*) [ "$n" = "$CM5_ETH_IFACE" ] || echo "$n" ;; esac
  done
}

step_security() {
  local home rules n
  ensure_auto_updates
  if [ "$SSH_HARDEN" = 1 ] && require_cm5_user; then
    home="$(getent passwd "$CM5_USER" | cut -d: -f6)"
    harden_ssh "$home/.ssh/authorized_keys"
  fi
  if [ "$CM5_ENABLE_UFW" = 1 ]; then
    # eth0 là dây riêng tới X25; Tailscale đã tự xác thực; Wi-Fi/LAN nhà chỉ mở SSH (và MAVLink nếu bật).
    rules=("allow in on $CM5_ETH_IFACE" "allow in on tailscale0" "allow 41641/udp")
    if [ "$CM5_SSH_ALLOW_LAN" = 1 ]; then rules+=("allow $(ssh_port)/tcp"); fi
    if [ "${CM5_MAVLINK_ALLOW_LAN:-0}" = 1 ]; then
      while read -r n; do
        [ -n "$n" ] || continue
        rules+=("allow in on $n to any port $CM5_TCP_SERVER_PORT proto tcp" "allow in on $n to any port $MAVLINK_LAN_PORT proto udp")
      done < <(lan_ifaces)
    fi
    apply_ufw "${rules[@]}"
  fi
}

# ---- Điều khiển các bước ----------------------------------------------------------------------

step_title() {
  case "$1" in
    packages) echo "Gói hệ thống" ;;
    usb) echo "Cổng USB của X25" ;;
    eth) echo "Cổng LAN nối X25 ($CM5_ETH_IFACE)" ;;
    wifi) echo "Wi-Fi" ;;
    tailscale) echo "Tailscale" ;;
    router) echo "uavlink-router" ;;
    security) echo "Bảo mật" ;;
  esac
}

# run_step <id> <số thứ tự>/<tổng>: chạy step_<id> trong tiến trình con (để `fail` thoát được), rồi phát sự kiện cuối.
# Khi áp dụng, bước hỏng thì dừng cả script; khi xem trước, chạy tiếp các bước còn lại.
run_step() {
  local id="$1" pos="$2" rc kind a b c d state=ok code="" msg="" args="" warned=0
  CUR_STEP="$id"
  STEP_FILE="$(mktemp)"
  export CUR_STEP STEP_FILE
  step "$pos $(step_title "$id")"
  ev_step "$id" start
  set +e
  ( set -e; "step_$id" )
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    while IFS=$'\t' read -r kind a b c d; do
      case "$kind" in
        warn) if [ "$warned" = 0 ]; then warned=1; state=warn; code="$a"; msg="$b"; args="$c"; fi ;;
        end) state="$a"; code="$b"; msg="$c"; args="$d"; break ;;
      esac
    done < "$STEP_FILE"
    ev_step "$id" "$state" "$code" "$msg" "$args"
  else
    if [ "$rc" -ne 10 ]; then
      ev_step "$id" fail step.failed "step $id failed (exit $rc)"
      bad "Bước $id hỏng (mã thoát $rc)"
    fi
    STEPS_FAILED=$((STEPS_FAILED + 1))
    if [ "$PREVIEW" != 1 ]; then
      rm -f "$STEP_FILE"
      exit 1
    fi
  fi
  rm -f "$STEP_FILE"
  CUR_STEP=""
}

main() {
  local want="" print_config=0 s i=0 n selected=() STEPS_FAILED=0 CUR_STEP="" STEP_FILE=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --preview) PREVIEW=1 ;;
      --print-config) print_config=1 ;;
      --steps) shift; [ $# -gt 0 ] || usage; want="$1" ;;
      --steps=*) want="${1#--steps=}" ;;
      -h | --help) usage ;;
      *) usage ;;
    esac
    shift
  done
  export PREVIEW

  if [ -n "$want" ]; then
    for s in ${want//,/ }; do
      case " ${ALL_STEPS[*]} " in *" $s "*) ;; *) echo "Bước không có: $s" >&2; usage ;; esac
    done
    for s in "${ALL_STEPS[@]}"; do
      case ",$want," in *",$s,"*) selected+=("$s") ;; esac
    done
  else
    selected=("${ALL_STEPS[@]}")
  fi

  load_config
  if [ "$print_config" = 1 ]; then
    render_router_conf_or_fail
    printf '%s\n' "$ROUTER_CONF"
    return 0
  fi

  if [ "$PREVIEW" != 1 ]; then
    if uavlink_is_wsl && [ "${UAVLINK_ALLOW_WSL:-0}" != 1 ]; then
      bad "Đang chạy trên WSL: không áp dụng thay đổi thật (đặt UAVLINK_ALLOW_WSL=1 nếu thực sự muốn). Dùng --preview để xem trước."
      ev_step env fail env.wsl "refusing to change the system on WSL (set UAVLINK_ALLOW_WSL=1 to force)"
      return 4
    fi
    if [ "$(id -u)" -ne 0 ]; then
      bad "Cần quyền root để áp dụng: chạy lại bằng sudo (hoặc dùng --preview)"
      ev_step env fail env.not_root "root is required to apply (or use --preview)"
      return 4
    fi
  fi
  export DEBIAN_FRONTEND=noninteractive

  n=${#selected[@]}
  for s in "${selected[@]}"; do
    i=$((i + 1))
    run_step "$s" "$i/$n"
  done

  if [ "$PREVIEW" = 1 ]; then
    step "Xem trước xong (không ghi gì)"
    if [ "$STEPS_FAILED" -gt 0 ]; then return 3; fi
    return 0
  fi

  # Để `apt purge` gỡ được sau khi tệp gói đã mất.
  install -d -m 755 "$STATE_DIR"
  if [ -f "$ROOT_DIR/cm5/uninstall-cm5.sh" ]; then
    install -m 755 "$ROOT_DIR/cm5/uninstall-cm5.sh" "$STATE_DIR/uninstall.sh"
  else
    warn_code uninstall.missing "Chưa có uninstall-cm5.sh để chép vào $STATE_DIR/uninstall.sh" \
      "uninstall-cm5.sh is missing, not copied to $STATE_DIR/uninstall.sh"
  fi
  install -m 644 "$ROOT_DIR/lib/events.sh" "$STATE_DIR/events.sh"

  step "Xong phần CM5"
  echo "  Cấu hình router : /etc/mavlink-router/main.conf"
  echo "  X25 -> CM5      : UDP $CM5_LAN_IP:$MAVLINK_LAN_PORT"
  if [ -n "$VPS_PUBLIC_IP" ]; then echo "  CM5 -> VPS      : UDP $VPS_PUBLIC_IP:$VPS_UPLINK_PORT"; fi
  echo "  Bước tiếp theo  : cấu hình X25 (uavlink x25 …) rồi chạy verify"
}

main "$@"
