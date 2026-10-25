"""The setup page's UI strings: vi.json and en.json agree, and app.js only asks for keys that exist."""
import json
import re
import unicodedata
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "share" / "web"

# Keys built at run time in app.js (tt(prefix, id) and validate.<code>).
DYNAMIC = (
    ["action." + a for a in "create update delete enable disable start restart mask unmask install set".split()]
    + ["kind." + k for k in "file service param firewall nm package user udev".split()]
    + ["role.%s.%s" % (r, f) for r in ("cm5", "vps") for f in ("name", "desc")]
    + ["stage." + s for s in ("cm5", "x25", "verify", "net")]
    + ["group." + g for g in ("x25", "lan", "vps", "gcs", "cm5", "other")]
    + ["sensor." + s for s in "gyro accel mag baro gps rc ahrs battery prearm".split()]
    + ["state." + s for s in ("ok", "warn", "fail", "skip")]
    + ["step.%s.name" % s for s in ("role", "preset", "settings", "precheck", "apply")]
    + ["stepname." + s for s in "packages usb eth wifi tailscale router security".split()]
    + ["validate." + c for c in ("required", "range", "enum", "conflict")]
    + ["validate.type." + t for t in ("string int port bool enum ipv4 host iface path cidr_list "
                                      "udp_targets source_list passphrase").split()]
    + ["err.role.vps_not_ready"]
)

# docs/STYLE.md: formal register, no personal address ("bạn", "chúng tôi", "mình", "hãy"; "we", "our").
INFORMAL = {
    "vi": re.compile(r"\b(bạn|chúng tôi|mình|hãy)\b", re.IGNORECASE),
    "en": re.compile(r"\b(we|our)\b", re.IGNORECASE),
}


def load(lang):
    with open(WEB / "i18n" / (lang + ".json"), encoding="utf-8") as f:
        return json.load(f)


class TestWebI18n(unittest.TestCase):
    def test_same_keys(self):
        vi, en = load("vi"), load("en")
        self.assertEqual(sorted(set(vi) - set(en)), [], "keys only in vi.json")
        self.assertEqual(sorted(set(en) - set(vi)), [], "keys only in en.json")

    def test_no_empty_values(self):
        for lang in ("vi", "en"):
            for key, val in load(lang).items():
                self.assertIsInstance(val, str, "%s: %s" % (lang, key))
                self.assertTrue(val.strip(), "%s: %s is empty" % (lang, key))

    def test_placeholders_match(self):
        vi, en = load("vi"), load("en")
        for key in vi:
            if key in en:
                self.assertEqual(sorted(re.findall(r"\{(\w+)\}", vi[key])),
                                 sorted(re.findall(r"\{(\w+)\}", en[key])), key)

    def test_formal_style(self):
        # No questions or exclamations anywhere in the UI: headings and confirmation prompts are statements.
        for lang in ("vi", "en"):
            for key, val in load(lang).items():
                val = unicodedata.normalize("NFC", val)
                self.assertNotIn("?", val, "%s: %s" % (lang, key))
                self.assertNotIn("!", val, "%s: %s" % (lang, key))
                m = INFORMAL[lang].search(val)
                self.assertIsNone(m, "%s: %s contains %r" % (lang, key, m and m.group(0)))

    def test_keys_used_in_app_js_exist(self):
        src = (WEB / "app.js").read_text(encoding="utf-8")
        used = set(re.findall(r"(?<![\w$.])t\(\s*\"([^\"]+)\"", src))
        self.assertGreater(len(used), 50)
        for lang in ("vi", "en"):
            have = load(lang)
            missing = sorted(k for k in used | set(DYNAMIC) if k not in have)
            self.assertEqual(missing, [], "missing in %s.json" % lang)

    def test_html_has_no_inline_code(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html), "inline script")
        self.assertIsNone(re.search(r"<style", html), "inline style element")
        self.assertIsNone(re.search(r"\sstyle=|\son\w+=", html), "inline style or event attribute")

    def test_no_external_urls(self):
        for name in ("index.html", "app.js", "style.css"):
            text = (WEB / name).read_text(encoding="utf-8").replace("http://www.w3.org/2000/svg", "")  # SVG namespace
            self.assertIsNone(re.search(r"https?://", text), name + " refers to an external URL")


if __name__ == "__main__":
    unittest.main()
