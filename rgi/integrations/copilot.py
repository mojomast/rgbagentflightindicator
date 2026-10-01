"""Report a GitHub Copilot SDK session to the panel, from the application side.

The GitHub Copilot SDK is the package an application uses to create and drive
Copilot sessions (``github-copilot-sdk`` on PyPI, ``@github/copilot-sdk`` on
npm). This adapter is neither a hook nor a watcher: the application hands it the
session object it already owns, and the adapter observes the events that
session emits. It never drives the session and never answers a permission or
input request on the application's behalf.

Scope, stated honestly: this follows sessions the application creates or adopts
through the SDK, where it holds the session object. It makes no claim about
Copilot conversations inside an editor or the CLI's own TUI, where there is no
SDK session to attach to.

The event names below are the SDK's generated ones (``SessionEventType`` in
``python/copilot/generated/session_events.py``, generated from the session
events schema shared by all language SDKs):

* ``user.message``, ``assistant.turn_start``, ``tool.execution_start`` ->
  ``working``
* ``permission.requested``, ``user_input.requested``, ``elicitation.requested``
  -> ``blocked(request=<the event's request id>)``
* the matching ``*.completed`` events -> ``resolve(<the same id>)``
* ``session.error`` -> ``error(message)``
* ``session.idle`` -> ``done()``. Outstanding waits keep the lane blocked; the
  reporter enforces that on its own, and this adapter does not resolve them
  early.
* ``session.task_complete`` -> ``info({"task": "complete"})`` at most. It is
  optional semantic detail, never the completion signal.

Payloads are read defensively from the SDK's per-event dataclasses or from
plain dicts, so a minimal duck-typed emitter that speaks the same event names
works too. Nothing here imports the SDK, and nothing here raises into the
application.
"""

from __future__ import annotations

import functools
import inspect
import threading
from datetime import datetime
from typing import Any, Callable

from ..report import Reporter, scrub

# Exact event type strings from the SDK's generated session-events schema.
EVENT_USER_MESSAGE = "user.message"
EVENT_TURN_START = "assistant.turn_start"
EVENT_TOOL_START = "tool.execution_start"
EVENT_IDLE = "session.idle"
EVENT_TASK_COMPLETE = "session.task_complete"
EVENT_ERROR = "session.error"
EVENT_PERMISSION_REQUESTED = "permission.requested"
EVENT_PERMISSION_COMPLETED = "permission.completed"
EVENT_USER_INPUT_REQUESTED = "user_input.requested"
EVENT_USER_INPUT_COMPLETED = "user_input.completed"
EVENT_ELICITATION_REQUESTED = "elicitation.requested"
EVENT_ELICITATION_COMPLETED = "elicitation.completed"

# Anything the session does that means "the agent is running".
_ACTIVITY = frozenset({EVENT_USER_MESSAGE, EVENT_TURN_START, EVENT_TOOL_START})

# Every event we subscribe to when the session only offers per-type listeners.
WATCHED = (
    EVENT_USER_MESSAGE,
    EVENT_TURN_START,
    EVENT_TOOL_START,
    EVENT_IDLE,
    EVENT_TASK_COMPLETE,
    EVENT_ERROR,
    EVENT_PERMISSION_REQUESTED,
    EVENT_PERMISSION_COMPLETED,
    EVENT_USER_INPUT_REQUESTED,
    EVENT_USER_INPUT_COMPLETED,
    EVENT_ELICITATION_REQUESTED,
    EVENT_ELICITATION_COMPLETED,
)


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present field, from a dict or an attribute object.

    Both spellings are passed by callers where the SDK uses snake_case and a
    JSON-shaped emitter would use camelCase.
    """
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _event_type(event: Any) -> str:
    raw = _field(event, "type", "event", "name", default="")
    if hasattr(raw, "value"):                    # SessionEventType enum member
        raw = raw.value
    try:
        return str(raw or "").strip()
    except Exception:                            # noqa: BLE001 - never raise
        return ""


def _event_data(event: Any) -> Any:
    return _field(event, "data", default=event)


def _event_time(event: Any) -> float | None:
    """The event's own timestamp in epoch seconds, when it carries one."""
    raw = _field(event, "timestamp", "time", "at", "created_at", "createdAt")
    if isinstance(raw, datetime):
        try:
            return raw.timestamp()
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _request_id(data: Any) -> str:
    raw = _field(data, "request_id", "requestId", default="")
    try:
        return str(raw).strip() if raw else ""
    except Exception:                            # noqa: BLE001 - never raise
        return ""


def _describe_permission(request: Any) -> tuple[str, str]:
    """(kind, detail) for a permission request object, without importing it."""
    try:
        kind = str(_field(request, "kind", default="") or "")
        for name in ("full_command_text", "fullCommandText", "intention",
                     "path", "file_path", "filePath", "url",
                     "tool_name", "toolName", "server_name", "serverName"):
            value = _field(request, name)
            if value:
                return kind or "permission", str(value)
        return kind or "permission", ""
    except Exception:                            # noqa: BLE001 - never raise
        return "permission", ""


def _positional_count(func: Callable) -> int:
    """How many positional parameters a callable takes, or -1 if unknown."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return -1
    return sum(1 for p in parameters.values()
               if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))


class CopilotReporter:
    """Observe one Copilot SDK session and mirror it onto one panel lane.

    Create it with the session's stable id, then :meth:`attach` the session.
    The first event claims the lane on demand, so an adapter that never sees an
    event never takes a lamp. :meth:`close` releases the lane and stops the
    subscriptions.
    """

    def __init__(self, session_id: str, *, label: str | None = None,
                 reporter: Reporter | None = None, namespace: str = "copilot",
                 log: Callable[[str], None] | None = None, **reporter_kwargs):
        self.session_id = str(session_id).strip()
        if not self.session_id:
            raise ValueError("session_id must be a stable, non-empty id")
        self._log = log
        if reporter is None:
            self.reporter = Reporter(namespace, self.session_id, label=label,
                                     log=log, **reporter_kwargs)
        else:
            self.reporter = reporter
        self._lock = threading.RLock()
        self._session: Any = None
        self._subscriptions: list[Callable[[], None]] = []
        self._wait_seq = 0

    # -- diagnostics -------------------------------------------------------
    def _note(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log(f"copilot: {message}")
        except Exception:                        # noqa: BLE001 - never raise
            pass

    # -- lifecycle ---------------------------------------------------------
    def start(self, *, label: str | None = None) -> bool:
        """Claim the lane now instead of waiting for the first event."""
        try:
            return self.reporter.start(label=label)
        except Exception as exc:                 # noqa: BLE001 - never raise
            self._note(f"could not start: {exc}")
            return False

    def attach(self, session: Any, *, label: str | None = None) -> bool:
        """Register on a session object. True when listeners were added.

        Two shapes work:

        * the Python SDK's ``session.on(handler)``, one callback for every
          event (the handler is additive; the application's own handlers are
          untouched);
        * an event-emitter shaped object with ``on(event_name, handler)``,
          where we register one listener per event, and a returned callable is
          kept as an unsubscribe.

        Attaching twice to the same object is a no-op. Never raises.
        """
        try:
            with self._lock:
                if session is self._session and self._subscriptions:
                    return True
                if self._session is not None:
                    self._unsubscribe()
                on = getattr(session, "on", None)
                if not callable(on):
                    self._note("session has no callable on(); not attached")
                    return False
                if not self._register(on):
                    return False
                self._session = session
                if label:
                    self.reporter.label = scrub(label, 80) or self.reporter.label
                return True
        except Exception as exc:                 # noqa: BLE001 - never raise
            self._note(f"attach failed: {exc}")
            return False

    def detach(self) -> None:
        """Stop observing. Never raises, safe to call twice."""
        try:
            with self._lock:
                self._unsubscribe()
        except Exception as exc:                 # noqa: BLE001 - never raise
            self._note(f"detach failed: {exc}")

    def close(self) -> None:
        """Detach and release the panel lane. Never raises."""
        try:
            self.detach()
        finally:
            try:
                self.reporter.close()
            except Exception as exc:             # noqa: BLE001 - never raise
                self._note(f"close failed: {exc}")

    def __enter__(self) -> "CopilotReporter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def pending(self) -> list[str]:
        """The request ids the lane is currently blocked on."""
        return self.reporter.pending

    # -- subscriptions -----------------------------------------------------
    def _register(self, on: Callable) -> bool:
        count = _positional_count(on)
        if count == 2:
            return self._register_each(on)
        if count == 1:
            return self._register_all(on)
        # Unknown shape: prefer the Python SDK form, fall back to per-event.
        try:
            return self._register_all(on)
        except TypeError:
            return self._register_each(on)

    def _register_all(self, on: Callable) -> bool:
        unsub = on(self._handle)
        self._remember(unsub)
        return True

    def _register_each(self, on: Callable) -> bool:
        before = len(self._subscriptions)
        try:
            for name in WATCHED:
                unsub = on(name, functools.partial(self._handle_named, name))
                self._remember(unsub)
        except Exception as exc:                 # noqa: BLE001 - roll back
            self._note(f"event registration failed: {exc}")
            while len(self._subscriptions) > before:
                self._drop_last()
            return False
        return True

    def _remember(self, unsub: Any) -> None:
        if callable(unsub):
            self._subscriptions.append(unsub)

    def _drop_last(self) -> None:
        try:
            self._subscriptions.pop()()
        except Exception:                        # noqa: BLE001 - never raise
            pass

    def _unsubscribe(self) -> None:
        for unsub in self._subscriptions:
            try:
                unsub()
            except Exception:                    # noqa: BLE001 - never raise
                pass
        self._subscriptions.clear()
        self._session = None

    # -- event mapping -----------------------------------------------------
    def _handle_named(self, name: str, event: Any) -> None:
        self._handle(event, fallback_name=name)

    def _handle(self, event: Any, fallback_name: str | None = None) -> None:
        try:
            name = _event_type(event) or (fallback_name or "")
            data = _event_data(event)
            at = _event_time(event)
            if name in _ACTIVITY:
                self.reporter.working(at=at)
            elif name == EVENT_IDLE:
                self.reporter.done(at=at)
            elif name == EVENT_TASK_COMPLETE:
                self._task_complete(data, at)
            elif name == EVENT_ERROR:
                self._error(data, at)
            elif name == EVENT_PERMISSION_REQUESTED:
                request = _field(data, "permission_request", "permissionRequest",
                                 default=data)
                kind, detail = _describe_permission(request)
                self._block(_request_id(data), kind, detail, at)
            elif name == EVENT_PERMISSION_COMPLETED:
                self._resolve(_request_id(data), at)
            elif name == EVENT_USER_INPUT_REQUESTED:
                question = str(_field(data, "question", default="") or "")
                self._block(_request_id(data), "question", question, at)
            elif name == EVENT_USER_INPUT_COMPLETED:
                self._resolve(_request_id(data), at)
            elif name == EVENT_ELICITATION_REQUESTED:
                message = str(_field(data, "message", "question", default="") or "")
                self._block(_request_id(data), "elicitation", message, at)
            elif name == EVENT_ELICITATION_COMPLETED:
                self._resolve(_request_id(data), at)
        except Exception as exc:                 # noqa: BLE001 - never raise
            self._note(f"ignored one event: {exc}")

    def _task_complete(self, data: Any, at: float | None) -> None:
        fields: dict = {"task": "complete"}
        success = _field(data, "success")
        if isinstance(success, bool):
            fields["task_success"] = success
        self.reporter.info(fields, at=at)

    def _error(self, data: Any, at: float | None) -> None:
        message = str(_field(data, "message", "error", default="") or "")
        kind = str(_field(data, "error_type", "errorType",
                          "error_code", "errorCode", default="") or "")
        text = f"{kind}: {message}".strip(": ") if (kind or message) \
            else "the session reported an error"
        self.reporter.error(text, at=at)

    def _block(self, request_id: str, action: str, message: str,
               at: float | None) -> None:
        if not (action or message):
            action = "attention"
        self.reporter.blocked(request=request_id or None, action=action,
                              message=message, at=at)

    def _resolve(self, request_id: str, at: float | None) -> None:
        # Only resolve ids we are actually blocked on: an event for a wait that
        # predates the attachment should not force a state push.
        if request_id and request_id in self.reporter.pending:
            self.reporter.resolve(request_id, at=at)

    # -- wrapping the application's handlers -------------------------------
    def wrap_permission_handler(self, handler: Callable | None) -> Callable | None:
        """Wrap an ``on_permission_request`` handler without changing it.

        The original handler is called first; its return value (or awaitable) is
        passed through unchanged, and its exceptions propagate. While an async
        handler is pending the lane is blocked; a sync handler cannot give the
        panel a live wait, so the blocked/resolve pair is only a record - the
        live view comes from the ``permission.requested`` event.
        """
        if not callable(handler):
            return handler

        def describe(request: Any, _args: tuple) -> tuple[str, str, Any]:
            kind, detail = _describe_permission(request)
            return kind, detail, _field(request, "tool_call_id", "toolCallId",
                                        "request_id", "requestId")

        return self._wrap(handler, describe, "permission")

    def wrap_user_input_handler(self, handler: Callable | None) -> Callable | None:
        """Wrap an ``on_user_input_request`` handler the same way."""
        if not callable(handler):
            return handler

        def describe(request: Any, _args: tuple) -> tuple[str, str, Any]:
            question = str(_field(request, "question", default="") or "")
            return "question", question, _field(request, "tool_call_id", "toolCallId",
                                                "request_id", "requestId")

        return self._wrap(handler, describe, "question")

    def _wrap(self, handler: Callable, describe: Callable, prefix: str) -> Callable:
        @functools.wraps(handler)
        def wrapped(request: Any, *args: Any, **kwargs: Any) -> Any:
            result = handler(request, *args, **kwargs)   # the app decides first
            try:
                action, message, hint = describe(request, args)
            except Exception:                # noqa: BLE001 - never raise here
                action, message, hint = prefix, "", ""
            try:
                rid = str(hint).strip() if hint else ""
            except Exception:                # noqa: BLE001 - never raise here
                rid = ""
            rid = rid or self._next_wait_id(prefix)
            if inspect.isawaitable(result):
                self._block(rid, action, message, None)   # visible while it waits
                async def finish():
                    try:
                        return await result
                    finally:
                        self._resolve(rid, None)
                return finish()
            self._block(rid, action, message, None)
            self._resolve(rid, None)
            return result

        return wrapped

    def _next_wait_id(self, prefix: str) -> str:
        with self._lock:
            self._wait_seq += 1
            return f"{prefix}-{self._wait_seq}"


def observe(session: Any, *, session_id: str | None = None,
            label: str | None = None, **kwargs: Any) -> CopilotReporter | None:
    """Build and attach an adapter for one SDK session in a single call.

    Derives the stable session id from the session object when it is not given.
    Returns ``None`` (never raises) when the session has no id or no usable
    ``on``.
    """
    try:
        sid = str(session_id or _field(session, "session_id", "sessionId",
                                       default="") or "").strip()
        if not sid:
            log = kwargs.get("log")
            if callable(log):
                log("copilot: session has no session_id; not observing")
            return None
        adapter = CopilotReporter(sid, label=label, **kwargs)
        if not adapter.attach(session):
            adapter.close()
            return None
        return adapter
    except Exception:                            # noqa: BLE001 - never raise
        return None
