# Phát hành uavlink lên PPA (hướng dẫn cho chủ dự án)

Mục tiêu: người dùng chỉ cần chạy

```bash
sudo add-apt-repository ppa:uavlink/stable
sudo apt update
sudo apt install uavlink
```

Gói được Launchpad build từ **source package** (không upload file `.deb`). Mỗi bản phát hành gồm hai source package
(`uavlink` và `uavlink-router`) cho từng bản Ubuntu (series). Việc build và ký do GitHub Actions làm khi bạn đẩy tag `v*`,
hoặc bạn tự chạy `tools/ppa-upload.sh` trong WSL.

Các bước 1–5 chỉ làm **một lần**.

## 1. Tạo tài khoản Launchpad

1. Vào <https://launchpad.net> → **Log in / Register**, tạo tài khoản (cần email thật).
2. Vào trang cá nhân → **Code of Conduct** → ký **Ubuntu Code of Conduct** (bắt buộc trước khi upload lên PPA).
   Bước này dùng khoá GPG ở mục 3, nên có thể làm sau mục 3.

## 2. Tạo team `uavlink` và PPA `stable`

1. <https://launchpad.net/people/+newteam> → Name: `uavlink`, Display name: `uavlink`, loại **Open** hoặc **Restricted**
   (nên chọn Restricted; bạn là chủ team).
2. Vào trang team → **Create a new PPA** (<https://launchpad.net/~uavlink/+activate-ppa>):
   - URL name: `stable` → địa chỉ cuối cùng là `ppa:uavlink/stable`.
   - Display name: `uavlink stable`. Mô tả: một dòng về dự án.
3. Vào PPA → **Change details** → **Processors**: tick **ARM (arm64)** (bắt buộc: Raspberry Pi CM5 là arm64) và giữ
   **AMD64**. Lưu lại.
   - Nếu không thấy arm64 trong danh sách thì đó là PPA cũ: tạo lại PPA hoặc nhờ Launchpad bật arm64.
4. Trong **Change details** có thể tick **Publish this archive** (mặc định bật).

## 3. Tạo khoá GPG và đưa lên Launchpad

Làm trong WSL (`wsl -d uavlink-dev`) hoặc máy Linux của bạn. Tên và email phải **trùng** dòng `Maintainer` trong
`debian/changelog` (hiện là `tomphan <phanthaitrung@gmail.com>`) và trùng email tài khoản Launchpad.

```bash
gpg --full-generate-key         # RSA 4096 (hoặc ed25519), không hết hạn hoặc 2–3 năm, đặt passphrase mạnh
gpg --list-secret-keys --keyid-format long
#   sec   rsa4096/ABCD1234EFGH5678 ...      <- ABCD1234EFGH5678 là key id
#         <FINGERPRINT 40 ký tự>
```

Lấy dấu vân tay (fingerprint) đầy đủ rồi đưa khoá công khai lên Ubuntu keyserver:

```bash
gpg --fingerprint ABCD1234EFGH5678
gpg --keyserver keyserver.ubuntu.com --send-keys <FINGERPRINT>
```

Trên Launchpad: trang cá nhân → **OpenPGP keys** → dán fingerprint → **Import Key**. Launchpad gửi một email mã hoá;
mở email, giải mã (`gpg --decrypt`) và bấm đường link xác nhận trong đó.

Sau đó quay lại mục 1 để ký Code of Conduct: tải file trên trang Code of Conduct, ký bằng `gpg --clearsign`, dán kết quả vào trang đó.

Sao lưu khoá bí mật ở nơi an toàn (không đưa vào git):

```bash
gpg --armor --export-secret-keys <FINGERPRINT> > uavlink-ppa-private.asc    # cất file này, rồi xoá khỏi máy làm việc
```

## 4. Thêm secret vào GitHub

Repo phải ở trên GitHub (`github.com/phanthaitrung/uavlink`). Vào **Settings → Secrets and variables → Actions →
New repository secret**:

| Tên secret | Nội dung |
|---|---|
| `PPA_GPG_PRIVATE_KEY` | toàn bộ nội dung file `uavlink-ppa-private.asc` (gồm dòng `-----BEGIN PGP PRIVATE KEY BLOCK-----`) |
| `PPA_GPG_PASSPHRASE` | passphrase của khoá |

Workflow không bao giờ in các giá trị này ra log. Đừng dán chúng vào issue, commit hay chat.

## 5. Giấy phép (LICENSE)

Mã của uavlink dùng giấy phép **MIT** (file `LICENSE` ở thư mục gốc, `License: Expat` trong `debian/copyright`).
Phần đóng kèm giữ giấy phép riêng: `pymavlink` trong `vendor/` (LGPL-3+), `qrcodegen.js` (MIT), `mavlink-router` trong
`uavlink-router` (xem `packaging/uavlink-router/debian/copyright`). Thêm thư viện mới nào thì ghi thêm vào `debian/copyright`.

## 6. Phát hành một phiên bản

1. Sửa `debian/changelog`: dòng đầu là phiên bản mới (ví dụ `uavlink (0.2.0) noble; urgency=medium`, kèm mô tả thay đổi).
   Tên series (`noble`) ở đây chỉ là mặc định; script tự đổi thành `0.2.0~noble1` và đúng series khi build.
   Với `uavlink-router` chỉ sửa `packaging/uavlink-router/debian/changelog` khi router thay đổi.
2. Commit, push lên `main`, chờ CI (tab **Actions**) xanh trên cả `ubuntu-24.04` và `ubuntu-24.04-arm`.
3. Tạo tag **trùng đúng phiên bản trong changelog** và đẩy lên:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

   Workflow **Release to PPA** build và ký source package cho từng series trong `DEFAULT_SERIES`
   (file `.github/workflows/release.yml`, mặc định `noble`) rồi `dput` lên `ppa:uavlink/stable`. Nếu tag không khớp
   changelog, workflow dừng trước khi upload.
4. **Chạy thử không upload:** tab **Actions → Release to PPA → Run workflow**, để ô *dry_run* được tick. Workflow build và ký
   nhưng không upload; file nằm trong artifact `ppa-source-packages`.
5. Thêm series mới: sửa `DEFAULT_SERIES` (ví dụ `noble plucky`). Mỗi series được upload riêng với phiên bản `0.2.0~noble1`,
   `0.2.0~plucky1`. Nếu phải upload lại (Launchpad từ chối cùng tên phiên bản với nội dung khác), tăng phiên bản trong changelog.

Lần upload đầu tiên của `uavlink-router` luôn kèm file `.orig.tar.xz` (script dùng `-sa`, nên lần nào cũng kèm; Launchpad bỏ
qua nếu file giống hệt đã có).

### Chạy tay trong WSL (không qua GitHub)

```bash
sudo apt install devscripts debhelper dput gnupg xz-utils
bash tools/ppa-upload.sh                      # --dry-run là mặc định: build, in lệnh dput, không upload
bash tools/ppa-upload.sh --throwaway-key      # thử luôn bước ký bằng khoá tạm (Launchpad sẽ không nhận)
PPA_GPG_KEY_ID=<FINGERPRINT> PPA_GPG_PASSPHRASE='…' bash tools/ppa-upload.sh --upload
```

Kết quả nằm trong `out/ppa/<series>/`. Từ Git Bash trên Windows, lệnh tự chạy trong distro `uavlink-dev`.

## 7. Kiểm tra bản build trên Launchpad

1. Mở <https://launchpad.net/~uavlink/+archive/ubuntu/stable>. Mục **Packages** hiện `Pending` rồi `Published`.
   Trong vài phút bạn cũng nhận email *Accepted* (hoặc *Rejected*, kèm lý do).
2. Bấm **View package details** → tab **Builds**: mỗi gói build cho `amd64` và `arm64` (`uavlink` kiểu `all` build một lần).
   Build `arm64` thường chậm hơn; đợi đến khi **Successfully built**. Xem log nếu lỗi.
3. Chỉ khi cả hai kiến trúc xong và trạng thái là **Published** thì người dùng mới cài được.

Lỗi hay gặp:

- *Rejected: Signature verification failed / not signed by a registered key*: khoá chưa xác nhận trên Launchpad, hoặc email trong
  `Maintainer` không khớp khoá.
- *Version older than the one already in the archive*: phiên bản phải luôn tăng; đừng tạo tag cũ.
- *Source/binary file already exists with different contents*: đã upload cùng phiên bản trước đó; tăng phiên bản.

## 8. Người dùng cài đặt

Trên Ubuntu 24.04 (amd64 hoặc Raspberry Pi arm64):

```bash
sudo add-apt-repository ppa:uavlink/stable
sudo apt update
sudo apt install uavlink
sudo uavlink setup
```

Cài gói không thay đổi cấu hình hệ thống; `sudo uavlink setup` mở trang cài đặt hai ngôn ngữ (Việt/Anh).
Cập nhật về sau: `sudo apt update && sudo apt upgrade`.
