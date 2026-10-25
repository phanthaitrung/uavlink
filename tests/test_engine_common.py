"""Manifest helpers of engine/lib/common.sh (ARCHITECTURE.md section 4.2), with systemctl/nmcli shims on PATH."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "engine" / "lib" / "common.sh"
EVENTS = ROOT / "engine" / "lib" / "events.sh"

SHIMS = {
    # systemctl show prints the properties of $SHIM_UNIT_STATE ("LoadState=… UnitFileState=… ActiveState=…"),
    # or fails like a systemd that is reexecuting when SHIM_SYSTEMD_DOWN is set.
    "systemctl": 'if [ -n "$SHIM_SYSTEMD_DOWN" ]; then echo "Failed to connect to bus: Transport endpoint is not '
                 'connected" >&2; exit 1; fi\n'
                 'case "$1" in show) for kv in $SHIM_UNIT_STATE; do echo "$kv"; done ;; esac',
    "nmcli": 'case "$*" in\n'
             '  "-t -f NAME connection show") echo x25-link ;;\n'
             '  "-g connection.uuid connection show x25-link") echo cd80-uuid ;;\n'
             '  "-t -f UUID,FILENAME connection show") echo "lo-uuid:/run/NetworkManager/system-connections/lo.nmconnection";'
             ' echo "cd80-uuid:$SHIM_NM_FILE" ;;\n'
             'esac',
}


class CommonTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        shims = self.tmp / "shims"
        shims.mkdir()
        for name, body in SHIMS.items():
            (shims / name).write_text(f"#!/bin/sh\n{body}\n", newline="\n")
            (shims / name).chmod(0o755)
        self.env = dict(os.environ, PATH=f"{shims}:{os.environ['PATH']}", UAVLINK_STATE_DIR=str(self.state),
                        UAVLINK_EVENTS="1", UAVLINK_SYSTEMD_WAIT="1", UAVLINK_NM_DIR=str(self.tmp / "nm"),
                        UAVLINK_NETPLAN_DIR=str(self.tmp / "netplan"))

    def sh(self, script, **env):
        prog = f'set -euo pipefail\n. "{EVENTS}"\n. "{COMMON}"\nCUR_STEP=test\n{script}\n'
        return subprocess.run(["bash", "-c", prog], env=dict(self.env, **env), capture_output=True, text=True,
                              timeout=60)

    def manifest(self):
        f = self.state / "manifest.tsv"
        return [line.split("\t") for line in f.read_text().splitlines()] if f.exists() else []

    def test_service_state_read_from_systemd(self):
        cases = [("LoadState=loaded UnitFileState=enabled ActiveState=active", "enabled-active"),
                 ("LoadState=loaded UnitFileState=enabled ActiveState=inactive", "enabled-inactive"),
                 ("LoadState=loaded UnitFileState=disabled ActiveState=active", "disabled-active"),
                 ("LoadState=loaded UnitFileState=static ActiveState=inactive", "disabled-inactive"),
                 ("LoadState=masked UnitFileState=masked ActiveState=inactive", "masked"),
                 ("LoadState=not-found UnitFileState= ActiveState=inactive", "absent")]
        for i, (state, want) in enumerate(cases):
            p = self.sh(f"record_service u{i}.service", SHIM_UNIT_STATE=state)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.manifest()[i], ["service", f"u{i}.service", want, "-"])

    @unittest.skipUnless(os.path.isdir("/run/systemd/system"), "systemd_ready only waits where systemd runs")
    def test_unreachable_systemd_records_nothing(self):
        # Seen during an unattended upgrade of systemd: guessing "absent" made uninstall disable the service.
        p = self.sh("record_service unattended-upgrades.service", SHIM_SYSTEMD_DOWN="1")
        self.assertEqual(p.returncode, 10)
        self.assertIn('"code":"systemd.unreachable"', p.stdout)
        self.assertEqual(self.manifest(), [])

    def test_group_and_directories(self):
        base = self.tmp / "var" / "log"
        base.mkdir(parents=True)
        p = self.sh(f'record_group pi dialout; record_group pi dialout\nmake_dir "{base}/mavlink-router/x" -m 750\n'
                    f'make_dir "{base}/mavlink-router/x"')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.manifest(), [["group", "pi:dialout", "absent", "-"],
                                           ["file", f"{base}/mavlink-router", "absent", "-"],
                                           ["file", f"{base}/mavlink-router/x", "absent", "-"]])
        self.assertEqual(oct((base / "mavlink-router" / "x").stat().st_mode & 0o777), "0o750")

    def test_existing_connection_as_netplan_file(self):
        netplan = self.tmp / "netplan"
        netplan.mkdir()
        (netplan / "90-NM-cd80-uuid.yaml").write_text("deploy.sh settings")
        p = self.sh("record_nm x25-link",
                    SHIM_NM_FILE="/run/NetworkManager/system-connections/netplan-NM-cd80-uuid.nmconnection")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = self.manifest()
        self.assertEqual(rows[0][:3], ["file", str(netplan / "90-NM-cd80-uuid.yaml"), "present"])
        self.assertEqual(Path(rows[0][3]).read_text(), "deploy.sh settings")
        self.assertEqual(rows[1], ["nm", "x25-link", "present", "-"])

    def test_existing_connection_as_keyfile(self):
        nm = self.tmp / "nm"
        nm.mkdir()
        (nm / "x25-link.nmconnection").write_text("[connection]\n")
        p = self.sh("record_nm x25-link", SHIM_NM_FILE=str(nm / "x25-link.nmconnection"))
        self.assertEqual(p.returncode, 0, p.stderr)
        (row,) = self.manifest()
        self.assertEqual(row[:3], ["nm", "x25-link", "present"])
        self.assertEqual(Path(row[3]).read_text(), "[connection]\n")


if __name__ == "__main__":
    unittest.main()
