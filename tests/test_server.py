import http.client
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "py"))

from uavlink import config, detect, paths, runner, server  # noqa: E402

FAKE_ENGINE = REPO / "tests" / "fixtures" / "fake-lib" / "engine"
PASSPHRASE = "Sup3rSecretPassphrase-123"


class ServerTestBase(unittest.TestCase):
    allow_wsl = "1"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uavlink-server-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.web = self.tmp / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<html>setup</html>", encoding="utf-8")
        (self.web / "app.js").write_text("// js", encoding="utf-8")
        (self.tmp / "secret.txt").write_text("outside", encoding="utf-8")
        for name in ("config", "state", "run"):
            (self.tmp / name).mkdir()
        patches = [
            mock.patch.object(paths, "ENGINE_DIR", FAKE_ENGINE),
            mock.patch.object(paths, "VENDOR_DIR", self.tmp / "vendor"),
            mock.patch.object(paths, "WEB_DIR", self.web),
            mock.patch.object(paths, "CONFIG_DIR", self.tmp / "config"),
            mock.patch.object(paths, "STATE_DIR", self.tmp / "state"),
            mock.patch.object(paths, "RUN_DIR", self.tmp / "run"),
            mock.patch.object(detect, "x25_ports", lambda root="/": []),
            mock.patch.dict(os.environ, {"UAVLINK_ALLOW_WSL": self.allow_wsl}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for key in list(os.environ):
            if key.startswith("FAKE_"):
                del os.environ[key]
        self.srv = server.SetupServer("127.0.0.1", 0, 600)
        self.thread = threading.Thread(target=self.srv.serve, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.srv.port
        self.host = "127.0.0.1:%d" % self.port
        self.cookie = "uavlink=" + self.srv.token

    def stop_server(self):
        job = runner.current_job()
        if job:
            job.cancel()
            job.wait(10)
        self.srv.stop()
        self.thread.join(10)

    def request(self, method, path, body=None, headers=None, cookie=True, origin=True, host=None, raw=None):
        h = {"Host": host or self.host}
        if cookie:
            h["Cookie"] = self.cookie
        if method != "GET":
            if origin:
                h["Origin"] = "http://" + h["Host"]
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            if data is not None:
                h["Content-Length"] = str(len(data))
            for k, v in h.items():
                conn.putheader(k, v)
            conn.endheaders(data)
            resp = conn.getresponse()
            payload = resp.read()
            return resp, payload
        finally:
            conn.close()

    def call(self, method, path, body=None, **kw):
        resp, payload = self.request(method, path, body, **kw)
        try:
            return resp.status, json.loads(payload)
        except ValueError:
            return resp.status, payload

    def wait_job(self, job_id, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            status, job = self.call("GET", "/api/jobs/" + job_id)
            if job["state"] != "running":
                return job
            time.sleep(0.05)
        self.fail("job did not end")

    @staticmethod
    def values():
        return config.defaults("cm5")


class AuthTest(ServerTestBase):
    def test_api_needs_cookie(self):
        status, body = self.call("GET", "/api/state", cookie=False)
        self.assertEqual((status, body["error"]), (401, "auth"))
        status, body = self.call("GET", "/api/state", headers={"Cookie": "uavlink=wrong"}, cookie=False)
        self.assertEqual(status, 401)

    def test_token_sets_cookie_and_redirects(self):
        resp, _ = self.request("GET", "/?t=" + self.srv.token, cookie=False)
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.getheader("Location"), "/")
        cookie = resp.getheader("Set-Cookie")
        self.assertEqual(cookie, "uavlink=%s; HttpOnly; SameSite=Strict; Path=/" % self.srv.token)
        status, _ = self.call("GET", "/api/state", headers={"Cookie": cookie.split(";")[0]}, cookie=False)
        self.assertEqual(status, 200)

    def test_bad_token_rejected(self):
        resp, _ = self.request("GET", "/?t=nope", cookie=False)
        self.assertEqual(resp.status, 401)
        self.assertIsNone(resp.getheader("Set-Cookie"))

    def test_bad_origin_403(self):
        status, body = self.call("POST", "/api/validate", {"role": "cm5"}, headers={"Origin": "http://evil.example"})
        self.assertEqual((status, body["error"]), (403, "origin"))
        status, _ = self.call("POST", "/api/validate", {"role": "cm5"}, origin=False)
        self.assertEqual(status, 403)

    def test_wrong_content_type_415(self):
        status, body = self.call("POST", "/api/validate", {"role": "cm5"}, headers={"Content-Type": "text/plain"})
        self.assertEqual((status, body["error"]), (415, "content_type"))

    def test_bad_host_421(self):
        status, body = self.call("GET", "/api/state", host="evil.example:%d" % self.port)
        self.assertEqual((status, body["error"]), (421, "host"))
        resp, _ = self.request("GET", "/", host="evil.example", cookie=False)
        self.assertEqual(resp.status, 421)
        resp, _ = self.request("GET", "/", host="localhost:%d" % self.port, cookie=False)
        self.assertEqual(resp.status, 200)

    def test_big_body_413(self):
        status, body = self.call("POST", "/api/validate", raw=b" " * (server.MAX_BODY + 1))
        self.assertEqual((status, body["error"]), (413, "too_large"))

    def test_security_headers(self):
        resp, _ = self.request("GET", "/", cookie=False)
        self.assertEqual(resp.getheader("Content-Security-Policy"), server.CSP)
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
        self.assertIsNone(resp.getheader("Cache-Control"))
        resp, _ = self.request("GET", "/api/state")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")


class StaticTest(ServerTestBase):
    def test_serves_page_and_assets(self):
        resp, body = self.request("GET", "/", cookie=False)
        self.assertEqual((resp.status, body), (200, b"<html>setup</html>"))
        self.assertTrue(resp.getheader("Content-Type").startswith("text/html"))
        resp, body = self.request("GET", "/app.js", cookie=False)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/javascript"))

    def test_traversal_blocked(self):
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt", "/a/../../secret.txt",
                     "/%2e%2e%2fsecret.txt", "/..\\secret.txt"):
            resp, body = self.request("GET", path, cookie=False)
            self.assertEqual(resp.status, 404, path)
            self.assertNotIn(b"outside", body)

    def test_post_to_page_is_405(self):
        status, _ = self.call("POST", "/", {})
        self.assertEqual(status, 405)


class ApiTest(ServerTestBase):
    def test_state(self):
        status, state = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        for key in ("version", "is_wsl", "preview_only", "role_guess", "host", "detect", "schema", "presets",
                    "current", "i18n"):
            self.assertIn(key, state)
        self.assertFalse(state["preview_only"])
        self.assertIn("hostname", state["host"])
        self.assertIn("step.packages", state["i18n"]["engine"])
        self.assertEqual(set(state["current"]), {"values", "secrets_present", "applied_at", "preset"})

    def test_state_lists_running_preset_first(self):
        running = {"id": "running", "role": "cm5", "name": {"en": "r"}, "desc": {"en": "r"}, "values": {}}
        with mock.patch.object(detect, "running_preset", lambda root="/": running):
            _, state = self.call("GET", "/api/state")
        self.assertEqual(state["presets"][0]["id"], "running")

    def test_validate(self):
        status, body = self.call("POST", "/api/validate", {"role": "cm5", "values": self.values()})
        self.assertEqual((status, body["ok"], body["errors"]), (200, True, {}))
        bad = dict(self.values(), CM5_LAN_IP="not-an-ip")
        status, body = self.call("POST", "/api/validate", {"role": "cm5", "values": bad})
        self.assertFalse(body["ok"])
        self.assertEqual(body["errors"]["CM5_LAN_IP"]["code"], "type.ipv4")

    def test_bad_requests(self):
        self.assertEqual(self.call("POST", "/api/validate", {"role": "nope"})[0], 400)
        self.assertEqual(self.call("POST", "/api/validate", raw=b"{not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/validate", raw=b"[1]")[0], 400)
        self.assertEqual(self.call("GET", "/api/nothing")[0], 404)
        self.assertEqual(self.call("GET", "/api/jobs/unknown")[0], 404)

    def test_precheck_job_and_stream(self):
        status, body = self.call("POST", "/api/precheck", {"role": "cm5", "values": self.values()})
        self.assertEqual(status, 200)
        resp, payload = self.request("GET", "/api/jobs/%s/stream" % body["job"])
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/event-stream"))
        text = payload.decode("utf-8")
        chunks = [c for c in text.split("\n\n") if c]
        self.assertTrue(chunks[0].startswith("data: "))
        self.assertIn('"id": "cm5"', text)
        self.assertEqual(chunks[-1], 'event: end\ndata: {"state": "ok"}')
        job = self.wait_job(body["job"])
        self.assertEqual(job["state"], "ok")
        self.assertEqual(job["events"][-1], {"t": "end", "state": "ok"})

    def test_precheck_invalid_values_400(self):
        status, body = self.call("POST", "/api/precheck", {"role": "cm5", "values": {"CM5_LAN_IP": "x"}})
        self.assertEqual((status, body["error"]), (400, "invalid"))
        self.assertIn("CM5_LAN_IP", body["errors"])

    def test_precheck_config_dir_is_private_and_removed(self):
        os.environ["FAKE_CM5_SLEEP"] = "1"
        _, body = self.call("POST", "/api/precheck", {"role": "cm5", "values": self.values()})
        job = runner.get_job(body["job"])
        config_dir = Path(job.config_dir)
        self.assertEqual(stat.S_IMODE(config_dir.stat().st_mode), 0o700)
        self.assertNotEqual(config_dir, paths.CONFIG_DIR)
        self.wait_job(body["job"])
        end = time.time() + 5
        while config_dir.exists() and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(config_dir.exists())
        self.assertEqual(list((self.tmp / "config").iterdir()), [])

    def test_second_job_is_busy(self):
        os.environ["FAKE_CM5_SLEEP"] = "3"
        _, first = self.call("POST", "/api/precheck", {"role": "cm5", "values": self.values()})
        status, body = self.call("POST", "/api/precheck", {"role": "cm5", "values": self.values()})
        self.assertEqual((status, body["error"]), (409, "busy"))
        status, body = self.call("POST", "/api/apply", {"role": "cm5", "values": self.values()})
        self.assertEqual((status, body["error"]), (409, "busy"))
        status, body = self.call("POST", "/api/verify", {"role": "cm5"})
        self.assertEqual((status, body["error"]), (409, "busy"))
        runner.get_job(first["job"]).cancel()
        self.wait_job(first["job"])

    def test_apply_validates_then_runs_and_unlocks_secret(self):
        status, body = self.call("POST", "/api/apply", {"role": "cm5", "values": {"CM5_LAN_IP": "x"}})
        self.assertEqual((status, body["error"]), (400, "invalid"))
        self.assertIn("CM5_LAN_IP", body["errors"])
        self.assertFalse((self.tmp / "config" / "config.env").exists())

        self.assertEqual(self.call("GET", "/api/secret/" + server.SECRET_KEY)[0], 404)
        values = dict(self.values(), MAVLINK_SIGNING="1")
        status, body = self.call("POST", "/api/apply", {"role": "cm5", "preset": "dgp-current", "values": values,
                                                        "secrets": {server.SECRET_KEY: PASSPHRASE}})
        self.assertEqual(status, 200)
        job = self.wait_job(body["job"])
        self.assertEqual(job["state"], "ok")
        self.assertNotIn(PASSPHRASE, json.dumps(job))
        self.assertTrue((self.tmp / "config" / "config.env").is_file())

        status, secret = self.call("GET", "/api/secret/" + server.SECRET_KEY)
        self.assertEqual((status, secret), (200, {"value": PASSPHRASE}))
        self.assertEqual(self.call("GET", "/api/secret/CM5_LAN_IP")[0], 404)
        self.assertEqual(self.call("GET", "/api/secret/UNKNOWN")[0], 404)
        self.assertEqual(self.call("GET", "/api/secret/" + server.SECRET_KEY, cookie=False)[0], 401)

        _, state = self.call("GET", "/api/state")
        self.assertTrue(state["current"]["applied_at"])
        self.assertEqual(state["current"]["preset"], "dgp-current")
        self.assertTrue(state["current"]["secrets_present"][server.SECRET_KEY])
        _, st = self.call("GET", "/api/status")
        self.assertEqual(st["applied"]["role"], "cm5")

    def test_verify_and_status(self):
        _, body = self.call("POST", "/api/verify", {"role": "cm5"})
        self.assertEqual(self.wait_job(body["job"])["state"], "ok")
        end = time.time() + 5
        while time.time() < end:
            _, st = self.call("GET", "/api/status")
            if st["last_verify"] and st["last_verify"]["state"] == "ok":
                break
            time.sleep(0.05)
        self.assertEqual(st["last_verify"]["state"], "ok")
        self.assertIn("services", st)

    def test_x25_backups_and_restore(self):
        backups = self.tmp / "state" / "x25-backups"
        backups.mkdir()
        (backups / "x25_params_20260101-000000.parm").write_text("A 1\n", encoding="utf-8")
        status, body = self.call("GET", "/api/x25/backups")
        self.assertEqual([b["name"] for b in body["backups"]], ["x25_params_20260101-000000.parm"])
        self.assertEqual(self.call("POST", "/api/x25/restore", {"file": "../../etc/passwd"})[0], 404)
        status, body = self.call("POST", "/api/x25/restore", {"file": "x25_params_20260101-000000.parm"})
        self.assertEqual(status, 200)
        self.wait_job(body["job"])

    def test_shutdown(self):
        status, body = self.call("POST", "/api/shutdown", {})
        self.assertEqual((status, body), (200, {"ok": True}))
        self.thread.join(10)
        self.assertFalse(self.thread.is_alive())


class PreviewOnlyTest(ServerTestBase):
    allow_wsl = "0"

    def test_apply_is_423_but_precheck_works(self):
        with mock.patch.object(detect, "is_wsl", lambda root="/": True):
            _, state = self.call("GET", "/api/state")
            self.assertTrue(state["preview_only"])
            status, body = self.call("POST", "/api/apply", {"role": "cm5", "values": self.values()})
            self.assertEqual((status, body["error"]), (423, "preview_only"))
            self.assertEqual(self.call("POST", "/api/x25/restore", {"file": "x"})[0], 423)
            self.assertEqual(self.call("POST", "/api/confirm", {})[0], 423)
            status, body = self.call("POST", "/api/precheck", {"role": "cm5", "values": self.values()})
            self.assertEqual(status, 200)
            self.wait_job(body["job"])
        self.assertFalse((self.tmp / "config" / "config.env").exists())


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uavlink-server-life-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(paths, "RUN_DIR", self.tmp / "run")
        p.start()
        self.addCleanup(p.stop)

    def test_setup_json_mode_and_content_and_removal(self):
        srv = server.SetupServer("127.0.0.1", 0, 600)
        thread = threading.Thread(target=srv.serve, daemon=True)
        thread.start()
        target = self.tmp / "run" / "setup.json"
        end = time.time() + 5
        while not target.exists() and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(set(data), {"listen", "port", "token", "pid", "started"})
        self.assertEqual((data["listen"], data["port"], data["pid"]), ("127.0.0.1", srv.port, os.getpid()))
        self.assertGreaterEqual(len(data["token"]), 24)
        srv.stop()
        thread.join(10)
        self.assertFalse(target.exists())

    def test_idle_exit(self):
        srv = server.SetupServer("127.0.0.1", 0, 1)
        thread = threading.Thread(target=srv.serve, daemon=True)
        started = time.time()
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.time() - started, 8)
        self.assertFalse((self.tmp / "run" / "setup.json").exists())

    def test_requests_keep_server_alive(self):
        srv = server.SetupServer("127.0.0.1", 0, 2)
        thread = threading.Thread(target=srv.serve, daemon=True)
        thread.start()
        for _ in range(4):
            time.sleep(0.8)
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
            conn.request("GET", "/api/state", headers={"Host": "127.0.0.1:%d" % srv.port})
            self.assertEqual(conn.getresponse().status, 401)
            conn.close()
        self.assertTrue(thread.is_alive())
        srv.stop()
        thread.join(10)

    def test_firewall_port_opened_and_removed_on_lan(self):
        calls = []
        with mock.patch.object(server, "ufw_active", lambda: True), \
                mock.patch.object(server, "ufw_allow", lambda port: calls.append(("allow", port))), \
                mock.patch.object(server, "ufw_delete", lambda port: calls.append(("delete", port))):
            srv = server.SetupServer("0.0.0.0", 0, 600)
            thread = threading.Thread(target=srv.serve, daemon=True)
            thread.start()
            end = time.time() + 5
            while not calls and time.time() < end:
                time.sleep(0.02)
            srv.stop()
            thread.join(10)
        self.assertEqual(calls, [("allow", srv.port), ("delete", srv.port)])

    def test_loopback_does_not_touch_firewall(self):
        calls = []
        with mock.patch.object(server, "ufw_active", lambda: calls.append("active") or True):
            srv = server.SetupServer("127.0.0.1", 0, 600)
            thread = threading.Thread(target=srv.serve, daemon=True)
            thread.start()
            time.sleep(0.2)
            srv.stop()
            thread.join(10)
        self.assertEqual(calls, [])

    def test_host_allowed(self):
        for ok in ("127.0.0.1", "127.0.0.1:8090", "[::1]:8090", "localhost:1", "192.168.1.5"):
            self.assertTrue(server.host_allowed(ok), ok)
        for bad in ("", None, "evil.example", "evil.example:8090", "127.0.0.1.evil.example"):
            self.assertFalse(server.host_allowed(bad), bad)


if __name__ == "__main__":
    unittest.main()
