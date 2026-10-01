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

`200` → `{"slot": 3, "key": "3", "group": "number-row"}`

`key` is the human-facing name of the lamp (`1`, `F5`, `zone-left`, …) and
`group` is the pool it belongs to. Calling this twice with the same `sessionID`
returns the same lamp rather than claiming a second one.

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

## GET /status

```json
{
  "backend": "sinowealth",
  "lamps": 126,
  "free": [2, 4, 5],
  "sessions": {
    "job-123": {"slot": 1, "key": "1", "agent": "hermes", "label": "nightly sync",
                "host": "kimi", "state": "working", "age": 12.3}
  }
}
```

## GET /slots

Every lamp in order, occupied or not — what a remote agent reads before deciding
which lamp to ask for.

```json
{
  "backend": "sinowealth",
  "free": [2],
  "slots": [
    {"slot": 1, "key": "1", "group": "number-row", "free": false,
     "sessionID": "job-123", "agent": "hermes", "label": "nightly sync",
     "host": "kimi", "state": "working", "age": 12.3},
    {"slot": 2, "key": "2", "group": "number-row", "free": true}
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
  free, or after `--stale` seconds of inactivity (default two hours).
- **An explicit `slot` request is never resolved by evicting someone else** — you
  get an honest `409` with the free list.
- **The frame is written only when the rendered result changes.** Most
  controllers repaint everything on any write, so this is what keeps the panel
  from flashing.
- **While the human types, colours freeze.** Some firmware drops keypresses while
  it is busy repainting. `--no-quiet` turns this off, `--quiet-ms` tunes it.
