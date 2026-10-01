"""The shared reporter: the promises every integration leans on.

These run against a real (mock) HTTP panel rather than a patched transport, so
they exercise the same urllib path the integrations use.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from rgi import config
from rgi.report import Reporter, canonical_state, scrub
from tests.mock_panel import MockPanel


class ReporterSetup(unittest.TestCase):
    def setUp(self):
        self.panel = MockPanel(token="tok", lanes=3).__enter__()
        self.addCleanup(self.panel.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.made: list[Reporter] = []

    def reporter(self, session="s1", **kwargs) -> Reporter:
        kwargs.setdefault("ident", "test-machine")
        reporter = Reporter("test", session, url=self.panel.url, token="tok",
                            state_dir=os.path.join(self.tmp.name, "state"),
                            **kwargs)
        self.made.append(reporter)
        return reporter

    def tearDown(self):
        for reporter in self.made:
            reporter.close()


class LaneLifecycleTest(ReporterSetup):
    def test_start_claims_a_free_slot_by_number(self):
        first = self.reporter("a")
        self.assertTrue(first.start())
        self.assertEqual(first.lane.slot, 0)
        claims = self.panel.requests_for("/session/start")
        self.assertEqual(len(claims), 1)
        # the slot was asked for by number: the daemon's LRU eviction never runs
        self.assertEqual(claims[0][2]["slot"], 0)
        self.assertEqual(claims[0][2]["ident"], "test-machine")

    def test_a_new_adapter_never_evicts_a_blocked_lane(self):
        with MockPanel(token="tok", lanes=1) as tiny:
            held = Reporter("test", "holder", url=tiny.url, token="tok",
                            ident="test-machine",
                            state_dir=os.path.join(self.tmp.name, "state"))
            late = Reporter("test", "late", url=tiny.url, token="tok",
                            ident="test-machine",
                            state_dir=os.path.join(self.tmp.name, "state"))
            self.made.extend([held, late])
            self.assertTrue(held.start())
            self.assertTrue(held.blocked(request="approval", message="waiting"))
            self.assertFalse(late.start())                # refuses, does not evict
            self.assertEqual(len(tiny.requests_for("/session/start")), 1)
            self.assertEqual(tiny.state_of("test:holder"), "blocked")
            self.assertIn("test:holder", tiny.sessions)

    def test_states_are_deduplicated(self):
        reporter = self.reporter()
        self.assertTrue(reporter.working())
        self.assertTrue(reporter.working())
        self.assertEqual(len(self.panel.requests_for("/session/state")), 1)
        self.assertTrue(reporter.done())
        self.assertEqual(len(self.panel.requests_for("/session/state")), 2)
        self.assertEqual(self.panel.state_of(reporter.session_key), "done")

    def test_an_older_event_cannot_overwrite_a_newer_one(self):
        reporter = self.reporter()
        self.assertTrue(reporter.working(at=1000.0))
        self.assertFalse(reporter.done(at=999.0))              # out of order
        self.assertEqual(self.panel.state_of(reporter.session_key), "working")
        self.assertEqual(len(self.panel.requests_for("/session/state")), 1)
        # and the dropped event left no trace: the next real completion works
        self.assertTrue(reporter.done(at=1001.0))
        self.assertEqual(self.panel.state_of(reporter.session_key), "done")

    def test_the_lane_is_reclaimed_after_a_daemon_restart(self):
        reporter = self.reporter()
        self.assertTrue(reporter.working())
        # the daemon restarts: every lane is gone, but the reporter is still here
        with self.panel.lock:
            self.panel.sessions.clear()
        self.assertTrue(reporter.done())                       # re-claims, then sets
        self.assertEqual(self.panel.state_of(reporter.session_key), "done")

    def test_heartbeat_recovers_a_lost_lane(self):
        reporter = self.reporter()
        self.assertTrue(reporter.working())
        with self.panel.lock:
            self.panel.sessions.clear()
        self.assertTrue(reporter.heartbeat())
        self.assertIsNotNone(reporter.lane)

    def test_done_with_a_grace_releases_the_lane_by_itself(self):
        reporter = self.reporter(release_grace=0.05)
        self.assertTrue(reporter.done())
        time.sleep(0.25)
        self.assertNotIn(reporter.session_key, self.panel.sessions)

    def test_end_is_safe_twice_and_after_a_restart(self):
        reporter = self.reporter()
        self.assertTrue(reporter.working())
        self.assertTrue(reporter.end())
        self.assertTrue(reporter.end())
        with self.panel.lock:
            self.panel.sessions.clear()
        self.assertTrue(reporter.end())


class AttentionTest(ReporterSetup):
    def test_two_concurrent_waits_stay_blocked_until_both_resolve(self):
        reporter = self.reporter()
        self.assertTrue(reporter.working())
        self.assertTrue(reporter.blocked(request="perm-1", action="bash",
                                         message="rm -rf build?"))
        self.assertTrue(reporter.blocked(request="perm-2", action="write",
                                         message="edit config?"))
        self.assertEqual(self.panel.state_of(reporter.session_key), "blocked")
        self.assertEqual(reporter.pending, ["perm-1", "perm-2"])
        info = self.panel.info_of(reporter.session_key)
        self.assertEqual(info["pending_requests"], ["perm-1", "perm-2"])

        self.assertTrue(reporter.resolve("perm-1"))
        self.assertEqual(self.panel.state_of(reporter.session_key), "blocked")
        self.assertTrue(reporter.resolve("perm-2"))
        # back to the state we were in before the wait, not to idle
        self.assertEqual(self.panel.state_of(reporter.session_key), "working")

    def test_a_completed_result_with_an_open_wait_is_still_blocked(self):
        reporter = self.reporter()
        reporter.working()
        reporter.blocked(request="approval", message="approve the deploy?")
        reporter.done()
        self.assertEqual(self.panel.state_of(reporter.session_key), "blocked")
        reporter.resolve("approval")
        self.assertEqual(self.panel.state_of(reporter.session_key), "done")

    def test_a_quiet_wait_is_not_stale(self):
        """Blocked lanes are never freed by the daemon, whatever the clock says."""
        reporter = self.reporter()
        reporter.blocked(request="long", message="human is thinking")
        stamp = os.path.getmtime(reporter._stamp_path())
        time.sleep(0.05)
        self.assertEqual(self.panel.state_of(reporter.session_key), "blocked")
        self.assertTrue(os.path.getmtime(reporter._stamp_path()) >= stamp)
        self.assertEqual(reporter.pending, ["long"])


class MetadataTest(ReporterSetup):
    def test_children_are_metadata_and_never_claim_a_lamp(self):
        reporter = self.reporter()
        reporter.working()
        self.assertTrue(reporter.child("sub-1", label="research", tokens=1200))
        self.assertTrue(reporter.child("sub-2", label="write tests"))
        self.assertEqual(len(self.panel.sessions), 1)
        children = self.panel.info_of(reporter.session_key)["children"]
        self.assertEqual([c["id"] for c in children], ["sub-1", "sub-2"])
        self.assertEqual(children[0]["tokens"], 1200)
        reporter.child_done("sub-1")
        children = self.panel.info_of(reporter.session_key)["children"]
        self.assertEqual(children[0]["state"], "done")

    def test_metadata_is_scrubbed_and_prompts_are_dropped(self):
        reporter = self.reporter()
        reporter.working()
        reporter.info({
            "note": "api_key=abcdef123456",
            "tokens": {"input": 10, "output": 5},
            "arguments": {"prompt": "the whole conversation"},
        })
        info = self.panel.info_of(reporter.session_key)
        self.assertNotIn("abcdef123456", info["note"])
        self.assertIn("[redacted]", info["note"])
        self.assertEqual(info["tokens"]["input"], 10)
        self.assertNotIn("prompt", info.get("arguments", {}))

    def test_scrub_redacts_the_usual_credentials(self):
        for text in ("sk-abcdefghijklmnop", "ghp_ABCDEFGHIJKLMNOPQRST",
                     "Authorization: Bearer abcdef", "password: hunter2"):
            self.assertIn("[redacted]", scrub(text), text)
        self.assertLessEqual(len(scrub("x" * 500)), 160)

    def test_canonical_state_maps_a_harnesss_vocabulary(self):
        self.assertEqual(canonical_state("running"), "working")
        self.assertEqual(canonical_state("task_complete"), "done")
        self.assertEqual(canonical_state("needs_input"), "blocked")
        self.assertEqual(canonical_state("working"), "working")
        self.assertIsNone(canonical_state("sideways"))


class FailureTest(ReporterSetup):
    def test_a_gone_daemon_is_survivable_and_never_raises(self):
        reporter = self.reporter()
        reporter.working()
        self.panel.stop()
        # a repeated state is a no-op; a change reports the failure without raising
        self.assertTrue(reporter.working())
        self.assertFalse(reporter.done())
        self.assertFalse(reporter.info({"note": "still alive"}))
        self.assertFalse(reporter.blocked(request="x"))
        self.assertFalse(reporter.heartbeat())

    def test_a_wrong_token_is_handled_once_and_silently(self):
        logs: list[str] = []
        reporter = Reporter("test", "s3", url=self.panel.url, token="wrong",
                            ident="test-machine", log=logs.append,
                            state_dir=os.path.join(self.tmp.name, "state"))
        self.made.append(reporter)
        self.assertFalse(reporter.working())
        self.assertFalse(reporter.working())
        auth_lines = [m for m in logs if "panel_auth" in m]
        self.assertEqual(len(auth_lines), 1)                  # logged once, not twice

    def test_the_token_never_appears_in_logs(self):
        logs: list[str] = []
        reporter = Reporter("test", "s4", url=self.panel.url, token="sup3rs3cret",
                            ident="test-machine", log=logs.append,
                            state_dir=os.path.join(self.tmp.name, "state"))
        self.made.append(reporter)
        reporter.working()
        self.assertFalse(any("sup3rs3cret" in line for line in logs))


class KeepaliveTest(ReporterSetup):
    def test_keepalive_heartbeats_in_the_background_and_stops(self):
        reporter = self.reporter()
        reporter.working()
        with reporter.keepalive(interval=0.05):
            # Wait for the first heartbeat rather than assuming a fixed sleep is
            # enough: under a loaded suite, 0.2 s is not a promise.
            deadline = time.time() + 5.0
            while not self.panel.requests_for("/session/info") and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.panel.requests_for("/session/info"),
                            "no heartbeat arrived within five seconds")
        during = len(self.panel.requests_for("/session/info"))
        time.sleep(0.2)
        self.assertEqual(len(self.panel.requests_for("/session/info")), during)


class ConfigTest(unittest.TestCase):
    def test_the_identifier_falls_back_to_the_machine(self):
        self.assertTrue(config.resolve_ident())
        self.assertEqual(config.resolve_ident("named"), "named")


if __name__ == "__main__":
    unittest.main()
