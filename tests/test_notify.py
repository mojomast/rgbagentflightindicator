"""The attention notifier, driven through a real EventHub and a fake clock.

A fake clock makes the min-duration, cooldown, repeat and quiet-hours maths
exact; an injected runner captures deliveries without spawning anything. The
cases are the ones that decide whether this feature is usable at all: fire once
per wait, never after a resolution, never at 3am for an error, and never let a
broken command touch the event stream.
"""

from __future__ import annotations

import json
import time
import unittest

from rgi.events import EventHub, LaneEvent
from rgi.notify import Notifier, in_quiet_hours, parse_quiet_hours


class Clock:
    def __init__(self, now: float):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class Recorder:
    """Stands in for the user's command; records argv, stdin JSON and env."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail = False

    def __call__(self, argv, payload, env):
        if self.fail:
            raise RuntimeError("delivery broke")
        self.calls.append((list(argv), json.loads(payload), dict(env)))
        return 0


def lane(state: str, **overrides) -> dict:
    data = {
        "slot": 0, "state": state, "changed_at": 1000.0,
        "label": "Fix tests", "ident": "machine-1", "host": "workstation",
        "agent": "opencode", "key": "F1", "acked": False, "info": {},
    }
    data.update(overrides)
    return data


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.hub = EventHub()
        self.clock = Clock(1000.0)
        self.recorder = Recorder()
        self.logs: list[tuple[str, str]] = []
        self.notifier: Notifier | None = None

    def make(self, command=("notify-tool",), **kwargs) -> Notifier:
        options = {
            "clock": self.clock,
            "runner": self.recorder,
            "log": lambda level, message: self.logs.append((level, message)),
        }
        options.update(kwargs)
        self.notifier = Notifier(
            self.hub, None if command is None else list(command), **options)
        self.addCleanup(self.notifier.stop)
        return self.notifier

    def publish(self, state, *, session="opencode:one", slot=0, kind="state",
                at=None, **lane_overrides):
        at = self.clock.now if at is None else at
        row = lane(state, slot=slot, **lane_overrides)
        self.hub.publish(LaneEvent(kind=kind, session_id=session, slot=slot,
                                   lane=row, at=at))
        return row

    def flush(self) -> None:
        assert self.notifier is not None
        self.notifier.join(2.0)

    # -- delivery ----------------------------------------------------------
    def test_blocked_fires_once_with_payload_and_env(self):
        self.make(min_duration_s=0)
        self.publish(
            "blocked", changed_at=999.5,
            info={"blocked_on": {"action": "approve", "message": "Allow write?"}})
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)
        argv, payload, env = self.recorder.calls[0]
        self.assertEqual(argv, ["notify-tool"])
        self.assertEqual(payload["event"], "state")
        self.assertEqual(payload["session"], "opencode:one")
        self.assertEqual(payload["state"], "blocked")
        self.assertEqual(payload["slot"], 0)
        self.assertEqual(payload["ident"], "machine-1")
        self.assertEqual(payload["label"], "Fix tests")
        self.assertEqual(payload["host"], "workstation")
        self.assertEqual(payload["what"], "approve: Allow write?")
        self.assertEqual(env["RGI_EVENT"], "state")
        self.assertEqual(env["RGI_SESSION"], "opencode:one")
        self.assertEqual(env["RGI_STATE"], "blocked")
        self.assertEqual(env["RGI_SLOT"], "0")
        self.assertEqual(env["RGI_IDENT"], "machine-1")
        self.assertEqual(env["RGI_LABEL"], "Fix tests")
        self.assertEqual(env["RGI_HOST"], "workstation")
        self.assertEqual(env["RGI_WHAT"], "approve: Allow write?")

    def test_ack_suppresses_while_the_wait_is_acked(self):
        self.make(min_duration_s=5)
        self.publish("blocked")
        self.publish("blocked", kind="ack", acked=True)
        self.clock.advance(100)
        self.notifier.poll()
        self.flush()
        self.assertEqual(self.recorder.calls, [])
        self.assertTrue(any("acknowledged" in message
                            for _, message in self.logs))

    def test_lane_already_acked_never_queues(self):
        self.make(min_duration_s=0)
        self.publish("blocked", acked=True)
        self.flush()
        self.assertEqual(self.recorder.calls, [])

    def test_custom_states_fire_on_done_only(self):
        self.make(min_duration_s=0, states=("done",))
        self.publish("blocked")
        self.publish("done", session="a", slot=0)
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertEqual(self.recorder.calls[0][1]["state"], "done")

    def test_repeated_state_does_not_refire(self):
        self.make(min_duration_s=0)
        self.publish("blocked", info={"blocked_on": {"message": "first"}})
        self.publish("blocked", info={"blocked_on": {"message": "second"}})
        self.publish("blocked", kind="info")
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)

    def test_transition_to_working_cancels(self):
        self.make(min_duration_s=5)
        self.publish("blocked")
        self.publish("working")
        self.clock.advance(100)
        self.notifier.poll()
        self.flush()
        self.assertEqual(self.recorder.calls, [])

    def test_end_cancels_pending(self):
        self.make(min_duration_s=5)
        self.publish("blocked")
        self.publish("blocked", kind="end")
        self.clock.advance(100)
        self.notifier.poll()
        self.flush()
        self.assertEqual(self.recorder.calls, [])

    def test_command_is_argv_and_missing_command_is_log_only(self):
        self.make(command=None, min_duration_s=0)
        self.publish("blocked")
        self.flush()
        self.assertEqual(self.recorder.calls, [])
        self.assertTrue(any("no command" in message for _, message in self.logs))

    def test_command_failure_is_swallowed(self):
        self.recorder.fail = True
        self.make(min_duration_s=0)
        self.publish("blocked")
        self.hub.publish(LaneEvent("state", "opencode:two", 1,
                                   lane("blocked", slot=1), at=self.clock.now))
        self.flush()
        self.assertTrue(any(level == "error" and "failed" in message
                            for level, message in self.logs))

    # -- the clockwork -----------------------------------------------------
    def test_min_duration_holds_then_fires(self):
        self.make(min_duration_s=3)
        self.publish("blocked", changed_at=self.clock.now)
        self.notifier.poll()
        self.assertEqual(self.recorder.calls, [])
        self.clock.advance(2.9)
        self.notifier.poll()
        self.assertEqual(self.recorder.calls, [])
        self.clock.advance(0.2)
        self.notifier.poll()
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)

    def test_global_cooldown_defers_the_second(self):
        self.make(min_duration_s=0, cooldown_s=30)
        self.publish("blocked", session="a", slot=0)
        self.publish("blocked", session="b", slot=1)
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)
        self.clock.advance(10)
        self.notifier.poll()
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)
        self.clock.advance(25)
        self.notifier.poll()
        self.flush()
        self.assertEqual(len(self.recorder.calls), 2)

    def test_one_repeat_while_the_wait_persists(self):
        self.make(min_duration_s=0, repeat_s=60, cooldown_s=0)
        self.publish("blocked", changed_at=self.clock.now)
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)
        self.clock.advance(59)
        self.notifier.poll()
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)
        self.clock.advance(1)
        self.notifier.poll()
        self.flush()
        self.assertEqual(len(self.recorder.calls), 2)
        self.clock.advance(600)
        self.notifier.poll()
        self.flush()
        self.assertEqual(len(self.recorder.calls), 2)   # capped: exactly one

    def test_resolution_cancels_the_repeat(self):
        self.make(min_duration_s=0, repeat_s=60, cooldown_s=0)
        self.publish("blocked")
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)
        self.publish("working")
        self.clock.advance(600)
        self.notifier.poll()
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)

    # -- quiet hours and dry run ------------------------------------------
    def test_quiet_hours_suppress_error_but_blocked_bypasses(self):
        self.make(min_duration_s=0, quiet_hours="22:00-07:00")
        self.clock.now = time.mktime((2026, 1, 1, 23, 0, 0, 0, 0, -1))
        self.publish("error", session="a", slot=0, changed_at=self.clock.now)
        self.flush()
        self.assertEqual(self.recorder.calls, [])
        blocked_at = self.clock.now
        self.publish("blocked", session="b", slot=1, changed_at=blocked_at)
        self.clock.advance(31)                       # past any cooldown
        self.publish("blocked", session="b", slot=1, changed_at=blocked_at,
                     info={"blocked_on": {"message": "still waiting"}})
        self.flush()
        self.assertEqual(len(self.recorder.calls), 1)
        self.assertTrue(any("quiet" in message for _, message in self.logs))

    def test_quiet_hour_ranges_parse_and_wrap_midnight(self):
        ranges = parse_quiet_hours("22:00-07:00, 12:30-13:00")
        self.assertEqual(len(ranges), 2)
        midnight = time.mktime((2026, 1, 1, 0, 30, 0, 0, 0, -1))
        morning = time.mktime((2026, 1, 1, 7, 0, 0, 0, 0, -1))
        lunch = time.mktime((2026, 1, 1, 12, 45, 0, 0, 0, -1))
        afternoon = time.mktime((2026, 1, 1, 15, 0, 0, 0, 0, -1))
        self.assertTrue(in_quiet_hours(ranges, midnight))
        self.assertFalse(in_quiet_hours(ranges, morning))
        self.assertTrue(in_quiet_hours(ranges, lunch))
        self.assertFalse(in_quiet_hours(ranges, afternoon))
        self.assertEqual(parse_quiet_hours("nonsense"), [])
        self.assertFalse(in_quiet_hours([], afternoon))

    def test_dry_run_runs_nothing(self):
        self.make(min_duration_s=0, dry_run=True)
        self.publish("blocked")
        self.flush()
        self.assertEqual(self.recorder.calls, [])
        self.assertTrue(any("dry-run" in message for _, message in self.logs))

    # -- failure isolation -------------------------------------------------
    def test_runner_failure_never_reaches_the_hub(self):
        self.recorder.fail = True
        self.make(min_duration_s=0)
        self.publish("blocked")                # would raise if isolation broke
        self.assertEqual(len(self.hub._subscribers), 1)
        self.flush()
        self.assertTrue(any("failed" in message for _, message in self.logs))


if __name__ == "__main__":
    unittest.main()
