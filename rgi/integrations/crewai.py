"""CrewAI adapter: the event bus drives one lane per crew kickoff / flow run.

CrewAI announces everything on a process-wide event bus. A listener attached to
that bus sees every crew, flow, agent and task event in the process, so the
interesting problem is not observation but **correlation**: two crews (or the
same crew kicked off twice, or a flow and its nested crews) interleave on one
bus, and the lane rules only work when each event is attributed to the root
execution it belongs to.

Correlation follows what the bus itself records on every event:

* each event carries ``event_id``, and the bus pairs an ending event with the
  scope it closes through ``started_event_id`` (``CrewKickoffCompletedEvent``
  points back at its ``CrewKickoffStartedEvent``, ``FlowFinishedEvent`` at its
  ``FlowStartedEvent``), plus the enclosing ``parent_event_id``;
* handlers run on the bus's own worker threads, so events can be *dispatched*
  in a different order than they were emitted. The adapter therefore never
  guesses by arrival order: it keys runs by the kickoff/flow event id and
  routes every other event through the recorded ids.

The lane rules, following CrewAI's lifecycle:

* **Only the root execution owns the lamp.** ``crew_kickoff_started`` /
  ``flow_started`` start the lane; only ``crew_kickoff_completed`` /
  ``flow_finished`` (or failures) end it. Agent and task events update
  ``child()`` metadata and never change the root state.
* **Human feedback blocks.** ``human_feedback_requested``, ``flow_paused`` and
  ``flow_input_requested`` become ``blocked`` waits keyed by the event's
  ``request_id`` when it has one and by its event id (or the flow/method for a
  pause) otherwise. The matching ``*_received`` event, or the paused method
  restarting, resolves that wait back to ``working``.
* **Concurrent kickoffs get separate lanes.** Two kickoffs are two event ids,
  two sessions, two ``crewai:`` lanes; resolving a wait on one never touches
  the other.

``crewai`` is imported inside ``attach`` only, so this module is importable
without the package; everything is duck-typed and testable with fakes. Nothing
here raises into the application: every public entry point is guarded and
reports failure as ``False``.

Usage::

    from rgi.integrations.crewai import listen

    with listen(label="nightly research") as panel:
        crew.kickoff(inputs={"topic": "rust"})     # a lane appears per kickoff
        # crew_kickoff_started -> working, agent/task events -> children,
        # human_feedback_requested -> blocked, completion -> done

    # or attach one listener for the whole process:
    from rgi.integrations.crewai import CrewAIReporter

    panel = CrewAIReporter(label="all crews")
    panel.attach()
    try:
        crew.kickoff()
    finally:
        panel.close()          # detach and release the lanes
"""

from __future__ import annotations

import functools
import importlib
import os
import re
import sys
import threading
import uuid
from contextlib import contextmanager

from ..report import Reporter, scrub

NAMESPACE = "crewai"
"""Panel namespace; the lane key is ``crewai:<kickoff event id>``."""

_MAX_SCOPES = 1024
_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")

# event class names -> module names, per CrewAI's event layout. The released
# package has all of these (some are not re-exported from `crewai.events`, so
# they come from `crewai.events.types.*`).
_EVENT_NAMES = {
    "crew_events": (
        "CrewKickoffStartedEvent", "CrewKickoffCompletedEvent", "CrewKickoffFailedEvent",
        "CrewTrainStartedEvent", "CrewTrainCompletedEvent", "CrewTrainFailedEvent",
        "CrewTestStartedEvent", "CrewTestCompletedEvent", "CrewTestFailedEvent",
    ),
    "flow_events": (
        "FlowStartedEvent", "FlowFinishedEvent", "FlowFailedEvent", "FlowPausedEvent",
        "MethodExecutionStartedEvent", "MethodExecutionFinishedEvent",
        "MethodExecutionFailedEvent", "MethodExecutionPausedEvent",
        "HumanFeedbackRequestedEvent", "HumanFeedbackReceivedEvent",
        "FlowInputRequestedEvent", "FlowInputReceivedEvent",
    ),
    "agent_events": (
        "AgentExecutionStartedEvent", "AgentExecutionCompletedEvent",
        "AgentExecutionErrorEvent",
    ),
    "task_events": (
        "TaskStartedEvent", "TaskCompletedEvent", "TaskFailedEvent",
    ),
}

_ROOT_START = frozenset({
    "crew_kickoff_started", "crew_train_started", "crew_test_started",
    "flow_started",
})
_ROOT_DONE = frozenset({
    "crew_kickoff_completed", "crew_train_completed", "crew_test_completed",
    "flow_finished",
})
_ROOT_FAIL = frozenset({
    "crew_kickoff_failed", "crew_train_failed", "crew_test_failed",
    "flow_failed",
})
_CHILD_START = frozenset({
    "agent_execution_started", "task_started", "agent_reasoning_started",
    "lite_agent_execution_started",
})
_CHILD_DONE = frozenset({
    "agent_execution_completed", "task_completed", "agent_reasoning_completed",
    "lite_agent_execution_completed",
})
_CHILD_FAIL = frozenset({
    "agent_execution_error", "task_failed", "agent_reasoning_failed",
    "lite_agent_execution_error",
})
_PAUSED = frozenset({"flow_paused", "method_execution_paused"})
_FEEDBACK = frozenset({"human_feedback_requested"})
_FEEDBACK_DONE = frozenset({"human_feedback_received"})
_INPUT = frozenset({"flow_input_requested"})
_INPUT_DONE = frozenset({"flow_input_received"})


# -- helpers -------------------------------------------------------------------

def _guarded(method):
    """Swallow every failure: an indicator may never break an agent run."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - by contract nothing escapes
            self._note_once(f"{type(exc).__name__}: {exc}")
            return False

    return wrapper


def _attr(value, *names, default=None):
    """Read the first present attribute (or dict key); properties may raise."""
    if isinstance(value, dict):
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
        return default
    for name in names:
        try:
            found = getattr(value, name, None)
        except Exception:  # noqa: BLE001 - odd objects may explode on access
            found = None
        if found is not None:
            return found
    return default


def _snake(name: str) -> str:
    return _CAMEL.sub("_", name).lower()


def _event_type(event) -> str:
    value = _attr(event, "type")
    if isinstance(value, str) and value:
        return value
    name = type(event).__name__
    if name.endswith("Event"):
        name = name[:-5]
    return _snake(name)


def _event_at(event) -> float | None:
    """The event's own timestamp as epoch seconds, when it carries one."""
    value = _attr(event, "timestamp")
    if value is None:
        return None
    try:
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        return float(value)
    except Exception:  # noqa: BLE001 - odd clocks are the framework's problem
        return None


def _event_id(event) -> str:
    value = _attr(event, "event_id", "id")
    return scrub(str(value), 64) if value else ""


def _parent_id(event) -> str:
    value = _attr(event, "parent_event_id")
    return scrub(str(value), 64) if value else ""


def _started_id(event) -> str:
    value = _attr(event, "started_event_id")
    return scrub(str(value), 64) if value else ""


def _name_of(value) -> str:
    for attribute in ("task_name", "agent_role", "name", "id"):
        found = _attr(value, attribute)
        if found:
            return str(found)
    return ""


# -- the lane ------------------------------------------------------------------

class _Run:
    """One root execution's lane: pending waits, children, terminal state."""

    def __init__(self, listener: "CrewAIReporter", key: str, label: str,
                 reporter: Reporter | None):
        self._listener = listener
        self.key = key
        self.label = label
        self.reporter = reporter
        self.requests: dict[str, str] = {}
        self.by_method: dict[str, str] = {}
        self.finished = False
        self.errored = False
        self._lock = threading.RLock()

    @property
    def pending(self) -> list[str]:
        return sorted(self.requests)

    def remember_method(self, method: str, request: str) -> None:
        """Pair a method with its open wait so a resolution without ids finds it."""
        method = scrub(str(method or ""), 60)
        if method:
            self.by_method[method] = request

    def _note_once(self, message: str) -> None:
        self._listener._note_once(message)

    @_guarded
    def working(self, *, at: float | None = None) -> bool:
        return bool(self.reporter and self.reporter.working(at=at))

    @_guarded
    def done(self, *, at: float | None = None) -> bool:
        self.finished = True
        return bool(self.reporter and self.reporter.done(at=at))

    @_guarded
    def error(self, message: str = "", *, at: float | None = None) -> bool:
        self.errored = True
        self.finished = True
        return bool(self.reporter and self.reporter.error(message, at=at))

    @_guarded
    def block(self, request: str, message: str = "", action: str = "input",
              *, at: float | None = None) -> bool:
        rid = scrub(str(request), 64) or "attention"
        if rid in self.requests:
            return True
        self.requests[rid] = scrub(message, 120)
        return bool(self.reporter and self.reporter.blocked(
            request=rid, action=action, message=message, at=at))

    @_guarded
    def resolve(self, request: str, *, at: float | None = None) -> bool:
        rid = scrub(str(request), 64) or "attention"
        self.requests.pop(rid, None)
        for method, mapped in list(self.by_method.items()):
            if mapped == rid:
                self.by_method.pop(method, None)
        return bool(self.reporter and self.reporter.resolve(rid, at=at))

    @_guarded
    def resume(self, *, at: float | None = None) -> bool:
        with self._lock:
            for rid in list(self.requests):
                self.resolve(rid, at=at)
            return self.working(at=at)

    @_guarded
    def child(self, child_id: str, *, state: str = "working", label: str = "") -> bool:
        if not self.reporter:
            return False
        return bool(self.reporter.child(scrub(str(child_id), 64) or "task",
                                        state=state, label=label))

    @_guarded
    def child_done(self, child_id: str) -> bool:
        return bool(self.reporter
                    and self.reporter.child_done(scrub(str(child_id), 64)))


class CrewAIReporter:
    """A listener on the CrewAI event bus that mirrors every root run.

    One instance can observe many concurrent crews/flows. Each root execution
    gets its own lane keyed by its kickoff/flow event id; agent and task events
    become child metadata under the run their ids point at.

    Attributes/properties:
        runs:     a snapshot ``{run key: _Run}`` of the lanes seen so far.
        attached: whether event handlers are registered on the bus.
        session:  optional explicit lane id; when set, every ``flow_started``
                  uses it, so a flow that is paused and resumed as a new
                  execution lands on one stable lane.
    """

    def __init__(self, *, label: str | None = None, session: str | None = None,
                 reporter: Reporter | None = None, **reporter_kwargs):
        self.label = label or "crewai run"
        self.session = str(session).strip() if session else None
        self._reporter = reporter
        self._reporter_kwargs = reporter_kwargs
        self._runs: dict[str, _Run] = {}
        self._scopes: dict[str, str] = {}
        self._registry: list[tuple[object, object, object]] = []
        self._attached = False
        self._noted: set[str] = set()
        self._lock = threading.RLock()

    # -- plumbing ----------------------------------------------------------
    def _note_once(self, message: str) -> None:
        """Diagnostics to stderr, once per distinct message, debug-gated."""
        message = scrub(message, 160)
        if not message or message in self._noted:
            return
        self._noted.add(message)
        if os.environ.get("RGI_HOOK_DEBUG") or os.environ.get("RGI_DEBUG"):
            print(f"[rgi crewai] {message}", file=sys.stderr)

    @property
    def runs(self) -> dict[str, _Run]:
        with self._lock:
            return dict(self._runs)

    @property
    def attached(self) -> bool:
        return self._attached

    def run(self, key: str) -> _Run | None:
        with self._lock:
            return self._runs.get(scrub(str(key), 64))

    # -- correlation -------------------------------------------------------
    def _remember_scope(self, event_id: str, key: str) -> None:
        if not event_id:
            return
        self._scopes[event_id] = key
        while len(self._scopes) > _MAX_SCOPES:
            self._scopes.pop(next(iter(self._scopes)))

    def _run_for(self, event) -> _Run | None:
        """Route an event to its root run using the bus's own pairing ids."""
        for candidate in (_started_id(event), _event_id(event), _parent_id(event)):
            key = self._scopes.get(candidate)
            if key is not None and key in self._runs:
                return self._runs[key]
        if len(self._runs) == 1:
            return next(iter(self._runs.values()))
        if self.session is not None:
            return self._runs.get(self.session)
        return None

    def _run_with_method(self, method: str, request=None) -> _Run | None:
        """The run that owns a wait for a method: a resolution without ids."""
        rid = scrub(str(request), 64) if request else ""
        method = scrub(str(method or ""), 60)
        for run in self._runs.values():
            if rid and rid in run.requests:
                return run
            if method and run.by_method.get(method) in run.requests:
                return run
        return None

    def _create_run(self, key: str, label: str) -> _Run:
        if key in self._runs:
            return self._runs[key]
        reporter = self._reporter
        if reporter is None:
            reporter = Reporter(NAMESPACE, key, label=label or self.label,
                                **self._reporter_kwargs)
        run = _Run(self, key, label or self.label, reporter)
        self._runs[key] = run
        return run

    def _start_run(self, event, *, at: float | None = None) -> _Run:
        kind = _event_type(event)
        if self.session is not None and kind == "flow_started":
            # an explicit session pins a flow's starts and resumes to one lane
            key = self.session
        else:
            key = (_event_id(event)
                   or scrub(str(_attr(event, "source_fingerprint") or ""), 64)
                   or self.session or uuid.uuid4().hex)
        label = str(_attr(event, "crew_name", "flow_name", default=self.label)
                    or self.label)
        run = self._create_run(key, label)
        self._remember_scope(key, key)
        self._remember_scope(_event_id(event), key)
        if not run.finished:
            run.working(at=at)
        return run

    # -- folding one bus event ---------------------------------------------
    @_guarded
    def observe(self, event) -> bool:
        """Fold one CrewAI event into the right lane (unknown events ignored)."""
        if event is None:
            return False
        with self._lock:
            return self._observe(event)

    def _observe(self, event) -> bool:
        kind = _event_type(event)
        at = _event_at(event)
        started = _started_id(event)

        if kind in _ROOT_START:
            run = self._start_run(event, at=at)
            return True

        if kind in _ROOT_DONE | _ROOT_FAIL:
            run = self._run_for(event)
            if run is None and started:
                # dispatched before its start: keep the result, pair later
                run = self._create_run(started, str(
                    _attr(event, "crew_name", "flow_name", default=self.label)
                    or self.label))
            if run is None:
                return False
            self._remember_scope(started, run.key)
            self._remember_scope(_event_id(event), run.key)
            if run.finished and not run.errored:
                return True                    # a late duplicate, not a new result
            if kind in _ROOT_FAIL:
                message = _attr(event, "error") or _attr(event, "details")
                return run.error(str(message or "run failed"), at=at)
            return run.done(at=at)

        if kind in _PAUSED:
            run = self._run_for(event)
            if run is None:
                return False
            self._remember_scope(_event_id(event), run.key)
            flow = _attr(event, "flow_id", "flow_name", default="flow")
            method = _attr(event, "method_name", default="method")
            request = f"paused:{flow}:{method}"
            return run.block(request, str(_attr(event, "message", default="") or ""),
                             action="input", at=at)

        if kind in _FEEDBACK:
            run = self._run_for(event)
            if run is None:
                return False
            self._remember_scope(_event_id(event), run.key)
            method = _attr(event, "method_name", default="review")
            request = (_attr(event, "request_id") or _event_id(event)
                       or f"feedback:{method}")
            run.remember_method(str(method), str(request))
            return run.block(str(request), str(_attr(event, "message", default="")
                                               or "human feedback requested"),
                             action="feedback", at=at)

        if kind in _FEEDBACK_DONE:
            method = str(_attr(event, "method_name", default="review"))
            run = self._run_for(event)
            request = _attr(event, "request_id")
            if run is None:
                run = self._run_with_method(method, request)
            if run is None:
                return False
            rid = (request or run.by_method.get(method) or f"feedback:{method}")
            if str(rid) in run.requests:
                return run.resolve(str(rid), at=at)
            return run.resume(at=at)

        if kind in _INPUT:
            run = self._run_for(event)
            if run is None:
                return False
            self._remember_scope(_event_id(event), run.key)
            method = _attr(event, "method_name", default="input")
            request = (_attr(event, "request_id") or _event_id(event)
                       or f"input:{_attr(event, 'flow_name', default='flow')}:{method}")
            run.remember_method(str(method), str(request))
            return run.block(str(request), str(_attr(event, "message", default="")
                                               or "user input requested"),
                             action="input", at=at)

        if kind in _INPUT_DONE:
            method = str(_attr(event, "method_name", default="input"))
            run = self._run_for(event)
            request = _attr(event, "request_id")
            if run is None:
                run = self._run_with_method(method, request)
            if run is None:
                return False
            rid = (request or run.by_method.get(method)
                   or f"input:{_attr(event, 'flow_name', default='flow')}:{method}")
            if str(rid) in run.requests:
                return run.resolve(str(rid), at=at)
            return run.resume(at=at)

        if kind == "method_execution_started":
            run = self._run_for(event)
            if run is None:
                return False
            self._remember_scope(_event_id(event), run.key)
            if run.requests:
                return run.resume(at=at)      # the paused method is running again
            if not run.finished:
                return run.working(at=at)
            return True

        if kind in ("method_execution_finished",):
            run = self._run_for(event)
            if run is not None:
                self._remember_scope(_event_id(event), run.key)
            return run is not None

        if kind == "method_execution_failed":
            run = self._run_for(event)
            if run is None:
                return False
            self._remember_scope(_event_id(event), run.key)
            return run.error(str(_attr(event, "error") or "flow method failed"), at=at)

        if kind in _CHILD_START | _CHILD_DONE | _CHILD_FAIL:
            run = self._run_for(event)
            if run is None:
                return False
            child_id = (_attr(event, "task_id", "agent_id", "event_id")
                        or "child")
            label = _name_of(event)
            if kind in _CHILD_DONE:
                return run.child_done(str(child_id))
            state = "error" if kind in _CHILD_FAIL else "working"
            return run.child(str(child_id), state=state, label=label)

        return False

    # -- bus attachment -----------------------------------------------------
    def attach(self) -> bool:
        """Register on the CrewAI event bus; ``False`` when it is not installed."""
        with self._lock:
            if self._attached:
                return True
            bus, classes = _load_bus()
            if bus is None or not classes:
                self._note_once("crewai is not installed; the listener is inert")
                return False
            handler = self._on_bus_event
            for cls in classes:
                try:
                    bus.register_handler(cls, handler)
                except Exception as exc:  # noqa: BLE001 - one class must not stop attach
                    self._note_once(f"register {getattr(cls, '__name__', cls)}: {exc}")
                    continue
                self._registry.append((bus, cls, handler))
            self._attached = bool(self._registry)
            return self._attached

    def detach(self) -> bool:
        """Remove the bus handlers; lanes keep whatever state they have."""
        with self._lock:
            for bus, cls, handler in self._registry:
                try:
                    bus.off(cls, handler)
                except Exception:  # noqa: BLE001 - detaching must not raise
                    pass
            self._registry.clear()
            self._attached = False
            return True

    def _on_bus_event(self, source, event) -> None:   # noqa: ARG002 - bus signature
        """The bus calls handlers as ``(source, event)`` on its own threads."""
        try:
            self.observe(event)
        except Exception as exc:  # noqa: BLE001 - the bus must never see a raise
            self._note_once(f"bus handler: {type(exc).__name__}: {exc}")

    def close(self) -> bool:
        """Detach and release every lane; safe to call twice."""
        with self._lock:
            self.detach()
            runs = list(self._runs.values())
            self._runs.clear()
            self._scopes.clear()
            self._closed = True
        for run in runs:
            try:
                if run.reporter is not None:
                    run.reporter.close()
            except Exception:  # noqa: BLE001
                pass
        return True

    # -- lifecycle ----------------------------------------------------------
    def __enter__(self) -> "CrewAIReporter":
        self.attach()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.detach()
        return False


def _load_bus():
    """The released bus and event classes, imported lazily (``(None, [])`` absent)."""
    bus = None
    for module in ("crewai.events.event_bus", "crewai.utilities.events.event_bus",
                   "crewai.utilities.events"):
        try:
            imported = importlib.import_module(module)
            bus = getattr(imported, "crewai_event_bus", None)
            if bus is not None:
                break
        except Exception:  # noqa: BLE001 - try the next layout
            bus = None
    if bus is None:
        return None, []
    classes = []
    for module_name, names in _EVENT_NAMES.items():
        event_module = None
        for path in (f"crewai.events.types.{module_name}",
                     f"crewai.utilities.events.types.{module_name}"):
            try:
                event_module = importlib.import_module(path)
                break
            except Exception:  # noqa: BLE001 - try the next layout
                event_module = None
        if event_module is None:
            continue
        for name in names:
            cls = getattr(event_module, name, None)
            if cls is not None:
                classes.append(cls)
    return bus, classes


@contextmanager
def listen(*, label: str | None = None, session: str | None = None,
           reporter: Reporter | None = None, **reporter_kwargs):
    """Listen to the CrewAI bus for the duration of the block.

    ``label`` is the fallback lane label (a crew's own name is preferred when
    the event carries one). ``session`` is an optional explicit lane id used
    for events the bus cannot correlate. Remaining keyword arguments go to
    ``rgi.report.Reporter``. The listener detaches on exit; use
    ``CrewAIReporter.close()`` to also release the lanes.
    """
    listener = CrewAIReporter(label=label, session=session, reporter=reporter,
                              **reporter_kwargs)
    listener.attach()
    try:
        yield listener
    finally:
        listener.detach()
