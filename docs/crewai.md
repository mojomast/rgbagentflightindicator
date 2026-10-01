# CrewAI crews and flows

CrewAI announces everything on one process-wide **event bus**: crews, flows,
agents, tasks and human feedback all arrive on the same listeners. The adapter
in [`rgi/integrations/crewai.py`](../rgi/integrations/crewai.py) attaches to
that bus and folds each event into the lane of the root execution it belongs
to. A runnable walkthrough is in
[`examples/crewai_demo.py`](../examples/crewai_demo.py).

**Scope, stated plainly:** the adapter observes the bus of the process it runs
in. A crew in another process is not visible; it makes no claim about hosted or
remote CrewAI runs.

## What it reports

| CrewAI event (`event.type`) | panel |
|---|---|
| `crew_kickoff_started`, `crew_train_started`, `crew_test_started`, `flow_started` | new lane, `working` |
| `crew_kickoff_completed`, `crew_train_completed`, `crew_test_completed`, `flow_finished` | `done()` |
| `crew_kickoff_failed`, `crew_train_failed`, `crew_test_failed`, `flow_failed`, `method_execution_failed` | `error(<message>)` |
| `human_feedback_requested` | `blocked(request=<request_id or event id>)` |
| `human_feedback_received` | resolves that wait, `working` |
| `flow_paused`, `method_execution_paused` | `blocked(request=paused:<flow>:<method>)` |
| `method_execution_started` on a paused run | resolves the paused wait, `working` |
| `flow_input_requested` | `blocked(request=<request_id or input:<flow>:<method>>)` |
| `flow_input_received` | resolves that wait, `working` |
| `agent_execution_started` / `_completed` / `_error`, `task_started` / `_completed` / `_failed`, reasoning events | `child()` / `child_done()` metadata only |

Only the root lifecycle changes the lamp. An agent or task finishing updates
the lane's children and never completes it.

## Correlating events that share one bus

Two crews can run at once, a flow can contain crews, and the bus dispatches
handlers on its own worker threads — so arrival order is not a correlation
mechanism. The adapter uses the ids the bus itself records:

- each event has an `event_id`;
- an ending event carries `started_event_id` pointing at the scope it closed
  (`crew_kickoff_completed` -> its `crew_kickoff_started`, `flow_finished` ->
  its `flow_started`);
- nested events carry the enclosing `parent_event_id`.

Every observed id is mapped to its lane, so an agent or task event finds its
crew, a feedback request finds its flow, and a resolution finds the lane that
holds that request id. Two kickoffs are two event ids, two lanes, and resolving
a wait on one never touches the other. Handlers may arrive out of order; the
adapter pairs `*_completed` with `*_started` by id rather than by arrival.

## Usage

```python
from rgi.integrations.crewai import listen

with listen(label="nightly research"):
    crew.kickoff(inputs={"topic": "rust"})
    # kickoff -> working; agent/task events -> children;
    # human feedback -> blocked; completion -> done
```

For a long-lived application, attach one listener for the process:

```python
from rgi.integrations.crewai import CrewAIReporter

panel = CrewAIReporter(label="all crews")
panel.attach()
try:
    crew.kickoff()
finally:
    panel.close()          # detach and release every lane
```

`listen()` detaches on exit but keeps the lanes' state (a completed crew stays
visible as `done`). `attach()` returns `False` when `crewai` is not installed,
and calling `observe(event)` directly works without the package — which is how
the tests drive it.

## Supported versions

- **Package:** `crewai` **1.15.23** (PyPI, retrieved 2026-10-01). Release:
  <https://github.com/crewAIInc/crewAI/releases/tag/1.15.23>.
- **How verified:** the released wheel was downloaded from PyPI and the event
  surface read: `crewai/events/event_bus.py` (`emit`, handler registration,
  thread-pool dispatch, scope pairing), `crewai/events/base_events.py`
  (`event_id`, `parent_event_id`, `started_event_id`, `emission_sequence`),
  `crewai/events/types/crew_events.py`,
  `crewai/events/types/flow_events.py` (`HumanFeedbackRequestedEvent`,
  `FlowPausedEvent`, ...) and `crewai/events/types/agent_events.py` /
  `task_events.py`. The official pages were used for the semantics:
  <https://docs.crewai.com/en/concepts/event-listeners> and
  <https://docs.crewai.com/en/concepts/flows>.
- **Simulated, not run against the real runtime.** The tests
  ([`tests/test_frameworks_graph.py`](../tests/test_frameworks_graph.py)) hand
  fake bus events to the adapter against the mock panel. No live crew or flow
  was kicked off against a real `crewai` install here.

Limitations:

- One listener observes the whole process; it cannot see events of a crew
  running in another process.
- `human_feedback_requested` / `flow_paused` events carry a `request_id` only
  when the configured feedback provider supplies one. Without it the adapter
  keys the wait by the event id (or by flow and method for pauses) and
  remembers which method owns it, so the matching `*_received` /
  `method_execution_started` event resolves that specific wait rather than
  every open one.
- A flow that is paused and later resumed may emit a fresh `flow_started` with
  a new event id, which would claim a new lane. Pass `session="<id>"` to
  `listen` / `CrewAIReporter` to pin every `flow_started` to that lane, so the
  resumed flow lands where it was.
- Task/agent children are keyed by `task_id` / `agent_id` when the event has
  one, so a task's start and completion pair up; events without ids fall back
  to the event id.
- CrewAI emits `crew_train_*` and `crew_test_*` lifecycles on the same bus;
  they are treated as root runs too.
- Everything the adapter publishes goes through the reporter's scrubbing:
  prompts, task descriptions and raw outputs are never forwarded; the wait
  message is bounded and scrubbed.
- The lane stays visible until its reporter is released (`close()`,
  `reporter.end()`) or the panel reclaims the slot.
- Token usage is not read from crew events; call `run.reporter.info(...)` if
  you need it.
