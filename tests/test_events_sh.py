import json
import os
import subprocess
import unittest
from pathlib import Path

EVENTS_SH = Path(__file__).resolve().parents[1] / "engine" / "lib" / "events.sh"


def run(script, events="1"):
    env = dict(os.environ, UAVLINK_EVENTS=events)
    out = subprocess.run(["bash", "-c", f'. "{EVENTS_SH}"; {script}'], env=env,
                         capture_output=True, text=True, check=True).stdout
    return [json.loads(line[len("@@UAVLINK "):]) for line in out.splitlines() if line.startswith("@@UAVLINK ")]


class EventsShTest(unittest.TestCase):
    def test_step_and_check(self):
        evs = run('ev_step eth start; ev_step eth fail eth.default_route "route via eth0" \'{"iface":"eth0"}\'; '
                  'ev_check lan.link true')
        self.assertEqual(evs[0], {"t": "step", "id": "eth", "state": "start"})
        self.assertEqual(evs[1], {"t": "step", "id": "eth", "state": "fail", "code": "eth.default_route",
                                  "msg": "route via eth0", "args": {"iface": "eth0"}})
        self.assertEqual(evs[2], {"t": "check", "id": "lan.link", "ok": True})

    def test_escaping(self):
        evs = run(r'''d=$'--- a\n+++ b\n\t"q" \\ x\e[31m'; ev_change file /etc/x update diff="$d" old:=1''')
        self.assertEqual(evs[0]["diff"], '--- a\n+++ b\n\t"q" \\ x[31m')
        self.assertEqual(evs[0]["old"], 1)

    def test_silent_without_flag(self):
        self.assertEqual(run("ev_step eth start; ev_info a b", events="0"), [])


if __name__ == "__main__":
    unittest.main()
