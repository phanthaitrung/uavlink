# Hướng dẫn tự kiểm tra uavlink trong WSL2

Tài liệu này giúp bạn tự chạy thử toàn bộ gói `uavlink` trong một bản Ubuntu 24.04 **mới tinh** trên WSL2, đúng như
người dùng sẽ làm, **trước khi** đẩy gì lên GitHub hay PPA. Mọi thứ chạy cục bộ, không đụng tới CM5, VPS hay X25 thật.

Có hai cách:

- **A. Chạy tự động** (một lệnh, khoảng 5–10 phút): script dựng gói, tạo distro tạm, kiểm tra từng bước và chụp ảnh.
- **B. Chạy tay** (để tự xem và bấm thử trang cài đặt): cài bằng một dòng `apt` từ kho cục bộ rồi mở trang trên Windows.

## 0. Chuẩn bị (một lần)

| Cần có | Kiểm tra |
|---|---|
| Windows 10/11 có WSL2 | `wsl --status` |
| Git Bash (chạy các script `.sh`) | mở "Git Bash" |
| Distro `uavlink-dev` (Ubuntu 24.04 có công cụ build: debhelper, meson, lintian, shellcheck…) | `wsl -l -v` |
| Ảnh Ubuntu sạch `D:\wsl\images\ubuntu-2404-pristine.tar` (systemd bật, user `pi`) | `dir D:\wsl\images` |
| Microsoft Edge (để chụp ảnh trang) và Node.js 22 trở lên trên Windows | `node --version` |
| Có mạng (apt tải `ufw`, `chrony`, `tcpdump`… từ kho Ubuntu) | |

Mọi lệnh dưới đây chạy trong **Git Bash**, thư mục gốc của repo (`D:\dynorch-projects\mavlink\uavlink`).

## 1. Dựng gói

```bash
bash tools/build-deb.sh                      # gói uavlink  -> out/uavlink_0.1.0_all.deb
bash packaging/uavlink-router/build.sh       # gói uavlink-router -> out/uavlink-router_…_amd64.deb (cắt mạng khi build)
```

Cả hai script tự chạy trong distro `uavlink-dev` và in kết quả `lintian` ở cuối. Lần đầu `uavlink-router` cần mạng để
tải mã nguồn mavlink-router (vào `out/`), các lần sau thì không.

## 2. Cách A: chạy tự động

```bash
bash tests/integration/run.sh              # dựng + thử + xóa distro tạm
bash tests/integration/run.sh --skip-build # dùng các .deb đã có trong out/
bash tests/integration/run.sh --keep       # giữ lại distro `uavlink-it` để xem
```

Script làm theo thứ tự:

1. Tạo kho apt cục bộ `out/repo` (và `out/repo-upgrade` có thêm bản `0.1.1` build tạm, không commit).
2. Nhập distro mới `uavlink-it` từ ảnh sạch.
3. Trong distro: cài `uavlink` bằng apt (kéo theo `uavlink-router`), kiểm tra URL có token, cookie, trang trả 200,
   `uavlink presets`, `uavlink apply --preset dgp-current --preview`, API `/api/precheck` + SSE, từ chối apply thật trên
   WSL, nâng cấp lên 0.1.1, quét log tìm bí mật, rồi `apt purge` và kiểm tra khôi phục từ manifest mẫu.
4. Chụp ảnh trang thật (VI và EN) bằng Edge ẩn → `out/screenshots/real/`.
5. Hủy đăng ký distro (trừ khi có `--keep`).

Kết quả đúng kết thúc bằng dòng `INTEGRATION: ALL CHECKS PASSED`. Mỗi kiểm tra in `ok` hoặc `FAIL` kèm đoạn log.

Chỉ chụp lại ảnh (cần distro `uavlink-it` còn và đã cài `uavlink`): `bash tests/integration/shots.sh uavlink-it`.

## 3. Cách B: chạy tay từng bước

### 3.1 Tạo kho apt cục bộ và distro sạch

```bash
bash tests/integration/make-repo.sh
wsl --import uavlink-try D:\wsl\uavlink-try D:\wsl\images\ubuntu-2404-pristine.tar --version 2
```

> **Quan trọng:** WSL tự tắt distro (và mọi dịch vụ trong đó, kể cả trang cài đặt) khoảng 15 giây sau khi cửa sổ
> terminal cuối cùng đóng. **Giữ một cửa sổ terminal WSL mở** trong suốt lúc thử, nếu không trang sẽ "chết" giữa chừng
> (trình duyệt báo `ERR_CONNECTION_REFUSED`).

Mở một cửa sổ **PowerShell** hoặc Windows Terminal và vào distro:

```powershell
wsl -d uavlink-try
```

(Bạn là user `pi`, có `sudo` không cần mật khẩu.) Nếu `systemctl is-system-running` chưa ra `running` thì chờ vài giây.

### 3.2 Khai báo kho cục bộ thay cho PPA

Trong distro:

```bash
sudo cp -r /mnt/d/dynorch-projects/mavlink/uavlink/out/repo /srv/uavlink-repo
echo 'deb [trusted=yes] file:/srv/uavlink-repo ./' | sudo tee /etc/apt/sources.list.d/uavlink-local.list
sudo apt update
```

(`trusted=yes` chỉ dùng cho kho thử này; PPA thật được ký bằng khóa.)

### 3.3 Cài bằng một dòng apt

```bash
sudo apt install uavlink
```

Gõ lệnh trên một terminal thật (không qua script/cron). Cuối quá trình cài, `postinst` mở trang cài đặt và in ra:

```text
uavlink — trang cài đặt / setup page
Mở trang cài đặt / Open the setup page:
  http://127.0.0.1:8090/?t=<mã-truy-cập>
```

Cài đặt **không thay đổi gì** trên hệ thống (không bật dịch vụ, không đổi mạng/tường lửa). Nếu không thấy URL, chạy lại:

```bash
sudo uavlink setup --no-browser
```

### 3.4 Mở trang từ Windows

Sao chép nguyên URL `http://127.0.0.1:8090/?t=…` vào Edge/Chrome trên Windows (WSL2 chuyển `127.0.0.1` của Windows vào
distro). Thêm `&lang=en` hoặc bấm nút **VI/EN** để đổi ngôn ngữ. Mã truy cập chỉ dùng một lần để đăng nhập (trình duyệt
nhận cookie rồi chuyển về `/`); mở lại bằng `sudo uavlink setup`, mỗi lần có mã mới.

### 3.5 Bấm gì và kỳ vọng gì

| Bước | Bấm | Kỳ vọng |
|---|---|---|
| ① Vai trò | chọn **CM5** → *Tiếp tục* | Thanh trên cùng hiện tên máy và IP. Vai trò VPS xuất hiện nhưng áp dụng sẽ báo `role.vps_not_ready` (bản sau). |
| ② Mẫu cấu hình | chọn **Cấu hình mặc định** (có ★) → *Tiếp tục* | Có các preset: `dgp-current`, `vps-starlink`, `tailscale-only`, `bench-lan`, `ardupilot-default`, `custom`. |
| ③ Thiết lập | xem các tab Mạng / X25 / Đường truyền / Bảo mật / Wi-Fi và Tailscale | Ô bắt buộc có dấu `*`. Gõ sai (ví dụ IP không hợp lệ) sẽ báo lỗi ngay tại ô. |
| ④ Kiểm tra trước | tự chạy | Banner vàng "WSL: chế độ xem thử". Danh sách "Thay đổi dự kiến" liệt kê tệp, dịch vụ, gói, tường lửa. **Trong WSL sẽ có một dấu ✖ ở "Cổng LAN nối mạch bay X25"** ("Tuyến Internet mặc định đang đi qua eth0…", `eth.default_route`) vì WSL chỉ có `eth0` và nó là đường ra Internet; đó là hành vi đúng của phép kiểm tra. Mục "Mạch bay X25 được nhận qua USB" không đạt vì WSL không có X25. |
| ⑤ Áp dụng | nút bị khóa ("chỉ xem trước") | Đúng như thiết kế: trên WSL không áp dụng thật. |
| Trạng thái | nút *Trạng thái* trên cùng | Hiện trạng dịch vụ; chưa có lần áp dụng nào. |

Cuối cùng (ở bước ⑤ sau khi áp dụng) bấm **Xong** (hoặc `curl` tới `/api/shutdown`): máy chủ cài đặt dừng và cổng 8090 đóng.

Thử thêm bằng dòng lệnh:

```bash
uavlink presets
uavlink apply --preset dgp-current --preview          # bảng thay đổi + kiểm tra, không ghi gì
sudo uavlink apply --preset dgp-current --yes         # phải bị TỪ CHỐI trên WSL (mã thoát 4, env.wsl)
```

### 3.6 Nâng cấp và gỡ

```bash
# nâng cấp: chép kho có bản 0.1.1 đè lên rồi cập nhật
sudo cp -r /mnt/d/dynorch-projects/mavlink/uavlink/out/repo-upgrade/. /srv/uavlink-repo/
sudo apt update && sudo apt install --only-upgrade uavlink
# kỳ vọng: không mở lại trang cài đặt, /etc/uavlink còn nguyên, dịch vụ không đổi

# gỡ sạch
sudo apt purge uavlink uavlink-router
# kỳ vọng: /etc/uavlink và /var/lib/uavlink biến mất
```

## 4. Đường X25 thật với ArduPilot SITL

```bash
bash tests/integration/sitl.sh          # khoảng 80 giây (lần đầu thêm vài phút để build SITL)
bash tests/integration/sitl.sh --keep   # giữ SITL và hai router chạy tiếp, in cổng và lệnh dừng
```

SITL ArduCopter 4.7.1 đóng vai X25 (build một lần, ngoài repo, vào `/var/tmp/ardupilot`; cần mạng lần đầu).
`x25_configure.py` chạy thật qua cổng SERIAL0 của SITL (TCP, thay cho USB): sao lưu, ghi tham số qua hai lần khởi động
lại, nạp khóa ký, kiểm tra Ethernet. Rồi chuỗi SITL `NET_P1` (UDP client) → mavlink-routerd của "CM5" (cấu hình từ
`install-cm5.sh --print-config`) → mavlink-routerd giả làm VPS → client TCP bên ngoài (`net_probe.py --mode live`:
`vps.x25_via_vps`, `vps.cmd_via_vps` có ký). Giá trị `dgp-current` chuyển sang địa chỉ 127.x và cổng trống: không đổi
interface, route hay tường lửa. Kết quả đúng kết thúc bằng `SITL: ALL CHECKS PASSED`; không nằm trong `tools/test.sh`.

Khác X25 thật: SITL không có `NET_IPADDR*`, `NET_NETMASK` (IP do hệ điều hành đặt), engine bỏ qua chúng (`x25.ip_by_os`).

## 5. Áp dụng thật trong một container (apply-container.sh)

WSL chỉ cho xem trước. Để chạy **thật** mọi bước ghi (NetworkManager, udev, ModemManager, router, ufw có hẹn giờ
hoàn tác, SSH, unattended-upgrades, manifest, gỡ cài đặt) mà không đụng mạng của WSL, dùng một container
`systemd-nspawn` trong `uavlink-dev`, có **không gian mạng riêng**:

```bash
bash tests/integration/apply-container.sh              # dựng gói + chạy hết + xóa container (~10 phút)
bash tests/integration/apply-container.sh --skip-build # dùng các .deb đã có trong out/
bash tests/integration/apply-container.sh --keep       # giữ container để xem: bash tests/integration/container.sh ssh
```

Cần trong `uavlink-dev` (một lần): `sudo apt-get install systemd-container slirp4netns libarchive-tools`.
Container dựng từ ảnh sạch `D:\wsl\images\ubuntu-2404-pristine.tar` vào `/var/lib/machines/uavlink-cm5`.

- Mạng: `tap0` ra Internet qua slirp4netns (NAT trong không gian mạng của container), `eth0` là "cáp X25": một cặp
  veth, đầu kia nằm trong một không gian mạng khác có `192.168.50.1` và một X25 giả (`fake-x25.py`: HEARTBEAT, đọc
  tham số có ký). VPS giả là một `mavlink-routerd` trong `uavlink-dev`, container thấy ở `10.0.2.2`.
  **Không có gì được cấu hình trong không gian mạng chung của WSL**; không bao giờ dùng VPS hay X25 thật.
- Trạng thái trước: như một CM5 đã chạy `deploy.sh` cũ (mavlink-router tự build trong `/usr/bin`, kết nối
  NetworkManager `x25-link`), ufw tắt, SSH nhận mật khẩu.
- Các bước: cài từ kho apt cục bộ → `uavlink apply` qua SSH → xác nhận bằng một kết nối SSH **mới** → `uavlink verify`
  → áp dụng lần hai (không được có thay đổi nào) → áp dụng một luật tường lửa khóa SSH mà không xác nhận: phải tự
  hoàn tác → áp dụng từ console (tự xác nhận) → quét bí mật trong log → `uavlink uninstall` → `apt purge`, và so
  trạng thái hệ thống với lúc đầu (tệp trong `/etc`, dịch vụ, ufw, kết nối NetworkManager, user, nhóm).
- Kết quả đúng kết thúc bằng `APPLY-CONTAINER: ALL CHECKS PASSED`; log và sự kiện ở `out/apply-container/`.
- Container chạy nhân WSL nên engine thấy "WSL": script đặt `UAVLINK_ALLOW_WSL=1` (an toàn vì mạng của container
  là riêng). Trong container ModemManager không chạy được (`ConditionVirtualization=!container`) và không có udevd;
  bước X25 bị bỏ qua (không có X25 qua USB; đường X25 thử ở mục 4).

## 6. Giới hạn đã biết

- **Không có X25 trong WSL.** Các bước X25 bị bỏ qua (`x25.usb` = false); không đọc/ghi tham số bay được. Đường X25
  thử được với SITL (mục 4).
- **Trên WSL chỉ xem trước.** `apply` thật và nút Áp dụng bị từ chối (`env.wsl`). Muốn áp dụng thật phải chạy trên CM5
  thật hoặc trong container của mục 5 (đừng đặt `UAVLINK_ALLOW_WSL=1` trong chính distro WSL: nó sẽ đổi mạng/tường
  lửa chung của mọi distro).
- Phép kiểm tra `eth.default_route` luôn báo ✖ trong WSL (xem bước ④); tương tự Tailscale "chưa đăng nhập".
- Vai trò **VPS** có trong giao diện nhưng engine chưa làm (giai đoạn sau).
- Kho thử dùng `trusted=yes`; PPA thật sẽ có khóa ký.
- WSL tự tắt distro khi không còn cửa sổ nào mở (xem 3.1).
- Một `uavlink apply` bị từ chối trên WSL vẫn có thể để lại `/etc/uavlink/config.env` (đã ghi cấu hình trước khi
  engine từ chối). Không ảnh hưởng gì khác; `apt purge` xóa nó.

## 7. Dọn dẹp

```powershell
wsl --unregister uavlink-try      # distro thử bằng tay
wsl --unregister uavlink-it       # distro của run.sh --keep
```

Container của mục 5 (Git Bash): `bash tests/integration/container.sh destroy`; bản lưu để thử lại nhanh
(`container.sh save <tên>`) nằm ở `/var/lib/machines/uavlink-cm5.<tên>` trong `uavlink-dev`.

Rồi xóa thư mục `D:\wsl\uavlink-try` nếu còn. Thư mục `out/` (gói, kho cục bộ, ảnh chụp) không được commit; xóa tùy ý.
