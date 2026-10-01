"""Claude Code command hooks: one process per event, one lane on the panel.

Claude Code runs a command hook as a child process and reads the exit code as a
decision: exit 2 blocks the agent on the events that can block (``PreToolUse``,
``UserPromptSubmit``, ``Stop``, ``SubagentStop``). A status panel must never
change what the agent does, so ``hook()`` always returns 0 and writes nothing to
stdout; diagnostics go to stderr, and only when ``RGI_HOOK_DEBUG=1``.

The installation is eight command hooks in ``settings.json`` (see
``docs/claude-code.md``); ``rgi/hooks.py`` reads the event's JSON from stdin and
hands it here. The mapping:

====================  =====================================================
``SessionStart``      adopt or claim the lane, then report ``working``
``UserPromptSubmit``  report ``working``; a new prompt ends every old wait
``PreToolUse``        report ``working``
``PostToolUse``       report ``working``; resolve the wait for that tool
``Notification``      a permission/input prompt -> ``blocked`` by request id
``Stop``              report ``done``; stopping ends every open wait
``SubagentStop``      ``child_done`` for that subagent
``SessionEnd``        resolve every open wait, then release the lane
====================  =====================================================

Each event is a separate process, so open waits cannot live in the Reporter's
memory: the Notification that opens a wait and the PostToolUse that closes it
are different processes. The open request ids are therefore persisted in a small
JSON file under ``RGI_STATE_DIR`` - the directory the Reporter already uses for
its event-ordering stamps - and every event re-registers whatever is still
open. That keeps the promise from ``docs/integrations.md`` true across
processes: concurrent waits survive each other, the lane reads ``blocked``
while any is open, and resolving the last one returns to the intended state.

The lane's session is ``claude-code:<session_id>``; a ``/resume`` reuses the
same id and lands on the same lamp. Claude Code sends no timestamps, so arrival
order is the order, which is what the Reporter does without an ``at``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

from ..config import CONFIG_DIR
from ..report import Reporter, scrub

NAMESPACE = "claude-code"

# The events this integration installs. Anything else that reaches hook() is
# ignored: a user may have added hooks of their own, and that is not our error.
_EVENTS = frozenset({
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Notification", "Stop", "SubagentStop", "SessionEnd",
})

# Notification types that mean a human is being waited on: a tool permission
# prompt, or a request for input. Other notification types (auth_success,
# agent_completed, ...) are side effects, not waits, and are ignored.
_INPUT_NOTIFICATIONS = frozenset({
    "permission_prompt",
    "idle_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
})

# Older payloads carry no notification_type; the wait is recognisable by text.
_LEGACY_INPUT = re.compile(r"(?i)permission|waiting for your input|needs your input")

# "Claude needs your permission to use Bash" -> Bash. Payloads usually name the
# tool only in prose; the structured tool_name/tool_use_id fields are used when
# a runtime (or a test) supplies them.
_TOOL_IN_MESSAGE = re.compile(
    r"(?i)\b(?:to use|using|use)\s+([A-Za-z0-9][A-Za-z0-9_.:\-]*)")
_NOT_A_TOOL = frozenset({"the", "a", "an", "tool", "it", "this", "your"})

# Stable payload fields that identify the wait, best first. tool_use_id ties the
# wait to the exact tool call whose PostToolUse will resolve it.
_ID_FIELDS = ("tool_use_id", "request_id", "notification_id", "elicitation_id")

_quiet = False


def hook(payload: dict) -> int:
    """Report one Claude Code event to the panel. Always 0, never stdout.

    Claude Code reads stdout as a decision on some events and the exit code as
    one on others, so both are off-limits: this returns 0 even when the payload
    is unreadable, the panel is gone, or the event is not one we know.
    """
    try:
        if isinstance(payload, dict):
            _handle(payload)
    except Exception as exc:                # noqa: BLE001 - never break the agent
        _log_once(exc)
    return 0


# -- dispatch ---------------------------------------------------------------


def _handle(payload: dict) -> None:
    event = _text(payload.get("hook_event_name"))
    session_id = payload.get("session_id")
    if event not in _EVENTS or not isinstance(session_id, str):
        return
    session_id = session_id.strip()
    if not session_id:
        return

    reporter = Reporter(NAMESPACE, session_id, label=_label(payload))
    if event == "SessionStart":
        before = _state_load(session_id)
        _state_save(session_id, {})     # a started/resumed session has no old waits
        reporter.start()                # adopt the lane, or claim a free one
        _reconcile(reporter, before, {}, "working")
    elif event == "SessionEnd":
        _session_end(reporter, session_id)
    elif event == "UserPromptSubmit":
        _resolve_all(reporter, session_id, "working")
    elif event == "PreToolUse":
        _tool_event(reporter, session_id, payload, resolve=False)
    elif event == "PostToolUse":
        _tool_event(reporter, session_id, payload, resolve=True)
    elif event == "Stop":
        _resolve_all(reporter, session_id, "done")
    elif event == "SubagentStop":
        reporter.child_done(_child_id(payload))
    elif event == "Notification":
        _notification(reporter, session_id, payload)


def _resolve_all(reporter: Reporter, session_id: str, intent: str) -> None:
    """Every open wait is over: a new prompt, or the agent stopped."""
    before = _state_load(session_id)
    _state_save(session_id, {})
    _reconcile(reporter, before, {}, intent)


def _tool_event(reporter: Reporter, session_id: str, payload: dict,
                *, resolve: bool) -> None:
    pending = _state_load(session_id)
    resolved = _matches(pending, payload) if resolve else set()
    remaining = {rid: meta for rid, meta in pending.items() if rid not in resolved}
    _state_save(session_id, remaining)
    _reconcile(reporter, pending, remaining, "working")


def _session_end(reporter: Reporter, session_id: str) -> None:
    pending = _state_load(session_id)
    _state_save(session_id, {})
    # A fresh process has no lane handle; adopt the one on the panel (or claim
    # a free slot) so end() releases the right lamp. end() is a no-op if there
    # is nothing to release.
    reporter.start()
    if pending:
        reporter.resolve(None)           # never leave a blocked lane behind
    reporter.end()


def _notification(reporter: Reporter, session_id: str, payload: dict) -> None:
    raw_message = _text(payload.get("message"))
    title = _text(payload.get("title"))
    kind = _text(payload.get("notification_type")).lower()
    if kind not in _INPUT_NOTIFICATIONS:
        if kind:
            return                       # a known type that is not a human wait
        if not _LEGACY_INPUT.search(f"{title} {raw_message}"):
            return                       # older payload, but not an input wait
        kind = "input"

    message = scrub(raw_message or title or "waiting for input", 120)
    tool = _tool_for(payload, raw_message, title)
    pending = _state_load(session_id)
    rid = _request_id(payload, session_id, kind, tool, title, message)
    pending[rid] = {"action": kind, "tool": tool,
                    "tool_use_id": _text(payload.get("tool_use_id")),
                    "message": message}
    _state_save(session_id, pending)
    _reconcile(reporter, pending, pending, "working")


def _matches(pending: dict, payload: dict) -> set[str]:
    """Which open waits this completed tool resolves.

    Exact when the payload carries ids (``tool_use_id``) or a ``tool_name`` that
    a wait recorded; otherwise the tool name is looked for in the message, and
    finally one unnamed wait is resolved per completed tool, oldest first, so
    concurrent unnamed waits drain one event at a time instead of together.
    """
    tool_use_id = _text(payload.get("tool_use_id"))
    tool = _text(payload.get("tool_name"))
    if tool_use_id:
        exact = {rid for rid, meta in pending.items()
                 if _text(meta.get("tool_use_id")) == tool_use_id}
        if exact:
            return exact
    if tool:
        named = {rid for rid, meta in pending.items()
                 if _text(meta.get("tool")) == tool}
        if named:
            return named
        mentioned = {rid for rid, meta in pending.items()
                     if not _text(meta.get("tool"))
                     and _mentions(meta.get("message"), tool)}
        if mentioned:
            return mentioned
    unnamed = sorted(rid for rid, meta in pending.items()
                     if not _text(meta.get("tool"))
                     and not _text(meta.get("tool_use_id")))
    return set(unnamed[:1])


def _reconcile(reporter: Reporter, before: dict, remaining: dict,
               intent: str) -> None:
    """Replay what is still open, then report the event's own intent.

    A new process knows nothing about waits opened by earlier ones, so every
    event re-registers the still-open requests. The lane is blocked while any
    remains, exactly as a single Reporter behaves; the last resolution returns
    to the state the event asked for (``working`` or ``done``).
    """
    for rid in sorted(remaining):
        meta = remaining[rid]
        reporter.blocked(request=rid, action=_text(meta.get("action")),
                         message=_text(meta.get("message")))
    if remaining:
        return                           # a real wait outlives the event's intent
    if before:
        # clear the attention metadata an earlier process published
        reporter.info({"pending_requests": [], "blocked_on": None})
    if intent == "done":
        reporter.done()
    else:
        reporter.working()


# -- identity ---------------------------------------------------------------


def _label(payload: dict) -> str:
    """The project folder's name, so a lane reads like the work it holds."""
    cwd = _text(payload.get("cwd"))
    name = os.path.basename(cwd.rstrip("/\\")) if cwd else ""
    return scrub(name, 60) or "Claude Code"


def _child_id(payload: dict) -> str:
    """The subagent's own id when the payload has one, else a stable stand-in."""
    agent_id = _text(payload.get("agent_id"))
    if agent_id:
        return scrub(agent_id, 64)
    basis = "\x1f".join((_text(payload.get("session_id")),
                         _text(payload.get("agent_transcript_path")),
                         _text(payload.get("agent_type"))))
    return f"cc-sub-{hashlib.sha1(basis.encode('utf-8')).hexdigest()[:12]}"


def _request_id(payload: dict, session_id: str, kind: str, tool: str,
                title: str, message: str) -> str:
    """A stable id for a wait: from the payload if it has one, else synthetic.

    The synthetic id is a hash of what the notification says, so the same
    notification arriving twice is the same wait, while two different prompts
    are two ids that must resolve independently.
    """
    for field in _ID_FIELDS:
        value = _text(payload.get(field))
        if value:
            return scrub(value, 64)
    basis = "\x1f".join((NAMESPACE, session_id, kind, tool, title, message))
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]
    return f"cc-{kind}-{digest}"[:64]


def _tool_for(payload: dict, message: str, title: str) -> str:
    tool = _text(payload.get("tool_name"))
    if tool:
        return scrub(tool, 60)
    match = _TOOL_IN_MESSAGE.search(f"{message} {title}")
    if not match:
        return ""
    candidate = match.group(1).strip(".,;:`\"'")
    if candidate.lower() in _NOT_A_TOOL:
        return ""
    return scrub(candidate, 60)


def _mentions(message: object, tool: str) -> bool:
    if not isinstance(message, str) or not tool:
        return False
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(tool)}(?![A-Za-z0-9_])"
    return re.search(pattern, message) is not None


# -- the wait file ----------------------------------------------------------
#
# One small file per session, in the same directory the Reporter already uses:
#
#   ~/.config/rgi/integrations/claude-code-<sha1(session)[:16]>.json
#
#   {"pending": {"<request id>": {"action": ..., "tool": ...,
#                                 "tool_use_id": ..., "message": ...}}}
#
# Writes are atomic (tmp + replace) and every failure is swallowed: a missing
# file just means no waits, which is the safe default.


def _state_dir() -> str:
    return (os.environ.get("RGI_STATE_DIR")
            or os.path.join(CONFIG_DIR, "integrations"))


def _state_path(session_id: str) -> str:
    digest = hashlib.sha1(f"{NAMESPACE}:{session_id}".encode("utf-8")).hexdigest()
    return os.path.join(_state_dir(), f"claude-code-{digest[:16]}.json")


def _state_load(session_id: str) -> dict:
    try:
        with open(_state_path(session_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    pending = data.get("pending") if isinstance(data, dict) else None
    return pending if isinstance(pending, dict) else {}


def _state_save(session_id: str, pending: dict) -> None:
    path = _state_path(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"pending": pending}, fh, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


# -- small helpers ----------------------------------------------------------


def _text(value: object) -> str:
    """A payload field as a stripped string; structures read as empty."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _log_once(exc: BaseException) -> None:
    global _quiet
    if _quiet:
        return
    _quiet = True
    if os.environ.get("RGI_HOOK_DEBUG") or os.environ.get("RGI_DEBUG"):
        print(f"[rgi] claude-code hook: {exc}", file=sys.stderr)
