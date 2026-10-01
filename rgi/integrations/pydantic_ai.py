"""Pydantic AI adapter: one lane per conversation, approvals block, external work does not.

Pydantic AI runs report their progress through the typed event stream
(``event_stream_handler`` / ``agent.run_stream_events``) and end with an
``AgentRunResult`` whose ``output`` may be a ``DeferredToolRequests`` object
when the run paused. The adapter folds both surfaces into one panel lane:

* The **whole run** owns the lamp. ``agent.run(...)`` is the top-level
  boundary; tool calls and capability hooks inside it are metadata only.
* **Approvals block.** A deferred tool call in ``DeferredToolRequests.approvals``
  is a human decision: the lane reports ``blocked`` with the ``tool_call_id``
  of every pending approval, returns to ``working`` as each is resolved (by a
  ``DeferredToolResultsEvent`` or a follow-up run), and is only reported
  ``done`` when the run finishes without them.
* **External work stays working.** A deferred call in
  ``DeferredToolRequests.calls`` (``CallDeferred``) is a tool being executed
  outside this run - a background worker or an app frontend - so the lane
  reports ``working`` and publishes the call ids as metadata. It is never
  ``blocked``: nobody is being asked to do anything.
* **A resume is the same conversation.** Correlation is by the Pydantic AI
  ``conversation_id`` you pass to ``agent.run(...)`` and to this adapter. A
  follow-up run that answers deferred calls lands on the same lane; message
  history alone does not identify a lane, so pick a stable id and persist it
  next to the history (``result.conversation_id`` when Pydantic AI generated
  one for you).

``pydantic_ai`` is imported nowhere in this file: events and results are
duck-typed, so the adapter works with the released package, with
``pydantic-ai-slim``, and in tests with fakes. Nothing here raises into the
agent; every entry point is guarded and reports failure as ``False``.

Usage::

    from pydantic_ai import Agent, DeferredToolRequests
    from rgi.integrations.pydantic_ai import report_run

    agent = Agent("openai:gpt-4o", output_type=[str, DeferredToolRequests])
    conversation_id = "order-1234"           # stable across resumes

    async with report_run(conversation_id=conversation_id, label="orders") as run:
        result = await agent.run(prompt, conversation_id=conversation_id,
                                 **run.run_kwargs)
        run.observe(result)
        if isinstance(result.output, DeferredToolRequests):
            deferred = result.output
            deferred.build_results(approve_all=True)   # gather human input ...
            result = await agent.run(                 # resume the same conversation
                message_history=result.all_messages(),
                deferred_tool_results=deferred.build_results(approve_all=True),
                conversation_id=conversation_id,
                **run.run_kwargs,
            )
            run.observe(result)
"""

from __future__ import annotations

import functools
import os
import sys
import threading
import uuid

from ..report import Reporter, scrub

NAMESPACE = "pydantic-ai"
"""Panel namespace; the lane key is ``pydantic-ai:<conversation id>``."""

_ENV_NAMES = ("RGI_CONVERSATION_ID", "RGI_SESSION")
_MAX_DEFERRED = 32

# Same-process resume: approvals outlive the `report_run` block that surfaced
# them, so the next block for the conversation re-blocks them before the
# follow-up run settles. A fresh process resumes by passing `pending=[...]`.
_PENDING_LOCK = threading.Lock()
_PENDING: dict[str, dict[str, str]] = {}


def _env_session() -> str | None:
    for name in _ENV_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def _remember(session: str, entries: dict[str, str]) -> None:
    with _PENDING_LOCK:
        stored = _PENDING.setdefault(session, {})
        stored.update(entries)
        while len(stored) > _MAX_DEFERRED:
            stored.pop(next(iter(stored)))


def _forget(session: str, requests: list[str]) -> None:
    with _PENDING_LOCK:
        stored = _PENDING.get(session)
        if stored is None:
            return
        for request in requests:
            stored.pop(request, None)
        if not stored:
            _PENDING.pop(session, None)


def _recall(session: str) -> dict[str, str]:
    with _PENDING_LOCK:
        return dict(_PENDING.get(session) or {})


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
    for name in names:
        try:
            found = getattr(value, name, None)
        except Exception:  # noqa: BLE001 - properties may raise on odd objects
            found = None
        if found is not None:
            return found
    return default


def _kind(event) -> str:
    return str(_attr(event, "event_kind", default="") or type(event).__name__)


def _looks_like_requests(value) -> bool:
    return _attr(value, "approvals") is not None and _attr(value, "calls") is not None


def _call_id(call, index: int, prefix: str) -> tuple[str, str]:
    """(stable request id, display name) for one deferred ``ToolCallPart``."""
    request = _attr(call, "tool_call_id", "id", default="")
    request = str(request or f"{prefix}-{index}")
    name = str(_attr(call, "tool_name", "name", default="") or prefix)
    return request, name


class RunReport:
    """One conversation's lane, usable as ``with`` / ``async with``.

    Properties:
        session:             the stable conversation id the lane is keyed by.
        pending:             approval request ids still open (these block).
        external:            deferred external call ids (these do not block).
        run_kwargs:          ``{"event_stream_handler": ...}`` to spread into
                             ``agent.run(...)`` / ``agent.run_stream(...)``.
        event_stream_handler: the async handler that feeds this lane.
        reporter:            the underlying ``rgi.report.Reporter`` (or ``None``).
    """

    def __init__(self, conversation_id: str | None = None, *, label: str | None = None,
                 reporter: Reporter | None = None, pending: list[str] | None = None,
                 **reporter_kwargs):
        session = str(conversation_id).strip() if conversation_id is not None else None
        self.session = session or _env_session() or uuid.uuid4().hex
        self.label = label or "pydantic-ai run"
        self._reporter = reporter
        self._reporter_kwargs = reporter_kwargs
        self._entered = False
        self._finished = False
        self._noted: set[str] = set()
        self._handler = None
        self._pending: dict[str, str] = {}
        self._external: dict[str, str] = {}
        for request in pending or []:
            request = scrub(str(request), 64)
            if request:
                self._pending[request] = "awaiting approval"

    # -- plumbing ----------------------------------------------------------
    def _note_once(self, message: str) -> None:
        message = scrub(message, 160)
        if not message or message in self._noted:
            return
        self._noted.add(message)
        if os.environ.get("RGI_HOOK_DEBUG") or os.environ.get("RGI_DEBUG"):
            print(f"[rgi pydantic-ai] {message}", file=sys.stderr)

    @property
    def reporter(self) -> Reporter | None:
        return self._reporter

    @property
    def pending(self) -> list[str]:
        return sorted(self._pending)

    @property
    def external(self) -> list[str]:
        return sorted(self._external)

    @property
    def run_kwargs(self) -> dict:
        return {"event_stream_handler": self.event_stream_handler}

    @property
    def event_stream_handler(self):
        if self._handler is None:
            report = self

            async def handler(context, events):  # noqa: ARG001 - framework signature
                try:
                    async for event in events:
                        report.handle_event(event)
                except Exception as exc:  # noqa: BLE001 - never fail the run
                    report._note_once(f"event stream: {type(exc).__name__}: {exc}")

            self._handler = handler
        return self._handler

    # -- the public state changes -----------------------------------------
    @_guarded
    def working(self) -> bool:
        return bool(self._reporter and self._reporter.working())

    @_guarded
    def done(self) -> bool:
        self._finished = True
        requests = list(self._pending)
        self._pending.clear()
        self._external.clear()
        _forget(self.session, requests)
        return bool(self._reporter and self._reporter.done())

    @_guarded
    def error(self, message: str = "") -> bool:
        self._finished = True
        return bool(self._reporter and self._reporter.error(message))

    @_guarded
    def info(self, fields: dict) -> bool:
        return bool(self._reporter and self._reporter.info(fields))

    @_guarded
    def heartbeat(self) -> bool:
        return bool(self._reporter and self._reporter.heartbeat())

    def resolve(self, request: str) -> bool:
        """Resolve one approval wait; the lane returns to ``working``."""
        rid = scrub(str(request), 64)
        try:
            self._pending.pop(rid, None)
            self._external.pop(rid, None)
            _forget(self.session, [rid])
            if self._reporter:
                return self._reporter.resolve(rid)
        except Exception as exc:  # noqa: BLE001 - nothing escapes
            self._note_once(f"resolve: {type(exc).__name__}: {exc}")
        return False

    def _block(self, request: str, message: str = "") -> bool:
        rid = scrub(str(request), 64) or "approval"
        message = scrub(message, 120)
        self._pending[rid] = message
        _remember(self.session, {rid: message})
        if self._reporter:
            return self._reporter.blocked(request=rid, action="approval", message=message)
        return False

    # -- deferred tool calls ----------------------------------------------
    @_guarded
    def _deferred(self, requests) -> bool:
        """Approvals block per id; external calls are reported as working."""
        approvals = list(_attr(requests, "approvals", default=[]) or [])[:_MAX_DEFERRED]
        calls = list(_attr(requests, "calls", default=[]) or [])[:_MAX_DEFERRED]
        approval_ids, external_ids = [], []
        for index, call in enumerate(approvals):
            request, name = _call_id(call, index, "approval")
            self._block(request, name)
            approval_ids.append(request)
        for index, call in enumerate(calls):
            request, name = _call_id(call, index, "external")
            self._external[request] = name
            external_ids.append(request)
        fields = {"deferred": {"approvals": approval_ids,
                               "external": external_ids,
                               "external_tools": [self._external[i] for i in external_ids]}}
        if approvals:
            return bool(self._reporter and self._reporter.info(fields))
        if self._reporter:
            self._reporter.working()
            return self._reporter.info(fields)
        return False

    @_guarded
    def _results(self, results) -> bool:
        """An inline resolution: every named approval/external call is over."""
        approvals = _attr(results, "approvals")
        calls = _attr(results, "calls")
        if approvals is None and calls is None and isinstance(results, dict):
            approvals = results          # a bare mapping of request id -> decision
        resolved = list(approvals or {})[:_MAX_DEFERRED]
        resolved += list(calls or {})[:_MAX_DEFERRED]
        for request in resolved:
            self.resolve(str(request))
        return True

    @_guarded
    def handle_event(self, event) -> bool:
        """Fold one ``AgentStreamEvent`` into the lane (unknown events ignored)."""
        kind = _kind(event)
        if kind in ("deferred_tool_requests", "DeferredToolRequestsEvent"):
            self._deferred(_attr(event, "requests", default=event))
            return True
        if kind in ("deferred_tool_results", "DeferredToolResultsEvent"):
            self._results(_attr(event, "results", default=event))
            return True
        if kind in ("function_tool_call", "FunctionToolCallEvent"):
            self._tool(event, "running")
            return True
        if kind in ("function_tool_result", "FunctionToolResultEvent"):
            self._tool(event, "done")
            return True
        if _looks_like_requests(event):                   # a bare requests object
            self._deferred(event)
            return True
        return False

    @_guarded
    def _tool(self, event, state: str) -> bool:
        part = _attr(event, "part", default=event)
        name = str(_attr(part, "tool_name", "name", default="") or "tool")
        if not self._reporter:
            return False
        return self._reporter.info({"tool": name, "tool_state": state})

    @_guarded
    def observe(self, result) -> bool:
        """Fold a settled ``AgentRunResult`` (or its output) into the lane.

        A deferred ``output`` keeps approvals blocked and external calls
        working; anything else means the run completed: open waits are
        resolved and the lane is reported ``done``.
        """
        if result is None:
            return False
        output = _attr(result, "output", default=result)
        if _looks_like_requests(output):
            approvals = _attr(output, "approvals", default=[])
            calls = _attr(output, "calls", default=[])
            if approvals or calls:
                self._deferred(output)
                return True
        for request in list(self._pending):
            self.resolve(request)
        self._external.clear()
        if self._finished:
            return True
        self.done()
        return True

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "RunReport":
        if self._entered:
            return self
        self._entered = True
        try:
            if self._reporter is None:
                self._reporter = Reporter(NAMESPACE, self.session, label=self.label,
                                          **self._reporter_kwargs)
            self._reporter.start()
            self._reporter.working()
            remembered = _recall(self.session)
            for request in sorted(set(remembered) | set(self._pending)):
                message = remembered.get(request) or self._pending.get(request) or ""
                self._block(request, message)
        except Exception as exc:  # noqa: BLE001 - a broken reporter is not fatal
            self._note_once(f"enter: {type(exc).__name__}: {exc}")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self.error(f"{exc_type.__name__}: {exc}")
        elif self._finished:
            pass                              # observe() already settled this run
        elif self._pending:
            _remember(self.session, dict(self._pending))   # still waiting
        else:
            self.done()
        return False                          # never swallow the run's own error

    async def __aenter__(self) -> "RunReport":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return self.__exit__(exc_type, exc, tb)


def report_run(conversation_id: str | None = None, **kwargs) -> RunReport:
    """Wrap one Pydantic AI run (and its resumes) in a panel lane.

    ``conversation_id`` is the stable identity: pass the same value to
    ``agent.run(..., conversation_id=...)`` and to every resumed run so all
    of them land on the same lane. When omitted, ``RGI_CONVERSATION_ID`` (or
    ``RGI_SESSION``) is used, then a fresh id is generated for this run - a
    generated id will not follow a resume.

    Extra keyword arguments go to ``rgi.report.Reporter`` (``label``,
    ``url``, ``token``, ``ident``, ``timeout``, ...). The return value is a
    ``RunReport`` context manager exposing ``.run_kwargs``,
    ``.event_stream_handler``, ``.observe(result)``, ``.resolve(id)``,
    ``.pending`` and ``.external``.
    """
    try:
        return RunReport(conversation_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a broken wrapper must not break a run
        fallback = RunReport.__new__(RunReport)
        fallback.session = str(conversation_id or "unavailable")
        fallback.label = "pydantic-ai run"
        fallback._reporter = None
        fallback._reporter_kwargs = {}
        fallback._entered = True
        fallback._finished = False
        fallback._noted = set()
        fallback._handler = None
        fallback._pending = {}
        fallback._external = {}
        fallback._note_once(f"report_run: {type(exc).__name__}: {exc}")
        return fallback
