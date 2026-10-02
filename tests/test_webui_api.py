"""The /ui static surface and /ui/api endpoints, against a real HTTP server."""

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
import importlib.util as importlib_util
from unittest import mock

from rgi import webconfig
from rgi.backends.dummy import DummyBackend
from rgi.daemon import Daemon, Device, Lanes, make_server

TOKEN = "ui-test-token"


class UiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = DummyBackend(count=6)
        cls.backend.open()
        cls.lanes = Lanes(count=3)
        cls.device = Device(cls.backend, pool=[0, 1, 2], label="dummy")
        cls.daemon = Daemon([cls.device], cls.lanes, quiet=False)
        cls.server = make_server("127.0.0.1", 0, cls.daemon, TOKEN)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.dir = __import__("tempfile").TemporaryDirectory()
        cls.config_path = os.path.join(cls.dir.name, "config.json")
        cls.patch = mock.patch.object(webconfig, "CONFIG_PATH", cls.config_path)
        cls.patch.start()
        cls.addClassCleanup(cls.patch.stop)
        cls.addClassCleanup(cls.dir.cleanup)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.backend.close()

    def setUp(self):
        self.lanes.clear()
        self.backend.frames.clear()
        self.daemon.config = webconfig.default_config()
        self.daemon.raw_config = {}
        self.daemon.revision = 0
        self.daemon.last_paint = 0.0
        self.device.override = None

    def call(self, path, method="GET", payload=None, token=TOKEN, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if token:
            request.add_header("X-LED-Token", token)
        if data:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()


class TestStatic(UiCase):
    def test_shell_is_served_without_a_token(self):
        status, headers, body = self.call("/ui/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())
        self.assertIn("Content-Security-Policy", headers)

    def test_javascript_gets_a_javascript_mime(self):
        status, headers, _ = self.call("/ui/lib/dom.js", token=None)
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))

    def test_etag_revalidation(self):
        _, headers, _ = self.call("/ui/app.css", token=None)
        etag = headers.get("ETag")
        self.assertTrue(etag)
        status, _, _ = self.call("/ui/app.css", token=None, headers={"If-None-Match": etag})
        self.assertEqual(status, 304)

    def test_traversal_and_unknown_assets_are_refused(self):
        status, _, _ = self.call("/ui/..%2f..%2frgi%2fdaemon.py", token=None)
        self.assertEqual(status, 404)
        status, _, _ = self.call("/ui/nope.js", token=None)
        self.assertEqual(status, 404)

    def test_api_needs_the_token(self):
        status, _, _ = self.call("/ui/api/status", token=None)
        self.assertEqual(status, 401)


class TestConfigApi(UiCase):
    def test_get_config_returns_effective_values_and_capabilities(self):
        status, _, body = self.call("/ui/api/config")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["appearance"]["states"]["working"]["color"], "#00ff00")
        self.assertEqual(data["capabilities"][0]["name"], "dummy")
        self.assertEqual(data["capabilities"][0]["lamps"][0]["label"], "`")

    def test_put_applies_validates_and_preserves_unknown_fields(self):
        _, _, body = self.call("/ui/api/config")
        original = json.loads(body)
        raw = {"schema_version": 1, "revision": 0, "future_field": {"keep": True}}
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        self.daemon.raw_config = raw
        draft = json.loads(json.dumps(original["config"]))
        draft["appearance"]["states"]["working"]["color"] = "#00aaff"
        status, _, body = self.call("/ui/api/config", "PUT", draft,
                                    headers={"If-Match": str(original["revision"])})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["revision"], 1)
        with open(self.config_path, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["future_field"], {"keep": True})
        self.assertEqual(self.daemon.appearance.render("working", 0.0), (0, 170, 255))

    def test_stale_revision_is_a_conflict(self):
        _, _, body = self.call("/ui/api/config")
        draft = json.loads(body)["config"]
        status, _, _ = self.call("/ui/api/config", "PUT", draft, headers={"If-Match": "99"})
        self.assertEqual(status, 409)

    def test_invalid_config_is_refused_and_not_written(self):
        _, _, body = self.call("/ui/api/config")
        draft = json.loads(body)["config"]
        draft["appearance"]["states"]["working"]["color"] = "green"
        status, _, body = self.call("/ui/api/config", "PUT", draft, headers={"If-Match": "0"})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)["error"]["code"], "validation_failed")
        self.assertFalse(os.path.exists(self.config_path))

    def test_validate_route_reports_field_paths(self):
        _, _, body = self.call("/ui/api/config")
        draft = json.loads(body)["config"]
        draft["settings"]["port"] = 70000
        status, _, body = self.call("/ui/api/validate", "POST", draft)
        errors = json.loads(body)["errors"]
        self.assertEqual(status, 200)
        self.assertTrue(any(e["path"] == "settings.port" for e in errors))

    def test_defaults_route_returns_the_builtin_config(self):
        status, _, body = self.call("/ui/api/defaults")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["config"]["schema_version"],
                         webconfig.SCHEMA_VERSION)


class TestOverlay(UiCase):
    def test_paint_writes_one_frame_and_delete_restores_lanes(self):
        self.lanes.claim("s", "opencode", None, None)
        self.lanes.set_state("s", "working")
        self.daemon.tick()
        self.backend.frames.clear()

        status, _, _ = self.call("/ui/api/paint", "POST", {
            "device": "dummy", "lamps": [{"index": 2, "rgb": [255, 0, 0]}],
            "duration_ms": 1500})
        self.assertEqual(status, 200)
        self.daemon.tick()
        self.assertEqual(self.backend.frames[-1][2], (255, 0, 0))

        status, _, _ = self.call("/ui/api/paint", "DELETE")
        self.assertEqual(status, 200)
        self.daemon.tick()
        self.assertNotEqual(self.backend.frames[-1][2], (255, 0, 0))

    def test_overlay_expires_and_lane_rendering_resumes(self):
        self.lanes.claim("s", "opencode", None, None)
        self.lanes.set_state("s", "working")
        self.call("/ui/api/paint", "POST", {
            "device": "dummy", "lamps": [{"index": 1, "rgb": [255, 255, 0]}],
            "duration_ms": 120})
        self.daemon.tick()
        self.assertEqual(self.backend.frames[-1][1], (255, 255, 0))
        time.sleep(0.2)
        self.backend.frames.clear()
        self.daemon.tick()
        self.assertEqual(self.backend.frames[-1][0], (0, 255, 0))   # lane 0 green

    def test_unknown_device_is_a_404(self):
        status, _, _ = self.call("/ui/api/paint", "POST", {
            "device": "not-a-device", "lamps": [], "duration_ms": 100})
        self.assertEqual(status, 404)


class TestAgentTest(UiCase):
    def test_test_agent_claims_lands_and_releases(self):
        config = webconfig.default_config()
        config["agents"] = [{"id": "tester", "label": "Tester",
                             "match": {"agent": "tester"}, "enabled": True}]
        self.daemon.apply_config(config)
        status, _, body = self.call("/ui/api/agents/tester/test", "POST", {"seconds": 0.3})
        started = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("slot", started)
        try:
            deadline = time.time() + 4
            seen = False
            while time.time() < deadline:
                self.daemon.tick()
                sessions = self.lanes.slot.keys()
                if started["sessionID"] in sessions:
                    seen = True
                    if self.lanes.state.get(started["sessionID"]) == "done":
                        break
                time.sleep(0.1)
            self.assertTrue(seen)
            self.assertEqual(self.lanes.state.get(started["sessionID"]), "done")
        finally:
            self.lanes.release(started["sessionID"])


class TestDiagnostics(UiCase):
    def test_health_and_logs(self):
        self.daemon.log_event("info", "test", "hello")
        status, _, body = self.call("/ui/api/health")
        health = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(health["devices"][0]["backend"], "dummy")
        status, _, body = self.call("/ui/api/logs")
        self.assertTrue(any(line["message"] == "hello"
                            for line in json.loads(body)["lines"]))

    def test_sse_sends_an_initial_snapshot(self):
        request = urllib.request.Request(self.base + "/ui/api/events")
        request.add_header("X-LED-Token", TOKEN)
        with urllib.request.urlopen(request, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            lines = []
            for _ in range(8):
                line = resp.readline()
                if not line or line in (b"\n", b"\r\n"):
                    break
                lines.append(line)
        self.assertTrue(any(b"event: snapshot" in line for line in lines), lines)


class TestReachApi(UiCase):
    def test_ack_dims_a_done_lane(self):
        self.call("/session/start", "POST", {"agent": "reacher", "sessionID": "r-1"})
        self.call("/session/state", "POST", {"sessionID": "r-1", "state": "done"})
        status, _, body = self.call("/session/ack", "POST", {"sessionID": "r-1"})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "idle")
        self.assertEqual(self.lanes.state["r-1"], "idle")

    def test_ack_keeps_a_blocked_lane_and_rearms_on_new_state(self):
        self.call("/session/start", "POST", {"agent": "reacher", "sessionID": "r-2"})
        self.call("/session/state", "POST", {"sessionID": "r-2", "state": "blocked"})
        status, _, body = self.call("/session/ack", "POST", {"sessionID": "r-2"})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(result["acked"])
        view = self.daemon.lane_view("r-2", self.lanes.slot["r-2"])
        self.assertTrue(view["acked"])
        self.call("/session/state", "POST", {"sessionID": "r-2", "state": "working"})
        view = self.daemon.lane_view("r-2", self.lanes.slot["r-2"])
        self.assertFalse(view["acked"])

    def test_ack_unknown_session_is_a_404(self):
        status, _, _ = self.call("/session/ack", "POST", {"sessionID": "nope"})
        self.assertEqual(status, 404)

    def test_aggregate_reports_counts_and_blocked_waits(self):
        self.call("/session/start", "POST", {"agent": "a", "sessionID": "agg-1"})
        self.call("/session/start", "POST", {"agent": "b", "sessionID": "agg-2"})
        self.call("/session/state", "POST", {"sessionID": "agg-1", "state": "working"})
        self.call("/session/state", "POST", {"sessionID": "agg-2", "state": "blocked"})
        status, _, body = self.call("/ui/api/aggregate")
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(result["counts"]["working"], 1)
        self.assertEqual(result["counts"]["blocked"], 1)
        self.assertEqual(result["blocked"][0]["session"], "agg-2")

    def test_history_endpoint_serves_the_tail(self):
        self.daemon.history_path = os.path.join(self.dir.name, "events.jsonl")
        with open(self.daemon.history_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "state": "done"}) + "\n")
        status, _, body = self.call("/ui/api/history?since=0")
        entries = json.loads(body)["entries"]
        self.assertEqual(status, 200)
        self.assertEqual(len(entries), 1)

    def test_stale_lane_is_annotated_but_never_changed(self):
        self.daemon.config["settings"]["stale_s"] = 1
        self.call("/session/start", "POST", {"agent": "a", "sessionID": "stale-1"})
        self.call("/session/state", "POST", {"sessionID": "stale-1", "state": "working"})
        self.lanes.changed["stale-1"] = time.monotonic() - 60
        self.lanes.updated["stale-1"] = time.time() - 60
        _, _, body = self.call("/session/stale-1")
        lane = json.loads(body)
        self.assertTrue(lane["stale"])
        self.assertEqual(lane["state"], "working")

    def test_hook_ingest_maps_a_claude_event(self):
        if importlib_util.find_spec("rgi.ingest") is None:
            self.skipTest("ingest module not present yet")
        payload = {"hook_event_name": "SessionStart",
                   "session_id": "abc-123", "cwd": "/repo",
                   "source": "startup"}
        status, _, body = self.call("/hook/claude-code", "POST", payload)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["source"], "claude-code")
        self.assertIn("claude-code:abc-123", self.lanes.slot)
        status, _, _ = self.call("/hook/claude-code", "POST",
                                 {"hook_event_name": "UserPromptSubmit",
                                  "session_id": "abc-123"})
        self.assertEqual(status, 200)
        self.assertEqual(self.lanes.state["claude-code:abc-123"], "working")


if __name__ == "__main__":
    unittest.main()
