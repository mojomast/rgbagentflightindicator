"""Generic ingest: turn native hook and webhook payloads into canonical actions.

The panel has one set of lane semantics; every harness spells the same few
facts differently. This module is the only place that translation lives: a
caller hands it a ``source`` name, the raw JSON body and (optionally) the
request headers, and gets back zero or more :class:`Action` objects that the
daemon applies through the vocabulary ``rgi/report.py`` already uses.

Design rules, deliberately kept:

* **Pure.** No threads, no sockets, no files, no daemon imports. The only
  dependency is :func:`rgi.report.canonical_state`, the single source of truth
  for state names.
* **Data, not code sprawl.** Each harness contributes a small mapping table;
  one engine walks it.
* **Never raise.** An unknown source, an unknown event, a malformed payload or
  a missing session id yields ``[]``. A broken payload must never break ingest.
* **No content, ever.** Only ids, names, counts and statuses are read out of a
  payload. Prompts, transcripts, tool arguments and tool results are never
  touched, so there is nothing to scrub here; the daemon and the reporter scrub
  whatever they publish downstream.

Two families of source share one shape. Hook sources (Claude Code, Codex,
Cursor, Copilot, Continue, Gemini CLI, Devin/Windsurf, Amazon Q) run through
one event engine keyed on the native event name. The CI providers (GitHub,
GitLab, Jenkins) run through one status engine keyed on the native status
word. ``generic`` accepts the panel's own session payload (and the
CloudEvents-shaped envelope from the ingestion research).

Actions
-------

``op`` is one of:

=============  =========================================================
``begin``      claim or adopt the lane; carries ``agent``, ``label``,
               ``slot`` and ``meta`` (``host``, ``ident``)
``state``      a transition; ``state`` is canonical (from report.py)
``info``       detail to merge; ``info`` is the payload
``end``        release the lane
``attention``  a human wait opens or closes. ``meta["open"]`` says which;
               ``request`` is the wait id. On a close, ``request=None``
               means "the wait this event resolves", ``meta["all"]`` means
               every wait, and ``meta["match"]`` names a tool to match
``activity``   current-action detail; ``info["running"]`` is a list of
               ``{"tool": ..., "id": ...}``
``snapshot``   full lane state plus ``meta["lease_s"]``
=============  =========================================================

``meta`` keys this module writes: ``event`` (native event name), ``at``
(epoch seconds when the payload carried a timestamp), ``delivery`` (webhook
delivery id; daemon-side dedup key), ``host``/``ident`` (lane identity),
``open`` (attention), ``match`` (tool fallback for a wait), ``all`` (a close
covers every wait) and ``lease_s`` (snapshot). See ``docs/ingest.md`` for the
full per-source mapping tables and how a daemon applies an action.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .report import canonical_state

__all__ = ["Action", "sources", "normalize"]


# ---------------------------------------------------------------------------
# the action
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One canonical thing to do to a lane. See the module docstring."""

    op: str
    session: str
    agent: str = ""
    label: str | None = None
    state: str | None = None
    info: dict = field(default_factory=dict)
    request: str | None = None
    slot: int | None = None
    meta: dict = field(default_factory=dict)


_OPS = ("begin", "state", "info", "end", "attention", "activity", "snapshot")

#: CloudEvents-shaped ``type`` values from the universal schema (research
#: doc section 7); attention has an open/close pair.
_TYPE_OPS = {
    "dev.rgi.session.begin": "begin",
    "dev.rgi.session.state": "state",
    "dev.rgi.session.info": "info",
    "dev.rgi.session.end": "end",
    "dev.rgi.session.attention": "attention",
    "dev.rgi.session.activity": "activity",
    "dev.rgi.session.snapshot": "snapshot",
}
_ATTENTION_TYPES = {
    "dev.rgi.attention.open": True,
    "dev.rgi.attention.close": False,
}


# ---------------------------------------------------------------------------
# small, boring helpers (all total: garbage in, empty out)
# ---------------------------------------------------------------------------

_FRACTION = re.compile(r"(\.\d{6})\d+")


def _text(value: object, limit: int = 200) -> str:
    """A stripped string for a payload field; structures read as empty."""
    if isinstance(value, str):
        out = value.strip()
    elif isinstance(value, bool) or value is None:
        return ""
    elif isinstance(value, (int, float)):
        out = str(value)
    else:
        return ""
    return out[:limit]


def _field(payload: Mapping, names: tuple) -> Any:
    """The first present, non-None raw value under any of ``names``."""
    if not isinstance(payload, Mapping):
        return None
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def _first(payload: Mapping, names: tuple, limit: int = 200) -> str:
    return _text(_field(payload, names), limit)


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _digits(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _compact(fields: dict) -> dict:
    """Drop empty values so an info payload never carries noise."""
    return {key: value for key, value in fields.items()
            if value not in ("", None, {}, [])}


def _header(headers: Mapping[str, str] | None, *names: str) -> str:
    if not headers:
        return ""
    wanted = {name.lower() for name in names}
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() in wanted:
            return _text(value, 300)
    return ""


def _word(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _canonical(value: object) -> str | None:
    """``report.canonical_state`` plus the one missing alias: cancel -> error.

    ``report.py`` maps ``cancelled``/``canceled`` to ``error``; harness
    vocabularies (A2A, MCP) also use the bare ``cancel``, which means the same
    thing, so it is folded in here before asking the shared mapper.
    """
    word = str(value or "").strip().lower()
    if word in ("cancel", "cancels"):
        word = "cancelled"
    return canonical_state(word)


def _epoch(value: object) -> float | None:
    """Epoch seconds from a seconds/ms number or an ISO 8601 string."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:                      # milliseconds
            number /= 1000.0
        return round(number, 3) if number > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return _epoch(float(text))
        except ValueError:
            pass
        try:
            stamp = _FRACTION.sub(r"\1", text)
            if stamp.endswith("Z"):
                stamp = stamp[:-1] + "+00:00"
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return round(parsed.timestamp(), 3)
    return None


def _cwd(payload: Mapping) -> str:
    value = _first(payload, ("cwd", "working_directory", "workspace"), 300)
    return value.replace("\\", "/").rstrip("/")


def _join(namespace: str, native: str) -> str:
    """A lane key is ``<namespace>:<session>``; an already-keyed id is kept."""
    native = str(native or "").strip()
    if not native:
        return ""
    if ":" in native:
        return native
    return f"{namespace}:{native}"


def _derived_session(namespace: str, payload: Mapping,
                     headers: Mapping[str, str] | None,
                     pid_fields: tuple) -> str:
    """A stable stand-in id for harnesses that never send one.

    The basis is the working directory plus the agent process id (``gpid`` or
    ``pid``, then process-id headers). Same pair, same lane; a different
    process in the same directory is a different lane. With no pid anywhere
    the directory is all there is, and two sessions in one directory share a
    lane - documented in ``docs/ingest.md`` rather than guessed around.
    """
    pid = _first(payload, pid_fields, 40) or _header(
        headers, "X-RGI-Pid", "X-RGI-Gpid", "X-Process-Id", "X-Parent-Pid")
    basis = "\x1f".join((namespace, _cwd(payload), pid))
    digest = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{namespace}:auto-{digest}"


def _action(op: str, session: str, **fields) -> Action:
    fields.setdefault("agent", "")
    return Action(op=op, session=session, **fields)


# ---------------------------------------------------------------------------
# the hook engine: one table per harness, one walker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    """What one native event does to a lane."""

    op: str = ""                           # "begin" | "state" | "end" | ""
    state: str | None = None
    state_from: tuple[str, Mapping[str, str]] | None = None
    close: str = ""                        # "" | "all" | "resolve"
    attention: str = ""                    # "" | "permission" | "notification" | "elicitation"
    activity: bool = False
    child: str = ""                        # "" | "start" | "stop"


#: The Claude Code event surface, which Codex, Continue and Cursor's
#: Claude-compatible config all clone. Per-harness profiles override entries.
_HOOK_RULES: dict[str, _Rule] = {
    "SessionStart": _Rule(op="begin", state="working", close="all"),
    "UserPromptSubmit": _Rule(op="state", state="working", close="all"),
    "PreToolUse": _Rule(op="state", state="working", activity=True),
    "PostToolUse": _Rule(op="state", state="working", activity=True,
                         close="resolve"),
    "PostToolUseFailure": _Rule(op="state", state="working", activity=True,
                                close="resolve"),
    "PermissionRequest": _Rule(attention="permission"),
    "Notification": _Rule(attention="notification"),
    "Elicitation": _Rule(attention="elicitation"),
    "Stop": _Rule(op="state", state="done", close="all"),
    "StopFailure": _Rule(op="state", state="error", close="all"),
    "Interrupt": _Rule(op="state", state="error", close="all"),
    "SubagentStart": _Rule(child="start"),
    "SubagentStop": _Rule(child="stop"),
    "PreCompact": _Rule(op="state", state="working"),
    "PostCompact": _Rule(op="state", state="working"),
    "SessionEnd": _Rule(op="end", close="all"),
}

#: Native spellings that do not collapse onto a rule name. Snake and case
#: variants (pre_tool_use, preToolUse, PreToolUse) collapse on their own.
_HOOK_ALIASES: dict[str, str] = {
    "agentstop": "Stop",                       # Copilot camelCase
    "userpromptsubmitted": "UserPromptSubmit",  # Copilot camelCase
    "userprompttransformed": "UserPromptSubmit",
    "sessionstarted": "SessionStart",
    "sessionended": "SessionEnd",
}

#: Notification types that are a human wait, and the tool action they carry.
_NOTIFICATION_ACTIONS = {
    "permissionprompt": "permission",
    "permissionrequest": "permission",
    "toolpermission": "permission",        # Gemini CLI
    "idleprompt": "input",
    "agentneedsinput": "input",
    "elicitationdialog": "elicitation",
    "elicitationurldialog": "elicitation",
}

#: Where a completed tool call names the wait it resolves, best first.
_CALL_ID_FIELDS = ("tool_use_id", "toolUseId", "tool_call_id", "toolCallId",
                   "call_id", "callId")


@dataclass(frozen=True)
class _HookProfile:
    """One harness's dialect of the shared hook schema."""

    namespace: str
    rules: Mapping[str, _Rule]
    session_fields: tuple
    request_fields: tuple
    tool_fields: tuple = ("tool_name", "toolName")
    event_fields: tuple = ("hook_event_name", "hookEventName", "event",
                           "event_name", "eventName")
    call_id_fields: tuple = _CALL_ID_FIELDS
    agent_id_fields: tuple = ("agent_id", "agentId", "subagent_id",
                              "subagentId")
    label_fallback: str = ""
    details: bool = False        # also look inside a top-level "details"
    derive: bool = False         # derive a session id when none is present
    pid_fields: tuple = ("gpid", "pid", "ppid")
    infer: Callable[[dict], str] | None = None


def _event_key(value: object) -> str:
    return (str(value or "").strip().lower()
            .replace("_", "").replace("-", "").replace(" ", ""))


def _build_index(rules: Mapping[str, _Rule]) -> dict[str, str]:
    index: dict[str, str] = {}
    for name in rules:
        index[_event_key(name)] = name
    for alias, name in _HOOK_ALIASES.items():
        if name in rules:
            index.setdefault(_event_key(alias), name)
    return index


def _hook_event(profile: _HookProfile, payload: dict,
                headers: Mapping[str, str] | None) -> str:
    raw = _first(payload, profile.event_fields, 80)
    if not raw:
        raw = _header(headers, "X-RGI-Event", "X-Hook-Event", "X-Event-Name")
    name = _PROFILE_INDEX[profile.namespace].get(_event_key(raw), "")
    if not name and profile.infer is not None:
        name = profile.infer(payload)
    return name


def _hook_session(profile: _HookProfile, payload: dict,
                  headers: Mapping[str, str] | None) -> str:
    native = _first(payload, profile.session_fields, 160)
    if native:
        return _join(profile.namespace, native)
    if profile.derive:
        return _derived_session(profile.namespace, payload, headers,
                                profile.pid_fields)
    return ""


def _hook_label(profile: _HookProfile, payload: dict) -> str:
    name = _cwd(payload)
    folder = name.rsplit("/", 1)[-1] if name else ""
    if folder:
        return folder
    return profile.label_fallback or profile.namespace


def _hook_meta(payload: dict, event: str) -> dict:
    meta: dict = {"event": event}
    at = _epoch(_field(payload, ("timestamp", "time", "ts", "at")))
    if at is not None:
        meta["at"] = at
    return meta


def _nested(profile: _HookProfile, payload: dict) -> dict:
    return _dict(payload.get("details")) if profile.details else {}


def _tool_name(profile: _HookProfile, payload: dict) -> str:
    tool = _first(payload, profile.tool_fields, 60)
    if not tool:
        tool = _first(_nested(profile, payload), profile.tool_fields, 60)
    return tool


def _request_field(profile: _HookProfile, payload: dict) -> str:
    found = _first(payload, profile.request_fields, 64)
    if not found:
        found = _first(_nested(profile, payload), profile.request_fields, 64)
    return found


def _synthetic_request(profile: _HookProfile, session: str, payload: dict,
                       meta: dict, kind: str, tool: str) -> str:
    """A stable wait id for harnesses that do not send one.

    The hash covers the namespace, lane, kind, tool and the event's own
    timestamp when it has one - never any message text. Two identical prompts
    are therefore the same wait, and a retried delivery stays the same wait.
    """
    basis = "\x1f".join((profile.namespace, session, kind, tool,
                         str(meta.get("at") or "")))
    digest = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{profile.namespace}-{kind or 'wait'}-{digest}"


def _notification_action(profile: _HookProfile, payload: dict) -> str:
    kind = _event_key(_first(payload, ("notification_type", "notificationType")))
    return _NOTIFICATION_ACTIONS.get(kind, "")


def _hook_closes(profile: _HookProfile, rule: _Rule, session: str, agent: str,
                 payload: dict, meta: dict) -> list[Action]:
    if rule.close == "all":
        return [_action("attention", session, agent=agent, request=None,
                        meta={**meta, "open": False, "all": True})]
    found = _request_field(profile, payload)
    if found:
        return [_action("attention", session, agent=agent, request=found,
                        meta={**meta, "open": False})]
    # No id in the payload: ask the daemon to resolve the wait this event
    # refers to, preferring a match on the completed tool's name.
    close_meta = {**meta, "open": False}
    tool = _tool_name(profile, payload)
    if tool:
        close_meta["match"] = tool
    return [_action("attention", session, agent=agent, request=None,
                    meta=close_meta)]


def _hook_attention(profile: _HookProfile, rule: _Rule, session: str,
                    agent: str, payload: dict, meta: dict) -> Action | None:
    if rule.attention == "notification":
        kind = _notification_action(profile, payload)
        if not kind:
            return None                        # a notification that is not a wait
    else:
        kind = rule.attention or "attention"
    tool = _tool_name(profile, payload)
    request = (_request_field(profile, payload)
               or _synthetic_request(profile, session, payload, meta, kind, tool))
    info: dict = {"action": kind}
    if tool:
        info["tool"] = tool
    return _action("attention", session, agent=agent, state="blocked", info=info,
                   request=request, meta={**meta, "open": True})


def _hook_child(profile: _HookProfile, rule: _Rule, session: str, agent: str,
                payload: dict, meta: dict) -> Action:
    child = _first(payload, profile.agent_id_fields, 64)
    kind = _first(payload, ("agent_type", "agentType", "subagent_type",
                            "subagentType"), 60)
    if not child:
        basis = "\x1f".join((profile.namespace, session, rule.child, kind))
        digest = hashlib.sha1(basis.encode("utf-8", "replace")).hexdigest()[:12]
        child = f"{profile.namespace}-sub-{digest}"
    return _action("info", session, agent=agent, meta=meta,
                   info={"children": [{"id": child, "label": kind or child,
                                       "state": "working" if rule.child == "start"
                                       else "done"}]})


def _hook_activity(profile: _HookProfile, session: str, agent: str,
                   payload: dict, meta: dict) -> Action | None:
    tool = _tool_name(profile, payload)
    if not tool:
        return None
    entry: dict = {"tool": tool}
    call_id = _first(payload, profile.call_id_fields, 64)
    if not call_id:
        call_id = _first(_nested(profile, payload), profile.call_id_fields, 64)
    if call_id:
        entry["id"] = call_id
    return _action("activity", session, agent=agent, info={"running": [entry]},
                   meta=meta)


def _rule_state(rule: _Rule, payload: dict) -> str | None:
    if rule.state_from is not None:
        name, mapping = rule.state_from
        native = _word(_field(payload, (name,)))
        return _canonical(mapping.get(native, rule.state))
    return _canonical(rule.state)


def _run_hooks(profile: _HookProfile, payload: dict,
               headers: Mapping[str, str] | None) -> list[Action]:
    event = _hook_event(profile, payload, headers)
    rule = profile.rules.get(event) if event else None
    if rule is None:
        return []                              # a hook we do not map: not an error
    session = _hook_session(profile, payload, headers)
    if not session:
        return []                              # no identity, no lane
    agent = profile.namespace
    meta = _hook_meta(payload, event)
    actions: list[Action] = []
    if rule.op == "begin":
        actions.append(_action("begin", session, agent=agent,
                               label=_hook_label(profile, payload),
                               slot=_digits(_field(payload, ("slot",))),
                               meta=meta))
    if rule.close:
        actions.extend(_hook_closes(profile, rule, session, agent, payload, meta))
    if rule.attention:
        wait = _hook_attention(profile, rule, session, agent, payload, meta)
        if wait is not None:
            actions.append(wait)
    if rule.child:
        actions.append(_hook_child(profile, rule, session, agent, payload, meta))
    if rule.op in ("begin", "state"):
        state = _rule_state(rule, payload)
        if state:
            actions.append(_action("state", session, agent=agent, state=state,
                                   meta=meta))
    if rule.activity:
        activity = _hook_activity(profile, session, agent, payload, meta)
        if activity is not None:
            actions.append(activity)
    if rule.op == "end":
        actions.append(_action("end", session, agent=agent, meta=meta))
    return actions


def _copilot_infer(payload: dict) -> str:
    """Copilot's camelCase payloads sometimes omit the event name entirely.

    The fields are distinctive enough to place the event: a tool result means
    ``postToolUse``, a tool error means ``postToolUseFailure``, and so on.
    """
    keys = {str(key).lower() for key in payload}
    tool = "toolname" in keys or "tool_name" in keys
    if "notification_type" in keys or "notificationtype" in keys:
        return "Notification"
    if tool and ("toolresult" in keys or "tool_result" in keys):
        return "PostToolUse"
    if tool and ("error" in keys or "failure_type" in keys or "failuretype" in keys):
        return "PostToolUseFailure"
    if tool and ("permission_kind" in keys or "permissionkind" in keys):
        return "PermissionRequest"
    if tool:
        return "PreToolUse"
    if "prompt" in keys:
        return "UserPromptSubmit"
    if "stopreason" in keys or "stop_reason" in keys:
        return "Stop"
    if "agentname" in keys or "agent_name" in keys:
        if "response" in keys or "last_assistant_message" in keys:
            return "SubagentStop"
        return "SubagentStart"
    if "error" in keys:
        return "ErrorOccurred"
    if "trigger" in keys and ("custominstructions" in keys
                              or "custom_instructions" in keys
                              or "context_usage_percent" in keys):
        return "PreCompact"
    if "reason" in keys and "cwd" in keys:
        return "SessionEnd"
    if "source" in keys and "cwd" in keys:
        return "SessionStart"
    return ""


_CURSOR_RULES: dict[str, _Rule] = {
    **_HOOK_RULES,
    "BeforeSubmitPrompt": _Rule(op="state", state="working", close="all"),
    "BeforeShellExecution": _Rule(op="state", state="working"),
    "AfterShellExecution": _Rule(op="state", state="working"),
    "BeforeMCPExecution": _Rule(op="state", state="working"),
    "AfterMCPExecution": _Rule(op="state", state="working"),
    "BeforeReadFile": _Rule(op="state", state="working"),
    "AfterFileEdit": _Rule(op="state", state="working"),
    "AfterAgentResponse": _Rule(op="state", state="working"),
    "AfterAgentThought": _Rule(op="state", state="working"),
    # Cursor's stop hook says how the turn ended; `aborted` is a cancel and
    # lands on `error`, exactly as report.py maps cancelled/canceled.
    "Stop": _Rule(op="state", state="done", close="all",
                  state_from=("status", {"completed": "done",
                                         "aborted": "error",
                                         "error": "error"})),
}

_GEMINI_RULES: dict[str, _Rule] = {
    "SessionStart": _Rule(op="begin", state="idle", close="all"),
    "BeforeAgent": _Rule(op="state", state="working", close="all"),
    "BeforeTool": _Rule(op="state", state="working", activity=True, close="all"),
    "AfterTool": _Rule(op="state", state="working", activity=True, close="all"),
    "Notification": _Rule(attention="notification"),
    "PreCompress": _Rule(op="state", state="working"),
    "AfterAgent": _Rule(op="state", state="done", close="all"),
    "SessionEnd": _Rule(op="end", close="all"),
}

_DEVIN_RULES: dict[str, _Rule] = {
    "PreReadCode": _Rule(op="state", state="working"),
    "PostReadCode": _Rule(op="state", state="working"),
    "PreWriteCode": _Rule(op="state", state="working"),
    "PostWriteCode": _Rule(op="state", state="working"),
    "PreRunCommand": _Rule(op="state", state="working"),
    "PostRunCommand": _Rule(op="state", state="working"),
    "PreMcpToolUse": _Rule(op="state", state="working"),
    "PostMcpToolUse": _Rule(op="state", state="working"),
    "PreUserPrompt": _Rule(op="state", state="working", close="all"),
    "PostCascadeResponse": _Rule(op="state", state="done", close="all"),
    "PostCascadeResponseWithTranscript": _Rule(op="state", state="done",
                                               close="all"),
    "PostSetupWorktree": _Rule(op="state", state="working"),
}

_AMAZON_Q_RULES: dict[str, _Rule] = {
    **_HOOK_RULES,
    "AgentSpawn": _Rule(op="begin", state="working", close="all"),
}

_PROFILES: dict[str, _HookProfile] = {
    "claude-code": _HookProfile(
        namespace="claude-code",
        rules=_HOOK_RULES,
        session_fields=("session_id", "sessionId", "sessionID"),
        request_fields=("tool_use_id", "request_id", "notification_id",
                        "elicitation_id"),
        label_fallback="Claude Code",
    ),
    "codex": _HookProfile(
        namespace="codex",
        rules=_HOOK_RULES,
        session_fields=("session_id", "sessionId", "sessionID", "thread_id",
                        "threadId"),
        request_fields=("tool_use_id", "request_id", "call_id", "id"),
        agent_id_fields=("agent_id", "agentId"),
        label_fallback="Codex",
    ),
    "continue": _HookProfile(
        namespace="continue",
        rules=_HOOK_RULES,
        session_fields=("session_id", "sessionId", "sessionID"),
        request_fields=("tool_use_id", "request_id", "notification_id"),
        label_fallback="Continue",
    ),
    "cursor": _HookProfile(
        namespace="cursor",
        rules=_CURSOR_RULES,
        session_fields=("session_id", "sessionId", "conversation_id",
                        "conversationId"),
        request_fields=_CALL_ID_FIELDS,
        agent_id_fields=("agent_id", "agentId", "subagent_id", "subagentId"),
        label_fallback="Cursor",
    ),
    "copilot": _HookProfile(
        namespace="copilot",
        rules={**_HOOK_RULES,
               "ErrorOccurred": _Rule(op="state", state="error", close="all")},
        session_fields=("session_id", "sessionId", "sessionID",
                        "conversation_id", "conversationId", "thread_id"),
        request_fields=("tool_use_id", "request_id", "call_id", "id"),
        agent_id_fields=("agent_id", "agentId", "agentName", "agent_name"),
        label_fallback="Copilot",
        derive=True,
        infer=_copilot_infer,
    ),
    "gemini-cli": _HookProfile(
        namespace="gemini-cli",
        rules=_GEMINI_RULES,
        session_fields=("session_id", "sessionId", "sessionID"),
        request_fields=("request_id", "requestId", "tool_call_id",
                        "toolCallId", "call_id", "callId", "approval_id",
                        "confirmation_id", "id"),
        tool_fields=("tool_name", "toolName", "title"),
        label_fallback="gemini-cli",
        details=True,
    ),
    "devin": _HookProfile(
        namespace="devin",
        rules=_DEVIN_RULES,
        session_fields=("trajectory_id", "trajectoryId", "session_id",
                        "sessionId"),
        request_fields=("tool_use_id", "request_id", "call_id", "id"),
        event_fields=("agent_action_name", "hook_event_name", "hookEventName"),
        label_fallback="Devin",
    ),
    "amazon-q": _HookProfile(
        namespace="amazon-q",
        rules=_AMAZON_Q_RULES,
        session_fields=("session_id", "sessionId", "sessionID"),
        request_fields=("tool_use_id", "request_id", "call_id", "id"),
        tool_fields=("tool_name", "toolName"),
        label_fallback="Amazon Q",
        derive=True,
    ),
}

_PROFILE_INDEX: dict[str, dict[str, str]] = {
    name: _build_index(profile.rules) for name, profile in _PROFILES.items()
}


# ---------------------------------------------------------------------------
# the generic source: rgi's own session payload and the CloudEvents envelope
# ---------------------------------------------------------------------------


def _generic(payload: dict, headers: Mapping[str, str] | None) -> list[Action]:
    outer = _dict(payload.get("data")) or payload
    body = outer
    snapshot = _dict(outer.get("snapshot")) or _dict(payload.get("snapshot"))
    op = _text(payload.get("op"), 24).lower()
    attention_open: bool | None = None
    if not op:
        raw_type = _text(payload.get("type"), 80).lower()
        op = _TYPE_OPS.get(raw_type, "")
        if raw_type in _ATTENTION_TYPES:
            op, attention_open = "attention", _ATTENTION_TYPES[raw_type]
    if snapshot:
        op = "snapshot"
        body = snapshot

    agent = (_first(body, ("agent", "harness", "namespace"), 40)
             or _first(outer, ("agent", "harness", "namespace"), 40)
             or _first(payload, ("rgiagent",), 40) or "generic")
    native = (_first(payload, ("subject",), 160)
              or _first(body, ("sessionID", "session_id", "sessionId",
                               "session"), 160)
              or _first(outer, ("sessionID", "session_id", "sessionId",
                                "session"), 160))
    if agent == "generic" and ":" in native:
        # a CloudEvents subject is already the canonical lane key; its
        # namespace is the harness, which is more useful than "generic".
        agent = native.split(":", 1)[0]

    # The documented panel payload: {agent, sessionID, label, state, host,
    # ident, info}. It claims the lane, reports the state, then the detail.
    if not op:
        if not native:
            return []
        session = _join(agent, native)
        meta = _generic_meta(payload, body)
        label = _first(body, ("label",), 80) or None
        state = _canonical(_field(body, ("state", "status")))
        detail = _dict(body.get("info"))
        actions = [_action("begin", session, agent=agent, label=label,
                           slot=_digits(_field(body, ("slot",))), meta=meta)]
        if state:
            actions.append(_action("state", session, agent=agent, state=state,
                                   meta=meta))
        if detail:
            actions.append(_action("info", session, agent=agent, info=detail,
                                   meta=meta))
        return actions

    if op not in _OPS:
        return []
    if not native:
        return []
    session = _join(agent, native)
    meta = _generic_meta(payload, body)
    info = _dict(body.get("info")) or _dict(payload.get("info"))
    request = (_first(body, ("request", "request_id", "requestId"), 64)
               or _first(payload, ("request",), 64) or None)
    state = _canonical(_field(body, ("state", "status")))
    action = _action(op, session, agent=agent,
                     label=_first(body, ("label",), 80) or None,
                     state=state, info=info, request=request,
                     slot=_digits(_field(body, ("slot",))), meta=meta)
    if op == "attention":
        if attention_open is None:
            attention_open = bool(body.get("open", True))
        action = replace(action, state="blocked" if attention_open else None,
                         meta={**meta, "open": attention_open})
    return [action]


def _generic_meta(payload: dict, body: dict) -> dict:
    meta: dict = {}
    ident = _first(body, ("ident", "identifier"), 60)
    host = _first(body, ("host", "machine"), 120)
    if ident:
        meta["ident"] = ident
    if host:
        meta["host"] = host
    when = _field(payload, ("time", "timestamp"))
    if when is None:
        when = _field(body, ("timestamp", "at"))
    at = _epoch(when)
    if at is not None:
        meta["at"] = at
    delivery = _first(payload, ("id",), 120)
    if delivery:
        meta["delivery"] = delivery
    lease = _field(body, ("lease_s", "leaseS", "lease"))
    if isinstance(lease, (int, float)) and not isinstance(lease, bool):
        meta["lease_s"] = float(lease)
    return meta


# ---------------------------------------------------------------------------
# CI providers: one status vocabulary, three payload dialects
# ---------------------------------------------------------------------------

_CI_END = frozenset({"cancelled", "canceled", "skipped", "aborted",
                     "not_built", "notbuilt"})

_CI_STATE_WORDS = {
    "queued": "idle",
    "pending": "idle",
    "created": "idle",
    "requested": "idle",
    "scheduled": "idle",
    "waiting": "blocked",
    "waiting_for_resource": "blocked",
    "manual": "blocked",
    "action_required": "blocked",
    "in_progress": "working",
    "running": "working",
    "started": "working",
    "preparing": "working",
    "building": "working",
    "success": "done",
    "succeeded": "done",
    "passed": "done",
    "neutral": "done",
    "failure": "error",
    "failed": "error",
    "error": "error",
    "timed_out": "error",
    "unstable": "error",
}


def _ci_verdict(word: object) -> tuple[bool, str | None]:
    """``(ends_the_lane, canonical_state)`` for a native CI status word."""
    native = _word(word)
    if not native:
        return False, None
    if native in _CI_END:
        return True, None
    target = _CI_STATE_WORDS.get(native, native)
    return False, _canonical(target)


def _ci_session(repo: str, pipeline: str, branch: str) -> str:
    return "ci:" + ":".join((repo or "unknown", pipeline or "run",
                             branch or "unknown"))


def _ci_actions(provider: str, session: str, label: str,
                state: str | None, ended: bool, info: dict,
                meta: dict) -> list[Action]:
    actions = [_action("begin", session, agent=provider, label=label,
                       meta=meta)]
    if state and not ended:
        actions.append(_action("state", session, agent=provider, state=state,
                               meta=meta))
    if info:
        actions.append(_action("info", session, agent=provider, info=info,
                               meta=meta))
    if ended:
        actions.append(_action("end", session, agent=provider, meta=meta))
    return actions


def _ci_meta(event: str, delivery: str, at: object = None,
             extra: dict | None = None) -> dict:
    meta: dict = {"event": event}
    if delivery:
        meta["delivery"] = delivery
    when = _epoch(at)
    if when is not None:
        meta["at"] = when
    if extra:
        meta.update(extra)
    return meta


def _delivery(kind: str, *parts: object) -> str:
    body = ":".join(str(part) for part in parts if part not in (None, ""))
    return f"{kind}:{body}" if body else kind


def _github_repo(payload: dict, run: dict) -> str:
    for source in (payload.get("repository"), run.get("repository"),
                   _dict(run.get("check_suite")).get("repository"),
                   run.get("head_repository")):
        repo = _dict(source)
        name = _first(repo, ("full_name", "fullName"), 160)
        if name:
            return name
    return "unknown"


def _github_branch(run: dict) -> str:
    for source in (run, _dict(run.get("check_suite"))):
        prs = source.get("pull_requests")
        if isinstance(prs, list) and prs:
            number = _digits(_dict(prs[0]).get("number"))
            if number is not None:
                return f"pr-{number}"
    branch = (_first(run, ("head_branch", "headBranch"), 120)
              or _first(_dict(run.get("check_suite")),
                        ("head_branch", "headBranch"), 120))
    if branch:
        return branch
    run_id = _digits(run.get("id"))
    return f"run-{run_id}" if run_id is not None else "unknown"


def _github(payload: dict, headers: Mapping[str, str] | None) -> list[Action]:
    if isinstance(payload.get("check_run"), dict):
        kind, run = "check_run", payload["check_run"]
    elif isinstance(payload.get("workflow_run"), dict):
        kind, run = "workflow_run", payload["workflow_run"]
    else:
        return []
    action = _first(payload, ("action",), 40)
    status = _first(run, ("status",), 40)
    conclusion = _first(run, ("conclusion",), 40)
    word = conclusion or status or action
    if _word(action) == "requested_action":
        word = "action_required"
    ended, state = _ci_verdict(word)
    repo = _github_repo(payload, run)
    name = _first(run, ("name", "workflow_name"), 80) or kind
    branch = _github_branch(run)
    session = _ci_session(repo, name, branch)
    delivery = (_header(headers, "X-GitHub-Delivery")
                or _delivery(kind, run.get("id"),
                             conclusion or action or status))
    when = _field(run, ("updated_at", "started_at", "created_at"))
    meta = _ci_meta(action or status, delivery, when)
    info = {"ci": _compact({
        "provider": "github", "kind": kind, "status": status,
        "conclusion": conclusion, "action": action,
        "run": _digits(run.get("id")),
        "url": _first(run, ("html_url", "details_url"), 200),
    })}
    label = f"{name} {branch}".strip()
    return _ci_actions("github", session, label, state, ended, info, meta)


def _gitlab_branch(payload: dict, attrs: dict) -> str:
    merge = _dict(payload.get("merge_request"))
    if merge:
        number = _digits(merge.get("iid"))
        if number is not None:
            return f"mr-{number}"
    ref = _first(attrs, ("ref",), 120)
    if ref:
        return ref
    run_id = _digits(attrs.get("id"))
    return f"pipeline-{run_id}" if run_id is not None else "unknown"


def _gitlab(payload: dict, headers: Mapping[str, str] | None) -> list[Action]:
    if _word(payload.get("object_kind")) != "pipeline":
        return []
    attrs = _dict(payload.get("object_attributes"))
    if not attrs:
        return []
    status = _first(attrs, ("status",), 40)
    ended, state = _ci_verdict(status)
    project = _dict(payload.get("project"))
    repo = (_first(project, ("path_with_namespace",), 160)
            or _first(project, ("name",), 120) or "unknown")
    run_id = _digits(attrs.get("id"))
    pipeline = (f"pipeline-{run_id}" if run_id is not None
                else _first(attrs, ("source",), 60) or "pipeline")
    branch = _gitlab_branch(payload, attrs)
    session = _ci_session(repo, pipeline, branch)
    event = _header(headers, "X-Gitlab-Event") or "pipeline"
    delivery = (_header(headers, "X-Gitlab-Event-UUID")
                or _delivery("pipeline", run_id, status))
    when = _field(attrs, ("updated_at", "created_at", "finished_at"))
    meta = _ci_meta(event, delivery, when)
    info = {"ci": _compact({
        "provider": "gitlab", "status": status,
        "ref": _first(attrs, ("ref",), 120), "pipeline": run_id,
        "url": _first(project, ("web_url",), 200),
    })}
    label = f"{repo} {branch}".strip()
    return _ci_actions("gitlab", session, label, state, ended, info, meta)


def _jenkins_branch(build: dict) -> str:
    for source in (build.get("parameters"), build.get("scm"),
                   _dict(build.get("scm")).get("branch")):
        params = _dict(source)
        branch = _first(params, ("BRANCH_NAME", "GIT_BRANCH", "BRANCH", "REF",
                                 "CHANGE_BRANCH", "branch"), 120)
        if branch:
            return branch
    return ""


def _jenkins_meta(build: dict) -> tuple[bool, str | None, str]:
    phase = _word(_field(build, ("phase",)))
    status = _first(build, ("status",), 40)
    if phase == "queued":
        return False, _canonical("idle"), phase
    if phase in ("started", "building"):
        return False, _canonical("working"), phase
    if phase == "deleted":
        return True, None, phase
    ended, state = _ci_verdict(status or phase)
    return ended, state, phase or status


def _jenkins_one(payload: dict) -> list[Action]:
    build = _dict(payload.get("build"))
    if not build:
        return []
    name = _first(payload, ("name",), 120) or _first(build, ("job_name",), 120)
    if not name:
        return []
    ended, state, event = _jenkins_meta(build)
    number = _digits(build.get("number"))
    branch = _jenkins_branch(build)
    pipeline = f"build-{number}" if number is not None else name
    lane_branch = branch or "job"
    session = _ci_session(name, pipeline, lane_branch)
    full_url = _first(build, ("full_url", "url"), 300)
    delivery = full_url or _delivery("jenkins", name, number, event)
    meta = _ci_meta(event, delivery)
    info = {"ci": _compact({
        "provider": "jenkins", "phase": event,
        "status": _first(build, ("status",), 40), "number": number,
        "url": full_url,
    })}
    suffix = branch or pipeline
    label = name if suffix == name else f"{name} {suffix}".strip()
    return _ci_actions("jenkins", session, label, state, ended, info, meta)


def _jenkins(payload: dict, headers: Mapping[str, str] | None) -> list[Action]:
    items: list = payload if isinstance(payload, list) else [payload]
    actions: list[Action] = []
    for item in items:
        actions.extend(_jenkins_one(_dict(item)))
    return actions


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_NORMALIZERS: dict[str, Callable[[dict, Mapping[str, str] | None], list[Action]]] = {
    "amazon-q": lambda payload, headers: _run_hooks(
        _PROFILES["amazon-q"], payload, headers),
    "claude-code": lambda payload, headers: _run_hooks(
        _PROFILES["claude-code"], payload, headers),
    "codex": lambda payload, headers: _run_hooks(
        _PROFILES["codex"], payload, headers),
    "continue": lambda payload, headers: _run_hooks(
        _PROFILES["continue"], payload, headers),
    "copilot": lambda payload, headers: _run_hooks(
        _PROFILES["copilot"], payload, headers),
    "cursor": lambda payload, headers: _run_hooks(
        _PROFILES["cursor"], payload, headers),
    "devin": lambda payload, headers: _run_hooks(
        _PROFILES["devin"], payload, headers),
    "gemini-cli": lambda payload, headers: _run_hooks(
        _PROFILES["gemini-cli"], payload, headers),
    "generic": _generic,
    "github": _github,
    "gitlab": _gitlab,
    "jenkins": _jenkins,
}

_SOURCES = ("amazon-q", "claude-code", "codex", "continue", "copilot",
            "cursor", "devin", "gemini-cli", "generic", "github", "gitlab",
            "jenkins")

_ALIASES = {
    "amazonq": "amazon-q",
    "q": "amazon-q",
    "gha": "github",
    "github-actions": "github",
    "gitlab-ci": "gitlab",
    "jenkins-ci": "jenkins",
    "claude": "claude-code",
    "codex-cli": "codex",
    "continue-cli": "continue",
    "copilot-cli": "copilot",
    "gemini": "gemini-cli",
    "windsurf": "devin",
    "cascade": "devin",
    "devin-desktop": "devin",
}


def sources() -> tuple[str, ...]:
    """Every canonical source name :func:`normalize` understands."""
    return _SOURCES


def normalize(source: str, payload: dict,
              headers: Mapping[str, str] | None = None) -> list[Action]:
    """Turn one native payload into canonical actions. Never raises.

    ``source`` names the dialect (``sources()`` lists them; a few friendly
    aliases such as ``windsurf`` and ``github-actions`` are accepted).
    ``payload`` is the raw JSON body. ``headers`` is the webhook/caller's
    header mapping, used for CI delivery ids and for harnesses that omit the
    event name from the body. An unknown source, an unknown event, a
    malformed body or a body without a session id all yield ``[]``.
    """
    try:
        name = (str(source or "").strip().lower()
                .replace("_", "-").replace(" ", "-"))
        canonical = _ALIASES.get(name, name)
        handler = _NORMALIZERS.get(canonical)
        if handler is None:
            return []
        if not isinstance(payload, dict) and not (
                canonical == "jenkins" and isinstance(payload, list)):
            return []
        return [action for action in handler(payload, headers)
                if isinstance(action, Action)]
    except Exception:                          # noqa: BLE001 - ingest never raises
        return []
