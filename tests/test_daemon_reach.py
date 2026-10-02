"""The v0.8 reach features: ack, stale annotation, events, aggregate, history.

These are the honest bits of "reach": a human can say they have seen a lane, a
lane whose reporter died is marked stale instead of confidently green, and every
transition is published once for observers to consume.
"""

import json
import os
import sys
import tempfile
import time
import unittest

from rgi.backends.dummy import DummyBackend
from rgi.daemon import Daemon, Device, Lanes


class ReachCase(unittest.TestCase):
    def setUp(self):
        self.backend = DummyBackend(count=6)
        self.backend.open()
        self.lanes = Lanes(count=3)
        self.device = Device(self.backend, pool=[0, 1, 2], label="dummy")
        self.daemon = Daemon([self.device], self.lanes, quiet=False)
        self.daemon.config["settings"]["stale_s"] = 60
        self.sid = "test:1"
        self.lanes.claim(self.sid, "tester", "a task", "host")
        self.slot = self.lanes.slot[self.sid]

    def frame(self):
        return self.device.frame(self.daemon.snapshot(), time.monotonic(), False)

    # -- ack -------------------------------------------------------------
    def test_ack_on_done_dims_the_lane_without_releasing_it(self):
        self.lanes.set_state(self.sid, "done")
        self.assertEqual(self.lanes.ack(self.sid), "idle")
        self.assertEqual(self.lanes.state[self.sid], "idle")
        self.assertIn(self.sid, self.lanes.slot)          # still held
        self.assertNotIn(self.sid, self.lanes.acked)

    def test_ack_on_blocked_keeps_the_state_but_stops_the_blink(self):
        self.lanes.set_state(self.sid, "blocked")
        # land in the blink-off half of the cycle
        self.lanes.changed[self.sid] = time.monotonic() - 0.28
        self.assertEqual(self.lanes.ack(self.sid), "blocked")
        self.assertEqual(self.lanes.state[self.sid], "blocked")
        self.assertIn(self.sid, self.lanes.acked)
        self.assertEqual(self.frame()[self.slot], (255, 0, 0))

    def test_the_lane_view_reports_acked(self):
        self.lanes.set_state(self.sid, "blocked")
        self.lanes.ack(self.sid)
        view = self.daemon.lane_view(self.sid, self.slot)
        self.assertTrue(view["acked"])

    def test_a_new_state_clears_an_ack(self):
        self.lanes.set_state(self.sid, "blocked")
        self.lanes.ack(self.sid)
        self.lanes.set_state(self.sid, "working")
        self.assertNotIn(self.sid, self.lanes.acked)
        self.assertFalse(self.daemon.lane_view(self.sid, self.slot)["acked"])

    # -- stale -----------------------------------------------------------
    def test_stale_annotation_tracks_activity(self):
        self.lanes.set_state(self.sid, "working")
        self.lanes.changed[self.sid] = time.monotonic() - 120
        self.lanes.updated[self.sid] = time.time() - 120
        view = self.daemon.lane_view(self.sid, self.slot)
        self.assertTrue(view["stale"])
        self.assertGreater(view["stale_s"], 60)
        self.daemon.lanes.set_info(self.sid, {"heartbeat": time.time()})
        view = self.daemon.lane_view(self.sid, self.slot)
        self.assertFalse(view["stale"])

    def test_idle_lanes_are_never_stale(self):
        self.lanes.set_state(self.sid, "idle")
        self.lanes.updated[self.sid] = time.time() - 99999
        self.assertFalse(self.daemon.lane_view(self.sid, self.slot)["stale"])

    def test_stale_and_recovered_events_are_published_once_each(self):
        seen = []
        self.daemon.hub.subscribe(lambda event: seen.append(event.kind))
        self.lanes.set_state(self.sid, "working")
        self.lanes.changed[self.sid] = time.monotonic() - 120
        self.lanes.updated[self.sid] = time.time() - 120
        self.daemon.poll_subsystems()
        self.daemon.poll_subsystems()
        self.assertEqual(seen.count("stale"), 1)
        self.daemon.lanes.set_info(self.sid, {"heartbeat": time.time()})
        self.daemon.poll_subsystems()
        self.assertEqual(seen.count("recovered"), 1)

    # -- aggregate and history -------------------------------------------
    def test_aggregate_counts_and_lists_blocked_waits(self):
        self.lanes.claim("test:2", "tester", "other", "host")
        self.lanes.set_state(self.sid, "working")
        self.lanes.set_state("test:2", "blocked")
        result = self.daemon.aggregate()
        self.assertEqual(result["counts"]["working"], 1)
        self.assertEqual(result["counts"]["blocked"], 1)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(len(result["blocked"]), 1)
        self.assertEqual(result["blocked"][0]["session"], "test:2")

    def test_history_tail_reads_jsonl_and_respects_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": 100.0, "state": "working"}) + "\n")
                fh.write("not json\n")
                fh.write(json.dumps({"at": 200.0, "state": "done"}) + "\n")
            self.daemon.history_path = path
            self.assertEqual(len(self.daemon.history_tail()), 2)
            entries = self.daemon.history_tail(since=150.0)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["state"], "done")

    def test_notifier_and_history_subsystems_wire_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "note.json")
            self.daemon.history_path = os.path.join(tmp, "events.jsonl")
            self.daemon.config["history"] = {"enabled": True, "max_bytes": 100_000}
            self.daemon.config["notify"] = {
                "enabled": True, "states": ["blocked"], "min_duration_s": 0.0,
                "cooldown_s": 0.0, "repeat_s": 0.0, "quiet_hours": "",
                "dry_run": False,
                "command": [
                    sys.executable, "-c",
                    "import sys,pathlib; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())",
                    marker,
                ],
            }
            self.daemon.start_subsystems()
            try:
                self.lanes.set_state(self.sid, "blocked")
                self.daemon.publish("state", self.sid, self.slot)
                deadline = time.time() + 4
                while time.time() < deadline and not os.path.exists(marker):
                    self.daemon.poll_subsystems()
                    time.sleep(0.05)
                self.assertTrue(os.path.exists(marker),
                                "the notifier command never ran")
                with open(marker, encoding="utf-8") as fh:
                    payload = json.load(fh)
                self.assertEqual(payload.get("session"), self.sid)
                self.assertEqual(payload.get("state"), "blocked")
                self.assertTrue(os.path.exists(self.daemon.history_path))
                with open(self.daemon.history_path, encoding="utf-8") as fh:
                    recorded = [json.loads(line) for line in fh if line.strip()]
                self.assertTrue(any(entry.get("session") == self.sid
                                    for entry in recorded))
            finally:
                self.daemon.stop_subsystems()

    def test_native_hook_events_drive_attention_by_id(self):
        from rgi.ingest import normalize
        base = {"session_id": "cc-9", "cwd": "/repo"}

        def apply(payload):
            self.daemon.apply_actions(
                normalize("claude-code", {**base, **payload}), source="claude-code")

        apply({"hook_event_name": "SessionStart"})
        sid = "claude-code:cc-9"
        self.assertEqual(self.lanes.state[sid], "working")
        apply({"hook_event_name": "Notification",
               "notification_type": "permission_prompt",
               "tool_name": "Bash", "tool_use_id": "tu-9",
               "message": "needs permission"})
        self.assertEqual(self.lanes.state[sid], "blocked")
        self.assertEqual(self.lanes.info[sid]["blocked_on"]["id"], "tu-9")
        apply({"hook_event_name": "PostToolUse", "tool_name": "Bash",
               "tool_use_id": "tu-9", "tool_response": {"stdout": "ok"}})
        self.assertEqual(self.lanes.state[sid], "working")
        self.assertIsNone(self.lanes.info[sid].get("blocked_on"))
        apply({"hook_event_name": "Stop", "stop_hook_active": False})
        self.assertEqual(self.lanes.state[sid], "done")
        apply({"hook_event_name": "SessionEnd"})
        self.assertNotIn(sid, self.lanes.slot)

    def test_apply_actions_claims_and_moves_a_lane(self):
        class Action:
            def __init__(self, **fields):
                self.__dict__.update(fields)
                self.meta = fields.get("meta", {})

        actions = [
            Action(op="begin", session="hook:1", agent="claude-code", label="hook task"),
            Action(op="state", session="hook:1", state="working"),
            Action(op="info", session="hook:1", info={"repo": "x"}),
            Action(op="end", session="hook:1"),
        ]
        self.assertEqual(self.daemon.apply_actions(actions, source="claude-code"), 4)
        self.assertNotIn("hook:1", self.lanes.slot)

    def test_delivery_ids_dedupe_actions(self):
        class Action:
            session = self.sid

            def __init__(self):
                self.op = "state"
                self.state = "blocked"
                self.agent = "test"
                self.label = None
                self.info = {}
                self.request = None
                self.slot = None
                self.meta = {"delivery": "abc-123"}

        self.assertEqual(self.daemon.apply_actions([Action()]), 1)
        self.assertEqual(self.daemon.apply_actions([Action()]), 0)


if __name__ == "__main__":
    unittest.main()
