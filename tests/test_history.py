"""The history ring and the digest: appends, rotation, bad lines, arithmetic.

Everything is a temporary file and a real EventHub; the tests pin the exact
record shape (so a scrolling ``jq`` keeps working), tolerate torn lines, and
check that spend is never double-counted across many events and that an open
blocked wait grows to ``now``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from rgi.events import EventHub, LaneEvent
from rgi.history import FIELDS, HistoryLog, digest, text


def lane(state: str, **overrides) -> dict:
    data = {
        "slot": 0, "state": state, "label": "task", "host": "ws",
        "ident": "machine-1", "agent": "opencode", "key": "F1", "info": {},
    }
    data.update(overrides)
    return data


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "history.jsonl")
        self.hub = EventHub()
        self.log = HistoryLog(self.path, hub=self.hub)

    def publish(self, kind: str, session: str, slot: int, row: dict,
                at: float) -> None:
        self.hub.publish(LaneEvent(kind=kind, session_id=session, slot=slot,
                                   lane=row, at=at))

    def lines(self) -> list[dict]:
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def write_rows(self, rows: list[dict]) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    # -- the log -----------------------------------------------------------
    def test_appends_one_bounded_line_per_event(self):
        self.publish("state", "opencode:one", 0, lane(
            "blocked", slot=0, host="ws", ident="machine-1", label="Fix tests",
            info={
                "tokens": {"input": 120, "output": 30, "cost": 0.42},
                "context": {"percent": 71.5},
                "prompt": "SECRET-PROMPT",
            }), 100.0)
        records = self.lines()
        self.assertEqual(len(records), 1)
        row = records[0]
        self.assertEqual(set(row), set(FIELDS))
        self.assertEqual(row["at"], 100.0)
        self.assertEqual(row["kind"], "state")
        self.assertEqual(row["session"], "opencode:one")
        self.assertEqual(row["slot"], 0)
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["host"], "ws")
        self.assertEqual(row["ident"], "machine-1")
        self.assertEqual(row["label"], "Fix tests")
        self.assertEqual(row["cost"], 0.42)
        self.assertEqual(row["tokens_in"], 120)
        self.assertEqual(row["tokens_out"], 30)
        self.assertEqual(row["context_percent"], 71.5)
        self.assertNotIn("SECRET", json.dumps(row))

    def test_missing_detail_is_null_not_dropped(self):
        self.publish("info", "a", 0, lane("working"), 5.0)
        row = self.lines()[0]
        self.assertIsNone(row["cost"])
        self.assertIsNone(row["tokens_in"])
        self.assertIsNone(row["tokens_out"])
        self.assertIsNone(row["context_percent"])

    def test_rotation_keeps_exactly_one_backup(self):
        log = HistoryLog(self.path, max_bytes=512)
        for index in range(40):
            log.handle(LaneEvent("state", f"s:{index}", 0,
                                 lane("working", changed_at=float(index)),
                                 at=float(index)))
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertLessEqual(os.path.getsize(self.path), 512)
        self.assertLessEqual(os.path.getsize(self.path + ".1"), 512)

    def test_a_broken_path_is_silently_ignored(self):
        log = HistoryLog(os.path.join(self.tmp.name, "missing", "dir", "h.jsonl"))
        log.handle(LaneEvent("state", "a", 0, lane("working"), at=1.0))
        # the file is created best-effort; either way nothing raises
        self.assertTrue(True)

    # -- the digest --------------------------------------------------------
    def test_malformed_lines_are_skipped(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("not json\n")
            fh.write("[1, 2, 3]\n")
            fh.write('{"at": "later", "kind": "state", "session": "a"}\n')
            fh.write("\n")
            fh.write(json.dumps({"at": 10.0, "kind": "state", "session": "a",
                                 "state": "done", "host": "ws"}) + "\n")
        summary = digest(self.path, now=20.0)
        self.assertEqual(summary["lanes"], 1)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["spend"], 0.0)

    def test_digest_counts_spend_and_longest_wait(self):
        self.write_rows([
            {"at": 100.0, "kind": "state", "session": "a", "state": "working",
             "host": "ws"},
            {"at": 105.0, "kind": "state", "session": "b", "state": "blocked",
             "host": "lap"},
            {"at": 110.0, "kind": "state", "session": "a", "state": "blocked",
             "host": "ws"},
            {"at": 130.0, "kind": "state", "session": "a", "state": "working",
             "host": "ws"},
            {"at": 140.0, "kind": "state", "session": "a", "state": "done",
             "host": "ws", "cost": 1.5},
            {"at": 150.0, "kind": "state", "session": "b", "state": "blocked",
             "host": "lap", "cost": 0.5},
        ])
        summary = digest(self.path, now=200.0)
        self.assertEqual(summary["lanes"], 2)
        self.assertEqual(summary["done"], 1)
        self.assertEqual(summary["error"], 0)
        self.assertEqual(summary["blocked"], 2)
        self.assertEqual(summary["blocked_open"], 1)
        self.assertEqual(summary["spend"], 2.0)          # latest per lane, summed
        self.assertEqual(summary["longest_wait_s"], 95.0)
        self.assertEqual(summary["by_host"], {"ws": 1, "lap": 1})

    def test_since_epoch_windows_transitions_and_waits(self):
        self.write_rows([
            {"at": 10.0, "kind": "state", "session": "a", "state": "done",
             "host": "ws", "cost": 1.0},
            {"at": 100.0, "kind": "state", "session": "b", "state": "blocked",
             "host": "lap"},
            {"at": 120.0, "kind": "state", "session": "b", "state": "working",
             "host": "lap"},
        ])
        all_time = digest(self.path, now=200.0)
        self.assertEqual(all_time["lanes"], 2)
        self.assertEqual(all_time["done"], 1)
        self.assertEqual(all_time["spend"], 1.0)

        recent = digest(self.path, since_epoch=50.0, now=200.0)
        self.assertEqual(recent["lanes"], 1)
        self.assertEqual(recent["done"], 0)
        self.assertEqual(recent["blocked"], 1)
        self.assertEqual(recent["blocked_open"], 0)
        self.assertEqual(recent["longest_wait_s"], 20.0)
        self.assertEqual(recent["spend"], 0.0)
        self.assertEqual(recent["by_host"], {"lap": 1})

    def test_open_wait_grows_to_now(self):
        self.write_rows([
            {"at": 100.0, "kind": "state", "session": "a", "state": "blocked",
             "host": "ws"},
        ])
        self.assertEqual(digest(self.path, now=190.0)["longest_wait_s"], 90.0)
        self.assertEqual(digest(self.path, now=100.0)["longest_wait_s"], 0.0)

    def test_end_closes_an_open_wait(self):
        self.write_rows([
            {"at": 100.0, "kind": "state", "session": "a", "state": "blocked",
             "host": "ws"},
            {"at": 130.0, "kind": "end", "session": "a", "state": "blocked",
             "host": "ws"},
        ])
        summary = digest(self.path, now=900.0)
        self.assertEqual(summary["blocked_open"], 0)
        self.assertEqual(summary["longest_wait_s"], 30.0)

    def test_missing_file_is_an_empty_digest(self):
        summary = digest(os.path.join(self.tmp.name, "absent.jsonl"), now=50.0)
        self.assertEqual(summary["lanes"], 0)
        self.assertEqual(summary["blocked_open"], 0)
        self.assertEqual(summary["by_host"], {})
        self.assertIn("no lane events", text(summary))

    def test_text_formats_a_short_digest(self):
        line = text({
            "lanes": 2, "done": 1, "error": 0, "blocked": 2,
            "blocked_open": 1, "spend": 4.21, "longest_wait_s": 750.0,
            "by_host": {"ws": 2, "lap": 1},
        })
        self.assertIn("2 lanes", line)
        self.assertIn("1 done", line)
        self.assertIn("2 blocked (1 open)", line)
        self.assertIn("$4.21", line)
        self.assertIn("longest wait 12m30s", line)
        self.assertIn("lap=1, ws=2", line)


if __name__ == "__main__":
    unittest.main()
