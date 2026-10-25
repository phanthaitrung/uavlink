#!/usr/bin/env python3
"""Read the events of `uavlink … --json` (one JSON object per line) for apply-container.sh.

  sandbox_events.py summary <file>      stages, steps, failed checks and changes, for the log
  sandbox_events.py changes <file>      the change events, "kind target action" per line
  sandbox_events.py rollback <file>     the armed rollback seconds, or nothing
  sandbox_events.py checks <file>       failed checks (warnings excluded), "id code" per line
  sandbox_events.py stage <file> <id>   the last state of a runner stage (ok, fail, skip…)
"""
import json
import sys


def events(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("{"):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def failed_checks(evs):
    return [e for e in evs if e.get("t") == "check" and not e.get("ok") and e.get("severity") != "warn"]


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, evs = argv[0], events(argv[1])
    if cmd == "changes":
        for e in evs:
            if e.get("t") == "change":
                print(e.get("kind", ""), e.get("target", ""), e.get("action", ""))
    elif cmd == "rollback":
        secs = [e.get("secs") for e in evs if e.get("t") == "rollback" and e.get("state") == "armed"]
        if secs:
            print(secs[-1])
    elif cmd == "checks":
        for e in failed_checks(evs):
            print(e.get("id", ""), e.get("code", ""), e.get("msg", ""))
    elif cmd == "stage":
        states = [e.get("state") for e in evs if e.get("t") == "stage" and e.get("id") == argv[2]]
        print(states[-1] if states else "")
    elif cmd == "summary":
        for e in evs:
            t = e.get("t")
            if t == "stage" and e.get("state") != "start":
                print("  stage %-8s %s %s" % (e.get("id"), e.get("state"), e.get("code") or e.get("exit") or ""))
            elif t == "step" and e.get("state") != "start":
                print("    step %-10s %s %s" % (e.get("id"), e.get("state"), e.get("code") or ""))
            elif t == "change":
                print("    change %s %s %s" % (e.get("kind"), e.get("target"), e.get("action")))
            elif t == "rollback":
                print("    rollback %s %s" % (e.get("state"), e.get("secs") or ""))
        for e in failed_checks(evs):
            print("    check FAILED %s %s %s" % (e.get("id"), e.get("code"), e.get("msg", "")))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
