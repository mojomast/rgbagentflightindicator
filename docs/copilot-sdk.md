# GitHub Copilot SDK sessions

The GitHub Copilot SDK is the package an application uses to create and drive
Copilot sessions (`github-copilot-sdk` on PyPI, `@github/copilot-sdk` on npm).
Unlike the hook-based harnesses, there is no process boundary to observe: the
application owns the session object, so it hands that object to this adapter,
and the adapter mirrors the session's events onto one lane.

**Scope, stated plainly:** this follows sessions that the application creates
or adopts through the SDK, on the machine running the application. It makes no
claim about observing every IDE Copilot conversation: Copilot Chat inside an
editor, or the CLI's own TUI, has no SDK session object for this adapter to
attach to.

The adapter lives in
[`rgi/integrations/copilot.py`](../rgi/integrations/copilot.py); a runnable
walkthrough is in
[`examples/copilot_sdk_demo.py`](../examples/copilot_sdk_demo.py).

## What it reports

Every row is an exact SDK event name, as generated in the SDK's session-events
schema shared by all languages.

| Copilot SDK event | panel |
|---|---|
| `user.message`, `assistant.turn_start`, `tool.execution_start` | `working` |
| `permission.requested` | `blocked(request=<requestId>)`, with the action kind and a short detail |
| `permission.completed` | `resolve(<same requestId>)` |
| `user_input.requested` | `blocked(request=<requestId>)`, with the question |
| `user_input.completed` | `resolve(<same requestId>)` |
| `elicitation.requested` | `blocked(request=<requestId>)`, with the message |
| `elicitation.completed` | `resolve(<same requestId>)` |
| `session.error` | `error(<message>)` |
| `session.task_complete` | `info({"task": "complete"})` only -- **never** the completion signal |
| `session.idle` | `done()` |

Two semantics worth knowing:

- A wait keeps the lane blocked even when `session.idle` has already landed:
  the reporter counts pending requests by id, and the adapter resolves a wait
  only when its own `*.completed` event arrives. Concurrent waits survive each
  other.
- `session.task_complete` is optional detail (the SDK sends it with a summary
  and a success flag); completion is `session.idle`, nothing else.

## Usage

```python
import asyncio

from copilot import CopilotClient
from copilot.session import PermissionHandler

from rgi.integrations.copilot import CopilotReporter


async def main():
    async with CopilotClient() as client:
        session = await client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            model="gpt-5",
        )

        # The session id is stable across resumes, so the lane is stable too.
        adapter = CopilotReporter(session.session_id, label="nightly report")
        adapter.attach(session)
        try:
            await session.send_and_wait("Summarize the open issues")
        finally:
            adapter.close()          # releases the lane


asyncio.run(main())
```

`observe(session, label=...)` does the same in one call, deriving the id from
`session.session_id` (it returns `None` instead of raising when the object has
neither an id nor a usable `on`).

The first event claims the lane on demand, so attaching an adapter that never
sees an event never takes a lamp. `attach()` accepts two shapes:

- the Python SDK's `on(handler)`, one callback for every event; and
- an event-emitter object with `on(event_name, handler)`, where the returned
  callable (if any) is kept as an unsubscribe.

Attaching is additive: the SDK broadcasts to every registered handler, so the
application's own listeners keep running, and the adapter never replaces or
filters them.

## Preserving the application's handlers

The adapter is an observer; it never answers a permission or input request.
If the application already has an `on_permission_request` /
`on_user_input_request` handler, leave it in place (or use it with
`wrap_permission_handler` / `wrap_user_input_handler`, which record the wait
and pass the original handler's decision through unchanged). If the application
has no handler, the SDK emits the request event and leaves it pending for
manual resolution -- the lane shows `blocked` either way, and the matching
completion event resolves it.

A wrapped async handler gives the panel a live wait while the human decides; a
sync handler returns in the same call, so the blocked/resolve pair is only a
record and the live view comes from the `*.requested` events.

## Node.js / TypeScript

No Node adapter ships in this repository; the same mapping is a few listeners
on the Node SDK's session, which also broadcasts to every `on` subscriber:

```ts
import { CopilotClient, approveAll } from "@github/copilot-sdk";

const client = new CopilotClient();
await client.start();
const session = await client.createSession({ model: "gpt-5", onPermissionRequest: approveAll });

const working = () => report("working");
session.on("user.message", working);
session.on("assistant.turn_start", working);
session.on("tool.execution_start", working);
session.on("permission.requested", (e) => blocked(String(e.data.requestId)));
session.on("permission.completed", (e) => resolve(String(e.data.requestId)));
session.on("user_input.requested", (e) => blocked(String(e.data.requestId)));
session.on("user_input.completed", (e) => resolve(String(e.data.requestId)));
session.on("elicitation.requested", (e) => blocked(String(e.data.requestId)));
session.on("elicitation.completed", (e) => resolve(String(e.data.requestId)));
session.on("session.error", (e) => error(String(e.data.message)));
session.on("session.task_complete", () => info({ task: "complete" }));
session.on("session.idle", () => done());
```

## Supported versions

- **Python SDK:** `github-copilot-sdk` **1.0.16** (PyPI, uploaded
  2026-09-30; requires Python 3.11+). This is the version the adapter targets.
  Event names and payload fields were verified against the generated
  [`python/copilot/generated/session_events.py`](https://github.com/github/copilot-sdk/blob/main/python/copilot/generated/session_events.py)
  (`SessionEventType`, `PermissionRequestedData`, `UserInputRequestedData`,
  `ElicitationRequestedData`, `SessionErrorData`, `SessionIdleData`,
  `SessionTaskCompleteData`) and the
  [`python/README.md`](https://github.com/github/copilot-sdk/blob/main/python/README.md),
  retrieved 2026-10-01. The SDK was generally available upstream at the time.
- **Node/TypeScript SDK:** `@github/copilot-sdk` **1.0.16** (npm latest,
  checked 2026-10-01 at <https://www.npmjs.com/package/@github/copilot-sdk>).
  The Node snippet above is illustrative; it is not tested here.
- **Simulated, not run against the real runtime.** The adapter's tests drive a
  fake session and the mock panel
  ([`tests/test_copilot.py`](../tests/test_copilot.py)); neither a live Copilot
  CLI runtime nor the real `github-copilot-sdk` package is exercised.

Limitations:

- SDK-owned sessions only. There is no claim about IDE Copilot conversations,
  Copilot Chat, or the CLI's TUI, and no watcher for them.
- Python only in this repository; the Node/TypeScript wiring is a reference,
  not a tested adapter.
- The adapter observes; it never answers or alters a request, and it never
  swallows an application handler's decision or exception.
- Payload fields are read defensively from the SDK's dataclasses or from plain
  dicts, using the event names above. A future SDK rename of an event or field
  degrades to "no report" for that event, never to an error.
- States follow event arrival order, so a `session.idle` after a
  `session.error` reports `done`; the error is visible until the run settles.
- Use one adapter per session. Two adapters with the same session id share the
  panel lane but track their pending waits independently.
- Resolutions for waits that started before `attach()` are ignored (the
  adapter never saw the request id), which avoids a false state push.
- No usage or token reporting: the SDK's token events are not part of this
  mapping.
