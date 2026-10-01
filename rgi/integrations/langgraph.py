"""LangGraph adapter: one lane per thread, an interrupt blocks until it is resumed.

LangGraph runs are checkpointed under a *thread*: the same ``thread_id`` resumes
the same paused graph, and a checkpoint is what an interrupted run is waiting
on. That makes the thread id the lane's identity, so a run that pauses, ends
its stream, and is resumed later by a ``Command(resume=...)`` lands on the same
lane instead of claiming a second one.

The rules this adapter keeps, because each of them is a LangGraph semantic:

* **Only a finished root run completes.** A stream ending is not completion
  when the graph paused: ``interrupt()`` surfaces as an interrupt payload (in
  ``stream_mode="updates"`` / ``"values"`` as ``{"__interrupt__": (...)}``, in
  the v2 stream parts as ``interrupts``, and in ``astream_events`` through
  ``GraphCallbackHandler.on_interrupt``). Every pending interrupt becomes a
  ``blocked`` request, keyed by the interrupt's own ``id`` when it has one and
  by a stable synthetic id otherwise. The lane stays blocked until a resume
  arrives, however many streams end in between.
* **A resume returns to working.** ``on_resume`` (and observing a
  ``Command(resume=...)`` or fresh run activity on the thread) resolves the
  open interrupts and reports ``working``; the run is only ``done`` when the
  root run ends with nothing pending.
* **The root owns the lamp; tasks are metadata.** Node runs seen through the
  callback API and ``stream_mode="tasks"`` entries become ``child()`` detail,
  never a lane of their own, and a child finishing never completes the lane.
* **Events are correlated by thread.** A callback event carrying a different
  ``thread_id`` in its metadata is ignored, so one handler can safely be shared
  by concurrent executions.

Neither ``langgraph`` nor ``langchain_core`` is imported at module import (the
callback base class is imported inside ``handler``); everything here is
duck-typed and works with the released package or with fakes in tests. Nothing
in this module raises into the agent: every public entry point is guarded and
reports failure as ``False``.

Usage::

    from rgi.integrations.langgraph import report_run

    thread_id = "research-42"                 # stable across resumes
    config = {"configurable": {"thread_id": thread_id}}

    with report_run(thread_id, label="research") as run:
        for chunk in graph.stream(input, config, stream_mode=["updates", "values"]):
            run.observe(chunk)                 # interrupts become blocked waits
        # exits blocked if the graph interrupted; done if it really finished

    # later, in the same process or a new one:
    with report_run(thread_id, label="research") as run:
        run.observe(Command(resume="approved"))    # returns to working
        for chunk in graph.stream(Command(resume="approved"), config):
            run.observe(chunk)
    # run.config carries the thread id and the callback handler:
    #   graph.stream(input, run.config)
"""

from __future__ import annotations

import functools
import hashlib
import os
import sys
import threading
import uuid

from ..report import Reporter, scrub

NAMESPACE = "langgraph"
"""Panel namespace; the lane key is ``langgraph:<thread id>``."""

_ENV_NAMES = ("RGI_THREAD_ID", "RGI_SESSION")
_MAX_PENDING = 32

_MISSING = object()
_STREAM_MODES = frozenset(
    {"values", "updates", "messages", "custom", "checkpoints", "tasks", "debug"}
)

# A wait outlives the ``report_run`` block that surfaced it: the next block for
# the thread re-blocks it before the resumed run settles, instead of blinking
# "working" while the human still has it open. A fresh process resumes by
# passing ``pending=[...]`` explicitly.
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


# -- small helpers -------------------------------------------------------------

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


def _interrupts(payload) -> list:
    """Interrupt payloads carried by a state dict or an object.

    Only LangGraph's reserved ``__interrupt__`` key counts on a mapping: a
    user state key literally named ``interrupts`` must not block the lane.
    """
    if payload is None:
        return []
    if isinstance(payload, dict):
        value = payload.get("__interrupt__")
    else:
        value = _attr(payload, "interrupts")
    if not value:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _part_interrupts(payload) -> list:
    """Interrupt payloads carried by a stream part, task payload, or params."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        value = payload.get("__interrupt__") or payload.get("interrupts")
    else:
        value = _attr(payload, "interrupts")
    if not value:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def _stream_part(event):
    """``(mode, payload, interrupts)`` for a v2 stream part, else None."""
    if not isinstance(event, dict):
        return None
    mode = event.get("type")
    if not isinstance(mode, str) or mode not in _STREAM_MODES or "data" not in event:
        return None
    return mode, event.get("data"), _part_interrupts(event)


def _is_command(event) -> bool:
    if isinstance(event, dict):
        return False
    if not hasattr(event, "resume"):
        return False
    return (type(event).__name__ == "Command" or hasattr(event, "goto")
            or hasattr(event, "graph"))


def _is_snapshot(event) -> bool:
    return (not isinstance(event, dict) and hasattr(event, "next")
            and hasattr(event, "config") and hasattr(event, "tasks"))


def _is_interrupt(event) -> bool:
    return (not isinstance(event, dict) and not _is_snapshot(event)
            and hasattr(event, "value") and hasattr(event, "id"))


def _serialized_name(serialized) -> str:
    name = _attr(serialized, "name")
    if not name and isinstance(serialized, dict):
        for key in ("id",):
            serialized_id = serialized.get(key)
            if isinstance(serialized_id, (list, tuple)) and serialized_id:
                name = serialized_id[-1]
    return str(name or "")


class _CallbackEvent:
    """The event shape this adapter reads from LangChain callback handlers."""

    __slots__ = ("event", "run_id", "parent_ids", "name", "metadata", "data", "tags")

    def __init__(self, event, run_id=None, parent_run_id=None, name="",
                 metadata=None, data=None, tags=None):
        self.event = event
        self.run_id = run_id
        self.parent_ids = [parent_run_id] if parent_run_id is not None else []
        self.name = name
        self.metadata = metadata
        self.data = data
        self.tags = list(tags or [])


# -- the lane ------------------------------------------------------------------

class RunReport:
    """One LangGraph thread's lane, usable as ``with`` / ``async with``.

    Attributes/properties:
        thread_id: the stable thread id the lane is keyed by (same as
                   ``session``).
        pending:   interrupt request ids still open (these block the lane).
        config:    a fresh ``RunnableConfig`` carrying this thread id and the
                   callback handler: ``graph.stream(input, run.config)``.
        handler:   the callback handler (a ``GraphCallbackHandler`` subclass
                   when ``langgraph`` is installed, a plain stand-in
                   otherwise) that feeds this lane.
        reporter:  the underlying ``rgi.report.Reporter`` (or ``None``).
    """

    def __init__(self, thread_id: str | None = None, *, label: str | None = None,
                 reporter: Reporter | None = None, pending: list[str] | None = None,
                 **reporter_kwargs):
        session = str(thread_id).strip() if thread_id is not None else None
        self.session = session or _env_session() or uuid.uuid4().hex
        self.label = label or "langgraph run"
        self._reporter = reporter
        self._reporter_kwargs = reporter_kwargs
        self._entered = False
        self._finished = False
        self._noted: set[str] = set()
        self._pending: dict[str, str] = {}
        self._handler = None
        self._lock = threading.RLock()
        for request in pending or []:
            request = scrub(str(request), 64)
            if request:
                self._pending[request] = "interrupt"

    # -- plumbing ----------------------------------------------------------
    def _note_once(self, message: str) -> None:
        """Diagnostics to stderr, once per distinct message, debug-gated."""
        message = scrub(message, 160)
        if not message or message in self._noted:
            return
        self._noted.add(message)
        if os.environ.get("RGI_HOOK_DEBUG") or os.environ.get("RGI_DEBUG"):
            print(f"[rgi langgraph] {message}", file=sys.stderr)

    @property
    def thread_id(self) -> str:
        return self.session

    @property
    def reporter(self) -> Reporter | None:
        return self._reporter

    @property
    def pending(self) -> list[str]:
        return sorted(self._pending)

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.session},
                "callbacks": [self.handler]}

    # -- the public state changes -----------------------------------------
    @_guarded
    def working(self) -> bool:
        return bool(self._reporter and self._reporter.working())

    @_guarded
    def done(self) -> bool:
        """Complete the lane explicitly (``finish`` is the stream-end guard).

        A pending interrupt still keeps the lane blocked in the reporter: a
        real wait outlives a completed result.
        """
        self._finished = True
        return bool(self._reporter and self._reporter.done())

    @_guarded
    def finish(self) -> bool:
        """The current stream ended: done unless an interrupt is still open."""
        with self._lock:
            if self._pending:
                return True          # a real wait outlives the stream
            if self._finished:
                return True
            self._finished = True
            return bool(self._reporter and self._reporter.done())

    @_guarded
    def error(self, message: str = "") -> bool:
        self._finished = True
        return bool(self._reporter and self._reporter.error(message))

    @_guarded
    def resume(self, request: str | None = None) -> bool:
        """Resolve open interrupts (all of them, or one) and report working."""
        with self._lock:
            requests = ([scrub(str(request), 64)] if request is not None
                        else list(self._pending))
            for rid in requests:
                self._resolve_locked(rid)
            return self.working()

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
        return bool(self._reporter.child(scrub(str(child_id), 64) or "task",
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
    def _interrupt_id(self, interrupt, index: int = 0) -> str:
        iid = _attr(interrupt, "id")
        if not iid:
            value = _attr(interrupt, "value", default=interrupt)
            digest = hashlib.sha1(
                f"{self.session}|{value!r}".encode("utf-8")).hexdigest()
            iid = f"interrupt-{digest[:16]}"
        return scrub(str(iid), 64) or f"interrupt-{index}"

    @_guarded
    def _block_interrupts(self, interrupts) -> bool:
        blocked = False
        for index, interrupt in enumerate(list(interrupts)[:_MAX_PENDING]):
            rid = self._interrupt_id(interrupt, index)
            message = repr(_attr(interrupt, "value", default=interrupt))
            if rid in self._pending:
                blocked = True
                continue
            self._pending[rid] = message
            _remember(self.session, {rid: scrub(message, 120)})
            if self._reporter is not None:
                self._reporter.blocked(request=rid, action="interrupt",
                                       message=message)
            blocked = True
        return blocked

    # -- event folding ------------------------------------------------------
    @_guarded
    def observe(self, event) -> bool:
        """Fold one LangGraph event into the lane (unknown shapes are ignored)."""
        if event is None:
            return False
        with self._lock:
            return self._observe(event)

    def _observe(self, event) -> bool:
        name = _attr(event, "event")
        if isinstance(name, str) and name.startswith("on_"):
            return self._on_callback(event, name)
        method = _attr(event, "method")
        params = _attr(event, "params")
        if isinstance(method, str) and params is not None:
            return self._on_protocol(method, params)
        part = _stream_part(event)
        if part is not None:
            return self._on_stream_part(*part)
        if _is_command(event):
            return self._on_command(event)
        if _is_snapshot(event):
            return self._on_snapshot(event)
        if _is_interrupt(event):
            return self._block_interrupts([event])
        if (not isinstance(event, dict) and hasattr(event, "id")
                and hasattr(event, "path")
                and _attr(event, "interrupts", default=_MISSING) is not _MISSING):
            return self._on_task(event)
        if isinstance(event, (tuple, list)):
            return self._on_part(event)
        if isinstance(event, dict):
            return self._on_dict(event)
        return False

    def _on_callback(self, event, event_name: str) -> bool:
        metadata = _attr(event, "metadata")
        thread = _attr(metadata, "thread_id")
        if thread is not None and str(thread) != str(self.session):
            return False
        parent_ids = _attr(event, "parent_ids")
        if parent_ids is None:
            parent_run_id = _attr(event, "parent_run_id")
            parent_ids = [parent_run_id] if parent_run_id is not None else []
        else:
            parent_ids = list(parent_ids)
        root = not parent_ids
        run_id = _attr(event, "run_id")
        chain_name = _attr(event, "name")
        data = _attr(event, "data")
        tags = _attr(event, "tags") or ()
        if not root and "langsmith:hidden" in tags:
            return True                       # internal channel runs are noise

        if event_name == "on_chain_start":
            if root:
                if self._pending:
                    self._resume_event(None)
                return self.working()
            return self.child(run_id or chain_name or "task", state="working",
                              label=str(chain_name or ""))
        if event_name == "on_chain_end":
            interrupts = _interrupts(data)
            if root:
                if interrupts:
                    return self._block_interrupts(interrupts)
                return self.finish()
            if data is not None:
                nested = _interrupts(data)
                if nested:
                    self._block_interrupts(nested)
            return self.child_done(run_id or chain_name or "task")
        if event_name == "on_chain_error":
            message = str(data or "chain failed")
            if root:
                return self.error(message)
            return self.child(run_id or chain_name or "task", state="error",
                              label=str(chain_name or ""))
        return False

    def _on_command(self, event) -> bool:
        resume = _attr(event, "resume")
        with self._lock:
            if isinstance(resume, dict):
                for rid in list(self._pending):
                    if rid in resume:
                        self._resolve_locked(rid)
            else:
                for rid in list(self._pending):
                    self._resolve_locked(rid)
            return self.working()

    def _on_snapshot(self, snapshot) -> bool:
        config = _attr(snapshot, "config")
        thread = _attr(_attr(config, "configurable"), "thread_id")
        if thread is not None and str(thread) != str(self.session):
            return False
        interrupts = _interrupts(snapshot)
        if not interrupts:
            for task in list(_attr(snapshot, "tasks", default=()) or ()):
                interrupts = interrupts or _interrupts(task)
        if interrupts:
            return self._block_interrupts(interrupts)
        if not _attr(snapshot, "next", default=()):
            return self.finish()
        return self.working()

    def _on_task(self, task) -> bool:
        """A ``PregelTask``-shaped object: child metadata, maybe a wait."""
        interrupts = _part_interrupts(task)
        if interrupts:
            self._block_interrupts(interrupts)
        child_id = _attr(task, "id")
        name = _attr(task, "name") or child_id or "task"
        error = _attr(task, "error")
        result = _attr(task, "result", default=_MISSING)
        if error is not None:
            return self.child(child_id or name, state="error", label=str(name))
        if result is not _MISSING:
            return self.child_done(child_id or name)
        return self.child(child_id or name, state="working", label=str(name))

    def _on_task_payload(self, payload) -> bool:
        if isinstance(payload, dict) and payload.get("type") in ("task", "task_result"):
            inner = payload.get("payload")
            if isinstance(inner, dict):
                payload = inner
        if isinstance(payload, dict):
            interrupts = _part_interrupts(payload)
            if interrupts:
                self._block_interrupts(interrupts)
            child_id = str(payload.get("id") or payload.get("name") or "task")
            name = str(payload.get("name") or child_id)
            error = payload.get("error")
            if "result" in payload or error is not None:
                if error is not None:
                    return self.child(child_id, state="error", label=name)
                return self.child_done(child_id)
            return self.child(child_id, state="working", label=name)
        if _attr(payload, "interrupts", default=_MISSING) is not _MISSING:
            return self._on_task(payload)
        return False

    def _on_stream_part(self, mode: str, payload, interrupts) -> bool:
        """One v2 ``StreamPart``: ``{"type", "ns", "data", "interrupts"}``."""
        if mode in ("tasks", "debug"):
            return self._on_task_payload(payload)
        if mode == "checkpoints":
            return False
        if interrupts:
            return self._block_interrupts(interrupts)
        if isinstance(payload, dict):
            pending = _interrupts(payload)
            if pending:
                return self._block_interrupts(pending)
        return self._activity()

    def _on_protocol(self, method: str, params) -> bool:
        data = _attr(params, "data")
        interrupts = _part_interrupts(params)
        if not interrupts and isinstance(data, dict):
            interrupts = _interrupts(data)
        if method in ("tasks", "debug"):
            return self._on_task_payload(data)
        if method in ("values", "updates", "custom", "messages"):
            if interrupts:
                return self._block_interrupts(interrupts)
            return self._on_dict(data) if isinstance(data, dict) else self._activity()
        if method == "lifecycle":
            kind = _attr(data, "event") if not isinstance(data, dict) else data.get("event")
            trigger = (_attr(data, "trigger_call_id") if not isinstance(data, dict)
                       else data.get("trigger_call_id"))
            if kind == "interrupted":
                namespace = _attr(params, "namespace", default=()) or ()
                label = "-".join(map(str, namespace)) or "root"
                request = f"lifecycle-{trigger}" if trigger else f"lifecycle-{label}"
                return self._block_interrupts([{"id": request}])
            if kind in ("started", "completed", "failed", "drained"):
                if trigger:
                    if kind == "completed":
                        return self.child_done(trigger)
                    state = "error" if kind == "failed" else "working"
                    return self.child(trigger, state=state)
                return False
            if kind == "resume":
                return self._activity()
            return False
        if interrupts:
            return self._block_interrupts(interrupts)
        return False

    def _on_part(self, part) -> bool:
        items = list(part)
        mode = None
        payload = None
        if len(items) == 2 and isinstance(items[0], str) and items[0] in _STREAM_MODES:
            mode, payload = items
        elif (len(items) == 3 and isinstance(items[1], str)
              and items[1] in _STREAM_MODES):
            _namespace, mode, payload = items
        elif len(items) == 2:
            payload = items[-1]
        else:
            return False
        if mode in ("tasks", "debug"):
            return self._on_task_payload(payload)
        if mode == "checkpoints":
            return False
        if isinstance(payload, dict):
            return self._on_dict(payload)
        if _attr(payload, "interrupts", default=_MISSING) is not _MISSING:
            return self._on_task(payload)
        if mode is not None:
            return self._activity()
        return False

    def _on_dict(self, payload: dict) -> bool:
        interrupts = _interrupts(payload)
        if interrupts:
            return self._block_interrupts(interrupts)
        return self._activity()

    def _activity(self) -> bool:
        if self._pending:
            return self.resume()
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
                self._reporter.blocked(request=request, action="interrupt",
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

    # -- callback handler ---------------------------------------------------
    @property
    def handler(self):
        if self._handler is None:
            self._handler = self._make_handler()
        return self._handler

    def _make_handler(self):
        """A ``GraphCallbackHandler`` subclass when LangGraph is importable."""
        try:
            from langgraph.callbacks import GraphCallbackHandler  # noqa: PLC0415
            base = GraphCallbackHandler
        except Exception:  # noqa: BLE001 - framework absent: still drivable
            try:
                from langchain_core.callbacks import BaseCallbackHandler  # noqa: PLC0415
                base = BaseCallbackHandler
            except Exception:  # noqa: BLE001
                base = object
        report = self

        class _PanelCallbackHandler(base):  # type: ignore[misc, valid-type]
            """Nowhere near the hot path: a state push happens per real change."""

            run_inline = True
            raise_error = False
            ignore_llm = True
            ignore_chat_model = True
            ignore_retriever = True
            ignore_agent = True
            ignore_retry = True
            ignore_custom_event = True

            def on_chain_start(self, serialized=None, inputs=None, *, run_id=None,
                               parent_run_id=None, tags=None, metadata=None, **kwargs):
                report._callback_event("on_chain_start", run_id, parent_run_id,
                                       _serialized_name(serialized), metadata, inputs,
                                       tags)

            def on_chain_end(self, outputs=None, *, run_id=None, parent_run_id=None,
                             tags=None, **kwargs):
                report._callback_event("on_chain_end", run_id, parent_run_id,
                                       None, None, outputs, tags)

            def on_chain_error(self, error=None, *, run_id=None, parent_run_id=None,
                               tags=None, **kwargs):
                report._callback_event("on_chain_error", run_id, parent_run_id,
                                       None, None, error, tags)

            def on_interrupt(self, event=None):        # noqa: D102
                report._interrupt_event(event)

            def on_resume(self, event=None):           # noqa: D102
                report._resume_event(event)

        return _PanelCallbackHandler()

    @_guarded
    def _callback_event(self, name, run_id, parent_run_id, label, metadata, data,
                        tags=None):
        return self.observe(_CallbackEvent(name, run_id=run_id,
                                           parent_run_id=parent_run_id,
                                           name=label, metadata=metadata, data=data,
                                           tags=tags))

    @_guarded
    def _interrupt_event(self, event) -> bool:
        interrupts = _interrupts(event)
        if interrupts:
            return self._block_interrupts(interrupts)
        checkpoint = _attr(event, "checkpoint_id")
        if checkpoint:
            return self._block_interrupts([{"id": str(checkpoint)}])
        return False

    @_guarded
    def _resume_event(self, event) -> bool:       # noqa: ARG002 - payload unused
        return self.resume()


def report_run(thread_id: str | None = None, **kwargs) -> RunReport:
    """Wrap one LangGraph thread (and its resumes) in a panel lane.

    ``thread_id`` is the stable identity: pass the same value in
    ``config={"configurable": {"thread_id": ...}}`` for every run and resume so
    all of them land on the same lane. When omitted, ``RGI_THREAD_ID`` (or
    ``RGI_SESSION``) is used, then a fresh id is generated - a generated id
    will not follow a resume.

    Extra keyword arguments go to ``rgi.report.Reporter`` (``label``, ``url``,
    ``token``, ``ident``, ``timeout``, ...). The return value is a
    ``RunReport`` context manager exposing ``.observe(chunk_or_event)``,
    ``.config``, ``.handler``, ``.resume(id)``, ``.finish()`` and ``.pending``.
    """
    try:
        return RunReport(thread_id, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a broken wrapper must not break a run
        fallback = RunReport.__new__(RunReport)
        fallback.session = str(thread_id or "unavailable")
        fallback.label = "langgraph run"
        fallback._reporter = None
        fallback._reporter_kwargs = {}
        fallback._entered = True          # nothing left to do on __enter__
        fallback._finished = False
        fallback._noted = {f"report_run: {exc}"}
        fallback._pending = {}
        fallback._handler = None
        fallback._lock = threading.RLock()
        return fallback
