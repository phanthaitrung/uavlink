"""Presets (ARCHITECTURE.md section 3.2): share/presets/*.json."""

import json

from . import config, paths


class PresetError(ValueError):
    """Unknown preset, wrong role, or override of a key the role does not have."""


def load_presets(role=None):
    """All presets (optionally of one role), sorted by `order` then id."""
    found = []
    folder = paths.SHARE_DIR / "presets"
    for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        with open(p, encoding="utf-8") as f:
            preset = json.load(f)
        if role is None or preset.get("role") == role:
            found.append(preset)
    found.sort(key=lambda x: (x.get("order", 1000), x["id"]))
    return found


def get(preset_id):
    """The preset with this id, or None."""
    for preset in load_presets():
        if preset["id"] == preset_id:
            return preset
    return None


def resolve(role, preset_id, overrides=None):
    """defaults(role) <- preset values <- overrides, all strings. Raises PresetError on unknown preset, role
    mismatch or unknown keys."""
    preset = get(preset_id)
    if preset is None:
        raise PresetError("unknown preset: %s" % preset_id)
    if preset.get("role") != role:
        raise PresetError("preset %s is for role %s, not %s" % (preset_id, preset.get("role"), role))
    values = config.defaults(role)
    for source, label in ((preset.get("values", {}), "preset " + preset_id), (overrides or {}, "overrides")):
        unknown = sorted(set(source) - set(values))
        if unknown:
            raise PresetError("unknown keys in %s for role %s: %s" % (label, role, ", ".join(unknown)))
        values.update({k: str(v) for k, v in source.items()})
    return values
