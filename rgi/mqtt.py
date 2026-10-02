"""MQTT publisher with Home Assistant discovery - optional dependency, isolated.

The REST bridge (``rgi.integrations.home_assistant``) works without anything
extra, but it polls and its entities are transient. MQTT is the transport the
survey calls the best fit for a panel like this: retained state survives a Home
Assistant restart, a will message turns an ungraceful death into an honest
``offline``, and discovery means one broker connection creates every entity
without YAML.

This module publishes a small, bounded surface:

``<base>/<ident>/availability``
    retained ``online``, with a Last Will of ``offline``;
``<base>/<ident>/state``
    the aggregate: the most urgent lane state and a count per state;
``<base>/<ident>/lane/<slot>/state``
    one sensor per claimed lane, with display attributes and no lane detail;
``homeassistant/sensor/<ident>/.../config``
    retained discovery configs, so Home Assistant creates the entities itself;
``<base>/<ident>/command/clear`` and ``.../command/ack``
    JSON ``{"session": "..."}``, dispatched to injected callbacks.

The rules match the rest of the repo:

* ``paho-mqtt`` is imported lazily inside :meth:`MqttPublisher.open`; when it is
  missing a single clear ``RuntimeError`` is raised for the daemon to log, and
  nothing else changes. It is never a hard dependency.
* every publish is compared by digest first, so an unchanged lane writes
  nothing - the same compare-then-publish discipline as the HA bridge;
* subscribers and callbacks never raise into the event hub, and ``stop()``
  publishes a retained ``offline`` before disconnecting cleanly;
* only scalar lane fields reach the broker: labels and hosts, never prompts,
  transcripts, tool arguments or other ``info`` content.

Usage::

    publisher = MqttPublisher(hub, daemon.status_payload, "workstation")
    try:
        publisher.open()
    except RuntimeError as exc:      # paho-mqtt not installed
        logger(str(exc))
    ...
    publisher.stop()
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading

#: most urgent first; the aggregate takes the first state present
URGENCY = ("blocked", "error", "working", "done", "idle", "offline")

#: states the aggregate counts
STATES = ("blocked", "error", "working", "done", "idle")

#: what ``stop()`` publishes so a clean shutdown is not a stale ``online``
OFFLINE = "offline"
ONLINE = "online"

_SLUG = re.compile(r"[^A-Za-z0-9_-]+")

#: the command topics this publisher subscribes to, and their callbacks
COMMANDS = ("clear", "ack")


def slug(value: object, fallback: str = "rgi") -> str:
    """A topic- and unique_id-safe token: no ``/``, ``+`` or ``#``."""
    cleaned = _SLUG.sub("_", str(value or "").strip()).strip("_")
    return cleaned or fallback


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _default_client(client_id: str):
    """Build a paho client without importing paho at module scope."""
    import paho.mqtt.client as mqtt                # noqa: PLC0415 - lazy on purpose

    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except (AttributeError, TypeError):            # paho-mqtt 1.x
        return mqtt.Client(client_id=client_id)


class MqttPublisher:
    """Mirror lane state onto MQTT, and dispatch clear/ack commands.

    ``snapshot`` is a callable returning the daemon's status shape
    (``{"sessions": {session_id: lane_view}}``) and is consulted on connect (and
    whenever the integrator calls :meth:`publish_snapshot`). ``hub`` events keep
    the live view current between snapshots. ``client_factory`` is a zero- or
    one-argument callable returning a paho-compatible client; tests inject a
    fake one, which also means the real ``paho`` import is skipped entirely.
    """

    def __init__(self, hub, snapshot, ident, *, host: str = "127.0.0.1",
                 port: int = 1883, username: str = "", password: str = "",
                 base_topic: str = "rgi", discovery: bool = True,
                 tls: bool = False, client_factory=None, log=None,
                 on_clear=None, on_ack=None):
        self.hub = hub
        self.snapshot = snapshot
        self.ident = str(ident or "rgi")
        self.slug = slug(self.ident)
        self.host = str(host or "127.0.0.1")
        try:
            self.port = int(port)
        except (TypeError, ValueError):
            self.port = 1883
        self.username = str(username or "")
        self.password = str(password or "")
        self.base_topic = str(base_topic or "rgi").strip("/") or "rgi"
        self.discovery = bool(discovery)
        self.tls = bool(tls)
        self._client_factory = client_factory
        self._log = log
        self._on_clear = on_clear if callable(on_clear) else None
        self._on_ack = on_ack if callable(on_ack) else None

        self.availability_topic = f"{self.base_topic}/{self.slug}/availability"
        self.state_topic = f"{self.base_topic}/{self.slug}/state"
        self.command_clear = f"{self.base_topic}/{self.slug}/command/clear"
        self.command_ack = f"{self.base_topic}/{self.slug}/command/ack"
        self.discovery_aggregate = (f"homeassistant/sensor/{self.slug}/"
                                    "aggregate/config")

        self._lock = threading.RLock()
        self._client = None
        self._connected = False
        self._stopped = False
        self._last: dict[str, str] = {}
        self._cache: dict[int, tuple[str, dict]] = {}
        self._said: set[str] = set()
        if hub is not None:
            hub.subscribe(self.handle)

    # -- naming -------------------------------------------------------------
    def lane_topic(self, slot: int) -> str:
        return f"{self.base_topic}/{self.slug}/lane/{int(slot)}/state"

    def lane_discovery_topic(self, slot: int) -> str:
        return f"homeassistant/sensor/{self.slug}/lane_{int(slot)}/config"

    def command_topic(self, action: str) -> str:
        return f"{self.base_topic}/{self.slug}/command/{action}"

    # -- logging ------------------------------------------------------------
    def _say(self, level: str, message: str) -> None:
        if self._log is None:
            return
        try:
            try:
                self._log(level, message)
            except TypeError:                      # a one-argument logger
                self._log(f"{level}: {message}")
        except Exception:                          # logging must not raise
            pass

    def _say_once(self, kind: str, level: str, message: str) -> None:
        if kind in self._said:
            return
        self._said.add(kind)
        self._say(level, message)

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> bool:
        """Connect and start the network loop.

        Raises ``RuntimeError`` only when paho-mqtt is missing and no injected
        factory exists. A refused broker (or a broken factory) is logged and
        reported as ``False``: MQTT must never be the reason the panel stops.
        """
        self._stopped = False
        try:
            client = self._build_client()
        except ImportError as exc:
            raise RuntimeError(
                "paho-mqtt is not installed, so MQTT publishing is disabled; "
                "install it with 'pip install paho-mqtt'") from exc
        except Exception as exc:
            self._say("error", f"mqtt: could not create the client: {exc}")
            return False

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        try:
            client.reconnect_delay_set(min_delay=1, max_delay=60)
        except Exception:                          # older/fake clients
            pass
        if self.username or self.password:
            try:
                client.username_pw_set(self.username or None,
                                       self.password or None)
            except Exception as exc:
                self._say("warn", f"mqtt: credentials rejected: {exc}")
        if self.tls:
            try:
                client.tls_set()
            except Exception as exc:
                self._say("warn", f"mqtt: TLS setup failed: {exc}")
        try:
            client.will_set(self.availability_topic, payload=OFFLINE,
                            qos=1, retain=True)
        except Exception as exc:
            self._say("warn", f"mqtt: will message rejected: {exc}")

        self._client = client
        try:
            client.connect(self.host, self.port, keepalive=60)
            client.loop_start()
        except Exception as exc:
            # paho's loop retries the first connection with backoff
            self._say_once("connect", "warn",
                           f"mqtt: {self.host}:{self.port} did not answer "
                           f"({exc}); retrying with backoff")
            try:
                client.loop_start()
            except Exception:
                pass
            return False
        self._say("info", f"mqtt: connecting to {self.host}:{self.port}")
        return True

    def _build_client(self):
        factory = self._client_factory
        if factory is None:
            return _default_client(f"rgi-{self.slug}")
        try:
            parameters = inspect.signature(factory).parameters
            takes_argument = any(
                parameter.kind in (parameter.POSITIONAL_ONLY,
                                   parameter.POSITIONAL_OR_KEYWORD)
                for parameter in parameters.values())
        except (TypeError, ValueError):
            takes_argument = False
        if takes_argument:
            return factory(f"rgi-{self.slug}")
        return factory()

    def stop(self) -> None:
        """Publish ``offline``, then disconnect and stop the network loop."""
        with self._lock:
            if self._stopped and self._client is None:
                return
            self._stopped = True
            self._connected = False
            client = self._client
        if client is None:
            return
        try:
            self._publish_raw(self.availability_topic, OFFLINE,
                              retain=True, force=True)
        except Exception:                          # best effort
            pass
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass
        with self._lock:
            self._client = None

    # -- paho callbacks -----------------------------------------------------
    @staticmethod
    def _refused(reason_code) -> bool:
        try:
            if hasattr(reason_code, "is_failure"):
                return bool(reason_code.is_failure)
            return int(reason_code) != 0
        except Exception:
            return False

    def _on_connect(self, client, userdata=None, flags=None, reason_code=0,
                    properties=None) -> None:
        try:
            if self._refused(reason_code):
                self._say("warn",
                          f"mqtt: broker refused the connection ({reason_code})")
                return
            with self._lock:
                self._connected = True
                self._last.clear()                 # every topic is republished
            self._say("info", f"mqtt: connected to {self.host}:{self.port}")
            self._publish_raw(self.availability_topic, ONLINE,
                              retain=True, force=True)
            if self.discovery:
                self._publish_discovery()
            for action in COMMANDS:
                try:
                    client.subscribe(self.command_topic(action), qos=1)
                except Exception as exc:
                    self._say("warn", f"mqtt: subscribe {action} failed: {exc}")
            self.publish_snapshot()
        except Exception as exc:                   # never raise outwards
            self._say("error", f"mqtt: connect handling failed: {exc}")

    def _on_disconnect(self, client, userdata=None, *args) -> None:
        with self._lock:
            self._connected = False
        self._say("warn", "mqtt: disconnected; paho will reconnect with backoff")

    def _on_message(self, client, userdata=None, message=None) -> None:
        try:
            topic = str(getattr(message, "topic", "") or "")
            payload = getattr(message, "payload", b"")
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", "replace")
            session = self._command_session(payload)
            if topic == self.command_clear:
                self._dispatch("clear", session)
            elif topic == self.command_ack:
                self._dispatch("ack", session)
        except Exception as exc:                   # a bad command is not fatal
            self._say("warn", f"mqtt: ignored a command ({exc})")

    @staticmethod
    def _command_session(payload: object) -> str:
        text = str(payload or "").strip()
        if not text:
            raise ValueError("empty command payload")
        if text.startswith("{"):
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("command must be a JSON object")
            session = data.get("session") or data.get("sessionID")
        else:
            session = text
        if not isinstance(session, str) or not session.strip():
            raise ValueError("command needs a non-empty 'session'")
        return session.strip()

    def _dispatch(self, action: str, session: str) -> None:
        callback = self._on_clear if action == "clear" else self._on_ack
        if callback is None:
            self._say("info", f"mqtt: {action} {session} (no handler configured)")
            return
        try:
            callback(session)
            self._say("info", f"mqtt: {action} {session}")
        except Exception as exc:                   # a handler must not escape
            self._say("error", f"mqtt: {action} {session} failed: {exc}")

    # -- the event stream ---------------------------------------------------
    def handle(self, event) -> None:
        """One lane event: update that lane and the aggregate. Never raises."""
        try:
            with self._lock:
                if self._stopped or not self._connected:
                    return                  # a connect republishes a full snapshot
            kind = str(getattr(event, "kind", "") or "")
            lane = getattr(event, "lane", None)
            lane = lane if isinstance(lane, dict) else {}
            slot = _int(lane.get("slot"))
            if slot is None:
                slot = _int(getattr(event, "slot", None))
            if slot is None:
                return
            if kind in ("end", "clear"):
                with self._lock:
                    self._cache.pop(slot, None)
                self._publish_lane(slot, None)
            elif isinstance(lane.get("state"), str):
                with self._lock:
                    self._cache[slot] = (str(event.session_id), lane)
                if self.discovery:
                    self._publish_lane_discovery(slot)
                self._publish_lane(slot, lane)
            else:
                self.publish_snapshot()
            self._publish_aggregate()
        except Exception as exc:                   # never raise into the hub
            self._say_once("event", "error", f"mqtt: event failed: {exc}")

    def publish_snapshot(self) -> bool:
        """Republish every lane (and the aggregate) from ``snapshot()``.

        Returns ``True`` when the snapshot was read; a raising or malformed
        snapshot is logged and skipped, leaving the last good state published.
        """
        try:
            data = self.snapshot() if callable(self.snapshot) else None
            sessions = data.get("sessions") if isinstance(data, dict) else None
            lanes: dict[int, tuple[str, dict]] = {}
            if isinstance(sessions, dict):
                for session_id, row in sessions.items():
                    if not isinstance(row, dict):
                        continue
                    slot = _int(row.get("slot"))
                    if slot is None:
                        continue
                    lanes[slot] = (str(session_id), row)
            with self._lock:
                stale = set(self._cache) - set(lanes)
                self._cache = lanes
            for slot in stale:
                self._publish_lane(slot, None)
            if self.discovery:
                for slot in lanes:
                    self._publish_lane_discovery(slot)
            for slot in sorted(lanes):
                self._publish_lane(slot, lanes[slot][1])
            self._publish_aggregate()
            return True
        except Exception as exc:
            self._say_once("snapshot", "error",
                           f"mqtt: snapshot failed: {exc}")
            return False

    # -- payloads -----------------------------------------------------------
    def _lane_payload(self, slot: int, lane: dict | None) -> dict:
        if lane is None:
            return {"state": OFFLINE, "slot": slot}
        payload = {
            "state": str(lane.get("state") or "idle"),
            "slot": slot,
            "key": _text(lane.get("key")),
            "label": _text(lane.get("label")),
            "agent": _text(lane.get("agent")),
            "ident": _text(lane.get("ident")),
            "host": _text(lane.get("host")),
            "in_flight_s": _number(lane.get("in_flight_s")),
        }
        return payload

    def _publish_lane(self, slot: int, lane: dict | None) -> None:
        self._publish(self.lane_topic(slot), self._lane_payload(slot, lane),
                      retain=True)

    def _aggregate_payload(self) -> dict:
        with self._lock:
            states = [str(lane.get("state") or "")
                      for _, lane in self._cache.values()]
        state = "idle"
        for candidate in URGENCY:
            if candidate in states:
                state = candidate
                break
        counts = {name: 0 for name in STATES}
        counts["other"] = 0
        for lane_state in states:
            if lane_state in counts:
                counts[lane_state] += 1
            else:
                counts["other"] += 1
        return {"state": state, "total": len(states), "counts": counts}

    def _publish_aggregate(self) -> None:
        self._publish(self.state_topic, self._aggregate_payload(), retain=True)

    def _device(self) -> dict:
        return {
            "identifiers": [f"rgi_{self.slug}"],
            "name": f"RGI {self.ident}",
            "manufacturer": "rgi",
            "model": "agent flight indicator",
        }

    def _publish_discovery(self) -> None:
        if not self.discovery:
            return
        common = {
            "availability_topic": self.availability_topic,
            "payload_available": ONLINE,
            "payload_not_available": OFFLINE,
            "json_attributes_topic": self.state_topic,
            "value_template": "{{ value_json.state }}",
        }
        aggregate = {
            "name": f"RGI {self.ident}",
            "unique_id": f"rgi_{self.slug}_aggregate",
            "state_topic": self.state_topic,
            "icon": "mdi:robot",
            "device": self._device(),
            **common,
        }
        self._publish(self.discovery_aggregate, aggregate, retain=True)

    def _publish_lane_discovery(self, slot: int) -> None:
        if not self.discovery:
            return
        config = {
            "name": f"RGI lane {int(slot)}",
            "unique_id": f"rgi_{self.slug}_lane_{int(slot)}",
            "state_topic": self.lane_topic(slot),
            "json_attributes_topic": self.lane_topic(slot),
            "value_template": "{{ value_json.state }}",
            "availability_topic": self.availability_topic,
            "payload_available": ONLINE,
            "payload_not_available": OFFLINE,
            "device": self._device(),
        }
        self._publish(self.lane_discovery_topic(slot), config, retain=True)

    # -- the wire -----------------------------------------------------------
    def _publish_raw(self, topic: str, raw: str, *, retain: bool,
                     force: bool = False) -> bool:
        with self._lock:
            client = self._client
            digest = _digest("raw:" + raw)
            if client is None:
                return False
            if not force and self._last.get(topic) == digest:
                return False
        try:
            info = client.publish(topic, raw, qos=1, retain=retain)
        except Exception as exc:
            self._say_once(f"publish:{topic}", "warn",
                           f"mqtt: publish {topic} failed: {exc}")
            return False
        code = getattr(info, "rc", 0)
        if code not in (0, None):
            self._say_once(f"rc:{topic}", "warn",
                           f"mqtt: publish {topic} rejected (rc={code})")
            return False
        with self._lock:
            self._last[topic] = digest
        return True

    def _publish(self, topic: str, payload, *, retain: bool,
                 force: bool = False) -> bool:
        raw = (json.dumps(payload, separators=(",", ":"), default=str)
               if isinstance(payload, (dict, list)) else str(payload))
        return self._publish_raw(topic, raw, retain=retain, force=force)
