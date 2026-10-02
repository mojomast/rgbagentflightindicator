"""The generic ingest normalizer: recorded payloads in, canonical actions out.

Two layers of coverage:

* ``tests/fixtures/ingest/<source>.json`` holds one recorded-shape payload per
  mapping row, with the exact expected actions in the sibling
  ``<source>.expected.json``. The fixture test walks every case.
* the direct tests below pin the contract the fixtures cannot express:
  ``sources()``, aliases, never-raise behavior, derived session ids, delivery
  ids, the attention open/close protocol, the canonical-state integration and
  the promise that prompt/transcript/tool-input content never reaches an
  action.
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path
from unittest import mock

from rgi import report
from rgi.ingest import Action, normalize, sources

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ingest"


def _dump(actions: list[Action]) -> list[dict]:
    return [dataclasses.asdict(action) for action in actions]


def _load(path: Path) -> tuple[dict, dict]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected = json.loads(path.with_suffix(".expected.json").read_text(encoding="utf-8"))
    return manifest, expected


class SourcesTest(unittest.TestCase):
    def test_sources_is_the_documented_tuple(self):
        self.assertEqual(sources(), (
            "amazon-q", "claude-code", "codex", "continue", "copilot",
            "cursor", "devin", "gemini-cli", "generic", "github", "gitlab",
            "jenkins",
        ))
        self.assertEqual(len(set(sources())), len(sources()))

    def test_unknown_source_is_empty_not_an_error(self):
        self.assertEqual(normalize("not-a-harness", {"anything": True}), [])
        self.assertEqual(normalize("", {"hook_event_name": "Stop"}), [])
        self.assertEqual(normalize(None, {"hook_event_name": "Stop"}), [])


class FixtureTest(unittest.TestCase):
    """Every recorded payload has a sibling expectation, and it matches."""

    def test_recorded_payloads_match_expected_actions(self):
        manifests = sorted(p for p in FIXTURES.glob("*.json")
                           if not p.name.endswith(".expected.json"))
        self.assertTrue(manifests, "no ingest fixtures found")
        covered = set()
        for path in manifests:
            manifest, expected = _load(path)
            covered.add(manifest["source"])
            self.assertIn(manifest["source"], sources(),
                          f"{path.name} names an unknown source")
            self.assertEqual(sorted(expected), sorted(c["name"] for c in manifest["cases"]),
                             f"{path.name} expected actions do not match its cases")
            for case in manifest["cases"]:
                with self.subTest(source=manifest["source"], case=case["name"]):
                    actions = normalize(manifest["source"], case["payload"],
                                        case.get("headers"))
                    self.assertEqual(_dump(actions), expected[case["name"]])
        self.assertEqual(covered, set(sources()),
                         "every source must have a fixture manifest")

    def test_every_emitted_state_is_canonical(self):
        """canonical_state integration: no fixture state escapes the five."""
        for path in FIXTURES.glob("*.json"):
            if path.name.endswith(".expected.json"):
                continue
            manifest, _ = _load(path)
            for case in manifest["cases"]:
                for action in normalize(manifest["source"], case["payload"],
                                        case.get("headers")):
                    if action.state is not None:
                        self.assertIn(action.state, report.STATES,
                                      f"{manifest['source']}/{case['name']} "
                                      f"emitted {action.state!r}")

    def test_every_action_has_a_known_op_and_session(self):
        for path in FIXTURES.glob("*.json"):
            if path.name.endswith(".expected.json"):
                continue
            manifest, _ = _load(path)
            for case in manifest["cases"]:
                for action in normalize(manifest["source"], case["payload"],
                                        case.get("headers")):
                    self.assertIn(action.op, ("begin", "state", "info", "end",
                                              "attention", "activity",
                                              "snapshot"))
                    self.assertTrue(action.session.startswith(
                        manifest["source"] + ":") or ":" in action.session)


class CanonicalStateIntegrationTest(unittest.TestCase):
    def test_the_shared_mapper_is_the_one_that_runs(self):
        with mock.patch("rgi.ingest.canonical_state",
                        wraps=report.canonical_state) as spy:
            actions = normalize("claude-code", {
                "hook_event_name": "Stop", "session_id": "s-1"})
            self.assertTrue(spy.called)
        self.assertEqual(actions[-1].state, "done")

    def test_cancel_maps_to_error_like_report_py(self):
        actions = normalize("generic", {"agent": "x", "sessionID": "a",
                                        "state": "cancel"})
        self.assertEqual(actions[-1].state, "error")
        self.assertEqual(report.canonical_state("cancelled"), "error")


class ClaudeCodeEventRowsTest(unittest.TestCase):
    """One assertion per mapped event, so a new row cannot go untested."""

    SESSION = {"session_id": "s-1", "cwd": "/srv/proj"}

    def rows(self, payload: dict) -> list[tuple[str, str | None]]:
        return [(a.op, a.state) for a in
                normalize("claude-code", {**self.SESSION, **payload})]

    def test_lifecycle_rows(self):
        self.assertEqual(self.rows({"hook_event_name": "SessionStart"}),
                         [("begin", None), ("attention", None),
                          ("state", "working")])
        self.assertEqual(self.rows({"hook_event_name": "UserPromptSubmit"}),
                         [("attention", None), ("state", "working")])
        self.assertEqual(self.rows({"hook_event_name": "Stop"}),
                         [("attention", None), ("state", "done")])
        self.assertEqual(self.rows({"hook_event_name": "StopFailure"}),
                         [("attention", None), ("state", "error")])
        self.assertEqual(self.rows({"hook_event_name": "PreCompact"}),
                         [("state", "working")])
        self.assertEqual(self.rows({"hook_event_name": "SessionEnd"}),
                         [("attention", None), ("end", None)])

    def test_tool_rows(self):
        self.assertEqual(self.rows({"hook_event_name": "PreToolUse",
                                    "tool_name": "Bash"}),
                         [("state", "working"), ("activity", None)])
        self.assertEqual(self.rows({"hook_event_name": "PostToolUse",
                                    "tool_name": "Bash"}),
                         [("attention", None), ("state", "working"),
                          ("activity", None)])
        self.assertEqual(self.rows({"hook_event_name": "PostToolUseFailure",
                                    "tool_name": "Bash"}),
                         [("attention", None), ("state", "working"),
                          ("activity", None)])

    def test_subagent_rows_are_info_only(self):
        self.assertEqual(self.rows({"hook_event_name": "SubagentStart"}),
                         [("info", None)])
        self.assertEqual(self.rows({"hook_event_name": "SubagentStop"}),
                         [("info", None)])

    def test_notification_rows(self):
        self.assertEqual(self.rows({"hook_event_name": "Notification",
                                    "notification_type": "permission_prompt"}),
                         [("attention", "blocked")])
        self.assertEqual(self.rows({"hook_event_name": "Notification",
                                    "notification_type": "auth_success"}), [])
        self.assertEqual(self.rows({"hook_event_name": "PermissionRequest"}),
                         [("attention", "blocked")])

    def test_unknown_event_and_missing_session(self):
        self.assertEqual(self.rows({"hook_event_name": "TeammateIdle"}), [])
        self.assertEqual(normalize("claude-code", {"hook_event_name": "Stop"}), [])


class CodexVariantTest(unittest.TestCase):
    def test_pascal_and_snake_event_names_are_the_same_event(self):
        base = {"session_id": "thr-1", "tool_name": "Bash", "tool_use_id": "c1"}
        pascal = normalize("codex", {"hook_event_name": "PreToolUse", **base})
        snake = normalize("codex", {"hook_event_name": "pre_tool_use", **base})
        self.assertEqual(_dump(pascal), _dump(snake))
        self.assertEqual(pascal[0].state, "working")
        self.assertEqual(pascal[1].op, "activity")

    def test_interrupt_is_a_cancel_and_maps_to_error(self):
        actions = normalize("codex", {"hook_event_name": "Interrupt",
                                      "session_id": "thr-1"})
        self.assertEqual(actions[-1].state, "error")

    def test_permission_request_opens_a_wait_with_a_synthetic_id(self):
        actions = normalize("codex", {"hook_event_name": "PermissionRequest",
                                      "session_id": "thr-1", "tool_name": "Bash"})
        wait = actions[0]
        self.assertEqual(wait.op, "attention")
        self.assertTrue(wait.meta["open"])
        self.assertTrue(wait.request.startswith("codex-permission-"))
        self.assertEqual(wait.info["action"], "permission")


class CursorTest(unittest.TestCase):
    def test_stop_status_decides_the_state(self):
        for status, state in (("completed", "done"), ("aborted", "error"),
                              ("error", "error")):
            with self.subTest(status=status):
                actions = normalize("cursor", {
                    "hook_event_name": "stop", "conversation_id": "c-1",
                    "status": status})
                self.assertEqual(actions[-1].state, state)
                self.assertFalse(actions[-1].session.endswith(":"))


class CopilotDerivationTest(unittest.TestCase):
    def test_no_session_id_derives_a_stable_id_from_cwd_and_pid(self):
        payload = {"hook_event_name": "sessionStart", "cwd": "/w/repo",
                   "pid": 4242, "source": "new"}
        first = normalize("copilot", payload)
        again = normalize("copilot", dict(payload))
        other = normalize("copilot", {**payload, "pid": 4243})
        self.assertEqual(first[0].session, again[0].session)
        self.assertNotEqual(first[0].session, other[0].session)
        self.assertTrue(first[0].session.startswith("copilot:auto-"))

    def test_process_id_headers_feed_the_derivation(self):
        payload = {"hook_event_name": "sessionStart", "cwd": "/w/repo"}
        first = normalize("copilot", payload, {"X-RGI-Pid": "77"})
        again = normalize("copilot", payload, {"X-RGI-Pid": "77"})
        other = normalize("copilot", payload, {"X-RGI-Pid": "78"})
        self.assertEqual(first[0].session, again[0].session)
        self.assertNotEqual(first[0].session, other[0].session)

    def test_camel_case_payloads_are_recognised_by_shape(self):
        actions = normalize("copilot", {"sessionId": "cp-1", "cwd": "/w/repo",
                                        "toolName": "bash", "toolArgs": {}})
        self.assertEqual([a.op for a in actions], ["state", "activity"])
        self.assertEqual(actions[0].state, "working")

    def test_camel_case_stop_is_done(self):
        actions = normalize("copilot", {"sessionId": "cp-1", "cwd": "/w/repo",
                                        "stopReason": "end_turn"})
        self.assertEqual(actions[-1].state, "done")


class AmazonQDerivationTest(unittest.TestCase):
    def test_no_session_id_anywhere_derives_a_stable_id(self):
        payload = {"hook_event_name": "userPromptSubmit", "cwd": "/work/q",
                   "pid": 9, "prompt": "hi"}
        first = normalize("amazon-q", payload)
        again = normalize("amazon-q", dict(payload))
        other = normalize("amazon-q", {**payload, "pid": 10})
        self.assertEqual(first[0].session, again[0].session)
        self.assertNotEqual(first[0].session, other[0].session)
        self.assertTrue(first[0].session.startswith("amazon-q:auto-"))

    def test_a_missing_pid_still_yields_a_stable_lane(self):
        payload = {"hook_event_name": "stop", "cwd": "/work/q"}
        self.assertEqual(normalize("amazon-q", payload)[0].session,
                         normalize("amazon-q", payload)[0].session)


class GeminiTest(unittest.TestCase):
    def test_start_is_idle_and_a_tool_permission_opens_the_wait(self):
        start = normalize("gemini-cli", {"hook_event_name": "SessionStart",
                                         "session_id": "g-1", "cwd": "/w"})
        self.assertEqual(start[-1].state, "idle")
        wait = normalize("gemini-cli", {
            "hook_event_name": "Notification", "session_id": "g-1",
            "notification_type": "ToolPermission",
            "details": {"toolName": "Shell"}})
        self.assertEqual(wait[0].op, "attention")
        self.assertEqual(wait[0].info["action"], "permission")
        self.assertEqual(wait[0].info["tool"], "Shell")

    def test_other_notifications_are_not_waits(self):
        self.assertEqual(normalize("gemini-cli", {
            "hook_event_name": "Notification", "session_id": "g-1",
            "notification_type": "SomethingElse"}), [])


class DevinTest(unittest.TestCase):
    def test_windsurf_and_cascade_are_aliases(self):
        payload = {"agent_action_name": "post_cascade_response",
                   "trajectory_id": "t-1"}
        expected = _dump(normalize("devin", payload))
        self.assertEqual(_dump(normalize("windsurf", payload)), expected)
        self.assertEqual(_dump(normalize("cascade", payload)), expected)
        self.assertEqual(normalize("devin", payload)[-1].state, "done")

    def test_cascade_payloads_use_trajectory_id_as_the_lane(self):
        actions = normalize("devin", {"agent_action_name": "pre_user_prompt",
                                      "trajectory_id": "traj-9"})
        self.assertEqual({a.session for a in actions}, {"devin:traj-9"})


class GenericTest(unittest.TestCase):
    def test_panel_payload_claims_reports_and_describes(self):
        actions = normalize("generic", {
            "agent": "hermes", "sessionID": "job-1", "label": "nightly",
            "state": "running", "host": "kimi", "ident": "h-3",
            "info": {"repo": "rgi"}})
        self.assertEqual([a.op for a in actions], ["begin", "state", "info"])
        self.assertEqual(actions[0].session, "hermes:job-1")
        self.assertEqual(actions[0].label, "nightly")
        self.assertEqual(actions[0].meta, {"host": "kimi", "ident": "h-3"})
        self.assertEqual(actions[1].state, "working")
        self.assertEqual(actions[2].info, {"repo": "rgi"})

    def test_a_keyed_session_id_is_already_canonical(self):
        actions = normalize("generic", {"agent": "hermes",
                                        "sessionID": "opencode:ses_1",
                                        "state": "done"})
        self.assertEqual(actions[0].session, "opencode:ses_1")

    def test_missing_session_is_empty(self):
        self.assertEqual(normalize("generic", {"agent": "x",
                                               "state": "working"}), [])

    def test_cloud_event_envelope(self):
        actions = normalize("generic", {
            "specversion": "1.0", "id": "evt-1",
            "type": "dev.rgi.session.begin", "subject": "codex:thr-9",
            "time": "2026-10-02T18:04:12Z",
            "data": {"harness": "codex", "label": "fix", "slot": 3}})
        self.assertEqual(len(actions), 1)
        begin = actions[0]
        self.assertEqual((begin.op, begin.session, begin.agent),
                         ("begin", "codex:thr-9", "codex"))
        self.assertEqual(begin.slot, 3)
        self.assertEqual(begin.meta["delivery"], "evt-1")

    def test_attention_open_and_close_types(self):
        opened = normalize("generic", {
            "type": "dev.rgi.attention.open", "subject": "codex:thr-1",
            "data": {"request": "r-1"}})
        self.assertTrue(opened[0].meta["open"])
        self.assertEqual(opened[0].state, "blocked")
        closed = normalize("generic", {
            "type": "dev.rgi.attention.close", "subject": "codex:thr-1",
            "data": {"request": "r-1"}})
        self.assertFalse(closed[0].meta["open"])
        self.assertIsNone(closed[0].state)

    def test_snapshot(self):
        actions = normalize("generic", {"op": "snapshot", "agent": "hermes",
                                        "sessionID": "job-1", "state": "working",
                                        "info": {"repo": "rgi"}, "lease_s": 30})
        self.assertEqual(actions[0].op, "snapshot")
        self.assertEqual(actions[0].meta["lease_s"], 30.0)
        self.assertEqual(actions[0].state, "working")


class AttentionProtocolTest(unittest.TestCase):
    def test_open_carries_id_action_and_blocked_state(self):
        actions = normalize("claude-code", {
            "hook_event_name": "Notification", "session_id": "s-1",
            "notification_type": "permission_prompt", "tool_name": "Bash",
            "tool_use_id": "toolu_1"})
        wait = actions[0]
        self.assertEqual(wait.op, "attention")
        self.assertEqual(wait.state, "blocked")
        self.assertEqual(wait.request, "toolu_1")
        self.assertEqual(wait.info, {"action": "permission", "tool": "Bash"})
        self.assertTrue(wait.meta["open"])

    def test_close_by_id_names_the_wait(self):
        actions = normalize("claude-code", {
            "hook_event_name": "PostToolUse", "session_id": "s-1",
            "tool_name": "Bash", "tool_use_id": "toolu_1"})
        close = actions[0]
        self.assertEqual(close.request, "toolu_1")
        self.assertFalse(close.meta["open"])

    def test_close_without_an_id_falls_back_to_the_tool_name(self):
        actions = normalize("claude-code", {
            "hook_event_name": "PostToolUse", "session_id": "s-1",
            "tool_name": "Read"})
        close = actions[0]
        self.assertIsNone(close.request)
        self.assertEqual(close.meta["match"], "Read")
        self.assertNotIn("all", close.meta)

    def test_events_that_end_a_turn_close_every_wait(self):
        for event in ("UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"):
            with self.subTest(event=event):
                actions = normalize("claude-code", {"hook_event_name": event,
                                                    "session_id": "s-1"})
                close = actions[0]
                self.assertEqual(close.op, "attention")
                self.assertIsNone(close.request)
                self.assertTrue(close.meta["all"])
                self.assertFalse(close.meta["open"])

    def test_a_synthetic_id_is_stable_across_deliveries(self):
        payload = {"hook_event_name": "Notification", "session_id": "s-1",
                   "notification_type": "elicitation_dialog"}
        first = normalize("claude-code", payload)[0].request
        again = normalize("claude-code", dict(payload))[0].request
        self.assertEqual(first, again)
        self.assertTrue(first.startswith("claude-code-elicitation-"))


class DeliveryIdTest(unittest.TestCase):
    GITHUB = {"action": "completed",
              "check_run": {"id": 5, "name": "tests", "status": "completed",
                            "conclusion": "failure", "head_branch": "main"},
              "repository": {"full_name": "o/r"}}
    GITLAB = {"object_kind": "pipeline",
              "object_attributes": {"id": 9, "status": "success", "ref": "main"},
              "project": {"path_with_namespace": "g/p"}}
    JENKINS = {"name": "job", "build": {"number": 3, "phase": "COMPLETED",
                                        "status": "SUCCESS",
                                        "full_url": "https://ci/job/3/"}}

    def test_github_header_wins(self):
        actions = normalize("github", self.GITHUB,
                            {"X-GitHub-Delivery": "d-1"})
        self.assertEqual(actions[0].meta["delivery"], "d-1")

    def test_github_falls_back_to_run_and_conclusion(self):
        actions = normalize("github", self.GITHUB)
        self.assertEqual(actions[0].meta["delivery"], "check_run:5:failure")

    def test_gitlab_uuid_header_wins(self):
        actions = normalize("gitlab", self.GITLAB,
                            {"X-Gitlab-Event-UUID": "u-1"})
        self.assertEqual(actions[0].meta["delivery"], "u-1")

    def test_jenkins_delivery_is_the_build_url(self):
        actions = normalize("jenkins", self.JENKINS)
        self.assertEqual(actions[0].meta["delivery"], "https://ci/job/3/")

    def test_a_jenkins_notification_may_be_a_batch(self):
        actions = normalize("jenkins", [self.JENKINS, self.JENKINS])
        self.assertEqual(len(actions), 6)


class CiMappingTest(unittest.TestCase):
    def _state(self, source: str, payload: dict) -> tuple[bool, str | None]:
        actions = normalize(source, payload)
        states = [a.state for a in actions if a.op == "state"]
        ended = any(a.op == "end" for a in actions)
        return ended, states[-1] if states else None

    def test_status_vocabulary(self):
        cases = [
            ("github", {"action": "created", "check_run": {"id": 1, "name": "n",
                       "status": "queued", "head_branch": "main"},
                       "repository": {"full_name": "o/r"}}, False, "idle"),
            ("github", {"action": "requested_action",
                       "check_run": {"id": 1, "name": "n", "status": "in_progress",
                                     "head_branch": "main"},
                       "repository": {"full_name": "o/r"}}, False, "blocked"),
            ("gitlab", {"object_kind": "pipeline", "object_attributes": {
                        "id": 2, "status": "timed_out", "ref": "main"},
                        "project": {"path_with_namespace": "g/p"}}, False, "error"),
            ("gitlab", {"object_kind": "pipeline", "object_attributes": {
                        "id": 2, "status": "skipped", "ref": "main"},
                        "project": {"path_with_namespace": "g/p"}}, True, None),
            ("jenkins", {"name": "j", "build": {"number": 1, "phase": "STARTED"}},
             False, "working"),
            ("jenkins", {"name": "j", "build": {"number": 1,
                        "phase": "QUEUED"}}, False, "idle"),
        ]
        for source, payload, ended, state in cases:
            with self.subTest(source=source, state=state, ended=ended):
                self.assertEqual(self._state(source, payload), (ended, state))

    def test_ci_session_shape(self):
        actions = normalize("github", {
            "action": "in_progress",
            "workflow_run": {"id": 8, "name": "CI", "status": "in_progress",
                             "pull_requests": [{"number": 42}]},
            "repository": {"full_name": "o/r"}})
        self.assertEqual(actions[0].session, "ci:o/r:CI:pr-42")

    def test_unknown_payloads_are_empty(self):
        self.assertEqual(normalize("github", {"action": "completed"}), [])
        self.assertEqual(normalize("gitlab", {"object_kind": "push"}), [])
        self.assertEqual(normalize("jenkins", {"name": "no-build"}), [])


class PrivacyTest(unittest.TestCase):
    """Prompts, transcripts and tool payloads must never reach an action."""

    MARKERS = ("SUPERSECRET_PROMPT", "SUPERSECRET_TRANSCRIPT",
               "SUPERSECRET_TOOLINPUT", "SUPERSECRET_TOOLRESPONSE",
               "SUPERSECRET_RESPONSE")

    def assert_clean(self, source: str, payload: dict):
        blob = json.dumps(_dump(normalize(source, payload)))
        for marker in self.MARKERS:
            self.assertNotIn(marker, blob,
                             f"{source} leaked {marker} into an action")

    def test_content_never_reaches_a_field(self):
        secret = {
            "prompt": self.MARKERS[0],
            "transcript_path": f"/tmp/{self.MARKERS[1]}",
            "tool_input": {"command": self.MARKERS[2]},
            "tool_response": self.MARKERS[3],
            "message": self.MARKERS[4],
        }
        self.assert_clean("claude-code", {
            "hook_event_name": "PostToolUse", "session_id": "s-1",
            "tool_name": "Bash", "tool_use_id": "t-1", **secret})
        self.assert_clean("claude-code", {
            "hook_event_name": "UserPromptSubmit", "session_id": "s-1",
            "prompt": self.MARKERS[0]})
        self.assert_clean("claude-code", {
            "hook_event_name": "Notification", "session_id": "s-1",
            "notification_type": "permission_prompt",
            "message": self.MARKERS[4]})
        self.assert_clean("codex", {
            "hook_event_name": "PermissionRequest", "session_id": "t-1",
            "tool_name": "Bash", "tool_input": {"command": self.MARKERS[2]}})
        self.assert_clean("gemini-cli", {
            "hook_event_name": "Notification", "session_id": "g-1",
            "notification_type": "ToolPermission",
            "details": {"type": "exec", "command": self.MARKERS[2]}})
        self.assert_clean("devin", {
            "agent_action_name": "post_cascade_response",
            "trajectory_id": "tr-1",
            "tool_info": {"response": self.MARKERS[4]}})


class RobustnessTest(unittest.TestCase):
    def test_garbage_never_raises(self):
        weird = [
            None, [], "a string", 42, object(),
            {"hook_event_name": ["Stop"], "session_id": {"x": 1}},
            {"hook_event_name": "Stop", "session_id": 3, "timestamp": {"a": 1}},
            {"object_kind": 5, "object_attributes": "x"},
            {"build": None, "name": []},
            {"data": "not-a-dict", "type": "dev.rgi.session.state"},
        ]
        for source in sources() + ("nope", ""):
            for payload in weird:
                with self.subTest(source=source, payload=payload):
                    self.assertIsInstance(normalize(source, payload),
                                          list)

    def test_headers_may_be_any_mapping_or_none(self):
        payload = {"action": "completed",
                   "check_run": {"id": 1, "name": "n", "status": "completed",
                                 "conclusion": "success"},
                   "repository": {"full_name": "o/r"}}
        for headers in (None, {}, {"x-github-delivery": "lower"},
                        {"X-GitHub-Delivery": 5}, object()):
            with self.subTest(headers=headers):
                actions = normalize("github", payload, headers)
                self.assertTrue(actions)

    def test_action_is_frozen_and_value_compared(self):
        one = Action("state", "a:b", state="working")
        two = Action("state", "a:b", state="working")
        self.assertEqual(one, two)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            one.state = "done"


if __name__ == "__main__":
    unittest.main()
