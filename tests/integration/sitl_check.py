#!/usr/bin/env python3
"""Helpers of tests/integration/sitl.sh (not a unit test).

  sitl_check.py ports                     free local ports and a free SITL instance, as shell assignments
  sitl_check.py params URL NAME=VALUE...  read parameters unsigned over URL (SITL SERIAL0 = the "USB" link), compare
  sitl_check.py events FILE               one line per @@UAVLINK event: "step ID STATE [CODE]", "check ID ok|fail|warn
                                          [CODE]", "info CODE", "change TARGET OLD NEW", "result ok|fail"
"""
import json
import os
import socket
import sys
import time

os.environ.setdefault("MAVLINK20", "1")
os.environ.setdefault("MAVLINK_DIALECT", "ardupilotmega")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "vendor"))


def _free(kind, port=0):
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        return None
    return s


def ports():
    held, out = [], {}
    for name, kind in (("USB_PORT", socket.SOCK_STREAM), ("CM5_TCP", socket.SOCK_STREAM),
                       ("VPS_TCP", socket.SOCK_STREAM), ("LAN_PORT", socket.SOCK_DGRAM),
                       ("UPLINK_PORT", socket.SOCK_DGRAM), ("GCS_UDP_PORT", socket.SOCK_DGRAM)):
        s = _free(kind)
        held.append(s)
        out[name] = s.getsockname()[1]
    # SITL -I N moves its other ports by 10*N: SERIAL1/2 on TCP 5762/5763, RC input on UDP 5501.
    for n in range(10, 100):
        socks = [_free(socket.SOCK_STREAM, 5762 + 10 * n), _free(socket.SOCK_STREAM, 5763 + 10 * n),
                 _free(socket.SOCK_DGRAM, 5501 + 10 * n)]
        held += socks
        if all(socks) and not {5762 + 10 * n, 5763 + 10 * n, 5501 + 10 * n} & set(out.values()):
            out["SITL_INSTANCE"] = n
            break
    for s in held:
        if s:
            s.close()
    if "SITL_INSTANCE" not in out:
        print("no free SITL instance", file=sys.stderr)
        return 1
    for k, v in out.items():
        print(f"{k}={v}")
    return 0


def params(url, wanted):
    from pymavlink import mavutil
    conn = mavutil.mavlink_connection(url, source_system=254, source_component=191)
    hb = conn.wait_heartbeat(timeout=15)
    if hb is None:
        print(f"no HEARTBEAT on {url}")
        return 1
    bad = 0
    for item in wanted:
        name, _, value = item.partition("=")
        got = None
        for _ in range(3):
            conn.mav.param_request_read_send(conn.target_system, conn.target_component, name.encode(), -1)
            end = time.time() + 2
            while got is None and time.time() < end:
                m = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=0.5)
                if m is not None and m.param_id.rstrip("\x00") == name:
                    got = m.param_value
            if got is not None:
                break
        ok = got is not None and abs(got - float(value)) < 1e-4
        bad += not ok
        print(f"{name} = {'missing' if got is None else f'{got:g}'}" + ("" if ok else f"   (want {value})"))
    conn.close()
    return 1 if bad else 0


def events(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("@@UAVLINK "):
                continue
            e = json.loads(line[len("@@UAVLINK "):])
            t, code = e["t"], e.get("code", "")
            if t == "step":
                parts = ["step", e["id"], e["state"], code]
            elif t == "check":
                state = "ok" if e["ok"] else "warn" if e.get("severity") == "warn" else "fail"
                parts = ["check", e["id"], state, code]
            elif t == "info":
                parts = ["info", code]
            elif t == "change":
                parts = ["change", e["target"], e.get("old", ""), e.get("new", "")]
            elif t == "result":
                parts = ["result", "ok" if e["ok"] else "fail"]
            else:
                parts = [t]
            print(" ".join(p for p in parts if p != ""))
    return 0


def main(argv):
    if argv[:1] == ["ports"]:
        return ports()
    if len(argv) >= 3 and argv[0] == "params":
        return params(argv[1], argv[2:])
    if len(argv) == 2 and argv[0] == "events":
        return events(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
