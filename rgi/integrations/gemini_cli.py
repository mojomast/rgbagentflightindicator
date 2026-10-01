"""Gemini CLI hooks: one process per event, one lane per session.

Gemini CLI runs a configured command for each lifecycle event, writes one JSON
object to its stdin, and reads stdout back as an optional decision. That makes
the process contract narrow, and this adapter keeps it to the letter:

* nothing is ever written to stdout - the CLI parses stdout as a decision, and
  a stray line can allow or block an action;
* the exit code is always 0 - a non-zero code is a block or a warning there;
* every failure is swallowed and only mentioned on stderr, and only when
  ``RGI_HOOK_DEBUG=1`` (or ``RGI_DEBUG=1``) is set.

The event map, verified against the released Gemini CLI 0.62.0 (see
``docs/gemini-cli.md`` for sources and limitations):

    SessionStart          resolve a stale wait, idle
    BeforeAgent           resolve a stale wait, working
    BeforeTool/AfterTool  resolve the approval wait, working
    Notification          blocked while a ToolPermission is pending
    PreCompress           resolve a stale wait, working
    AfterAgent            resolve the wait (a denial ends the turn), done
    SessionEnd            resolve everything, release the lane

A hook process is short-lived: the state lives on the panel, not in this
module. The lane is ``gemini-cli:<session_id>``, so a resumed session continues
on the same lane and a new prompt clears a completion left by the previous turn.

Released Gemini CLI does not put a linking id in a ToolPermission notification,
so a pending wait is resolved by the next event of that session - approval
(tool activity), denial (the turn's AfterAgent), cancellation (a new
BeforeAgent), compression, a resume (SessionStart), or shutdown (SessionEnd).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

from ..report import Reporter

NAMESPACE = "gemini-cli"

# Events this adapter maps; anything else Gemini sends is left alone.
_EVENTS = frozenset({
    "SessionStart", "BeforeAgent", "BeforeTool", "AfterTool",
    "Notification", "PreCompress", "AfterAgent", "SessionEnd",
})

# Places an approval id could live in a Notification payload. The released
# 0.62.0 payload has none, so the synthetic digest below usually wins.
_ID_KEYS = (
    "request_id", "requestId", "tool_call_id", "toolCallId", "call_id",
    "callId", "approval_id", "confirmation_id", "id",
)

# Trimming more than microsecond precision keeps fromisoformat happy.
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _debug(message: str) -> None:
    """Diagnostics, opt-in and stderr-only: stdout is a decision channel."""
    if os.environ.get("RGI_HOOK_DEBUG") or os.environ.get("RGI_DEBUG"):
        print(f"[rgi] {NAMESPACE}: {message}", file=sys.stderr)


def _as_text(value: object) -> str:
    """A stripped string for a field, or empty text for anything else."""
    return value.strip() if isinstance(value, str) else ""


def _session_id(payload: dict) -> str:
    """The session id from the payload or the CLI's own environment.

    An id that cannot be established is never invented: a lane with a made-up
    name would be a lamp that nobody can ever release, and a guessed name
    (the transcript file, for instance) collides between sessions.
    """
    sid = payload.get("session_id")
    if sid is None:
        sid = os.environ.get("GEMINI_SESSION_ID")
    if sid is not None and str(sid).strip():
        return str(sid).strip()
    return ""


def _epoch(timestamp: object) -> float | None:
    """Gemini sends ISO 8601; anything unclear means 'arrival order'."""
    text = _as_text(timestamp)
    if not text:
        return None
    try:
        stamp = _FRACTION.sub(r"\1", text)
        stamp = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _request_id(payload: dict) -> str:
    """A stable id for one pending approval.

    The payload's own id wins when a version of the harness sends one. Released
    Gemini CLI (0.62.0) sends only the notification message and serializable
    details, so an identical request hashes to the same synthetic id in every
    short-lived hook process.
    """
    details = payload.get("details")
    details = details if isinstance(details, dict) else {}
    for source in (payload, details):
        for key in _ID_KEYS:
            value = source.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    material = json.dumps(
        {
            "type": payload.get("notification_type"),
            "message": payload.get("message"),
            "details": details,
        },
        sort_keys=True,
        default=str,
    )
    return f"gemini-{hashlib.sha1(material.encode('utf-8')).hexdigest()[:16]}"


def _label(payload: dict) -> str:
    """A short panel label; the project directory is more useful than a UUID."""
    cwd = _as_text(payload.get("cwd")).replace("\\", "/")
    name = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    return f"gemini {name}" if name else NAMESPACE


def _notification(reporter: Reporter, payload: dict, at: float | None) -> None:
    """A ToolPermission alert: the CLI is waiting for a human decision."""
    kind = _as_text(payload.get("notification_type")).lower()
    if kind != "toolpermission":
        _debug(f"ignoring notification {kind or '<missing>'!r}")
        return
    details = payload.get("details")
    details = details if isinstance(details, dict) else {}
    action = (_as_text(details.get("type")) or _as_text(details.get("toolName"))
              or "tool")
    message = (_as_text(payload.get("message"))
               or _as_text(details.get("title"))
               or "waiting for approval")
    request = _request_id(payload)
    reporter.blocked(request=request, action=action, message=message, at=at)
    _debug(f"wait {request} opened by {action}")


def _apply(reporter: Reporter, event: str, payload: dict,
           at: float | None) -> None:
    """One reported state per event; never a decision, never an exception."""
    if event == "Notification":
        _notification(reporter, payload, at)
        return
    # Every other mapped event proves the approval flow is over: a tool ran, a
    # turn ended, or the session is going away. Resolving first clears a wait
    # left by a denial or a cancellation, then the event's own state lands.
    # BeforeTool/AfterTool also clear a premature `done` with `working`.
    reporter.resolve(at=at)
    if event == "SessionStart":
        reporter.idle(at=at)
    elif event in ("BeforeAgent", "BeforeTool", "AfterTool", "PreCompress"):
        reporter.working(at=at)
    elif event == "AfterAgent":
        reporter.done(at=at)
    elif event == "SessionEnd":
        reporter.end()


def _handle(payload: dict) -> None:
    event = _as_text(payload.get("hook_event_name"))
    if event not in _EVENTS:
        _debug(f"ignoring event {event or '<missing>'!r}")
        return
    session = _session_id(payload)
    if not session:
        _debug(f"no session id in {event}; nothing to report")
        return
    _apply(Reporter(NAMESPACE, session, label=_label(payload)), event, payload,
           _epoch(payload.get("timestamp")))


def hook(payload: dict) -> int:
    """Report one event for ``rgi hook gemini-cli``.

    Always returns 0: the exit code is a decision channel and a status
    indicator must never block, warn about, or otherwise change an agent run.
    """
    try:
        if isinstance(payload, dict):
            _handle(payload)
    except Exception as exc:  # noqa: BLE001 - a broken panel must not break work
        _debug(f"swallowed: {exc!r}")
    return 0
