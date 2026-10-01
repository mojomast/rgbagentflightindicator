# Microsoft Agent Framework

The Microsoft Agent Framework (`agent-framework` on PyPI) has two boundaries
this adapter keeps apart: an **agent run** (wrapped by middleware) and a
**workflow run** (observed through its event stream), where a single executor
finishing is never the workflow finishing. The adapter lives in
[`rgi/integrations/ms_agent.py`](../rgi/integrations/ms_agent.py); a runnable
walkthrough is in [`examples/ms_agent_demo.py`](../examples/ms_agent_demo.py).

**Scope, stated plainly:** the adapter observes runs in the process it runs in,
through the middleware you pass to `agent.run(...)` and the events you hand it
from `workflow.run(...)`. It does not attach to remote agents or hosted
services.

## What it reports

| Agent Framework surface | panel |
|---|---|
| `agent.run(..., middleware=[run.middleware])` starts | `working` |
| the run returns a response without a human wait | `done()` |
| streamed `AgentResponseUpdate`s | `working`, or `blocked` for a carried request; call `run.finish()` when the stream is drained |
| the run raises | `error(<message>)`, re-raised unchanged |
| a response whose `user_input_requests` contains a `function_approval_request` | `blocked(request=<content id>)` |
| `WorkflowEvent` `request_info` (`ctx.request_info(...)`) | `blocked(request=<request_id>)` |
| `run.answer(request_id)` after sending the response | resolves that wait, `working` |
| `WorkflowEvent` `executor_invoked` / `executor_completed` / `executor_failed` | `child()` / `child_done()` metadata only |
| `WorkflowEvent` `started`, `output`, `intermediate`, status `STARTED` / `IN_PROGRESS` | `working` |
| status `IDLE` | `done()` — only if no request is open |
| status `IDLE_WITH_PENDING_REQUESTS` | stays `blocked` |
| `WorkflowEvent` `failed`, status `FAILED` / `CANCELLED` | `error(<message>)` |
| `WorkflowRunResult` (`observe_result`) | folds its events and its final state |

## Executor vs workflow completion

The framework runs executors in supersteps until the graph is idle. An
`executor_completed` event only says that one executor finished — other
executors may still receive messages — so it is published as child metadata and
the lane stays `working`. Completion is the *workflow's* terminal state:

- status `IDLE` after a run that has no pending requests, or
- the final state of a `WorkflowRunResult` (`IDLE`), or
- `failed` / `FAILED` / `CANCELLED` for the error path.

A workflow waiting for a human reports `IDLE_WITH_PENDING_REQUESTS`: the
adapter keeps the lane `blocked` under the `request_info` request id until
`run.answer(request_id)` says the response has been sent.

## Usage

An agent run, wrapped by middleware:

```python
from rgi.integrations.ms_agent import report_run

with report_run("ticket-42", label="triage") as run:
    response = await agent.run(prompt, middleware=[run.middleware])
    run.observe(response)                       # approval -> blocked
    for request in response.user_input_requests:
        request.to_function_approval_response(approved=True)   # human decides
        run.answer(request.id)
```

A workflow, observed through its event stream:

```python
with report_run("nightly", label="nightly workflow") as run:
    request_id = None
    async for event in workflow.run(message, stream=True):
        run.observe(event)                      # request_info -> blocked
        if event.type == "request_info":
            request_id = event.request_id
    if request_id:
        run.answer(request_id)
        result = await workflow.run(responses={request_id: "yes"})
        run.observe_result(result)              # IDLE -> done
```

`run.run_kwargs` gives `{"middleware": [run.middleware]}` for spreading into
`agent.run(...)`, and `run.middleware` can be appended to an existing
middleware list the same way. `report_run(session)` reuses the lane when the
same session id resumes after a request.

## Supported versions

- **Packages:** `agent-framework` **1.19.0**, which pins
  `agent-framework-core[all]==1.19.0` (PyPI, retrieved 2026-10-01). Release:
  <https://github.com/microsoft/agent-framework/releases/tag/python-1.19.0>.
- **How verified:** the released wheels were downloaded from PyPI and the
  surface read: `agent_framework/_middleware.py` (`AgentMiddleware`,
  `AgentContext.result`, user-input requests),
  `agent_framework/_workflows/_events.py` (`WorkflowEvent` types,
  `WorkflowRunState`, `request_info` with `request_id`),
  `agent_framework/_workflows/_workflow.py` (`WorkflowRunResult`,
  `get_final_state`, `IDLE_WITH_PENDING_REQUESTS`) and
  `agent_framework/_workflows/_workflow_context.py`
  (`ctx.request_info(...)`). The official pages were used for the semantics:
  <https://learn.microsoft.com/en-us/agent-framework/overview/agent-framework-overview>,
  <https://learn.microsoft.com/en-us/agent-framework/workflows/workflows> and
  <https://learn.microsoft.com/en-us/agent-framework/workflows/human-in-the-loop>.
- **Simulated, not run against the real runtime.** The tests
  ([`tests/test_frameworks_graph.py`](../tests/test_frameworks_graph.py)) drive
  fake middleware contexts, responses and workflow events against the mock
  panel. No agent or workflow was executed against a live `agent-framework`
  install here; the example's real-runtime branch is illustrative.

Limitations:

- Streaming agent runs need one extra call: middleware cannot see when the
  caller finishes consuming a `ResponseStream`, so the lane stays `working`
  until you call `run.finish()` after the stream is drained. Non-streaming
  responses settle themselves.
- Answering is explicit. After sending the framework the response
  (`workflow.run(responses={...})` or a follow-up agent run), call
  `run.answer(request_id)`; the adapter does not guess which request a new run
  settled.
- The session id is yours. Pass a stable value to `report_run` and reuse it on
  every continuation; a generated id will not follow a resume. The same id
  namespaces a lane for both agent middleware and workflow observation.
- `executor_failed` is child metadata, not a lane error: the workflow decides
  whether the failure is fatal (`failed` / `FAILED` do report `error`).
- Orchestration event types (`group_chat`, `handoff_sent`,
  `magentic_orchestrator`) and `superstep_*` are ignored.
- Token usage is not published; call `run.info(...)` from your own code if the
  response carries usage you want on the panel.
- The lane stays visible until the panel releases it; `run.reporter.end()`
  releases it explicitly.
