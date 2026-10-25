import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))

from uavlink import paths  # noqa: E402


class PathsTest(unittest.TestCase):
    def test_source_tree_paths(self):
        self.assertTrue(paths.is_source_tree())
        self.assertEqual(paths.ENGINE_DIR, REPO / "engine")
        self.assertEqual(paths.SHARE_DIR, REPO / "share")
        self.assertEqual(paths.CONFIG_FILE, Path("/etc/uavlink/config.env"))

    def test_env_override(self):
        code = "from uavlink import paths; print(paths.CONFIG_FILE, paths.STATE_DIR)"
        env = dict(os.environ, UAVLINK_CONFIG_DIR="/tmp/c", UAVLINK_STATE_DIR="/tmp/s", PYTHONPATH=str(REPO / "py"))
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.split(), ["/tmp/c/config.env", "/tmp/s"])


if __name__ == "__main__":
    unittest.main()
