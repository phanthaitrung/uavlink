import unittest

from py.uavlink import config, presets

CM5_IDS = {"dgp-current", "vps-starlink", "tailscale-only", "bench-lan", "ardupilot-default", "custom"}
VPS_IDS = {"vps-current", "vps-minimal", "vps-restricted"}


class PresetsTest(unittest.TestCase):
    def test_ids_per_role(self):
        self.assertEqual({p["id"] for p in presets.load_presets("cm5")}, CM5_IDS)
        self.assertEqual({p["id"] for p in presets.load_presets("vps")}, VPS_IDS)
        self.assertEqual(len(presets.load_presets()), len(CM5_IDS | VPS_IDS))

    def test_sorted_and_star(self):
        for role, star in (("cm5", "dgp-current"), ("vps", "vps-current")):
            lst = presets.load_presets(role)
            self.assertEqual([p["order"] for p in lst], sorted(p["order"] for p in lst))
            self.assertEqual([p["id"] for p in lst if p.get("star")], [star])
            self.assertEqual(lst[0]["id"], star)

    def test_every_preset_resolves_and_validates(self):
        for p in presets.load_presets():
            values = presets.resolve(p["role"], p["id"], {})
            self.assertEqual(set(values), set(config.defaults(p["role"])), p["id"])
            self.assertEqual(config.validate(p["role"], values), {}, p["id"])

    def test_keys_exist_in_schema_for_role(self):
        schema = config.load_schema()["keys"]
        for p in presets.load_presets():
            for k in list(p["values"]) + list(p.get("ask", [])):
                self.assertIn(k, schema, (p["id"], k))
                self.assertIn(p["role"], schema[k]["roles"], (p["id"], k))
            for f in ("name", "desc", "values", "ask"):
                self.assertIn(f, p, p["id"])

    def test_dgp_current_equals_defaults(self):
        self.assertEqual(presets.resolve("cm5", "dgp-current", {}), config.defaults("cm5"))
        self.assertEqual(presets.resolve("vps", "vps-current", {}), config.defaults("vps"))

    def test_common_parts(self):
        for p in presets.load_presets("cm5"):
            v = presets.resolve("cm5", p["id"], {})
            self.assertEqual(v["CM5_ROUTER_LOG_MODE"], "while-armed", p["id"])
            self.assertEqual(v["SERVICE_SANDBOX"], "1", p["id"])
            self.assertEqual(v["SSH_HARDEN"], "1", p["id"])
            self.assertEqual(v["CM5_ENABLE_UFW"], "1", p["id"])
            if p["id"] == "bench-lan":
                self.assertEqual(v["MAVLINK_SIGNING"], "0")
                self.assertEqual(v["CM5_MAVLINK_ALLOW_LAN"], "1")
                self.assertIn("warning", p)
            else:
                self.assertEqual(v["MAVLINK_SIGNING"], "1", p["id"])
                self.assertEqual(v["CM5_MAVLINK_ALLOW_LAN"], "0", p["id"])
                self.assertNotIn("warning", p)

    def test_specific_presets(self):
        v = presets.resolve("cm5", "ardupilot-default", {})
        self.assertEqual((v["X25_IP"], v["CM5_LAN_IP"]), ("192.168.144.14", "192.168.144.2"))
        self.assertEqual(presets.get("vps-starlink")["ask"][0], "VPS_PUBLIC_IP")
        self.assertIn("CM5_EXTRA_UDP_TARGETS", presets.get("tailscale-only")["ask"])
        self.assertEqual(presets.resolve("cm5", "tailscale-only", {})["CM5_SSH_ALLOW_LAN"], "0")
        self.assertEqual(presets.resolve("vps", "vps-minimal", {})["VPS_INSTALL_WEB"], "0")
        self.assertEqual(presets.resolve("vps", "vps-restricted", {})["VPS_GCS_ALLOW_FROM"], "100.64.0.0/10")

    def test_overrides(self):
        v = presets.resolve("cm5", "dgp-current", {"CM5_LAN_IP": "192.168.50.9", "CM5_TCP_SERVER_PORT": 5761})
        self.assertEqual(v["CM5_LAN_IP"], "192.168.50.9")
        self.assertEqual(v["CM5_TCP_SERVER_PORT"], "5761")

    def test_errors(self):
        with self.assertRaisesRegex(presets.PresetError, "unknown keys.*NOPE"):
            presets.resolve("cm5", "dgp-current", {"NOPE": "1"})
        with self.assertRaisesRegex(presets.PresetError, "VPS_DOMAIN"):
            presets.resolve("cm5", "dgp-current", {"VPS_DOMAIN": "x.example.com"})
        with self.assertRaisesRegex(presets.PresetError, "unknown preset"):
            presets.resolve("cm5", "nope", {})
        with self.assertRaisesRegex(presets.PresetError, "role"):
            presets.resolve("vps", "dgp-current", {})
        self.assertIsNone(presets.get("nope"))


if __name__ == "__main__":
    unittest.main()
