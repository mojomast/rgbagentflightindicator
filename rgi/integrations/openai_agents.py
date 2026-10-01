"""OpenAI Agents SDK adapter: one lane per top-level ``Runner`` run.

The SDK already has the lifecycle this needs, so the adapter is thin: build a
``RunHooks`` implementation, hand it to ``Runner.run(...)``, and fold the
finished ``RunResult`` back into one lane. The rules follow the SDK's own
semantics:

* The **root** run owns the lamp. ``Runner.run`` wraps the whole loop -
  handoffs and ``Agent.as_tool()`` calls happen inside it - so only that
  top-level boundary reports ``done``. An individual agent finishing, a
  handoff completing, or a tool returning is metadata (``child()``), never a
  lamp change.
* Approvals are a *pause*, not an end. ``RunResult.interruptions`` holds one
  ``ToolApprovalItem`` per tool call waiting on a human; each becomes a
  ``blocked`` request keyed by the item's ``call_id``. The lane stays blocked
  while any request is open, returns to ``working`` as they resolve, and is
  only reported ``done`` when the run finishes with no interruptions left.
* A resume is the same conversation. The lane identity is the
  ``conversation_id`` you give this adapter, so a paused run continued later
  lands on the same lane instead of claiming a second one. Pass the same
  string to ``Runner.run(..., conversation_id=...)`` so the SDK's own
  conversation tracking agrees with the lamp.

The ``agents`` package is imported inside functions, never at module import,
so this module stays importable without the SDK installed. Nothing here
raises into the agent: the public entry points are guarded and report a
failure as ``False``.

Usage::

    from agents import Agent, Runner
    from rgi.integrations.openai_agents import report_run

    conversation_id = "ticket-1234"          # stable across resumes

    async with report_run(conversation_id=conversation_id, label="triage") as run:
        result = await Runner.run(agent, prompt, hooks=run.hooks,
                                  conversation_id=conversation_id)
        run.observe(result)
        while result.interruptions:          # human-in-the-loop approval
            state = result.to_state()
            for item in result.interruptions:
                state.approve(item)          # or state.reject(item)
                run.resolve(item.call_id)
            result = await Runner.run(agent, state, hooks=run.hooks)
            run.observe(result)
"""

from __future__ import annotations

import functools
import os
import sys
import threading
import uuid

from ..report import Reporter, scrub

NAMESPACE = "openai-agents"
"""Panel namespace; the lane key is ``openai-agents:<conversation id>``."""

_ENV_NAMES = ("RGI_CONVERSATION_ID", "RGI_SESSION")
_MAX_PENDING = 32

# Approvals outlive a single `report_run` block when the application loops
# over `Runner.run`. Remembering them per conversation lets the next block in
# the same process re-block the lane before the resumed run settles, instead
# of blinking "working" while a human still has an open request. A fresh
# process resumes by passing `pending=[...]` explicitly.
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


def _name(value, fallback: str = "agent") -> str:
    """Best-effort display name for an agent/tool/context object."""
    for attribute in ("name", "tool_name", "agent_name", "id"):
        try:
            found = getattr(value, attribute, None)
        except Exception:  # noqa: BLE001 - properties may raise on odd objects
            found = None
        if found:
            return str(found)
    return fallback


class RunReport:
    """One root run's lane, usable as ``with`` / ``async with``.

    Attributes/properties:
        session:  the stable conversation id the lane is keyed by.
        pending:  approval request ids still open.
        hooks:    a ``RunHooks`` instance to pass to ``Runner.run`` (built on
                  ``__enter__``; a plain fallback object when ``agents`` is
                  not installed, so the wrapper is testable and importable
                  everywhere).
        reporter: the underlying ``rgi.report.Reporter`` (or ``None``).
    """

    def __init__(self, conversation_id: str | None = None, *, label: str | None = None,
                 reporter: Reporter | None = None, pending: list[str] | None = None,
                 **reporter_kwargs):
        session = str(conversation_id).strip() if conversation_id is not None else None
        self.session = session or _env_session() or uuid.uuid4().hex
        self.label = label or "openai-agents run"
        self._reporter = reporter
        self._reporter_kwargs = reporter_kwargs
        self._entered = False
        self._finished = False
        self._noted: set[str] = set()
        self._hooks: object | None = None
        self._pending: dict[str, str] = {}
        for request in pending or []:
            request = scrub(str(request), 64)
            if request:
                self._pending[request] = "awaiting approval"

    # -- plumbing ----------------------------------------------------------
    def _note_once(self, message: str) -> None:
        """Diagnostics to stderr, once per distinct message, debug-gated."""
        message = scrub(message, 160)
        if not message or message in self._noted:
            return
        self._noted.add(message)
        if os.environ.get("RGI_HOOK_DEBUG") or os.environ.get("RGI_DEBUG"):
            print(f"[rgi openai-agents] {message}", file=sys.stderr)

    @property
    def reporter(self) -> Reporter | None:
        return self._reporter

    @property
    def pending(self) -> list[str]:
        return sorted(self._pending)

    @property
    def hooks(self):
        if self._hooks is None:
            self._hooks = self._make_hooks()
        return self._hooks

    # -- the public state changes -----------------------------------------
    @_guarded
    def working(self) -> bool:
        return bool(self._reporter and self._reporter.working())

    @_guarded
    def done(self) -> bool:
        self._finished = True
        requests = list(self._pending)
        self._pending.clear()
        _forget(self.session, requests)
        return bool(self._reporter and self._reporter.done())

    @_guarded
    def error(self, message: str = "") -> bool:
        self._finished = True
        return bool(self._reporter and self._reporter.error(message))

    @_guarded
    def child(self, child_id: str, *, state: str = "working", label: str = "",
              tokens: int | None = None) -> bool:
        if not self._reporter:
            return False
        return self._reporter.child(child_id, state=state, label=label, tokens=tokens)

    @_guarded
    def child_done(self, child_id: str) -> bool:
        return bool(self._reporter and self._reporter.child_done(child_id))

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

    # -- folding a finished run back into the lane -------------------------
    def _interruptions(self, result) -> dict[str, str]:
        """Map each pending ``ToolApprovalItem`` to a request id and label."""
        items = getattr(result, "interruptions", None)
        if not items:
            return {}
        current: dict[str, str] = {}
        for index, item in enumerate(list(items)[:_MAX_PENDING]):
            raw = getattr(item, "raw_item", None)
            request = None
            for candidate in (getattr(item, "call_id", None),
                              getattr(raw, "call_id", None),
                              getattr(raw, "id", None),
                              getattr(item, "id", None)):
                if candidate:
                    request = str(candidate)
                    break
            label = _name(item, _name(raw, "approval"))
            current[request or f"approval-{index}"] = label
        return current

    @_guarded
    def observe(self, result) -> bool:
        """Fold a settled ``RunResult`` into the lane.

        Interruptions still present keep their waits (blocked, one id each);
        waits that are no longer in the result are resolved. With no
        interruptions left, and only then, the run is reported ``done``.
        A streaming result that has not completed yet stays ``working``.
        """
        if result is None:
            return False
        current = self._interruptions(result)
        for request in list(self._pending):
            if request not in current:
                self.resolve(request)
        for request, label in current.items():
            self._block(request, label)
        if current:
            return True
        if getattr(result, "is_complete", True) is False:
            return False                      # stream not drained; not done yet
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
        self._hooks = self._make_hooks()
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

    # -- hooks -------------------------------------------------------------
    def _make_hooks(self):
        """A ``RunHooks`` subclass when the SDK is importable, else a stand-in."""
        try:
            from agents.lifecycle import RunHooks  # noqa: PLC0415 - optional dep
            base = RunHooks
        except Exception:  # noqa: BLE001 - SDK absent: hooks are still drivable
            base = object

        report = self

        class _RunHooks(base):  # type: ignore[misc, valid-type]
            """Panel hooks: children are metadata, the root boundary is the lane."""

            async def on_agent_start(self, context, agent, *args, **kwargs) -> None:
                report._agent_started(agent)

            async def on_agent_end(self, context, agent, output=None, *args, **kwargs) -> None:
                report._agent_finished(agent)

            async def on_handoff(self, context, from_agent=None, to_agent=None,
                                 *args, **kwargs) -> None:
                report._handoff(from_agent, to_agent)

            async def on_tool_start(self, context, agent=None, tool=None,
                                    *args, **kwargs) -> None:
                report._tool(tool, "running")

            async def on_tool_end(self, context, agent=None, tool=None, result=None,
                                  *args, **kwargs) -> None:
                report._tool(tool, "done")

        return _RunHooks()

    @_guarded
    def _agent_started(self, agent) -> bool:
        name = _name(agent)
        return self._child_bounded(name, "working", name)

    @_guarded
    def _agent_finished(self, agent) -> bool:
        name = _name(agent)
        return self._child_bounded(name, "done", name)

    @_guarded
    def _handoff(self, from_agent, to_agent) -> bool:
        source, target = _name(from_agent), _name(to_agent)
        self._child_bounded(source, "done", source)
        return self._child_bounded(target, "working", f"{source} -> {target}")

    @_guarded
    def _tool(self, tool, state: str) -> bool:
        name = _name(tool, "tool")
        if not self._reporter:
            return False
        return self._reporter.info({"tool": name, "tool_state": state})

    def _child_bounded(self, child_id: str, state: str, label: str) -> bool:
        if not self._reporter:
            return False
        child_id = scrub(child_id, 64) or "agent"
        return self._reporter.child(child_id, state=state, label=scrub(label, 60))


def report_run(conversation_id: str | None = None, **kwargs) -> RunReport:
    """Wrap one top-level ``Runner`` run in a panel lane.

    ``conversation_id`` is the stable identity: pass the same value to
    ``Runner.run(..., conversation_id=...)`` and to every resumed run so all
    of them land on the same lane. When omitted, ``RGI_CONVERSATION_ID`` (or
    ``RGI_SESSION``) is used, then a fresh id is generated for this run -
    a generated id will not follow a resume.

    Extra keyword arguments go to ``rgi.report.Reporter`` (``label``,
    ``url``, ``token``, ``ident``, ``timeout``, ...). The return value is a
    ``RunReport`` context manager exposing ``.hooks``, ``.observe(result)``,
    ``.resolve(id)`` and ``.pending``.
    """
    try:
        return RunReport(conversation_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a broken wrapper must not break a run
        fallback = RunReport.__new__(RunReport)
        fallback.session = str(conversation_id or "unavailable")
        fallback.label = "openai-agents run"
        fallback._reporter = None
        fallback._reporter_kwargs = {}
        fallback._entered = True          # nothing left to do on __enter__
        fallback._finished = False
        fallback._noted = set()
        fallback._hooks = None
        fallback._pending = {}
        fallback._note_once(f"report_run: {type(exc).__name__}: {exc}")
        return fallback
