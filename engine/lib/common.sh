# shellcheck shell=bash
# Hàm dùng chung cho các script cài đặt. Nạp bằng `. lib/common.sh`, không chạy trực tiếp.

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Nơi engine ghi lại trạng thái cũ để gỡ cài đặt (ARCHITECTURE.md mục 4.2).
STATE_DIR="${UAVLINK_STATE_DIR:-/var/lib/uavlink}"
MANIFEST="$STATE_DIR/manifest.tsv"
BACKUP_DIR="$STATE_DIR/backup"

# PREVIEW=1: không ghi gì (tệp, dịch vụ, NetworkManager, ufw, gói, user), chỉ phát sự kiện `change`.
PREVIEW="${PREVIEW:-0}"

if [ -t 1 ]; then
  C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_YEL=$'\e[33m'; C_CYN=$'\e[36m'; C_BLD=$'\e[1m'; C_OFF=$'\e[0m'
else
  C_RED=; C_GRN=; C_YEL=; C_CYN=; C_BLD=; C_OFF=
fi

step() { printf '\n%s=== %s ===%s\n' "$C_BLD" "$*" "$C_OFF"; }
info() { printf '%s==>%s %s\n' "$C_CYN" "$C_OFF" "$*"; }
ok()   { printf '%s[OK]%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
bad()  { printf '%s[HỎNG]%s %s\n' "$C_RED" "$C_OFF" "$*"; }
warn() { printf '%s[CẢNH BÁO]%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }

# Lỗi không có mã riêng. Trong một bước của install-cm5.sh (CUR_STEP đặt) thì báo bước hỏng và thoát 10.
die() {
  printf '%s[LỖI]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2
  if [ -n "${CUR_STEP:-}" ]; then
    ev_step "$CUR_STEP" fail step.failed "$*"
    exit 10
  fi
  exit 1
}

# fail <mã> <thông báo vi> <thông báo en> [args-json]: bước hỏng, có mã để giao diện dịch.
fail() {
  printf '%s[LỖI]%s %s\n' "$C_RED" "$C_OFF" "$2" >&2
  ev_step "${CUR_STEP:-unknown}" fail "$1" "$3" "${4:-}"
  exit 10
}

# JSON object từ các cặp khoá giá trị: jobj iface eth0 ...
jobj() {
  local out='{' sep=''
  while [ $# -ge 2 ]; do
    out+="$sep$(ev_str "$1"):$(ev_str "$2")"; sep=,; shift 2
  done
  printf '%s}' "$out"
}

# Trạng thái cuối của bước hiện tại nằm trong $STEP_FILE (install-cm5.sh đọc lại sau khi bước chạy xong).
#   step_end <skip|warn|ok> [mã] [thông báo en] [args-json]   đặt trạng thái cuối (gọi cuối bước)
#   step_warn <mã> [thông báo en] [args-json]                          ghi nhận một cảnh báo (bước thành `warn`)
step_end()  { [ -z "${STEP_FILE:-}" ] || printf 'end\t%s\t%s\t%s\t%s\n' "$1" "${2:-}" "${3:-}" "${4:-}" >> "$STEP_FILE"; }
step_warn() { [ -z "${STEP_FILE:-}" ] || printf 'warn\t%s\t%s\t%s\n' "$1" "${2:-}" "${3:-}" >> "$STEP_FILE"; }

# warn_code <mã> <vi> <en> [args-json]: cảnh báo có mã (in ra, phát `info`, bước thành `warn`).
warn_code() {
  warn "$2"
  ev_info "$1" "$3" "${4:-}"
  step_warn "$1" "$3" "${4:-}"
}

# check_ev <id> <true|false> <mã> <vi> <en> [args-json]: kết quả một phép kiểm tra chỉ-đọc.
check_ev() {
  ev_check "$1" "$2" "$3" "$5" "${6:-}"
  if [ "$2" = true ]; then ok "$4"; else warn "$4"; fi
}

# Nạp $UAVLINK_CONFIG_DIR/config.env (mặc định /etc/uavlink) rồi secrets.env (thắng nếu trùng khoá).
# Mọi biến được export cho tiến trình con. Khoá thiếu trong tệp lấy giá trị mặc định để cấu hình cũ vẫn chạy.
load_config() {
  local dir="${UAVLINK_CONFIG_DIR:-/etc/uavlink}"
  [ -f "$dir/config.env" ] || die "Không thấy tệp cấu hình $dir/config.env"
  # shellcheck source=events.sh
  . "$ROOT_DIR/lib/events.sh"
  set -a
  # shellcheck disable=SC1091
  . "$dir/config.env"
  if [ -f "$dir/secrets.env" ]; then
    if [ -r "$dir/secrets.env" ]; then
      # shellcheck disable=SC1091
      . "$dir/secrets.env"
    else
      warn "Không đọc được $dir/secrets.env (cần root), bỏ qua các bí mật"
    fi
  fi
  config_defaults
  set +a
  if [ -z "$CM5_USER" ]; then CM5_USER="$(detect_cm5_user || true)"; fi
  return 0
}

# Giá trị mặc định cho khoá chưa có (chỉ khi biến chưa đặt: rỗng là giá trị hợp lệ cho một số khoá).
config_defaults() {
  : "${UAVLINK_ROLE=cm5}" "${UAVLINK_PRESET=}"
  : "${CM5_USER=}" "${CM5_ETH_IFACE=eth0}" "${CM5_ETH_CON_NAME=x25-link}" "${CM5_LAN_IP=192.168.50.2}"
  : "${CM5_ETH_EXTRA_ADDRS=192.168.144.2/24}" "${CM5_TCP_SERVER_PORT=5760}" "${CM5_EXTRA_UDP_TARGETS=}"
  : "${CM5_ROUTER_LOG_DIR=/var/log/mavlink-router}" "${CM5_ROUTER_LOG_MODE=while-armed}"
  : "${CM5_DISABLE_MODEMMANAGER=1}" "${CM5_MAVLINK_ALLOW_LAN=0}"
  : "${X25_IP=192.168.50.1}" "${X25_NETMASK=24}" "${X25_GATEWAY=0.0.0.0}" "${X25_SERIAL=auto}" "${X25_BAUD=115200}"
  : "${X25_BACKUP_DIR=/var/lib/uavlink/x25-backups}" "${MAVLINK_LAN_PORT=14550}"
  : "${VPS_PUBLIC_IP=}" "${VPS_UPLINK_PORT=14555}"
  : "${ROUTER_REPORT_STATS=false}"
  : "${CM5_INSTALL_TAILSCALE=0}" "${CM5_TS_HOSTNAME=cm5}" "${CM5_WIFI_IFACE=wlan0}" "${CM5_WIFI_SSID=}"
  : "${MAVLINK_SIGNING=1}" "${SSH_HARDEN=1}" "${CM5_ENABLE_UFW=1}" "${CM5_SSH_ALLOW_LAN=1}"
  : "${SERVICE_SANDBOX=1}" "${FIREWALL_ROLLBACK_SECS=180}"
  : "${MAVLINK_SIGNING_PASSPHRASE=}" "${WEB_BASIC_AUTH_PASS=}" "${TS_AUTHKEY=}" "${CM5_WIFI_PSK=}"
}

# User chạy phiên desktop đang hoạt động, nếu không có thì SUDO_USER, nếu không nữa thì uid 1000.
detect_cm5_user() {
  local sid u
  if command -v loginctl >/dev/null 2>&1; then
    while read -r sid _; do
      [ -n "$sid" ] || continue
      if [ "$(loginctl show-session "$sid" -p Active --value 2>/dev/null)" = yes ]; then
        case "$(loginctl show-session "$sid" -p Type --value 2>/dev/null)" in
          x11 | wayland | mir)
            u="$(loginctl show-session "$sid" -p Name --value 2>/dev/null)"
            if [ -n "$u" ] && [ "$u" != root ]; then echo "$u"; return 0; fi ;;
        esac
      fi
    done < <(loginctl list-sessions --no-legend 2>/dev/null)
  fi
  if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then echo "$SUDO_USER"; return 0; fi
  getent passwd 1000 | cut -d: -f1 || true
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "Cần quyền root: chạy lại bằng sudo"
}

# Chạy lệnh bằng root (sudo -n nếu đang là user thường).
as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}

# Chạy lệnh bằng user khác (giữ nguyên nếu đã là user đó).
run_as() {
  local user="$1"; shift
  if [ "$(id -un)" = "$user" ]; then "$@"; else sudo -u "$user" -H "$@"; fi
}

# ---- Manifest: trạng thái cũ để gỡ cài đặt (ARCHITECTURE.md mục 4.2) ------------------------
# Mỗi hàm record_* ghi một dòng `kind<TAB>target<TAB>trạng thái cũ<TAB>bản sao lưu hoặc -` trước lần đổi đầu tiên.
# Chỉ dòng đầu của mỗi (kind, target) có nghĩa, nên gọi lại là vô hại. Không làm gì khi PREVIEW=1.

manifest_seen() {
  [ -f "$MANIFEST" ] && awk -F'\t' -v k="$1" -v t="$2" '$1 == k && $2 == t {f = 1} END {exit !f}' "$MANIFEST"
}

manifest_add() {
  install -d -m 755 "$STATE_DIR"
  install -d -m 700 "$BACKUP_DIR"
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$MANIFEST"
}

backup_name() {
  local n="${1//\//_}"
  echo "${n#_}"
}

record_file() {
  local p="$1" b
  [ "$PREVIEW" != 1 ] || return 0
  manifest_seen file "$p" && return 0
  if [ -e "$p" ]; then
    install -d -m 700 "$BACKUP_DIR"
    b="$BACKUP_DIR/$(backup_name "$p")"
    cp -a "$p" "$b"
    manifest_add file "$p" present "$b"
  else
    manifest_add file "$p" absent -
  fi
}

# systemd trả lời được chưa? Khi unattended-upgrades nâng cấp systemd, nó tự nạp lại và systemctl tạm thời báo lỗi
# ("Transport endpoint is not connected"): chờ, tối đa UAVLINK_SYSTEMD_WAIT giây (mặc định 60), rồi dừng bước.
# Không chờ trên máy không chạy systemd.
systemd_ready() {
  local i secs="${UAVLINK_SYSTEMD_WAIT:-60}"
  [ -d /run/systemd/system ] || return 0
  for ((i = 0; i <= secs; i++)); do
    systemctl show -p Version --value >/dev/null 2>&1 && return 0
    [ "$i" -lt "$secs" ] && sleep 1
  done
  fail systemd.unreachable "systemd không trả lời (systemctl lỗi), dừng để không ghi nhầm trạng thái dịch vụ" \
    "systemd does not answer (systemctl fails), stopping so that no wrong service state is recorded"
}

# Trạng thái cũ đọc được từ systemd, không bao giờ đoán: một trạng thái ghi nhầm (vd "absent" khi systemctl lỗi)
# làm gỡ cài đặt tắt nhầm dịch vụ đó.
record_service() {
  local u="$1" out k v load="" en="" act="" st
  [ "$PREVIEW" != 1 ] || return 0
  manifest_seen service "$u" && return 0
  systemd_ready
  out="$(systemctl show -p LoadState -p UnitFileState -p ActiveState "$u" 2>/dev/null)" \
    || fail systemd.unreachable "Không đọc được trạng thái $u từ systemd" "cannot read the state of $u from systemd"
  while IFS='=' read -r k v; do
    case "$k" in LoadState) load="$v" ;; UnitFileState) en="$v" ;; ActiveState) act="$v" ;; esac
  done <<<"$out"
  case "$load" in
    not-found) st=absent ;;
    masked) st=masked ;;
    loaded)
      case "$en" in masked*) st=masked ;; enabled*) st=enabled ;; *) st=disabled ;; esac
      if [ "$st" != masked ]; then
        if [ "$act" = active ]; then st="$st-active"; else st="$st-inactive"; fi
      fi ;;
    *) fail systemd.unreachable "Không đọc được trạng thái $u từ systemd ($load)" "cannot read the state of $u from systemd ($load)" ;;
  esac
  manifest_add service "$u" "$st" -
}

# record_group <user> <group>: user sắp được thêm vào nhóm (gọi chỉ khi chưa là thành viên).
record_group() {
  [ "$PREVIEW" != 1 ] || return 0
  manifest_seen group "$1:$2" && return 0
  manifest_add group "$1:$2" absent -
}

# make_dir <thư mục> [tùy chọn install -d…]: tạo thư mục; mỗi cấp chưa có được ghi vào manifest (`file … absent`)
# để gỡ cài đặt xoá lại khi nó trống. Thư mục đã có chỉ bị đổi quyền/chủ khi có tùy chọn.
make_dir() {
  local dir="$1" d missing=(); shift
  d="$dir"
  while [ ! -e "$d" ] && [ "$d" != / ] && [ "$d" != . ]; do missing=("$d" "${missing[@]}"); d="$(dirname "$d")"; done
  for d in "${missing[@]}"; do record_file "$d"; done
  if [ ${#missing[@]} -gt 0 ] || [ $# -gt 0 ]; then install -d "$@" "$dir"; fi
}

record_ufw() {
  local st b f
  [ "$PREVIEW" != 1 ] || return 0
  manifest_seen ufw ufw && return 0
  if grep -q '^Status: active' <<<"$(ufw status 2>/dev/null)"; then st=active; else st=inactive; fi
  b="$BACKUP_DIR/ufw"
  install -d -m 700 "$b"
  for f in /etc/ufw/*.rules /etc/ufw/ufw.conf; do
    [ -f "$f" ] && cp -a "$f" "$b/"
  done
  manifest_add ufw ufw "$st" "$b"
}

# Kết nối có sẵn được lưu ở đâu thì sao lưu ở đó: tệp keyfile trong /etc/NetworkManager/system-connections (bản
# sao lưu đi kèm dòng nm), hoặc (Ubuntu 24.04, NetworkManager ghi qua netplan) /etc/netplan/90-NM-<uuid>.yaml, ghi
# bằng một dòng `file` riêng; khi đó dòng nm không có bản sao lưu và gỡ cài đặt chỉ cần cho NetworkManager đọc lại.
NM_KEYFILE_DIR="${UAVLINK_NM_DIR:-/etc/NetworkManager/system-connections}"
NETPLAN_DIR="${UAVLINK_NETPLAN_DIR:-/etc/netplan}"
record_nm() {
  local name="$1" cons uuid file b=-
  [ "$PREVIEW" != 1 ] || return 0
  manifest_seen nm "$name" && return 0
  cons="$(nmcli -t -f NAME connection show 2>/dev/null || true)"
  if grep -qxF "$name" <<<"$cons"; then
    uuid="$(nmcli -g connection.uuid connection show "$name" 2>/dev/null || true)"
    file="$(nmcli -t -f UUID,FILENAME connection show 2>/dev/null | awk -F: -v u="$uuid" '$1 == u && !f {sub(/^[^:]*:/, ""); print; f = 1}')"
    case "$file" in
      "$NM_KEYFILE_DIR"/*)
        install -d -m 700 "$BACKUP_DIR"
        b="$BACKUP_DIR/nm-$(backup_name "$name").nmconnection"
        cp -a "$file" "$b" ;;
      */netplan-*)
        if [ -n "$uuid" ] && [ -f "$NETPLAN_DIR/90-NM-$uuid.yaml" ]; then record_file "$NETPLAN_DIR/90-NM-$uuid.yaml"; fi ;;
    esac
    manifest_add nm "$name" present "$b"
  else
    manifest_add nm "$name" absent -
  fi
}

record_user() {
  [ "$PREVIEW" != 1 ] || return 0
  manifest_seen user "$1" && return 0
  manifest_add user "$1" absent -
}

# ---- Ghi tệp, gói, dịch vụ -----------------------------------------------------------------

# Thay giá trị bí mật (CM5_WIFI_PSK, ...) bằng <redacted> trong văn bản từ stdin.
redact_secrets() {
  local text v k
  text="$(cat; printf x)"; text="${text%x}"
  for k in MAVLINK_SIGNING_PASSPHRASE WEB_BASIC_AUTH_PASS TS_AUTHKEY CM5_WIFI_PSK; do
    v="${!k:-}"
    [ "${#v}" -ge 4 ] || continue
    text="${text//"$v"/<redacted>}"
  done
  printf '%s' "$text"
}

# file_diff <đích> <tệp mới>: diff thống nhất, bí mật đã che.
file_diff() {
  local dst="$1" new="$2" old=/dev/null label="a$1"
  if [ -f "$dst" ] && [ -r "$dst" ]; then old="$dst"; else label=/dev/null; fi
  { diff -u --label "$label" --label "b$dst" "$old" "$new" || true; } | redact_secrets
}

# Cài các gói apt còn thiếu (cần root). Chờ tối đa 5 phút nếu apt đang bị khóa.
apt_install() {
  local missing=() p
  for p in "$@"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q 'install ok installed' || missing+=("$p")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    ok "Đã có đủ gói: $*"
    return 0
  fi
  for p in "${missing[@]}"; do ev_change package "$p" install; done
  if [ "$PREVIEW" = 1 ]; then
    info "Sẽ cài gói: ${missing[*]}"
    return 0
  fi
  info "Cài gói: ${missing[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update -q
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 install -y -q "${missing[@]}"
}

# Có Internet không: ping 1.1.1.1, hoặc (khi ICMP bị chặn) mở được kết nối TCP tới một trong
# UAVLINK_INTERNET_HOSTS ("ip:cổng ip:cổng", mặc định như net_probe.py).
internet_up() {
  local hp
  ping -c 2 -W 3 1.1.1.1 >/dev/null 2>&1 && return 0
  for hp in ${UAVLINK_INTERNET_HOSTS:-1.1.1.1:443 8.8.8.8:443}; do
    timeout 4 bash -c "exec 3<>/dev/tcp/${hp%:*}/${hp##*:}" 2>/dev/null && return 0
  done
  return 1
}

is_ipv4() {
  [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]
}

# In IPv4 của một tên miền (hoặc chính địa chỉ nếu đã là IP). mavlink-router chỉ nhận IP.
resolve_ipv4() {
  if is_ipv4 "$1"; then echo "$1"; return 0; fi
  local ip
  ip="$(getent ahostsv4 "$1" 2>/dev/null | awk 'NR==1{print $1}')"
  [ -n "$ip" ] || return 1
  echo "$ip"
}

# write_if_changed <đường dẫn> <quyền>   (nội dung đọc từ stdin)
# Trả 0 nếu đã ghi (tệp mới hoặc nội dung khác; khi xem trước: sẽ ghi), 1 nếu giữ nguyên.
# Bản gốc được sao lưu vào $BACKUP_DIR và ghi vào manifest trước lần ghi đầu tiên.
write_if_changed() {
  local dst="$1" mode="${2:-644}" tmp action=create diff=""
  tmp="$(mktemp)"
  cat > "$tmp"
  if [ -f "$dst" ] && [ -r "$dst" ] && cmp -s "$tmp" "$dst"; then
    rm -f "$tmp"
    return 1
  fi
  [ ! -e "$dst" ] || action=update
  if [ -e "$dst" ] && [ ! -r "$dst" ]; then
    warn_code file.unreadable "Không đọc được $dst (cần root), không so sánh được nội dung" \
      "cannot read $dst (needs root), contents not compared" "$(jobj path "$dst")"
  else
    diff="$(file_diff "$dst" "$tmp")"
  fi
  if [ "$PREVIEW" = 1 ]; then
    ev_change file "$dst" "$action" "diff=$diff"
    info "Sẽ ghi $dst"
    rm -f "$tmp"
    return 0
  fi
  make_dir "$(dirname "$dst")"
  record_file "$dst"
  install -m "$mode" "$tmp" "$dst"
  rm -f "$tmp"
  ev_change file "$dst" "$action" "diff=$diff"
  info "Đã ghi $dst"
  return 0
}

# svc_do <enable|enable-now|disable|disable-now|start|restart|mask|unmask> <unit>
# Phát `change`, ghi manifest rồi mới đổi (không đổi gì khi xem trước). Trả mã thoát của systemctl.
svc_do() {
  local action="$1" unit="$2" ev="$1"
  case "$action" in
    enable-now) ev=enable ;;
    disable-now) ev=disable ;;
  esac
  ev_change service "$unit" "$ev"
  if [ "$PREVIEW" = 1 ]; then
    info "Sẽ $ev $unit"
    return 0
  fi
  record_service "$unit"
  case "$action" in
    enable-now) systemctl enable --now "$unit" >/dev/null 2>&1 ;;
    disable-now) systemctl disable --now "$unit" >/dev/null 2>&1 ;;
    enable | disable | mask | unmask) systemctl "$action" "$unit" >/dev/null 2>&1 ;;
    *) systemctl "$action" "$unit" ;;
  esac
}

svc_is_enabled() { systemd_ready; systemctl is-enabled --quiet "$1" 2>/dev/null; }
svc_is_active()  { systemd_ready; systemctl is-active --quiet "$1" 2>/dev/null; }

# Chờ một URL HTTP trả lời 2xx, tối đa <giây>.
wait_http() {
  local url="$1" secs="${2:-30}" i
  for ((i = 0; i < secs; i++)); do
    if curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

# Các tùy chọn systemd giới hạn quyền cho dịch vụ.
SANDBOX_OPTIONS="NoNewPrivileges=yes
CapabilityBoundingSet=
UMask=0027
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
DevicePolicy=closed
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native"

ensure_system_user() {
  if id "$1" >/dev/null 2>&1; then return 0; fi
  ev_change user "$1" create
  if [ "$PREVIEW" = 1 ]; then
    info "Sẽ tạo user hệ thống $1"
    return 0
  fi
  record_user "$1"
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$1"
  info "Đã tạo user hệ thống $1"
}

ROUTER_UNIT=uavlink-router.service
# shellcheck disable=SC2034  # dùng trong cm5/install-cm5.sh
ROUTER_BIN=/usr/lib/uavlink-router/mavlink-routerd
ROUTER_DROPIN_DIR=/etc/systemd/system/uavlink-router.service.d
ROUTER_SANDBOX_FILE=$ROUTER_DROPIN_DIR/20-sandbox.conf

# setup_router_service [thư mục uavlink-router cần ghi, vd thư mục log]
# Unit do gói uavlink-router cung cấp; ở đây chỉ ghi drop-in tự khởi động lại sau sự cố/mất mạng và
# (SERVICE_SANDBOX=1) chạy bằng user mavlink-router với quyền tối thiểu. Trả 0 nếu có thay đổi.
setup_router_service() {
  local rw_dir="${1:-}" changed=1 dropin sandbox
  dropin="# Tạo bởi uavlink: chờ mạng, luôn tự chạy lại
[Unit]
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=3"
  if write_if_changed "$ROUTER_DROPIN_DIR/10-restart.conf" 644 <<<"$dropin"; then changed=0; fi

  if [ "$SERVICE_SANDBOX" = 1 ]; then
    ensure_system_user mavlink-router
    sandbox="# Tạo bởi uavlink: mavlink-router không chạy bằng root.
# DevicePolicy=closed chặn /dev/tty*: đặt SERVICE_SANDBOX=0 nếu thêm endpoint serial.
[Service]
User=mavlink-router
Group=mavlink-router
$SANDBOX_OPTIONS
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK"
    if [ -n "$rw_dir" ]; then
      if [ "$PREVIEW" != 1 ]; then
        make_dir "$rw_dir" -m 750 -o mavlink-router -g mavlink-router
        chown -R mavlink-router:mavlink-router "$rw_dir"
      fi
      sandbox="$sandbox
ReadWritePaths=$rw_dir"
    fi
    if write_if_changed "$ROUTER_SANDBOX_FILE" 644 <<<"$sandbox"; then changed=0; fi
  elif [ -f "$ROUTER_SANDBOX_FILE" ]; then
    ev_change file "$ROUTER_SANDBOX_FILE" delete
    if [ "$PREVIEW" != 1 ]; then
      record_file "$ROUTER_SANDBOX_FILE"
      rm -f "$ROUTER_SANDBOX_FILE"
    fi
    changed=0
  fi
  if [ "$PREVIEW" != 1 ]; then
    systemctl daemon-reload
    if ! svc_is_enabled "$ROUTER_UNIT"; then svc_do enable "$ROUTER_UNIT" || true; fi
  elif ! svc_is_enabled "$ROUTER_UNIT"; then
    svc_do enable "$ROUTER_UNIT"
  fi
  return $changed
}

# Khởi động lại uavlink-router nếu cấu hình đổi ($1=1) hoặc dịch vụ đang không chạy.
# Không chạy được trong sandbox thì gỡ sandbox và chạy lại như cũ, để không mất đường MAVLink.
restart_router() {
  if [ "$1" = 1 ] || ! svc_is_active "$ROUTER_UNIT"; then
    if [ "$PREVIEW" = 1 ]; then
      svc_do restart "$ROUTER_UNIT"
      return 0
    fi
    svc_do restart "$ROUTER_UNIT" || true
    sleep 2
    if ! svc_is_active "$ROUTER_UNIT" && [ -f "$ROUTER_SANDBOX_FILE" ]; then
      journalctl -u "$ROUTER_UNIT" -n 15 --no-pager >&2 || true
      warn_code router.sandbox_fallback "uavlink-router không chạy được với sandbox: gỡ $ROUTER_SANDBOX_FILE, chạy lại bằng root" \
        "uavlink-router failed in the sandbox: removed $ROUTER_SANDBOX_FILE, running as root"
      record_file "$ROUTER_SANDBOX_FILE"
      rm -f "$ROUTER_SANDBOX_FILE"
      systemctl daemon-reload
      systemctl restart "$ROUTER_UNIT" || true
      sleep 2
    fi
    if ! svc_is_active "$ROUTER_UNIT"; then
      journalctl -u "$ROUTER_UNIT" -n 30 --no-pager >&2 || true
      fail router.not_running "uavlink-router không chạy được, xem log ở trên" \
        "uavlink-router does not start, see the log"
    fi
    ok "Đã khởi động lại uavlink-router"
  else
    ok "uavlink-router đang chạy, cấu hình không đổi"
  fi
}

# Đếm gói tin khớp bộ lọc tcpdump trong <giây>; thành công nếu bắt được ít nhất 1 gói.
saw_packets() {
  local iface="$1" filter="$2" secs="${3:-8}" n
  n="$( { timeout "$secs" tcpdump -lnni "$iface" -c 3 "$filter" 2>/dev/null || true; } | wc -l)"
  [ "$n" -ge 1 ]
}

# ---- Bảo mật ------------------------------------------------------------------------

ROLLBACK_UNIT=uavlink-rollback
SSH_HARDEN_FILE=/etc/ssh/sshd_config.d/10-uavlink-hardening.conf
UFW_SIG_FILE=/etc/ufw/uavlink.sig

# Hẹn giờ tự tắt UFW và gỡ cấu hình SSH vừa thêm sau FIREWALL_ROLLBACK_SECS giây, phòng khi thay đổi
# làm mất đường SSH. `uavlink confirm` (hoặc trang cài đặt) hủy hẹn giờ bằng một kết nối MỚI sau khi cài xong.
# NO_ROLLBACK=1 để bỏ qua (khi đang ngồi trước máy).
arm_rollback() {
  [ "${NO_ROLLBACK:-0}" != 1 ] || return 0
  if [ "$PREVIEW" = 1 ]; then
    ev_change service "$ROLLBACK_UNIT.timer" start secs:="$FIREWALL_ROLLBACK_SECS"
    return 0
  fi
  if systemctl is-active --quiet "$ROLLBACK_UNIT.timer"; then return 0; fi
  systemctl reset-failed "$ROLLBACK_UNIT.service" "$ROLLBACK_UNIT.timer" >/dev/null 2>&1 || true
  # Không hẹn giờ được thì dừng hẳn, không đổi tường lửa/SSH khi không có đường lui
  systemd-run --quiet --unit="$ROLLBACK_UNIT" --description="uavlink: hoàn tác tường lửa và SSH" \
    --on-active="${FIREWALL_ROLLBACK_SECS}s" /bin/sh -c \
    "ufw --force disable; rm -f $UFW_SIG_FILE $SSH_HARDEN_FILE; systemctl reload ssh || true" \
    || fail rollback.arm_failed "Không đặt được hẹn giờ tự hoàn tác ($ROLLBACK_UNIT), dừng trước khi đổi tường lửa/SSH" \
      "cannot arm the rollback timer ($ROLLBACK_UNIT), stopping before changing the firewall/SSH"
  ev_rollback armed "$FIREWALL_ROLLBACK_SECS"
  warn "Tường lửa/SSH sẽ TỰ HOÀN TÁC sau ${FIREWALL_ROLLBACK_SECS}s nếu không được xác nhận."
  warn "Xác nhận bằng một phiên MỚI: sudo systemctl stop $ROLLBACK_UNIT.timer   (hoặc: uavlink confirm)"
}

ssh_port() {
  local p
  p="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}')"
  echo "${p:-22}"
}

# harden_ssh <tệp authorized_keys của user dùng để quản trị>
# Chỉ cho đăng nhập bằng khóa. Không làm gì nếu user đó chưa có khóa nào.
harden_ssh() {
  local keys="$1" content
  if [ ! -s "$keys" ]; then
    if [ "$PREVIEW" = 1 ] && [ "$(id -u)" -ne 0 ] && [ ! -r "$keys" ]; then
      warn_code ssh.keys_unknown "Không đọc được $keys (cần root): giả định đã có khóa SSH" \
        "cannot read $keys (needs root): assuming SSH keys are present" "$(jobj path "$keys")"
    else
      warn_code ssh.no_keys "$keys trống: giữ nguyên đăng nhập SSH bằng mật khẩu để không tự khóa mình ở ngoài" \
        "$keys is empty: keeping SSH password login so you are not locked out" "$(jobj path "$keys")"
      return 0
    fi
  fi
  content="# Tạo bởi uavlink: chỉ đăng nhập bằng khóa.
# sshd lấy giá trị đọc được đầu tiên; tên 10- để đứng trước 50-cloud-init.conf.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
LoginGraceTime 30"
  if [ -f "$SSH_HARDEN_FILE" ] && [ -r "$SSH_HARDEN_FILE" ] && cmp -s "$SSH_HARDEN_FILE" <(printf '%s\n' "$content"); then
    ok "SSH chỉ cho đăng nhập bằng khóa"
    return 0
  fi
  arm_rollback
  write_if_changed "$SSH_HARDEN_FILE" 644 <<<"$content" || true
  if [ "$PREVIEW" = 1 ]; then return 0; fi
  if ! sshd -t; then
    rm -f "$SSH_HARDEN_FILE"
    fail ssh.config_invalid "Cấu hình sshd lỗi, đã gỡ $SSH_HARDEN_FILE" \
      "sshd configuration is invalid, removed $SSH_HARDEN_FILE"
  fi
  systemctl reload ssh 2>/dev/null || true
  ok "SSH: tắt đăng nhập bằng mật khẩu, root chỉ vào bằng khóa"
  return 0
}

# apply_ufw <luật>...: mỗi đối số là phần sau chữ `ufw` của một luật, vd "allow in on eth0".
# Bộ luật đổi (hoặc UFW đang tắt) thì xoá toàn bộ luật cũ, áp dụng lại và hẹn giờ hoàn tác.
apply_ufw() {
  local sig rule st active=0 known=1
  apt_install ufw
  sig="$(printf '%s\n' "$@" | sha256sum | cut -d' ' -f1)"
  if command -v ufw >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
      st="$(ufw status 2>/dev/null || true)"
      if grep -q '^Status: active' <<<"$st"; then active=1; fi
    else
      known=0
    fi
  fi
  if { [ "$active" = 1 ] || [ "$known" = 0 ]; } && [ "$(cat "$UFW_SIG_FILE" 2>/dev/null)" = "$sig" ]; then
    ok "Tường lửa UFW đã đúng"
    return 0
  fi
  arm_rollback
  for rule in "$@"; do ev_change firewall "$rule" create; done
  if [ "$active" != 1 ]; then ev_change firewall ufw enable; fi
  if [ "$PREVIEW" = 1 ]; then
    info "Sẽ đặt lại UFW với ${#} luật"
    write_if_changed "$UFW_SIG_FILE" 644 <<<"$sig" || true
    return 0
  fi
  record_ufw
  local before f
  before="$(ls /etc/ufw 2>/dev/null)"
  ufw --force reset >/dev/null
  # reset giữ bản sao (*.rules.YYYYMMDD_HHMMSS) của các luật nó thay; bản gốc đã ở $BACKUP_DIR/ufw.
  for f in /etc/ufw/*.rules.[0-9]*_[0-9]*; do
    if [ -e "$f" ] && ! grep -qxF "${f##*/}" <<<"$before"; then rm -f "$f"; fi
  done
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  for rule in "$@"; do
    # shellcheck disable=SC2086
    ufw $rule >/dev/null
  done
  ufw --force enable >/dev/null
  write_if_changed "$UFW_SIG_FILE" 644 <<<"$sig" || true
  ufw status verbose
}

# Bật tự cài bản vá bảo mật hằng ngày (mặc định của Ubuntu, đặt lại nếu đã bị tắt).
ensure_auto_updates() {
  apt_install unattended-upgrades
  write_if_changed /etc/apt/apt.conf.d/20auto-upgrades 644 <<'CONF' || true
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
  if ! svc_is_enabled unattended-upgrades.service || ! svc_is_active unattended-upgrades.service; then
    svc_do enable-now unattended-upgrades.service || true
  fi
  ok "Tự cài bản vá bảo mật (unattended-upgrades) đang bật"
}

check_signing_passphrase() {
  [ "$MAVLINK_SIGNING" = 1 ] || [ -n "$MAVLINK_SIGNING_PASSPHRASE" ] || return 0
  if [[ ! "$MAVLINK_SIGNING_PASSPHRASE" =~ ^[A-Za-z0-9._-]{12,}$ ]]; then
    die "MAVLINK_SIGNING_PASSPHRASE (secrets.env) phải có ít nhất 12 ký tự, chỉ gồm chữ, số và . _ -"
  fi
}
