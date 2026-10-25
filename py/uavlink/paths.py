"""Every path the package uses (ARCHITECTURE.md section 2). Environment variables override them for tests."""

import os
from pathlib import Path

_PKG_PARENT = Path(__file__).resolve().parents[2]   # repo root, or /usr/lib/uavlink when installed


def is_source_tree():
    """True when running from a git checkout instead of the installed package."""
    return (_PKG_PARENT / "ARCHITECTURE.md").is_file() and (_PKG_PARENT / "share").is_dir()


def _env_path(name, default):
    value = os.environ.get(name)
    return Path(value) if value else Path(default)


LIB_DIR = _env_path("UAVLINK_LIB_DIR", _PKG_PARENT)
ENGINE_DIR = LIB_DIR / "engine"
VENDOR_DIR = LIB_DIR / "vendor"
SHARE_DIR = _env_path("UAVLINK_SHARE_DIR", _PKG_PARENT / "share" if is_source_tree() else "/usr/share/uavlink")
WEB_DIR = SHARE_DIR / "web"
CONFIG_DIR = _env_path("UAVLINK_CONFIG_DIR", "/etc/uavlink")
STATE_DIR = _env_path("UAVLINK_STATE_DIR", "/var/lib/uavlink")
RUN_DIR = _env_path("UAVLINK_RUN_DIR", "/run/uavlink")
CONFIG_FILE = CONFIG_DIR / "config.env"
SECRETS_FILE = CONFIG_DIR / "secrets.env"
