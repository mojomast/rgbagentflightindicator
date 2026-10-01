# OpenAI Agents SDK

One lamp per **conversation**, driven by the top-level `Runner` run. The
adapter reuses [`rgi.report.Reporter`](../rgi/report.py) and imports `agents`
inside functions, so the module is safe to import when the SDK is not
installed.

```
pip install openai-agents
export RGI_URL=http://<panel-host>:8730     # default: http://127.0.0.1:8730
export RGI_TOKEN=$TOKEN
```

A complete, runnable shape is in
[examples/openai_agents_demo.py](../examples/openai_agents_demo.py).

## Quickstart

```python
import asyncio
from agents import Agent, Runner
from rgi.integrations.openai_agents import report_run

agent = Agent(name="assistant", model="gpt-4o-mini", instructions="Be brief.")

async def main() -> None:
    conversation = "ticket-1234"            # stable: reuse it to resume

    async with report_run(conversation_id=conversation, label="ticket") as run:
        result = await Runner.run(agent, "Book a table for two",
                                  hooks=run.hooks,
                                  conversation_id=conversation)
        run.observe(result)                 # interrupted -> blocked, else done
        while result.interruptions:         # human-in-the-loop approval
            state = result.to_state()
            for item in result.interruptions:
                state.approve(item)         # or state.reject(item)
                run.resolve(item.call_id)   # lane returns to working
            result = await Runner.run(agent, state, hooks=run.hooks)
            run.observe(result)
        print(result.final_output)

asyncio.run(main())
```

`report_run` is both a sync and an async context manager. Runners and hooks
are otherwise untouched.

## What moves the lamp

| Event | Lane |
| --- | --- |
| `report_run(...)` enters | `working` (after adopting/claiming the lane) |
| `on_agent_start` | child metadata only |
| `on_handoff` | source child `done`, target child `working` - **never** the lane |
| `on_agent_end` (an agent produced its output) | child metadata only |
| `on_tool_start` / `on_tool_end` | `info()` metadata only |
| `result.interruptions` seen by `observe` | `blocked`, one request per `ToolApprovalItem.call_id` |
| an interruption no longer in the result | that wait resolves; lane back to `working` |
| `observe()` with no interruptions left | `done` |
| exception through the `report_run` block | `error(message)`, then the exception is re-raised |
| dead panel | nothing raises; every call returns `False` |

The root boundary is the top-level `Runner.run`. A handoff or an individual
agent finishing inside that loop is only metadata: the lane would otherwise
blink "finished" every time a specialist hands work on. The SDK does not
replay `on_agent_start` for a resumed `RunState` before pending approvals are
resolved, so the adapter never derives "done" from hooks - only from the
finished run result.

## Approval pauses and resumes

When the model calls a tool with `needs_approval` (or a hosted/MCP equivalent),
the runner pauses and `RunResult.interruptions` holds a `ToolApprovalItem` per
call. `report_run` reports one `blocked` request per item, keyed by
`item.call_id`; the lane stays `blocked` while any is open, and `done` is only
reported once a result with **no** interruptions remains.

The lane identity is the `conversation_id` you pass. Pass the same string to
`Runner.run(..., conversation_id=...)` so the SDK's own conversation tracking
and the lamp agree, and persist it next to the serialized `RunState`:

```python
result = await Runner.run(agent, prompt, hooks=run.hooks,
                          conversation_id="ticket-1234")
if result.interruptions:
    state = result.to_state()
    save("ticket-1234", state.to_string(), pending=run.pending)
```

A resume in the same process needs nothing else: the next
`report_run(conversation_id="ticket-1234")` re-blocks the remembered waits
before the run starts. A resume in a **fresh process** should hand them back
explicitly, from the saved `pending` list:

```python
with report_run(conversation_id="ticket-1234", pending=saved_pending) as run:
    stored = load_state()
    state = await RunState.from_json(agent, stored)
    result = await Runner.run(agent, state, hooks=run.hooks)
    run.observe(result)                     # resolves waits that are gone
```

Interruptions raised after a handoff or inside `Agent.as_tool()` surface on
the outer result, so they are handled exactly the same way. Streaming runs
follow the same flow: drain `result.stream_events()` first, then call
`observe(result)` - it will not report `done` while a stream is still
incomplete (`is_complete` is `False`).

If you do not pass a `conversation_id`, `RGI_CONVERSATION_ID` (or
`RGI_SESSION`) is used, and otherwise a fresh id is generated for the run - a
generated id will not follow a resume, so a resumed run would claim a second
lane.

## The wrapper

```python
report_run(conversation_id=None, *, label=None, pending=None,
           reporter=None, **reporter_kwargs) -> RunReport
```

* `conversation_id` - stable lane identity (see above).
* `label` - short lane label (`Reporter` scrubs and bounds it).
* `pending` - request ids to keep blocked before the run starts.
* `reporter` - a prepared `Reporter` to reuse instead of building one.
* `**reporter_kwargs` - `url`, `token`, `ident`, `timeout`... forwarded to
  `Reporter`; the environment normally supplies them.

`RunReport` exposes `hooks`, `observe(result)`, `resolve(request)`,
`pending`, `reporter`, plus `working()`, `done()`, `error(message)`,
`child(...)`, `info(...)` and `heartbeat()` if you want to drive the lane
yourself. Hook-based applications that already own a run loop can share state
through `reporter` (for example, a long quiet wait can wrap
`reporter.keepalive()`).

Diagnostics go to stderr only when `RGI_HOOK_DEBUG=1`. The lane is otherwise
silent, and stdout is never written to.

## Supported versions

* `openai-agents` **0.22.3** (Python >= 3.10), verified on 2026-10-01 against
  the released wheel (`agents-0.22.3`): `RunHooks`/`RunHooksBase` in
  [`agents/lifecycle.py`](https://openai.github.io/openai-agents-python/ref/lifecycle/),
  `Runner.run(..., hooks=..., conversation_id=...)` in
  [`agents/run.py`](https://openai.github.io/openai-agents-python/ref/run/),
  and `RunResult.interruptions` / `ToolApprovalItem.call_id` in
  [`agents/result.py`](https://openai.github.io/openai-agents-python/ref/result/).
  The human-in-the-loop flow is documented at
  <https://openai.github.io/openai-agents-python/human_in_the_loop/>.

### Limitations

* **Simulated, not run against the real runtime.** The test suite drives the
  adapter with fake hook objects, interruption items and run results against
  the mock panel; no model calls happen in CI. The SDK's real hook timing was
  read from the 0.22.3 sources, not executed.
* The lane is only managed for runs started with `hooks=run.hooks`; a run that
  omits them still gets working/done but no child or tool metadata.
* Cross-process resume keeps the lane blocked only if you pass `pending=[...]`;
  the in-process memory of open waits does not survive a restart.
* The adapter does not use `on_llm_start` / `on_llm_end`, and does not publish
  token usage or model context.
* Hook calls are synchronous best-effort HTTP with the reporter's timeout
  (2 s default) inside the SDK's async loop; on a dead panel an event can
  delay the loop by up to that timeout.
* Pending waits are capped at 32 per lane and children at 12, matching the
  reporter's bounding.
