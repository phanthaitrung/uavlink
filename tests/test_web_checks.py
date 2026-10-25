"""Grouped checks on the setup page (ARCHITECTURE.md 4.1) and the mock server's pre-check/apply streams (4.3)."""
import importlib.util
import re
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "share" / "web" / "app.js"

# ARCHITECTURE.md 4.1: the page groups checks by the first part of the id; anything else goes to "other".
EXPECTED_GROUPS = {
    "x25": "x25", "lan": "lan", "eth": "lan", "internet": "vps", "vps": "vps", "gcs": "gcs",
    "cm5": "cm5", "tailscale": "cm5", "router": "cm5", "ssh": "cm5", "ufw": "cm5", "signing": "cm5",
}
X25_STATUS_FIELDS = {"firmware", "board", "vehicle", "system_status", "armed", "sensors", "gps", "battery", "ekf",
                     "prearm", "net"}


def load_mock():
    spec = importlib.util.spec_from_file_location("mock_server", ROOT / "tools" / "mock-server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mock(**kw):
    opts = dict(preview_only=False, vps=False, no_x25=False, armed=False, fail_apply=False, delay=0)
    opts.update(kw)
    return MOCK.Mock(types.SimpleNamespace(**opts))


MOCK = load_mock()


def checks(events):
    return [e for e in events if isinstance(e, dict) and e.get("t") == "check"]


class TestGroups(unittest.TestCase):
    def test_app_js_group_map(self):
        src = APP_JS.read_text(encoding="utf-8")
        m = re.search(r"var GROUP_OF = \{(.*?)\};", src, re.S)
        self.assertIsNotNone(m, "GROUP_OF not found in app.js")
        found = dict(re.findall(r"(\w+): \"(\w+)\"", m.group(1)))
        self.assertEqual(found, EXPECTED_GROUPS)
        order = re.search(r"var GROUP_ORDER = \[(.*?)\];", src).group(1)
        self.assertEqual(re.findall(r"\"(\w+)\"", order), ["x25", "lan", "vps", "gcs", "cm5", "other"])


class TestMockStreams(unittest.TestCase):
    def setUp(self):
        self.m = mock()
        self.values = dict(self.m.default_values)

    def test_precheck_stages_checks_and_status(self):
        evs = self.m.scripts("precheck", self.values)
        stages = [e["id"] for e in evs if isinstance(e, dict) and e.get("t") == "stage" and e["state"] == "start"]
        self.assertEqual(stages, ["cm5", "x25", "net"])
        cs = checks(evs)
        warn = [c["id"] for c in cs if c.get("severity") == "warn"]
        fail = [c["id"] for c in cs if c["ok"] is False and c.get("severity") != "warn"]
        self.assertGreaterEqual(len(warn), 2)
        self.assertEqual(fail, ["vps.gcs_udp"])
        for c in cs:
            if c.get("severity"):
                self.assertIs(c["ok"], False, c["id"])
        ids = {c["id"] for c in cs}
        for cid in ("x25.sensors", "x25.gps", "x25.battery", "x25.ekf", "x25.prearm", "x25.lan_ping", "vps.uplink",
                    "vps.downlink", "vps.gcs_udp", "cm5.listen", "cm5.firewall"):
            self.assertIn(cid, ids)
        status = [e for e in evs if isinstance(e, dict) and e.get("t") == "info" and e.get("code") == "x25.status"]
        self.assertEqual(len(status), 1)
        self.assertEqual(set(status[0]["data"]), X25_STATUS_FIELDS)
        self.assertIn("FAIL", evs, "a failed check fails the pre-check job, as in the runner")

    def test_apply_stream(self):
        evs = self.m.scripts("apply", self.values, props_off=True)
        stages = [e["id"] for e in evs if isinstance(e, dict) and e.get("t") == "stage" and e["state"] == "start"]
        self.assertEqual(stages, ["cm5", "x25", "verify", "net"])
        cs = checks(evs)
        self.assertTrue(any(c.get("severity") == "warn" for c in cs))
        self.assertEqual([c["id"] for c in cs if c["ok"] is False and c.get("severity") != "warn"], ["vps.gcs_udp"])
        self.assertTrue(any(e.get("code") == "x25.status" for e in evs if isinstance(e, dict)))
        self.assertNotIn("FAIL", evs)

    def test_no_x25(self):
        m = mock(no_x25=True)
        for kind in ("precheck", "apply"):
            evs = m.scripts(kind, self.values, props_off=True)
            self.assertEqual([c["id"] for c in checks(evs) if c["id"].startswith("x25.")], [], kind)
            self.assertFalse(any(e.get("code") == "x25.status" for e in evs if isinstance(e, dict)), kind)

    def test_every_code_has_both_languages(self):
        m = mock(armed=True)
        evs = m.scripts("precheck", self.values) + self.m.scripts("apply", self.values, props_off=True)
        evs += self.m.net_events(self.values, live=True)[0]
        for e in evs:
            if isinstance(e, dict) and e.get("code"):
                text = m.engine.get(e["code"])
                self.assertIsNotNone(text, e["code"])
                self.assertEqual(set(text), {"vi", "en"}, e["code"])
                for lang in ("vi", "en"):
                    for name in re.findall(r"\{(\w+)\}", text[lang]):
                        self.assertIn(name, e.get("args") or {}, "%s: {%s}" % (e["code"], name))


if __name__ == "__main__":
    unittest.main()
