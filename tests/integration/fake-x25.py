#!/usr/bin/env python3
"""A fake X25 flight controller for the container sandbox (apply-container.sh), at the far end of the container's
X25 cable (run it in that network namespace: ip netns exec <X25_NETNS> …).

  fake-x25.py <host:port>     (passphrase from FAKE_X25_PASSPHRASE; empty = no signing)

Sends vehicle heartbeats (sysid 1, ArduCopter, disarmed) at 10 Hz to <host:port> (the CM5 address and
MAVLINK_LAN_PORT, as NET_P1 of the X25 does) and answers PARAM_REQUEST_READ of the NET_* values in PARAMS. With a
passphrase it signs what it sends and ignores unsigned PARAM_REQUEST_READ, like an X25 that enforces MAVLink2 signing.
Prints a counter line every 10 s. Runs until killed. Never point it at a real vehicle or VPS.
"""
import hashlib
import os
import socket
import sys
import time

os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "vendor"))
from pymavlink import mavutil  # noqa: E402

MAV = mavutil.mavlink
SIGNING_EPOCH = 1420070400
# What verify-cm5.sh (NET_IPADDR3) and net_probe.py (NET_ENABLE) read, as on the X25 with the dgp-current values.
PARAMS = {"NET_ENABLE": 1.0, "NET_IPADDR3": 1.0, "NET_NETMASK": 24.0, "NET_OPTIONS": 0.0, "NET_P1_PORT": 14550.0}


class _Out:
    def __init__(self, send):
        self.write = send


def main():
    if len(sys.argv) != 2 or ":" not in sys.argv[1]:
        print(__doc__, file=sys.stderr)
        return 2
    host, port = sys.argv[1].rsplit(":", 1)
    target = (host, int(port))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.05)

    def send(data):
        try:
            sock.sendto(data, target)
        except OSError:
            pass

    mav = MAV.MAVLink(_Out(send), srcSystem=1, srcComponent=1)
    mav.robust_parsing = True
    passphrase = os.environ.get("FAKE_X25_PASSPHRASE", "")
    rejected = [0]
    if passphrase:
        s = mav.signing
        s.secret_key = hashlib.sha256(passphrase.encode()).digest()
        s.sign_outgoing = True
        s.timestamp = int((time.time() - SIGNING_EPOCH) * 100000)

        def allow_unsigned(_mav, msg_id):
            if msg_id == MAV.MAVLINK_MSG_ID_PARAM_REQUEST_READ:
                rejected[0] += 1
                return False
            return True
        s.allow_unsigned_callback = allow_unsigned

    sent = answered = received = 0
    next_hb = next_report = 0.0
    print(f"fake X25 -> {host}:{port}, signing {'on' if passphrase else 'off'}", flush=True)
    while True:
        now = time.time()
        if now >= next_hb:
            mav.heartbeat_send(MAV.MAV_TYPE_QUADROTOR, MAV.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0, MAV.MAV_STATE_STANDBY)
            sent += 1
            next_hb = now + 0.1
        if now >= next_report:
            print(f"heartbeats={sent} received={received} answered={answered} rejected_unsigned={rejected[0]}",
                  flush=True)
            next_report = now + 10
        try:
            data = sock.recv(65535)
        except OSError:
            continue
        for m in mav.parse_buffer(data) or []:
            if m.get_type() == "BAD_DATA":
                continue
            received += 1
            if m.get_type() == "PARAM_REQUEST_READ" and m.target_system == 1:
                name = m.param_id.decode("ascii", "ignore") if isinstance(m.param_id, bytes) else m.param_id
                name = name.rstrip("\x00")
                if name in PARAMS:
                    mav.param_value_send(name.encode(), PARAMS[name], MAV.MAV_PARAM_TYPE_REAL32, len(PARAMS),
                                         list(PARAMS).index(name))
                    answered += 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
