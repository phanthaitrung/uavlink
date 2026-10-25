import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "engine" / "cm5" / "install-cm5.sh"
FIXTURE = ROOT / "tests" / "fixtures" / "cm5-dgp"
I18N = ROOT / "share" / "i18n" / "engine-cm5.json"
PREFIX = "@@UAVLINK "


def run_install(*args, state_dir, extra_env=None):
    env = dict(os.environ, UAVLINK_EVENTS="1", UAVLINK_CONFIG_DIR=str(FIXTURE), UAVLINK_STATE_DIR=str(state_dir))
    env.update(extra_env or {})
    proc = subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=120)
    events = [json.loads(line[len(PREFIX):]) for line in proc.stdout.splitlines() if line.startswith(PREFIX)]
    return proc, events


class InstallPreviewTest(unittest.TestCase):
    def test_preview_emits_events_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as state:
            proc, events = run_install("--preview", state_dir=state)
            self.assertIn(proc.returncode, (0, 3), proc.stderr)
            self.assertEqual(os.listdir(state), [])
            steps = [e["id"] for e in events if e["t"] == "step" and e["state"] == "start"]
            self.assertEqual(steps, ["packages", "usb", "eth", "wifi", "tailscale", "router", "security"])
            for sid in steps:
                finals = [e for e in events if e["t"] == "step" and e["id"] == sid and e["state"] != "start"]
                self.assertEqual(len(finals), 1, sid)
            main = [e for e in events if e["t"] == "change" and e["target"] == "/etc/mavlink-router/main.conf"]
            self.assertEqual(len(main), 1)
            self.assertIn(main[0]["action"], ("create", "update"))
            self.assertIn("TcpServerPort = 5760", main[0]["diff"])
            targets = {e["target"] for e in events if e["t"] == "change"}
            self.assertIn("/etc/udev/rules.d/99-ardupilot-no-modemmanager.rules", targets)
            self.assertIn("/etc/systemd/system/uavlink-router.service.d/10-restart.conf", targets)

    def test_preview_single_step(self):
        with tempfile.TemporaryDirectory() as state:
            proc, events = run_install("--preview", "--steps", "router", state_dir=state)
            self.assertIn(proc.returncode, (0, 3), proc.stderr)
            started = [e["id"] for e in events if e["t"] == "step" and e["state"] == "start"]
            self.assertEqual(started, ["router"])
            self.assertTrue(any(e["t"] == "check" and e["id"] == "vps.reach" for e in events))

    def test_unknown_step_is_usage_error(self):
        with tempfile.TemporaryDirectory() as state:
            proc, _ = run_install("--preview", "--steps", "nope", state_dir=state)
            self.assertEqual(proc.returncode, 2)

    def test_apply_refused_on_wsl(self):
        with tempfile.TemporaryDirectory() as state:
            proc, events = run_install(state_dir=state, extra_env={"WSL_DISTRO_NAME": "test", "UAVLINK_ALLOW_WSL": "0"})
            self.assertEqual(proc.returncode, 4, proc.stderr)
            self.assertEqual(os.listdir(state), [])
            fails = [e for e in events if e["t"] == "step" and e["state"] == "fail"]
            self.assertEqual([e["code"] for e in fails], ["env.wsl"])


NM_PROPS = ["lo", "yes", "100", "manual", "192.168.50.2/24, 192.168.144.2/24", "", "", "yes", "yes", "disabled"]


class EthIdempotenceTest(unittest.TestCase):
    """step eth with a NetworkManager shim: a connection that is already right is neither modified nor re-activated
    (each re-activation drops the X25 link for a moment)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        config = tmp / "config"
        config.mkdir()
        # lo: an interface every test machine has, with carrier and without the default route.
        (config / "config.env").write_text((FIXTURE / "config.env").read_text(encoding="utf-8") + "CM5_ETH_IFACE='lo'\n",
                                           encoding="utf-8", newline="\n")
        shims = tmp / "shims"
        shims.mkdir()
        (shims / "systemctl").write_text('#!/bin/sh\ncase "$*" in *NetworkManager*) exit 0 ;; esac\nexit 1\n',
                                         newline="\n")
        (shims / "nmcli").write_text(
            '#!/bin/sh\ncase "$*" in\n'
            '  "-t -f NAME connection show") echo x25-link ;;\n'
            '  "-g "*" connection show x25-link") cat "$SHIM_NM_VALUES" ;;\n'
            '  "-t -f NAME,DEVICE connection show --active") echo "$SHIM_NM_ACTIVE" ;;\n'
            'esac\n', newline="\n")
        for f in shims.iterdir():
            f.chmod(0o755)
        self.tmp, self.config, self.shims = tmp, config, shims

    def nm_changes(self, values, active="x25-link:lo"):
        (self.tmp / "values").write_text("\n".join(values) + "\n", newline="\n")
        env = dict(os.environ, UAVLINK_EVENTS="1", UAVLINK_CONFIG_DIR=str(self.config), PATH=f"{self.shims}:{os.environ['PATH']}",
                   SHIM_NM_VALUES=str(self.tmp / "values"), SHIM_NM_ACTIVE=active)
        with tempfile.TemporaryDirectory() as state:
            env["UAVLINK_STATE_DIR"] = state
            proc = subprocess.run(["bash", str(INSTALL), "--preview", "--steps", "eth"], env=env, capture_output=True,
                                  text=True, timeout=60)
        events = [json.loads(line[len(PREFIX):]) for line in proc.stdout.splitlines() if line.startswith(PREFIX)]
        self.assertEqual([e["state"] for e in events if e["t"] == "step"], ["start", "ok"], proc.stdout + proc.stderr)
        return [(e["target"], e["action"]) for e in events if e["t"] == "change" and e["kind"] == "nm"]

    def test_right_and_active_connection_is_left_alone(self):
        self.assertEqual(self.nm_changes(NM_PROPS), [])

    def test_right_but_inactive_connection_is_only_activated(self):
        self.assertEqual(self.nm_changes(NM_PROPS, active=""), [("x25-link", "start")])

    def test_different_connection_is_updated(self):
        values = list(NM_PROPS)
        values[4] = "192.168.144.20/24"           # the address of the old installer
        self.assertEqual(self.nm_changes(values), [("x25-link", "update")])


class RouterConfTest(unittest.TestCase):
    def test_render_matches_legacy_output_for_dgp_values(self):
        with tempfile.TemporaryDirectory() as state:
            env = dict(os.environ, UAVLINK_EVENTS="0", UAVLINK_CONFIG_DIR=str(FIXTURE), UAVLINK_STATE_DIR=state)
            out = subprocess.run(["bash", str(INSTALL), "--print-config"], env=env, capture_output=True, text=True,
                                 check=True).stdout
        expected = (FIXTURE / "main.conf.expected").read_text(encoding="utf-8")
        self.assertEqual(out, expected)


class I18nTest(unittest.TestCase):
    def test_every_entry_is_bilingual(self):
        data = json.loads(I18N.read_text(encoding="utf-8"))
        for code, texts in data.items():
            self.assertEqual(sorted(texts), ["en", "vi"], code)
            self.assertTrue(texts["vi"] and texts["en"], code)

    def test_every_emitted_code_is_translated(self):
        data = json.loads(I18N.read_text(encoding="utf-8"))
        src = (ROOT / "engine" / "lib" / "common.sh").read_text(encoding="utf-8") + \
            INSTALL.read_text(encoding="utf-8")
        code = r"([a-z0-9_]+\.[a-z0-9_]+)"
        patterns = [rf"\bfail {code}", rf"\bwarn_code {code}", rf"\bstep_warn {code}",
                    rf"\bstep_end \w+ {code}", rf"\bcheck_ev [\w.]+ (?:true|false) {code}",
                    rf"\bev_info {code}", rf"\bev_step \S+ fail {code}"]
        used = {m for p in patterns for m in re.findall(p, src)}
        self.assertGreater(len(used), 20)
        self.assertEqual(sorted(used - set(data)), [])
        for sid in ("packages", "usb", "eth", "wifi", "tailscale", "router", "security"):
            self.assertIn(f"step.{sid}", data)


if __name__ == "__main__":
    unittest.main()


class PlaceholderArgsTest(unittest.TestCase):
    def test_every_placeholder_of_a_coded_event_has_args(self):
        texts = {}
        for f in sorted((ROOT / "share" / "i18n").glob("engine-*.json")):
            texts.update(json.loads(f.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as state:
            proc, events = run_install("--preview", state_dir=state)
        self.assertIn(proc.returncode, (0, 3), proc.stderr)
        checked = 0
        for ev in events:
            code = ev.get("code")
            if code not in texts:
                continue
            checked += 1
            wanted = set(re.findall(r"\{(\w+)\}", " ".join(texts[code].values())))
            missing = wanted - set(ev.get("args") or {})
            self.assertFalse(missing, "%s event %s lacks args %s" % (ev["t"], code, sorted(missing)))
        self.assertGreater(checked, 0)
