"""The Zoo Code CLI adapter: event mapping, the human-wait rule, and the runner.

These tests drive a recorded NDJSON stream - the shape Zoo Code's CLI actually
prints - against the real (mock) HTTP panel. The CLI itself is neither installed
nor needed: the runner is tested with a fake process that replays lines.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from rgi.integrations.zoo_cli import (
    EVENT_ASSISTANT,
    EVENT_CONTROL,
    EVENT_ERROR,
    EVENT_QUEUE,
    EVENT_RESULT,
    EVENT_SYSTEM,
    EVENT_THINKING,
    EVENT_TOOL_RESULT,
    EVENT_TOOL_USE,
    EVENT_USER,
    SUBTYPE_FOLLOWUP,
    SUBTYPE_INIT,
    ZooStreamReporter,
    find_binary,
    run_zoo,
    session_id_in,
)
from tests.mock_panel import MockPanel

SESSION = "11111111-2222-4333-8444-555555555555"
LANE = f"zoo:{SESSION}"


def stream(*events: dict) -> str:
    """NDJSON, the way the CLI writes it."""
    import json
    return "".join(json.dumps(e) + "\n" for e in events)


INIT = {"type": "system", "subtype": SUBTYPE_INIT, "content": "Task started",
        "protocol": "roo-cli-stream", "schemaVersion": 1}
ASSISTANT = {"type": "assistant", "id": 1, "content": "Looking at it", "done": True}
FOLLOWUP = {"type": "assistant", "id": 2, "subtype": SUBTYPE_FOLLOWUP,
            "content": "Which database should I use?", "done": True}
ANSWER = {"type": "user", "id": 3, "content": "postgres"}
COMMAND = {"type": "tool_use", "id": 10, "subtype": "command",
           "tool_use": {"name": "execute_command", "input": {"command": "pytest -q"}}}
COMMAND_DONE = {"type": "tool_result", "id": 10,
                "tool_result": {"name": "execute_command", "exitCode": 1,
                                "output": "1 failed"}}
RESULT_OK = {"type": "result", "success": True, "content": "fixed",
             "cost": {"totalCost": 0.42, "inputTokens": 1200, "outputTokens": 300,
                      "cacheWrites": 40, "cacheReads": 900}}
RESULT_BAD = {"type": "result", "success": False, "content": "ran out of credit"}


class FakeProcess:
    """A subprocess that replays lines, in the shape Popen hands back."""

    def __init__(self, lines, code: int = 0, interrupt: bool = False):
        self._lines = list(lines)
        self._code = code
        self._interrupt = interrupt
        self.terminated = False

    @property
    def stdout(self):
        """Popen hands back the pipe on the process; here it is the process."""
        return self

    def __iter__(self):
        return self

    def __next__(self):
        if self._interrupt:
            self._interrupt = False
            raise KeyboardInterrupt
        if not self._lines:
            raise StopIteration
        return self._lines.pop(0)

    def terminate(self):
        self.terminated = True

    def wait(self):
        return self._code


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.panel = MockPanel(token="tok", lanes=4).__enter__()
        self.addCleanup(self.panel.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs: list[str] = []
        self.env = mock.patch.dict(os.environ, {
            "RGI_URL": self.panel.url,
            "RGI_TOKEN": "tok",
            "RGI_IDENT": "test-machine",
            "RGI_STATE_DIR": os.path.join(self.tmp.name, "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.adapters: list[ZooStreamReporter] = []

    def tearDown(self):
        for adapter in self.adapters:
            adapter.close()

    def lane(self, **kwargs) -> ZooStreamReporter:
        kwargs.setdefault("session_id", SESSION)
        kwargs.setdefault("log", self.logs.append)
        adapter = ZooStreamReporter(**kwargs)
        self.adapters.append(adapter)
        return adapter

    def states(self) -> list[str]:
        return [payload.get("state")
                for _, path, payload in self.panel.requests_for("/session/state")
                if path == "/session/state"]


class LaneLifecycleTest(AdapterTest):
    def test_init_claims_a_lane_named_after_the_prompt(self):
        lane = self.lane(label="fix the failing tests")
        lane.handle(INIT)
        starts = self.panel.requests_for("/session/start")
        self.assertEqual(len(starts), 1)
        _, _, payload = starts[0]
        self.assertEqual(payload["sessionID"], LANE)
        self.assertEqual(payload["label"], "fix the failing tests")
        self.assertEqual(payload["agent"], "zoo")
        self.assertEqual(payload["ident"], "test-machine")
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_a_stream_with_no_events_never_takes_a_lamp(self):
        lane = self.lane()
        lane.close()
        self.assertEqual(self.panel.requests_for("/session/start"), [])

    def test_activity_reports_working_and_the_result_reports_done(self):
        lane = self.lane()
        for event in (INIT, ASSISTANT, {"type": EVENT_THINKING, "id": 9}):
            lane.handle(event)
            self.assertEqual(self.panel.state_of(LANE), "working")
        lane.handle(RESULT_OK)
        self.assertEqual(self.panel.state_of(LANE), "done")

    def test_a_failed_result_is_an_error_not_a_done(self):
        lane = self.lane()
        lane.handle(INIT)
        lane.handle(RESULT_BAD)
        self.assertEqual(self.panel.state_of(LANE), "error")
        self.assertIn("ran out of credit", str(self.panel.info_of(LANE)))

    def test_an_error_event_reddens_and_the_next_event_recovers(self):
        lane = self.lane()
        lane.handle(INIT)
        lane.handle({"type": EVENT_ERROR, "content": "rate limited, retrying"})
        self.assertEqual(self.panel.state_of(LANE), "error")
        lane.handle(ASSISTANT)               # Zoo carried on by itself
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_close_releases_the_lane(self):
        lane = self.lane()
        lane.handle(INIT)
        lane.close()
        lane.close()                          # twice is fine
        self.assertIsNone(self.panel.state_of(LANE))


class HumanWaitTest(AdapterTest):
    def test_a_followup_question_blocks_and_the_answer_releases_it(self):
        lane = self.lane()
        lane.handle(INIT)
        lane.handle(FOLLOWUP)
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.assertEqual(lane.pending, ["followup-1"])
        info = self.panel.info_of(LANE)
        self.assertEqual(info["blocked_on"]["action"], "question")
        self.assertIn("Which database", info["blocked_on"]["message"])

        lane.handle(ANSWER)
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(lane.pending, [])

    def test_two_questions_are_counted_separately(self):
        lane = self.lane()
        lane.handle(FOLLOWUP)
        lane.handle({**FOLLOWUP, "id": 7, "content": "And the port?"})
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.assertEqual(len(lane.pending), 2)
        lane.handle(ANSWER)                   # one answer clears both waits
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_a_failing_command_is_work_not_a_human_wait(self):
        lane = self.lane()
        lane.handle(INIT)
        lane.handle(COMMAND)
        lane.handle(COMMAND_DONE)              # exitCode 1
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(self.panel.info_of(LANE)["last_exit"], 1)

    def test_a_tool_use_alone_never_blocks_by_default(self):
        lane = self.lane()
        lane.handle(COMMAND)
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(lane.pending, [])

    def test_with_require_approval_a_tool_waits_for_its_result(self):
        lane = self.lane(block_on_tool_use=True)
        lane.handle(COMMAND)
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.assertEqual(lane.pending, ["10"])
        self.assertEqual(self.panel.info_of(LANE)["blocked_on"]["action"],
                         "execute_command")
        lane.handle(COMMAND_DONE)
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(lane.pending, [])

    def test_the_end_of_a_run_clears_a_question_nobody_answered(self):
        lane = self.lane()
        lane.handle(INIT)
        lane.handle(FOLLOWUP)
        lane.handle(RESULT_BAD)                # the run died mid-question
        self.assertEqual(self.panel.state_of(LANE), "error")
        self.assertEqual(lane.pending, [])


class DetailTest(AdapterTest):
    def test_a_tool_call_publishes_the_tool_and_a_short_step(self):
        lane = self.lane()
        lane.handle(COMMAND)
        info = self.panel.info_of(LANE)
        self.assertEqual(info["tool"], "execute_command")
        self.assertEqual(info["step"], "pytest -q")

    def test_cost_is_published_in_the_words_the_sidebar_reads(self):
        lane = self.lane()
        lane.handle(RESULT_OK)
        tokens = self.panel.info_of(LANE)["tokens"]
        self.assertEqual(tokens["input"], 1200)
        self.assertEqual(tokens["output"], 300)
        self.assertEqual(tokens["cost"], 0.42)
        self.assertEqual(tokens["cache_read"], 900)

    def test_queue_depth_is_published(self):
        lane = self.lane()
        lane.handle({"type": EVENT_QUEUE, "subtype": "enqueued", "queueDepth": 2})
        self.assertEqual(self.panel.info_of(LANE)["queued"], 2)

    def test_the_lane_id_follows_zoos_own_task_id_when_none_was_given(self):
        lane = self.lane(session_id=None)
        lane.handle({**INIT, "taskId": "abc-123"})
        self.assertEqual(lane.reporter.session_key, "zoo:abc-123")

    def test_a_generated_id_is_used_when_the_stream_has_none(self):
        lane = self.lane(session_id=None)
        lane.handle(INIT)
        self.assertTrue(lane.reporter.session_key.startswith("zoo:"))
        self.assertNotEqual(lane.reporter.session_key, "zoo:")

    def test_secrets_in_a_command_line_are_scrubbed_before_publishing(self):
        lane = self.lane()
        lane.handle({**COMMAND, "tool_use": {"name": "execute_command",
                                            "input": {"command": "curl -H 'token: sk-abcdefghijklmnop' https://x"}}})
        step = self.panel.info_of(LANE)["step"]
        self.assertNotIn("sk-abcdefghijklmnop", step)
        self.assertIn("[redacted]", step)


class MalformedInputTest(AdapterTest):
    def test_a_line_that_is_not_json_is_ignored(self):
        lane = self.lane()
        self.assertFalse(lane.feed_line("[CLI] connecting to provider..."))
        self.assertFalse(lane.feed_line(""))
        self.assertFalse(lane.feed_line("   "))
        self.assertEqual(self.panel.requests_for("/session/start"), [])

    def test_unknown_and_control_events_are_ignored(self):
        lane = self.lane()
        self.assertFalse(lane.handle({"type": EVENT_CONTROL, "subtype": "ack",
                                      "requestId": "r1"}))
        self.assertFalse(lane.handle({"type": "something_new", "content": "hi"}))
        self.assertFalse(lane.handle({"content": "no type at all"}))
        self.assertFalse(lane.handle("not even a dict"))

    def test_an_unexpected_protocol_is_a_note_not_a_failure(self):
        lane = self.lane()
        lane.handle({**INIT, "protocol": "something-else-v9"})
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertTrue(any("protocol" in line for line in self.logs))

    def test_a_dead_panel_does_not_raise_into_the_run(self):
        lane = self.lane(session_id=SESSION, url="http://127.0.0.1:1",
                         token="tok", timeout=0.2)
        for event in (INIT, ASSISTANT, FOLLOWUP, COMMAND, RESULT_OK):
            lane.handle(event)                 # must not raise
        lane.close()

    def test_the_whole_ndjson_stream_replays_through_feed_line(self):
        lane = self.lane()
        for line in stream(INIT, ASSISTANT, COMMAND, COMMAND_DONE, RESULT_OK).splitlines():
            lane.feed_line(line)
        self.assertEqual(self.panel.state_of(LANE), "done")


class RunnerTest(AdapterTest):
    def run_with(self, process: FakeProcess, echo: bool = False, **kwargs) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("rgi.integrations.zoo_cli.find_binary", return_value="roo"), \
                mock.patch("rgi.integrations.zoo_cli.subprocess.Popen",
                           return_value=process) as popen, \
                contextlib.redirect_stdout(out):
            code = run_zoo([], prompt="fix the tests", echo=echo,
                           session_id=SESSION, **kwargs)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[1:], ["--print", "--output-format", "stream-json",
                                    "fix the tests"])
        return code, out.getvalue()

    def test_it_runs_the_cli_echoes_the_stream_and_returns_its_exit_code(self):
        process = FakeProcess(stream(INIT, ASSISTANT, RESULT_OK).splitlines(keepends=True),
                              code=0)
        code, echoed = self.run_with(process, echo=True)
        self.assertEqual(code, 0)
        self.assertEqual(echoed.count("\n"), 3)
        self.assertIn('"type": "result"', echoed)
        states = self.states()
        self.assertIn("working", states)
        self.assertEqual(states[-1], "done")
        self.assertTrue(self.panel.requests_for("/session/end"))

    def test_a_failing_run_reports_the_error_and_the_exit_code(self):
        process = FakeProcess(stream(INIT, {"type": EVENT_ERROR,
                                            "content": "provider exploded"}).splitlines(keepends=True),
                              code=3)
        code, _ = self.run_with(process)
        self.assertEqual(code, 3)
        self.assertEqual(self.states()[-1], "error")

    def test_an_interrupt_terminates_the_child_and_says_so(self):
        process = FakeProcess([stream(INIT)], code=0, interrupt=True)
        code, _ = self.run_with(process)
        self.assertEqual(code, 130)
        self.assertTrue(process.terminated)
        self.assertEqual(self.states()[-1], "error")
        self.assertTrue(self.panel.requests_for("/session/end"))

    def test_zoos_own_resume_id_becomes_the_lane_id(self):
        process = FakeProcess([])
        with mock.patch("rgi.integrations.zoo_cli.find_binary", return_value="roo"), \
                mock.patch("rgi.integrations.zoo_cli.subprocess.Popen",
                           return_value=process) as popen, \
                contextlib.redirect_stdout(io.StringIO()):
            run_zoo(["--session-id", SESSION], prompt="carry on")
        argv = popen.call_args[0][0]
        self.assertEqual(session_id_in(argv), SESSION)
        self.assertEqual(argv[1:], ["--print", "--output-format", "stream-json",
                                    "--session-id", SESSION, "carry on"])


class BinaryLookupTest(unittest.TestCase):
    def test_the_resume_id_is_read_from_every_spelling(self):
        self.assertEqual(session_id_in(["--session-id", "abc"]), "abc")
        self.assertEqual(session_id_in(["--session-id=abc"]), "abc")
        self.assertEqual(session_id_in(["--create-with-session-id", "abc"]), "abc")
        self.assertEqual(session_id_in(["--create-with-session-id=abc"]), "abc")
        self.assertEqual(session_id_in(["--print", "task"]), "")

    def test_an_explicit_path_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "roo")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n")
            self.assertEqual(find_binary(path), path)

    def test_a_missing_cli_says_how_to_get_one(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("rgi.integrations.zoo_cli.shutil.which", return_value=None):
            with self.assertRaises(FileNotFoundError) as caught:
                find_binary()
        self.assertIn("RGI_ZOO_BIN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
