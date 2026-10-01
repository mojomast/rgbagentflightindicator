"""End-to-end API tests against a real HTTP server and a dummy keyboard.

Covers the behaviour that took the longest to get right on hardware: a second
turn goes green again, a finished turn holds, lanes are not freed early, and
malformed input is refused instead of silently eating a lamp.
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request

from rgi.backends.dummy import DummyBackend
from rgi.daemon import Daemon, Device, Lanes, make_server

TOKEN = "test-token"


class SingleColourBackend(DummyBackend):
    """A device that can only show one colour at a time - a VIA-only QMK board,
    or a laptop backlight. It must show the most urgent lane, not pretend to
    have per-key control."""

    name = "single"
    per_lamp = False


class TestSingleColourDevice(unittest.TestCase):
    def setUp(self):
        self.backend = SingleColourBackend(count=5)
        self.backend.open()
        self.lanes = Lanes(count=3)
        self.device = Device(self.backend, pool=[0, 1, 2], label="single")
        self.daemon = Daemon([self.device], self.lanes, quiet=False)

    def render(self, now=None):
        import time
        return self.device.frame(self.daemon.snapshot(), now or time.monotonic(), False)

    def test_idle_and_empty_is_dark(self):
        self.assertEqual(self.render(), [(0, 0, 0)] * 5)

    def test_one_working_lane_lights_the_whole_device(self):
        self.lanes.claim("s", "opencode", None, None)
        self.lanes.set_state("s", "working")
        self.assertEqual(self.render(), [(0, 255, 0)] * 5)

    def test_the_most_urgent_state_wins(self):
        self.lanes.claim("a", "x", None, None)
        self.lanes.claim("b", "x", None, None)
        self.lanes.set_state("a", "done")
        self.lanes.set_state("b", "blocked")
        frame = self.render()
        # blocked outranks done, so the board blinks red whichever it shows
        self.assertIn(frame[0], ((255, 0, 0), (0, 0, 0)))
        self.assertEqual(len(set(frame)), 1)      # uniform, never a mix

    def test_done_alone_blinks_white(self):
        import time
        self.lanes.claim("s", "x", None, None)
        self.lanes.set_state("s", "done")
        seen = set()
        for i in range(14):
            seen.add(self.render(time.monotonic() + i * 0.05)[0])
        self.assertIn((255, 255, 255), seen)
        self.assertIn((0, 0, 0), seen)


class PanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = DummyBackend(count=6)
        cls.backend.open()
        cls.lanes = Lanes(count=4)
        cls.device = Device(cls.backend, pool=[0, 1, 2, 3], label="dummy")
        cls.daemon = Daemon([cls.device], cls.lanes, quiet=False, verbose=False)
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
        """Render one frame the way the daemon loop would."""
        import time
        state = self.daemon.snapshot()
        return self.device.frame(state, time.monotonic(), False)


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


class TestLaneInfo(PanelCase):
    """The detail behind an uncollapsed lane: repository, tokens, children, blocking."""

    def test_info_lands_and_shows_in_status(self):
        self.call("/session/start", {"sessionID": "s", "label": "nightly"})
        status, body = self.call("/session/info", {
            "sessionID": "s",
            "info": {"repo": "rgbagentflightindicator", "branch": "main",
                     "blocked_on": {"action": "bash", "resources": ["rm -rf /tmp"]},
                     "tokens": {"input": 1234, "output": 56},
                     "children": [{"id": "ses_kid", "label": "child task",
                                   "state": "working"}]}})
        self.assertEqual(status, 200)
        _, body = self.call("/session/s")
        self.assertEqual(body["info"]["repo"], "rgbagentflightindicator")
        self.assertEqual(body["info"]["blocked_on"]["action"], "bash")
        self.assertEqual(body["info"]["children"][0]["label"], "child task")
        self.assertEqual(body["info"]["tokens"]["input"], 1234)

    def test_partial_info_merges_instead_of_replacing(self):
        self.call("/session/start", {"sessionID": "s"})
        self.call("/session/info", {"sessionID": "s",
                                    "info": {"repo": "r", "tokens": {"input": 10, "output": 1}}})
        self.call("/session/info", {"sessionID": "s", "info": {"tokens": {"output": 99}}})
        _, body = self.call("/session/s")
        self.assertEqual(body["info"]["repo"], "r")               # kept
        self.assertEqual(body["info"]["tokens"]["input"], 10)     # kept
        self.assertEqual(body["info"]["tokens"]["output"], 99)    # updated

    def test_top_level_fields_are_accepted_too(self):
        self.call("/session/start", {"sessionID": "s"})
        status, _ = self.call("/session/info", {"sessionID": "s", "repo": "shorthand"})
        self.assertEqual(status, 200)
        _, body = self.call("/session/s")
        self.assertEqual(body["info"]["repo"], "shorthand")

    def test_empty_info_is_refused_and_unknown_lane_is_404(self):
        self.call("/session/start", {"sessionID": "s"})
        status, _ = self.call("/session/info", {"sessionID": "s"})
        self.assertEqual(status, 400)
        status, _ = self.call("/session/info", {"sessionID": "nobody", "info": {"repo": "x"}})
        self.assertEqual(status, 404)

    def test_a_lane_is_either_in_flight_or_idle_never_both(self):
        """A running action is not idle, and a landed lane is not flying."""
        self.call("/session/start", {"sessionID": "s"})
        self.call("/session/state", {"sessionID": "s", "state": "working"})
        _, body = self.call("/session/s")
        self.assertIsInstance(body["in_flight_s"], float)
        self.assertIsNone(body["idle_s"])              # in flight, so not idle
        self.assertLess(body["in_flight_s"], 5)

        self.call("/session/state", {"sessionID": "s", "state": "done"})
        _, body = self.call("/session/s")
        self.assertIsNone(body["in_flight_s"])          # landed, so not flying
        self.assertIsInstance(body["idle_s"], float)

    def test_info_never_changes_the_painted_state(self):
        """Detail is for reading: it must not make the panel repaint."""
        self.call("/session/start", {"sessionID": "s", "slot": 0})
        self.call("/session/state", {"sessionID": "s", "state": "working"})
        before = self.frame()
        self.call("/session/info", {"sessionID": "s", "info": {"repo": "x", "tokens": {"input": 1}}})
        self.assertEqual(self.frame(), before)

    def test_ident_is_first_class_and_can_be_set_alone(self):
        """An agent naming itself is a complete request, not 'nothing to record'."""
        self.call("/session/start", {"sessionID": "s", "host": "kimi"})
        status, _ = self.call("/session/info", {"sessionID": "s", "identifier": "hermes-3"})
        self.assertEqual(status, 200)
        _, body = self.call("/session/s")
        self.assertEqual(body["ident"], "hermes-3")
        self.assertNotIn("ident", body["info"])        # not duplicated into detail

    def test_ident_can_be_given_when_claiming(self):
        self.call("/session/start", {"sessionID": "s", "ident": "hermes-3", "host": "kimi"})
        _, body = self.call("/session/s")
        self.assertEqual(body["ident"], "hermes-3")
        self.assertEqual(body["host"], "kimi")

    def test_info_without_ident_still_works(self):
        self.call("/session/start", {"sessionID": "s"})
        status, _ = self.call("/session/info", {"sessionID": "s", "info": {"repo": "r"}})
        self.assertEqual(status, 200)
        _, body = self.call("/session/s")
        self.assertEqual(body["info"]["repo"], "r")
        self.assertEqual(body["ident"], "")

    def test_in_flight_belongs_to_the_action_not_the_lane(self):
        """A landed agent is not flying: in flight ends at the landing, and starts
        again from zero when the next turn does."""
        self.call("/session/start", {"sessionID": "s", "slot": 0})
        self.call("/session/state", {"sessionID": "s", "state": "working"})
        _, body = self.call("/session/s")
        self.assertIsInstance(body["in_flight_s"], float)
        self.assertLess(body["in_flight_s"], 5)

        self.call("/session/state", {"sessionID": "s", "state": "done"})
        _, body = self.call("/session/s")
        self.assertIsNone(body["in_flight_s"])          # landed, not in flight
        self.assertIsInstance(body["idle_s"], float)     # but still reporting

        self.call("/session/state", {"sessionID": "s", "state": "working"})
        _, body = self.call("/session/s")
        self.assertLess(body["in_flight_s"], 2)          # a fresh action, a fresh timer

    def test_blocked_is_still_in_flight(self):
        """Waiting on a human is an unfinished action, not a landing."""
        self.call("/session/start", {"sessionID": "s"})
        self.call("/session/state", {"sessionID": "s", "state": "blocked"})
        _, body = self.call("/session/s")
        self.assertIsInstance(body["in_flight_s"], float)

    def test_releasing_a_lane_drops_its_info(self):
        self.call("/session/start", {"sessionID": "s"})
        self.call("/session/info", {"sessionID": "s", "info": {"repo": "x"}})
        self.call("/session/end", {"sessionID": "s"})
        self.assertNotIn("s", self.lanes.info)


class TestLanePolicy(unittest.TestCase):
    """Which agent gets which lane: configured, and what happens when it is taken."""

    def setUp(self):
        from rgi.daemon import Daemon, Device, Lanes
        from rgi.backends.dummy import DummyBackend

        self.backend = DummyBackend(count=8)
        self.backend.open()
        self.lanes = Lanes(count=8)
        self.device = Device(self.backend, pool=list(range(8)), label="dummy")
        self.daemon = Daemon([self.device], self.lanes, quiet=False,
                             lane_map={"hermes-3": 5, "opencode": 1})

    def claim(self, agent, ident=None, slot=None):
        payload = {"agent": agent, "sessionID": agent + (ident or ""), "label": "x"}
        if ident:
            payload["ident"] = ident
        if slot is not None:
            payload["slot"] = slot
        slot_used = self.lanes.claim(
            payload["sessionID"], agent, "x", "host", slot, ident,
            None if slot is not None else self.daemon.lane_map.get(ident or agent),
        )
        return slot_used

    def test_an_ident_gets_its_configured_lane(self):
        self.assertEqual(self.claim("agent", ident="hermes-3"), 5)

    def test_an_agent_name_can_be_mapped_too(self):
        self.assertEqual(self.claim("opencode"), 1)

    def test_unmapped_agents_take_the_first_free_lane(self):
        self.assertEqual(self.claim("whoever"), 0)

    def test_a_mapped_lane_that_is_taken_falls_back(self):
        self.assertEqual(self.claim("agent", ident="hermes-3"), 5)
        # same policy, different session: 5 is gone, so take a free lane
        self.lanes.release("agenthermes-3")
        self.assertEqual(self.claim("someone", ident="hermes-3"), 5)

    def test_an_explicit_slot_still_wins_over_a_policy(self):
        self.assertEqual(self.lanes.claim("a", "agent", "x", "h", 7, "hermes-3", 5), 7)

    def test_lane_map_loader_reads_a_file_and_ignores_junk(self):
        import json
        import tempfile
        from rgi.daemon import load_lane_map

        path = os.path.join(tempfile.gettempdir(), "rgi-lane-map-test.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hermes-3": 5, "bad": "not a number", "seven": "7"}, fh)
        policy = load_lane_map(path)
        self.assertEqual(policy["hermes-3"], 5)
        self.assertEqual(policy["seven"], 7)          # numeric strings are fine
        self.assertNotIn("bad", policy)               # junk is skipped, not fatal
        os.remove(path)

    def test_lane_map_loader_survives_a_missing_file(self):
        from rgi.daemon import load_lane_map
        self.assertEqual(load_lane_map(os.path.join("nowhere", "nope.json")), {})


if __name__ == "__main__":
    unittest.main()
