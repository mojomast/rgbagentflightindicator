"""The Gemini CLI hook: event map, silence, and cross-process semantics.

In real use every ``hook()`` call is its own short-lived process, so these
tests drive the public entry point with payloads shaped like released Gemini
CLI 0.62.0 events, point it at a real mock panel through the environment, and
read the lane's state back. The hook must never write to stdout: the harness
parses stdout as a decision.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from rgi.integrations import gemini_cli
from rgi.report import Reporter
from tests.mock_panel import MockPanel

SESSION = "8d4c6d2e-0000-4000-8000-000000000001"
LANE = f"gemini-cli:{SESSION}"


class GeminiHookTest(unittest.TestCase):
    """One mock panel and one state directory per test, like one machine."""

    def setUp(self):
        self.panel = MockPanel(token="tok", lanes=4).__enter__()
        self.addCleanup(self.panel.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.env = {
            "RGI_URL": self.panel.url,
            "RGI_TOKEN": "tok",
            "RGI_IDENT": "test-machine",
            "RGI_STATE_DIR": self.state_dir,
            "RGI_HOOK_DEBUG": "",
            "RGI_DEBUG": "",
        }
        self.base = datetime.now(timezone.utc)
        self.tick = 0

    # -- building representative Gemini CLI payloads -----------------------
    def stamp(self, offset: int | None = None) -> str:
        if offset is None:
            self.tick += 1
            offset = self.tick
        moment = self.base + timedelta(seconds=offset)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def payload(self, event: str, **fields) -> dict:
        base = {
            "session_id": SESSION,
            "transcript_path": "/tmp/gemini/chats/session.json",
            "cwd": "/srv/project",
            "hook_event_name": event,
            "timestamp": self.stamp(),
        }
        base.update(fields)
        return base

    def permission(self, **fields) -> dict:
        base = {
            "notification_type": "ToolPermission",
            "message": "Tool Shell requires execution",
            "details": {"type": "exec", "title": "Shell",
                        "command": "make deploy", "rootCommand": "make"},
        }
        base.update(fields)
        return self.payload("Notification", **base)

    def call(self, payload, **env_overrides) -> int:
        """Run one hook event; stdout must stay untouched, the code must be 0."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.dict(os.environ, {**self.env, **env_overrides}):
                code = gemini_cli.hook(payload)
        self.assertEqual(out.getvalue(), "", "the hook wrote to stdout")
        self.assertEqual(code, 0, "the hook returned a decision exit code")
        self.last_stderr = err.getvalue()
        return code

    def states(self) -> list[str]:
        return [entry["state"]
                for _, path, entry in self.panel.requests_for("/session/state")]


class LifecycleTest(GeminiHookTest):
    def test_before_agent_claims_a_lane_as_working(self):
        self.call(self.payload("BeforeAgent", prompt="do the thing"))
        self.assertEqual(self.panel.state_of(LANE), "working")
        session = self.panel.sessions[LANE]
        self.assertEqual(session["agent"], "gemini-cli")
        self.assertEqual(session["ident"], "test-machine")
        self.assertEqual(session["label"], "gemini project")

    def test_after_agent_reports_done_and_keeps_the_lane(self):
        self.call(self.payload("BeforeAgent", prompt="do the thing"))
        self.call(self.payload("AfterAgent", prompt="do the thing",
                               prompt_response="finished",
                               stop_hook_active=False))
        self.assertEqual(self.panel.state_of(LANE), "done")
        self.assertIn(LANE, self.panel.sessions)       # held until released

    def test_tool_activity_clears_a_premature_done(self):
        self.call(self.payload("BeforeAgent", prompt="do the thing"))
        self.call(self.payload("AfterAgent", prompt="do the thing",
                               prompt_response="done", stop_hook_active=False))
        self.call(self.payload("BeforeTool", tool_name="run_shell_command",
                               tool_input={"command": "ls"}))
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(self.states()[-1], "working")

    def test_after_tool_keeps_working(self):
        self.call(self.payload("BeforeTool", tool_name="run_shell_command",
                               tool_input={"command": "ls"}))
        self.call(self.payload("AfterTool", tool_name="run_shell_command",
                               tool_input={"command": "ls"},
                               tool_response={"llmContent": "ok"}))
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_precompress_clears_a_premature_done(self):
        self.call(self.payload("AfterAgent", prompt="do the thing",
                               prompt_response="done", stop_hook_active=False))
        self.assertEqual(self.panel.state_of(LANE), "done")
        self.call(self.payload("PreCompress", trigger="auto"))
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_session_start_idles_and_clears_a_stale_wait(self):
        stale = Reporter("gemini-cli", SESSION, url=self.panel.url, token="tok",
                         ident="test-machine", state_dir=self.state_dir)
        self.assertTrue(stale.blocked(request="crashed-run",
                                      message="never answered"))
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.call(self.payload("SessionStart", source="resume",
                               timestamp=self.stamp(3600)))
        self.assertEqual(self.panel.state_of(LANE), "idle")
        self.assertEqual(self.panel.info_of(LANE)["pending_requests"], [])
        self.assertNotIn("blocked_on", self.panel.info_of(LANE))

    def test_session_end_resolves_and_releases_the_lane(self):
        self.call(self.permission())
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.call(self.payload("SessionEnd", reason="exit"))
        self.assertNotIn(LANE, self.panel.sessions)

    def test_unknown_event_is_ignored(self):
        self.call(self.payload("BeforeModel", llm_request={}))
        self.assertEqual(len(self.panel.sessions), 0)


class ApprovalTest(GeminiHookTest):
    def test_a_pending_tool_permission_blocks(self):
        self.call(self.permission())
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        info = self.panel.info_of(LANE)
        self.assertEqual(len(info["pending_requests"]), 1)
        self.assertEqual(info["blocked_on"]["action"], "exec")

    def test_a_denial_resolves_the_wait_without_leaving_blocked(self):
        """No tool runs after a denial; the turn's AfterAgent ends the wait."""
        self.call(self.permission())
        self.call(self.payload("AfterAgent", prompt="deploy",
                               prompt_response="denied",
                               stop_hook_active=False))
        self.assertEqual(self.panel.state_of(LANE), "done")
        info = self.panel.info_of(LANE)
        self.assertEqual(info["pending_requests"], [])
        self.assertNotIn("blocked_on", info)

    def test_an_approved_tool_resolves_the_wait_and_works(self):
        self.call(self.permission())
        self.call(self.payload("BeforeTool", tool_name="run_shell_command",
                               tool_input={"command": "make deploy"}))
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(self.panel.info_of(LANE)["pending_requests"], [])

    def test_an_after_tool_also_resolves_the_wait(self):
        self.call(self.permission())
        self.call(self.payload("AfterTool", tool_name="run_shell_command",
                               tool_input={"command": "make deploy"},
                               tool_response={"llmContent": "done"}))
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(self.panel.info_of(LANE)["pending_requests"], [])

    def test_a_new_prompt_resolves_a_cancelled_wait(self):
        self.call(self.permission())
        self.call(self.payload("BeforeAgent", prompt="never mind"))
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(self.panel.info_of(LANE)["pending_requests"], [])

    def test_the_synthetic_request_id_is_stable_and_distinct(self):
        self.call(self.permission())
        first = self.panel.info_of(LANE)["pending_requests"]
        self.call(self.permission())                     # same request again
        self.assertEqual(self.panel.info_of(LANE)["pending_requests"], first)
        self.call(self.permission(
            message="Tool Edit requires editing",
            details={"type": "edit", "title": "Edit", "fileName": "app.py"}))
        second = self.panel.info_of(LANE)["pending_requests"]
        self.assertNotEqual(first, second)

    def test_a_request_id_from_the_payload_wins(self):
        self.call(self.permission(request_id="req-42"))
        self.assertEqual(self.panel.info_of(LANE)["pending_requests"],
                         ["req-42"])


class RobustnessTest(GeminiHookTest):
    def test_an_older_event_cannot_overwrite_a_newer_state(self):
        self.call(self.payload("BeforeAgent", prompt="newer",
                               timestamp=self.stamp(20)))
        self.call(self.payload("AfterAgent", prompt="stale",
                               prompt_response="old", stop_hook_active=False,
                               timestamp=self.stamp(10)))
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_malformed_payloads_return_zero_and_touch_nothing(self):
        for payload in ({}, {"hook_event_name": "BeforeAgent"},
                        {"session_id": SESSION, "hook_event_name": 7},
                        {"session_id": SESSION, "hook_event_name": "Sideways"},
                        None, "not a dict", []):
            self.assertEqual(self.call(payload), 0)
        self.assertEqual(len(self.panel.sessions), 0)

    def test_a_missing_session_id_reports_nothing(self):
        payload = self.payload("BeforeAgent", prompt="x")
        del payload["session_id"]
        self.call(payload)
        self.assertEqual(len(self.panel.sessions), 0)

    def test_the_environment_can_supply_the_session_id(self):
        payload = self.payload("BeforeAgent", prompt="x")
        del payload["session_id"]
        self.call(payload, GEMINI_SESSION_ID=SESSION)
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_a_malformed_timestamp_falls_back_to_arrival_order(self):
        self.call(self.payload("BeforeAgent", prompt="x", timestamp="soon"))
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_a_dead_panel_still_returns_zero_and_stays_silent(self):
        self.panel.stop()
        self.call(self.payload("BeforeAgent", prompt="work"))
        self.assertEqual(self.last_stderr, "")

    def test_stdout_and_stderr_stay_empty_for_every_event(self):
        events = [
            self.payload("SessionStart", source="startup"),
            self.payload("BeforeAgent", prompt="go"),
            self.payload("BeforeTool", tool_name="read_file",
                         tool_input={"path": "notes.md"}),
            self.payload("AfterTool", tool_name="read_file",
                         tool_input={"path": "notes.md"},
                         tool_response={"llmContent": "hi"}),
            self.permission(),
            self.payload("PreCompress", trigger="manual"),
            self.payload("AfterAgent", prompt="go", prompt_response="hi",
                         stop_hook_active=False),
            self.payload("SessionEnd", reason="exit"),
            self.payload("Unmapped", anything=True),
            {},
        ]
        for payload in events:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with mock.patch.dict(os.environ, self.env):
                    self.assertEqual(gemini_cli.hook(payload), 0)
            self.assertEqual(out.getvalue(), "", f"stdout polluted by {payload}")
            self.assertEqual(err.getvalue(), "", f"stderr noise from {payload}")

    def test_debug_diagnostics_only_reach_stderr_and_never_stdout(self):
        out, err = io.StringIO(), io.StringIO()
        payload = {"session_id": SESSION, "hook_event_name": "Sideways"}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.dict(os.environ,
                                 {**self.env, "RGI_HOOK_DEBUG": "1"}):
                self.assertEqual(gemini_cli.hook(payload), 0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("[rgi]", err.getvalue())
        self.assertIn("Sideways", err.getvalue())

    def test_a_broken_panel_with_debug_on_is_still_stdout_silent(self):
        self.panel.stop()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with mock.patch.dict(os.environ,
                                 {**self.env, "RGI_HOOK_DEBUG": "1"}):
                self.assertEqual(
                    gemini_cli.hook(self.payload("BeforeAgent", prompt="x")),
                    0)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("panel_unreachable", err.getvalue())


if __name__ == "__main__":
    unittest.main()
