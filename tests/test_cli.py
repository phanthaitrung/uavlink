import contextlib
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from py.uavlink import cli, config, detect, paths, runner

REPO = Path(__file__).resolve().parents[1]
FAKE_ENGINE = REPO / "tests" / "fixtures" / "fake-lib" / "engine"


class CliTestBase(unittest.TestCase):
    root = False
    lang = "en_US.UTF-8"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uavlink-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config, self.state, self.run_dir = self.tmp / "config", self.tmp / "state", self.tmp / "run"
        self.config.mkdir()
        patches = [
            mock.patch.object(paths, "ENGINE_DIR", FAKE_ENGINE),
            mock.patch.object(paths, "VENDOR_DIR", self.tmp / "vendor"),
            mock.patch.object(paths, "CONFIG_DIR", self.config),
            mock.patch.object(paths, "STATE_DIR", self.state),
            mock.patch.object(paths, "RUN_DIR", self.run_dir),
            mock.patch.object(detect, "x25_ports", lambda root="/": []),
            mock.patch.object(detect, "is_wsl", lambda root="/": False),
            mock.patch.object(cli, "_is_root", lambda: self.root),
            mock.patch.object(cli, "_confirm_quiet", lambda: True),
            mock.patch.object(cli, "PROC", str(self.tmp / "proc")),     # no session or sshd of the test runner
            mock.patch.dict(os.environ, {"LANG": self.lang, "LC_ALL": ""}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            os.environ.pop(key, None)

    def run_cli(self, *argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def apply_real(self, *extra):
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        return self.run_cli("apply", "--preset", "dgp-current", "--yes", "--props-off", *extra)


class PresetsAndUsageTest(CliTestBase):
    def test_presets_list(self):
        code, out, _ = self.run_cli("presets")
        self.assertEqual(code, 0)
        self.assertIn("dgp-current", out)
        self.assertIn("vps-current", out)

    def test_presets_by_role(self):
        _, out, _ = self.run_cli("presets", "--role", "vps")
        self.assertIn("vps-current", out)
        self.assertNotIn("dgp-current", out)

    def test_version(self):
        code, out, _ = self.run_cli("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "uavlink " + cli.VERSION)

    def test_no_command_and_bad_option_are_usage_errors(self):
        self.assertEqual(self.run_cli()[0], 2)
        self.assertEqual(self.run_cli("apply")[0], 2)
        self.assertEqual(self.run_cli("bogus")[0], 2)

    def test_help_is_bilingual(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("Cấu hình", out)
        self.assertIn("Configure", out)

    def test_unknown_preset_and_bad_set(self):
        code, _, err = self.run_cli("apply", "--preset", "nope", "--preview")
        self.assertEqual(code, 2)
        self.assertIn("nope", err)
        code, _, err = self.run_cli("apply", "--preset", "dgp-current", "--set", "oops", "--preview")
        self.assertEqual(code, 2)
        self.assertIn("oops", err)
        code, _, err = self.run_cli("apply", "--preset", "dgp-current", "--set", "NOT_A_KEY=1", "--preview")
        self.assertEqual(code, 2)
        self.assertIn("NOT_A_KEY", err)

    def test_validation_errors_exit_2(self):
        code, out, err = self.run_cli("apply", "--preset", "dgp-current", "--set", "CM5_LAN_IP=not-an-ip")
        self.assertEqual(code, 2)
        self.assertIn("CM5_LAN_IP", err)
        self.assertEqual(out, "")
        self.assertFalse((self.config / "config.env").exists())

    def test_validation_errors_json(self):
        code, out, _ = self.run_cli("apply", "--preset", "dgp-current", "--set", "X25_NETMASK=99", "--json")
        self.assertEqual(code, 2)
        ev = json.loads(out)
        self.assertEqual(ev["t"], "validation")
        self.assertEqual(ev["errors"]["X25_NETMASK"]["code"], "range")

    def test_setup_and_serve_dispatch_lazily(self):
        launch = mock.Mock(return_value=7)
        with mock.patch("py.uavlink.launch.main", launch):
            self.assertEqual(self.run_cli("setup", "--lan", "--port", "9000")[0], 7)
        launch.assert_called_once_with(["--lan", "--port", "9000"])
        fake_server = types.SimpleNamespace(main=mock.Mock(return_value=0))
        with mock.patch.dict("sys.modules", {"py.uavlink.server": fake_server}):
            self.assertEqual(self.run_cli("serve", "--foo")[0], 0)
        fake_server.main.assert_called_once_with(["--foo"])


class WslGuardTest(CliTestBase):
    def test_apply_refused_on_wsl_before_saving(self):
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        with mock.patch.object(detect, "is_wsl", lambda root="/": True),                 mock.patch.dict(os.environ, {"UAVLINK_ALLOW_WSL": "0"}):
            code, _, err = self.run_cli("apply", "--preset", "dgp-current", "--yes", "--props-off")
        self.assertEqual(code, 4)
        self.assertIn("env.wsl", err)
        self.assertFalse((self.config / "config.env").exists())

    def test_apply_not_root_does_not_save(self):
        code, _, _ = self.run_cli("apply", "--preset", "dgp-current", "--yes", "--props-off")
        self.assertEqual(code, 4)
        self.assertFalse((self.config / "config.env").exists())


class PreviewTest(CliTestBase):
    def test_preview_prints_table_and_removes_temp_dir(self):
        code, out, _ = self.run_cli("apply", "--preset", "dgp-current", "--preview")
        self.assertEqual(code, 0)
        self.assertIn("Will change", out)
        self.assertIn("NET_IPADDR3", out)
        self.assertIn("14 → 1", out)
        self.assertEqual(list(self.run_dir.glob("preview-*")), [])
        self.assertFalse((self.config / "config.env").exists())

    def test_preview_does_not_need_root(self):
        self.assertFalse(self.root)
        self.assertEqual(self.run_cli("apply", "--preset", "dgp-current", "--preview")[0], 0)

    def test_preview_json_prints_raw_events(self):
        code, out, _ = self.run_cli("apply", "--preset", "dgp-current", "--preview", "--json")
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertIn(("stage", "cm5"), [(e["t"], e.get("id")) for e in events])
        self.assertTrue(any(e["t"] == "change" and e["target"] == "NET_IPADDR3" for e in events))

    def test_preview_failure_exit_code(self):
        with mock.patch.dict(os.environ, {"FAKE_CM5_EXIT": "3"}):
            code, out, _ = self.run_cli("apply", "--preset", "dgp-current", "--preview")
        self.assertEqual(code, 1)
        self.assertIn("✖", out)

    def test_preview_refused_exit_code_is_kept(self):
        with mock.patch.dict(os.environ, {"FAKE_CM5_EXIT": "4"}):
            self.assertEqual(self.run_cli("apply", "--preset", "dgp-current", "--preview")[0], 4)


class ApplyTest(CliTestBase):
    def test_non_root_is_refused(self):
        code, _, err = self.run_cli("apply", "--preset", "dgp-current", "--yes", "--props-off")
        self.assertEqual(code, 4)
        self.assertIn("sudo", err)
        self.assertFalse((self.config / "config.env").exists())

    def test_apply_as_root(self):
        code, out, _ = self.apply_real()
        self.assertEqual(code, 0, out)
        self.assertIn("✔ CM5 install", out)
        self.assertIn("✔ Acceptance checks", out)
        self.assertIn("⚠ X25 setup", out)               # no X25 connected: skipped
        self.assertIn("config get MAVLINK_SIGNING_PASSPHRASE", out)
        self.assertIn("192.168.50.2", out)              # GCS info
        values, present = config.load_current()
        self.assertEqual(values["UAVLINK_PRESET"], "dgp-current")
        self.assertTrue(present["MAVLINK_SIGNING_PASSPHRASE"])
        self.assertTrue((self.state / "applied.json").exists())

    def test_apply_json(self):
        code, out, _ = self.apply_real("--json")
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(events[-1], {"t": "end", "state": "ok"})
        self.assertNotIn("✔", out)

    def test_failed_engine(self):
        with mock.patch.dict(os.environ, {"FAKE_CM5_EXIT": "1"}):
            code, out, _ = self.apply_real()
        self.assertEqual(code, 1)
        self.assertIn("✖", out)

    def test_set_overrides_value(self):
        code, _, _ = self.apply_real("--set", "CM5_LAN_IP=192.168.50.9")
        self.assertEqual(code, 0)
        self.assertEqual(config.load_current()[0]["CM5_LAN_IP"], "192.168.50.9")

    def test_asks_for_confirmation_without_yes(self):
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        code, _, err = self.run_cli("apply", "--preset", "dgp-current", "--props-off", stdin="n\n")
        self.assertEqual(code, 1)
        self.assertIn("Cancelled", err)
        self.assertFalse((self.config / "config.env").exists())
        code, out, _ = self.run_cli("apply", "--preset", "dgp-current", "--props-off", stdin="y\n")
        self.assertEqual(code, 0, out)

    def test_asks_about_propellers_when_x25_detected(self):
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        with mock.patch.object(detect, "x25_ports", lambda root="/": ["/dev/ttyACM0"]):
            _, out, err = self.run_cli("apply", "--preset", "dgp-current", "--yes", stdin="n\n")
        self.assertIn("PROPELLERS", err)
        self.assertIn("⚠ X25 setup", out)               # answer no: flight controller untouched

    def test_rollback_hint_over_ssh_and_auto_confirm_on_console(self):
        progress = cli.Progress()
        ev = {"t": "rollback", "state": "armed", "secs": 180}
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "10.0.0.2 5000 10.0.0.1 22"}), \
                contextlib.redirect_stdout(out):
            progress(ev)
        self.assertIn("NEW SSH session", out.getvalue())
        self.assertIn("sudo uavlink confirm", out.getvalue())
        self.assertEqual(progress.rollback_secs, 180)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            progress(ev)
        self.assertEqual(out.getvalue(), "")

    def test_console_apply_confirms_rollback(self):
        job = mock.Mock(state="ok", events=[])
        job.subscribe.side_effect = lambda cb: cb({"t": "rollback", "state": "armed", "secs": 60})
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        with mock.patch.object(runner, "apply", return_value=job), \
                mock.patch.object(cli, "_confirm_quiet", return_value=True) as confirm:
            code, out, _ = self.run_cli("apply", "--preset", "dgp-current", "--yes", "--props-off")
        self.assertEqual(code, 0)
        confirm.assert_called_once()
        self.assertIn("Confirmed automatically", out)

    def _apply_with_rollback(self, state, remote, *extra):
        job = mock.Mock(state=state, events=[])
        job.subscribe.side_effect = lambda cb: cb({"t": "rollback", "state": "armed", "secs": 180})
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        with mock.patch.object(runner, "apply", return_value=job), \
                mock.patch.object(cli, "over_ssh", return_value=remote), \
                mock.patch.object(cli, "_confirm_quiet", return_value=True) as confirm:
            code, out, _ = self.run_cli("apply", "--preset", "dgp-current", "--yes", "--props-off", *extra)
        return code, out, confirm

    def test_apply_over_ssh_under_sudo_leaves_the_rollback_armed(self):
        # `sudo uavlink apply` in an SSH session: SSH_CONNECTION is gone, the session is still remote.
        code, out, confirm = self._apply_with_rollback("ok", True)
        self.assertEqual(code, 0)
        confirm.assert_not_called()
        self.assertGreater(out.rindex("NEW SSH session"), out.index("Apply finished"))   # reminded at the end
        code, out, confirm = self._apply_with_rollback("ok", True, "--json")
        confirm.assert_not_called()

    def test_console_confirms_also_when_a_later_stage_failed(self):
        # The verify stage failing (no X25 yet) says nothing about SSH: the firewall must not roll back silently.
        code, out, confirm = self._apply_with_rollback("failed", False)
        self.assertEqual(code, 1)
        confirm.assert_called_once()
        self.assertIn("✖", out)
        self.assertIn("Confirmed automatically", out)
        code, out, confirm = self._apply_with_rollback("failed", False, "--json")
        confirm.assert_called_once()

    def test_busy(self):
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        with mock.patch.object(runner, "apply", side_effect=runner.Busy("x")):
            code, _, err = self.run_cli("apply", "--preset", "dgp-current", "--yes", "--props-off")
        self.assertEqual(code, 4)
        self.assertIn("Another job", err)


class SshSessionTest(unittest.TestCase):
    """cli.over_ssh() on a fake /proc: environment, logind session of the cgroup, sshd among the parents."""

    def setUp(self):
        self.proc = Path(tempfile.mkdtemp(prefix="uavlink-proc-"))
        self.addCleanup(shutil.rmtree, self.proc, True)
        p = mock.patch.object(cli, "PROC", str(self.proc))
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            os.environ.pop(key, None)

    def stat(self, pid, comm, ppid):
        d = self.proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "stat").write_text(f"{pid if pid != 'self' else 400} ({comm}) S {ppid} 1 1 0 -1 4194560 0\n")

    def cgroup(self, path):
        (self.proc / "self").mkdir(exist_ok=True)
        (self.proc / "self" / "cgroup").write_text(f"0::{path}\n")

    def logind(self, out):
        return mock.patch.object(cli.subprocess, "run", return_value=types.SimpleNamespace(stdout=out, returncode=0))

    def test_environment(self):
        self.assertFalse(cli.over_ssh())
        os.environ["SSH_CONNECTION"] = "10.0.2.2 5000 10.0.2.100 22"
        self.assertTrue(cli.over_ssh())

    def test_sshd_parent_under_sudo(self):
        # python3 <- sudo <- sudo <- bash <- sshd: pi@notty <- sshd (the listener) <- systemd
        self.stat("self", "python3", 30)
        self.stat(30, "sudo", 20)
        self.stat(20, "sudo", 15)
        self.stat(15, "bash", 10)
        self.stat(10, "sshd", 5)
        self.stat(5, "sshd", 1)
        self.assertTrue(cli.over_ssh())
        self.stat(10, "sshd-session", 5)          # OpenSSH 9.8 and later
        self.assertTrue(cli.over_ssh())

    def test_logind_remote_session(self):
        self.cgroup("/user.slice/user-1000.slice/session-7.scope")
        with self.logind("Remote=yes\nService=sshd\n") as run:
            self.assertTrue(cli.over_ssh())
        self.assertEqual(run.call_args[0][0][:3], ["loginctl", "show-session", "7"])
        with self.logind("Remote=no\nService=sshd\n"):
            self.assertTrue(cli.over_ssh())

    def test_local_console(self):
        self.cgroup("/user.slice/user-1000.slice/session-2.scope")
        self.stat("self", "python3", 30)
        self.stat(30, "sudo", 20)
        self.stat(20, "bash", 10)
        self.stat(10, "login", 1)
        with self.logind("Remote=no\nService=login\n"):
            self.assertFalse(cli.over_ssh())
        self.cgroup("/system.slice/run-u12.service")      # systemd-run, the setup server: no session at all
        with self.logind(""):
            self.assertFalse(cli.over_ssh())

    def test_odd_command_names_and_missing_parents(self):
        self.stat("self", "my (odd) prog", 30)
        self.stat(30, "sshd", 1)
        self.assertTrue(cli.over_ssh())
        self.stat(30, "x) S 1 (sshd", 99)          # a name made to look like a parent: not an sshd, parent 99 is gone
        self.assertFalse(cli.over_ssh())


class ConfigCommandsTest(CliTestBase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.apply_real()[0], 0)
        CliTestBase.root = False
        self.passphrase = config.read_env(str(self.config / "secrets.env"))["MAVLINK_SIGNING_PASSPHRASE"]

    def test_show_masks_secrets(self):
        code, out, _ = self.run_cli("config", "show")
        self.assertEqual(code, 0)
        self.assertIn("CM5_LAN_IP=192.168.50.2", out)
        self.assertIn("MAVLINK_SIGNING_PASSPHRASE=********", out)
        self.assertNotIn(self.passphrase, out)

    def test_get_plain_and_secret(self):
        self.assertEqual(self.run_cli("config", "get", "CM5_LAN_IP")[1].strip(), "192.168.50.2")
        code, out, _ = self.run_cli("config", "get", "MAVLINK_SIGNING_PASSPHRASE")
        self.assertEqual((code, out), (4, ""))
        CliTestBase.root = True
        code, out, _ = self.run_cli("config", "get", "MAVLINK_SIGNING_PASSPHRASE")
        self.assertEqual((code, out.strip()), (0, self.passphrase))
        self.assertEqual(self.run_cli("config", "get", "NOPE")[0], 1)

    def test_set_validates_and_saves_without_applying(self):
        code, _, _ = self.run_cli("config", "set", "CM5_LAN_IP=192.168.50.7", "VPS_UPLINK_PORT=14600")
        self.assertEqual(code, 0)
        values = config.load_current()[0]
        self.assertEqual((values["CM5_LAN_IP"], values["VPS_UPLINK_PORT"]), ("192.168.50.7", "14600"))
        self.assertEqual(values["UAVLINK_PRESET"], "dgp-current")
        code, _, err = self.run_cli("config", "set", "VPS_UPLINK_PORT=99999")
        self.assertEqual(code, 2)
        self.assertIn("VPS_UPLINK_PORT", err)
        self.assertEqual(config.load_current()[0]["VPS_UPLINK_PORT"], "14600")
        self.assertEqual(self.run_cli("config", "set", "garbage")[0], 2)

    def test_set_secret_needs_root(self):
        self.assertEqual(self.run_cli("config", "set", "MAVLINK_SIGNING_PASSPHRASE=abcdefghijkl1234")[0], 4)
        CliTestBase.root = True
        self.assertEqual(self.run_cli("config", "set", "MAVLINK_SIGNING_PASSPHRASE=abcdefghijkl1234")[0], 0)
        self.assertEqual(config.read_env(str(self.config / "secrets.env"))["MAVLINK_SIGNING_PASSPHRASE"],
                         "abcdefghijkl1234")

    def test_set_without_config_fails(self):
        for name in ("config.env", "secrets.env"):
            (self.config / name).unlink()
        code, _, err = self.run_cli("config", "set", "CM5_LAN_IP=192.168.50.7")
        self.assertEqual(code, 1)
        self.assertIn("UAVLINK_ROLE", err)


class StatusAndMiscTest(CliTestBase):
    def test_status(self):
        self.state.mkdir()
        (self.state / "applied.json").write_text(json.dumps(
            {"preset": "dgp-current", "role": "cm5", "time": "2026-10-24T10:00:00Z", "version": "0.1.0"}))
        with mock.patch.object(cli, "_unit_state", lambda unit: "active"):
            code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("uavlink-router.service", out)
        self.assertIn("uavlink-rollback.timer", out)
        self.assertIn("2026-10-24T10:00:00Z", out)
        self.assertIn("none", out)                       # no verify log yet

    def test_verify(self):
        with mock.patch.object(detect, "role_guess", lambda root="/": "cm5"):
            code, out, _ = self.run_cli("verify")
        self.assertEqual(code, 0)
        self.assertIn("✔ Acceptance checks", out)

    def test_confirm(self):
        self.assertEqual(self.run_cli("confirm")[0], 4)
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        code, out, _ = self.run_cli("confirm")
        self.assertEqual(code, 0)
        self.assertIn("Confirmed", out)

    def test_x25_backups_and_restore(self):
        code, out, _ = self.run_cli("x25", "backups")
        self.assertIn("No X25 backups", out)
        folder = self.state / "x25-backups"
        folder.mkdir(parents=True)
        (folder / "x25_params_1.parm").write_text("A 1\n")
        self.assertIn("x25_params_1.parm", self.run_cli("x25", "backups")[1])
        self.assertEqual(self.run_cli("x25", "restore", "missing.parm")[0], 1)
        self.assertEqual(self.run_cli("x25", "restore", "x25_params_1.parm", "--props-off")[0], 4)
        CliTestBase.root = True
        self.addCleanup(setattr, CliTestBase, "root", False)
        self.assertEqual(self.run_cli("x25", "restore", "x25_params_1.parm", "--props-off")[0], 0)
        self.assertEqual(self.run_cli("x25", "restore", "x25_params_1.parm", stdin="n\n")[0], 1)

    def test_uninstall(self):
        self.assertEqual(self.run_cli("uninstall")[0], 4)
        code, out, _ = self.run_cli("uninstall", "--preview")
        self.assertEqual(code, 0)
        self.assertIn("Will change", out)


class VietnameseTest(CliTestBase):
    lang = "vi_VN.UTF-8"

    def test_language_selection(self):
        self.assertEqual(cli.lang(), "vi")
        with mock.patch.dict(os.environ, {"LC_ALL": "C.UTF-8"}):
            self.assertEqual(cli.lang(), "en")
        with mock.patch.dict(os.environ, {"LC_ALL": "", "LANG": ""}):
            self.assertEqual(cli.lang(), "en")

    def test_preview_in_vietnamese(self):
        code, out, _ = self.run_cli("apply", "--preset", "dgp-current", "--preview")
        self.assertEqual(code, 0)
        self.assertIn("Sẽ thay đổi", out)
        self.assertIn("Cài đặt CM5", out)

    def test_engine_codes_translated_and_fall_back_to_msg(self):
        self.assertIn("X25", cli.step_name("eth"))
        self.assertEqual(cli.engine_text("no.such.code", {}, "english fallback"), "english fallback")
        self.assertEqual(cli.engine_text("no.such.code"), "no.such.code")
        self.assertEqual(cli.engine_text("lan.link.ok", {}, "no args"), "no args")   # missing placeholder: use msg
        self.assertEqual(cli.engine_text("lan.link.ok", {"iface": "eth0"}).count("eth0"), 1)

    def test_errors_in_vietnamese(self):
        _, _, err = self.run_cli("apply", "--preset", "dgp-current", "--set", "X25_NETMASK=99")
        self.assertIn("Cấu hình không hợp lệ", err)

    def test_english_when_not_vietnamese(self):
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}):
            self.assertEqual(cli.engine_text("lan.link.ok", {"iface": "eth0"}),
                             cli._engine_messages()["lan.link.ok"]["en"].format(iface="eth0"))


if __name__ == "__main__":
    unittest.main()
