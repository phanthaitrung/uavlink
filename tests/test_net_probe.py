"""engine/tools/net_probe.py against a local fake VPS and a fake X25 (ARCHITECTURE.md section 4.3).

The fake VPS is a mavlink-routerd configured like the real one (UDP Server uplink port, TCP server, UDP Server GCS
port) on free local ports. The binary comes from UAVLINK_TEST_ROUTERD, PATH, the installed uavlink-router package or
out/uavlink-router_*.deb (`bash packaging/uavlink-router/build.sh`); without one those tests are skipped. The commands
the probe runs (systemctl, ping, ufw, tailscale) are shims on PATH. Never point these tests at the real VPS or CM5.
"""
import atexit
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "engine" / "tools" / "net_probe.py"
sys.path.insert(0, str(REPO / "vendor"))

spec = importlib.util.spec_from_file_location("net_probe", SCRIPT)
net = importlib.util.module_from_spec(spec)
spec.loader.exec_module(net)
MAV = net.MAV

PASSPHRASE = "fake-vps-signing-passphrase-42"
LINUX = sys.platform.startswith("linux")

SHIMS = {
    "systemctl": '[ "$1" = is-active ] && [ "$3" = "${SHIM_ROUTER_UNIT:-none}" ] && exit 0; exit 3',
    "ping": 'case " $SHIM_FAIL " in *" ping "*) exit 1;; esac; exit 0',
    "ufw": 'if [ -n "${SHIM_UFW_FILE:-}" ]; then cat "$SHIM_UFW_FILE"; else echo "ERROR: You need to be root" >&2; '
           'exit 1; fi',
    "tailscale": 'case " $SHIM_FAIL " in *" tailscale "*) exit 1;; esac; echo "pong from gcs (100.70.184.3) in 9ms"',
}

UFW_HEAD = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
"""
UFW_OK = UFW_HEAD + """Anywhere on eth0           ALLOW IN    Anywhere
Anywhere on tailscale0     ALLOW IN    Anywhere
41641/udp                  ALLOW IN    Anywhere
22/tcp                     ALLOW IN    Anywhere
Anywhere (v6) on eth0      ALLOW IN    Anywhere (v6)
Anywhere (v6) on tailscale0 ALLOW IN    Anywhere (v6)
"""
UFW_NO_TAILSCALE = UFW_HEAD + """Anywhere on eth0           ALLOW IN    Anywhere
22/tcp                     ALLOW IN    Anywhere
"""
UFW_PORT_FROM_X25 = UFW_HEAD + """14550/udp on eth0          ALLOW IN    192.168.50.0/24
Anywhere on tailscale0     ALLOW IN    Anywhere
"""


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_extracted = []


def find_routerd():
    for c in (os.environ.get("UAVLINK_TEST_ROUTERD"), shutil.which("mavlink-routerd"),
              "/usr/lib/uavlink-router/mavlink-routerd"):
        if c and os.access(c, os.X_OK):
            return c
    debs = sorted(p for p in REPO.glob("out/uavlink-router_*.deb") if "dbgsym" not in p.name)
    if not (LINUX and debs and shutil.which("dpkg-deb")):
        return None
    d = tempfile.mkdtemp(prefix="uavlink-routerd-")
    _extracted.append(d)
    if subprocess.run(["dpkg-deb", "-x", str(debs[-1]), d], capture_output=True).returncode != 0:
        return None
    path = os.path.join(d, "usr", "lib", "uavlink-router", "mavlink-routerd")
    return path if os.access(path, os.X_OK) else None


atexit.register(lambda: [shutil.rmtree(d, ignore_errors=True) for d in _extracted])
ROUTERD = find_routerd() if LINUX else None
needs_router = unittest.skipUnless(ROUTERD, "no mavlink-routerd (run bash packaging/uavlink-router/build.sh)")


def _known_texts():
    merged = {}
    for f in sorted((REPO / "share" / "i18n").glob("engine-*.json")):
        merged.update(json.loads(f.read_text(encoding="utf-8")))
    return merged


KNOWN = _known_texts()


class Router:
    """A mavlink-routerd with the given endpoints; `tcp` is its TCP server port."""

    def __init__(self, tmp, name, tcp, endpoints):
        self.tcp = tcp
        conf = tmp / f"{name}.conf"
        lines = ["[General]", f"TcpServerPort = {tcp}", "ReportStats = false", "MavlinkDialect = ardupilotmega", ""]
        for ep, mode, addr, port in endpoints:
            lines += [f"[UdpEndpoint {ep}]", f"Mode = {mode}", f"Address = {addr}", f"Port = {port}", ""]
        conf.write_text("\n".join(lines), encoding="utf-8")
        self.conf = conf
        confdir = tmp / f"{name}.d"
        confdir.mkdir()
        self.log = open(tmp / f"{name}.log", "wb")
        self.proc = subprocess.Popen([ROUTERD, "-c", str(conf), "-d", str(confdir)], stdout=self.log,
                                     stderr=subprocess.STDOUT)
        end = time.time() + 10
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", tcp), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"{name} router did not start: {(tmp / f'{name}.log').read_text()}")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()


class _Out:
    def __init__(self, write):
        self.write = write


# NET_* of a configured X25 (ChibiOS with Ethernet), and of SITL / a Linux board: no NET_IPADDR*, NET_NETMASK there.
X25_PARAMS = {"NET_ENABLE": 1.0, "NET_IPADDR3": 1.0, "NET_NETMASK": 24.0, "NET_OPTIONS": 0.0, "NET_P1_PORT": 14550.0}
SITL_PARAMS = {"NET_ENABLE": 1.0, "NET_OPTIONS": 0.0, "NET_P1_PORT": 14550.0}


class FakeX25(threading.Thread):
    """Vehicle heartbeats at 10 Hz to `target` (UDP); answers PARAM_REQUEST_READ of the `params` it has, only signed
    requests when it has a `passphrase`. Counts what it gets from the probe (sysid 250)."""

    def __init__(self, target, passphrase=None, sysid=1, params=X25_PARAMS):
        super().__init__(daemon=True)
        self.sysid, self.target, self.params = sysid, target, params
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.mav = MAV.MAVLink(_Out(self._send), srcSystem=sysid, srcComponent=1)
        self.mav.robust_parsing = True
        if passphrase:
            s = self.mav.signing
            s.secret_key = hashlib.sha256(passphrase.encode()).digest()
            s.sign_outgoing = True
            s.allow_unsigned_callback = lambda mav, msg_id: msg_id != MAV.MAVLINK_MSG_ID_PARAM_REQUEST_READ
            s.timestamp = int((time.time() - net.SIGNING_EPOCH) * 100000)
        self.answered = 0
        self.from_probe = []
        self.seen = set()           # source sysids of everything received
        self._halt = threading.Event()

    def _send(self, data):
        try:
            self.sock.sendto(data, self.target)
        except OSError:
            pass

    def run(self):
        next_hb = 0
        while not self._halt.is_set():
            if time.time() >= next_hb:
                self.mav.heartbeat_send(MAV.MAV_TYPE_QUADROTOR, MAV.MAV_AUTOPILOT_ARDUPILOTMEGA, 0, 0,
                                        MAV.MAV_STATE_STANDBY)
                next_hb = time.time() + 0.1
            try:
                data = self.sock.recv(65535)
            except OSError:
                continue
            for m in self.mav.parse_buffer(data) or []:
                if m.get_type() == "BAD_DATA":
                    continue
                self.seen.add(m.get_srcSystem())
                if m.get_srcSystem() == net.PROBE_SYSID:
                    self.from_probe.append(m.get_type())
                if m.get_type() == "PARAM_REQUEST_READ" and m.target_system == self.sysid:
                    pid = m.param_id
                    name = (pid.decode("ascii", "ignore") if isinstance(pid, bytes) else pid).rstrip("\x00")
                    if name in self.params:     # like ArduPilot: a name it does not have gets no answer
                        self.mav.param_value_send(name.encode(), self.params[name], MAV.MAV_PARAM_TYPE_REAL32,
                                                  len(self.params), 0)
                        self.answered += 1

    def stop(self):
        self._halt.set()
        self.join(2)
        self.sock.close()


class Observer(threading.Thread):
    """A TCP client of the fake VPS that records every message; with `gcs_sysid` it also sends GCS heartbeats."""

    def __init__(self, port, gcs_sysid=None):
        super().__init__(daemon=True)
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.sock.settimeout(0.05)
        self.mav = MAV.MAVLink(_Out(self.sock.sendall), srcSystem=gcs_sysid or 0, srcComponent=190)
        self.mav.robust_parsing = True
        self.gcs = gcs_sysid
        self.msgs = []
        self._halt = threading.Event()

    def run(self):
        next_hb = 0
        while not self._halt.is_set():
            if self.gcs and time.time() >= next_hb:
                self.mav.heartbeat_send(MAV.MAV_TYPE_GCS, MAV.MAV_AUTOPILOT_INVALID, 0, 0, 0)
                next_hb = time.time() + 0.2
            try:
                data = self.sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self.msgs += [m for m in self.mav.parse_buffer(data) or [] if m.get_type() != "BAD_DATA"]

    def stop(self):
        self._halt.set()
        self.join(2)
        self.sock.close()


class ProbeTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="uavlink-net-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.shims = self.tmp / "shims"
        self.shims.mkdir()
        for name, body in SHIMS.items():
            sh = self.shims / name
            sh.write_text(f"#!/bin/sh\n{body}\n", newline="\n")
            sh.chmod(0o755)
        self.config_dir = self.tmp / "config"
        self.config_dir.mkdir()
        self.ports = {"vps_tcp": free_port(), "uplink": free_port(socket.SOCK_DGRAM),
                      "gcs_udp": free_port(socket.SOCK_DGRAM), "cm5_tcp": free_port(),
                      "lan": free_port(socket.SOCK_DGRAM)}
        self.config = {
            "X25_IP": "127.0.0.1", "MAVLINK_LAN_PORT": self.ports["lan"], "CM5_ETH_IFACE": "eth0",
            "CM5_TCP_SERVER_PORT": self.ports["cm5_tcp"], "VPS_PUBLIC_IP": "127.0.0.1",
            "VPS_UPLINK_PORT": self.ports["uplink"], "VPS_GCS_TCP_PORT": self.ports["vps_tcp"],
            "VPS_GCS_UDP_PORT": self.ports["gcs_udp"], "CM5_EXTRA_UDP_TARGETS": "office=100.70.184.3:14550",
            "MAVLINK_SIGNING": "0", "CM5_INSTALL_TAILSCALE": "1",
        }
        self.secrets = None
        self.env = {}
        self.router_conf = self.tmp / "none.conf"
        self.internet = f"127.0.0.1:{self.ports['vps_tcp']}"
        self.cleanups = []

    def tearDown(self):
        for stop in reversed(self.cleanups):
            stop()

    # -- fakes --

    def fake_vps(self):
        r = Router(self.tmp, "vps", self.ports["vps_tcp"],
                   [("uplink", "Server", "127.0.0.1", self.ports["uplink"]),
                    ("gcs", "Server", "127.0.0.1", self.ports["gcs_udp"])])
        self.cleanups.append(r.stop)
        return r

    def local_router(self):
        """The router of "this" CM5: X25 on the LAN port, pushes to the fake VPS, TCP server."""
        r = Router(self.tmp, "cm5", self.ports["cm5_tcp"],
                   [("x25_ethernet", "Server", "127.0.0.1", self.ports["lan"]),
                    ("vps_mavlink", "Normal", "127.0.0.1", self.ports["uplink"])])
        self.cleanups.append(r.stop)
        self.router_conf = r.conf
        self.env["SHIM_ROUTER_UNIT"] = "uavlink-router.service"
        return r

    def fake_x25(self, port, passphrase=None, params=X25_PARAMS):
        x = FakeX25(("127.0.0.1", port), passphrase, params=params)
        x.start()
        self.cleanups.append(x.stop)
        return x

    def observer(self, gcs_sysid=None):
        o = Observer(self.ports["vps_tcp"], gcs_sysid)
        o.start()
        self.cleanups.append(o.stop)
        return o

    def ufw(self, text):
        f = self.tmp / "ufw.txt"
        f.write_text(text, encoding="utf-8")
        self.env["SHIM_UFW_FILE"] = str(f)

    # -- run --

    def run_probe(self, *args, timeout=60):
        lines = [f"{k}='{v}'" for k, v in self.config.items()]
        (self.config_dir / "config.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if self.secrets is not None:
            (self.config_dir / "secrets.env").write_text(self.secrets, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SHIM_", "MAVLINK", "UAVLINK_"))}
        env.update({
            "PATH": f"{self.shims}:{os.environ.get('PATH', '/usr/bin:/bin')}", "UAVLINK_EVENTS": "1",
            "UAVLINK_CONFIG_DIR": str(self.config_dir), "UAVLINK_ROUTER_CONF": str(self.router_conf),
            "UAVLINK_FALLBACK_SECRETS": str(self.tmp / "fallback-secrets.env"),
            "UAVLINK_INTERNET_HOSTS": self.internet, "PYTHONPATH": str(REPO / "vendor"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.update(self.env)
        t0 = time.time()
        p = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True,
                           timeout=timeout)
        self.elapsed = time.time() - t0
        evs = [json.loads(line[len("@@UAVLINK "):]) for line in p.stdout.splitlines()
               if line.startswith("@@UAVLINK ")]
        self.assert_texts_known(evs)
        self.out = p.stdout + p.stderr
        return p, evs

    def assert_texts_known(self, evs):
        for e in evs:
            if e.get("code"):
                self.assertIn(e["code"], KNOWN, e)
            if e["t"] == "step":
                self.assertIn("step." + e["id"], KNOWN, e)
            if e["t"] == "check":
                cid = "gcs.tailscale" if e["id"].startswith("gcs.tailscale.") else e["id"]
                self.assertIn(cid, KNOWN, e)
                self.assertTrue(e.get("msg"), e)

    @staticmethod
    def checks(evs):
        return {e["id"]: e for e in evs if e["t"] == "check"}

    @staticmethod
    def steps(evs):
        return {e["id"]: e for e in evs if e["t"] == "step" and e["state"] != "start"}

    def assertCheck(self, evs, cid, code, ok=True, warn=False):
        c = self.checks(evs).get(cid)
        self.assertIsNotNone(c, f"no check {cid}:\n{self.out}")
        self.assertEqual((c["ok"], c.get("code")), (ok, code), f"{c}\n{self.out}")
        self.assertEqual(c.get("severity") == "warn", warn, c)
        return c


@unittest.skipUnless(LINUX, "needs Linux sockets and /proc/net")
class ProbeModeTest(ProbeTestBase):
    @needs_router
    def test_probe_proves_both_ways_and_gcs_udp(self):
        self.fake_vps()
        x25 = self.fake_x25(self.ports["lan"])          # no local router: the probe listens on the LAN port itself
        watch = self.observer()
        self.ufw(UFW_OK)
        p, evs = self.run_probe()
        self.assertEqual(p.returncode, 0, self.out)
        self.assertIn({"t": "info", "code": "net.mode.probe", "msg": evs[0]["msg"]}, evs)
        self.assertCheck(evs, "x25.lan_ping", "x25.lan_ping.ok")
        lan = self.assertCheck(evs, "x25.lan_mavlink", "x25.lan_mavlink.ok")
        self.assertEqual(lan["args"], {"ip": "127.0.0.1", "port": self.ports["lan"], "sysid": 1})
        self.assertCheck(evs, "internet", "internet.reachable")
        self.assertNotIn("vps.dns", self.checks(evs))   # an IP needs no DNS
        self.assertCheck(evs, "vps.ping", "vps.ping.ok")
        self.assertCheck(evs, "vps.tcp", "vps.tcp.ok")
        up = self.assertCheck(evs, "vps.uplink", "vps.uplink.relayed")
        self.assertEqual(up["args"], {"ip": "127.0.0.1", "port": self.ports["uplink"],
                                      "tcp_port": self.ports["vps_tcp"]})
        self.assertCheck(evs, "vps.downlink", "vps.downlink.relayed")
        self.assertCheck(evs, "vps.gcs_udp", "vps.gcs_udp.ok")
        self.assertCheck(evs, "gcs.tailscale.office", "gcs.tailscale.ok")
        self.assertCheck(evs, "cm5.firewall", "cm5.firewall.ok")
        self.assertIn("cm5.router_stopped", [e.get("code") for e in evs if e["t"] == "info"])
        self.assertNotIn("cm5.listen", self.checks(evs))
        self.assertEqual({s: e["state"] for s, e in self.steps(evs).items()},
                         {"net.lan": "ok", "net.internet": "ok", "net.vps": "ok", "net.gcs": "ok", "net.cm5": "ok"})
        self.assertEqual(evs[-1]["t"], "result")
        self.assertTrue(evs[-1]["ok"])
        self.assertEqual(evs[-1]["data"]["mode"], "probe")
        # What an outside GCS saw from the probe: GCS-type heartbeats only, never a vehicle.
        mine = [m for m in watch.msgs if m.get_srcSystem() == net.PROBE_SYSID]
        self.assertTrue(mine)
        self.assertEqual({m.get_type() for m in mine}, {"HEARTBEAT"})
        self.assertEqual({(m.type, m.autopilot) for m in mine}, {(MAV.MAV_TYPE_GCS, MAV.MAV_AUTOPILOT_INVALID)})
        self.assertEqual(x25.from_probe, [])
        self.assertLess(self.elapsed, 15)

    @needs_router
    def test_udp_tests_skipped_while_another_cm5_is_live(self):
        self.config["X25_IP"] = ""
        self.fake_vps()
        other = self.fake_x25(self.ports["uplink"])     # another CM5 already pushes a vehicle to the VPS
        watch = self.observer()
        p, evs = self.run_probe("--mode", "probe")
        self.assertEqual(p.returncode, 0, self.out)      # warnings only
        c = self.assertCheck(evs, "vps.udp", "vps.udp_skipped_live", ok=False, warn=True)
        self.assertEqual(c["args"], {"sysid": 1})
        for cid in ("vps.uplink", "vps.downlink", "vps.gcs_udp"):
            self.assertNotIn(cid, self.checks(evs))
        self.assertCheck(evs, "vps.tcp", "vps.tcp.ok")
        self.assertEqual(self.steps(evs)["net.vps"]["state"], "warn")
        # Nothing from the probe reached the live vehicle or the GCS side.
        self.assertEqual(other.from_probe, [])
        self.assertEqual([m for m in watch.msgs if m.get_srcSystem() == net.PROBE_SYSID], [])
        # The VPS still sends GCS traffic to the live CM5: its uplink endpoint was not taken over.
        watch.mav.srcSystem = 251
        watch.mav.heartbeat_send(MAV.MAV_TYPE_GCS, MAV.MAV_AUTOPILOT_INVALID, 0, 0, 0)
        end = time.time() + 3
        while time.time() < end and 251 not in other.seen:
            time.sleep(0.05)
        self.assertIn(251, other.seen, "the live CM5 lost its place on the VPS uplink endpoint")

    @needs_router
    def test_gcs_udp_skipped_while_a_gcs_is_connected(self):
        self.fake_vps()
        self.fake_x25(self.ports["lan"])
        self.observer(gcs_sysid=255)                     # Mission Planner on the VPS
        p, evs = self.run_probe("--mode", "probe")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertCheck(evs, "vps.uplink", "vps.uplink.relayed")
        self.assertCheck(evs, "vps.downlink", "vps.downlink.relayed")
        c = self.assertCheck(evs, "vps.gcs_udp", "vps.gcs_udp_skipped_live", ok=False, warn=True)
        self.assertEqual(c["args"], {"sysid": 255})


@unittest.skipUnless(LINUX, "needs Linux sockets and /proc/net")
class LiveModeTest(ProbeTestBase):
    def live_setup(self, x25_passphrase=PASSPHRASE):
        self.config["MAVLINK_SIGNING"] = "1"
        self.fake_vps()
        self.local_router()
        return self.fake_x25(self.ports["lan"], x25_passphrase)

    @needs_router
    def test_auto_picks_live_and_reads_a_signed_param_through_the_vps(self):
        x25 = self.live_setup()
        self.secrets = f"MAVLINK_SIGNING_PASSPHRASE='{PASSPHRASE}'\n"
        watch = self.observer()
        p, evs = self.run_probe()
        self.assertEqual(p.returncode, 0, self.out)
        self.assertIn("net.mode.live", [e.get("code") for e in evs if e["t"] == "info"])
        lan = self.assertCheck(evs, "x25.lan_mavlink", "x25.lan_mavlink.router")
        self.assertEqual(lan["args"], {"port": self.ports["cm5_tcp"], "sysid": 1})
        self.assertCheck(evs, "vps.tcp", "vps.tcp.ok")
        via = self.assertCheck(evs, "vps.x25_via_vps", "vps.x25_via_vps.ok")
        self.assertEqual(via["args"]["sysid"], 1)
        cmd = self.assertCheck(evs, "vps.cmd_via_vps", "vps.cmd_via_vps.ok")
        self.assertEqual(cmd["args"], {"param": "NET_ENABLE", "value": "1"})
        self.assertGreaterEqual(x25.answered, 1)
        listen = self.assertCheck(evs, "cm5.listen", "cm5.listen.ok")
        self.assertEqual(listen["args"], {"tcp": self.ports["cm5_tcp"], "udp": self.ports["lan"]})
        for cid in ("vps.udp", "vps.uplink", "vps.downlink", "vps.gcs_udp"):
            self.assertNotIn(cid, self.checks(evs))
        # Live mode talks to the VPS only over TCP (compid 192), never through its UDP endpoints.
        comps = {m.get_srcComponent() for m in watch.msgs if m.get_srcSystem() == net.PROBE_SYSID}
        self.assertLessEqual(comps, {net.COMP_TCP})
        self.assertNotIn(PASSPHRASE, self.out)
        self.assertEqual(evs[-1]["data"]["mode"], "live")

    @needs_router
    def test_signed_read_on_a_board_without_host_ip_params(self):
        # SITL / Linux boards have no NET_IPADDR*: the probe must read a parameter every networked firmware has
        # (it read NET_IPADDR3 before, which such a board never answers).
        self.config["MAVLINK_SIGNING"] = "1"
        self.secrets = f"MAVLINK_SIGNING_PASSPHRASE='{PASSPHRASE}'\n"
        self.fake_vps()
        self.local_router()
        x25 = self.fake_x25(self.ports["lan"], PASSPHRASE, params=SITL_PARAMS)
        p, evs = self.run_probe("--mode", "live")
        self.assertEqual(p.returncode, 0, self.out)
        cmd = self.assertCheck(evs, "vps.cmd_via_vps", "vps.cmd_via_vps.ok")
        self.assertEqual(cmd["args"], {"param": "NET_ENABLE", "value": "1"})
        self.assertGreaterEqual(x25.answered, 1)

    @needs_router
    def test_passphrase_from_the_default_secrets_file(self):
        self.live_setup()
        (self.tmp / "fallback-secrets.env").write_text(f'MAVLINK_SIGNING_PASSPHRASE="{PASSPHRASE}"\n')
        p, evs = self.run_probe("--mode", "live")
        self.assertCheck(evs, "vps.cmd_via_vps", "vps.cmd_via_vps.ok")

    @needs_router
    def test_missing_passphrase_is_a_warning(self):
        self.live_setup()
        p, evs = self.run_probe("--mode", "live")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertCheck(evs, "vps.x25_via_vps", "vps.x25_via_vps.ok")
        self.assertCheck(evs, "vps.cmd_via_vps", "vps.cmd_no_passphrase", ok=False, warn=True)
        self.assertEqual(self.steps(evs)["net.vps"]["state"], "warn")

    @needs_router
    def test_wrong_passphrase_fails_the_command(self):
        x25 = self.live_setup()
        self.secrets = "MAVLINK_SIGNING_PASSPHRASE='not-the-key-on-the-x25'\n"
        p, evs = self.run_probe("--mode", "live", "--timeout", "10")
        self.assertEqual(p.returncode, 3, self.out)
        self.assertCheck(evs, "vps.x25_via_vps", "vps.x25_via_vps.ok")     # telemetry still flows
        self.assertCheck(evs, "vps.cmd_via_vps", "vps.cmd_via_vps.fail", ok=False)
        self.assertEqual(x25.answered, 0)

    @needs_router
    def test_unsigned_read_when_signing_is_off(self):
        self.fake_vps()
        self.local_router()
        self.fake_x25(self.ports["lan"])
        p, evs = self.run_probe("--mode", "live")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertCheck(evs, "vps.cmd_via_vps", "vps.cmd_via_vps.ok")


class _TcpServer(threading.Thread):
    """Accepts on `port` and then: "silent" (sends nothing), "close" (closes at once) or "http" (non-MAVLink)."""

    def __init__(self, port, how):
        super().__init__(daemon=True)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(8)
        self.srv.settimeout(0.1)
        self.how, self.conns = how, []
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                continue
            if self.how == "close":
                c.close()
                continue
            if self.how == "http":
                c.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n" * 4)
            self.conns.append(c)

    def stop(self):
        self._halt.set()
        self.join(2)
        for c in self.conns:
            c.close()
        self.srv.close()


@unittest.skipUnless(LINUX, "needs Linux sockets and /proc/net")
class FailureTest(ProbeTestBase):
    def tcp_server(self, how):
        s = _TcpServer(self.ports["vps_tcp"], how)
        s.start()
        self.cleanups.append(s.stop)

    def test_refused_and_unreachable_vps(self):
        self.internet = "127.0.0.1:9"
        p, evs = self.run_probe("--timeout", "8")
        self.assertEqual(p.returncode, 3)
        self.assertCheck(evs, "vps.tcp", "vps.tcp.refused", ok=False)
        self.assertCheck(evs, "internet", "internet.unreachable", ok=False)
        for cid in ("vps.udp", "vps.uplink", "vps.downlink", "vps.gcs_udp"):
            self.assertNotIn(cid, self.checks(evs))        # without the TCP view nothing UDP is sent

        self.config["VPS_PUBLIC_IP"] = "192.0.2.1"            # TEST-NET-1: never answers
        self.env["SHIM_FAIL"] = "ping"
        p, evs = self.run_probe("--timeout", "8")
        self.assertEqual(p.returncode, 3)
        self.assertCheck(evs, "vps.ping", "vps.ping.fail", ok=False, warn=True)
        c = self.checks(evs)["vps.tcp"]
        self.assertFalse(c["ok"])
        self.assertIn(c["code"], ("vps.tcp.timeout", "vps.tcp.error"))
        self.assertLess(self.elapsed, 8 + 2)

    def test_unresolvable_name(self):
        self.config["VPS_PUBLIC_IP"] = "no-such-host.invalid"
        p, evs = self.run_probe("--timeout", "8")
        self.assertEqual(p.returncode, 3)
        c = self.assertCheck(evs, "vps.dns", "vps.dns.fail", ok=False)
        self.assertEqual(c["args"], {"host": "no-such-host.invalid"})
        self.assertNotIn("vps.tcp", self.checks(evs))

    def test_silent_vps_times_out_within_the_budget(self):
        self.tcp_server("silent")                       # TCP accepts, UDP ports drop everything
        p, evs = self.run_probe("--mode", "probe", "--timeout", "6")
        self.assertEqual(p.returncode, 3)
        self.assertCheck(evs, "x25.lan_mavlink", "x25.lan_mavlink.fail", ok=False)
        self.assertCheck(evs, "vps.tcp", "vps.tcp.ok")
        self.assertCheck(evs, "vps.uplink", "vps.uplink.lost", ok=False)
        self.assertCheck(evs, "vps.downlink", "vps.downlink.lost", ok=False)
        self.assertCheck(evs, "vps.gcs_udp", "vps.gcs_udp.fail", ok=False)
        self.assertLess(self.elapsed, 6 + 1.5)

    def test_closed_and_not_mavlink(self):
        self.tcp_server("close")
        p, evs = self.run_probe("--mode", "probe", "--timeout", "5")
        self.assertCheck(evs, "vps.tcp", "vps.tcp.closed", ok=False)
        self.assertNotIn("vps.uplink", self.checks(evs))
        self.tearDown()
        self.cleanups = []
        self.ports["vps_tcp"] = free_port()
        self.config["VPS_GCS_TCP_PORT"] = self.ports["vps_tcp"]
        self.tcp_server("http")
        p, evs = self.run_probe("--mode", "probe", "--timeout", "5")
        self.assertCheck(evs, "vps.tcp", "vps.tcp.not_mavlink", ok=False)

    def test_live_mode_without_router(self):
        self.config["VPS_PUBLIC_IP"] = ""
        p, evs = self.run_probe("--mode", "live", "--timeout", "5")
        self.assertEqual(p.returncode, 3)
        self.assertCheck(evs, "x25.lan_mavlink", "x25.lan_mavlink.router_fail", ok=False)
        self.assertCheck(evs, "cm5.listen", "cm5.listen.none", ok=False)

    def test_nothing_configured_is_skipped(self):
        self.config.update({"X25_IP": "", "VPS_PUBLIC_IP": "", "CM5_EXTRA_UDP_TARGETS": "", "CM5_INSTALL_TAILSCALE": "0"})
        self.internet = "127.0.0.1:9"
        p, evs = self.run_probe("--timeout", "5")
        self.assertEqual(p.returncode, 0, self.out)          # without a VPS, no Internet is only a warning
        steps = self.steps(evs)
        self.assertEqual((steps["net.lan"]["state"], steps["net.lan"]["code"]), ("skip", "net.no_x25_ip"))
        self.assertEqual((steps["net.vps"]["state"], steps["net.vps"]["code"]), ("skip", "net.no_vps"))
        self.assertEqual((steps["net.gcs"]["state"], steps["net.gcs"]["code"]), ("skip", "gcs.no_targets"))
        self.assertCheck(evs, "internet", "internet.unreachable", ok=False, warn=True)

    def test_tailscale_and_firewall_results(self):
        self.config["VPS_PUBLIC_IP"] = ""
        self.config["CM5_EXTRA_UDP_TARGETS"] = "office=100.70.184.3:14550 lan=192.168.1.5:14550 home=100.101.1.2:14550"
        self.env["SHIM_FAIL"] = "tailscale"
        self.ufw(UFW_NO_TAILSCALE)
        p, evs = self.run_probe("--timeout", "5")
        self.assertEqual(p.returncode, 3)
        checks = self.checks(evs)
        self.assertEqual(sorted(c for c in checks if c.startswith("gcs.")), ["gcs.tailscale.home", "gcs.tailscale.office"])
        c = self.assertCheck(evs, "gcs.tailscale.office", "gcs.tailscale.fail", ok=False, warn=True)
        self.assertEqual(c["args"], {"name": "office", "ip": "100.70.184.3"})
        self.assertCheck(evs, "cm5.firewall", "cm5.firewall.no_tailscale", ok=False)

        del self.env["SHIM_UFW_FILE"]                        # not root: the state cannot be read
        p, evs = self.run_probe("--timeout", "5")
        self.assertCheck(evs, "cm5.firewall", "cm5.firewall.unknown", ok=False, warn=True)

    @unittest.skipIf(shutil.which("tailscale"), "a real tailscale is installed")
    def test_no_tailscale_binary(self):
        (self.shims / "tailscale").unlink()
        self.config["VPS_PUBLIC_IP"] = ""
        p, evs = self.run_probe("--timeout", "5")
        self.assertEqual(self.steps(evs)["net.gcs"]["code"], "gcs.no_tailscale")

    def test_lan_port_busy(self):
        self.config["VPS_PUBLIC_IP"] = ""
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("0.0.0.0", self.ports["lan"]))
            p, evs = self.run_probe("--mode", "probe", "--timeout", "5")
        self.assertCheck(evs, "x25.lan_mavlink", "x25.lan_mavlink.port_busy", ok=False, warn=True)

    def test_usage(self):
        for args in (["--mode", "x"], ["--timeout", "0"]):
            p = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                               env=dict(os.environ, PYTHONPATH=str(REPO / "vendor")))
            self.assertEqual(p.returncode, 2, args)


class HelpersTest(unittest.TestCase):
    def test_router_conf_finds_the_vps_endpoint(self):
        sections = net.read_router_conf(REPO / "tests" / "fixtures" / "cm5-dgp" / "main.conf.expected")
        self.assertEqual(net.vps_endpoint(sections, {"209.97.167.92"}), "vps_mavlink")
        self.assertEqual(net.vps_endpoint(sections, {"mav.dgp.vn", "203.0.113.9"}), None)
        servers = [s["name"] for s in sections if s.get("mode") == "Server"]
        self.assertEqual(servers, ["x25_ethernet"])         # a Server endpoint is never the uplink
        self.assertEqual(net.read_router_conf("/nonexistent/main.conf"), [])

    def test_manual_fixture_matches_its_fake_vps(self):
        d = REPO / "tests" / "fixtures" / "net-fake-vps"
        cfg = net.read_env_file(d / "config.env")
        vps = net.read_router_conf(d / "vps.conf")
        general = next(s for s in vps if s["type"] == "general")
        ports = {s["name"]: s["port"] for s in vps if s["type"] == "udpendpoint"}
        self.assertEqual((cfg["VPS_PUBLIC_IP"], cfg["VPS_GCS_TCP_PORT"]), ("127.0.0.1", general["tcpserverport"]))
        self.assertEqual((cfg["VPS_UPLINK_PORT"], cfg["VPS_GCS_UDP_PORT"]), (ports["uplink"], ports["gcs"]))

    def test_tailscale_targets(self):
        self.assertEqual(net.tailscale_targets("office=100.70.184.3:14550 lan=192.168.1.5:14550 bad x=y:1 "
                                               "edge=100.127.255.254:1 out=100.128.0.1:1"),
                         [("office", "100.70.184.3"), ("edge", "100.127.255.254")])

    def test_ufw_rules(self):
        active, default_allow, rules = net.ufw_rules(UFW_OK)
        self.assertTrue(active)
        self.assertFalse(default_allow)
        self.assertTrue(net._ufw_allows(rules, "eth0", 14550, "192.168.50.1"))
        self.assertTrue(net._ufw_allows(rules, "tailscale0"))
        _, _, rules = net.ufw_rules(UFW_NO_TAILSCALE)
        self.assertFalse(net._ufw_allows(rules, "tailscale0"))
        _, _, rules = net.ufw_rules(UFW_PORT_FROM_X25)
        self.assertTrue(net._ufw_allows(rules, "eth0", 14550, "192.168.50.1"))
        self.assertFalse(net._ufw_allows(rules, "eth0", 14550, "10.0.0.5"))
        self.assertFalse(net._ufw_allows(rules, "eth0", 14551, "192.168.50.1"))
        self.assertEqual(net.ufw_rules("Status: inactive\n")[0], False)

    def test_listening_reads_proc_net(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "tcp").write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue\n"
                "   0: 0100007F:1680 00000000:0000 0A 00000000:00000000\n"
                "   1: 0100007F:1681 0100007F:D2F0 01 00000000:00000000\n")
            Path(d, "udp6").write_text(
                "  sl  local_address rem_address   st\n"
                "   0: 00000000000000000000000000000000:38D6 00000000000000000000000000000000:0000 07\n")
            os.environ["UAVLINK_PROC_NET"] = d
            try:
                self.assertTrue(net.listening("tcp", 5760))
                self.assertFalse(net.listening("tcp", 5761))       # an established connection, not a listener
                self.assertTrue(net.listening("udp", 14550))
                self.assertFalse(net.listening("udp", 14551))
            finally:
                del os.environ["UAVLINK_PROC_NET"]

    def test_default_budget(self):
        w = net.WAITS
        ping = w["ping"] + 0.2                               # 2 packets 0.2 s apart
        probe = (w["dns"] + ping + w["lan"] + w["internet"] + ping + w["connect"] + w["listen"] + w["uplink"]
                 + w["downlink"] + w["gcs_udp"] + w["tailscale"])
        live = (w["dns"] + ping + w["connect"] + w["lan"] + w["internet"] + ping + w["connect"]
                + w["x25_via_vps"] + w["cmd"] + w["tailscale"])
        self.assertLessEqual(max(probe, live), net.DEFAULT_TIMEOUT - 2)    # room for start-up


if __name__ == "__main__":
    unittest.main()
