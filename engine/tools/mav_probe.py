#!/usr/bin/env python3
"""
Kiểm tra nhanh một đường MAVLink: chờ HEARTBEAT của mạch bay, tùy chọn đọc một tham số
(để chắc chắn lệnh đi được cả chiều về mạch bay). Thoát 0 nếu đạt.
Có biến môi trường MAVLINK_SIGNING_PASSPHRASE thì lệnh gửi đi được ký MAVLink2.

  mav_probe.py tcp:127.0.0.1:5760 --param NET_IPADDR3
  mav_probe.py tcp:127.0.0.1:5760 --param NET_IPADDR3 --check-signing   # lệnh không ký phải bị từ chối
  mav_probe.py udpout:mav.dgp.vn:14550
"""
import argparse
import hashlib
import os
import sys
import time

# Dùng MAVLink2 ngay từ đầu. Mặc định pymavlink bắt đầu bằng MAVLink1 rồi tự chuyển khi nhận gói
# MAVLink2 đầu tiên, và lúc chuyển thì mất cấu hình ký đã đặt.
os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")
try:
    from pymavlink import mavutil  # noqa: E402
except ImportError:  # người gọi chưa đặt PYTHONPATH: dùng bản đóng gói kèm <engine>/../vendor
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "vendor"))
    from pymavlink import mavutil  # noqa: E402

MAV = mavutil.mavlink


def param_name(msg):
    pid = msg.param_id
    if isinstance(pid, bytes):
        pid = pid.decode("ascii", "ignore")
    return pid.rstrip("\x00")


def read_param(conn, sysid, compid, name, tries=3):
    for _ in range(tries):
        conn.mav.param_request_read_send(sysid, compid, name.encode("ascii"), -1)
        end = time.time() + 3
        while time.time() < end:
            pv = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
            if pv is not None and param_name(pv) == name:
                return pv.param_value
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="chuỗi kết nối pymavlink, vd tcp:127.0.0.1:5760")
    ap.add_argument("--param", help="tên tham số cần đọc thử")
    ap.add_argument("--check-signing", action="store_true",
                    help="đọc --param bằng lệnh có ký (phải được) rồi không ký (phải bị từ chối)")
    ap.add_argument("--unsigned", action="store_true", help="không ký lệnh dù có passphrase")
    ap.add_argument("--timeout", type=float, default=15)
    ap.add_argument("--source-system", type=int, default=254)
    a = ap.parse_args()

    passphrase = "" if a.unsigned else os.environ.get("MAVLINK_SIGNING_PASSPHRASE", "")
    if a.check_signing and (not passphrase or not a.param):
        print("--check-signing cần --param và MAVLINK_SIGNING_PASSPHRASE")
        return 1

    try:
        conn = mavutil.mavlink_connection(a.url, source_system=a.source_system, source_component=191)
    except Exception as e:
        print(f"Không mở được {a.url}: {e}")
        return 1
    if passphrase:
        conn.setup_signing(hashlib.sha256(passphrase.encode("utf-8")).digest(), sign_outgoing=True,
                           allow_unsigned_callback=lambda mav, msg_id: True)

    # Với udpout phải gửi trước thì relay mới biết địa chỉ để trả về
    if a.url.startswith("udpout:"):
        conn.mav.heartbeat_send(MAV.MAV_TYPE_GCS, MAV.MAV_AUTOPILOT_INVALID, 0, 0, 0)

    hb = None
    end = time.time() + a.timeout
    while time.time() < end:
        msg = conn.recv_match(type="HEARTBEAT", blocking=True, timeout=1)
        if msg is not None and msg.autopilot != MAV.MAV_AUTOPILOT_INVALID:
            hb = msg
            break
        if a.url.startswith("udpout:"):
            conn.mav.heartbeat_send(MAV.MAV_TYPE_GCS, MAV.MAV_AUTOPILOT_INVALID, 0, 0, 0)
    if hb is None:
        print(f"Không nhận được HEARTBEAT của mạch bay qua {a.url} trong {a.timeout:.0f}s")
        return 1

    sysid, compid = hb.get_srcSystem(), hb.get_srcComponent()
    armed = bool(hb.base_mode & MAV.MAV_MODE_FLAG_SAFETY_ARMED)
    print(f"HEARTBEAT sysid={sysid} compid={compid} mode={mavutil.mode_string_v10(hb)} "
          f"{'ARMED' if armed else 'disarmed'}")
    if not a.param:
        return 0

    signed = " (lệnh có ký)" if passphrase else ""
    value = read_param(conn, sysid, compid, a.param)
    if value is None:
        print(f"Không đọc được tham số {a.param}{signed}: chiều lệnh về mạch bay không thông, hoặc sai khóa ký")
        return 1
    print(f"PARAM {a.param} = {value:g}{signed}")

    if a.check_signing:
        conn.mav.signing.sign_outgoing = False
        if read_param(conn, sysid, compid, a.param, tries=2) is not None:
            print("Mạch bay vẫn nhận lệnh KHÔNG ký: ký MAVLink2 chưa bật")
            return 1
        print("Lệnh không ký bị từ chối: ký MAVLink2 đang được bắt buộc")
    return 0


if __name__ == "__main__":
    sys.exit(main())
