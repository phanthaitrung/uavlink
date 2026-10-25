import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))

from uavlink import launch  # noqa: E402

IP_ADDR = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 192.168.50.2/24 brd 192.168.50.255 scope global eth0\\       valid_lft forever
3: tailscale0    inet 100.101.102.103/32 scope global tailscale0\\       valid_lft forever
4: eth0    inet 192.168.50.2/24 scope global secondary eth0\\       valid_lft forever
"""

SHOW_LOCAL = "Type=wayland\nActive=yes\nRemote=no\nName=pi\nUser=1000\nDisplay=\n"
SHOW_SSH = "Type=tty\nActive=yes\nRemote=yes\nName=pi\nUser=1000\nDisplay=\n"
SHOW_INACTIVE = "Type=x11\nActive=no\nRemote=no\nName=bob\nUser=1001\nDisplay=:1\n"


class UrlTest(unittest.TestCase):
    def test_loopback_only(self):
        lines = launch.build_urls(8090, "TOK", False)
        self.assertIn("  http://127.0.0.1:8090/?t=TOK", lines)
        self.assertFalse(any("ssh -L" in line for line in lines))
        self.assertTrue(any("sudo uavlink setup" in line for line in lines))

    def test_ssh_hint(self):
        lines = launch.build_urls(8091, "T", False, ssh_connection="10.0.0.5 5555 192.168.1.9 22", user="pi")
        self.assertIn("  ssh -L 8091:127.0.0.1:8091 pi@192.168.1.9", lines)

    def test_lan_lines_and_tailscale(self):
        ips = launch.parse_ip_addr(IP_ADDR)
        self.assertEqual(ips, ["127.0.0.1", "192.168.50.2", "100.101.102.103"])
        lines = launch.build_urls(8090, "T", True, ips=ips, ssh_connection="1.1.1.1 1 2.2.2.2 22")
        self.assertIn("  http://192.168.50.2:8090/?t=T", lines)
        self.assertIn("  http://100.101.102.103:8090/?t=T   (Tailscale)", lines)
        self.assertFalse(any("ssh -L" in line for line in lines))
        self.assertEqual(sum("127.0.0.1" in line for line in lines), 1)

    def test_tailscale_range(self):
        self.assertTrue(launch.is_tailscale("100.64.0.1"))
        self.assertTrue(launch.is_tailscale("100.127.255.254"))
        self.assertFalse(launch.is_tailscale("100.128.0.1"))
        self.assertFalse(launch.is_tailscale("not-an-ip"))


class LoginctlTest(unittest.TestCase):
    def test_parse_session_list(self):
        text = "      1 1000 pi   seat0 tty1\n     c2 0    root -     -\n"
        self.assertEqual(launch.parse_session_list(text), ["1", "c2"])

    def test_pick_graphical(self):
        props = launch.pick_graphical_session([SHOW_SSH, SHOW_INACTIVE, SHOW_LOCAL])
        self.assertEqual(props["Name"], "pi")
        self.assertEqual(props["User"], "1000")

    def test_none_graphical(self):
        self.assertIsNone(launch.pick_graphical_session([SHOW_SSH, SHOW_INACTIVE]))
        self.assertIsNone(launch.pick_graphical_session([]))


class ServeEnvTest(unittest.TestCase):
    def test_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            target = launch.write_serve_env("0.0.0.0", 9000, run)
            self.assertEqual(target.read_text(), "UAVLINK_LISTEN=0.0.0.0\nUAVLINK_PORT=9000\n")
            self.assertFalse((run / "serve.env.tmp").exists())

    def test_run_dir_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = ("from uavlink import launch, paths; launch.write_serve_env('127.0.0.1', 8090); "
                    "print((paths.RUN_DIR / 'serve.env').read_text(), end='')")
            import subprocess
            env = dict(os.environ, UAVLINK_RUN_DIR=tmp, PYTHONPATH=str(REPO / "py"))
            out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
            self.assertEqual(out.stdout, "UAVLINK_LISTEN=127.0.0.1\nUAVLINK_PORT=8090\n")

    def test_wait_for_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run / "setup.json").write_text('{"token": "abc", "pid": 42, "port": 8090}')
            self.assertEqual(launch.wait_for_setup(42, 1, run)["token"], "abc")
            self.assertIsNone(launch.wait_for_setup(43, 0.3, run))


if __name__ == "__main__":
    unittest.main()
