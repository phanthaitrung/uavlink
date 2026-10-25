import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "engine" / "tools" / "verify-cm5.sh"
I18N = ROOT / "share" / "i18n" / "engine-verify.json"

CONFIG = """CM5_ETH_IFACE=eth0
CM5_LAN_IP=192.168.144.20
X25_IP=192.168.144.14
MAVLINK_LAN_PORT=14550
CM5_TCP_SERVER_PORT=5760
VPS_PUBLIC_IP=203.0.113.9
VPS_UPLINK_PORT=14551
MAVLINK_SIGNING=1
SSH_HARDEN=1
CM5_ENABLE_UFW=1
SERVICE_SANDBOX=1
"""

SHIMS = {
    "id": "echo 0",
    "ip": 'case "$*" in *route*) echo "default via 10.0.0.1 dev wlan0";; *"addr show"*) '
          'echo "2: eth0    inet 192.168.144.20/24 scope global eth0";; esac',
    "ping": 'case "$*" in *192.168.144.14*) case "$SHIM_FAIL" in *ping*) exit 1;; esac;; '
            '*1.1.1.1*) case "$SHIM_FAIL" in *icmp*) exit 1;; esac;; esac',
    "systemctl": 'case "$1" in is-active) exit 0;; show) echo 123;; esac',
    "ps": "echo mavlink-router",
    "python3": 'echo "NET_IPADDR3=14"',
    "tcpdump": 'echo "12:00:00 IP a > b: UDP"',
    "sshd": 'case "$SHIM_FAIL" in *ssh*) echo "passwordauthentication yes";; *) echo "passwordauthentication no";; esac',
    "ufw": 'echo "Status: active"',
}


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        (tmp / "config.env").write_text(CONFIG, newline="\n")
        net = tmp / "net" / "eth0"
        net.mkdir(parents=True)
        (net / "carrier").write_text("1\n")
        shims = tmp / "shims"
        shims.mkdir()
        for name, body in SHIMS.items():
            sh = shims / name
            sh.write_text(f"#!/bin/sh\n{body}\n", newline="\n")
            sh.chmod(0o755)
        self.tmp, self.shims = tmp, shims

    def run_script(self, fail="", **extra):
        env = dict(os.environ, **extra, PATH=f"{self.shims}:{os.environ['PATH']}", UAVLINK_EVENTS="1", SHIM_FAIL=fail,
                   CONFIG_FILE=str(self.tmp / "config.env"), UAVLINK_CONFIG_DIR=str(self.tmp),
                   UAVLINK_SYS_NET=str(self.tmp / "net"))
        p = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        evs = [json.loads(line[len("@@UAVLINK "):]) for line in p.stdout.splitlines() if line.startswith("@@UAVLINK ")]
        return p, evs

    def test_all_pass(self):
        p, evs = self.run_script()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        checks = {e["id"]: e for e in evs if e["t"] == "check"}
        self.assertEqual(set(checks), {"lan.link", "lan.addr", "lan.route", "x25.ping", "router.active", "x25.udp_in",
                                       "mavlink.two_way", "internet", "vps.uplink", "signing.enforced",
                                       "ssh.no_password", "ufw.active", "router.not_root"})
        self.assertTrue(all(c["ok"] for c in checks.values()))
        self.assertEqual(checks["lan.addr"]["args"], {"iface": "eth0", "ip": "192.168.144.20"})
        self.assertEqual(evs[-1], {"t": "result", "ok": True, "data": {"passed": 13, "failed": 0}})

    def test_failed_checks_exit_3(self):
        p, evs = self.run_script(fail="ping ssh")
        self.assertEqual(p.returncode, 3)
        checks = {e["id"]: e for e in evs if e["t"] == "check"}
        self.assertFalse(checks["x25.ping"]["ok"])
        self.assertEqual(checks["x25.ping"]["code"], "x25.ping.fail")
        self.assertFalse(checks["ssh.no_password"]["ok"])
        self.assertEqual(evs[-1], {"t": "result", "ok": False, "data": {"passed": 11, "failed": 2}})

    def test_i18n_covers_codes(self):
        _, bad = self.run_script(fail="ping")
        _, good = self.run_script()
        codes = {e["code"] for e in bad + good if e["t"] == "check"}
        catalog = json.loads(I18N.read_text(encoding="utf-8"))
        self.assertLessEqual(codes, set(catalog))
        for code, text in catalog.items():
            self.assertEqual(set(text), {"vi", "en"}, code)
            self.assertTrue(text["vi"] and text["en"], code)

    def test_long_output_after_the_match_is_not_a_failure(self):
        # `sshd -T | grep -q` under pipefail: grep stops at the match, sshd dies of SIGPIPE writing the rest and the
        # check failed although password login was off (seen on a real apply, sshd -T prints over 4 KB).
        (self.shims / "sshd").write_text('#!/bin/bash\necho "passwordauthentication no"\n'
                                         'for i in $(seq 1 20000); do echo "option$i value"; done\n', newline="\n")
        (self.shims / "ufw").write_text('#!/bin/bash\necho "Status: active"\n'
                                        'for i in $(seq 1 20000); do echo "rule $i ALLOW IN Anywhere"; done\n',
                                        newline="\n")
        p, evs = self.run_script()
        self.assertEqual(p.returncode, 0, p.stdout)
        checks = {e["id"]: e["ok"] for e in evs if e["t"] == "check"}
        self.assertTrue(checks["ssh.no_password"])
        self.assertTrue(checks["ufw.active"])

    def test_internet_without_icmp_falls_back_to_tcp(self):
        # ICMP blocked (a sandbox NAT, some mobile networks): a TCP connection still proves the Internet.
        with socket.socket() as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(4)
            port = srv.getsockname()[1]
            _, evs = self.run_script(fail="icmp", UAVLINK_INTERNET_HOSTS=f"127.0.0.1:{port}")
        self.assertTrue(next(e for e in evs if e.get("id") == "internet")["ok"])
        with socket.socket() as closed:
            closed.bind(("127.0.0.1", 0))
            port = closed.getsockname()[1]
        p, evs = self.run_script(fail="icmp", UAVLINK_INTERNET_HOSTS=f"127.0.0.1:{port}")
        self.assertFalse(next(e for e in evs if e.get("id") == "internet")["ok"])
        self.assertEqual(p.returncode, 3)

    def test_probe_failure_is_a_failed_check(self):
        (self.shims / "python3").write_text("#!/bin/sh\nexit 1\n", newline="\n")
        p, evs = self.run_script()
        self.assertEqual(p.returncode, 3)
        self.assertFalse(next(e for e in evs if e.get("id") == "mavlink.two_way")["ok"])


if __name__ == "__main__":
    unittest.main()
