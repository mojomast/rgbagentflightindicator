"""The Claude Code command-hook adapter, end to end against the mock panel.

Each ``claude_code.hook()`` call is a separate process in reality, so the tests
never carry Python state across calls: every event must rebuild the lane and the
open waits from the payload, the panel, and ``RGI_STATE_DIR`` alone. The tests
drive the exact payload shapes from the official hooks reference
(https://code.claude.com/docs/en/hooks) and assert the two rules a hook cannot
break - exit 0 and an empty stdout - on every call.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from rgi import hooks
from rgi.integrations import claude_code
from tests.mock_panel import MockPanel

SESSION = "sess-1"
LANE = "claude-code:sess-1"


class ClaudeCodeHookTest(unittest.TestCase):
    def setUp(self):
        self.panel = MockPanel(token="tok", lanes=3).__enter__()
        self.addCleanup(self.panel.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "RGI_URL": self.panel.url,
            "RGI_TOKEN": "tok",
            "RGI_IDENT": "test-machine",
            "RGI_STATE_DIR": self.tmp.name,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers -----------------------------------------------------------
    def send(self, event: str, **fields) -> int:
        """One hook process: asserts exit 0 and empty stdout on the way out."""
        payload = {"hook_event_name": event, "session_id": SESSION, **fields}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = claude_code.hook(payload)
        self.assertEqual(out.getvalue(), "", "hooks must never write to stdout")
        self.assertEqual(code, 0, "hooks must always exit 0")
        return code

    def state(self) -> str | None:
        return self.panel.state_of(LANE)

    def pending(self) -> list:
        return self.panel.info_of(LANE).get("pending_requests") or []

    def children(self) -> list:
        return self.panel.info_of(LANE).get("children") or []

    def permission(self, message: str, **fields) -> int:
        return self.send("Notification", notification_type="permission_prompt",
                         message=message, **fields)

    # -- lifecycle ---------------------------------------------------------
    def test_a_prompt_works_and_a_stop_completes(self):
        self.send("UserPromptSubmit", prompt="do the thing")
        self.assertEqual(self.state(), "working")
        self.send("Stop", stop_hook_active=False)
        self.assertEqual(self.state(), "done")

    def test_activity_after_stop_reopens_the_lane(self):
        self.send("UserPromptSubmit", prompt="first")
        self.send("Stop")
        self.assertEqual(self.state(), "done")

        self.send("PostToolUse", tool_name="Read", tool_use_id="toolu_1",
                  tool_input={}, tool_response={})
        self.assertEqual(self.state(), "working")

        self.send("Stop")
        self.send("PreToolUse", tool_name="Bash", tool_use_id="toolu_2",
                  tool_input={"command": "ls"})
        self.assertEqual(self.state(), "working")

    def test_session_start_claims_a_labelled_working_lane(self):
        self.send("SessionStart", source="startup", cwd="/srv/proj")
        self.assertIn(LANE, self.panel.sessions)
        self.assertEqual(self.state(), "working")
        self.assertEqual(self.panel.sessions[LANE]["label"], "proj")

    def test_unknown_events_are_ignored(self):
        self.send("PreCompact", trigger="auto")
        self.assertNotIn(LANE, self.panel.sessions)

    # -- attention ---------------------------------------------------------
    def test_two_concurrent_permissions_stay_blocked_until_each_resolves(self):
        self.permission("Claude needs your permission to use Bash")
        bash_id = self.pending()[0]
        self.permission("Claude needs your permission to use Read")
        read_id = next(rid for rid in self.pending() if rid != bash_id)
        self.assertEqual(self.state(), "blocked")

        # the Bash tool reports back: Read's wait must still hold the lane
        self.send("PostToolUse", tool_name="Bash", tool_use_id="toolu_1",
                  tool_input={}, tool_response={})
        self.assertEqual(self.state(), "blocked")
        self.assertEqual(self.pending(), [read_id])

        self.send("PostToolUse", tool_name="Read", tool_use_id="toolu_2",
                  tool_input={}, tool_response={})
        self.assertEqual(self.state(), "working")
        self.assertEqual(self.pending(), [])

    def test_a_stable_payload_id_ties_the_wait_to_its_tool_call(self):
        self.permission("Claude needs your permission to use Bash",
                        tool_use_id="toolu_01ABC")
        self.assertEqual(self.pending(), ["toolu_01ABC"])
        self.assertEqual(self.state(), "blocked")

        # the id, not the name, decides which wait resolved
        self.send("PostToolUse", tool_name="WebFetch", tool_use_id="toolu_01ABC",
                  tool_input={}, tool_response={})
        self.assertEqual(self.state(), "working")
        self.assertEqual(self.pending(), [])

    def test_stop_resolves_an_open_wait(self):
        self.send("UserPromptSubmit", prompt="go")
        self.permission("Claude needs your permission to use Bash")
        self.assertEqual(self.state(), "blocked")
        self.send("Stop")
        self.assertEqual(self.state(), "done")
        self.assertEqual(self.pending(), [])

    def test_a_new_prompt_resolves_an_idle_prompt(self):
        self.send("UserPromptSubmit", prompt="go")
        self.send("Stop")
        self.send("Notification", notification_type="idle_prompt",
                  message="Claude is waiting for your input")
        self.assertEqual(self.state(), "blocked")

        self.send("UserPromptSubmit", prompt="next")
        self.assertEqual(self.state(), "working")
        self.assertEqual(self.pending(), [])

    def test_a_legacy_notification_without_a_type_still_blocks(self):
        self.send("Notification", title="Permission needed",
                  message="Claude needs your permission to use Bash")
        self.assertEqual(self.state(), "blocked")
        self.assertEqual(len(self.pending()), 1)

    def test_non_wait_notifications_are_ignored(self):
        self.send("Notification", notification_type="auth_success",
                  message="Authenticated")
        self.assertNotIn(LANE, self.panel.sessions)

    def test_a_resumed_session_starts_clean(self):
        self.permission("Claude needs your permission to use Bash")
        self.assertEqual(self.state(), "blocked")
        self.send("SessionStart", source="resume", cwd="/srv/proj")
        self.assertEqual(self.state(), "working")
        self.assertEqual(self.pending(), [])

    # -- children and release ---------------------------------------------
    def test_subagent_stop_reports_the_child_done(self):
        self.send("UserPromptSubmit", prompt="go")
        self.send("SubagentStop", stop_hook_active=False, agent_id="agent-abc123",
                  agent_type="Explore",
                  agent_transcript_path="/srv/.claude/subagents/agent-abc123.jsonl")
        self.assertEqual([c["id"] for c in self.children()], ["agent-abc123"])
        self.assertEqual(self.children()[0]["state"], "done")

    def test_session_end_resolves_every_wait_and_releases_the_lane(self):
        self.send("SessionStart", source="startup", cwd="/srv/proj")
        self.permission("Claude needs your permission to use Bash")
        self.assertEqual(self.state(), "blocked")

        self.send("SessionEnd", reason="prompt_input_exit")
        self.assertNotIn(LANE, self.panel.sessions)

    # -- the absolute rules ------------------------------------------------
    def test_malformed_payloads_return_zero_without_touching_the_panel(self):
        for payload in ({}, {"hook_event_name": "Stop"}, {"session_id": "s"},
                        {"hook_event_name": "Stop", "session_id": ""},
                        {"hook_event_name": "Stop", "session_id": 42},
                        {"hook_event_name": ["Stop"], "session_id": "s"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = claude_code.hook(payload)
            self.assertEqual(code, 0, payload)
            self.assertEqual(out.getvalue(), "", payload)
        for payload in (None, ["Stop"], "Stop", 3):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = claude_code.hook(payload)
            self.assertEqual(code, 0, payload)
            self.assertEqual(out.getvalue(), "", payload)
        self.assertEqual(self.panel.sessions, {})

    def test_a_panel_that_is_down_is_survivable_and_silent(self):
        self.panel.stop()               # every call below now times out
        self.send("SessionStart", source="startup")
        self.permission("Claude needs your permission to use Bash")
        self.send("SessionEnd", reason="other")

    def test_the_cli_route_survives_garbage_on_stdin(self):
        for raw in ("not json at all", "", "[]", '"a string"'):
            out = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(raw)), \
                    contextlib.redirect_stdout(out):
                code = hooks.run("claude-code")
            self.assertEqual(code, 0, raw)
            self.assertEqual(out.getvalue(), "", raw)
        self.assertEqual(self.panel.sessions, {})


if __name__ == "__main__":
    unittest.main()
