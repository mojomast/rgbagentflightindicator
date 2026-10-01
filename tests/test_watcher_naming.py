"""A machine's name reaches its existing lanes too.

Lanes are named when they are created, and a re-claim never renames one - so a
machine that updates its watcher keeps nameless lanes until the daemon restarts.
The watcher adopts them on the next reconcile instead, and leaves a personal
name alone.
"""

from __future__ import annotations

import unittest

from rgi.watchers import opencode

ROOT = "ses_root00000000000000000000001"
CHILD = "ses_child0000000000000000000002"
NAMED = "ses_named0000000000000000000003"


class FakeWatcher(opencode.Watcher):
    def __init__(self, ident: str = "brrr"):
        super().__init__(url="http://127.0.0.1:1", ident=ident)
        self.posts: list[tuple[str, dict]] = []
        self.ok = True

    def post(self, path: str, payload: dict):
        self.posts.append((path, payload))
        return {"ok": self.ok}


def lane(ident=None) -> dict:
    return {"slot": 1, "key": "1", "state": "working", "ident": ident}


def bound(*, ignored: bool = False) -> dict:
    return {"slot": 1, "key": "1", "state": "working", "stopped_at": None,
            "done_at": None, "ignored": ignored}


class NameLanesTest(unittest.TestCase):
    def test_a_nameless_lane_gets_the_machine_name(self):
        watcher = FakeWatcher("brrr")
        watcher.bound[ROOT] = bound()
        watcher.name_lanes({ROOT: lane()})
        self.assertEqual(watcher.posts,
                         [("/session/info", {"sessionID": ROOT, "ident": "brrr"})])

    def test_a_personal_name_is_left_alone(self):
        watcher = FakeWatcher("brrr")
        watcher.bound[NAMED] = bound()
        watcher.name_lanes({NAMED: lane("Clanker01")})
        self.assertEqual(watcher.posts, [])

    def test_a_child_is_not_named(self):
        watcher = FakeWatcher("brrr")
        watcher.bound[CHILD] = bound(ignored=True)
        watcher.name_lanes({CHILD: lane()})
        self.assertEqual(watcher.posts, [])

    def test_a_lane_the_panel_does_not_have_is_skipped(self):
        watcher = FakeWatcher("brrr")
        watcher.bound[ROOT] = bound()
        watcher.name_lanes({})               # reconcile re-claims it separately
        self.assertEqual(watcher.posts, [])

    def test_a_refused_write_is_silent_and_harmless(self):
        watcher = FakeWatcher("brrr")
        watcher.ok = False
        watcher.bound[ROOT] = bound()
        watcher.name_lanes({ROOT: lane()})   # no exception, just no rename
        self.assertEqual(len(watcher.posts), 1)

    def test_the_name_comes_from_the_watcher_s_own_resolution(self):
        watcher = FakeWatcher("kimi")
        watcher.bound[ROOT] = bound()
        watcher.name_lanes({ROOT: lane()})
        self.assertEqual(watcher.posts[0][1]["ident"], "kimi")


if __name__ == "__main__":
    unittest.main()
