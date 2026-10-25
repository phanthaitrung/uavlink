import json
import re
import unittest

from py.uavlink import config, paths, presets


def bilingual_objects(node, where="$"):
    """Yield (where, dict) for every dict that has a vi or en key."""
    if isinstance(node, dict):
        if "vi" in node or "en" in node:
            yield where, node
        else:
            for k, v in node.items():
                yield from bilingual_objects(v, "%s.%s" % (where, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from bilingual_objects(v, "%s[%d]" % (where, i))


def check(test, name, data):
    count = 0
    for where, obj in bilingual_objects(data):
        count += 1
        for lang in ("vi", "en"):
            test.assertIsInstance(obj.get(lang), str, "%s %s: missing %s" % (name, where, lang))
            test.assertTrue(obj[lang].strip(), "%s %s: empty %s" % (name, where, lang))
    return count


INFORMAL_VI = re.compile(r"(?<!\w)(bạn|chúng tôi)(?!\w)", re.IGNORECASE)


def check_style(test, name, data, headings):
    """docs/STYLE.md: headings (labels, names) are statements, vi texts are impersonal."""
    for where, obj in bilingual_objects(data):
        test.assertIsNone(INFORMAL_VI.search(obj["vi"]), "%s %s: %r" % (name, where, obj["vi"]))
    for where, obj in headings:
        for lang in ("vi", "en"):
            test.assertNotIn("?", obj[lang], "%s %s: question in %s heading" % (name, where, lang))


class I18nDataTest(unittest.TestCase):
    def test_schema(self):
        schema = config.load_schema()
        self.assertGreater(check(self, "schema.json", schema), 100)
        for name, spec in schema["keys"].items():
            self.assertTrue(spec["label"]["vi"] and spec["label"]["en"], name)
            self.assertTrue(spec["help"]["vi"] and spec["help"]["en"], name)
            for opt in spec.get("options", []):
                self.assertTrue(opt["label"]["vi"] and opt["label"]["en"], name)
        for tab in schema["tabs"]:
            self.assertTrue(tab["label"]["vi"] and tab["label"]["en"], tab["id"])
        headings = [("tabs.%s" % t["id"], t["label"]) for t in schema["tabs"]]
        for name, spec in schema["keys"].items():
            headings.append((name, spec["label"]))
            headings += [("%s=%s" % (name, o["value"]), o["label"]) for o in spec.get("options", [])]
        check_style(self, "schema.json", schema, headings)

    def test_presets(self):
        for p in presets.load_presets():
            check(self, p["id"], p)
            self.assertTrue(p["name"]["vi"] and p["desc"]["en"], p["id"])
            check_style(self, p["id"], p, [("name", p["name"])])

    def test_engine_messages(self):
        files = sorted((paths.SHARE_DIR / "i18n").glob("engine-*.json")) if (paths.SHARE_DIR / "i18n").is_dir() else []
        for f in files:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            for code, msg in data.items():
                self.assertEqual(set(msg), {"vi", "en"}, "%s %s" % (f.name, code))
                self.assertTrue(msg["vi"].strip() and msg["en"].strip(), "%s %s" % (f.name, code))


if __name__ == "__main__":
    unittest.main()
