# HTTP API

The panel is a small JSON service. Everything an agent (or a script) needs is
here. Requests and responses are `application/json`.

Every request needs the shared secret once the panel is reachable from anywhere
but localhost:

```
X-LED-Token: <token>
```

It comes from `--token`, `RGI_TOKEN`, or `~/.config/rgi/token` — in that order.
With no token configured the panel refuses nothing, so bind it to localhost if
you have not set one.

## POST /session/start

Claim a lamp.

```json
{"agent": "hermes", "sessionID": "job-123", "label": "nightly sync",
 "host": "kimi", "slot": 3}
```

| field | required | notes |
|---|---|---|
| `sessionID` | yes | unique and stable for the task |
| `agent` | no | defaults to `"agent"` |
| `label` | no | what the human reads; falls back to the session id |
| `host` | no | the machine; falls back to the caller's address |
| `slot` | no | ask for a specific lamp |

`200` → `{"slot": 3, "key": "3", "devices": ["sinowealth"]}`

`key` is the human-facing name of the lamp (`1`, `F5`, `zone-left`, …) and
`devices` lists the devices whose pool is deep enough to show that lane, so the
same lane can appear on more than one keyboard. Calling this twice with the same
`sessionID` returns the same lamp rather than claiming a second one.

## POST /session/state

Report a transition. Transitions only — every call repaints the device.

```json
{"sessionID": "job-123", "state": "working"}
```

| state | panel | use it for |
|---|---|---|
| `working` | steady green | busy |
| `done` | blinks white, then holds | finished |
| `blocked` | blinking red | needs a human |
| `error` | steady red | failed |
| `idle` | dim | claimed, nothing happening |

`blocked` also accepts `question`, `permission`, `pending`, `approval`,
`waiting`. A `blocked` lamp blinks until a different state arrives.

`200 {"ok": true}`, or `404` if the session is not holding a lamp.

## POST /session/end

```json
{"sessionID": "job-123"}
```

Releases the lamp. Always idempotent, always `200 {"ok": true}`.

## POST /clear

Releases every lamp. For a human cleaning up, not for agents.

## POST /session/info

Detail for a lane: never painted, never a repaint, only what `/status` and the
editor sidebar show. Send it when it changes.

```json
{"sessionID": "job-123", "ident": "hermes-3",
 "info": {"repo": "rgbagentflightindicator", "branch": "main",
          "tokens": {"input": 2400000, "output": 940000, "cache_read": 800000000,
                     "cost": 4.12},
          "context": {"entries": 473, "compactions": 3, "percent": 42.5, "limit": 128000},
          "blocked_on": {"action": "bash", "resources": ["rm -rf /tmp"],
                         "message": "delete the build directory?"},
          "children": [{"id": "ses_child", "label": "subagent task",
                        "state": "working", "tokens": 6710}]}}
```

`200 {"ok": true}`, `404` if the session holds no lane, `400` if there is nothing
to record. Fields merge recursively one level, so a partial update keeps the rest;
sending `null` deletes a field. Loose top-level fields work too
(`{"sessionID": "job-123", "repo": "x"}`) for agents in a hurry.

`ident` is special: it is *who the agent is*, so it may also be sent when claiming
the lane (`{"sessionID": …, "ident": "hermes-3"}`), it is stored as a first-class
field rather than loose detail, and setting only it is a valid request. It is the
name the lane shows; the harness it runs in and the host are part of the detail
lines, visible only when a lane is uncollapsed.

The shared reporter every integration uses (`rgi/report.py`) adds two ordinary
conventions on top of that, and reading them is how a dashboard tells "waiting
for a human" from "gone": `pending_requests` is the list of unresolved
approval/input ids, with `blocked_on` describing the first of them, and
`heartbeat` is a timestamp written during a quiet wait to prove the lane is still
alive. Nothing enforces either name; they are just the fields the integrations
agree on.

By default the name is the machine's: `rgi watch` claims every local session under
`RGI_IDENT`, then `~/.config/rgi/name`, then the hostname. An `ident` sent at claim
time is only used when the lane is created — to rename a lane that already exists,
send `ident` to `POST /session/info`.

`children` is how subagents are shown: a child session should not claim its own
lamp, but listing it here puts it under its parent's lane, indented, with its own
state mark. The editor shows all of this when a lane is uncollapsed (click it; the
block's title collapses or expands every lane).

## GET /status

```json
{
  "devices": [
    {"label": "evision", "backend": "evision", "lamps": 126, "per_lamp": true,
     "lanes": ["0", "1", "2"]},
    {"label": "sinowealth", "backend": "sinowealth", "lamps": 126, "per_lamp": true,
     "lanes": ["`", "1", "2"]}
  ],
  "backend": "evision",
  "lamps": 126,
  "lanes": 12,
  "lane_map": {"hermes-3": 5, "opencode": 1},
  "free": [2, 4, 5],
  "sessions": {
    "job-123": {"slot": 1, "key": "1", "agent": "hermes", "label": "nightly sync",
                "host": "kimi", "ident": "hermes-3", "state": "working",
                "age": 12.3, "in_flight_s": 84.2, "idle_s": 3.1,
                "info": {"repo": "nightly", "running": [{"tool": "bash", "detail": "npm test"}]}}
  }
}
```

The three times mean different things, and they are easy to confuse:

| field | meaning |
|---|---|
| `age` | how long this session has held the lane |
| `in_flight_s` | how long the **current action** has been running. `null` for `done`, `error` and `idle` — a landed agent is not in flight — and it restarts from zero when the next action begins. `blocked` counts, because waiting is unfinished, not landed |
| `idle_s` | how long since the lane last reported anything, and `null` **while it is in flight**. In flight and idle are mutually exclusive: a running action is not idle, and a landed lane is not flying, so exactly one of the two is ever present |

`backend`, `lamps` and `key` describe the **primary** device (the first one);
`devices` describes all of them. A lane's `key` is whatever that lamp is called on
the primary device, so it differs between boards — the `slot` is the stable
identifier. `lane_map` is the configured agent→lane policy, so an agent can see
the rule it is subject to.

## GET /slots

Every lamp of the **primary** device in order, occupied or not — what a remote
agent reads before deciding which lamp to ask for. `lane` is the lane slot a lamp
is carrying, so you can compare layouts across devices.

```json
{
  "backend": "evision",
  "device": "evision",
  "free": [2],
  "slots": [
    {"slot": 0, "key": "0", "group": "unmapped", "lane": 0, "free": false,
     "sessionID": "job-123", "agent": "hermes", "label": "nightly sync",
     "host": "kimi", "state": "working", "age": 12.3},
    {"slot": 1, "key": "1", "group": "unmapped", "lane": 1, "free": true}
  ]
}
```

## GET /session/<sessionID>

"Which lamp am I in?" — and whether you still hold one.

```
200  {"found": true, "sessionID": "job-123", "slot": 3, "key": "3", "agent": "hermes",
      "label": "nightly sync", "host": "kimi", "state": "working", "age": 12.3}

404  {"found": false, "sessionID": "job-123",
      "hint": "not holding a lamp - POST /session/start"}
```

A `404` is a useful signal: you never claimed a lamp, you released it, or the
panel was restarted. Claim again if you still need one.

## GET /files and GET /files/\<name\>

The OpenCode plugin and the agent prompts, so a machine can fetch the current
copy instead of being handed one that drifts.

```sh
curl -sS -H "X-LED-Token: $TOKEN" http://panel:8730/files
curl -sS -H "X-LED-Token: $TOKEN" http://panel:8730/files/AGENT_PROMPT.md
```

## Errors

| code | meaning |
|---|---|
| `400` | the body was not valid JSON, or a field is wrong; the response says which, and lists the states that are accepted |
| `401` | missing or wrong `X-LED-Token` |
| `404` | no such session (or unknown path) |
| `409` | no free lamp — or the `slot` you asked for is taken; the response lists what is free |

Bad requests are refused with a reason rather than half-applied: an early version
accepted a body with no `sessionID` and quietly claimed a lamp called `unknown`.

## Behaviour worth knowing

- **Lamps are scarce.** A keyboard has a dozen. A lane is released explicitly, or
  evicted as least-recently-used when a new session needs one and nothing is
  free. The daemon has no idle timeout of its own; the OpenCode watcher releases
  sessions it has not seen for `rgi watch --stale` seconds (default two hours).
- **An explicit `slot` request is never resolved by evicting someone else** — you
  get an honest `409` with the free list.
- **The frame is written only when the rendered result changes.** Most
  controllers repaint everything on any write, so this is what keeps the panel
  from flashing.
- **While the human types, nothing is written.** Some firmware drops keypresses
  while it is busy repainting, so a typing burst means total silence: a lane
  change that lands in it - a flash, a new lane - is held and painted in one
  frame once typing pauses. `--no-quiet` turns this off; `--quiet-ms` (default
  1500) is how long "typing" lasts after the last keystroke.

## The web UI API

Served by the same daemon, same origin, behind the same `X-LED-Token`. The
static shell (`/ui/`, `/ui/<asset>`) carries no data and needs no token; every
API call does. Full usage and the config schema are in
[docs/webui.md](webui.md); the route table is:

```
GET    /ui/                      the shell (static assets under /ui/<asset>)
GET    /ui/api/status            /status shape + revision + active test overlays
GET    /ui/api/config            effective config + device capabilities
PUT    /ui/api/config            validate, write atomically, apply (If-Match: revision)
POST   /ui/api/validate          field-level validation without writing
GET    /ui/api/defaults          the built-in config, for resets
GET    /ui/api/profiles          packaged device profiles (metadata only)
GET    /ui/api/profiles/<id>     one profile including its layout
POST   /ui/api/test              time-boxed hardware overlay (one device)
DELETE /ui/api/test              cancel overlays, restore lane rendering
POST   /ui/api/paint             short mapping probe; consumed by the render loop
GET    /ui/api/events            coalesced snapshot stream (text/event-stream)
GET    /ui/api/health            per-device status and configured endpoints
GET    /ui/api/logs?since=N      the daemon's in-memory ring buffer
GET    /ui/api/aggregate         one aggregate state plus every blocked wait
GET    /ui/api/history?since=N   the transition history ring (JSONL on disk)
POST   /ui/api/agents/<id>/test  start, land and release a synthetic test lane
POST   /session/ack              mark a lane seen: done dims, blocked stops blinking
POST   /hook/<source>            native hook/webhook payloads for the generic ingest
POST   /ingest                   {"source": ..., ...} - the same, source in the body
POST   /v1/metrics|logs|traces   OTLP/HTTP JSON telemetry (otlp must be enabled)
```

Errors use one envelope with a field path where one exists:
`{"error": {"code", "message", "field?", "hint?", "details?"}}`. A `PUT` with a
stale `If-Match` is a `409 revision_conflict`; validation failures are `422`
with every field path listed; the config file is never partially written. The
HTTP layer never writes a backend directly — overlays are consumed by the one
render loop.
