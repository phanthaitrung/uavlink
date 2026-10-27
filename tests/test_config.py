import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from py.uavlink import config, presets

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy-config.env"


class ReadEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, text):
        p = self.tmp / "x.env"
        p.write_text(text, encoding="utf-8")
        return p

    def test_legacy_file(self):
        d = config.read_env(FIXTURE)
        self.assertEqual(d["CM5_LAN_IP"], "192.168.50.2")                      # inline comment stripped
        self.assertEqual(d["CM5_ETH_EXTRA_ADDRS"], "192.168.144.2/24")         # double quotes
        self.assertEqual(d["CM5_EXTRA_UDP_TARGETS"], "ground_station_office=100.70.184.3:14550")
        self.assertEqual(d["MAV_VENV"], "/home/$CM5_USER/mavvenv")             # never expanded
        self.assertEqual(d["X25_BACKUP_DIR"], "~/x25-backups")
        self.assertEqual(d["LETSENCRYPT_EMAIL"], "")
        self.assertEqual(d["MAVLINK_SIGNING_PASSPHRASE"], "")
        self.assertEqual(d["VPS_GCS_ALLOW_FROM"], "any")
        self.assertEqual(d["WEB_PIP_PACKAGES"], "fastapi==0.158.2 uvicorn==0.54.0 websockets==17.3 pymavlink==2.4.50")
        self.assertNotIn("#", "".join(d))

    def test_missing_file(self):
        self.assertEqual(config.read_env(self.tmp / "nope.env"), {})

    def test_formats(self):
        p = self.write("# c\n\nA=1\nexport B='x y'  # note\nC=\"q\\\"r\\$s\"\nD=a\\ b\nE='it'\\''s'\r\nF=#lit\nbad line\nG=\"$(touch pwned)\"\n")
        d = config.read_env(p)
        self.assertEqual(d, {"A": "1", "B": "x y", "C": 'q"r$s', "D": "a b", "E": "it's", "F": "#lit", "G": "$(touch pwned)"})
        self.assertFalse((self.tmp / "pwned").exists())

    def test_multiline_single_quotes(self):
        p = self.write("A='one\ntwo'\nB=2\n")
        self.assertEqual(config.read_env(p), {"A": "one\ntwo", "B": "2"})


class WriteEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_round_trip(self):
        values = {"A": "plain", "B": "with space", "C": "it's", "D": "$HOME `x` \"q\" \\ #", "E": "", "F": "a'b'c"}
        p = self.tmp / "c.env"
        config.write_env(p, values, 0o644)
        self.assertEqual(config.read_env(p), values)

    def test_engine_can_read(self):
        values = {"A": "with space", "B": "it's $HOME", "C": "`id` \"q\""}
        p = self.tmp / "c.env"
        config.write_env(p, values, 0o644)
        for k, v in values.items():
            out = subprocess.run(["bash", "-c", 'set -a; . "$1"; printf %s "${!2}"', "x", str(p), k],
                                 capture_output=True, text=True, check=True).stdout
            self.assertEqual(out, v)

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX modes")
    def test_mode_and_atomic(self):
        p = self.tmp / "s.env"
        config.write_env(p, {"A": "1"}, 0o600)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        config.write_env(p, {"A": "2"}, 0o644)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o644)
        self.assertEqual(config.read_env(p), {"A": "2"})
        self.assertEqual([x.name for x in self.tmp.iterdir()], ["s.env"])      # no temp file left

    def test_bad_key(self):
        with self.assertRaises(ValueError):
            config.write_env(self.tmp / "c.env", {"A B": "1"}, 0o644)
        self.assertEqual(list(self.tmp.iterdir()), [])


class SchemaTest(unittest.TestCase):
    def test_defaults_roles(self):
        cm5, vps = config.defaults("cm5"), config.defaults("vps")
        self.assertEqual(cm5["CM5_LAN_IP"], "192.168.50.2")
        self.assertNotIn("VPS_DOMAIN", cm5)
        self.assertIn("VPS_DOMAIN", vps)
        self.assertNotIn("CM5_LAN_IP", vps)
        self.assertIn("SSH_HARDEN", cm5)
        self.assertIn("SSH_HARDEN", vps)

    def test_defaults_match_legacy_config(self):
        legacy = config.read_env(FIXTURE)
        # Since 0.1.3 Tailscale is off by default (the VPS carries MAVLink), unlike the hand-built setup.
        differ = ("X25_BACKUP_DIR", "CM5_USER", "MAVLINK_SIGNING_PASSPHRASE", "CM5_INSTALL_TAILSCALE", "CM5_EXTRA_UDP_TARGETS")
        for role in ("cm5", "vps"):
            for k, v in config.defaults(role).items():
                if k in legacy and k not in differ:
                    self.assertEqual(v, legacy[k], k)

    def test_vps_presets_leave_tailscale_off(self):
        for p in presets.load_presets("cm5"):
            values = presets.resolve("cm5", p["id"], {})
            if p.get("values", {}).get("VPS_PUBLIC_IP") or "VPS_PUBLIC_IP" in p.get("ask", []):
                self.assertEqual(values["CM5_INSTALL_TAILSCALE"], "0", p["id"])
                self.assertEqual(values["CM5_EXTRA_UDP_TARGETS"], "", p["id"])

    def test_secret_keys(self):
        self.assertEqual(set(config.secret_keys()),
                         {"MAVLINK_SIGNING_PASSPHRASE", "WEB_BASIC_AUTH_PASS", "TS_AUTHKEY", "CM5_WIFI_PSK"})

    def test_schema_shape(self):
        schema = config.load_schema()
        tabs = {t["id"] for t in schema["tabs"]}
        for name, spec in schema["keys"].items():
            self.assertIn(spec["tab"], tabs, name)
            for t in spec.get("role_tabs", {}).values():
                self.assertIn(t, tabs, name)
            self.assertIn(spec["type"], {"string", "int", "port", "bool", "enum", "ipv4", "host", "iface", "path",
                                         "cidr_list", "udp_targets", "source_list", "passphrase"}, name)
            for f in ("roles", "default", "label", "help", "advanced", "secret"):
                self.assertIn(f, spec, name)
            if spec["type"] == "enum":
                self.assertIn(spec["default"], [o["value"] for o in spec["options"]], name)
            for dep in spec.get("show_if", {}):
                self.assertIn(dep, schema["keys"], name)
            self.assertEqual(spec["required"], not spec.get("allow_empty", False), name)


class ValidateTest(unittest.TestCase):
    def cm5(self, **over):
        v = config.defaults("cm5")
        v.update(over)
        return config.validate("cm5", v)

    def test_defaults_valid(self):
        self.assertEqual(self.cm5(), {})
        self.assertEqual(config.validate("vps", config.defaults("vps")), {})

    def test_required(self):
        self.assertEqual(self.cm5(CM5_LAN_IP="")["CM5_LAN_IP"]["code"], "required")
        self.assertEqual(self.cm5(VPS_PUBLIC_IP=""), {})                       # allow_empty

    def test_types(self):
        for key, bad, code in [
            ("CM5_LAN_IP", "192.168.50", "type.ipv4"), ("CM5_LAN_IP", "300.1.1.1", "type.ipv4"),
            ("CM5_TCP_SERVER_PORT", "abc", "type.port"), ("MAVLINK_SIGNING", "yes", "type.bool"),
            ("CM5_ETH_IFACE", "bad name", "type.iface"), ("X25_BACKUP_DIR", "rel/path", "type.path"),
            ("CM5_ETH_EXTRA_ADDRS", "192.168.144.2", "type.cidr_list"),
            ("CM5_EXTRA_UDP_TARGETS", "a=1.2.3.4", "type.udp_targets"),
            ("CM5_EXTRA_UDP_TARGETS", "a=1.2.3.4:70000", "type.udp_targets"),
            ("VPS_PUBLIC_IP", "bad_host!", "type.host"), ("VPS_PUBLIC_IP", "1.2.3.400", "type.host"),
        ]:
            self.assertEqual(self.cm5(**{key: bad}).get(key, {}).get("code"), code, (key, bad))

    def test_valid_forms(self):
        self.assertEqual(self.cm5(VPS_PUBLIC_IP="mav.example.com", CM5_EXTRA_UDP_TARGETS="a=1.2.3.4:1 b=5.6.7.8:2",
                                  CM5_ETH_EXTRA_ADDRS="10.0.0.1/8 192.168.1.1/24"), {})

    def test_range(self):
        for port in ("0", "65536", "-1"):
            e = self.cm5(CM5_TCP_SERVER_PORT=port)["CM5_TCP_SERVER_PORT"]
            self.assertEqual((e["code"], e["args"]), ("range", {"min": 1, "max": 65535}))
        self.assertEqual(self.cm5(CM5_TCP_SERVER_PORT="65535"), {})
        self.assertEqual(self.cm5(X25_NETMASK="31")["X25_NETMASK"]["code"], "range")

    def test_enum(self):
        e = self.cm5(CM5_ROUTER_LOG_MODE="sometimes")["CM5_ROUTER_LOG_MODE"]
        self.assertEqual(e["code"], "enum")
        self.assertEqual(e["args"]["options"], ["while-armed", "always"])

    def test_conflict(self):
        e = self.cm5(CM5_LAN_IP="192.168.50.1")
        self.assertEqual(e["X25_IP"], {"code": "conflict", "args": {"other": "CM5_LAN_IP"}})
        self.assertEqual(e["CM5_LAN_IP"]["code"], "conflict")
        e = self.cm5(CM5_LAN_IP="192.168.60.2")
        self.assertEqual(e["CM5_LAN_IP"]["args"], {"other": "X25_IP"})
        self.assertEqual(self.cm5(CM5_LAN_IP="192.168.144.2", X25_IP="192.168.144.14"), {})
        self.assertEqual(self.cm5(CM5_LAN_IP="10.0.0.2", X25_IP="10.0.1.1", X25_NETMASK="16"), {})

    def test_passphrase(self):
        self.assertEqual(self.cm5(MAVLINK_SIGNING_PASSPHRASE="short")["MAVLINK_SIGNING_PASSPHRASE"]["code"], "type.passphrase")
        self.assertEqual(self.cm5(MAVLINK_SIGNING_PASSPHRASE="has space 123456")["MAVLINK_SIGNING_PASSPHRASE"]["code"],
                         "type.passphrase")
        self.assertEqual(self.cm5(MAVLINK_SIGNING_PASSPHRASE="abcdEFGH.12_-"), {})
        # hidden when signing is off
        self.assertEqual(self.cm5(MAVLINK_SIGNING="0", MAVLINK_SIGNING_PASSPHRASE="short"), {})

    def test_vps(self):
        v = config.defaults("vps")
        v["VPS_GCS_ALLOW_FROM"] = "10.0.0.1 192.168.0.0/16"
        self.assertEqual(config.validate("vps", v), {})
        v["VPS_GCS_ALLOW_FROM"] = "somewhere"
        self.assertEqual(config.validate("vps", v)["VPS_GCS_ALLOW_FROM"]["code"], "type.source_list")
        v["VPS_GCS_ALLOW_FROM"] = "any"
        v["WEB_BACKEND"] = "ruby"
        self.assertEqual(config.validate("vps", v)["WEB_BACKEND"]["code"], "enum")


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "etc" / "uavlink"

    def test_save_and_load(self):
        values = presets.resolve("cm5", "dgp-current", {})
        res = config.save("cm5", "dgp-current", values, {"CM5_WIFI_PSK": "wifi pass'word"}, config_dir=self.dir)
        self.assertEqual(res["generated"], ["MAVLINK_SIGNING_PASSPHRASE"])
        cfg = config.read_env(self.dir / "config.env")
        sec = config.read_env(self.dir / "secrets.env")
        self.assertEqual(cfg["UAVLINK_ROLE"], "cm5")
        self.assertEqual(cfg["UAVLINK_PRESET"], "dgp-current")
        self.assertEqual(cfg["CM5_LAN_IP"], "192.168.50.2")
        self.assertFalse(set(cfg) & set(config.secret_keys()))
        self.assertNotIn("VPS_DOMAIN", cfg)
        self.assertEqual(sec["CM5_WIFI_PSK"], "wifi pass'word")
        self.assertRegex(sec["MAVLINK_SIGNING_PASSPHRASE"], r"^[A-Za-z0-9]{32}$")
        if hasattr(os, "geteuid"):
            self.assertEqual(stat.S_IMODE((self.dir / "config.env").stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((self.dir / "secrets.env").stat().st_mode), 0o600)
        loaded, present = config.load_current(self.dir)
        self.assertEqual(loaded["CM5_LAN_IP"], "192.168.50.2")
        self.assertNotIn("CM5_WIFI_PSK", loaded)
        self.assertTrue(present["CM5_WIFI_PSK"])
        self.assertTrue(present["MAVLINK_SIGNING_PASSPHRASE"])
        self.assertFalse(present["TS_AUTHKEY"])

    def test_keeps_existing_secrets(self):
        values = presets.resolve("cm5", "dgp-current", {})
        config.save("cm5", "dgp-current", values, {"CM5_WIFI_PSK": "firstpass", "TS_AUTHKEY": "tskey-1"}, config_dir=self.dir)
        first = config.read_env(self.dir / "secrets.env")
        values["CM5_LAN_IP"] = "192.168.50.3"
        config.save("cm5", "custom", values, {"TS_AUTHKEY": "", "CM5_WIFI_PSK": "newpass"}, config_dir=self.dir)
        second = config.read_env(self.dir / "secrets.env")
        self.assertEqual(second["MAVLINK_SIGNING_PASSPHRASE"], first["MAVLINK_SIGNING_PASSPHRASE"])
        self.assertEqual(second["TS_AUTHKEY"], "tskey-1")
        self.assertEqual(second["CM5_WIFI_PSK"], "newpass")
        self.assertEqual(config.read_env(self.dir / "config.env")["UAVLINK_PRESET"], "custom")

    def test_no_passphrase_when_signing_off(self):
        values = presets.resolve("cm5", "bench-lan", {})
        res = config.save("cm5", "bench-lan", values, {}, config_dir=self.dir)
        self.assertEqual(res["generated"], [])
        self.assertEqual(config.read_env(self.dir / "secrets.env"), {})

    def test_given_passphrase_kept(self):
        values = presets.resolve("cm5", "dgp-current", {})
        res = config.save("cm5", "dgp-current", values, {"MAVLINK_SIGNING_PASSPHRASE": "my-own-passphrase"}, config_dir=self.dir)
        self.assertEqual(res["generated"], [])
        self.assertEqual(config.read_env(self.dir / "secrets.env")["MAVLINK_SIGNING_PASSPHRASE"], "my-own-passphrase")

    def test_invalid_and_unknown(self):
        values = presets.resolve("cm5", "dgp-current", {})
        values["CM5_TCP_SERVER_PORT"] = "0"
        with self.assertRaises(config.ConfigError) as cm:
            config.save("cm5", "dgp-current", values, {}, config_dir=self.dir)
        self.assertEqual(cm.exception.errors["CM5_TCP_SERVER_PORT"]["code"], "range")
        with self.assertRaises(config.ConfigError):
            config.save("cm5", "dgp-current", {"VPS_DOMAIN": "x.example.com"}, {}, config_dir=self.dir)
        self.assertFalse(self.dir.exists())

    def test_vps_role(self):
        values = presets.resolve("vps", "vps-current", {})
        config.save("vps", "vps-current", values, {"WEB_BASIC_AUTH_PASS": "s3cret"}, config_dir=self.dir)
        cfg = config.read_env(self.dir / "config.env")
        self.assertEqual(cfg["UAVLINK_ROLE"], "vps")
        self.assertEqual(cfg["VPS_DOMAIN"], "mav.dgp.vn")
        self.assertNotIn("MAVLINK_SIGNING", cfg)
        self.assertEqual(config.read_env(self.dir / "secrets.env"), {"WEB_BASIC_AUTH_PASS": "s3cret"})


class RedactTest(unittest.TestCase):
    def test_redact(self):
        text = "pass=abcd1234efgh and key tskey-xyz, again abcd1234efgh"
        self.assertEqual(config.redact(text, {"A": "abcd1234efgh", "B": "tskey-xyz", "C": ""}), "pass=*** and key ***, again ***")
        self.assertEqual(config.redact(text, ["abcd1234efgh"]), "pass=*** and key tskey-xyz, again ***")
        self.assertEqual(config.redact("nothing", {"A": ""}), "nothing")
        self.assertEqual(config.redact("abcabc", ["abc", "abcabc"]), "***")


if __name__ == "__main__":
    unittest.main()
