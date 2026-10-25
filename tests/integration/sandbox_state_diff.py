#!/usr/bin/env python3
"""Compare two sandbox-state.sh snapshots of the sandbox container (apply-container.sh).

  sandbox_state_diff.py <before> <after> [--pkg-files FILE] [--allow REGEX]… [--changed-pkgs]

--changed-pkgs   only print the packages installed, removed or changed between the two snapshots, one per line.
--pkg-files      `dpkg -L` output of those packages: their files, the systemd units they ship (enablement links,
                 unit states), their sysv rc links and local apparmor overrides are not compared. Package installs
                 are not undone by uavlink uninstall.
--allow          a difference whose line ("section<TAB>key") matches REGEX is printed as allowed, not counted.
Exit 0 when nothing else differs, 1 otherwise.
"""
import argparse
import os
import re
import sys


def load(path):
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) < 2:
                continue
            section, key = parts[0], parts[1]
            out[(section, key)] = parts[2] if len(parts) > 2 else ""
    return out


def changed_pkgs(a, b):
    names = {k for s, k in a if s == "pkg"} | {k for s, k in b if s == "pkg"}
    return sorted(n for n in names if a.get(("pkg", n)) != b.get(("pkg", n)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--pkg-files")
    ap.add_argument("--allow", action="append", default=[])
    ap.add_argument("--changed-pkgs", action="store_true")
    a = ap.parse_args(argv)
    before, after = load(a.before), load(a.after)
    if a.changed_pkgs:
        print("\n".join(changed_pkgs(before, after)))
        return 0

    pkg_paths, pkg_units, pkg_inits, pkg_profiles = set(), set(), set(), set()
    if a.pkg_files:
        with open(a.pkg_files, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.strip()
                if not p.startswith("/"):
                    continue
                pkg_paths.add(p)
                if re.search(r"/systemd/system/[^/]+\.(service|socket|timer)$", p):
                    pkg_units.add(os.path.basename(p))
                if p.startswith("/etc/init.d/"):
                    pkg_inits.add(os.path.basename(p))
                if re.match(r"/etc/apparmor\.d/[^/]+$", p):
                    pkg_profiles.add(os.path.basename(p))
    allow = [re.compile(r) for r in a.allow]

    def from_package(section, key):
        if section == "pkg":
            return True
        if section in ("unit", "unitfile"):
            return key in pkg_units
        if section == "file":
            if key in pkg_paths:
                return True
            if key.startswith("/etc/systemd/system/") and os.path.basename(key) in pkg_units:
                return True
            # made by the package scripts, not listed by dpkg -L: sysv links, empty local apparmor overrides
            m = re.match(r"/etc/rc[0-6S]\.d/[SK][0-9][0-9](.+)$", key)
            if m and m.group(1) in pkg_inits:
                return True
            if key.startswith("/etc/apparmor.d/local/") and os.path.basename(key) in pkg_profiles:
                return True
        return False

    bad = 0
    for k in sorted(set(before) | set(after)):
        old, new = before.get(k), after.get(k)
        if old == new:
            continue
        section, key = k
        label = "%s\t%s" % (section, key)
        text = "%s %s: %s -> %s" % (section, key, "(none)" if old is None else old, "(none)" if new is None else new)
        if from_package(section, key):
            continue
        if any(r.search(label) for r in allow):
            print("  allowed  " + text)
            continue
        print("  DIFF     " + text)
        bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
