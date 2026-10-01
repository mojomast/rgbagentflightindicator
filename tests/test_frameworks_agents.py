"""Framework adapters driven with fake framework objects - neither SDK is installed.

The OpenAI Agents SDK and Pydantic AI are optional dependencies; this file
imports neither. It hands the adapters the same shapes the frameworks do -
run hooks, interruption items, deferred-tool events, run results - and checks
the lane rules against the real HTTP ``MockPanel``:

* the root run owns the lamp and starts as ``working``;
* a child agent or handoff completing never finishes the root;
* an approval interruption blocks (one request per id) until resolved, and
  only then is the run reported ``done``;
* an external/background deferred call stays ``working``, never ``blocked``;
* a resume with the same conversation id reuses the lane, no second claim;
* an exception through the wrapper is reported as ``error`` and re-raised;
* a dead panel never raises into the application.
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import tempfile
import unittest
import uuid
from unittest import mock

from rgi.integrations import load as load_adapter
from rgi.integrations import openai_agents as oa
from rgi.integrations import pydantic_ai as pa
from tests.mock_panel import MockPanel


# -- fakes shaped like the frameworks ---------------------------------------

class FakeAgent:
    def __init__(self, name: str):
        self.name = name


class FakeInterruption:
    """Stands in for ``agents.items.ToolApprovalItem``."""

    def __init__(self, call_id: str, name: str):
        self.call_id = call_id
        self.name = name


class FakeResult:
    """Stands in for ``agents.RunResult``."""

    def __init__(self, interruptions=(), final_output="ok"):
        self.interruptions = list(interruptions)
        self.final_output = final_output


class FakeCall:
    """Stands in for ``pydantic_ai.messages.ToolCallPart``."""

    def __init__(self, tool_call_id: str, tool_name: str):
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name


class FakeRequests:
    """Stands in for ``pydantic_ai.tools.DeferredToolRequests``."""

    def __init__(self, approvals=(), calls=()):
        self.approvals = list(approvals)
        self.calls = list(calls)


class FakeRequestsEvent:
    event_kind = "deferred_tool_requests"

    def __init__(self, requests):
        self.requests = requests


class FakeResultsEvent:
    event_kind = "deferred_tool_results"

    def __init__(self, results):
        self.results = results


class FakeResults:
    """Stands in for ``pydantic_ai.tools.DeferredToolResults``."""

    def __init__(self, approvals=None, calls=None):
        self.approvals = dict(approvals or {})
        self.calls = dict(calls or {})


class FakeToolEvent:
    def __init__(self, tool_name, kind="function_tool_call"):
        self.part = FakeCall("call-1", tool_name)
        self.event_kind = kind


class FakeRunResult:
    """Stands in for ``pydantic_ai.AgentRunResult``."""

    def __init__(self, output):
        self.output = output


async def _aiter(events):
    for event in events:
        yield event


def _drive(handler, events) -> None:
    """Run an async event-stream handler over a finite list of fake events."""

    async def runner():
        await handler(None, _aiter(events))

    asyncio.run(runner())


class PanelCase(unittest.TestCase):
    """A live mock panel; the adapters are pointed at it through the env."""

    def setUp(self):
        self.panel = MockPanel(token="tok", lanes=6)
        self.panel.__enter__()
        self.addCleanup(self.panel.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {
            "RGI_URL": self.panel.url,
            "RGI_TOKEN": "tok",
            "RGI_IDENT": "test-machine",
            "RGI_STATE_DIR": os.path.join(self.tmp.name, "state"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, name: str = "run") -> str:
        return f"{name}-{uuid.uuid4().hex[:10]}"

    def key(self, namespace: str, session: str) -> str:
        return f"{namespace}:{session}"

    def kill_panel(self) -> None:
        self.panel.stop()
        self.panel.stop = lambda: None       # the registered cleanup is a no-op now


# -- OpenAI Agents SDK adapter ---------------------------------------------

class OpenAIAgentsTest(PanelCase):
    def test_root_run_reports_working_then_done(self):
        session = self.session("root")
        key = self.key(oa.NAMESPACE, session)
        with oa.report_run(session, label="triage") as run:
            self.assertEqual(self.panel.state_of(key), "working")
            self.assertIsNotNone(run.reporter)
        self.assertEqual(self.panel.state_of(key), "done")

    def test_child_and_handoff_completion_do_not_finish_the_root(self):
        session = self.session("children")
        key = self.key(oa.NAMESPACE, session)
        with oa.report_run(session) as run:
            hooks = run.hooks
            asyncio.run(hooks.on_agent_start(None, FakeAgent("triage")))
            asyncio.run(hooks.on_handoff(None, FakeAgent("triage"), FakeAgent("billing")))
            self.assertEqual(self.panel.state_of(key), "working")   # handoff, not finished
            asyncio.run(hooks.on_agent_end(None, FakeAgent("billing"), "final output"))
            # an individual agent finishing is still not the root finishing
            self.assertEqual(self.panel.state_of(key), "working")
            children = {c["id"]: c for c in self.panel.info_of(key).get("children", [])}
            self.assertEqual(children["triage"]["state"], "done")
            self.assertEqual(children["billing"]["state"], "done")
        self.assertEqual(self.panel.state_of(key), "done")

    def test_an_approval_interruption_blocks_until_resolved_then_done(self):
        session = self.session("approval")
        key = self.key(oa.NAMESPACE, session)
        with oa.report_run(session) as run:
            run.observe(FakeResult([FakeInterruption("call-1", "send_email")]))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["call-1"])
            self.assertEqual(self.panel.info_of(key).get("pending_requests"), ["call-1"])
            run.resolve("call-1")
            self.assertEqual(self.panel.state_of(key), "working")
            run.observe(FakeResult([]))                       # the resumed run finished
            self.assertEqual(self.panel.state_of(key), "done")
            self.assertEqual(run.pending, [])
        self.assertEqual(self.panel.state_of(key), "done")

    def test_two_interruptions_need_two_resolutions(self):
        session = self.session("two")
        key = self.key(oa.NAMESPACE, session)
        with oa.report_run(session) as run:
            run.observe(FakeResult([FakeInterruption("a", "tool_a"),
                                    FakeInterruption("b", "tool_b")]))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["a", "b"])
            run.resolve("a")
            self.assertEqual(self.panel.state_of(key), "blocked")   # b is still open
            run.resolve("b")
            self.assertEqual(self.panel.state_of(key), "working")

    def test_a_resumed_result_resolves_only_the_waits_that_are_gone(self):
        session = self.session("partial")
        key = self.key(oa.NAMESPACE, session)
        with oa.report_run(session) as run:
            run.observe(FakeResult([FakeInterruption("a", "tool_a"),
                                    FakeInterruption("b", "tool_b")]))
            run.observe(FakeResult([FakeInterruption("b", "tool_b")]))   # a was answered
            self.assertEqual(run.pending, ["b"])
            self.assertEqual(self.panel.state_of(key), "blocked")

    def test_resume_keeps_the_pause_blocked_until_the_human_answers(self):
        session = self.session("resume-approval")
        key = self.key(oa.NAMESPACE, session)
        with oa.report_run(session) as first:
            first.observe(FakeResult([FakeInterruption("call-9", "wire_money")]))
        self.assertEqual(self.panel.state_of(key), "blocked")     # no premature done
        with oa.report_run(session) as second:
            self.assertEqual(self.panel.state_of(key), "blocked")  # still waiting
            self.assertEqual(second.pending, ["call-9"])
            second.observe(FakeResult([]))                        # now it was answered
            self.assertEqual(self.panel.state_of(key), "done")

    def test_resume_with_the_same_id_reuses_the_lane_without_a_second_claim(self):
        session = self.session("resume-lane")
        key = self.key(oa.NAMESPACE, session)
        with oa.report_run(session):
            slot = self.panel.sessions[key]["slot"]
        with oa.report_run(session):
            self.assertEqual(self.panel.state_of(key), "working")
        self.assertEqual(self.panel.state_of(key), "done")
        self.assertEqual(self.panel.sessions[key]["slot"], slot)
        self.assertEqual(len(self.panel.requests_for("/session/start")), 1)
        ours = [k for k in self.panel.sessions if k.startswith(oa.NAMESPACE + ":")]
        self.assertEqual(ours, [key])

    def test_an_exception_through_the_wrapper_reports_error_and_reraises(self):
        session = self.session("error")
        key = self.key(oa.NAMESPACE, session)
        with self.assertRaises(RuntimeError):
            with oa.report_run(session):
                raise RuntimeError("model exploded")
        self.assertEqual(self.panel.state_of(key), "error")
        self.assertIn("model exploded", self.panel.info_of(key).get("error", ""))

    def test_odd_hook_arguments_never_raise_into_the_run(self):
        session = self.session("hooks")
        key = self.key(oa.NAMESPACE, session)
        with oa.report_run(session) as run:
            hooks = run.hooks
            asyncio.run(hooks.on_agent_start(None, None))
            asyncio.run(hooks.on_agent_end(None, object(), None))
            asyncio.run(hooks.on_handoff(None, None, None))
            asyncio.run(hooks.on_tool_start(None, None, None))
            asyncio.run(hooks.on_tool_end(None, None, object(), None))
            self.assertEqual(self.panel.state_of(key), "working")

    def test_a_dead_panel_never_raises(self):
        self.kill_panel()
        session = self.session("dead")
        with oa.report_run(session, timeout=0.1) as run:
            run.observe(FakeResult([FakeInterruption("call-1", "send_email")]))
            run.resolve("call-1")
            run.heartbeat()
            run.info({"k": "v"})
            run.child("helper")
            run.done()
        # reaching the end is the assertion: nothing raised


# -- Pydantic AI adapter ----------------------------------------------------

class PydanticAITest(PanelCase):
    def test_root_run_reports_working_then_done(self):
        session = self.session("root")
        key = self.key(pa.NAMESPACE, session)
        with pa.report_run(session, label="orders") as run:
            self.assertEqual(self.panel.state_of(key), "working")
            self.assertIn("event_stream_handler", run.run_kwargs)
        self.assertEqual(self.panel.state_of(key), "done")

    def test_deferred_approval_blocks_with_its_id_and_resolves(self):
        session = self.session("approval")
        key = self.key(pa.NAMESPACE, session)
        with pa.report_run(session) as run:
            _drive(run.event_stream_handler, [
                FakeRequestsEvent(FakeRequests(approvals=[FakeCall("ap-1", "delete_file")]))])
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["ap-1"])
            self.assertEqual(self.panel.info_of(key).get("pending_requests"), ["ap-1"])
            _drive(run.event_stream_handler, [FakeResultsEvent(FakeResults({"ap-1": True}))])
            self.assertEqual(self.panel.state_of(key), "working")
            run.observe(FakeRunResult(output="done"))
            self.assertEqual(self.panel.state_of(key), "done")
            self.assertEqual(run.pending, [])

    def test_external_background_call_stays_working_not_blocked(self):
        session = self.session("external")
        key = self.key(pa.NAMESPACE, session)
        with pa.report_run(session) as run:
            _drive(run.event_stream_handler, [
                FakeRequestsEvent(FakeRequests(calls=[FakeCall("bg-1", "slow_job")]))])
            self.assertEqual(self.panel.state_of(key), "working")
            self.assertEqual(run.pending, [])
            self.assertEqual(run.external, ["bg-1"])
            info = self.panel.info_of(key)
            self.assertEqual(info["deferred"]["external"], ["bg-1"])
            self.assertNotIn("pending_requests", info)
            run.observe(FakeRunResult(output="finished"))     # the result came back
            self.assertEqual(self.panel.state_of(key), "done")

    def test_a_mixed_batch_blocks_for_the_approval_but_keeps_external_visible(self):
        session = self.session("mixed")
        key = self.key(pa.NAMESPACE, session)
        with pa.report_run(session) as run:
            run.observe(FakeRunResult(output=FakeRequests(
                approvals=[FakeCall("ap-2", "wire")],
                calls=[FakeCall("bg-2", "index_rebuild")])))
            self.assertEqual(self.panel.state_of(key), "blocked")   # a human is needed
            self.assertEqual(run.pending, ["ap-2"])
            self.assertEqual(run.external, ["bg-2"])
            self.assertEqual(self.panel.info_of(key)["deferred"]["external"], ["bg-2"])
            run.resolve("ap-2")
            self.assertEqual(self.panel.state_of(key), "working")   # external work continues
            run.observe(FakeRunResult(output="done"))
            self.assertEqual(self.panel.state_of(key), "done")

    def test_deferred_output_seen_without_the_event_stream(self):
        session = self.session("observe")
        key = self.key(pa.NAMESPACE, session)
        with pa.report_run(session) as run:
            run.observe(FakeRunResult(output=FakeRequests(
                approvals=[FakeCall("ap-3", "send_invoice")])))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["ap-3"])
            run.observe(FakeRunResult(output="sent"))
            self.assertEqual(self.panel.state_of(key), "done")

    def test_resume_reuses_the_lane_without_a_second_claim(self):
        session = self.session("resume-lane")
        key = self.key(pa.NAMESPACE, session)
        with pa.report_run(session):
            slot = self.panel.sessions[key]["slot"]
        with pa.report_run(session):
            self.assertEqual(self.panel.state_of(key), "working")
        self.assertEqual(self.panel.state_of(key), "done")
        self.assertEqual(self.panel.sessions[key]["slot"], slot)
        self.assertEqual(len(self.panel.requests_for("/session/start")), 1)
        ours = [k for k in self.panel.sessions if k.startswith(pa.NAMESPACE + ":")]
        self.assertEqual(ours, [key])

    def test_resume_keeps_the_pause_blocked_until_resolved(self):
        session = self.session("resume-approval")
        key = self.key(pa.NAMESPACE, session)
        with pa.report_run(session) as first:
            first.observe(FakeRunResult(output=FakeRequests(
                approvals=[FakeCall("ap-9", "publish")])))
        self.assertEqual(self.panel.state_of(key), "blocked")
        with pa.report_run(session) as second:
            self.assertEqual(self.panel.state_of(key), "blocked")
            second.observe(FakeRunResult(output="published"))
            self.assertEqual(self.panel.state_of(key), "done")

    def test_event_stream_errors_do_not_escape_into_the_run(self):
        session = self.session("stream-error")
        key = self.key(pa.NAMESPACE, session)

        async def events():
            yield FakeRequestsEvent(FakeRequests(approvals=[FakeCall("ap-4", "x")]))
            raise ValueError("stream broke")

        async def drive():
            await run.event_stream_handler(None, events())

        with pa.report_run(session) as run:
            asyncio.run(drive())
            self.assertEqual(self.panel.state_of(key), "blocked")   # what arrived counted

    def test_tool_events_are_metadata_and_do_not_change_the_lane(self):
        session = self.session("tools")
        key = self.key(pa.NAMESPACE, session)
        with pa.report_run(session) as run:
            _drive(run.event_stream_handler, [FakeToolEvent("search_docs")])
            self.assertEqual(self.panel.state_of(key), "working")
            self.assertEqual(self.panel.info_of(key).get("tool"), "search_docs")

    def test_an_exception_through_the_wrapper_reports_error_and_reraises(self):
        session = self.session("error")
        key = self.key(pa.NAMESPACE, session)
        with self.assertRaises(RuntimeError):
            with pa.report_run(session):
                raise RuntimeError("provider exploded")
        self.assertEqual(self.panel.state_of(key), "error")
        self.assertIn("provider exploded", self.panel.info_of(key).get("error", ""))

    def test_a_dead_panel_never_raises(self):
        self.kill_panel()
        session = self.session("dead")
        with pa.report_run(session, timeout=0.1) as run:
            run.observe(FakeRunResult(output=FakeRequests(
                approvals=[FakeCall("ap-5", "send_invoice")])))
            _drive(run.event_stream_handler,
                   [FakeResultsEvent({"ap-5": True})])
            run.heartbeat()
            run.info({"k": "v"})
            run.done()
        # reaching the end is the assertion: nothing raised


# -- both modules are importable without either framework -------------------

class LazyImportTest(unittest.TestCase):
    FRAMEWORKS = {"agents", "openai", "pydantic_ai", "pydantic"}

    def test_adapters_do_not_import_their_framework_at_module_level(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for relative in ("rgi/integrations/openai_agents.py",
                         "rgi/integrations/pydantic_ai.py"):
            tree = ast.parse((root / relative).read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif (isinstance(node, ast.ImportFrom) and node.level == 0
                      and node.module):
                    imported.add(node.module.split(".")[0])
            overlap = imported & self.FRAMEWORKS
            self.assertFalse(overlap, f"{relative} imports {sorted(overlap)} at module level")

    def test_the_registry_loads_both_adapters(self):
        self.assertTrue(hasattr(load_adapter("openai-agents"), "report_run"))
        self.assertTrue(hasattr(load_adapter("pydantic-ai"), "report_run"))


if __name__ == "__main__":
    unittest.main()
