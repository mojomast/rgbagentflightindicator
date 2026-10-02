"""OTLP/HTTP JSON in, lane metadata out - the receiver never reports state.

A framework that already speaks OpenTelemetry should not need an rgi adapter.
This receiver turns OTLP JSON for metrics, logs and traces into a small set of
:class:`InfoUpdate` values that merge straight into a lane's ``info``: tokens,
cost, context pressure, model and activity. It deliberately emits **no lane
state** - a span ending does not mean a session ended, and only the harness's
own hook/event path may say working/done/blocked/error.

The wire format is the OTLP/HTTP JSON mapping, handled tolerantly because
every exporter versions its own spelling: field names may be camelCase or
snake_case, int64 values may arrive as JSON strings or numbers, and attribute
wrappers may use either casing. :meth:`OtlpReceiver.handle` never raises: bad
data returns ``[]``.

Session attribution, first hit wins - two lanes for one conversation is the
bug this file exists to avoid:

1. ``session.id`` (resource attributes first, then the record's own)
2. ``gen_ai.conversation.id``
3. ``rgi.session``
4. ``service.instance.id``
5. fallback ``otlp:<service.name>:<derived>``, where ``<derived>`` is the
   first 12 characters of the record's trace id when there is one, otherwise a
   one-hour bucket of the record timestamp, or of the injected clock when the
   record carries no timestamp at all.

When one of the ids above was found and ``service.name`` names a known harness
(``claude-code``, ``claude_code``, ``gemini-cli``, ...), the lane is
``<harness>:<id>`` instead of ``otlp:<id>`` - the same namespace the hook
integrations use, so hook-provided state and OTLP-provided detail share a lane.

Fields emitted (all optional; dicts merge one level deep like
``/session/info``):

* ``tokens``: ``input``, ``output``, ``cache_read``, ``cache_write`` and
  ``reasoning`` (ints), plus ``cost`` (float, USD)
* ``context``: ``percent``, ``used``, ``limit``, ``entries``, ``compactions``
* ``model``: requested or serving model, as a short string
* ``activity``: current or last operation/tool/event, as a short string

Privacy: an attribute whose key looks like content - prompt, content, body,
message, completion, transcript, tool arguments, input or output - is dropped
unless it is an id or a count. Message bodies are never parsed; a log body is
read only when it is a short, known vendor event name such as
``claude_code.tool_result``. Bounds: 200 attributes per record, 500 updates
per call, 10_000 records inspected per call, and ``max_body`` bytes of raw
JSON.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["InfoUpdate", "OtlpReceiver", "attr_value", "attrs_to_dict"]

_ATTR_CAP = 200
_UPDATE_CAP = 500
_RECORD_CAP = 10_000

# Wrapper names in the OTLP JSON mapping, camelCase and snake_case.
_ATTR_WRAPPERS = (
    ("stringValue", "string_value", "string"),
    ("intValue", "int_value", "int"),
    ("doubleValue", "double_value", "double"),
    ("boolValue", "bool_value", "bool"),
    ("arrayValue", "array_value", "array"),
    ("kvlistValue", "kvlist_value", "kvlist"),
    ("bytesValue", "bytes_value", "bytes"),
)

# Content-shaped keys are never stored; ids and counts are the exception, and
# token counters (``input_tokens`` etc.) ride on that exception.
_CONTENT = re.compile(
    r"prompt|content|body|message|completion|transcript"
    r"|(?:^|[._-])(?:args?|arguments?|inputs?|outputs?)(?:[._-]|$)",
    re.I,
)
_COUNTY = re.compile(
    r"(?:^|[._-])(?:id|ids|count|counts|total|tokens|length|size|chars)"
    r"(?:[._-]|$)",
    re.I,
)

_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SLUG = re.compile(r"[^a-z0-9._-]+")

_HARNESSES = {
    "claude-code": "claude-code",
    "claude_code": "claude-code",
    "claudecode": "claude-code",
    "claude-code-sdk": "claude-code",
    "gemini-cli": "gemini-cli",
    "gemini_cli": "gemini-cli",
    "gemini": "gemini-cli",
    "openai-agents": "openai-agents",
    "openai_agents": "openai-agents",
    "pydantic-ai": "pydantic-ai",
    "pydantic_ai": "pydantic-ai",
    "langgraph": "langgraph",
    "langchain": "langchain",
    "crewai": "crewai",
    "ms-agent": "ms-agent",
    "ms_agent": "ms-agent",
    "agent-framework": "ms-agent",
    "agent_framework": "ms-agent",
    "vercel-ai": "vercel-ai",
    "vercel_ai": "vercel-ai",
    "google-adk": "google-adk",
    "google_adk": "google-adk",
    "adk": "google-adk",
    "codex": "codex",
    "opencode": "opencode",
    "copilot": "copilot",
    "cursor": "cursor",
    "continue": "continue",
    "aider": "aider",
    "goose": "goose",
}

_SESSION_KEYS = (
    ("session.id", "session_id", "sessionId"),
    ("gen_ai.conversation.id", "gen_ai.conversation_id",
     "gen_ai.conversationId", "conversation.id", "conversation_id"),
    ("rgi.session", "rgi_session", "rgi.session_id"),
    ("service.instance.id", "service.instance_id", "service_instance_id",
     "service.instanceId"),
)

_INPUT_KEYS = (
    "gen_ai.usage.input_tokens", "gen_ai.usage.inputTokens",
    "gen_ai.usage.prompt_tokens", "gen_ai.usage.promptTokens",
    "input_tokens", "inputTokens", "prompt_tokens", "promptTokens",
    "llm.token_count.prompt",
)
_OUTPUT_KEYS = (
    "gen_ai.usage.output_tokens", "gen_ai.usage.outputTokens",
    "gen_ai.usage.completion_tokens", "gen_ai.usage.completionTokens",
    "output_tokens", "outputTokens", "completion_tokens", "completionTokens",
    "llm.token_count.completion",
)
_CACHE_READ_KEYS = (
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.cache_read.inputTokens",
    "cache_read_tokens", "cacheReadTokens", "cached_tokens", "cache_read",
    "llm.token_count.cache_read",
)
_CACHE_WRITE_KEYS = (
    "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.usage.cache_creation.inputTokens",
    "cache_creation_tokens", "cacheCreationTokens", "cache_write_tokens",
)
_REASONING_KEYS = (
    "gen_ai.usage.reasoning_tokens", "gen_ai.usage.reasoningTokens",
    "reasoning_tokens", "reasoningTokens", "thought_tokens",
)
_COST_KEYS = (
    "gen_ai.usage.cost", "gen_ai.usage.cost_usd", "gen_ai.usage.costUsd",
    "cost_usd", "costUsd", "cost", "llm.cost.total",
)
_MODEL_KEYS = (
    "gen_ai.response.model", "gen_ai.response.model_name",
    "gen_ai.request.model", "gen_ai.request.model_name",
    "llm.model_name", "model", "model_name", "model_id",
)
_ACTIVITY_KEYS = (
    "gen_ai.operation.name",
    "gen_ai.tool.name",
    "tool_name", "tool.name", "function_name", "function.name",
    "operation.name", "openinference.span.kind",
)

_PERCENT_KEYS = (
    "context.percent", "context.window.percent", "context_window.percent",
    "context.usage_percent", "claude_code.context.percent",
    "gemini_cli.context.percent",
)
_CONTEXT_USED_KEYS = (
    "claude_code.context.tokens", "context.tokens", "context.used",
    "context.used_tokens", "context_window.used_tokens",
    "context_window.used",
)
_CONTEXT_LIMIT_KEYS = (
    "claude_code.context.limit", "context.limit", "context.limit_tokens",
    "context_window.limit", "context_window.limit_tokens",
    "context_window_size",
)
_COMPACTION_KEYS = (
    "context.compactions", "claude_code.compactions",
    "gemini_cli.compactions", "context.compaction.count",
    "context.compaction_count", "compaction.count", "compaction_count",
    "compact.count", "compactions",
)
_ENTRY_KEYS = ("context.entries", "context.entry.count", "context.entry_count")

_EVENT_KEYS = ("event.name", "event_name", "event")
_EVENT_BODY = re.compile(
    r"^(?:claude_code|gemini_cli|codex|opencode|openai_agents|pydantic_ai"
    r"|langgraph|crewai|ms_agent|gen_ai|rgi)\.[A-Za-z0-9_.-]{1,60}$"
)

_TIME_FIELDS = (
    "timeUnixNano", "time_unix_nano",
    "observedTimeUnixNano", "observed_time_unix_nano",
    "startTimeUnixNano", "start_time_unix_nano",
    "endTimeUnixNano", "end_time_unix_nano",
)

_TOKEN_TYPES = {
    "input": "input", "prompt": "input", "input_tokens": "input",
    "output": "output", "completion": "output", "output_tokens": "output",
    "cache_read": "cache_read", "cache_read_tokens": "cache_read",
    "cached": "cache_read", "cacheread": "cache_read", "cache": "cache_read",
    "cache_creation": "cache_write", "cache_write": "cache_write",
    "cachecreation": "cache_write", "cachewrite": "cache_write",
    "reasoning": "reasoning", "reasoning_tokens": "reasoning",
    "thought": "reasoning",
}


@dataclass(frozen=True)
class InfoUpdate:
    """One lane's worth of merge-ready detail, never a state.

    ``session`` is the canonical ``<namespace>:<id>`` key. ``fields`` merges
    into lane info one level deep. ``meta`` carries provenance (which signal,
    which event, how many records were folded, the latest timestamp).
    """

    session: str
    fields: dict
    meta: dict = field(default_factory=dict)


# -- scalar and attribute helpers -----------------------------------------

def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return None if number is None else int(number)


def _clean(value: Any, limit: int = 80) -> str:
    text = _SPACES.sub(" ", str(value)).strip()
    text = _CONTROL.sub("", text)
    return text[:limit]


def _text(value: Any, limit: int = 80) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return _clean(value, limit)


def _slug(value: Any, limit: int = 32) -> str:
    text = _SLUG.sub("-", _clean(value, limit * 2).lower()).strip("-._")
    return text[:limit] or "unknown"


def _lookup(attrs: dict, names: tuple[str, ...]) -> Any:
    for name in names:
        if name in attrs:
            return attrs[name]
    return None


def attr_value(raw: Any) -> Any:
    """Unwrap one OTLP JSON ``AnyValue``; unknown or bytes shapes give None.

    Tolerates a bare scalar (some exporters skip the wrapper), the camelCase
    and snake_case wrappers, arrays and nested key/value lists. Byte values
    are deliberately dropped: they are only ever content.
    """
    if isinstance(raw, dict):
        for camel, snake, kind in _ATTR_WRAPPERS:
            if camel not in raw and snake not in raw:
                continue
            value = raw.get(camel, raw.get(snake))
            if kind == "string":
                return value if isinstance(value, str) else None
            if kind == "int":
                return _int(value)
            if kind == "double":
                return _float(value)
            if kind == "bool":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                    return value.strip().lower() == "true"
                return None
            if kind == "array":
                items = value.get("values") if isinstance(value, dict) else value
                if not isinstance(items, list):
                    return None
                return [attr_value(item) for item in items[:_ATTR_CAP]]
            if kind == "kvlist":
                items = value.get("values") if isinstance(value, dict) else value
                if isinstance(items, list):
                    return attrs_to_dict(items)
                if isinstance(items, dict):
                    return {str(key): attr_value(item)
                            for key, item in list(items.items())[:_ATTR_CAP]}
                return None
            return None                    # bytes: never metadata
        if len(raw) == 1 and ("value" in raw or "Value" in raw):
            return attr_value(raw.get("value", raw.get("Value")))
        return None
    if isinstance(raw, bool) or isinstance(raw, (int, float, str)):
        return raw
    if isinstance(raw, list):
        return [attr_value(item) for item in raw[:_ATTR_CAP]]
    return None


def attrs_to_dict(attributes: Any, limit: int = _ATTR_CAP) -> dict:
    """An OTLP attribute list as a plain dict, content-scrubbed and bounded.

    This is the only door attributes come through, so the privacy rule lives
    here: content-shaped keys are dropped unless they are ids or counts. A
    malformed list yields an empty dict rather than an exception.
    """
    out: dict = {}
    if not isinstance(attributes, list):
        return out
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = _ATTR_CAP
    for item in attributes[:limit]:
        if not isinstance(item, dict):
            continue
        key = item.get("key", item.get("Key"))
        if not isinstance(key, str) or not key.strip():
            continue
        key = key.strip()[:80]
        if _CONTENT.search(key) and not _COUNTY.search(key):
            continue
        value = attr_value(item.get("value", item.get("Value")))
        if value is None:
            continue
        out[key] = value
    return out


# -- reading the OTLP structure -------------------------------------------

def _series(mapping: Any, *names: str) -> list:
    if not isinstance(mapping, dict):
        return []
    for name in names:
        value = mapping.get(name)
        if isinstance(value, list):
            return value
    return []


def _resource_attrs(container: dict) -> dict:
    resource = container.get("resource")
    if not isinstance(resource, dict):
        return {}
    return attrs_to_dict(resource.get("attributes"))


def _points(metric: dict) -> list:
    for wrapper in ("sum", "gauge", "histogram", "exponentialHistogram",
                    "exponential_histogram"):
        node = metric.get(wrapper)
        if isinstance(node, dict):
            return _series(node, "dataPoints", "data_points")
    return _series(metric, "dataPoints", "data_points")


def _signal(path: Any) -> str | None:
    if not isinstance(path, str):
        return None
    clean = path.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
    if clean.endswith("/v1/metrics"):
        return "metrics"
    if clean.endswith("/v1/logs"):
        return "logs"
    if clean.endswith("/v1/traces"):
        return "traces"
    return None


def _service_name(res_attrs: dict) -> str:
    return _text(_lookup(res_attrs, ("service.name", "service_name")), 64)


def _namespace(res_attrs: dict) -> str | None:
    text = _text(_lookup(res_attrs, ("service.name", "service_name")), 80)
    if not text:
        return None
    return _HARNESSES.get(text.lower().split("@", 1)[0].strip())


def _session_id(res_attrs: dict, rec_attrs: dict) -> str | None:
    for names in _SESSION_KEYS:
        for source in (res_attrs, rec_attrs):
            value = _lookup(source, names)
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                return str(value)
            text = _text(value, 64)
            if text:
                return text
    return None


def _epoch_seconds(value: Any) -> float | None:
    number = _float(attr_value(value))
    if number is None or number <= 0:
        return None
    if number > 1e17:                      # nanoseconds
        return number / 1_000_000_000.0
    if number > 1e14:                      # microseconds
        return number / 1_000_000.0
    if number > 1e11:                      # milliseconds
        return number / 1_000.0
    return number


def _record_seconds(record: dict) -> float | None:
    for name in _TIME_FIELDS:
        if name in record:
            seconds = _epoch_seconds(record[name])
            if seconds is not None:
                return seconds
    return None


def _derived_id(record: dict, clock) -> str:
    trace = record.get("traceId", record.get("trace_id"))
    if isinstance(trace, str):
        digest = re.sub(r"[^0-9A-Za-z]", "", trace)[:12]
        if digest:
            return digest
    seconds = _record_seconds(record)
    if seconds is None:
        try:
            seconds = float(clock())
        except Exception:
            seconds = time.time()
    return str(int(seconds // 3600))


def _event_name(record: dict, attrs: dict) -> str | None:
    text = _text(_lookup(attrs, _EVENT_KEYS), 80)
    if text:
        return text
    body = attr_value(record.get("body"))
    if isinstance(body, str) and _EVENT_BODY.match(body.strip()):
        return body.strip()
    return None


# -- field extraction -------------------------------------------------------

def _context_fields(attrs: dict, used: Any = None) -> dict:
    context: dict = {}
    if used is None:
        used = _int(_lookup(attrs, _CONTEXT_USED_KEYS))
    else:
        used = _int(used)
    limit = _int(_lookup(attrs, _CONTEXT_LIMIT_KEYS))
    if limit is None and used is not None:
        # claude_code.context.tokens ships its denominator as a bare `limit`.
        limit = _int(attrs.get("limit"))
    percent = _float(_lookup(attrs, _PERCENT_KEYS))
    if used is not None and used >= 0:
        context["used"] = used
    if limit is not None and limit > 0:
        context["limit"] = limit
    if percent is None and used is not None and limit:
        percent = 100.0 * used / limit
    if percent is not None and percent >= 0:
        context["percent"] = round(percent, 1)
    compactions = _int(_lookup(attrs, _COMPACTION_KEYS))
    if compactions is not None and compactions >= 0:
        context["compactions"] = compactions
    entries = _int(_lookup(attrs, _ENTRY_KEYS))
    if entries is not None and entries >= 0:
        context["entries"] = entries
    return context


def _fields_from_attrs(attrs: dict, event: str | None = None) -> dict:
    fields: dict = {}
    tokens: dict = {}
    for name, keys in (("input", _INPUT_KEYS), ("output", _OUTPUT_KEYS),
                       ("cache_read", _CACHE_READ_KEYS),
                       ("cache_write", _CACHE_WRITE_KEYS),
                       ("reasoning", _REASONING_KEYS)):
        value = _int(_lookup(attrs, keys))
        if value is not None:
            tokens[name] = value
    cost = _float(_lookup(attrs, _COST_KEYS))
    if cost is not None:
        tokens["cost"] = round(cost, 6)
    if tokens:
        fields["tokens"] = tokens

    model = _text(_lookup(attrs, _MODEL_KEYS), 80)
    if model:
        fields["model"] = model

    context = _context_fields(attrs)
    if context:
        fields["context"] = context

    activity = _text(_lookup(attrs, _ACTIVITY_KEYS), 60)
    if not activity and event:
        activity = _text(event, 60)
    if activity:
        fields["activity"] = activity
    return fields


def _token_kind(value: Any) -> str | None:
    text = _text(value, 40).lower().replace("-", "_").replace(" ", "_")
    text = re.sub(r"[^a-z0-9_]", "", text)
    if not text:
        return None
    return _TOKEN_TYPES.get(text) or _TOKEN_TYPES.get(text.replace("_", ""))


def _token_kind_from_name(name: str) -> str | None:
    if name.endswith(("input_tokens", "prompt_tokens", "input")):
        return "input"
    if name.endswith(("output_tokens", "completion_tokens", "output")):
        return "output"
    if name.endswith("cache_read"):
        return "cache_read"
    if name.endswith("cache_write"):
        return "cache_write"
    if name.endswith("reasoning_tokens"):
        return "reasoning"
    return None


def _is_token_usage(name: str) -> bool:
    return (name == "gen_ai.client.token.usage"
            or name.endswith(".token.usage")
            or name.endswith("_token_usage")
            or name.endswith("token_usage")
            or name.endswith("_tokens"))


def _is_cost(name: str) -> bool:
    return (name in ("gen_ai.client.cost", "gen_ai.usage.cost")
            or name.endswith((".cost", ".cost.usage"))
            or name.endswith(("_cost", ".cost_usd")))


def _is_duration(name: str) -> bool:
    return name.endswith(("operation.duration", ".duration", "_duration"))


def _is_context_metric(name: str) -> bool:
    """A metric whose data point value is the context window's used tokens."""
    return name.endswith(("context.tokens", "context.used",
                          "context_window.used", "context_window.used_tokens"))


def _point_number(point: dict) -> float | None:
    for key in ("asInt", "as_int", "asDouble", "as_double", "value", "sum"):
        if key in point:
            number = _float(attr_value(point.get(key)))
            if number is not None:
                return number
    if "count" in point:
        return _float(attr_value(point.get("count")))
    return None


def _metric_tokens(name: str, attrs: dict, point: dict) -> dict:
    value = _point_number(point)
    if value is None or value < 0:
        return {}
    if _is_cost(name):
        return {"cost": round(value, 6)}
    if not _is_token_usage(name):
        return {}
    kind = _token_kind(_lookup(
        attrs, ("gen_ai.token.type", "gen_ai.token_type", "gen_ai.tokenType",
                "token.type", "type")))
    if kind is None:
        kind = _token_kind_from_name(name)
    return {kind: int(value)} if kind else {}


# -- result folding ---------------------------------------------------------

class _Fold:
    """Fold one request's records into at most ``limit`` lane updates.

    Fields merge one level deep, exactly like the panel's ``/session/info``,
    so input and output counters split over two data points land as one
    ``tokens`` dict.
    """

    def __init__(self, limit: int, record_limit: int):
        self.limit = limit
        self.record_limit = record_limit
        self.records = 0
        self.fields: dict = {}
        self.meta: dict = {}

    def record(self) -> bool:
        if self.records >= self.record_limit:
            return False
        self.records += 1
        return True

    def add(self, session: str, fields: dict, meta: dict) -> bool:
        if session not in self.fields:
            if len(self.fields) >= self.limit:
                return False
            self.fields[session] = {}
            self.meta[session] = {"records": 0, "signals": []}
        target = self.fields[session]
        for key, value in fields.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                target[key].update(value)
            else:
                target[key] = value
        book = self.meta[session]
        book["records"] += 1
        signal = meta.get("signal")
        if signal and signal not in book["signals"]:
            book["signals"].append(signal)
        for key in ("service", "event"):
            if meta.get(key):
                book[key] = meta[key]
        at = meta.get("at")
        if at is not None and at > book.get("at", 0):
            book["at"] = at
        return True

    def updates(self) -> list[InfoUpdate]:
        out: list[InfoUpdate] = []
        for session, fields in self.fields.items():
            book = self.meta[session]
            signals = book.pop("signals", [])
            if len(signals) == 1:
                book["signal"] = signals[0]
            elif signals:
                book["signal"] = "mixed"
            out.append(InfoUpdate(session=session, fields=fields, meta=book))
        return out


# -- the receiver -----------------------------------------------------------

class OtlpReceiver:
    """Stateless OTLP/HTTP JSON -> lane metadata.

    ``max_body`` bounds raw string/bytes bodies only; a parsed dict is already
    in memory and is bounded record-by-record instead. ``clock`` is used only
    to bucket a session when a record has neither a session id nor a timestamp.
    """

    MAX_ATTRS = _ATTR_CAP
    MAX_UPDATES = _UPDATE_CAP
    MAX_RECORDS = _RECORD_CAP

    def __init__(self, *, max_body: int = 1_000_000, clock=time.time):
        try:
            self.max_body = max(1, int(max_body))
        except (TypeError, ValueError):
            self.max_body = 1_000_000
        self.clock = clock if callable(clock) else time.time

    def handle(self, path: str, payload: Any) -> list[InfoUpdate]:
        """One OTLP export request -> at most one update per derived session.

        ``path`` is ``/v1/metrics``, ``/v1/logs`` or ``/v1/traces`` (a full
        URL ending in one of those is fine). ``payload`` is the parsed JSON
        object, or the raw JSON string/bytes. Bad input returns ``[]``.
        """
        try:
            signal = _signal(path)
            if signal is None:
                return []
            data = self._decode(payload)
            if data is None:
                return []
            fold = _Fold(self.MAX_UPDATES, self.MAX_RECORDS)
            if signal == "metrics":
                self._metrics(data, fold)
            elif signal == "logs":
                self._logs(data, fold)
            else:
                self._traces(data, fold)
            return fold.updates()
        except Exception:                  # noqa: BLE001 - never raise upward
            return []

    # -- decoding ----------------------------------------------------------
    def _decode(self, payload: Any) -> dict | None:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, (bytes, bytearray)):
            if len(payload) > self.max_body:
                return None
            try:
                payload = bytes(payload).decode("utf-8", "replace")
            except Exception:
                return None
        if isinstance(payload, str):
            if len(payload) > self.max_body:
                return None
            try:
                data = json.loads(payload)
            except (ValueError, TypeError):
                return None
            return data if isinstance(data, dict) else None
        return None

    # -- session -----------------------------------------------------------
    def _session(self, res_attrs: dict, rec_attrs: dict, record: dict) -> str:
        explicit = _session_id(res_attrs, rec_attrs)
        if explicit is not None:
            return f"{_namespace(res_attrs) or 'otlp'}:{explicit}"
        service = _slug(_service_name(res_attrs))
        return f"otlp:{service}:{_derived_id(record, self.clock)}"

    @staticmethod
    def _meta(signal: str, record: dict, service: str,
              event: str | None = None) -> dict:
        meta: dict = {"signal": signal}
        if service:
            meta["service"] = service
        at = _record_seconds(record)
        if at is not None:
            meta["at"] = round(at, 3)
        if event:
            meta["event"] = event
        return meta

    # -- signals -----------------------------------------------------------
    def _metrics(self, data: dict, fold: _Fold) -> bool:
        for container in _series(data, "resourceMetrics", "resource_metrics"):
            if not isinstance(container, dict):
                continue
            res_attrs = _resource_attrs(container)
            service = _service_name(res_attrs)
            for scope in _series(container, "scopeMetrics", "scope_metrics"):
                for metric in _series(scope, "metrics"):
                    if not self._metric(metric, res_attrs, service, fold):
                        return False
        return True

    def _metric(self, metric: Any, res_attrs: dict, service: str,
                fold: _Fold) -> bool:
        if not isinstance(metric, dict):
            return True
        raw_name = metric.get("name")
        name = raw_name.strip().lower() if isinstance(raw_name, str) else ""
        for point in _points(metric):
            if not isinstance(point, dict):
                continue
            if not fold.record():
                return False
            if _is_duration(name):        # latency is not lane metadata
                continue
            rec_attrs = attrs_to_dict(point.get("attributes"), self.MAX_ATTRS)
            attrs = {**res_attrs, **rec_attrs}
            fields = _fields_from_attrs(attrs)
            tokens = _metric_tokens(name, attrs, point)
            if tokens:
                fields.setdefault("tokens", {}).update(tokens)
            if _is_context_metric(name):
                context = _context_fields(attrs, used=_point_number(point))
                if context:
                    fields.setdefault("context", {}).update(context)
            if not fields:
                continue
            session = self._session(res_attrs, rec_attrs, point)
            if not fold.add(session, fields,
                            self._meta("metrics", point, service)):
                return False
        return True

    def _logs(self, data: dict, fold: _Fold) -> bool:
        for container in _series(data, "resourceLogs", "resource_logs"):
            if not isinstance(container, dict):
                continue
            res_attrs = _resource_attrs(container)
            service = _service_name(res_attrs)
            for scope in _series(container, "scopeLogs", "scope_logs"):
                for record in _series(scope, "logRecords", "log_records"):
                    if not isinstance(record, dict):
                        continue
                    if not fold.record():
                        return False
                    rec_attrs = attrs_to_dict(record.get("attributes"),
                                              self.MAX_ATTRS)
                    attrs = {**res_attrs, **rec_attrs}
                    event = _event_name(record, attrs)
                    fields = _fields_from_attrs(attrs, event=event)
                    if not fields:
                        continue
                    session = self._session(res_attrs, rec_attrs, record)
                    if not fold.add(session, fields,
                                    self._meta("logs", record, service, event)):
                        return False
        return True

    def _traces(self, data: dict, fold: _Fold) -> bool:
        for container in _series(data, "resourceSpans", "resource_spans"):
            if not isinstance(container, dict):
                continue
            res_attrs = _resource_attrs(container)
            service = _service_name(res_attrs)
            for scope in _series(container, "scopeSpans", "scope_spans"):
                for span in _series(scope, "spans"):
                    if not isinstance(span, dict):
                        continue
                    if not fold.record():
                        return False
                    rec_attrs = attrs_to_dict(span.get("attributes"),
                                              self.MAX_ATTRS)
                    attrs = {**res_attrs, **rec_attrs}
                    event = _text(span.get("name"), 80) or None
                    fields = _fields_from_attrs(attrs, event=event)
                    if not fields:
                        continue
                    session = self._session(res_attrs, rec_attrs, span)
                    if not fold.add(session, fields,
                                    self._meta("traces", span, service, event)):
                        return False
        return True
