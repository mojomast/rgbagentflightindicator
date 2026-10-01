"""The Copilot SDK adapter: event mapping, wrappers, and total failure tolerance.

These tests drive a fake session/emitter against the real (mock) HTTP panel;
the third-party ``github-copilot-sdk`` package is neither imported nor needed.
"""

from __future__ import annotations

import asyncio
import enum
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from rgi.integrations.copilot import (
    EVENT_ELICITATION_COMPLETED,
    EVENT_ELICITATION_REQUESTED,
    EVENT_ERROR,
    EVENT_IDLE,
    EVENT_PERMISSION_COMPLETED,
    EVENT_PERMISSION_REQUESTED,
    EVENT_TASK_COMPLETE,
    EVENT_TOOL_START,
    EVENT_TURN_START,
    EVENT_USER_INPUT_COMPLETED,
    EVENT_USER_INPUT_REQUESTED,
    EVENT_USER_MESSAGE,
    WATCHED,
    CopilotReporter,
    observe,
)
from tests.mock_panel import MockPanel

SESSION = "s-1"
LANE = f"copilot:{SESSION}"


class FakeSession:
    """The emitter shape: ``on(event_name, handler)``, one event at a time."""

    session_id = None

    def __init__(self):
        self.listeners: dict[str, list] = {}
        self.off_calls = 0

    def on(self, event_type, handler):
        self.listeners.setdefault(event_type, []).append(handler)

        def off():
            self.off_calls += 1
            try:
                self.listeners[event_type].remove(handler)
            except ValueError:
                pass

        return off

    def emit(self, event_type, data=None):
        event = SimpleNamespace(type=event_type, data=data, timestamp=None)
        return [handler(event) for handler in list(self.listeners.get(event_type, []))]


class FakeSdkSession:
    """The Python SDK shape: ``on(handler)`` receives every session event."""

    def __init__(self, session_id="sdk-1"):
        self.session_id = session_id
        self.handlers = []

    def on(self, handler):
        self.handlers.append(handler)

        def off():
            try:
                self.handlers.remove(handler)
            except ValueError:
                pass

        return off

    def emit(self, event_type, data=None):
        event = SimpleNamespace(type=event_type, data=data, timestamp=None)
        for handler in list(self.handlers):
            handler(event)


class _Kind(enum.Enum):
    """Stands in for the SDK's generated ``SessionEventType`` enum."""

    IDLE = "session.idle"


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
        self.adapters: list[CopilotReporter] = []

    def tearDown(self):
        for adapter in self.adapters:
            adapter.close()

    def adapter(self, session_id=SESSION, **kwargs) -> CopilotReporter:
        kwargs.setdefault("log", self.logs.append)
        adapter = CopilotReporter(session_id, **kwargs)
        self.adapters.append(adapter)
        return adapter

    def session(self, adapter) -> FakeSession:
        fake = FakeSession()
        self.assertTrue(adapter.attach(fake))
        return fake


class StateMappingTest(AdapterTest):
    def test_activity_reports_working_and_idle_reports_done(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_USER_MESSAGE, SimpleNamespace(content="hi"))
        self.assertEqual(self.panel.state_of(LANE), "working")
        fake.emit(EVENT_TURN_START)
        self.assertEqual(self.panel.state_of(LANE), "working")
        fake.emit(EVENT_TOOL_START, SimpleNamespace(tool_name="bash"))
        self.assertEqual(self.panel.state_of(LANE), "working")
        fake.emit(EVENT_IDLE)
        self.assertEqual(self.panel.state_of(LANE), "done")

    def test_permission_request_blocks_and_completion_resolves(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_USER_MESSAGE)
        fake.emit(EVENT_PERMISSION_REQUESTED, SimpleNamespace(
            request_id="perm-1",
            permission_request=SimpleNamespace(kind="shell",
                                               full_command_text="npm test --silent")))
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.assertEqual(adapter.pending, ["perm-1"])
        info = self.panel.info_of(LANE)
        self.assertEqual(info["pending_requests"], ["perm-1"])
        self.assertEqual(info["blocked_on"]["action"], "shell")
        self.assertIn("npm test", info["blocked_on"]["message"])

        fake.emit(EVENT_PERMISSION_COMPLETED, SimpleNamespace(
            request_id="perm-1", result=SimpleNamespace(kind="approve-once")))
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(adapter.pending, [])

    def test_user_input_request_blocks_and_completion_resolves(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_USER_MESSAGE)
        # a dict-shaped payload with camelCase keys, like a JSON emitter
        fake.emit(EVENT_USER_INPUT_REQUESTED,
                  {"requestId": "ask-1", "question": "Deploy where?"})
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.assertEqual(self.panel.info_of(LANE)["blocked_on"]["action"], "question")
        fake.emit(EVENT_USER_INPUT_COMPLETED, {"requestId": "ask-1", "answer": "staging"})
        self.assertEqual(self.panel.state_of(LANE), "working")
        self.assertEqual(adapter.pending, [])

    def test_elicitation_request_blocks_and_completion_resolves(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_USER_MESSAGE)
        fake.emit(EVENT_ELICITATION_REQUESTED,
                  SimpleNamespace(request_id="el-1", message="Pick a region"))
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.assertEqual(adapter.pending, ["el-1"])
        fake.emit(EVENT_ELICITATION_COMPLETED,
                  SimpleNamespace(request_id="el-1", action="accept"))
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_idle_with_an_open_wait_stays_blocked_until_it_resolves(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_USER_MESSAGE)
        fake.emit(EVENT_PERMISSION_REQUESTED, SimpleNamespace(
            request_id="perm-9",
            permission_request=SimpleNamespace(kind="write", path="notes.txt")))
        fake.emit(EVENT_IDLE)
        # session.idle is done, but the wait keeps the lane blocked, and the
        # adapter does not resolve it early
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        self.assertEqual(adapter.pending, ["perm-9"])
        fake.emit(EVENT_PERMISSION_COMPLETED, SimpleNamespace(request_id="perm-9"))
        self.assertEqual(self.panel.state_of(LANE), "done")
        self.assertEqual(adapter.pending, [])

    def test_concurrent_waits_survive_each_other(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_USER_MESSAGE)
        fake.emit(EVENT_PERMISSION_REQUESTED, SimpleNamespace(
            request_id="wp-1",
            permission_request=SimpleNamespace(kind="write", path="a.txt")))
        fake.emit(EVENT_USER_INPUT_REQUESTED,
                  {"requestId": "q-2", "question": "and this one?"})
        self.assertEqual(adapter.pending, ["q-2", "wp-1"])
        fake.emit(EVENT_PERMISSION_COMPLETED, SimpleNamespace(request_id="wp-1"))
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        fake.emit(EVENT_USER_INPUT_COMPLETED, {"requestId": "q-2", "answer": "yes"})
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_task_complete_is_information_never_the_completion_signal(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_USER_MESSAGE)
        fake.emit(EVENT_TASK_COMPLETE, SimpleNamespace(success=True, summary="all green"))
        self.assertEqual(self.panel.state_of(LANE), "working")
        info = self.panel.info_of(LANE)
        self.assertEqual(info["task"], "complete")
        self.assertIs(info["task_success"], True)

    def test_error_reports_error(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_ERROR, SimpleNamespace(error_type="model_call", message="boom"))
        self.assertEqual(self.panel.state_of(LANE), "error")
        self.assertIn("boom", self.panel.info_of(LANE)["error"])


class HandlerPreservationTest(AdapterTest):
    def test_the_apps_own_listeners_keep_working_unchanged(self):
        adapter = self.adapter()
        fake = FakeSession()
        decision = {"kind": "approve-once"}
        seen = []

        def app_handler(event):
            seen.append(event)
            return decision

        fake.on(EVENT_PERMISSION_REQUESTED, app_handler)
        self.assertTrue(adapter.attach(fake))
        results = fake.emit(EVENT_PERMISSION_REQUESTED, SimpleNamespace(
            request_id="p-1",
            permission_request=SimpleNamespace(kind="shell", full_command_text="ls")))
        self.assertEqual(len(seen), 1)           # the app handler still ran
        self.assertIs(results[0], decision)      # with its decision untouched
        self.assertIs(results[1], None)          # and the observer added none
        self.assertEqual(self.panel.state_of(LANE), "blocked")

    def test_wrapped_permission_handler_returns_the_original_decision(self):
        adapter = self.adapter()
        order = []
        decision = {"kind": "approve-once"}

        def original(request, invocation):
            order.append("app")
            return decision

        wrapped = adapter.wrap_permission_handler(original)
        result = wrapped(SimpleNamespace(kind="shell", full_command_text="rm -rf build",
                                         tool_call_id="call-7"),
                         {"session_id": SESSION})
        self.assertEqual(order, ["app"])         # the original ran
        self.assertIs(result, decision)          # and its decision came back unchanged
        self.assertEqual(adapter.pending, [])    # the wait was recorded and resolved
        pushed = [p for _, path, p in self.panel.requests if path == "/session/info"]
        wait_ids = [i for p in pushed
                    for i in (p.get("info") or {}).get("pending_requests") or []]
        self.assertIn("call-7", wait_ids)        # keyed by the tool call id

    def test_wrapped_async_handler_stays_blocked_while_it_awaits(self):
        adapter = self.adapter()
        decision = {"kind": "approve-once"}

        async def original(request, invocation):
            await asyncio.sleep(0)
            return decision

        wrapped = adapter.wrap_permission_handler(original)
        coroutine = wrapped(SimpleNamespace(kind="write", path="notes.txt",
                                            tool_call_id="call-8"),
                            {"session_id": SESSION})
        self.assertEqual(self.panel.state_of(LANE), "blocked")
        result = asyncio.run(coroutine)
        self.assertIs(result, decision)
        self.assertEqual(self.panel.state_of(LANE), "idle")
        self.assertEqual(adapter.pending, [])

    def test_wrapped_user_input_handler_passes_the_answer_through(self):
        adapter = self.adapter()
        answer = {"answer": "staging", "wasFreeform": True}

        def original(request, invocation):
            return answer

        wrapped = adapter.wrap_user_input_handler(original)
        result = wrapped({"question": "Deploy where?", "requestId": "ask-9"},
                         {"session_id": SESSION})
        self.assertIs(result, answer)
        self.assertEqual(adapter.pending, [])
        pushed = [p for _, path, p in self.panel.requests if path == "/session/info"]
        wait_ids = [i for p in pushed
                    for i in (p.get("info") or {}).get("pending_requests") or []]
        self.assertIn("ask-9", wait_ids)

    def test_a_raising_handler_propagates_and_touches_nothing(self):
        adapter = self.adapter()

        def original(request, invocation):
            raise RuntimeError("the app said no")

        wrapped = adapter.wrap_permission_handler(original)
        with self.assertRaises(RuntimeError):
            wrapped(SimpleNamespace(kind="shell"), {})
        self.assertNotIn(LANE, self.panel.sessions)
        self.assertEqual(adapter.pending, [])


class WiringTest(AdapterTest):
    def test_the_python_sdk_shape_registers_one_all_event_handler(self):
        sdk = FakeSdkSession()
        adapter = observe(sdk, log=self.logs.append)
        self.assertIsNotNone(adapter)
        self.adapters.append(adapter)
        self.assertEqual(len(sdk.handlers), 1)
        sdk.emit(EVENT_USER_MESSAGE, SimpleNamespace(content="hello"))
        self.assertEqual(self.panel.state_of("copilot:sdk-1"), "working")
        sdk.emit(EVENT_PERMISSION_REQUESTED, SimpleNamespace(
            request_id="p-1",
            permission_request=SimpleNamespace(kind="shell", full_command_text="ls")))
        self.assertEqual(self.panel.state_of("copilot:sdk-1"), "blocked")
        sdk.emit(EVENT_PERMISSION_COMPLETED, SimpleNamespace(request_id="p-1"))
        sdk.emit(_Kind.IDLE)                     # enum member, like the SDK's
        self.assertEqual(self.panel.state_of("copilot:sdk-1"), "done")

    def test_attach_is_idempotent_and_detach_stops_the_reports(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        self.assertTrue(adapter.attach(fake))    # twice is a no-op
        self.assertEqual(len(fake.listeners[EVENT_IDLE]), 1)
        fake.emit(EVENT_USER_MESSAGE)
        adapter.detach()
        self.assertEqual(fake.off_calls, len(WATCHED))
        self.assertTrue(all(not handlers for handlers in fake.listeners.values()))
        fake.emit(EVENT_IDLE)
        self.assertEqual(self.panel.state_of(LANE), "working")

    def test_attach_refuses_objects_it_cannot_register_with(self):
        adapter = self.adapter()
        self.assertFalse(adapter.attach(object()))

        class Hostile:
            def on(self, *args):
                raise RuntimeError("no listeners here")

        self.assertFalse(adapter.attach(Hostile()))
        self.assertEqual(adapter.pending, [])

    def test_observe_returns_none_when_it_cannot_derive_an_id(self):
        self.assertIsNone(observe(object(), log=self.logs.append))
        self.assertIsNone(observe(FakeSession(), log=self.logs.append))


class FailureToleranceTest(AdapterTest):
    def test_every_event_is_survivable_when_the_daemon_answers_with_errors(self):
        adapter = self.adapter()
        fake = self.session(adapter)
        fake.emit(EVENT_USER_MESSAGE)            # lane is claimed
        self.panel.offline = True                # every request answers 503
        events = [
            (EVENT_TURN_START, None),
            (EVENT_TOOL_START, SimpleNamespace(tool_name="bash")),
            (EVENT_PERMISSION_REQUESTED, SimpleNamespace(
                request_id="down-1",
                permission_request=SimpleNamespace(kind="shell",
                                                   full_command_text="ls"))),
            (EVENT_USER_INPUT_REQUESTED, {"requestId": "down-2", "question": "?"}),
            (EVENT_ELICITATION_REQUESTED,
             SimpleNamespace(request_id="down-3", message="?")),
            (EVENT_PERMISSION_COMPLETED, SimpleNamespace(request_id="down-1")),
            (EVENT_USER_INPUT_COMPLETED, {"requestId": "down-2"}),
            (EVENT_ELICITATION_COMPLETED, SimpleNamespace(request_id="down-3")),
            (EVENT_TASK_COMPLETE, SimpleNamespace(success=False)),
            (EVENT_ERROR, SimpleNamespace(error_type="system", message="down")),
            (EVENT_IDLE, None),
        ]
        for name, data in events:
            fake.emit(name, data)                # must not raise
        self.assertFalse(adapter.reporter.online)
        adapter.close()                          # must not raise either

    def test_every_event_is_survivable_when_the_panel_is_unreachable(self):
        # port 0 is never listened on: a connection failure, not an HTTP error
        adapter = self.adapter(url="http://127.0.0.1:0", token="tok")
        fake = self.session(adapter)
        for name, data in [
            (EVENT_USER_MESSAGE, None),
            (EVENT_PERMISSION_REQUESTED, SimpleNamespace(
                request_id="gone-1",
                permission_request=SimpleNamespace(kind="write", path="a.txt"))),
            (EVENT_TASK_COMPLETE, SimpleNamespace(success=True)),
            (EVENT_ERROR, SimpleNamespace(message="gone")),
            (EVENT_IDLE, None),
        ]:
            fake.emit(name, data)                # must not raise
        self.assertFalse(adapter.reporter.online)
        adapter.close()                          # must not raise either


if __name__ == "__main__":
    unittest.main()
