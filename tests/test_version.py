import re
import unittest
from pathlib import Path

from py.uavlink import VERSION

REPO = Path(__file__).resolve().parents[1]


def changelog_version():
    first = (REPO / "debian" / "changelog").read_text(encoding="utf-8").splitlines()[0]
    m = re.match(r"uavlink \(([^)]+)\) ", first)
    assert m, first
    return m.group(1)


class VersionTest(unittest.TestCase):
    """Every place that names the version follows the top entry of debian/changelog."""

    def test_code(self):
        self.assertEqual(VERSION, changelog_version(), "update VERSION in py/uavlink/__init__.py")

    def test_manual_page(self):
        th = (REPO / "debian" / "uavlink.1").read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(f'"uavlink {changelog_version()}"', th, "update the .TH line of debian/uavlink.1")


if __name__ == "__main__":
    unittest.main()
