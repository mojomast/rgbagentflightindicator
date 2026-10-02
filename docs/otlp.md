# OTLP receiver: telemetry in, lane detail out

`rgi.otlp` turns OTLP/HTTP JSON from any OpenTelemetry-speaking agent
framework into plain lane metadata: tokens, cost, context pressure, model and
activity. It is a library module - the daemon can hand it a request body and
merge the returned `InfoUpdate` fields into `/session/info`.

The one rule that matters: **the receiver never reports a lane state**. A span
ending does not mean a session ended, and a token counter moving does not mean
an agent is working. Only the harness's own hook/event path may say
`working` / `done` / `blocked` / `error`. OTLP is the detail half of a lane,
never the lamp half.

```python
from rgi.otlp import OtlpReceiver

receiver = OtlpReceiver()                    # max_body=1_000_000, clock=time.time
updates = receiver.handle("/v1/metrics", body)

for update in updates:
    # update.session  -> "claude-code:ses_01J8CLAUDE"
    # update.fields   -> {"tokens": {"input": 1200, "output": 340}, ...}
    # update.meta     -> {"signal": "metrics", "records": 6, ...}
    ...
```

`handle(path, payload)` takes the parsed JSON object, or the raw JSON
string/bytes (then `max_body` applies). `path` is `/v1/metrics`, `/v1/logs` or
`/v1/traces`; a full URL ending in one of those works too. It never raises:
unknown paths, malformed JSON and malformed objects all return `[]`.
`clock` is injectable so the timestamp fallback below is deterministic in
tests.

## Where the fields land

Every record in one request is folded per derived session, so a token counter
split over two data points arrives as one update with one `tokens` dict.
Nested dicts merge one level deep, exactly like `POST /session/info`, and a
repeated key wins with the newest value - token counters are cumulative, so
the latest reading is the right one, not a sum.

| field | shape | source examples |
|---|---|---|
| `tokens.input` | int | `gen_ai.client.token.usage` + `gen_ai.token.type=input`; `input_tokens`; `prompt_tokens`; `gen_ai.usage.input_tokens`; `cache_read_tokens`; `cache_creation_tokens`; `reasoning_tokens`; `cost_usd` |
| `tokens.output` | int | the same counter with `type=output` / `completion_tokens` |
| `tokens.cache_read` | int | `type=cacheRead`, `cached_tokens`, `gen_ai.usage.cache_read.input_tokens` |
| `tokens.cache_write` | int | `type=cacheCreation`, `cache_creation_tokens` |
| `tokens.reasoning` | int | `reasoning_tokens`, `thought_tokens` |
| `tokens.cost` | float (USD) | `gen_ai.client.cost`, `gen_ai.usage.cost`, `claude_code.cost.usage`, `cost_usd` |
| `context.percent` | float | `context.percent`, `context.window.percent`; computed from used/limit when absent |
| `context.used` | int | `claude_code.context.tokens` (attribute or metric value), `context.tokens`, `context.used` |
| `context.limit` | int | `claude_code.context.limit`, `context.limit`, bare `limit` next to a used value |
| `context.entries` | int | `context.entries` |
| `context.compactions` | int | `context.compactions`, `compaction.count`, `compact.count` |
| `model` | string | `gen_ai.request.model`, `gen_ai.response.model`, `model`, `llm.model_name` |
| `activity` | string | `gen_ai.operation.name` (spans), `gen_ai.tool.name` / `tool_name` / `function_name`, otherwise the vendor event name (`claude_code.tool_result`) or span name |

`gen_ai.client.operation.duration` and anything else ending in `duration` is
deliberately ignored: latency is not lane metadata.

`meta` carries provenance, not lane detail: `signal` (`metrics` | `logs` |
`traces` | `mixed`), `records` folded, `service` name, the latest `event`
name, and `at` (record timestamp in epoch seconds) when the payload had one.

## Session attribution

Two lanes for one conversation is the bug this receiver exists to avoid. The
first hit wins, checked against resource attributes first and then the
record's own attributes:

1. `session.id`
2. `gen_ai.conversation.id`
3. `rgi.session`
4. `service.instance.id`

When one of those is found it becomes the id half of the lane key. If
`service.name` names a known harness (`claude-code` / `claude_code`,
`gemini-cli`, `openai-agents`, `pydantic-ai`, `langgraph`, `crewai`,
`ms-agent`, `vercel-ai`, `google-adk`, ...), the lane is
`<harness>:<id>` - the same namespace the hook integrations use, so hook
state and OTLP detail share one lane.

With no id at all, the fallback is:

```
otlp:<service.name>:<derived>
```

`<derived>` is the first 12 characters of the record's trace id when there is
one, otherwise a one-hour bucket of the record timestamp (`int(seconds // 3600)`),
or of the receiver's clock when the record has no timestamp. Missing
`service.name` becomes `unknown`. Records from one hour with no identity
therefore group onto one lane, and roll over on the hour.

## Privacy

Attributes pass through one door (`attrs_to_dict`), which drops every key that
looks like content - `prompt`, `content`, `body`, `message`, `completion`,
`transcript`, `args`/`arguments`, bare `input`/`output`, `*_text` - unless it
is an id or a count (`message_id`, `tool_use_id`, `content.count`,
`prompt_length`, `input_tokens`). Message bodies are never parsed; a log body
is read only when it is a short, known vendor event name such as
`claude_code.api_request`. Session id, model, tool name and numeric counters
are the only strings that ever reach an update.

## Bounds

* `max_body` (default 1,000,000 bytes) for raw JSON text/bytes;
* 200 attributes scanned per record (`OtlpReceiver.MAX_ATTRS`);
* 500 updates returned per call (`MAX_UPDATES`);
* 10,000 records inspected per call (`MAX_RECORDS`);
* malformed shapes never raise; the call returns what was understood so far.

## Compatibility

Tested shapes live in `tests/fixtures/otlp/`: Claude Code metrics and events,
Gemini CLI events, a camelCase Pydantic AI / OpenAI Agents span batch, and a
snake_case GenAI metric batch. Attribute wrappers may be camelCase or
snake_case, and int64s may arrive as JSON strings or numbers.

Development caveats worth remembering: OTel GenAI conventions are still
Development and mixed generations are normal (`prompt_tokens` vs
`input_tokens`). This receiver coalesces both rather than trusting either.
OTLP gives no approvals, so `blocked` still needs a hook or the harness's own
event surface. Protobuf and gRPC export are out of scope; point exporters at
OTLP/HTTP JSON.
