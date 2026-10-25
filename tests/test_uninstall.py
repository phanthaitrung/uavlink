import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "engine" / "cm5" / "uninstall-cm5.sh"
SHIMS = ["systemctl", "ufw", "nmcli", "userdel", "udevadm", "getent", "id", "gpasswd", "netplan"]
SHIM = """#!/bin/sh
echo "$(basename "$0") $*" >> "$SHIM_LOG"
case "$(basename "$0") $1" in
  "systemctl list-unit-files") echo "$2 enabled" ;;
  "ufw status") echo "${SHIM_UFW_STATUS:-}" ;;
  "id -nG") echo "${SHIM_GROUPS:-}" ;;
  "nmcli -t") if [ "$2 $3" = "general status" ] && [ -n "${SHIM_NM_DOWN:-}" ]; then exit 1; fi ;;
  "udevadm control") if [ -n "${SHIM_UDEV_FAIL:-}" ]; then exit 1; fi ;;
esac
exit 0
"""


class UninstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.backup = self.state / "backup"
        self.backup.mkdir(parents=True)
        self.etc = self.tmp / "etc"
        self.etc.mkdir()
        self.ufw_dir = self.tmp / "ufw"
        self.ufw_dir.mkdir()
        self.nm_dir = self.tmp / "nm"
        self.nm_dir.mkdir()
        self.log = self.tmp / "calls.log"
        shims = self.tmp / "shims"
        shims.mkdir()
        for name in SHIMS:
            sh = shims / name
            sh.write_text(SHIM, newline="\n")
            sh.chmod(0o755)
        self.shims = shims
        self.extra = {}

    def env(self):
        env = dict(os.environ, PATH=f"{self.shims}:{os.environ['PATH']}", SHIM_LOG=str(self.log),
                   UAVLINK_STATE_DIR=str(self.state), UAVLINK_UFW_DIR=str(self.ufw_dir),
                   UAVLINK_NM_DIR=str(self.nm_dir), UAVLINK_ALLOW_WSL="1", UAVLINK_EVENTS="1",
                   UAVLINK_SSH_DIR=str(self.tmp / "ssh"), UAVLINK_UDEV_CONTROL=str(self.tmp / "no-udevd"),
                   UAVLINK_NM_WAIT="2")
        env.update(self.extra)
        return env

    def run_script(self, *args, script=SCRIPT):
        return subprocess.run(["bash", str(script), *args], env=self.env(), capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def events(self, proc):
        return [json.loads(line[len("@@UAVLINK "):]) for line in proc.stdout.splitlines()
                if line.startswith("@@UAVLINK ")]

    def manifest(self, *rows):
        (self.state / "manifest.tsv").write_text("".join("\t".join(r) + "\n" for r in rows), newline="\n")

    def fixture(self):
        created = self.etc / "created.conf"
        created.write_text("new")
        changed = self.etc / "changed.conf"
        changed.write_text("new")
        (self.backup / "changed.conf").write_text("original")
        ufw_bk = self.backup / "ufw"
        ufw_bk.mkdir()
        (ufw_bk / "user.rules").write_text("old rules")
        (self.ufw_dir / "user.rules").write_text("new rules")
        self.manifest(
            ("file", str(created), "absent", "-"),
            ("file", str(changed), "present", str(self.backup / "changed.conf")),
            # later lines for the same target must be ignored
            ("file", str(created), "present", str(self.backup / "nothing")),
            ("service", "mavlink-router.service", "enabled-inactive", "-"),
            ("service", "other.service", "disabled-active", "-"),
            ("ufw", "ufw", "active", str(ufw_bk)),
            ("nm", "x25-link", "absent", "-"),
            ("user", "mavlink-router", "absent", "-"),
        )
        return created, changed

    def test_restores_everything(self):
        created, changed = self.fixture()
        self.extra["SHIM_UFW_STATUS"] = "Status: active"
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(created.exists())
        self.assertEqual(changed.read_text(), "original")
        self.assertEqual((self.ufw_dir / "user.rules").read_text(), "old rules")
        calls = self.calls()
        for expected in ["systemctl enable mavlink-router.service", "systemctl stop mavlink-router.service",
                         "systemctl disable other.service", "systemctl start other.service",
                         "systemctl stop uavlink-router.service", "systemctl disable uavlink-router.service",
                         "systemctl stop uavlink-rollback.timer", "systemctl disable uavlink-rollback.timer",
                         "ufw reload", "nmcli connection delete x25-link", "userdel mavlink-router",
                         "systemctl daemon-reload", "udevadm control --reload"]:
            self.assertIn(expected, calls)
        self.assertNotIn("ufw --force disable", calls)
        self.assertNotIn("systemctl reload ssh.service", calls)     # no SSH file was touched
        self.assertLess(calls.index("userdel mavlink-router"), calls.index("systemctl daemon-reload"))
        self.assertTrue((self.state / "manifest.tsv").exists())
        self.assertEqual(self.events(p)[-1], {"t": "result", "ok": True})

    def test_ufw_prior_inactive(self):
        self.manifest(("ufw", "ufw", "inactive", "-"))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn("ufw --force disable", self.calls())
        self.assertNotIn("ufw reload", self.calls())

    def test_nm_and_service_prior_state(self):
        (self.backup / "wifi.nmconnection").write_text("[connection]\n")
        self.manifest(("nm", "wifi", "present", str(self.backup / "wifi.nmconnection")),
                      ("service", "mavlink-router.service", "enabled-active", "-"))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual((self.nm_dir / "wifi.nmconnection").read_text(), "[connection]\n")
        self.assertIn("nmcli connection reload", self.calls())
        self.assertIn("systemctl enable mavlink-router.service", self.calls())
        self.assertIn("systemctl start mavlink-router.service", self.calls())

    def test_preview_changes_nothing(self):
        created, changed = self.fixture()
        p = self.run_script("--preview", "--purge")
        self.assertEqual(p.returncode, 0, p.stderr)
        # only read-only lookups are allowed
        lookups = ("nmcli connection show", "nmcli -t general status", "nmcli -t -f NAME connection show", "getent ",
                   "ufw status", "id -nG")
        self.assertEqual([c for c in self.calls() if not c.startswith(lookups)], [])
        self.assertTrue(created.exists())
        self.assertEqual(changed.read_text(), "new")
        self.assertEqual((self.ufw_dir / "user.rules").read_text(), "new rules")
        self.assertTrue((self.state / "manifest.tsv").exists())
        changes = {(e["kind"], e["target"], e["action"]) for e in self.events(p) if e["t"] == "change"}
        self.assertIn(("file", str(created), "delete"), changes)
        self.assertIn(("file", str(changed), "update"), changes)
        self.assertIn(("service", "mavlink-router.service", "enable"), changes)
        self.assertIn(("firewall", "ufw", "update"), changes)
        self.assertIn(("nm", "x25-link", "delete"), changes)
        self.assertIn(("user", "mavlink-router", "delete"), changes)
        self.assertIn(("file", str(self.state / "manifest.tsv"), "delete"), changes)

    def test_idempotent(self):
        created, changed = self.fixture()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertFalse(created.exists())
        self.assertEqual(changed.read_text(), "original")

    def test_purge_deletes_backups_and_manifest(self):
        self.fixture()
        self.assertEqual(self.run_script("--purge").returncode, 0)
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.state / "manifest.tsv").exists())
        self.assertEqual(self.run_script("--purge").returncode, 0)

    def test_missing_manifest_is_nothing_to_do(self):
        p = self.run_script()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_missing_backup_is_reported(self):
        target = self.etc / "x.conf"
        target.write_text("new")
        self.manifest(("file", str(target), "present", str(self.backup / "gone")))
        self.assertEqual(self.run_script().returncode, 1)
        self.assertEqual(target.read_text(), "new")

    def test_group_membership_removed_only_when_still_there(self):
        self.manifest(("group", "pi:dialout", "absent", "-"))
        self.extra["SHIM_GROUPS"] = "pi sudo dialout"
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn("gpasswd -d pi dialout", self.calls())
        self.log.unlink()
        self.extra["SHIM_GROUPS"] = "pi sudo"
        self.assertEqual(self.run_script().returncode, 0)
        self.assertNotIn("gpasswd -d pi dialout", self.calls())

    def test_created_directories_removed_only_when_empty(self):
        dropin = self.etc / "uavlink-router.service.d"
        dropin.mkdir()
        conf = dropin / "10-restart.conf"
        conf.write_text("x")
        logs = self.etc / "mavlink-router-logs"
        logs.mkdir()
        (logs / "flight.tlog").write_text("keep me")
        self.manifest(("file", str(dropin), "absent", "-"), ("file", str(conf), "absent", "-"),
                      ("file", str(logs), "absent", "-"))
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(dropin.exists())
        self.assertEqual((logs / "flight.tlog").read_text(), "keep me")

    def test_ufw_prior_inactive_puts_the_rule_files_back(self):
        bk = self.backup / "ufw"
        bk.mkdir()
        (bk / "user.rules").write_text("original rules")
        (bk / "ufw.conf").write_text("ENABLED=no\n")
        (self.ufw_dir / "user.rules").write_text("uavlink rules")
        (self.ufw_dir / "ufw.conf").write_text("ENABLED=yes\n")
        self.manifest(("ufw", "ufw", "inactive", str(bk)))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual((self.ufw_dir / "user.rules").read_text(), "original rules")
        self.assertEqual((self.ufw_dir / "ufw.conf").read_text(), "ENABLED=no\n")
        self.assertIn("ufw --force disable", self.calls())

    def test_ufw_prior_active_enabled_again_after_a_rollback(self):
        bk = self.backup / "ufw"
        bk.mkdir()
        (bk / "user.rules").write_text("original rules")
        self.manifest(("ufw", "ufw", "active", str(bk)))
        self.extra["SHIM_UFW_STATUS"] = "Status: inactive"
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn("ufw --force enable", self.calls())
        self.assertNotIn("ufw reload", self.calls())

    def test_nm_connection_kept_as_a_netplan_file(self):
        # Ubuntu 24.04: NetworkManager stores the connection as /etc/netplan/90-NM-<uuid>.yaml.
        yaml = self.etc / "90-NM-cd80.yaml"
        yaml.write_text("changed by uavlink")
        (self.backup / "netplan.yaml").write_text("deploy.sh settings")
        self.manifest(("file", str(yaml), "present", str(self.backup / "netplan.yaml")),
                      ("nm", "x25-link", "present", "-"))
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(yaml.read_text(), "deploy.sh settings")
        calls = self.calls()
        self.assertLess(calls.index("netplan generate"), calls.index("nmcli connection reload"))

    def test_networkmanager_not_answering_is_a_failure(self):
        # Never conclude "the connection is already gone" from a NetworkManager that is down.
        self.manifest(("nm", "x25-link", "absent", "-"))
        self.extra.update(SHIM_NM_DOWN="1", UAVLINK_NM_WAIT="1")
        p = self.run_script()
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("nmcli connection delete x25-link", self.calls())

    def test_udev_reload_failure_matters_only_with_udevd(self):
        self.manifest(("file", str(self.etc / "gone.rules"), "absent", "-"))
        self.extra["SHIM_UDEV_FAIL"] = "1"
        self.assertEqual(self.run_script().returncode, 0)        # a container: no udevd socket
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.tmp / "udev-control"))
        self.extra["UAVLINK_UDEV_CONTROL"] = str(self.tmp / "udev-control")
        self.assertEqual(self.run_script().returncode, 1)

    def test_sshd_reloaded_after_its_files_are_restored(self):
        conf = self.tmp / "ssh" / "sshd_config.d" / "10-uavlink-hardening.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("PasswordAuthentication no\n")
        self.manifest(("file", str(conf), "absent", "-"))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertFalse(conf.exists())
        self.assertIn("systemctl reload ssh.service", self.calls())

    def test_usage_error(self):
        self.assertEqual(self.run_script("--bogus").returncode, 2)

    def test_works_without_events_sh(self):
        lone = self.tmp / "lone"
        lone.mkdir()
        copy = lone / "uninstall.sh"
        copy.write_text(SCRIPT.read_text(), newline="\n")
        created, _ = self.fixture()
        p = self.run_script(script=copy)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(created.exists())
        self.assertNotIn("@@UAVLINK", p.stdout)


if __name__ == "__main__":
    unittest.main()
