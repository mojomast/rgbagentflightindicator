# Coder AgentAPI (universal CLI watcher)

[coder/agentapi](https://github.com/coder/agentapi) is an adapter of adapters:
it runs one coding CLI in an in-memory terminal (PTY) or over ACP and puts the
same two HTTP endpoints in front of every one of them. One watcher against one
AgentAPI instance therefore covers the agents this project would otherwise need
a separate adapter for:

> Aider, Goose, Amp, Auggie, Claude Code, Codex, Gemini, Amazon Q, OpenCode,
> GitHub Copilot and Cursor CLI — the eleven the upstream README lists.

This is an **optional, opt-in** watcher: it only runs when a human points it at
a running AgentAPI server. Machines that use hooks or a native adapter never
need it.

## Usage

Start AgentAPI with whichever agent you want a lamp for:

```sh
agentapi server -- claude
agentapi server --type=codex -- codex
agentapi server -- aider --model sonnet
agentapi server -- goose
```

Then watch the instance, standalone:

```sh
python -m rgi.watchers.agentapi --url http://127.0.0.1:3284 --name claude
python -m rgi.watchers.agentapi --url http://127.0.0.1:3284 --name goose --label "goose"
```

AgentAPI's default port is 3284. The panel address and token resolve exactly as
every other rgi client: `RGI_URL`/`RGI_TOKEN`, then `~/.config/rgi/url` and
`~/.config/rgi/token`. Flags:

| flag | meaning |
|---|---|
| `--url` | AgentAPI base url (default `RGI_AGENTAPI_URL`, then `http://127.0.0.1:3284`) |
| `--name` | lane name (default: the URL's host:port) |
| `--label` | label to show on the lane |
| `--panel-url` | panel address override |
| `--token` | panel token override |
| `--ident` | name this machine's lanes carry |
| `--poll` | seconds between control ticks while the stream is quiet |

Programmatically, it is a `start()`/`stop()` watcher around the shared
[reporter](../rgi/report.py), so no panel details leak into it:

```python
from rgi.report import Reporter
from rgi.watchers.agentapi import AgentApiWatcher

reporter = Reporter("agentapi", "claude", url="http://127.0.0.1:8730")
watcher = AgentApiWatcher("http://127.0.0.1:3284", reporter=reporter,
                          label="claude via AgentAPI")
watcher.start()          # one control thread; watcher.stop() releases the lane
```

The `rgi watch --agentapi URL` flag is expected to build exactly this. One
watcher watches one AgentAPI instance; run several, with distinct `--name`
values, to cover several.

## What it reports

AgentAPI exposes `GET /status` as `running` or `stable`, and `GET /events` as
Server-Sent Events. The released schema (0.12.2) names the events
`status_change`, `message_update` and `agent_error`; earlier notes call the same
things `message` and `turn_completed`. The parser accepts every spelling, and
NDJSON line-per-event streams, so a schema drift upstream costs fidelity at
worst, never the watcher.

| wire signal | lane |
|---|---|
| `status_change` / `status` `running` | *in flight* |
| `turn_completed` | *complete* |
| `status_change stable` after a turn | *complete* briefly, then *idle* |
| `status_change stable` with no turn seen | *idle* |
| `turn_completed` with token usage | the numbers land as `tokens.input`, `tokens.output`, `tokens.total`, `tokens.cache_read`, `tokens.cost` |
| `agent_error` with a message | *needs attention*, with the message |
| `agent_error` with `level: "warning"` | lane detail, not a red lamp |
| connection lost | **no state change** — logged once, reconciled on reconnect |
| `message` / `message_update` | no state change; message content is never forwarded |

`agent_error` is an extension beyond the mapping in the research notes: the
event is in the released schema and an agent that says it errored is worth a
red lamp. The next state event recovers the lane.

## Identity and lifecycle

The lane key is `agentapi:<name>`, where `<name>` is `--name` or the URL's
host:port. If the wire carries a `session_id` (some versions do; the released
`/status` body does not), the key becomes `agentapi:<name>:<id>`, so an
AgentAPI process that restarts with a fresh session gets a fresh lane instead
of wearing the old conversation's number. A lane that has been idle for two
hours is released, and re-claimed when the agent works again — the lamp means
the session, and it is not handed to somebody else while a turn is live.

A blocked lane is never released: the switch to a new session id, the idle
timeout and shutdown all check the reporter's pending waits before ending a
lane. Connection loss is deliberately not a state change — AgentAPI may be
restarting while the turn continues — and on reconnect the watcher asks
`/status`, re-claims through `heartbeat()` if the panel restarted too, and
repaints.

## Design notes

- **One control thread, one short reader.** The control thread ticks every
  `--poll` seconds while the stream is quiet (done→idle grace, liveness
  heartbeat, stale release). The stdlib HTTP stream cannot survive a socket
  read timeout, so each `/events` connection is read by a short-lived reader
  thread that hands lines to the control loop; `stop()` wakes it with a socket
  `shutdown`.
- **Tolerant parsing.** SSE comments, `id`/`retry` fields, blank `data` lines,
  malformed JSON, unknown event names and oversized lines are all ignored; a
  bad frame cannot stop the stream.
- **Nothing forwarded by default.** Message content is not published. The only
  strings that reach the panel are `agent_type`, `transport`, the error message
  (scrubbed) and the token counts.
- **Every failure is swallowed.** Short timeouts on `/status`, exponential
  backoff on `/events`, and no raise into the caller; a panel or AgentAPI that
  is down costs the indicator, never the agent.

## Troubleshooting

- **`could not read /events`** — check the URL; AgentAPI's default is
  `http://127.0.0.1:3284`, and its own allowed-hosts check rejects requests
  whose `Host` header is not localhost unless it was started with
  `--allowed-hosts`.
- **nothing on the panel** — the panel may be full or the token wrong; the log
  says which. Check `python -m rgi doctor`.
- **the lane sits idle while the agent works** — the agent's TUI may have
  changed under AgentAPI's PTY parser and it never emitted `status_change
  running`. Upstream owns that; the watcher only reports what it hears.
- **two watchers, one instance** — same `--name` means the same lane; the
  second watcher adopts it rather than duplicating it, but running two is still
  not recommended. Run one watcher per AgentAPI instance.

## Supported versions

- **AgentAPI:** OpenAPI schema **0.12.2**, archived by Coder on **2026-09-13**
  (<https://github.com/coder/agentapi>); the repository is MIT and read-only.
  The deprecation is real: expect no further fixes upstream. The watcher still
  works and is small; treat it as optional and opt-in, exactly as the research
  notes recommend.
- **The wire format was read from the released schema, not guessed.**
  `GET /status` -> `{agent_type, status: "running"|"stable", transport:
  "acp"|"pty"}` and the `/events` oneOf (`agent_error`, `message_update`,
  `status_change`) come from
  [`openapi.json`](https://github.com/coder/agentapi/blob/main/openapi.json) at
  `main`, retrieved 2026-10-02. `turn_completed` and the `message` spelling come
  from the state-ingestion research notes; the parser accepts both so a schema
  from either side of that drift works.
- **Simulated, not run against a live agent.** The tests drive a localhost mock
  of `/status` and `/events` with a scripted SSE sequence (stable -> running ->
  turn_completed -> stable), a reconnect/reconcile case, and malformed frames,
  against the repo's mock panel
  ([`tests/test_agentapi.py`](../tests/test_agentapi.py)). No live AgentAPI
  process, no real CLI and no provider was exercised, and there is no hardware
  in the loop.
- **Not wired into `rgi watch` here.** `main()` is importable and runnable as
  above; the `--agentapi` flag is the integrator's edit to `rgi/cli.py`.

Limitations:

- No `blocked` signal. AgentAPI detects human waits indirectly from the PTY and
  does not expose one on `/status`; this watcher will not guess, so a waiting
  agent shows as `stable`/idle rather than red. Harnesses with real hooks
  (Claude Code, Gemini CLI, Copilot) should keep using those for approvals.
- PTY parsing fidelity is upstream's: AgentAPI's own README concedes it needs
  occasional updates when a TUI changes.
- `session_id` is treated as optional. When no version sends one, every restart
  of the same AgentAPI instance reuses the same `agentapi:<name>` lane.
- Usage is only reported when an event actually carries it; most agents print
  cost in the TUI and AgentAPI does not scrape numbers out of it.
- `--agentapi` watches one instance per process; there is no discovery of
  running AgentAPI servers.
