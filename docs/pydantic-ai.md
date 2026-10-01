# Pydantic AI

One lamp per **conversation**, driven by the whole `agent.run(...)` and by the
typed event stream it produces. The adapter reuses
[`rgi.report.Reporter`](../rgi/report.py) and imports nothing from
`pydantic_ai`: events and results are read by shape, so it works with
`pydantic-ai` and `pydantic-ai-slim` alike, and the module is safe to import
when the framework is not installed.

```
pip install pydantic-ai          # or pydantic-ai-slim
export RGI_URL=http://<panel-host>:8730     # default: http://127.0.0.1:8730
export RGI_TOKEN=$TOKEN
```

A complete, runnable shape is in
[examples/pydantic_ai_demo.py](../examples/pydantic_ai_demo.py).

## Quickstart

```python
import asyncio
from pydantic_ai import Agent, DeferredToolRequests
from rgi.integrations.pydantic_ai import report_run

agent = Agent("openai:gpt-4o-mini", output_type=[str, DeferredToolRequests],
              instructions="Be brief.")

async def main() -> None:
    conversation = "order-1234"             # stable: reuse it to resume

    async with report_run(conversation_id=conversation, label="orders") as run:
        result = await agent.run("Refund order 4711",
                                 conversation_id=conversation,
                                 **run.run_kwargs)   # feeds the lane
        run.observe(result)

        while isinstance(result.output, DeferredToolRequests):
            deferred = result.output
            approvals = {call.tool_call_id: True for call in deferred.approvals}
            result = await agent.run(
                deferred_tool_results=deferred.build_results(approvals=approvals),
                message_history=result.all_messages(),
                conversation_id=conversation,   # same lane on resume
                **run.run_kwargs,
            )
            run.observe(result)

        print(result.output)

asyncio.run(main())
```

`report_run` is both a sync and an async context manager, and
`run.run_kwargs` carries exactly one thing: the adapter's
`event_stream_handler`.

## Approvals block, external work stays working

Both kinds of deferred tool call end up in `DeferredToolRequests`, but they
mean different things and the lane says so:

| Deferred call | Meaning | Lane |
| --- | --- | --- |
| `DeferredToolRequests.approvals` (`ApprovalRequired`, `requires_approval=True`) | a human must answer | `blocked`, one request per `tool_call_id` |
| `DeferredToolRequests.calls` (`CallDeferred`, `ExternalToolset`) | a background worker / frontend is producing the result | `working` - the ids are published as `deferred.external` metadata, never as a wait |
| `DeferredToolResultsEvent` | an inline resolver answered some calls | those waits resolve; lane back to `working` |
| result with a real `output` | the run finished | `done` |
| exception through the `report_run` block | a failure | `error(message)`, then the exception is re-raised |
| dead panel | - | nothing raises; every call returns `False` |

The distinction matters because `blocked` is the state that blinks for a
human. A slow job that is simply running elsewhere must not look like a
question.

## Events and the stream handler

```python
result = await agent.run(prompt, conversation_id=cid, **run.run_kwargs)
run.observe(result)
```

While the run is alive, the handler folds these events in as they arrive:

* `DeferredToolRequestsEvent` - approvals become `blocked` requests at once
  (before any `HandleDeferredToolCalls` handler runs); external calls are
  recorded as working.
* `DeferredToolResultsEvent` - requests resolved inline are resolved on the
  lane too.
* `FunctionToolCallEvent` / `FunctionToolResultEvent` - `info()` metadata.

If your application already uses its own handler, do not spread
`run.run_kwargs`; instead forward every event to the adapter from yours:

```python
async def my_handler(ctx, events):
    async for event in events:
        run.handle_event(event)     # never raises, unknown events ignored
        ...                         # your own handling

result = await agent.run(prompt, conversation_id=cid,
                         event_stream_handler=my_handler)
run.observe(result)
```

`observe()` accepts the finished `AgentRunResult`, or its `output` directly.
When you pass the paused run's `DeferredToolRequests` to a follow-up run as
`deferred_tool_results=` there is no results event (the caller already knows
them), and `observe()` on the completed follow-up resolves the old waits and
reports `done`.

## Conversation identity across resumes

Pydantic AI treats the deferred-tool follow-up as a **separate agent run with
its own `run_id`**; pause/resume correlation is by `conversation_id` (and the
message history you carry across). The adapter uses the same
`conversation_id` as the lane:

```python
conversation = result.conversation_id          # persist this with the history
result = await agent.run(
    message_history=result.all_messages(),
    deferred_tool_results=results,
    conversation_id=conversation,              # same lane, no second claim
    **run.run_kwargs,
)
```

Do not use the special value `"new"` as a lane id - it asks Pydantic AI for a
fresh conversation and would name a different lane every run. If you pass no
`conversation_id`, `RGI_CONVERSATION_ID` (or `RGI_SESSION`) is used, and
otherwise a generated id, which will not follow a resume.

A resume in the same process needs nothing else: the next `report_run` with
the same id re-blocks remembered waits before the follow-up starts. In a
fresh process, pass them back explicitly:

```python
with report_run(conversation_id=cid, pending=saved_pending) as run:
    result = await agent.run(message_history=history,
                             deferred_tool_results=results,
                             conversation_id=cid, **run.run_kwargs)
    run.observe(result)
```

## The wrapper

```python
report_run(conversation_id=None, *, label=None, pending=None,
           reporter=None, **reporter_kwargs) -> RunReport
```

`RunReport` exposes `run_kwargs`, `event_stream_handler`,
`observe(result)`, `handle_event(event)`, `resolve(request)`, `pending`,
`external`, `reporter`, plus `working()`, `done()`, `error(message)`,
`info(...)` and `heartbeat()` for driving the lane directly.

Diagnostics go to stderr only when `RGI_HOOK_DEBUG=1`. Stdout is never
written to.

## Supported versions

* `pydantic-ai` / `pydantic-ai-slim` **2.52.0** (Python >= 3.10), verified on
  2026-10-01 against the released wheel (`pydantic_ai_slim-2.52.0`):
  `DeferredToolRequests.approvals` / `.calls` and `DeferredToolResults` in
  [`pydantic_ai/_deferred.py`](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/),
  `DeferredToolRequestsEvent` / `DeferredToolResultsEvent` in
  [`pydantic_ai/messages.py`](https://pydantic.dev/docs/ai/api/pydantic-ai/messages/),
  and `event_stream_handler` / `conversation_id` / `deferred_tool_results` on
  the run methods in
  [`pydantic_ai/agent/abstract.py`](https://pydantic.dev/docs/ai/api/pydantic-ai/agent/).

### Limitations

* **Simulated, not run against the real runtime.** The tests drive the adapter
  with fake events and results against the mock panel; no model calls happen
  in CI. Hook and event shapes were read from the 2.52.0 wheel, not executed.
* The adapter is built on the per-run `event_stream_handler`, which 2.52.0
  supports. Applications using the newer `Hooks` capability can call
  `run.handle_event(...)` from their own listener, but that combination was
  not exercised here.
* The lane knows the run and its tools; it does not model handoffs or
  sub-agents, because Pydantic AI runs are single-agent.
* External calls only keep the lane `working` until the next `observe()` /
  `report_run` block settles the conversation; the adapter cannot tell whether
  the background worker is still alive or even running at all.
* Cross-process resume keeps the lane blocked only if you pass
  `pending=[...]`; the in-process memory of open waits does not survive a
  restart.
* Event handling is synchronous best-effort HTTP with the reporter's timeout
  (2 s default) inside the async stream; a dead panel can delay an event by up
  to that timeout.
* Pending waits are capped at 32 per lane, matching the reporter's bounding.
