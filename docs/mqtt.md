# MQTT with Home Assistant discovery

The [Home Assistant bridge](home-assistant.md) publishes over HA's REST API: it
polls, and its entities disappear on a Home Assistant restart. `rgi/mqtt.py` is
the other path. It keeps a connection to a broker, publishes retained state and
Home Assistant **MQTT discovery** configs, and adds a Last Will so an ungraceful
death reads as `offline` instead of a stale `working`.

MQTT is optional and isolated:

- `paho-mqtt` is imported lazily. With the package missing, `open()` raises one
  clear `RuntimeError` for the daemon to log once; nothing else changes.
- Every publish is compared by digest first (the same compare-then-publish rule
  as the REST bridge), so an unchanged lane writes nothing.
- Subscribers and command handlers never raise into the event hub, and a
  publish failure is logged once and retried on the next change.

```
pip install paho-mqtt
```

## Configuration

| key | default | meaning |
| --- | --- | --- |
| `enabled` | `false` | the integrator only constructs the publisher when true |
| `host` | `127.0.0.1` | broker address |
| `port` | `1883` | broker port |
| `username` / `password` | `""` | optional credentials |
| `base_topic` | `rgi` | prefix for state, availability and command topics |
| `discovery` | `true` | publish Home Assistant discovery configs |
| `tls` | `false` | enable TLS with the system CA store |

The publisher is constructed with the event hub, the daemon's status callable
and an identity, plus optional `on_clear(session)` / `on_ack(session)` callbacks:

```python
from rgi.events import EventHub
from rgi.mqtt import MqttPublisher

hub = EventHub()
publisher = MqttPublisher(
    hub,
    daemon.status_payload,          # returns {"sessions": {sid: lane_view}}
    "workstation",
    host="homeassistant.local",
    on_clear=daemon.clear_session,  # injected: the publisher never imports the daemon
    on_ack=daemon.ack_session,
)
try:
    publisher.open()
except RuntimeError as exc:         # paho-mqtt not installed
    log(str(exc))
...
publisher.stop()                    # publishes a retained offline first
```

## Topics

With `base_topic = "rgi"` and `ident = "workstation"`:

| topic | retained | payload |
| --- | --- | --- |
| `rgi/workstation/availability` | yes | `online`, with LWT `offline` |
| `rgi/workstation/state` | yes | aggregate, `{"state": …, "total": …, "counts": {…}}` |
| `rgi/workstation/lane/<slot>/state` | yes | one lane, `{"state": …, "slot": …, "key": …, "label": …, "agent": …, "ident": …, "host": …, "in_flight_s": …}` |
| `homeassistant/sensor/workstation/aggregate/config` | yes | discovery config |
| `homeassistant/sensor/workstation/lane_<slot>/config` | yes | discovery config |
| `rgi/workstation/command/clear` | — | subscribe: `{"session": "…"}` |
| `rgi/workstation/command/ack` | — | subscribe: `{"session": "…"}` |

The aggregate takes the **most urgent** lane state in the order
`blocked > error > working > done > idle > offline`; an empty panel is `idle`,
and a lane whose session ends is published as a retained `offline` so it stops
looking active. The `counts` object has one entry per state plus `other`.

Only scalar lane fields are published. The lane's `info` object — prompts,
transcripts, tool arguments, cost detail — never reaches the broker.

## Home Assistant discovery

On every successful connect the publisher republishes availability, discovery
and a full snapshot of lanes, so a Home Assistant or broker restart heals
itself. Each discovery config carries `unique_id`, `state_topic`,
`json_attributes_topic`, `availability_topic`, and a `device` block grouping
all of the panel's entities under one device:

```json
{
  "name": "RGI workstation",
  "unique_id": "rgi_workstation_aggregate",
  "state_topic": "rgi/workstation/state",
  "json_attributes_topic": "rgi/workstation/state",
  "value_template": "{{ value_json.state }}",
  "availability_topic": "rgi/workstation/availability",
  "payload_available": "online",
  "payload_not_available": "offline"
}
```

The entities appear under **Settings → Devices & Services → MQTT** without any
YAML. Enable "retain" cleanup in your broker if you remove a panel.

## Commands

The publisher subscribes to `.../command/clear` and `.../command/ack` and
accepts a JSON object with a `session` key (a bare session string also works).
The callbacks are injected, so the module never imports the daemon:

```yaml
# Home Assistant automation: acknowledge one known session
- alias: Ack the overnight review lane
  triggers:
    - trigger: time
      at: "09:00:00"
  actions:
    - action: mqtt.publish
      data:
        topic: rgi/workstation/command/ack
        payload: '{"session": "opencode:review-2026-10-02"}'
```

A malformed payload is logged and ignored; a raising callback is caught and
logged. Whether an ack clears the lamp or just silences the notifier is the
daemon's policy, not the publisher's.

## Reconnect and failure behaviour

- `reconnect_delay_set(1, 60)` configures paho's exponential backoff, and the
  network loop started by `open()` retries the first connection too. Every
  reconnect republishes everything.
- A refused broker is logged, `open()` returns `False`, and the daemon continues
  without MQTT.
- `stop()` publishes a retained `offline`, disconnects and stops the loop, so an
  intentional shutdown does not leave a stale `online` topic behind.
- The tests drive a fake client: no broker, network or hardware is involved.

## Limits

- One aggregate plus one sensor per claimed lane (at most the lane count). Lane
  configs for released slots remain retained but the state is `offline`; delete
  them from the broker if that bothers you.
- Discovery is Home Assistant's spec, re-implemented here; it is tested against
  a fake client, not against a live broker.
- TLS uses Python's default verification, with no certificate pinning.
- The publisher is an observer: it holds no lane and never changes panel state.
  Clear/ack exist only because injected callbacks make them explicit.
