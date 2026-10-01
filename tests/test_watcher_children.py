"""A subagent must never hold a lamp.

The OpenCode API reports a child session with ``parentID = ses_<parent>``; the
watcher learns that from ``/api/session``, which it refreshes every twenty
seconds. A subagent can become active inside that window - so the watcher asks
before it binds, and hands back a lamp if it ever bound one first.
"""

from __future__ import annotations

import time
import unittest

from rgi.watchers import opencode

ROOT = "ses_root00000000000000000000001"
CHILD = "ses_child0000000000000000000002"
OTHER = "ses_root00000000000000000000003"


def record(sid: str, parent: str | None = None, title: str = "x") -> dict:
    rec = {"id": sid, "title": title, "time": {"updated": 0}}
    if parent:
        rec["parentID"] = parent
    return rec


class FakeWatcher(opencode.Watcher):
    """A Watcher with the OpenCode CLI and the panel replaced by bookkeeping."""

    def __init__(self, metadata_rounds: list[list[dict]]):
        super().__init__(url="http://127.0.0.1:1")
        self.metadata_rounds = list(metadata_rounds)
        self.refreshes = 0
        self.bound_calls: list[str] = []
        self.freed_calls: list[tuple[str, str]] = []

    def cli(self, path: str):
        if path == "/api/session":
            self.refreshes += 1
            data = self.metadata_rounds.pop(0) if self.metadata_rounds else []
            return {"data": data}, None
        return {}, None

    def bind(self, sid: str, want: int | None = None) -> dict | None:
        self.bound_calls.append(sid)
        return {"slot": 0, "key": "0", "state": None, "stopped_at": None,
                "done_at": None, "ignored": False}

    def free(self, sid: str, reason: str) -> None:
        self.freed_calls.append((sid, reason))
        self.bound.pop(sid, None)


def lane(state: str = "working") -> dict:
    return {"slot": 3, "key": "3", "state": state, "stopped_at": None,
            "done_at": None, "ignored": False}


class ClassifyBeforeBindTest(unittest.TestCase):
    def test_a_child_that_starts_between_refreshes_is_classified_before_binding(self):
        watcher = FakeWatcher([
            [record(ROOT), record(OTHER)],                                # scheduled
            [record(ROOT), record(OTHER), record(CHILD, parent=ROOT)],    # on demand
        ])
        watcher.refresh_metadata()                   # the 20 s refresh, child unknown
        self.assertNotIn(CHILD, watcher.parents)

        watcher.classify_new({ROOT, OTHER, CHILD})
        self.assertEqual(watcher.refreshes, 2)       # it asked, once
        self.assertEqual(watcher.parents.get(CHILD), ROOT)   # and now it knows
        self.assertEqual(watcher.bound_calls, [])    # nothing was bound meanwhile

    def test_each_new_session_costs_one_ask_and_no_more(self):
        watcher = FakeWatcher([[record(ROOT)], [record(ROOT), record(CHILD, parent=ROOT)]])
        watcher.classify_new({ROOT, CHILD})
        self.assertEqual(watcher.refreshes, 1)
        watcher.classify_new({ROOT, CHILD, OTHER})   # OTHER is new; ROOT/CHILD are known
        self.assertEqual(watcher.refreshes, 2)
        watcher.classify_new({ROOT, CHILD, OTHER})   # nobody new
        self.assertEqual(watcher.refreshes, 2)

    def test_a_known_root_is_left_alone(self):
        watcher = FakeWatcher([[record(ROOT)]])
        watcher.refresh_metadata()
        watcher.classify_new({ROOT})
        self.assertEqual(watcher.refreshes, 1)
        self.assertFalse(watcher.parents.get(ROOT))


class ChildListingTest(unittest.TestCase):
    """What an uncollapsed lane shows: in-flight children, and no ghosts."""

    def watcher_with(self, *, active: bool, seen_ago: float, updated_ago: float):
        watcher = FakeWatcher([[]])
        watcher.children = {ROOT: [CHILD]}
        watcher._running = {CHILD} if active else set()
        watcher.records[CHILD] = {
            "id": CHILD, "title": "a long child task", "tokens": {"output": 7},
            "time": {"updated": (time.time() - updated_ago) * 1000},
        }
        watcher._child_seen[CHILD] = time.time() - seen_ago
        watcher._context = lambda sid, now: {}          # not the subject here
        watcher._running_tools = lambda sid, now: []
        return watcher

    def test_a_long_running_child_stays_in_flight(self):
        # Active now, and its record has not moved for an hour - the old filter
        # used that record and dropped it, which is exactly the bug.
        watcher = self.watcher_with(active=True, seen_ago=0, updated_ago=3600)
        info = watcher._lane_info(ROOT, time.time())
        self.assertEqual([c["id"] for c in info["children"]], [CHILD])

    def test_a_finished_child_drops_out_after_the_grace(self):
        watcher = self.watcher_with(active=False, seen_ago=opencode.CHILD_GRACE + 60,
                                    updated_ago=10)
        info = watcher._lane_info(ROOT, time.time())
        self.assertEqual(info["children"], [])

    def test_a_child_that_just_stopped_lingers_briefly(self):
        watcher = self.watcher_with(active=False, seen_ago=5, updated_ago=5)
        info = watcher._lane_info(ROOT, time.time())
        self.assertEqual([c["id"] for c in info["children"]], [CHILD])


class ReleaseSubagentTest(unittest.TestCase):
    def test_a_bound_child_gives_its_lamp_back(self):
        watcher = FakeWatcher([[]])
        watcher.parents[CHILD] = ROOT                # metadata has caught up
        watcher.bound[CHILD] = lane()
        watcher.bound[ROOT] = lane()
        watcher.release_subagents()
        self.assertEqual(watcher.freed_calls,
                         [(CHILD, "subagent - children are metadata, not lanes")])
        self.assertNotIn(CHILD, watcher.bound)
        self.assertIn(ROOT, watcher.bound)           # the root keeps its lane

    def test_include_subagents_is_still_honoured(self):
        watcher = FakeWatcher([[]])
        watcher.include_subagents = True
        watcher.parents[CHILD] = ROOT
        watcher.bound[CHILD] = lane()
        watcher.release_subagents()
        self.assertEqual(watcher.freed_calls, [])
        self.assertIn(CHILD, watcher.bound)

    def test_an_ignored_entry_is_not_freed_again(self):
        watcher = FakeWatcher([[]])
        watcher.parents[CHILD] = ROOT
        watcher.bound[CHILD] = {"slot": None, "key": None, "state": None,
                                "ignored": True, "stopped_at": None, "done_at": None}
        watcher.release_subagents()
        self.assertEqual(watcher.freed_calls, [])


if __name__ == "__main__":
    unittest.main()
