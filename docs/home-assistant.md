# Home Assistant

The Home Assistant bridge runs next to the agents, reads the panel's `/status`
through the shared reporter, and publishes one aggregate entity plus transition
events through Home Assistant's authenticated REST API. It is outbound only: the
bridge does not need Home Assistant to reach the panel machine, install a
custom component, or open any port.

The important API distinction, taken from Home Assistant's own documentation:

- `POST /api/states/<entity_id>` **sets a state representation**. It creates or
  updates what Home Assistant believes, and explicitly does not communicate
  with a device. The bridge uses this for `sensor.rgi_panel`.
- `POST /api/services/<domain>/<service>` **calls a service**, which is how an
  automation actually operates a device (turn a light on, send a notification).
- `POST /api/events/<event_type>` fires an event that automations can trigger
  on. The bridge uses this for `rgi_status_changed`.

See [the examples](#the-examples) for all three patterns.

## Setup

### 1. Create a long-lived access token

Log in to Home Assistant in a browser, open your profile
(`http://homeassistant.local:8123/profile`), and use **Long-Lived Access
Tokens** to create one. Home Assistant documents these as valid for 10 years and
shows the value only once, so store it before closing the dialog. Delete the
token on the profile page to revoke it.

### 2. Point the bridge at Home Assistant

Either export the two variables:

```sh
export RGI_HA_URL="http://homeassistant.local:8123"
export RGI_HA_TOKEN="<token>"
```

or write a JSON file at `~/.config/rgi/home-assistant.json`:

```json
{
  "url": "http://homeassistant.local:8123",
  "token": "<token>"
}
```

That file lives **outside the repository** and must never be committed: a
long-lived token can call the whole Home Assistant API. `RGI_HA_CONFIG` selects
a different path; `~/.config/rgi/` is where the rest of the panel's local
configuration already lives. Precedence is explicit `--url`, then the
environment, then the file. There is deliberately no `--token` flag, so a
secret cannot leak into a process list or a shell history.

### 3. Run it

From the repository root (the package must be importable, e.g. installed with
`pip install -e .`):

```sh
python -m rgi.integrations.home_assistant
```

Useful options:

| option | meaning |
| --- | --- |
| `--url URL` | Home Assistant base URL (overrides the environment and file) |
| `--config PATH` | credential file to read |
| `--entity ID` | aggregate entity, default `sensor.rgi_panel` |
| `--event TYPE` | transition event, default `rgi_status_changed` |
| `--interval N` | seconds between polls, default 5 |
| `--timeout N` | seconds per HTTP request, default 2 |
| `--once` | poll once and exit (use it from a system timer) |

Diagnostics and errors go to **stderr** only, and the process always exits 0:
the bridge is an indicator, never the reason a workflow stops. The panel side
uses `RGI_URL`/`RGI_TOKEN` exactly like every other integration; the Home
Assistant side uses the variables above.

### 4. Check it

Open **Developer Tools → States** in Home Assistant and search for
`sensor.rgi_panel`, or ask the API directly:

```sh
curl -H "Authorization: Bearer <token>" http://homeassistant.local:8123/api/
```

A healthy API answers `{"message": "API running."}`. The bridge calls the same
endpoint on startup to validate the URL and token before publishing anything.

## The entity and event contract

`sensor.rgi_panel` has one of four states:

| state | when |
| --- | --- |
| `attention` | at least one lane is `blocked` or `error` |
| `working` | at least one lane is in flight (`working`, `stopping`) |
| `idle` | the panel answered and nothing is blocked or in flight |
| `offline` | the panel's `/status` did not answer (daemon down, timeout, bad panel token) |

The attributes are bounded by design:

```json
{
  "state": "attention",
  "attributes": {
    "friendly_name": "RGI panel",
    "total": 2,
    "counts": {"idle": 0, "working": 0, "blocked": 1, "done": 0, "error": 1, "other": 0},
    "sessions": [
      {"key": "opencode:fix-tests", "name": "Fix tests", "state": "blocked", "slot": 0, "label": "Fix tests"},
      {"key": "claude-code:docs", "name": "docs", "state": "error", "slot": 4, "label": "docs"}
    ]
  }
}
```

- `total` and `counts` cover every session the panel reports (`other` catches
  any state outside the normal vocabulary).
- `sessions` is truncated to 20 entries, sorted by lane, and each entry has
  exactly `key`, `name`, `state`, `slot` and `label`. Prompts, transcripts,
  tool arguments and other lane detail are never forwarded.
- While the panel is `offline`, the counts are zero and the list is empty -
  an unavailable panel must not look like an idle one.

The bridge also fires `rgi_status_changed` with a payload identical to the one
it posts to the entity (`state` plus `attributes`). It fires only when the
aggregate state or its published attributes change; the first state published
after startup is a baseline, not a transition. Publications are compared by a
SHA-256 hash of the payload, so an unchanged poll sends nothing, and a failed
state post is retried with exponential backoff (capped at 60 seconds) until
Home Assistant accepts it.

## State updates are not service calls

The bridge only ever writes. It never reads back `sensor.rgi_panel`, never
subscribes to `rgi_status_changed`, and never calls a service, so it cannot
fight an automation or react to its own writes. An automation is the right
place to act:

- trigger on the entity state (`sensor.rgi_panel` changing to `attention`) or
  on the `rgi_status_changed` event;
- then call a service such as `light.turn_on` or
  `notify.persistent_notification` to operate a physical device or notify a
  person.

## The examples

Three files under [`examples/home_assistant/`](../examples/home_assistant/) show
the contract end to end:

- [`dashboard.yaml`](../examples/home_assistant/dashboard.yaml) is a YAML-mode
  Lovelace dashboard. Its markdown cards read `counts` and `sessions` from the
  aggregate attributes, so the list of active sessions follows the entity
  without creating one entity per session.
- [`desk_light.yaml`](../examples/home_assistant/desk_light.yaml) turns a desk
  light into a copy of the panel. A state trigger on `sensor.rgi_panel` chooses
  a colour and brightness, and the action is a `light.turn_on` **service call** -
  the bridge never touches the lamp. The `default` branch covers `offline`.
- [`attention.yaml`](../examples/home_assistant/attention.yaml) triggers on the
  `rgi_status_changed` event with `event_data: {state: attention}` and sends a
  persistent notification listing the blocked or failed sessions. Replace the
  notification service with a mobile app one to reach a phone.

Copy them into YAML-mode dashboards / `automations.yaml`, or adapt them into
packages. Entity ids such as `light.desk` are placeholders.

## Security notes

- The long-lived token grants full API access. Keep it in the environment or in
  `~/.config/rgi/home-assistant.json`, never in the repository, a command line,
  or a log. The bridge redacts the token from every message it could print and
  refuses HTTP redirects so the bearer header is never forwarded to another
  host.
- Use `https://` when Home Assistant is not on the same trusted host. The
  bridge uses Python's standard TLS verification; there is no certificate
  pinning.
- Rotating the token is enough to cut the bridge off; it logs the resulting 401
  once and keeps retrying with backoff.

## Supported versions

| package | version verified | how |
| --- | --- | --- |
| Home Assistant Core | 2026.9 (stable 2026-09-02; 2026.9.3 latest patch on PyPI, 2026-10-01) | official developer docs: [REST API](https://developers.home-assistant.io/docs/api/rest/) and [Authentication API](https://developers.home-assistant.io/docs/auth_api/), retrieved 2026-10-01 |
| Python (bridge) | 3.10+ | standard library only; tested with `unittest` against a local mock of the documented REST surface |

The endpoint shapes used here (`GET /api/`, `POST /api/states/<entity_id>`,
`POST /api/events/<event_type>`, `Authorization: Bearer TOKEN`, long-lived
access tokens) come from those two official pages. **The tests are simulated,
not run against the real runtime**: `tests/test_home_assistant.py` drives the
bridge against a localhost mock that implements exactly those documented
routes, plus `tests/mock_panel.py` for the panel side.

### Limitations

- Simulated, not validated against a real Home Assistant instance: no real
  WebSocket API, recorder, automation engine or mobile app was exercised.
- Only three HA endpoints are used. There is no WebSocket subscription, no
  discovery, no config flow and no custom component.
- States created through `POST /api/states/<entity_id>` are not backed by a real
  entity and do not survive a Home Assistant restart. The bridge heals a failed
  state post on its next poll, and republishes whenever the payload changes; a
  restart while the payload is unchanged and no request failed is healed by
  restarting the bridge (or running it with `--once` from a timer).
- One aggregate entity only. The session list is truncated at 20 entries;
  counts always cover all sessions.
- Polling only (default every 5 seconds). Transition events are best-effort and
  only on change, so a very short transition can be missed.
- No service calls by design: the bridge reports, it does not control devices.
- Bearer-token authentication only (no OAuth). The token is created manually
  and must be stored by the operator.
- Only `http`/`https` base URLs are accepted, and redirects are refused, so the
  configured URL must be the final one.
