"""Engine message texts follow docs/STYLE.md: no questions, formal register, same placeholders in vi and en."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / "share" / "i18n").glob("engine-*.json"))

# Words of the casual register that STYLE.md rule 1 rules out.
CASUAL_VI = re.compile(r"\b(bạn|chúng tôi|mình|nhé)\b", re.IGNORECASE)
CASUAL_EN = re.compile(r"\bwe\b", re.IGNORECASE)


def texts():
    """Yield (file name, code, lang, text) for every engine message."""
    for f in FILES:
        for code, msg in json.loads(f.read_text(encoding="utf-8")).items():
            for lang, text in msg.items():
                yield f.name, code, lang, text


class EngineTextStyleTest(unittest.TestCase):
    def test_files_exist(self):
        self.assertEqual([f.name for f in FILES],
                         ["engine-cm5.json", "engine-net.json", "engine-verify.json", "engine-x25.json"])

    def test_no_questions_or_exclamations(self):
        for name, code, lang, text in texts():
            self.assertNotIn("?", text, "%s %s %s" % (name, code, lang))
            self.assertNotIn("!", text, "%s %s %s" % (name, code, lang))

    def test_formal_register(self):
        for name, code, lang, text in texts():
            pattern = CASUAL_VI if lang == "vi" else CASUAL_EN
            self.assertIsNone(pattern.search(text), "%s %s %s: %r" % (name, code, lang, text))

    def test_vi_and_en_share_placeholders(self):
        for f in FILES:
            for code, msg in json.loads(f.read_text(encoding="utf-8")).items():
                vi = sorted(re.findall(r"\{(\w+)\}", msg["vi"]))
                en = sorted(re.findall(r"\{(\w+)\}", msg["en"]))
                self.assertEqual(vi, en, "%s %s" % (f.name, code))


if __name__ == "__main__":
    unittest.main()
