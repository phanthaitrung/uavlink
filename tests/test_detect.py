import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))

from uavlink import detect  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "detect"
CM5 = FIX / "cm5"


def fake_run(cmd, timeout=detect.CMD_TIMEOUT):
    """Command output from tests/fixtures/detect/cmds/<words joined by _>.txt; unknown commands are 'not found'."""
    path = FIX / "cmds" / ("_".join(cmd) + ".txt")
    if path.is_file():
        return 0, path.read_text(encoding="utf-8")
    return 127, ""


class StubbedTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(detect, "_run", fake_run)
        p.start()
        self.addCleanup(p.stop)


class DetectTest(StubbedTest):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if not k.startswith("SSH_")}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_cm5_tree(self):
        d = detect.detect(CM5)
        self.assertFalse(d["is_wsl"])
        self.assertEqual(d["model"], "Raspberry Pi Compute Module 5 Rev 1.0")
        self.assertEqual(d["hostname"], "cm5")
        self.assertEqual(d["x25_ports"], ["/dev/serial/by-id/usb-ArduPilot_CUAV-X25-EVO_0001-if00"])
        self.assertEqual(d["default_route_iface"], "wlan0")
        self.assertEqual(d["tailscale"], {"ip": "100.88.1.2", "up": True})
        self.assertEqual(d["wifi"], {"iface": "wlan0", "ssid": "Home: 5G"})
        self.assertEqual(d["router"], {"conf_path": "/etc/mavlink-router/main.conf", "exists": True,
                                       "service": {"uavlink-router": "active", "mavlink-router": "inactive"}})
        self.assertEqual(d["desktop_user"], "pi")
        self.assertIsNone(d["ssh_session"])
        json.dumps(d)                                        # serialisable

    def test_ifaces(self):
        by_name = {i["name"]: i for i in detect.detect(CM5)["ifaces"]}
        self.assertEqual(sorted(by_name), ["eth0", "tailscale0", "wlan0"])        # lo is left out
        eth = by_name["eth0"]
        self.assertEqual((eth["mac"], eth["carrier"], eth["speed"]), ("2c:cf:67:aa:bb:cc", True, 1000))
        self.assertEqual(eth["ipv4"], ["192.168.50.2/24", "192.168.144.2/24"])
        self.assertFalse(eth["is_default_route"] or eth["wireless"])
        self.assertTrue(by_name["wlan0"]["is_default_route"] and by_name["wlan0"]["wireless"])
        self.assertIsNone(by_name["tailscale0"]["carrier"])

    def test_wsl(self):
        self.assertTrue(detect.is_wsl(FIX / "wsl"))
        self.assertFalse(detect.is_wsl(CM5))
        self.assertEqual(detect.role_guess(FIX / "wsl"), "vps")
        self.assertEqual(detect.detect(FIX / "wsl")["x25_ports"], [])

    def test_role_guess(self):
        self.assertEqual(detect.role_guess(CM5), "cm5")
        self.assertEqual(detect.role_guess(FIX / "does-not-exist"), "vps")

    def test_ssh_session_from_env(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "10.0.0.5 5000 10.0.0.9 22"}):
            self.assertEqual(detect.detect(CM5)["ssh_session"], {"client": "10.0.0.5"})

    def test_probes_never_raise(self):
        def broken(cmd, timeout=0):
            raise RuntimeError("boom")
        with mock.patch.object(detect, "_run", broken):
            d = detect.detect(FIX / "does-not-exist")
        self.assertEqual(d["ifaces"], [])
        self.assertEqual(d["tailscale"], {"ip": None, "up": False})
        self.assertFalse(d["router"]["exists"])
        self.assertIsNone(d["desktop_user"])
        self.assertIsNone(detect.running_preset(FIX / "does-not-exist"))

    def test_split_nmcli(self):
        self.assertEqual(detect._split_nmcli(r"yes:My\:Net\\x:wlan0"), ["yes", "My:Net\\x", "wlan0"])


class RealRunTest(unittest.TestCase):
    def test_missing_command_and_timeout(self):
        self.assertEqual(detect._run(["uavlink-no-such-command"]), (127, ""))
        self.assertEqual(detect._run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2), (124, ""))
        self.assertEqual(detect._run([sys.executable, "-c", "print('hi')"]), (0, "hi\n"))

    def test_detect_on_this_machine(self):
        d = detect.detect()
        self.assertIsInstance(d["is_wsl"], bool)
        self.assertIsInstance(d["ifaces"], list)
        self.assertIsInstance(d["x25_ports"], list)


class RunningPresetTest(StubbedTest):
    EXPECTED = {
        "CM5_TCP_SERVER_PORT": "5760",
        "ROUTER_REPORT_STATS": "false",
        "CM5_ROUTER_LOG_DIR": "/var/log/mavlink-router",
        "CM5_ROUTER_LOG_MODE": "while-armed",
        "MAVLINK_LAN_PORT": "14550",
        "VPS_PUBLIC_IP": "209.97.167.92",
        "VPS_UPLINK_PORT": "14555",
        "CM5_EXTRA_UDP_TARGETS": "ground_station_office=100.70.184.3:14550",
        "CM5_ETH_CON_NAME": "x25-link",
        "CM5_ETH_IFACE": "eth0",
        "CM5_LAN_IP": "192.168.50.2",
        "X25_NETMASK": "24",
        "CM5_ETH_EXTRA_ADDRS": "192.168.144.2/24",
    }

    def test_dgp_network_manager(self):
        preset = detect.running_preset(CM5)
        self.assertEqual(preset["id"], "running")
        self.assertEqual(preset["role"], "cm5")
        for key in ("name", "desc"):
            self.assertEqual(sorted(preset[key]), ["en", "vi"])
            self.assertTrue(all(preset[key].values()))
        self.assertEqual(preset["values"], self.EXPECTED)

    def test_netplan(self):
        preset = detect.running_preset(FIX / "cm5-netplan")
        self.assertEqual(preset["values"], {"CM5_ETH_IFACE": "eth0", "CM5_LAN_IP": "192.168.50.2",
                                            "X25_NETMASK": "24", "CM5_ETH_EXTRA_ADDRS": "192.168.144.2/24"})

    def test_parse_router_conf(self):
        sections = detect.parse_router_conf((CM5 / "etc/mavlink-router/main.conf").read_text(encoding="utf-8"))
        self.assertEqual(list(sections), ["General", "UdpEndpoint x25_ethernet", "UdpEndpoint vps_mavlink",
                                          "UdpEndpoint ground_station_office"])
        self.assertEqual(sections["UdpEndpoint x25_ethernet"], {"Mode": "Server", "Address": "0.0.0.0", "Port": "14550"})

    def test_router_conf_without_vps_or_logging(self):
        values = detect._router_values("[General]\nTcpServerPort = 5761\n\n[UdpEndpoint x25_ethernet]\n"
                                       "Mode = Server\nAddress = 0.0.0.0\nPort = 14551\n")
        self.assertEqual(values, {"CM5_TCP_SERVER_PORT": "5761", "CM5_ROUTER_LOG_DIR": "",
                                  "MAVLINK_LAN_PORT": "14551", "VPS_PUBLIC_IP": "", "CM5_EXTRA_UDP_TARGETS": ""})


if __name__ == "__main__":
    unittest.main()
