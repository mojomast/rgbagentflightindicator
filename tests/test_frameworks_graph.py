"""Framework adapters driven with fake framework objects - no frameworks installed.

LangGraph, CrewAI and the Microsoft Agent Framework are optional dependencies;
this file imports none of them. It hands each adapter the same shapes the
frameworks emit - stream chunks and callback events, bus events, workflow
events and middleware contexts - and checks the lane rules against the real
HTTP ``MockPanel``:

* an interrupt / request_info blocks by id and a stream ending does not
  complete the lane; answering returns it to ``working`` and only then is the
  run ``done``;
* a child agent, task or executor completing updates children metadata and
  never finishes the root;
* concurrent executions get separate lanes (two sessions, two namespaces) and
  do not cross-resolve;
* an error path reports ``error`` (and an exception through the wrapper is
  re-raised);
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
from types import SimpleNamespace
from unittest import mock

from rgi.integrations import crewai as ca
from rgi.integrations import langgraph as lg
from rgi.integrations import load as load_adapter
from rgi.integrations import ms_agent as ma
from tests.mock_panel import MockPanel


# -- fakes shaped like the frameworks ---------------------------------------

class FakeInterrupt:
    """Stands in for ``langgraph.types.Interrupt``."""

    def __init__(self, iid: str, value: str):
        self.id = iid
        self.value = value


class FakeCallback:
    """Stands in for a LangChain callback event (``on_chain_*``)."""

    def __init__(self, event: str, run_id: str = "run-1", parent_ids=(),
                 name: str = "LangGraph", metadata=None, data=None):
        self.event = event
        self.run_id = run_id
        self.parent_ids = list(parent_ids)
        self.name = name
        self.metadata = metadata
        self.data = data


class FakeCommand:
    """Stands in for ``langgraph.types.Command``."""

    def __init__(self, resume):
        self.resume = resume
        self.goto = ()
        self.update = None


class FakeLifecycle:
    """Stands in for ``GraphInterruptEvent`` / ``GraphResumeEvent``."""

    def __init__(self, interrupts=(), checkpoint_id="ck-1"):
        self.interrupts = tuple(interrupts)
        self.checkpoint_id = checkpoint_id
        self.run_id = None
        self.checkpoint_ns = ()


class FakeCrewEvent:
    """Stands in for a CrewAI bus event (its ``.type`` plus payload fields)."""

    def __init__(self, type: str, **fields):
        self.type = type
        for name, value in fields.items():
            setattr(self, name, value)


class FakeWorkflowEvent:
    """Stands in for ``agent_framework.WorkflowEvent``."""

    def __init__(self, type: str, **fields):
        self.type = type
        for name, value in fields.items():
            setattr(self, name, value)


class FakeApprovalRequest:
    """Stands in for a ``function_approval_request`` Content."""

    def __init__(self, request_id: str, name: str):
        self.type = "function_approval_request"
        self.id = request_id
        self.user_input_request = True
        self.function_call = SimpleNamespace(call_id=request_id, name=name)


class FakeAgentResponse:
    """Stands in for a settled ``AgentResponse`` (outside the framework)."""

    def __init__(self, requests=()):
        self.messages = []
        self.user_input_requests = list(requests)


class FakeAgentUpdate:
    """Stands in for a streamed ``AgentResponseUpdate``."""

    def __init__(self, requests=()):
        self.contents = []
        self.user_input_requests = list(requests)


class FakeContext:
    """Stands in for ``AgentContext`` in a middleware invocation."""

    def __init__(self, result=None):
        self.result = result
        self.metadata = {}


class FakeWorkflowResult:
    """Stands in for ``WorkflowRunResult`` (a list of events plus getters)."""

    def __init__(self, events=(), requests=(), state=None):
        self.events = list(events)
        self._requests = list(requests)
        self._state = state

    def __iter__(self):
        return iter(self.events)

    def get_request_info_events(self):
        return list(self._requests)

    def get_final_state(self):
        if self._state is None:
            raise RuntimeError("no status events were emitted")
        return self._state


async def _noop():
    return None


class PanelCase(unittest.TestCase):
    """A live mock panel; the adapters are pointed at it through the env."""

    def setUp(self):
        self.panel = MockPanel(token="tok", lanes=8)
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

    def lanes(self, namespace: str) -> list[str]:
        return [k for k in self.panel.sessions
                if k.startswith(namespace + ":")]

    def kill_panel(self) -> None:
        self.panel.stop()
        self.panel.stop = lambda: None       # the registered cleanup is a no-op now


# -- LangGraph adapter --------------------------------------------------------

class LangGraphTest(PanelCase):
    def test_root_run_reports_working_then_done(self):
        thread = self.session("root")
        key = self.key(lg.NAMESPACE, thread)
        with lg.report_run(thread, label="research") as run:
            self.assertEqual(self.panel.state_of(key), "working")
            self.assertEqual(run.thread_id, thread)
            self.assertEqual(run.config["configurable"]["thread_id"], thread)
            self.assertIsNotNone(run.handler)
        self.assertEqual(self.panel.state_of(key), "done")
        self.assertEqual(len(self.panel.requests_for("/session/start")), 1)

    def test_interrupt_blocks_and_a_stream_end_keeps_it_blocked(self):
        thread = self.session("interrupt")
        key = self.key(lg.NAMESPACE, thread)
        with lg.report_run(thread, label="deploy") as run:
            self.assertTrue(run.observe(
                {"__interrupt__": (FakeInterrupt("int-1", "approve the deploy?"),)}))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["int-1"])
            self.assertEqual(self.panel.info_of(key).get("pending_requests"), ["int-1"])
            # a root run "ending" while paused is not completion
            run.observe(FakeCallback("on_chain_end",
                                     data={"__interrupt__": (FakeInterrupt("int-1", "?"),)}))
            run.observe(FakeCallback("on_chain_end", data={}))
            self.assertEqual(self.panel.state_of(key), "blocked")
        # the stream ended too: the wait outlives it
        self.assertEqual(self.panel.state_of(key), "blocked")
        self.assertEqual(run.pending, ["int-1"])

    def test_resume_returns_to_working_then_done_on_the_same_lane(self):
        thread = self.session("resume")
        key = self.key(lg.NAMESPACE, thread)
        with lg.report_run(thread) as first:
            first.observe({"__interrupt__": (FakeInterrupt("int-2", "ok?"),)})
        self.assertEqual(self.panel.state_of(key), "blocked")
        slot = self.panel.sessions[key]["slot"]

        with lg.report_run(thread) as second:
            self.assertEqual(self.panel.state_of(key), "blocked")   # still waiting
            self.assertEqual(second.pending, ["int-2"])
            self.assertTrue(second.resume("int-2"))
            self.assertEqual(self.panel.state_of(key), "working")
            second.observe(FakeCommand(resume="approved"))           # a Command resume
            second.observe(FakeCallback("on_chain_start", run_id="root-2"))
            second.observe(FakeCallback("on_chain_end", run_id="root-2", data={}))
            self.assertEqual(self.panel.state_of(key), "done")
        self.assertEqual(self.panel.state_of(key), "done")
        self.assertEqual(self.panel.sessions[key]["slot"], slot)
        self.assertEqual(len(self.panel.requests_for("/session/start")), 1)
        self.assertEqual(self.lanes(lg.NAMESPACE), [key])

    def test_lifecycle_callbacks_drive_block_and_resume(self):
        thread = self.session("lifecycle")
        key = self.key(lg.NAMESPACE, thread)
        with lg.report_run(thread) as run:
            run.handler.on_interrupt(FakeLifecycle([FakeInterrupt("int-7", "approve?")]))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["int-7"])
            run.handler.on_resume(FakeLifecycle())
            self.assertEqual(self.panel.state_of(key), "working")
            self.assertEqual(run.pending, [])
        self.assertEqual(self.panel.state_of(key), "done")

    def test_tasks_and_node_completion_update_children_not_the_root(self):
        thread = self.session("children")
        key = self.key(lg.NAMESPACE, thread)
        with lg.report_run(thread) as run:
            run.observe(FakeCallback("on_chain_start", run_id="root"))
            run.observe(FakeCallback("on_chain_start", run_id="node-1",
                                     parent_ids=["root"], name="research",
                                     metadata={"langgraph_node": "research"}))
            run.observe(FakeCallback("on_chain_end", run_id="node-1",
                                     parent_ids=["root"]))
            self.assertEqual(self.panel.state_of(key), "working")
            children = {c["id"]: c for c in self.panel.info_of(key).get("children", [])}
            self.assertEqual(children["node-1"]["state"], "done")

            # stream_mode="tasks": started and result entries are children
            run.observe(("tasks", {"id": "task-2", "name": "writer"}))
            run.observe(("tasks", {"id": "task-2", "name": "writer",
                                   "result": {"draft": "done"}}))
            self.assertEqual(self.panel.state_of(key), "working")
            children = {c["id"]: c for c in self.panel.info_of(key).get("children", [])}
            self.assertEqual(children["task-2"]["state"], "done")

            run.observe(FakeCallback("on_chain_end", run_id="root", data={}))
            self.assertEqual(self.panel.state_of(key), "done")

    def test_a_task_interrupt_blocks_without_the_root_finishing(self):
        thread = self.session("task-interrupt")
        key = self.key(lg.NAMESPACE, thread)
        with lg.report_run(thread) as run:
            run.observe(("tasks", {"id": "task-3", "name": "writer",
                                   "interrupts": (FakeInterrupt("int-3", "type yes"),)}))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["int-3"])

    def test_v3_protocol_values_carry_interrupts(self):
        thread = self.session("v3")
        key = self.key(lg.NAMESPACE, thread)
        with lg.report_run(thread) as run:
            run.observe({"method": "values",
                         "params": {"namespace": [], "data": {"step": 1},
                                    "interrupts": (FakeInterrupt("int-9", "v3?"),)}})
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["int-9"])

    def test_a_state_snapshot_syncs_a_paused_then_finished_thread(self):
        thread = self.session("snapshot")
        key = self.key(lg.NAMESPACE, thread)
        paused = SimpleNamespace(
            config={"configurable": {"thread_id": thread}},
            next=("ask",), tasks=(), interrupts=(FakeInterrupt("int-s", "s?"),))
        finished = SimpleNamespace(
            config={"configurable": {"thread_id": thread}},
            next=(), tasks=(), interrupts=())
        with lg.report_run(thread) as run:
            run.observe(paused)
            self.assertEqual(self.panel.state_of(key), "blocked")
            run.resume("int-s")
            self.assertEqual(self.panel.state_of(key), "working")
            run.observe(finished)               # get_state() says the thread is over
            self.assertEqual(self.panel.state_of(key), "done")

    def test_known_pending_ids_reblock_a_fresh_report_run(self):
        thread = self.session("pending-arg")
        key = self.key(lg.NAMESPACE, thread)
        with lg.report_run(thread, pending=["int-q"]) as run:
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["int-q"])
            self.assertTrue(run.resume())
            self.assertEqual(self.panel.state_of(key), "working")
        self.assertEqual(self.panel.state_of(key), "done")

    def test_two_threads_get_two_lanes_and_do_not_cross_resolve(self):
        first, second = self.session("thread-a"), self.session("thread-b")
        key_a, key_b = self.key(lg.NAMESPACE, first), self.key(lg.NAMESPACE, second)
        with lg.report_run(first) as run_a, lg.report_run(second) as run_b:
            self.assertEqual(self.panel.state_of(key_a), "working")
            self.assertEqual(self.panel.state_of(key_b), "working")
            run_a.observe({"__interrupt__": (FakeInterrupt("int-a", "one?"),)})
            self.assertEqual(self.panel.state_of(key_a), "blocked")
            self.assertEqual(self.panel.state_of(key_b), "working")
            run_b.resume("int-a")                       # the other lane's request
            self.assertEqual(self.panel.state_of(key_a), "blocked")
            self.assertEqual(run_a.pending, ["int-a"])
            self.assertEqual(run_b.pending, [])
            self.assertEqual(len(self.lanes(lg.NAMESPACE)), 2)
        self.assertEqual(self.panel.state_of(key_b), "done")

    def test_error_paths_report_error_and_an_exception_is_reraised(self):
        crash = self.session("crash")
        key = self.key(lg.NAMESPACE, crash)
        with lg.report_run(crash) as run:
            run.observe(FakeCallback("on_chain_start", run_id="root"))
            run.observe(FakeCallback("on_chain_error", run_id="root",
                                     data=RuntimeError("model exploded")))
            self.assertEqual(self.panel.state_of(key), "error")
        self.assertEqual(self.panel.state_of(key), "error")     # exit keeps it

        wrapper = self.session("wrapper")
        crashed = self.key(lg.NAMESPACE, wrapper)
        with self.assertRaises(RuntimeError):
            with lg.report_run(wrapper):
                raise RuntimeError("graph wrapper exploded")
        self.assertEqual(self.panel.state_of(crashed), "error")

    def test_a_dead_panel_never_raises(self):
        self.kill_panel()
        thread = self.session("dead")
        with lg.report_run(thread, timeout=0.1) as run:
            run.observe({"__interrupt__": (FakeInterrupt("int-x", "x?"),)})
            run.observe(FakeCommand(resume="yes"))
            run.observe(FakeCallback("on_chain_start", run_id="root"))
            run.handler.on_interrupt(FakeLifecycle([FakeInterrupt("int-y", "y?")]))
            run.handler.on_resume(FakeLifecycle())
            run.child("helper")
            run.child_done("helper")
            run.heartbeat()
            run.info({"k": "v"})
            run.finish()
        # reaching the end is the assertion: nothing raised


# -- CrewAI adapter ------------------------------------------------------------

class CrewAITest(PanelCase):
    def listener(self) -> ca.CrewAIReporter:
        return ca.CrewAIReporter(label="crews")

    def test_kickoff_lifecycle_reports_working_then_done(self):
        panel = self.listener()
        panel.observe(FakeCrewEvent("crew_kickoff_started", event_id="kick-1",
                                    crew_name="nightly"))
        key = self.key(ca.NAMESPACE, "kick-1")
        self.assertEqual(self.panel.state_of(key), "working")
        self.assertEqual(self.panel.sessions[key]["label"], "nightly")
        panel.observe(FakeCrewEvent("crew_kickoff_completed", event_id="done-1",
                                    started_event_id="kick-1", crew_name="nightly"))
        self.assertEqual(self.panel.state_of(key), "done")

    def test_agent_and_task_completion_update_children_not_the_root(self):
        panel = self.listener()
        panel.observe(FakeCrewEvent("crew_kickoff_started", event_id="kick-2",
                                    crew_name="nightly"))
        key = self.key(ca.NAMESPACE, "kick-2")
        panel.observe(FakeCrewEvent("agent_execution_started", event_id="a-1",
                                    agent_id="agent-1", agent_role="researcher",
                                    parent_event_id="kick-2"))
        panel.observe(FakeCrewEvent("task_started", event_id="t-1",
                                    task_id="task-1", task_name="research",
                                    parent_event_id="a-1"))
        panel.observe(FakeCrewEvent("task_completed", event_id="t-2",
                                    task_id="task-1", task_name="research",
                                    started_event_id="t-1"))
        panel.observe(FakeCrewEvent("agent_execution_completed", event_id="a-2",
                                    agent_id="agent-1", agent_role="researcher",
                                    started_event_id="a-1"))
        self.assertEqual(self.panel.state_of(key), "working")    # nothing completed
        children = {c["id"]: c for c in self.panel.info_of(key).get("children", [])}
        self.assertEqual(children["task-1"]["state"], "done")
        self.assertEqual(children["agent-1"]["state"], "done")
        self.assertEqual(self.lanes(ca.NAMESPACE), [key])        # one lane only

        panel.observe(FakeCrewEvent("crew_kickoff_completed", event_id="done-2",
                                    started_event_id="kick-2", crew_name="nightly"))
        self.assertEqual(self.panel.state_of(key), "done")

    def test_human_feedback_blocks_and_received_resumes(self):
        panel = self.listener()
        panel.observe(FakeCrewEvent("crew_kickoff_started", event_id="kick-3",
                                    crew_name="review"))
        key = self.key(ca.NAMESPACE, "kick-3")
        panel.observe(FakeCrewEvent("human_feedback_requested", event_id="f-1",
                                    request_id="fb-1", method_name="review",
                                    message="Approve the draft?"))
        self.assertEqual(self.panel.state_of(key), "blocked")
        self.assertEqual(panel.run("kick-3").pending, ["fb-1"])
        panel.observe(FakeCrewEvent("human_feedback_received", event_id="f-2",
                                    request_id="fb-1", method_name="review"))
        self.assertEqual(self.panel.state_of(key), "working")

        # no request id on the event: the event id is the wait, and the
        # method remembers it so the matching *_received event finds it
        panel.observe(FakeCrewEvent("human_feedback_requested", event_id="f-3",
                                    method_name="second"))
        self.assertEqual(self.panel.state_of(key), "blocked")
        self.assertEqual(panel.run("kick-3").pending, ["f-3"])
        panel.observe(FakeCrewEvent("human_feedback_received", event_id="f-4",
                                    method_name="second"))
        self.assertEqual(self.panel.state_of(key), "working")
        panel.observe(FakeCrewEvent("crew_kickoff_completed", event_id="done-3",
                                    started_event_id="kick-3", crew_name="review"))
        self.assertEqual(self.panel.state_of(key), "done")

    def test_flow_pause_blocks_and_the_paused_method_restarting_resumes(self):
        panel = self.listener()
        panel.observe(FakeCrewEvent("flow_started", event_id="flow-1",
                                    flow_name="pipeline"))
        key = self.key(ca.NAMESPACE, "flow-1")
        panel.observe(FakeCrewEvent("flow_paused", event_id="p-1", flow_id="flow-1",
                                    method_name="review", message="waiting",
                                    parent_event_id="flow-1"))
        self.assertEqual(self.panel.state_of(key), "blocked")
        self.assertEqual(panel.run("flow-1").pending, ["paused:flow-1:review"])
        panel.observe(FakeCrewEvent("method_execution_started", event_id="m-2",
                                    flow_name="pipeline", method_name="review",
                                    parent_event_id="flow-1"))
        self.assertEqual(self.panel.state_of(key), "working")
        panel.observe(FakeCrewEvent("flow_finished", event_id="fin-1",
                                    flow_name="pipeline", started_event_id="flow-1"))
        self.assertEqual(self.panel.state_of(key), "done")

    def test_concurrent_kickoffs_get_separate_lanes_and_do_not_cross_resolve(self):
        panel = self.listener()
        panel.observe(FakeCrewEvent("crew_kickoff_started", event_id="kick-a",
                                    crew_name="alpha"))
        panel.observe(FakeCrewEvent("crew_kickoff_started", event_id="kick-b",
                                    crew_name="beta"))
        key_a = self.key(ca.NAMESPACE, "kick-a")
        key_b = self.key(ca.NAMESPACE, "kick-b")
        self.assertEqual(self.panel.state_of(key_a), "working")
        self.assertEqual(self.panel.state_of(key_b), "working")

        panel.observe(FakeCrewEvent("human_feedback_requested", event_id="fa",
                                    request_id="ra", method_name="review",
                                    parent_event_id="kick-a"))
        panel.observe(FakeCrewEvent("human_feedback_requested", event_id="fb",
                                    request_id="rb", method_name="review",
                                    parent_event_id="kick-b"))
        self.assertEqual(self.panel.state_of(key_a), "blocked")
        self.assertEqual(self.panel.state_of(key_b), "blocked")

        panel.observe(FakeCrewEvent("human_feedback_received", event_id="fa-2",
                                    request_id="ra", method_name="review"))
        self.assertEqual(self.panel.state_of(key_a), "working")
        self.assertEqual(self.panel.state_of(key_b), "blocked")   # not cross-resolved
        panel.observe(FakeCrewEvent("crew_kickoff_completed", event_id="da",
                                    started_event_id="kick-a", crew_name="alpha"))
        self.assertEqual(self.panel.state_of(key_a), "done")
        self.assertEqual(self.panel.state_of(key_b), "blocked")
        self.assertEqual(len(self.lanes(ca.NAMESPACE)), 2)

    def test_an_explicit_session_pins_flow_starts_and_resumes_to_one_lane(self):
        panel = ca.CrewAIReporter(label="pinned", session="pinned-flow")
        panel.observe(FakeCrewEvent("flow_started", event_id="flow-a",
                                    flow_name="pipeline"))
        key = self.key(ca.NAMESPACE, "pinned-flow")
        self.assertEqual(self.panel.state_of(key), "working")
        panel.observe(FakeCrewEvent("flow_paused", event_id="p-1", flow_id="pinned-flow",
                                    method_name="review", message="wait",
                                    parent_event_id="flow-a"))
        self.assertEqual(self.panel.state_of(key), "blocked")
        panel.observe(FakeCrewEvent("method_execution_started", event_id="m-1",
                                    flow_name="pipeline", method_name="review",
                                    parent_event_id="flow-a"))
        self.assertEqual(self.panel.state_of(key), "working")
        # a resumed flow emits a fresh flow_started: same lane, not a second one
        panel.observe(FakeCrewEvent("flow_started", event_id="flow-b",
                                    flow_name="pipeline"))
        panel.observe(FakeCrewEvent("flow_finished", event_id="fin-2",
                                    flow_name="pipeline",
                                    started_event_id="flow-b"))
        self.assertEqual(self.panel.state_of(key), "done")
        self.assertEqual(self.lanes(ca.NAMESPACE), [key])

    def test_a_failed_kickoff_reports_error(self):
        panel = self.listener()
        panel.observe(FakeCrewEvent("crew_kickoff_started", event_id="kick-err",
                                    crew_name="bad"))
        panel.observe(FakeCrewEvent("crew_kickoff_failed", event_id="err-1",
                                    started_event_id="kick-err",
                                    error="llm exploded"))
        key = self.key(ca.NAMESPACE, "kick-err")
        self.assertEqual(self.panel.state_of(key), "error")
        self.assertIn("llm exploded", self.panel.info_of(key).get("error", ""))

    def test_attach_and_listen_are_harmless_without_crewai(self):
        panel = self.listener()
        self.assertFalse(panel.attach())          # nothing installed to attach to
        self.assertFalse(panel.attached)
        with ca.listen(label="x") as listener:
            listener.observe(FakeCrewEvent("crew_kickoff_started", event_id="kick-l",
                                           crew_name="live"))
            self.assertEqual(self.panel.state_of(self.key(ca.NAMESPACE, "kick-l")),
                             "working")
        # detaching keeps the lane's state
        self.assertEqual(self.panel.state_of(self.key(ca.NAMESPACE, "kick-l")),
                         "working")

    def test_a_dead_panel_never_raises(self):
        self.kill_panel()
        panel = self.listener()
        panel.observe(FakeCrewEvent("crew_kickoff_started", event_id="kick-dead",
                                    crew_name="dead"))
        panel.observe(FakeCrewEvent("human_feedback_requested", event_id="f-dead",
                                    request_id="r-dead", method_name="review"))
        panel.observe(FakeCrewEvent("human_feedback_received", event_id="f-dead-2",
                                    request_id="r-dead", method_name="review"))
        panel.observe(FakeCrewEvent("crew_kickoff_completed", event_id="d-dead",
                                    started_event_id="kick-dead"))
        panel.observe(FakeCrewEvent("crew_kickoff_failed", event_id="x-dead",
                                    started_event_id="kick-dead", error="x"))
        panel.attach()
        panel.detach()
        panel.close()
        # reaching the end is the assertion: nothing raised


# -- Microsoft Agent Framework adapter ----------------------------------------

class MSAgentTest(PanelCase):
    def test_middleware_marks_the_root_run_working_then_done(self):
        session = self.session("root")
        key = self.key(ma.NAMESPACE, session)
        with ma.report_run(session, label="triage") as run:
            self.assertEqual(self.panel.state_of(key), "working")
            asyncio.run(run.middleware.process(FakeContext(None), _noop))
            self.assertEqual(self.panel.state_of(key), "done")
            self.assertEqual(run.run_kwargs["middleware"], [run.middleware])
        self.assertEqual(self.panel.state_of(key), "done")

    def test_request_info_blocks_by_request_id_until_answered(self):
        session = self.session("approval")
        key = self.key(ma.NAMESPACE, session)
        with ma.report_run(session) as run:
            run.observe(FakeWorkflowEvent("started"))
            run.observe(FakeWorkflowEvent("request_info", request_id="req-7",
                                          source_executor_id="approval",
                                          data="deploy?"))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["req-7"])
            self.assertEqual(self.panel.info_of(key).get("pending_requests"), ["req-7"])
            run.observe(FakeWorkflowEvent("status",
                                          state="IN_PROGRESS_PENDING_REQUESTS"))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertTrue(run.answer("req-7"))
            self.assertEqual(self.panel.state_of(key), "working")
            self.assertEqual(run.pending, [])
            run.observe(FakeWorkflowEvent("status", state="IDLE"))
            self.assertEqual(self.panel.state_of(key), "done")

    def test_executor_completion_is_not_workflow_completion(self):
        session = self.session("executors")
        key = self.key(ma.NAMESPACE, session)
        with ma.report_run(session) as run:
            run.observe(FakeWorkflowEvent("started"))
            run.observe(FakeWorkflowEvent("executor_invoked", executor_id="writer"))
            run.observe(FakeWorkflowEvent("executor_completed", executor_id="writer"))
            self.assertEqual(self.panel.state_of(key), "working")
            children = {c["id"]: c for c in self.panel.info_of(key).get("children", [])}
            self.assertEqual(children["writer"]["state"], "done")
            run.observe(FakeWorkflowEvent("status", state="IDLE"))
            self.assertEqual(self.panel.state_of(key), "done")

    def test_agent_response_approval_blocks_and_is_answered(self):
        session = self.session("agent-approval")
        key = self.key(ma.NAMESPACE, session)
        with ma.report_run(session) as run:
            response = FakeAgentResponse([FakeApprovalRequest("ap-1", "send_email")])
            run.observe(response)
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["ap-1"])
            asyncio.run(run.middleware.process(FakeContext(response), _noop))
            self.assertEqual(self.panel.state_of(key), "blocked")   # not done yet
            self.assertTrue(run.answer("ap-1"))
            self.assertEqual(self.panel.state_of(key), "working")
            asyncio.run(run.middleware.process(
                FakeContext(FakeAgentResponse()), _noop))
            self.assertEqual(self.panel.state_of(key), "done")

    def test_streamed_updates_keep_the_lane_working_until_finished(self):
        session = self.session("stream")
        key = self.key(ma.NAMESPACE, session)
        with ma.report_run(session) as run:
            run.observe(FakeAgentUpdate())
            self.assertEqual(self.panel.state_of(key), "working")
            run.observe(FakeAgentUpdate([FakeApprovalRequest("ap-2", "wire_money")]))
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["ap-2"])
            run.answer("ap-2")
            self.assertEqual(self.panel.state_of(key), "working")
            run.finish()
            self.assertEqual(self.panel.state_of(key), "done")

    def test_workflow_result_final_state_drives_completion(self):
        session = self.session("result")
        key = self.key(ma.NAMESPACE, session)
        with ma.report_run(session) as run:
            pending_result = FakeWorkflowResult(
                events=[FakeWorkflowEvent("started")],
                requests=[FakeWorkflowEvent("request_info", request_id="rq-1",
                                            source_executor_id="hitl",
                                            data="ok?")],
                state="IDLE_WITH_PENDING_REQUESTS")
            run.observe_result(pending_result)
            self.assertEqual(self.panel.state_of(key), "blocked")
            self.assertEqual(run.pending, ["rq-1"])
            run.answer("rq-1")
            run.observe_result(FakeWorkflowResult(state="IDLE"))
            self.assertEqual(self.panel.state_of(key), "done")

    def test_two_sessions_get_two_lanes_and_do_not_cross_resolve(self):
        first, second = self.session("one"), self.session("two")
        key_a, key_b = self.key(ma.NAMESPACE, first), self.key(ma.NAMESPACE, second)
        with ma.report_run(first) as run_a, ma.report_run(second) as run_b:
            run_a.observe(FakeWorkflowEvent("request_info", request_id="ra",
                                            source_executor_id="hitl", data="a?"))
            run_b.observe(FakeWorkflowEvent("request_info", request_id="rb",
                                            source_executor_id="hitl", data="b?"))
            self.assertEqual(self.panel.state_of(key_a), "blocked")
            self.assertEqual(self.panel.state_of(key_b), "blocked")
            run_b.answer("ra")                     # the other lane's request id
            self.assertEqual(self.panel.state_of(key_a), "blocked")
            self.assertEqual(run_a.pending, ["ra"])
            run_b.answer("rb")
            self.assertEqual(self.panel.state_of(key_b), "working")
            self.assertEqual(len(self.lanes(ma.NAMESPACE)), 2)

    def test_failed_workflow_reports_error_and_the_exit_keeps_it(self):
        session = self.session("failed")
        key = self.key(ma.NAMESPACE, session)
        with ma.report_run(session) as run:
            run.observe(FakeWorkflowEvent("started"))
            run.observe(FakeWorkflowEvent("failed", details=SimpleNamespace(
                message="executor exploded", error_type="RuntimeError")))
            self.assertEqual(self.panel.state_of(key), "error")
            self.assertIn("executor exploded", self.panel.info_of(key).get("error", ""))
        self.assertEqual(self.panel.state_of(key), "error")

        wrapper = self.session("wrapper")
        crashed = self.key(ma.NAMESPACE, wrapper)
        with self.assertRaises(RuntimeError):
            with ma.report_run(wrapper):
                raise RuntimeError("agent run exploded")
        self.assertEqual(self.panel.state_of(crashed), "error")

    def test_a_dead_panel_never_raises(self):
        self.kill_panel()
        session = self.session("dead")
        with ma.report_run(session, timeout=0.1) as run:
            run.observe(FakeWorkflowEvent("started"))
            run.observe(FakeWorkflowEvent("request_info", request_id="r-dead",
                                          source_executor_id="hitl", data="?"))
            run.answer("r-dead")
            run.observe(FakeAgentResponse([FakeApprovalRequest("ap-dead", "tool")]))
            asyncio.run(run.middleware.process(FakeContext(None), _noop))
            run.child("executor")
            run.child_done("executor")
            run.heartbeat()
            run.info({"k": "v"})
            run.finish()
        # reaching the end is the assertion: nothing raised


# -- lanes across namespaces ---------------------------------------------------

class LaneIsolationTest(PanelCase):
    def test_two_namespace_sessions_get_two_lanes_and_do_not_cross_resolve(self):
        thread = self.session("graph")
        session = self.session("agent")
        graph_key = self.key(lg.NAMESPACE, thread)
        agent_key = self.key(ma.NAMESPACE, session)
        with lg.report_run(thread) as graph_run, ma.report_run(session) as agent_run:
            graph_run.observe({"__interrupt__": (FakeInterrupt("lg-1", "ok?"),)})
            agent_run.observe(FakeWorkflowEvent("request_info", request_id="ms-1",
                                                source_executor_id="hitl",
                                                data="ok?"))
            self.assertEqual(self.panel.state_of(graph_key), "blocked")
            self.assertEqual(self.panel.state_of(agent_key), "blocked")
            self.assertEqual(len(self.panel.sessions), 2)
            agent_run.answer("ms-1")
            self.assertEqual(self.panel.state_of(agent_key), "working")
            self.assertEqual(self.panel.state_of(graph_key), "blocked")   # untouched
            self.assertEqual(graph_run.pending, ["lg-1"])
            self.assertEqual(agent_run.pending, [])


# -- all three modules are importable without any framework --------------------

class LazyImportTest(unittest.TestCase):
    FRAMEWORKS = {"langgraph", "langchain_core", "crewai", "agent_framework"}
    FILES = ("rgi/integrations/langgraph.py", "rgi/integrations/crewai.py",
             "rgi/integrations/ms_agent.py")

    def test_adapters_do_not_import_their_framework_at_module_level(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for relative in self.FILES:
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

    def test_the_registry_loads_all_three_adapters(self):
        self.assertTrue(hasattr(load_adapter("langgraph"), "report_run"))
        self.assertTrue(hasattr(load_adapter("crewai"), "listen"))
        self.assertTrue(hasattr(load_adapter("ms-agent"), "report_run"))


if __name__ == "__main__":
    unittest.main()
