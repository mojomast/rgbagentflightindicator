# LangGraph threads

LangGraph checkpoints a run under a **thread**: the same `thread_id` resumes the
same paused graph, and `interrupt()` pauses that thread waiting on a human. The
adapter in [`rgi/integrations/langgraph.py`](../rgi/integrations/langgraph.py)
therefore treats the thread id as the lane's identity, so a run that pauses,
ends its stream, and is resumed later by a `Command(resume=...)` lands on the
same lane instead of claiming a second one. A runnable walkthrough is in
[`examples/langgraph_demo.py`](../examples/langgraph_demo.py).

**Scope, stated plainly:** the adapter observes a graph run *in your process*.
You feed it the chunks you stream (`run.observe(chunk)`), or pass its callback
handler in the run config and let LangGraph call it. It does not attach to
another process's graph, and it does not read checkpoints on its own.

## What it reports

| LangGraph surface | panel |
|---|---|
| root run started (any stream chunk; `on_chain_start` with no parent) | `working` |
| `on_chain_end` for the root run, or the stream ending, with nothing pending | `done()` |
| `interrupt()` — `{"__interrupt__": (...)}` in `updates`/`values`, `interrupts` on v2 stream parts, `GraphCallbackHandler.on_interrupt` | `blocked(request=<interrupt id or a stable synthetic>)` |
| `Command(resume=...)`, `GraphCallbackHandler.on_resume` | resolves the open waits, `working` |
| an interrupt present when the root run ends | stays `blocked` — a paused graph is not complete |
| root `on_chain_error`, or an exception through `report_run` | `error(<message>)` |
| node runs and `stream_mode="tasks"` entries | `child()` / `child_done()` metadata only |
| `graph.get_state(config)` (`StateSnapshot`) | syncs the lane: pending interrupts block, empty `next` completes |

An interrupt only becomes a wait if it is *surfaced to the adapter*. That can be
a stream chunk, a v2 stream part, a v3 protocol event, `StateSnapshot`, a bare
`Interrupt`, or the lifecycle callback. All of them resolve to the same
`blocked(request=...)`.

## The two rules that matter

**A stream ending is not completion when the graph paused.** `graph.stream()`
returns normally when `interrupt()` fires; the run is *pending*, not finished.
The adapter only reports `done` when the root run ends with no open interrupt
(or the stream ends without one). While any interrupt is open the lane reads
`blocked`, and it stays that way across process restarts — pass
`pending=["<id>"]` to `report_run` to restore waits you already know about.

**The root owns the lamp; tasks are metadata.** Node runs seen through the
callback API and task entries from `stream_mode="tasks"` update the lane's
children. A node or task finishing never changes the lane's state.

## Usage

```python
from rgi.integrations.langgraph import report_run

thread_id = "research-42"                       # stable across resumes
config = {"configurable": {"thread_id": thread_id}}

with report_run(thread_id, label="research") as run:
    for chunk in graph.stream(input, config, stream_mode=["updates", "values"]):
        run.observe(chunk)                      # interrupts -> blocked
    # root done -> done; interrupt seen -> blocked

with report_run(thread_id, label="research") as run:   # same lane, later
    for chunk in graph.stream(Command(resume="approved"), config,
                              stream_mode=["updates", "values"]):
        run.observe(chunk)
```

`report_run(thread_id).config` saves you from repeating the config: it carries
the thread id *and* the adapter's callback handler, so resume and lifecycle
events reach the lane even if you do not observe every chunk:

```python
with report_run(thread_id) as run:
    graph.invoke(input, run.config)             # handler sees start/end/interrupt
```

If you drive `astream_events` yourself, pass each event to `run.observe`; the
adapter reads `event`, `run_id`, `parent_ids`, `metadata` and `data`, and
ignores callback events whose metadata names a different `thread_id` — so one
handler can safely be shared by concurrent threads.

`graph.get_state(config)` can be handed to `run.observe` as well: it is how a
fresh process learns that a thread is paused (`snapshot.interrupts`), or that
it already ran to completion (`snapshot.next` empty).

## Supported versions

- **Package:** `langgraph` **1.2.12** (PyPI, retrieved 2026-10-01); the
  callback base it depends on, `langchain-core` (`>=1.4.7`), was checked at
  **1.6.6**. Release: <https://github.com/langchain-ai/langgraph/releases/tag/1.2.12>.
- **How verified:** the released wheels were downloaded from PyPI and the
  relevant source read: `langgraph/types.py` (the `Interrupt.id` field,
  `StateSnapshot.interrupts`, `Command(resume=...)`),
  `langgraph/callbacks.py` (`GraphCallbackHandler.on_interrupt` /
  `on_resume`), `langgraph/pregel/_loop.py` (interrupts emitted as
  `{"__interrupt__": (...)}` in `updates`/`values` and as `interrupts` on
  stream parts), and `langgraph/stream/transformers.py` (v3 `lifecycle` and
  `values` events). The official pages were used for the semantics:
  <https://docs.langchain.com/oss/python/langgraph/interrupts>,
  <https://docs.langchain.com/oss/python/langgraph/streaming>,
  <https://docs.langchain.com/oss/python/langgraph/persistence>.
- **Simulated, not run against the real runtime.** The tests
  ([`tests/test_frameworks_graph.py`](../tests/test_frameworks_graph.py)) drive
  fake chunks, callback events and lifecycle payloads against the mock panel.
  No graph was executed against a live `langgraph` install here.

Limitations:

- The adapter observes; it never resumes a graph itself and never answers an
  interrupt. `Command(resume=...)` stays your call.
- Interrupts without an `id` (or non-`Interrupt` payloads) get a synthetic id
  derived from the thread id and the payload. It is stable across repeats of
  the same payload, but it is not a LangGraph resume id — resume with the real
  `Command(resume=...)` value, not that string.
- `on_resume` lifecycle callbacks exist in LangGraph 1.2's
  `GraphCallbackHandler`; on older releases the adapter still blocks from
  stream interrupts, but a resume is only noticed when the next chunk,
  `Command`, or root run start arrives.
- A resume resolves *every* open interrupt on the thread. With several
  concurrent interrupts, answer them individually with `run.resolve(id)` if
  only one was resumed; LangGraph re-surfaces anything still pending at the
  next pause.
- Callback events do not carry timestamps, so same-lane ordering follows
  arrival order. Events from another `thread_id` are ignored, not reported as
  another lane — the lane is created by `report_run`, never by an event.
- Internal LangChain runs tagged `langsmith:hidden` are ignored so the child
  list stays readable; other non-root chain runs appear as children.
- `stream_mode="debug"` task wrappers are handled; unrelated debug entries
  (checkpoints, step timing) are ignored.
- The lane stays visible in `done`/`blocked` until the panel releases it or
  the process ends; `run.reporter.end()` releases it explicitly.
- Token usage is not read from LangGraph state. Publish it yourself with
  `run.info({"tokens": {...}})` if the graph tracks it.
