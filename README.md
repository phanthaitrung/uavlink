# uavlink

Gói apt cấu hình Raspberry Pi CM5 + CUAV X25 EVO (ArduCopter) truyền MAVLink qua LAN, Tailscale và VPS relay, có trang
cấu hình song ngữ Việt/Anh. *Apt package that configures a Raspberry Pi CM5 + CUAV X25 EVO companion link, with a
bilingual (Vietnamese/English) setup page.*

## Cài đặt / Install

Ubuntu 24.04 (Raspberry Pi CM5 arm64 hoặc amd64):

```bash
sudo add-apt-repository ppa:uavlink/stable
sudo apt update
sudo apt install uavlink
sudo uavlink setup
```

Cài gói không thay đổi cấu hình hệ thống; `sudo uavlink setup` mở trang cấu hình song ngữ. Gói phát hành tại
<https://launchpad.net/~uavlink/+archive/ubuntu/stable>. Cách phát hành: [docs/RELEASING.md](docs/RELEASING.md).

Kiến trúc và quy ước: [ARCHITECTURE.md](ARCHITECTURE.md).

Giấy phép / License: [MIT](LICENSE). Thư viện đóng kèm giữ giấy phép riêng (xem [LICENSE](LICENSE)).
