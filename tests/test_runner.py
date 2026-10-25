import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))

from uavlink import detect, paths, runner  # noqa: E402

FAKE_ENGINE = REPO / "tests" / "fixtures" / "fake-lib" / "engine"
SECRET = "S3cretPassw0rd-xyz"


class RunnerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uavlink-runner-"))
        self.addCleanup(self._cleanup)
        self.config = self.tmp / "config"
        self.state = self.tmp / "state"
        self.run_dir = self.tmp / "run"
        self.config.mkdir()
        patches = [
            mock.patch.object(paths, "ENGINE_DIR", FAKE_ENGINE),
            mock.patch.object(paths, "VENDOR_DIR", self.tmp / "vendor"),
            mock.patch.object(paths, "CONFIG_DIR", self.config),
            mock.patch.object(paths, "STATE_DIR", self.state),
            mock.patch.object(paths, "RUN_DIR", self.run_dir),
            mock.patch.object(detect, "x25_ports", lambda root="/": []),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for key in list(os.environ):
            if key.startswith("FAKE_"):
                del os.environ[key]
        self.jobs = []

    def _cleanup(self):
        for job in self.jobs:
            job.cancel()
            job.wait(10)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, job):
        self.jobs.append(job)
        return job

    def finish(self, job, timeout=20):
        self.assertTrue(job.wait(timeout), "job did not finish")
        return job

    def wait_for(self, predicate, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition not reached")

    @staticmethod
    def stages(job):
        return [(e["id"], e["state"]) for e in job.events if e["t"] == "stage"]

    def log_text(self):
        files = sorted((self.state / "logs").glob("*.log"))
        self.assertEqual(len(files), 1, files)
        return files[0].read_text(encoding="utf-8")


class PipelineTest(RunnerTestBase):
    def test_precheck_parses_events_and_logs(self):
        job = self.finish(self.job(runner.precheck("cm5", self.config)))
        self.assertEqual(job.state, "ok")
        self.assertEqual(self.stages(job), [("cm5", "start"), ("cm5", "ok"), ("x25", "start"), ("x25", "ok"),
                                            ("net", "start"), ("net", "ok")])
        self.assertEqual(job.events[-1], {"t": "end", "state": "ok"})
        self.assertIn({"t": "step", "id": "eth", "state": "start"}, job.events)
        self.assertIn({"t": "change", "kind": "param", "target": "NET_IPADDR3", "old": "14", "new": "1"}, job.events)
        self.assertIn("vps.tcp", [e.get("id") for e in job.events if e["t"] == "check"])
        logs = [e["line"] for e in job.events if e["t"] == "log"]
        self.assertIn("args: --preview", logs)
        self.assertIn("args: --mode auto", logs)
        self.assertIn("@@UAVLINK not json", logs)            # a broken event line stays a log line
        self.assertTrue(job.started and job.ended)

    def test_engine_environment(self):
        job = self.finish(self.job(runner.precheck("cm5", self.config)))
        env_line = next(e["line"] for e in job.events if e["t"] == "log" and e["line"].startswith("env:"))
        self.assertEqual(env_line, f"env: events=1 config={self.config} state={self.state} "
                                   f"pythonpath={self.tmp / 'vendor'} lc=C.UTF-8 bytecode=1")

    def test_precheck_runs_all_stages_after_a_failure(self):
        os.environ["FAKE_CM5_EXIT"] = "1"
        os.environ["FAKE_X25_EXIT"] = "1"
        job = self.finish(self.job(runner.precheck("cm5", self.config)))
        self.assertEqual(job.state, "failed")
        self.assertEqual(self.stages(job), [("cm5", "start"), ("cm5", "fail"), ("x25", "start"), ("x25", "fail"),
                                            ("net", "start"), ("net", "ok")])
        self.assertIn({"t": "stage", "id": "cm5", "state": "fail", "exit": 1}, job.events)
        self.assertEqual(job.events[-1], {"t": "end", "state": "failed"})

    def test_precheck_net_failure_fails_the_job(self):
        os.environ["FAKE_NET_EXIT"] = "3"
        job = self.finish(self.job(runner.precheck("cm5", self.config)))
        self.assertEqual(job.state, "failed")
        self.assertEqual(self.stages(job)[-1], ("net", "fail"))
        self.assertIn({"t": "stage", "id": "net", "state": "fail", "exit": 3}, job.events)

    def test_apply_skips_x25_unless_props_off_and_port(self):
        job = self.finish(self.job(runner.apply("cm5", self.config)))
        self.assertEqual(job.state, "ok")
        self.assertEqual(self.stages(job), [("cm5", "start"), ("cm5", "ok"), ("x25", "skip"),
                                            ("verify", "start"), ("verify", "ok")])
        skip = next(e for e in job.events if e["t"] == "stage" and e["state"] == "skip")
        self.assertEqual(skip["code"], "x25.props_not_off")

        with mock.patch.object(detect, "x25_ports", lambda root="/": []):
            job = self.finish(self.job(runner.apply("cm5", self.config, props_off=True)))
        skip = next(e for e in job.events if e["t"] == "stage" and e["state"] == "skip")
        self.assertEqual(skip["code"], "x25.not_connected")

    def test_apply_runs_x25_with_yes(self):
        with mock.patch.object(detect, "x25_ports", lambda root="/": ["/dev/serial/by-id/usb-ArduPilot_X25-if00"]):
            job = self.finish(self.job(runner.apply("cm5", self.config, props_off=True)))
        self.assertEqual(self.stages(job)[2:4], [("x25", "start"), ("x25", "ok")])
        self.assertIn({"t": "log", "line": "args: --yes"}, job.events)

    def test_apply_stops_at_first_failure(self):
        os.environ["FAKE_CM5_EXIT"] = "1"
        job = self.finish(self.job(runner.apply("cm5", self.config, props_off=True)))
        self.assertEqual(job.state, "failed")
        self.assertEqual([s for s in self.stages(job) if s[1] != "skip"], [("cm5", "start"), ("cm5", "fail")])
        not_run = [e for e in job.events if e["t"] == "stage" and e.get("code") == "stage.not_run"]
        self.assertEqual({e["id"] for e in not_run}, {"verify"})
        self.assertFalse((self.state / "applied.json").exists())

    def test_verify_runs_verify_cm5_then_net_probe_live(self):
        job = self.finish(self.job(runner.verify("cm5")))
        self.assertEqual(job.state, "ok")
        self.assertEqual(self.stages(job), [("verify", "start"), ("verify", "ok")])
        self.assertEqual([e["line"] for e in job.events if e["t"] == "log"], ["args: ", "args: --mode live"])
        self.assertEqual([e["id"] for e in job.events if e["t"] == "check"], ["lan.link", "vps.tcp"])

    def test_verify_exit_3_is_failed(self):
        os.environ["FAKE_VERIFY_EXIT"] = "3"
        job = self.finish(self.job(runner.verify("cm5")))
        self.assertEqual(job.state, "failed")
        self.assertIn({"t": "stage", "id": "verify", "state": "fail", "exit": 3}, job.events)
        # the network checks still run after a failed verify-cm5.sh
        self.assertEqual([e.get("id") for e in job.events if e["t"] == "check"], ["lan.link", "vps.tcp"])

    def test_verify_fails_on_net_probe_and_keeps_the_first_exit_code(self):
        os.environ["FAKE_NET_EXIT"] = "3"
        job = self.finish(self.job(runner.verify("cm5")))
        self.assertIn({"t": "stage", "id": "verify", "state": "fail", "exit": 3}, job.events)
        os.environ["FAKE_VERIFY_EXIT"] = "1"
        job = self.finish(self.job(runner.verify("cm5")))
        self.assertIn({"t": "stage", "id": "verify", "state": "fail", "exit": 1}, job.events)

        os.environ.pop("FAKE_VERIFY_EXIT")
        job = self.finish(self.job(runner.apply("cm5", self.config)))
        self.assertEqual(job.state, "failed")
        self.assertIn("args: --mode live", [e["line"] for e in job.events if e["t"] == "log"])
        self.assertFalse((self.state / "applied.json").exists())

    def test_applied_json_on_success(self):
        (self.config / "config.env").write_text("CM5_LAN_IP='192.168.50.2'\nUAVLINK_PRESET='dgp-current'\n")
        self.finish(self.job(runner.apply("cm5", self.config)))
        data = json.loads((self.state / "applied.json").read_text())
        self.assertEqual(sorted(data), ["preset", "role", "time", "version"])
        self.assertEqual((data["preset"], data["role"]), ("dgp-current", "cm5"))
        self.assertRegex(data["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_vps_not_ready(self):
        for start in (lambda: runner.precheck("vps", self.config), lambda: runner.apply("vps", self.config),
                      lambda: runner.verify("vps")):
            job = self.job(start())
            self.assertTrue(job.wait(1))
            self.assertEqual(job.state, "failed")
            self.assertIn("role.vps_not_ready", [e.get("code") for e in job.events])
        self.assertFalse((self.state / "applied.json").exists())

    def test_x25_restore_and_uninstall(self):
        job = self.finish(self.job(runner.x25_restore("/var/lib/uavlink/x25-backups/a.parm")))
        self.assertIn({"t": "log", "line": "args: --restore /var/lib/uavlink/x25-backups/a.parm --yes"}, job.events)

        job = self.finish(self.job(runner.uninstall(preview=True)))
        self.assertIn({"t": "log", "line": "fake uninstall-cm5 args: --preview"}, job.events)

        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "uninstall.sh").write_text('echo "saved uninstall $*"\n')
        job = self.finish(self.job(runner.uninstall()))
        self.assertEqual([e["line"] for e in job.events if e["t"] == "log"], ["saved uninstall "])


class JobControlTest(RunnerTestBase):
    def test_subscribe_replays_then_streams(self):
        os.environ["FAKE_CM5_SLEEP"] = "1"
        job = self.job(runner.precheck("cm5", self.config))
        self.wait_for(lambda: any(e.get("state") == "start" and e.get("id") == "eth" for e in job.events))
        seen = []
        job.subscribe(seen.append)
        self.assertEqual(seen, job.events[:len(seen)])
        self.assertTrue(seen)                                # replay happened
        self.finish(job)
        self.assertEqual(seen, job.events)                   # nothing missed or duplicated
        late = []
        job.subscribe(late.append)
        self.assertEqual(late, job.events)

    def test_a_failing_subscriber_does_not_break_the_job(self):
        def boom(ev):
            raise RuntimeError("x")
        job = self.job(runner.precheck("cm5", self.config))
        job.subscribe(boom)
        self.assertEqual(self.finish(job).state, "ok")

    def test_cancel(self):
        os.environ["FAKE_CM5_SLEEP"] = "30"
        job = self.job(runner.precheck("cm5", self.config))
        self.wait_for(lambda: ("cm5", "start") in self.stages(job) and any(e["t"] == "step" for e in job.events))
        t0 = time.time()
        job.cancel()
        self.finish(job, 10)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.events[-1], {"t": "end", "state": "failed", "reason": "cancelled"})
        self.assertNotIn(("x25", "start"), self.stages(job))

    def test_cancel_stops_before_the_next_command_of_a_stage(self):
        os.environ["FAKE_VERIFY_SLEEP"] = "30"
        job = self.job(runner.verify("cm5"))
        self.wait_for(lambda: any(e["t"] == "check" for e in job.events))
        job.cancel()
        self.finish(job, 10)
        self.assertEqual(job.events[-1], {"t": "end", "state": "failed", "reason": "cancelled"})
        self.assertNotIn("args: --mode live", [e.get("line") for e in job.events])

    def test_one_job_at_a_time(self):
        os.environ["FAKE_CM5_SLEEP"] = "30"
        first = self.job(runner.precheck("cm5", self.config))
        self.assertIs(runner.current_job(), first)
        with self.assertRaises(runner.Busy):
            runner.apply("cm5", self.config)
        with self.assertRaises(runner.Busy):
            runner.verify("cm5")
        first.cancel()
        self.finish(first, 10)
        self.assertIsNone(runner.current_job())
        os.environ.pop("FAKE_CM5_SLEEP")
        self.assertEqual(self.finish(self.job(runner.verify("cm5"))).state, "ok")

    def test_get_job(self):
        job = self.finish(self.job(runner.verify("cm5")))
        self.assertIs(runner.get_job(job.id), job)
        self.assertIsNone(runner.get_job("nope"))
        self.assertEqual(job.to_dict()["id"], job.id)


class RedactionTest(RunnerTestBase):
    def test_secrets_never_appear(self):
        (self.config / "secrets.env").write_text(
            f"MAVLINK_SIGNING_PASSPHRASE='{SECRET}'\nCM5_WIFI_PSK=\"quoted psk 1234\"\nTS_AUTHKEY=\n# c=ignored1234\n")
        os.environ["FAKE_ECHO"] = SECRET
        job = self.finish(self.job(runner.precheck("cm5", self.config)))
        dumped = json.dumps(job.events)
        self.assertNotIn(SECRET, dumped)
        self.assertNotIn(SECRET, self.log_text())
        self.assertIn("leak: ***", [e["line"] for e in job.events if e["t"] == "log"])
        info = next(e for e in job.events if e["t"] == "info")
        self.assertEqual((info["msg"], info["args"]["v"]), ("***", "***"))

    def test_read_secret_values(self):
        (self.config / "secrets.env").write_text("A='it'\\''s a long one'\nB=plain-value # note\nC=ab\nD=\"x y z w\"\n")
        values = runner.read_secret_values(self.config)
        self.assertLessEqual({"it's a long one", "plain-value", "x y z w"}, values)
        self.assertNotIn("ab", values)                       # too short to redact safely
        self.assertEqual(runner.read_secret_values(self.tmp / "missing"), set())


if __name__ == "__main__":
    unittest.main()
