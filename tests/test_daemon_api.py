"""End-to-end API tests against a real HTTP server and a dummy keyboard.

Covers the behaviour that took the longest to get right on hardware: a second
turn goes green again, a finished turn holds, lanes are not freed early, and
malformed input is refused instead of silently eating a lamp.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from rgi.backends.dummy import DummyBackend
from rgi.daemon import Daemon, Lanes, Renderer, make_server

TOKEN = "test-token"


class PanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = DummyBackend(count=6)
        cls.backend.open()
        cls.lanes = Lanes(pool=[0, 1, 2, 3])
        cls.daemon = Daemon(cls.backend, cls.lanes,
                            Renderer(cls.backend, cls.lanes, quiet=False), verbose=False)
        cls.server = make_server("127.0.0.1", 0, cls.daemon, TOKEN)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.backend.close()

    def setUp(self):
        self.lanes.clear()
        self.backend.frames.clear()

    # -- helpers ----------------------------------------------------------
    def call(self, path, payload=None, token=TOKEN, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or
                                     ("POST" if data else "GET"))
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-LED-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def frame(self):
        """Render one frame the way the daemon loop would, and return the lit lanes."""
        colours = self.daemon.renderer.frame(__import__("time").monotonic())
        return colours


class TestAuth(PanelCase):
    def test_missing_token_is_refused(self):
        status, body = self.call("/status", token=None)
        self.assertEqual(status, 401)
        self.assertIn("X-LED-Token", body["error"])


class TestLanes(PanelCase):
    def test_claim_and_report(self):
        status, body = self.call("/session/start",
                                 {"agent": "hermes", "sessionID": "job-1",
                                  "label": "nightly sync", "host": "kimi"})
        self.assertEqual(status, 200)
        self.assertEqual(body["slot"], 0)
        self.assertEqual(body["key"], "`")

        status, _ = self.call("/session/state", {"sessionID": "job-1", "state": "working"})
        self.assertEqual(status, 200)

        status, body = self.call("/session/job-1")
        self.assertEqual(status, 200)
        self.assertEqual(body["host"], "kimi")
        self.assertEqual(body["state"], "working")

    def test_second_turn_goes_green_again(self):
        self.call("/session/start", {"sessionID": "s", "label": "x"})
        self.call("/session/state", {"sessionID": "s", "state": "working"})
        self.call("/session/state", {"sessionID": "s", "state": "done"})
        self.call("/session/state", {"sessionID": "s", "state": "working"})
        _, body = self.call("/session/s")
        self.assertEqual(body["state"], "working")

    def test_a_finished_lane_stays_lit(self):
        self.call("/session/start", {"sessionID": "s", "label": "x"})
        self.call("/session/state", {"sessionID": "s", "state": "done"})
        _, body = self.call("/status")
        self.assertIn("s", body["sessions"])

    def test_release_frees_the_lamp(self):
        self.call("/session/start", {"sessionID": "s"})
        self.call("/session/end", {"sessionID": "s"})
        _, body = self.call("/status")
        self.assertNotIn("s", body["sessions"])
        self.assertIn(0, body["free"])

    def test_slot_can_be_requested_and_conflicts_are_reported(self):
        status, body = self.call("/session/start", {"sessionID": "a", "slot": 2})
        self.assertEqual((status, body["slot"]), (200, 2))
        status, body = self.call("/session/start", {"sessionID": "b", "slot": 2})
        self.assertEqual(status, 409)
        # an explicit request is refused honestly; nobody else gets evicted
        self.assertNotIn(2, body["free"])
        self.assertIn("a", self.lanes.slot)

    def test_all_lamps_taken_evicts_the_least_recently_used(self):
        for i in range(4):
            self.call("/session/start", {"sessionID": f"s{i}"})
        status, body = self.call("/session/start", {"sessionID": "extra"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.lanes.slot), 4)
        self.assertIn("extra", self.lanes.slot)

    def test_unknown_state_is_refused_with_the_list(self):
        self.call("/session/start", {"sessionID": "s"})
        status, body = self.call("/session/state", {"sessionID": "s", "state": "wroking"})
        self.assertEqual(status, 400)
        self.assertIn("working", body["known"])

    def test_state_for_an_unknown_session_is_404(self):
        status, _ = self.call("/session/state", {"sessionID": "nope", "state": "working"})
        self.assertEqual(status, 404)

    def test_malformed_start_does_not_consume_a_lamp(self):
        status, _ = self.call("/session/start", {"agent": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(self.lanes.slot, {})


class TestRendering(PanelCase):
    def test_only_the_lane_lights_up(self):
        self.call("/session/start", {"sessionID": "s", "slot": 1})
        self.call("/session/state", {"sessionID": "s", "state": "working"})
        colours = self.frame()
        self.assertEqual(colours[1], (0, 255, 0))
        self.assertEqual(colours[0], (0, 0, 0))

    def test_blocked_blinks_red(self):
        self.call("/session/start", {"sessionID": "s", "slot": 1})
        self.call("/session/state", {"sessionID": "s", "state": "blocked"})
        seen = set()
        for _ in range(12):
            colours = self.frame()
            seen.add(colours[1])
            __import__("time").sleep(0.05)
        self.assertIn((255, 0, 0), seen)
        self.assertIn((0, 0, 0), seen)          # it blinks, it is not solid

    def test_a_done_lane_ends_up_steady_white(self):
        import time
        self.call("/session/start", {"sessionID": "s", "slot": 1})
        self.call("/session/state", {"sessionID": "s", "state": "done"})
        self.lanes.changed["s"] = time.monotonic() - 100     # long after the blinks
        self.assertEqual(self.frame()[1], (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
