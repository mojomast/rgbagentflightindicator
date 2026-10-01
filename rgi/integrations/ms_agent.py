"""Microsoft Agent Framework adapter: middleware for agent runs, events for workflows.

The Agent Framework draws a clear line the panel must respect: an **executor**
finishing is not the workflow finishing. A workflow runs to ``IDLE`` (or fails)
as a whole, and only that completes the lane; executor lifecycles are
``child()`` metadata. Agent runs have their own boundary, and the framework's
middleware pipeline is the exact root hook for them, so the adapter offers a
middleware object to pass to ``agent.run(...)``.

The rules:

* **Middleware around the root run owns the lamp.** ``agent.run(...)`` with
  ``middleware=[run.middleware]`` reports ``working`` before the run and
  ``done`` after it returns without a wait. An exception reports ``error`` and
  is re-raised - the adapter never swallows the run's own failure.
* **A genuine human wait blocks, by request id.** Workflow ``request_info``
  events (``ctx.request_info(...)``, including approval executors) and
  ``function_approval_request`` contents in an agent response make the lane
  ``blocked`` under the event's ``request_id`` / the content's ``id``. While a
  request is open a final ``IDLE`` does not complete the lane; once it is
  answered (``run.answer(request_id)``) the lane returns to ``working``.
* **Executors are children.** ``executor_invoked`` / ``executor_completed`` /
  ``executor_failed`` update ``child()`` detail and never change the root
  state. Workflow completion is the terminal ``status`` (``IDLE``), a
  ``failed`` event, or the final state of a ``WorkflowRunResult`` - and
  ``IDLE_WITH_PENDING_REQUESTS`` is explicitly not completion.
* **A resume is the same session.** The session id is yours: pass the same
  value when continuing after a request and the lane is reused.

``agent_framework`` is imported inside ``middleware`` only (to subclass
``AgentMiddleware`` when it is installed); everything is duck-typed and
testable with fakes. Nothing here raises into the application: every public
entry point is guarded and reports failure as ``False``.

Usage::

    from rgi.integrations.ms_agent import report_run

    with report_run("ticket-42", label="triage") as run:
        response = await agent.run(prompt, middleware=[run.middleware])
        run.observe(response)
        for request in response.user_input_requests:      # approvals
            run.answer(request.id)                        # the human said yes ...

    # workflows stream their own events; children stay children
    with report_run("nightly", label="nightly workflow") as run:
        async for event in workflow.run(message, stream=True):
            run.observe(event)                            # request_info -> blocked
        run.finish()
"""

from __future__ import annotations

import functools
import hashlib
import os
import sys
import threading
import uuid

from ..report import Reporter, scrub

NAMESPACE = "ms-agent"
"""Panel namespace; the lane key is ``ms-agent:<session id>``."""

_ENV_NAMES = ("RGI_SESSION", "RGI_WORKFLOW_ID", "RGI_CONVERSATION_ID")
_MAX_PENDING = 32
_MISSING = object()

# A wait outlives the ``report_run`` block that surfaced it (an approval is
# answered on a later run), so the next block re-blocks it before the resumed
# run settles. A fresh process resumes by passing ``pending=[...]``.
_PENDING_LOCK = threading.Lock()
_PENDING: dict[str, dict[str, str]] = {}


# -- pending registry ---------------------------------------------------------

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
        while len(stored) > _MAX_PENDING:
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


def _state_name(state) -> str:
    """``WorkflowRunState.IDLE`` / ``"idle"`` / anything -> upper-case text."""
    value = _attr(state, "value", default=state)
    if value is None:
        return ""
    name = str(getattr(value, "name", value))
    return name.split(".")[-1].strip().upper()


def _looks_like_stream(result) -> bool:
    if result is None:
        return False
    if callable(_attr(result, "get_final_response")):
        return True
    try:
        return hasattr(result, "__aiter__")
    except Exception:  # noqa: BLE001
        return False


def _looks_like_response(result) -> bool:
    """A settled ``AgentResponse``: it carries messages and input requests."""
    return (_attr(result, "user_input_requests", default=_MISSING) is not _MISSING
            and _attr(result, "messages", default=_MISSING) is not _MISSING)


def _looks_like_update(result) -> bool:
    """A streamed ``AgentResponseUpdate``: input requests but no messages."""
    return (_attr(result, "user_input_requests", default=_MISSING) is not _MISSING
            and _attr(result, "messages", default=_MISSING) is _MISSING)


def _looks_like_result(result) -> bool:
    return callable(_attr(result, "get_final_state")) \
        or callable(_attr(result, "get_request_info_events"))


def _request_id(event, index: int = 0) -> str:
    rid = _attr(event, "request_id", "_request_id")
    if rid:
        return scrub(str(rid), 64)
    source = _attr(event, "source_executor_id", default="")
    data = _attr(event, "data")
    digest = hashlib.sha1(f"{source}|{data!r}".encode("utf-8")).hexdigest()
    return f"request-{digest[:16]}" if source or data is not None else f"request-{index}"


def _approval_requests(result) -> list[tuple[str, str]]:
    """``(request id, label)`` for every human wait in an agent response."""
    found: list[tuple[str, str]] = []
    contents = _attr(result, "user_input_requests", default=None)
    if contents is None:
        return found
    for index, content in enumerate(list(contents)[:_MAX_PENDING]):
        ctype = _attr(content, "type")
        if ctype is None:
            continue
        call = _attr(content, "function_call")
        rid = (_attr(content, "id", "call_id")
               or _attr(call, "call_id")
               or f"approval-{index}")
        label = str(_attr(call, "name", "tool_name")
                    or (ctype if ctype != "function_approval_request" else "approval"))
        found.append((scrub(str(rid), 64), label))
    return found


# -- the lane ------------------------------------------------------------------

class RunReport:
    """One Microsoft Agent Framework session's lane, ``with`` / ``async with``.

    Attributes/properties:
        session:    the stable session id the lane is keyed by.
        pending:    request ids still open (these block the lane).
        middleware: the agent middleware object to pass to ``agent.run``.
        run_kwargs: ``{"middleware": [middleware]}`` to spread into
                    ``agent.run(...)``.
        reporter:   the underlying ``rgi.report.Reporter`` (or ``None``).
    """

    def __init__(self, session: str | None = None, *, label: str | None = None,
                 reporter: Reporter | None = None, pending: list[str] | None = None,
                 **reporter_kwargs):
        name = str(session).strip() if session is not None else None
        self.session = name or _env_session() or uuid.uuid4().hex
        self.label = label or "agent-framework run"
        self._reporter = reporter
        self._reporter_kwargs = reporter_kwargs
        self._entered = False
        self._finished = False
        self._noted: set[str] = set()
        self._pending: dict[str, str] = {}
        self._middleware = None
        self._lock = threading.RLock()
        for request in pending or []:
            request = scrub(str(request), 64)
            if request:
                self._pending[request] = "awaiting input"

    # -- plumbing ----------------------------------------------------------
    def _note_once(self, message: str) -> None:
        """Diagnostics to stderr, once per distinct message, debug-gated."""
        message = scrub(message, 160)
        if not message or message in self._noted:
            return
        self._noted.add(message)
        if os.environ.get("RGI_HOOK_DEBUG") or os.environ.get("RGI_DEBUG"):
            print(f"[rgi ms-agent] {message}", file=sys.stderr)

    @property
    def reporter(self) -> Reporter | None:
        return self._reporter

    @property
    def pending(self) -> list[str]:
        return sorted(self._pending)

    @property
    def run_kwargs(self) -> dict:
        return {"middleware": [self.middleware]}

    # -- the public state changes -----------------------------------------
    @_guarded
    def working(self) -> bool:
        return bool(self._reporter and self._reporter.working())

    @_guarded
    def finish(self) -> bool:
        """The run/settled result is over: done unless a request is open."""
        with self._lock:
            if self._pending:
                return True          # a real wait outlives the run
            if self._finished:
                return True
            self._finished = True
            return bool(self._reporter and self._reporter.done())

    @_guarded
    def error(self, message: str = "") -> bool:
        self._finished = True
        return bool(self._reporter and self._reporter.error(message))

    @_guarded
    def answer(self, request: str | None = None) -> bool:
        """Answer open requests (all of them, or one); the lane returns to working."""
        with self._lock:
            requests = ([scrub(str(request), 64)] if request is not None
                        else list(self._pending))
            for rid in requests:
                self._resolve_locked(rid)
            return self.working()

    def resume(self, request: str | None = None) -> bool:
        """Alias for :meth:`answer` for a continuation run."""
        return self.answer(request)

    @_guarded
    def resolve(self, request: str) -> bool:
        with self._lock:
            return self._resolve_locked(scrub(str(request), 64))

    def _resolve_locked(self, request: str) -> bool:
        self._pending.pop(request, None)
        _forget(self.session, [request])
        if self._reporter is not None:
            return bool(self._reporter.resolve(request))
        return False

    @_guarded
    def child(self, child_id: str, *, state: str = "working", label: str = "",
              tokens: int | None = None) -> bool:
        if not self._reporter:
            return False
        return bool(self._reporter.child(scrub(str(child_id), 64) or "executor",
                                         state=state, label=label, tokens=tokens))

    @_guarded
    def child_done(self, child_id: str) -> bool:
        return bool(self._reporter
                    and self._reporter.child_done(scrub(str(child_id), 64)))

    @_guarded
    def info(self, fields: dict) -> bool:
        return bool(self._reporter and self._reporter.info(fields))

    @_guarded
    def heartbeat(self) -> bool:
        return bool(self._reporter and self._reporter.heartbeat())

    # -- attention ----------------------------------------------------------
    @_guarded
    def _block(self, request: str, message: str = "", action: str = "input") -> bool:
        rid = scrub(str(request), 64) or "request"
        if rid in self._pending:
            return True
        self._pending[rid] = scrub(message, 120)
        _remember(self.session, {rid: scrub(message, 120)})
        if self._reporter is not None:
            self._reporter.blocked(request=rid, action=action, message=message)
        return True

    # -- folding framework objects -----------------------------------------
    @_guarded
    def observe(self, event) -> bool:
        """Fold one workflow event, response, or approval content into the lane."""
        if event is None:
            return False
        with self._lock:
            if isinstance(event, (list, tuple)):
                if _looks_like_result(event):
                    return self._observe_result_locked(event)
                handled = False
                for item in list(event):
                    if self._observe(item):
                        handled = True
                return handled
            return self._observe(event)

    def _observe(self, event) -> bool:
        kind = _attr(event, "type")
        if isinstance(kind, str):
            if self._is_workflow_kind(kind):
                return self._on_workflow_event(event)
            if kind == "function_approval_request" or _attr(event, "user_input_request") is True:
                return self._on_approval_content(event)
        if _looks_like_result(event):
            return self._observe_result_locked(event)
        if _looks_like_response(event):
            return self._settle(event)
        if _looks_like_update(event):
            return self._observe_update(event)
        return False

    @staticmethod
    def _is_workflow_kind(kind: str) -> bool:
        return kind in (
            "started", "status", "failed", "output", "intermediate", "data",
            "request_info", "warning", "error", "superstep_started",
            "superstep_completed", "executor_invoked", "executor_completed",
            "executor_failed", "executor_bypassed",
        )

    def _on_workflow_event(self, event) -> bool:
        kind = str(_attr(event, "type"))
        if kind in ("started", "output", "intermediate", "data"):
            return self._activity()
        if kind == "status":
            return self._on_status(_state_name(_attr(event, "state")))
        if kind == "failed":
            details = _attr(event, "details")
            message = _attr(details, "message") or str(details or "workflow failed")
            return self.error(message)
        if kind == "request_info":
            rid = _request_id(event)
            return self._block(rid, repr(_attr(event, "data")), action="input")
        if kind in ("executor_invoked", "executor_bypassed"):
            return self.child(str(_attr(event, "executor_id") or "executor"),
                              state="working")
        if kind == "executor_completed":
            return self.child_done(str(_attr(event, "executor_id") or "executor"))
        if kind == "executor_failed":
            details = _attr(event, "details")
            return self.child(str(_attr(event, "executor_id") or "executor"),
                              state="error", label=str(_attr(details, "message") or ""))
        if kind in ("warning", "error"):
            # non-fatal diagnostics from user code: detail, never a state change
            return bool(self._reporter and self._reporter.info(
                {"note": str(_attr(event, "data") or kind)}))
        return False

    def _on_status(self, state: str) -> bool:
        if state in ("STARTED", "IN_PROGRESS"):
            return self._activity()
        if state == "IN_PROGRESS_PENDING_REQUESTS":
            return True                        # a request_info already blocked it
        if state == "IDLE":
            return self.finish()
        if state == "IDLE_WITH_PENDING_REQUESTS":
            return True                        # paused, not complete
        if state == "FAILED":
            return self.error("workflow failed")
        if state == "CANCELLED":
            return self.error("workflow cancelled")
        return False

    def _on_approval_content(self, content) -> bool:
        rid = (_attr(content, "id", "call_id")
               or _attr(_attr(content, "function_call"), "call_id")
               or "approval")
        label = str(_attr(_attr(content, "function_call"), "name", "tool_name")
                    or "approval")
        return self._block(str(rid), label, action="approval")

    @_guarded
    def _settle(self, result) -> bool:
        """A settled agent response: approvals block, otherwise the run is done."""
        requests = _approval_requests(result)
        for rid, label in requests:
            self._block(rid, label, action="approval")
        if requests:
            return True
        if _looks_like_stream(result):
            return True                        # consumed later; finish() then
        return self.finish()

    @_guarded
    def _observe_update(self, update) -> bool:
        """One streamed ``AgentResponseUpdate``: a wait blocks, otherwise busy."""
        requests = _approval_requests(update)
        for rid, label in requests:
            self._block(rid, label, action="approval")
        if requests:
            return True
        return self.working()

    def _observe_result_locked(self, result) -> bool:
        try:
            events = list(result)
        except TypeError:
            events = [result]
        for event in events:
            self._observe(event)
        getter = _attr(result, "get_request_info_events")
        if callable(getter):
            try:
                for request in list(getter() or [])[:_MAX_PENDING]:
                    self._observe(request)
            except Exception as exc:  # noqa: BLE001
                self._note_once(f"result requests: {type(exc).__name__}: {exc}")
        state_getter = _attr(result, "get_final_state")
        if callable(state_getter):
            try:
                state = _state_name(state_getter())
            except Exception:  # noqa: BLE001 - no status events emitted
                state = ""
            if state:
                return bool(self._on_status(state))
        return self.finish()

    @_guarded
    def observe_result(self, result) -> bool:
        """Fold a settled ``WorkflowRunResult`` (events + final state) in."""
        if result is None:
            return False
        with self._lock:
            return bool(self._observe_result_locked(result))

    def _activity(self) -> bool:
        return self.working()

    # -- lifecycle ----------------------------------------------------------
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
                self._pending.setdefault(request, message)
                self._reporter.blocked(request=request, action="input",
                                       message=message)
        except Exception as exc:  # noqa: BLE001 - a broken reporter is not fatal
            self._note_once(f"enter: {type(exc).__name__}: {exc}")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is not None:
                self.error(f"{exc_type.__name__}: {exc}")
            elif self._pending:
                _remember(self.session, dict(self._pending))   # still waiting
            else:
                self.finish()
        except Exception as exc:  # noqa: BLE001 - never mask the run's error
            self._note_once(f"exit: {type(exc).__name__}: {exc}")
        return False                          # never swallow the run's own error

    async def __aenter__(self) -> "RunReport":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return self.__exit__(exc_type, exc, tb)

    # -- agent middleware ---------------------------------------------------
    @property
    def middleware(self):
        if self._middleware is None:
            self._middleware = self._make_middleware()
        return self._middleware

    def _make_middleware(self):
        """An ``AgentMiddleware`` subclass when the framework is importable."""
        try:
            from agent_framework import AgentMiddleware  # noqa: PLC0415
            base = AgentMiddleware
        except Exception:  # noqa: BLE001 - framework absent: still drivable
            base = object
        report = self

        class _PanelMiddleware(base):  # type: ignore[misc, valid-type]
            """Wraps the root agent run: the only boundary that completes it."""

            async def process(self, context, call_next):  # noqa: D102
                report.working()
                try:
                    await call_next()
                except BaseException as exc:
                    report.error(f"{type(exc).__name__}: {exc}")
                    raise
                result = _attr(context, "result")
                if _looks_like_stream(result):
                    return                    # consumed after the middleware returns
                report._settle(result)

        return _PanelMiddleware()


def report_run(session: str | None = None, **kwargs) -> RunReport:
    """Wrap one Agent Framework session (and its resumptions) in a panel lane.

    ``session`` is the stable identity: pass the same value for every run that
    continues the same conversation/workflow so the lane is reused. When
    omitted, ``RGI_SESSION`` (or ``RGI_WORKFLOW_ID`` / ``RGI_CONVERSATION_ID``)
    is used, then a fresh id is generated - a generated id will not follow a
    resume.

    Extra keyword arguments go to ``rgi.report.Reporter`` (``label``, ``url``,
    ``token``, ``ident``, ``timeout``, ...). The return value is a ``RunReport``
    context manager exposing ``.middleware``, ``.run_kwargs``,
    ``.observe(event_or_result)``, ``.answer(id)``, ``.finish()`` and
    ``.pending``.
    """
    try:
        return RunReport(session, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a broken wrapper must not break a run
        fallback = RunReport.__new__(RunReport)
        fallback.session = str(session or "unavailable")
        fallback.label = "agent-framework run"
        fallback._reporter = None
        fallback._reporter_kwargs = {}
        fallback._entered = True          # nothing left to do on __enter__
        fallback._finished = False
        fallback._noted = {f"report_run: {exc}"}
        fallback._pending = {}
        fallback._middleware = None
        fallback._lock = threading.RLock()
        return fallback
