"""The daemon: owns one or more backends, holds the lane table, serves the HTTP API.

    rgi daemon                      every supported keyboard that is connected
    rgi daemon --backend evision    just that one

A lane is a logical slot (0, 1, 2 …). Each connected device maps those slots onto
its own lamps, so one session can appear as `1` on one keyboard and `7` on
another - and a device that cannot do per-key colour shows the most urgent state
across all lanes as one colour instead of lying about it.

    POST /session/start   {"agent","sessionID","label","host","slot"} -> {"slot","key"}
    POST /session/state   {"sessionID","state"}      working|done|blocked|error|idle
    POST /session/end     {"sessionID"}
    GET  /status          every lane, and every device
    GET  /slots           every lamp of the primary device, with its occupant
    GET  /session/<sid>   which lane a session holds
    GET  /files/<name>    the OpenCode plugin and the agent prompts
    GET  /ui/             the web configuration UI (static; no auth needed)
    GET  /ui/api/*        live status, config, SSE, health, logs (X-LED-Token)
    PUT  /ui/api/config   validate and apply a whole config revision

Behaviour worth knowing:

* a lane keeps its lamp until the session releases it or is evicted as
  least-recently-used when a new session needs one. Lamps are scarce: a keyboard
  has a dozen, not a thousand. (``rgi watch --stale`` releases lanes for sessions
  the watcher has not seen in a while; the daemon itself has no idle timeout.)
* each device's frame is only written when *that device's* rendered result
  changes. Most controllers repaint everything on any write, so a needless write
  is a visible flash.
* nothing is written at all while the human is typing, because some firmware
  drops keypresses while it is busy repainting. Changes that land in a typing
  burst - including a lane going done or blocked - are held and painted in one
  frame once typing pauses (see docs/troubleshooting.md).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import profiles
from . import ui as ui_api
from . import webconfig
from . import VERSION as RGI_VERSION
from .appearance import Appearance
from .backends import (OFF, RGB, Backend, BackendUnavailable, auto_backends,
                       load, opt_in_backends)
from .config import resolve_token               # noqa: F401  (re-exported)
from .events import EventHub, LaneEvent
from .files import published_files, read_published_bytes
from .webconfig import layout_labels, layout_zone_of

PALETTE: dict[str, RGB] = {
    "working": (0, 255, 0),      # in flight
    "done": (255, 255, 255),     # complete
    "blocked": (255, 0, 0),      # needs a human
    "error": (255, 0, 0),        # failed
    "idle": (40, 40, 40),        # claimed, doing nothing
    "off": (0, 0, 0),
}
BLOCKING = {"blocked", "question", "permission", "pending", "approval", "waiting"}
STATES = set(PALETTE) | BLOCKING

# when a device can only show one colour, this decides which state wins
URGENCY = ["blocked", "error", "working", "done", "idle", "off"]

TICK = 0.12              # render loop interval
BLINK_PERIOD = 0.28      # half period
DONE_BLINKS = 10         # white blinks when a session lands, then hold
BLOCK_BLINKS = 0         # 0 = blink until the agent says otherwise

DEFAULT_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".config", "rgi", "token")
DEFAULT_LANE_MAP = os.path.join(os.path.expanduser("~"), ".config", "rgi", "lanes.json")


def load_lane_map(path: str | None = None) -> dict[str, int]:
    """Agent identity -> lane, so an agent always lands on the same key.

    Keys are matched against the agent's `ident` first and its `agent` name
    second, so `{"hermes-3": 5}` and `{"opencode": 1}` both work. A policy is
    advice, not a fence: if the lane is taken the session gets a free one rather
    than being refused.
    """
    candidates = [path] if path else [DEFAULT_LANE_MAP]
    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError:
            continue
        except ValueError as exc:
            print(f"[rgi] {candidate}: not valid JSON ({exc}); ignoring the lane map")
            return {}
        if not isinstance(data, dict):
            print(f"[rgi] {candidate}: expected an object of name -> lane; ignoring it")
            return {}
        policy: dict[str, int] = {}
        for key, value in data.items():
            try:
                policy[str(key)] = int(value)
            except (TypeError, ValueError):
                print(f"[rgi] {candidate}: lane for {key!r} is not a number; ignored")
        return policy
    return {}


def lane_for(policy: dict[str, int], agent: str, ident: str | None) -> int | None:
    """The configured lane for an agent, if any."""
    if ident and ident in policy:
        return policy[ident]
    if agent in policy:
        return policy[agent]
    return None


def _ident() -> str:
    """This machine's display name, matching config.resolve_ident's fallback."""
    try:
        return socket.gethostname().split(".")[0].lower()
    except Exception:
        return "rgi"


# --------------------------------------------------------------------------
# typing-aware quiet mode (Windows): hold a steady frame while keys are pressed
# --------------------------------------------------------------------------
def ms_since_input() -> float:
    if os.name != "nt":
        return float("inf")
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return float("inf")
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime))
    except Exception:
        return float("inf")


class Lanes:
    """Who holds which lane, and what they are doing. Device-agnostic."""

    def __init__(self, count: int = 12):
        self.lock = threading.RLock()
        self.count = count
        self.slot: dict[str, int] = {}
        self.state: dict[str, str] = {}
        self.agent: dict[str, str] = {}
        self.label: dict[str, str] = {}
        self.host: dict[str, str] = {}
        self.ident: dict[str, str] = {}           # the agent's own identifier
        self.info: dict[str, dict] = {}          # free-form per lane, never painted
        self.since: dict[str, float] = {}
        self.changed: dict[str, float] = {}
        self.updated: dict[str, float] = {}
        self.acked: dict[str, float] = {}        # epoch of "I have seen this"

    def free(self) -> list[int]:
        with self.lock:
            used = set(self.slot.values())
            return [s for s in range(self.count) if s not in used]

    def claim(self, sid: str, agent: str, label: str | None, host: str | None,
              want: int | None = None, ident: str | None = None,
              prefer: int | None = None) -> int | None:
        """Give a session a lane.

        `want` is an explicit request and is refused if unavailable; `prefer` is
        a configured default for this agent and quietly falls back to any free
        lane, because a policy should never be the reason work cannot start.
        """
        with self.lock:
            if sid in self.slot:
                return self.slot[sid]
            free = self.free()
            if want is not None:
                if want not in free:
                    return None
                slot = want
            elif prefer is not None and prefer in free:
                slot = prefer
            else:
                if not free:
                    return None
                slot = free[0]
            now = time.time()
            self.slot[sid] = slot
            self.state[sid] = "idle"
            self.agent[sid] = agent
            self.label[sid] = label or sid
            self.host[sid] = host or ""
            self.ident[sid] = ident or ""
            self.since[sid] = now
            self.changed[sid] = time.monotonic()
            self.updated[sid] = now
            return slot

    def set_state(self, sid: str, state: str) -> bool:
        with self.lock:
            if sid not in self.slot:
                return False
            state = state.lower()
            if state in BLOCKING:
                state = "blocked"
            if state not in PALETTE:
                raise KeyError(state)
            if self.state.get(sid) != state:
                self.changed[sid] = time.monotonic()
                # a new state re-arms attention: an ack is never a permanent mute
                self.acked.pop(sid, None)
            self.state[sid] = state
            self.updated[sid] = time.time()
            return True

    def ack(self, sid: str) -> str | None:
        """'I have seen this'. Done lanes dim; blocked lanes stop blinking.

        The lane is never released and the state is never faked: a new state or
        a new wait clears the ack and re-arms attention.
        """
        with self.lock:
            if sid not in self.slot:
                return None
            state = self.state.get(sid)
            if state == "done":
                self.set_state(sid, "idle")
                self.acked.pop(sid, None)
                return "idle"
            if state in ("blocked", "error"):
                self.acked[sid] = time.time()
            self.updated[sid] = time.time()
            return state

    def release(self, sid: str) -> None:
        with self.lock:
            for table in (self.slot, self.state, self.agent, self.label, self.host,
                          self.ident, self.info, self.since, self.changed, self.updated,
                          self.acked):
                table.pop(sid, None)

    def set_ident(self, sid: str, ident: str) -> bool:
        """The agent's own identifier: who it is, not which machine it is on."""
        with self.lock:
            if sid not in self.slot:
                return False
            self.ident[sid] = ident.strip()
            return True

    def set_info(self, sid: str, fields: dict) -> bool:
        """Merge free-form detail into a lane.

        Detail is for reading, not painting: it changes what /status and the
        editor sidebar show, and never causes a frame write. It does count as a
        sign of life, so the lane's "idle" timer resets - otherwise a busy lane
        and a stuck one report identical numbers, since both are derived from the
        last state change.
        """
        with self.lock:
            if sid not in self.slot:
                return False
            current = self.info.setdefault(sid, {})
            before = (current.get("blocked_on") or {})
            before_id = before.get("id") if isinstance(before, dict) else None
            for key, value in fields.items():
                if value is None:
                    current.pop(key, None)
                elif isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key].update(value)
                else:
                    current[key] = value
            after = current.get("blocked_on") or {}
            after_id = after.get("id") if isinstance(after, dict) else None
            if after_id and after_id != before_id:
                # a new wait re-arms attention even while the state stays blocked
                self.acked.pop(sid, None)
            self.updated[sid] = time.time()
            return True

    def clear(self) -> None:
        with self.lock:
            for sid in list(self.slot):
                self.release(sid)

    def evict_lru(self) -> str | None:
        """Give up the least recently used lane that is not in flight."""
        with self.lock:
            candidates = [(self.updated.get(sid, 0), sid) for sid, s in self.slot.items()
                          if self.state.get(sid) != "working"]
            if not candidates:
                return None
            candidates.sort()
            self.release(candidates[0][1])
            return candidates[0][1]


class Device:
    """One keyboard, plus which of its lamps carry lanes."""

    def __init__(self, backend: Backend, pool: list[int], label: str | None = None):
        self.backend = backend
        self.pool = list(pool)
        self.label = label or backend.name
        self.last: tuple | None = None
        # Layout labels, zones and static lamp colours come from the config;
        # all are display-only and safe to swap between ticks.
        self.label_overrides: dict[int, str] = {}
        self.lamp_zones: dict[int, str] = {}
        self.layout_origin: str | None = None
        self.override: dict | None = None      # time-boxed test/mapping frame

    @property
    def per_lamp(self) -> bool:
        return bool(getattr(self.backend, "per_lamp", True))

    def key_name(self, slot: int) -> str:
        lamps = self.backend.lamps()
        if 0 <= slot < len(self.pool) and self.pool[slot] < len(lamps):
            lamp = self.pool[slot]
            return self.label_overrides.get(lamp, lamps[lamp].label)
        return f"slot{slot}"

    def describe(self) -> dict:
        info = self.backend.describe()
        info.update(label=self.label, per_lamp=self.per_lamp,
                    lanes=[self.key_name(i) for i in range(min(len(self.pool), 12))])
        return info

    def frame(self, lane_state, now: float, quiet: bool,
              appearance: Appearance | None = None) -> list[RGB]:
        """Colours for this device's lamps, one per lamp, in lamps() order."""
        app = appearance or Appearance.default()
        lamps = self.backend.lamps()
        values = [OFF] * len(lamps)

        if not self.per_lamp:
            # One colour for the whole device: show the most urgent lane, and use
            # that lane's own transition time for the blink. Claiming per-session
            # lamps we cannot address would be a lie.
            winner_state = winner_slot = None
            for state in URGENCY:
                for slot in range(len(self.pool)):
                    if lane_state.get(slot) == state:
                        winner_state, winner_slot = state, slot
                        break
                if winner_state:
                    break
            if winner_state is None or winner_state == "off":
                return values

            started = lane_state.get(("changed", winner_slot), now)
            acked = lane_state.get(("acked", winner_slot), False)
            return [app.render(winner_state, now - started, quiet or acked)] * len(lamps)

        for sid_slot, state in lane_state.items():
            if not isinstance(sid_slot, int) or sid_slot >= len(self.pool):
                continue
            lamp = self.pool[sid_slot]
            if not (0 <= lamp < len(values)):
                continue
            started = lane_state.get(("changed", sid_slot), now)
            acked = lane_state.get(("acked", sid_slot), False)
            colour = app.render(state, now - started, quiet or acked)
            override = app.lamp_override(self.backend.name, lamp)
            if override is None:
                zone = self.lamp_zones.get(lamp)
                if zone:
                    override = app.zone_override(self.backend.name, zone)
            values[lamp] = override if override is not None else colour
        return values


class Daemon:
    def __init__(self, devices: list[Device], lanes: Lanes,
                 quiet: bool = True, quiet_ms: int = 1500, verbose: bool = False,
                 lane_map: dict[str, int] | None = None,
                 config: dict | None = None, raw_config: dict | None = None,
                 quiet_configurable: bool = True):
        self.devices = devices
        self.lanes = lanes
        self.quiet = quiet
        self.quiet_ms = quiet_ms
        self.verbose = verbose
        self.lane_map = dict(lane_map or {})
        self._base_lane_map = dict(lane_map or {})    # legacy lanes.json; config wins
        self.config = config if isinstance(config, dict) else webconfig.default_config()
        self.raw_config = raw_config if isinstance(raw_config, dict) else {}
        self.revision = int(self.config.get("revision") or 0)
        self.appearance = Appearance.from_config(self.config)
        self.quiet_configurable = quiet_configurable
        self.logs = ui_api.LogRing()
        self.broadcaster = ui_api.Broadcaster(self.ui_status)
        self.last_paint = 0.0
        self._device_errors: dict[str, str] = {}
        self.hub = EventHub(on_error=self._event_error)
        self._stale: dict[str, bool] = {}
        self._tickers: list = []
        self._seen_deliveries: dict[str, float] = {}
        self._waits: dict[str, list[str]] = {}
        self._intent: dict[str, str] = {}
        self.notifier = None
        self.mqtt = None
        self.history = None
        self.otlp = None
        self.history_path = os.path.join(webconfig.CONFIG_DIR, "history", "events.jsonl")

    def _event_error(self, fn, exc) -> None:
        try:
            self.log_event("error", "events",
                           f"subscriber {getattr(fn, '__qualname__', fn)} failed: {exc}")
        except Exception:
            pass

    def snapshot(self) -> dict:
        """Slot -> state, plus the blink timestamps, taken under the lane lock."""
        out: dict = {}
        with self.lanes.lock:
            for sid, slot in self.lanes.slot.items():
                out[slot] = self.lanes.state.get(sid, "idle")
                out[("changed", slot)] = self.lanes.changed.get(sid, time.monotonic())
                out[("acked", slot)] = sid in self.lanes.acked
        return out

    # -- views for the HTTP layer ----------------------------------------
    def lane_view(self, sid: str, slot: int) -> dict:
        lanes = self.lanes
        state = lanes.state.get(sid)
        changed = lanes.changed.get(sid)
        updated = lanes.updated.get(sid)
        # A lane is either in flight or idle, never both: an action that is running
        # is not idle, and a lane that has landed is not flying. Exactly one of
        # these is ever present, which makes the pair unambiguous to read.
        flying = state in ("working", "blocked", "stopping")
        primary = self.devices[0] if self.devices else None
        with lanes.lock:
            now = time.time()
            changed_at = (now - (time.monotonic() - changed)) if changed else None
            info = lanes.info.get(sid) or {}
            try:
                heartbeat = float(info.get("heartbeat") or 0.0)
            except (TypeError, ValueError):
                heartbeat = 0.0
            try:
                threshold = float((self.config.get("settings") or {}).get("stale_s", 900) or 0)
            except (TypeError, ValueError):
                threshold = 900.0
            activity = max(changed_at or 0.0, updated or 0.0, heartbeat)
            stale_s = None
            if state in ("working", "blocked") and activity:
                stale_s = max(0.0, now - activity)
            return {
                "slot": slot,
                "key": primary.key_name(slot) if primary else f"slot{slot}",
                "agent": lanes.agent.get(sid),
                "label": lanes.label.get(sid),
                "host": lanes.host.get(sid),
                "ident": lanes.ident.get(sid),
                "state": state,
                "age": round(now - lanes.since.get(sid, now), 1),
                "in_flight_s": round(time.monotonic() - changed, 1) if (flying and changed) else None,
                "idle_s": None if flying else (round(now - updated, 1) if updated else None),
                "changed_at": changed_at,
                "idle_at": None if flying else updated,
                "acked": sid in lanes.acked,
                "stale": bool(stale_s is not None and threshold and stale_s > threshold),
                "stale_s": round(stale_s, 1) if stale_s is not None else None,
                "info": info,
            }

    def status_payload(self) -> dict:
        primary = self.devices[0] if self.devices else None
        with self.lanes.lock:
            return {
                "devices": [d.describe() for d in self.devices],
                "backend": primary.backend.name if primary else None,
                "lamps": len(primary.backend.lamps()) if primary else 0,
                "lanes": self.lanes.count,
                "lane_map": self.lane_map,
                "free": self.lanes.free(),
                "sessions": {sid: self.lane_view(sid, slot)
                             for sid, slot in self.lanes.slot.items()},
            }

    def ui_status(self) -> dict:
        """The /status shape plus revision and active test overlays."""
        payload = self.status_payload()
        payload["revision"] = self.revision
        now = time.monotonic()
        payload["tests"] = [
            {"device": d.label, "label": (d.override or {}).get("label"),
             "expires_in": round(max(0.0, (d.override or {}).get("deadline", now) - now), 1)}
            for d in self.devices if d.override
        ]
        return payload

    def capabilities(self) -> list[dict]:
        out = []
        for device in self.devices:
            lamps = device.backend.lamps()
            layout, origin = profiles.resolve_layout(
                self.config, device.backend.name, device.backend.name, len(lamps))
            records = {rec.get("index"): rec
                       for rec in (layout or {}).get("lamps") or []
                       if isinstance(rec, dict)}
            zones = layout_zone_of(layout)
            labels = layout_labels(layout)
            lamp_infos = []
            for lamp in lamps:
                record = records.get(lamp.index) or {}
                kind = record.get("kind")
                if kind is None:
                    kind = ("key" if lamp.group in ("number-row", "function-row", "key")
                            else "unknown")
                lamp_infos.append({
                    "index": lamp.index,
                    "label": labels.get(lamp.index, lamp.label),
                    "group": lamp.group,
                    "kind": kind,
                    "zone": zones.get(lamp.index),
                    "present": record.get("present", True),
                    "source": record.get("source"),
                    "confidence": record.get("confidence"),
                    "verified": record.get("verified", False),
                })
            profile = None
            if origin and origin.startswith("profile:"):
                pid = origin.split(":", 1)[1]
                found = profiles.load(pid) or {}
                profile = {"id": pid, "label": found.get("label"), "active": True,
                           "verified": found.get("verified", False),
                           "confidence": found.get("confidence")}
            else:
                candidates = profiles.match(device.backend.name, len(lamps))
                if candidates:
                    top = candidates[0]
                    profile = {"id": top.get("id"), "label": top.get("label"),
                               "active": False, "verified": top.get("verified", False),
                               "confidence": top.get("confidence")}
            out.append({
                "name": device.backend.name,
                "label": device.label,
                "per_lamp": device.per_lamp,
                "min_interval": float(getattr(device.backend, "min_interval", 0.0)),
                "pool": list(device.pool),
                "layout_origin": origin,
                "profile": profile,
                "lamps": lamp_infos,
            })
        return out

    def health(self) -> dict:
        endpoints = []
        for entry in self.config.get("endpoints") or []:
            if not isinstance(entry, dict):
                continue
            endpoints.append({
                "id": entry.get("id"),
                "kind": entry.get("kind"),
                "url": entry.get("url"),
                "enabled": entry.get("enabled", True),
                "live": False,
                "note": "configured at startup; changes need a restart",
            })
        return {
            "version": RGI_VERSION,
            "devices": [{
                "label": d.label,
                "backend": d.backend.name,
                "ok": True,
                "lamps": len(d.backend.lamps()),
                "per_lamp": d.per_lamp,
                "min_interval": float(getattr(d.backend, "min_interval", 0.0)),
                "last_error": self._device_errors.get(d.label),
            } for d in self.devices],
            "endpoints": endpoints,
            "sse_clients": self.broadcaster.clients,
            "logging": "in-memory ring, last 500 lines",
        }

    # -- runtime config ---------------------------------------------------
    def apply_config(self, config: dict,
                     pools: bool = True) -> tuple[list[str], list[str]]:
        """Swap the live config; returns (applied, restart_required).

        Appearance, lane preferences, pools, layout labels and static lamp
        colours take effect on the next tick. Host, port, lane count, endpoint
        parameters and device enable flags are startup concerns, and saying so
        is better than pretending a hot reload happened.
        """
        old = self.config or {}
        new = config if isinstance(config, dict) else webconfig.default_config()
        with self.lanes.lock:
            self.config = new
            self.revision = int(new.get("revision") or 0)
            self.appearance = Appearance.from_config(new)
            if self.quiet_configurable:
                settings = new.get("settings") or {}
                self.quiet = bool(settings.get("quiet", self.quiet))
                self.quiet_ms = int(settings.get("quiet_ms", self.quiet_ms))
            self.lane_map = dict(self._base_lane_map)
            self.lane_map.update(webconfig.compile_lane_map(new))
            for device in self.devices:
                entry = (new.get("devices") or {}).get(device.backend.name)
                if not isinstance(entry, dict):
                    continue
                lamps = device.backend.lamps()
                pool = entry.get("lane_pool")
                if pools and isinstance(pool, list):
                    clean = [p for p in pool
                             if isinstance(p, int) and not isinstance(p, bool)
                             and 0 <= p < len(lamps)]
                    if clean:
                        device.pool = clean
                layout, origin = profiles.resolve_layout(
                    new, device.backend.name, device.backend.name,
                    len(lamps))
                device.layout_origin = origin
                device.label_overrides = layout_labels(layout)
                device.lamp_zones = layout_zone_of(layout)
                device.last = None
        restart: list[str] = []
        old_settings, new_settings = old.get("settings") or {}, new.get("settings") or {}
        for key in ("host", "port", "count"):
            if old_settings.get(key) != new_settings.get(key):
                restart.append(f"settings.{key}")
        if (old.get("endpoints") or []) != (new.get("endpoints") or []):
            restart.append("endpoints")
        for key in ("notify", "mqtt", "otlp"):
            if (old.get(key) or {}) != (new.get(key) or {}):
                restart.append(key)
        if ((old.get("history") or {}).get("enabled")
                != (new.get("history") or {}).get("enabled")):
            restart.append("history")
        old_devices, new_devices = old.get("devices") or {}, new.get("devices") or {}
        for name in set(old_devices) | set(new_devices):
            if ((old_devices.get(name) or {}).get("enabled")
                    != (new_devices.get(name) or {}).get("enabled")):
                restart.append(f"devices.{name}.enabled")
        self.notify()
        return (["appearance", "lanes.overrides", "devices.lane_pool",
                 "devices.layout", "lamp_overrides", "settings.quiet"], restart)

    # -- hardware test/mapping overlays -----------------------------------
    def set_overlay(self, device: Device, frame, seconds: float,
                    label: str = "test") -> dict:
        """Paint one frame for a moment, consumed by the render loop.

        The HTTP thread never writes the backend; this is the single-writer
        rule that keeps a test from racing the live panel.
        """
        seconds = max(0.05, float(seconds))
        device.override = {
            "frame": tuple(frame),
            "deadline": time.monotonic() + seconds,
            "expires_at": time.time() + seconds,
            "label": label,
        }
        device.last = None
        self.notify()
        return {"device": device.label, "label": label,
                "duration_s": seconds, "expires_at": device.override["expires_at"]}

    def clear_overlays(self) -> list[str]:
        cleared = []
        for device in self.devices:
            if device.override:
                device.override = None
                device.last = None
                cleared.append(device.label)
        if cleared:
            self.notify()
        return cleared

    # -- diagnostics ------------------------------------------------------
    def log_event(self, level: str, source: str, message: str) -> None:
        """Record a line for the Logs page. Deliberately does not notify SSE:
        a chatty error must not turn into a stream of snapshots."""
        self.logs.add(level, source, message)

    def notify(self) -> None:
        try:
            self.broadcaster.bump()
        except Exception:
            pass

    # -- events and optional subsystems -----------------------------------
    def publish(self, kind: str, sid: str, slot: int, lane: dict | None = None) -> None:
        if lane is None:
            try:
                lane = self.lane_view(sid, slot)
            except Exception:
                lane = {}
        try:
            self.hub.publish(LaneEvent(kind=kind, session_id=sid, slot=slot, lane=lane))
        except Exception:
            pass

    def poll_subsystems(self) -> None:
        """Per-tick work: stale transitions and subsystem timers."""
        for sid, slot in list(self.lanes.slot.items()):
            try:
                view = self.lane_view(sid, slot)
            except Exception:
                continue
            now_stale = bool(view.get("stale"))
            if now_stale != self._stale.get(sid, False):
                self._stale[sid] = now_stale
                self.publish("stale" if now_stale else "recovered", sid, slot, view)
        for sid in list(self._stale):
            if sid not in self.lanes.slot:
                self._stale.pop(sid, None)
        for ticker in list(self._tickers):
            try:
                ticker()
            except Exception as exc:
                self.log_event("error", "tick", f"{ticker}: {exc}")

    def apply_actions(self, actions, source: str = "") -> int:
        """Apply normalized ingest actions (rgi.ingest.Action-shaped)."""
        applied = 0
        for action in actions or []:
            meta = getattr(action, "meta", None) or {}
            delivery = meta.get("delivery")
            if delivery:
                if delivery in self._seen_deliveries:
                    continue
                self._seen_deliveries[delivery] = time.time()
                if len(self._seen_deliveries) > 500:
                    cutoff = time.time() - 3600
                    self._seen_deliveries = {
                        key: value for key, value in self._seen_deliveries.items()
                        if value >= cutoff}
            sid = action.session
            op = action.op
            slot = self.lanes.slot.get(sid)
            meta = meta if isinstance(meta, dict) else {}
            if op in ("begin", "snapshot"):
                if slot is None:
                    slot = self.lanes.claim(sid, action.agent or source or "ingest",
                                            action.label,
                                            meta.get("host") or None, None,
                                            meta.get("ident") or None,
                                            getattr(action, "slot", None))
                if slot is None:
                    self.log_event("warn", "ingest", f"{sid}: no free lane")
                    continue
                self.publish("start", sid, slot)
            elif op == "state" and action.state:
                if slot is None:
                    slot = self.lanes.claim(sid, action.agent or source or "ingest",
                                            action.label, None)
                    if slot is not None:
                        self.publish("start", sid, slot)
                if slot is not None:
                    try:
                        if self.lanes.set_state(sid, action.state):
                            if action.state in ("working", "done", "idle"):
                                self._intent[sid] = action.state
                            self.publish("state", sid, slot)
                    except KeyError:
                        pass
            elif op in ("info", "activity") and action.info:
                if slot is not None and self.lanes.set_info(sid, action.info):
                    self.publish("info", sid, slot)
            elif op == "attention":
                if slot is None:
                    slot = self.lanes.claim(sid, action.agent or source or "ingest",
                                            action.label, None)
                    if slot is not None:
                        self.publish("start", sid, slot)
                if slot is None:
                    continue
                waits = self._waits.setdefault(sid, [])
                if meta.get("open"):
                    wait_id = str(action.request or "wait")
                    if wait_id not in waits:
                        waits.append(wait_id)
                    if self.lanes.state.get(sid) != "blocked":
                        self._intent[sid] = self.lanes.state.get(sid, "working")
                    fields = dict(action.info or {})
                    fields.setdefault("blocked_on", {"id": wait_id})
                    self.lanes.set_info(sid, fields)
                    try:
                        self.lanes.set_state(sid, "blocked")
                    except KeyError:
                        pass
                    self.publish("state", sid, slot)
                else:
                    request = action.request
                    if meta.get("all") or (request is None and not meta.get("match")):
                        waits.clear()
                    elif request:
                        if request in waits:
                            waits.remove(request)
                    elif meta.get("match"):
                        needle = str(meta["match"]).lower()
                        victim = next((w for w in waits if needle in w.lower()), None)
                        if victim is None and waits:
                            victim = waits[0]
                        if victim is not None:
                            waits.remove(victim)
                    self.lanes.set_info(sid, {"blocked_on": None})
                    if not waits:
                        self._waits.pop(sid, None)
                        self.lanes.set_state(sid, self._intent.pop(sid, "working"))
                    self.publish("info", sid, slot)
            elif op == "end":
                if slot is not None:
                    view = self.lane_view(sid, slot)
                    self.lanes.release(sid)
                    self._stale.pop(sid, None)
                    self._waits.pop(sid, None)
                    self._intent.pop(sid, None)
                    self.publish("end", sid, slot, view)
            applied += 1
        if applied:
            self.notify()
        return applied

    def apply_otlp(self, updates) -> int:
        applied = 0
        for update in updates or []:
            sid = getattr(update, "session", "")
            if sid not in self.lanes.slot:
                continue
            if self.lanes.set_info(sid, getattr(update, "fields", {}) or {}):
                self.publish("info", sid, self.lanes.slot[sid])
                applied += 1
        if applied:
            self.notify()
        return applied

    def aggregate(self) -> dict:
        counts = {name: 0 for name in ("working", "blocked", "error", "done", "idle")}
        blocked = []
        with self.lanes.lock:
            for sid, slot in self.lanes.slot.items():
                state = self.lanes.state.get(sid, "idle")
                if state in counts:
                    counts[state] += 1
                if state in ("blocked", "error"):
                    view = self.lane_view(sid, slot)
                    blocked.append({"session": sid, "slot": slot,
                                    "label": view.get("label"), "host": view.get("host"),
                                    "wait_s": view.get("in_flight_s"),
                                    "what": (view.get("info") or {}).get("blocked_on")})
        urgent = next((state for state in ("blocked", "error", "working", "done", "idle")
                       if counts.get(state)), "idle")
        return {"state": urgent, "counts": counts, "blocked": blocked}

    def history_tail(self, since: float | None = None, limit: int = 500) -> list[dict]:
        try:
            with open(self.history_path, encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if since and float(entry.get("at") or 0) < since:
                continue
            out.append(entry)
        return out

    def mqtt_clear(self, session: str | None = None) -> None:
        if not session:
            for sid in list(self.lanes.slot):
                self.mqtt_clear(sid)
            return
        slot = self.lanes.slot.get(session)
        if slot is None:
            return
        view = self.lane_view(session, slot)
        self.lanes.release(session)
        self._stale.pop(session, None)
        self.publish("end", session, slot, view)
        self.notify()

    def mqtt_ack(self, session: str | None = None) -> None:
        if not session:
            return
        slot = self.lanes.slot.get(session)
        state = self.lanes.ack(session)
        if state is not None and slot is not None:
            self.publish("ack", session, slot)
            self.notify()

    def start_subsystems(self) -> None:
        """Construct optional observers from the config; never fatal."""
        cfg = self.config or {}
        history_cfg = cfg.get("history") or {}
        if history_cfg.get("enabled", True):
            try:
                from .history import HistoryLog
                self.history = HistoryLog(
                    self.history_path,
                    max_bytes=int(history_cfg.get("max_bytes", 2_000_000)))
                self.hub.subscribe(self.history.handle)
                self.log_event("info", "history", f"recording transitions to {self.history_path}")
            except Exception as exc:
                self.log_event("warn", "history", f"disabled: {exc}")

        notify_cfg = cfg.get("notify") or {}
        if notify_cfg.get("enabled"):
            try:
                from .notify import Notifier
                self.notifier = Notifier(
                    self.hub,
                    [str(part) for part in (notify_cfg.get("command") or [])],
                    states=tuple(notify_cfg.get("states") or ("blocked", "error")),
                    min_duration_s=float(notify_cfg.get("min_duration_s", 3.0)),
                    cooldown_s=float(notify_cfg.get("cooldown_s", 30.0)),
                    repeat_s=float(notify_cfg.get("repeat_s", 0.0)),
                    quiet_hours=str(notify_cfg.get("quiet_hours") or ""),
                    dry_run=bool(notify_cfg.get("dry_run")),
                    log=lambda level, message: self.log_event(level, "notify", message))
                self.hub.subscribe(self.notifier.handle)
                self._tickers.append(self.notifier.poll)
                self.log_event("info", "notify", "attention notifier armed")
            except Exception as exc:
                self.log_event("warn", "notify", f"disabled: {exc}")

        mqtt_cfg = cfg.get("mqtt") or {}
        if mqtt_cfg.get("enabled"):
            try:
                from .mqtt import MqttPublisher
                self.mqtt = MqttPublisher(
                    self.hub, self.ui_status, _ident(),
                    host=str(mqtt_cfg.get("host") or "127.0.0.1"),
                    port=int(mqtt_cfg.get("port", 1883)),
                    username=str(mqtt_cfg.get("username") or ""),
                    password=str(mqtt_cfg.get("password") or ""),
                    base_topic=str(mqtt_cfg.get("base_topic") or "rgi"),
                    discovery=bool(mqtt_cfg.get("discovery", True)),
                    tls=bool(mqtt_cfg.get("tls")),
                    log=lambda level, message: self.log_event(level, "mqtt", message),
                    on_clear=self.mqtt_clear, on_ack=self.mqtt_ack)
                self.mqtt.open()
                self.log_event("info", "mqtt", f"publishing to {mqtt_cfg.get('host')}:{mqtt_cfg.get('port')}")
            except Exception as exc:
                self.log_event("warn", "mqtt", f"disabled: {exc}")

        if (cfg.get("otlp") or {}).get("enabled"):
            try:
                from .otlp import OtlpReceiver
                self.otlp = OtlpReceiver()
                self.log_event("info", "otlp", "OTLP/HTTP JSON receiver enabled")
            except Exception as exc:
                self.log_event("warn", "otlp", f"disabled: {exc}")

    def stop_subsystems(self) -> None:
        for obj in (self.mqtt, self.notifier, self.history):
            if obj is None:
                continue
            for method in ("stop", "close"):
                fn = getattr(obj, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break

    def tick(self) -> None:
        """One render pass: paint every device whose frame changed.

        While the human is typing, nothing is written at all. Some firmware
        drops keypresses while it is busy repainting, and the worst moment to
        write is exactly when a lane lands mid-typing burst - so the frame is
        held, and the newest state goes out in a single write once typing
        pauses. The input check is repeated immediately before the write
        because the render above takes long enough for a keystroke to arrive
        after it.
        """
        now = time.monotonic()
        quiet = self.quiet and ms_since_input() < self.quiet_ms
        state = self.snapshot()
        for device in self.devices:
            overlay = device.override
            if overlay is not None:
                if now < overlay["deadline"]:
                    # A test or mapping probe: an explicit human action, so the
                    # typing hold does not apply - they clicked, not typed.
                    colours = list(overlay["frame"])
                    key = ("overlay",) + tuple(colours)
                    if key != device.last:
                        device.backend.write(colours)
                        device.last = key
                    continue
                device.override = None      # expired: hand the board back
                device.last = None
                self.notify()
            colours = device.frame(state, now, quiet, self.appearance)
            key = tuple(colours)
            if key == device.last:
                continue
            if self.quiet and ms_since_input() < self.quiet_ms:
                continue                    # typing: hold the frame, write nothing
            device.backend.write(colours)
            device.last = key
            if self.verbose:
                lit = " ".join(
                    f"{device.backend.lamps()[i].label}={c}"
                    for i, c in enumerate(colours) if c != OFF
                )
                print(f"[rgi] {device.label}: {lit or '(all off)'}", flush=True)
        self.poll_subsystems()

    def run(self) -> None:
        while True:
            try:
                self.tick()
            except BackendUnavailable as exc:
                for device in self.devices:
                    print(f"[rgi] {device.label} lost: {exc}")
                    self._device_errors[device.label] = str(exc)
                    self.log_event("error", device.label, f"lost: {exc}")
                    try:
                        device.backend.close()
                        device.backend.open()
                        device.last = None
                        self._device_errors.pop(device.label, None)
                        self.log_event("info", device.label, "reconnected")
                        self.notify()
                    except Exception as exc2:
                        print(f"[rgi] {device.label} reconnect failed: {exc2}")
                        self._device_errors[device.label] = str(exc2)
                        self.log_event("error", device.label,
                                       f"reconnect failed: {exc2}")
            except Exception as exc:                       # keep the panel alive
                print(f"[rgi] write failed: {exc}")
                self.log_event("error", "render", f"write failed: {exc}")
            time.sleep(TICK)


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    daemon: Daemon = None                     # set by make_server()
    token: str = ""

    def log_message(self, *args):
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, text: str, ctype="text/plain; charset=utf-8") -> None:
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, code: int, blob: bytes, ctype="application/octet-stream") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _authorised(self) -> bool:
        return not self.token or self.headers.get("X-LED-Token") == self.token

    def _primary(self) -> Device:
        return self.daemon.devices[0]

    def _lane(self, sid: str, slot: int) -> dict:
        return self.daemon.lane_view(sid, slot)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        raw_path = self.path.split("?")[0]
        path = raw_path.rstrip("/")
        if path == "/ui/api" or path.startswith("/ui/api/"):
            if not self._authorised():
                self._send(401, {"error": "missing or bad X-LED-Token"})
                return
            if ui_api.handle_api_get(self, path):
                return
            self._send(404, ui_api.envelope("not_found", f"no such API route: {path}"))
            return
        if path == "/ui" or path.startswith("/ui/"):
            # The shell and its assets carry no data, so they need no token;
            # every /ui/api call does.
            if ui_api.serve_static(self, raw_path.rstrip("/")):
                return
            self._send(404, {"error": "not found"})
            return
        if not self._authorised():
            self._send(401, {"error": "missing or bad X-LED-Token"})
            return
        lanes = self.daemon.lanes

        if path == "/status":
            self._send(200, self.daemon.status_payload())
            return

        if path == "/slots":
            device = self._primary()
            lamps = device.backend.lamps()
            with lanes.lock:
                owner = {slot: sid for sid, slot in lanes.slot.items()}
                lamp_to_slot = {lamp: slot for slot, lamp in enumerate(device.pool)}
                slots = []
                for lamp in lamps:
                    slot = lamp_to_slot.get(lamp.index)
                    sid = owner.get(slot) if slot is not None else None
                    entry = {"slot": lamp.index, "key": lamp.label,
                             "group": lamp.group, "free": sid is None,
                             "lane": slot}
                    if sid:
                        entry.update(sessionID=sid, **self._lane(sid, slot))
                    slots.append(entry)
                self._send(200, {"backend": device.backend.name, "device": device.label,
                                 "free": lanes.free(), "slots": slots})
            return

        if path.startswith("/session/"):
            sid = path[len("/session/"):]
            with lanes.lock:
                if sid not in lanes.slot:
                    self._send(404, {"found": False, "sessionID": sid,
                                     "hint": "not holding a lamp - POST /session/start"})
                    return
                self._send(200, dict(found=True, sessionID=sid,
                                     **self._lane(sid, lanes.slot[sid])))
            return

        if path == "/files":
            self._send(200, {"files": published_files(), "hint": "GET /files/<name>"})
            return

        if path.startswith("/files/"):
            name = path[len("/files/"):]
            blob = read_published_bytes(name)
            if blob is None:
                self._send(404, {"error": f"not published: {name!r}",
                                 "published": [f["name"] for f in published_files()]})
                return
            # a built client wheel is binary; everything else in there is text
            ctype = ("application/octet-stream" if name.endswith(".whl")
                     else "text/plain; charset=utf-8")
            self._send_bytes(200, blob, ctype)
            return

        self._send(404, {"error": "not found",
                         "known": ["/status", "/slots", "/session/<id>", "/files/<name>"]})

    def do_POST(self):
        raw_path = self.path.split("?")[0]
        path = raw_path.rstrip("/")
        if not self._authorised():
            self._send(401, {"error": "missing or bad X-LED-Token"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > webconfig.BODY_MAX:
            self._send(413, ui_api.envelope(
                "body_too_large", f"body over {webconfig.BODY_MAX} bytes"))
            return
        data = self._body()
        if path.startswith("/ui/api/"):
            if ui_api.handle_api_post(self, path, data):
                return
            self._send(404, ui_api.envelope("not_found", f"no such API route: {path}"))
            return
        lanes = self.daemon.lanes

        if path == "/session/start":
            sid = data.get("sessionID")
            if not isinstance(sid, str) or not sid.strip():
                self._send(400, {"error": "sessionID is required and must be a non-empty string"})
                return
            sid = sid.strip()
            want = data.get("slot")
            if want is not None:
                try:
                    want = int(want)
                except (TypeError, ValueError):
                    self._send(400, {"error": "slot must be an integer"})
                    return
            host = data.get("host") or self.client_address[0]
            ident = data.get("ident") or data.get("identifier")
            prefer = None
            if want is None:
                prefer = lane_for(getattr(self.daemon, "lane_map", {}),
                                  data.get("agent", "agent"), ident)
            slot = lanes.claim(sid, data.get("agent", "agent"), data.get("label"),
                               host, want, ident, prefer)
            if slot is None and want is None:
                # Everything is taken and nothing specific was asked for: retire
                # the least recently used lane. An explicit slot request is never
                # resolved by evicting someone else - that gets an honest 409.
                if lanes.evict_lru() is not None:
                    slot = lanes.claim(sid, data.get("agent", "agent"),
                                       data.get("label"), host, want, ident, prefer)
            if slot is None:
                free = lanes.free()
                reason = (f"slot {want} is not available" if want is not None
                          else "no free lanes")
                self._send(409, {"error": reason, "free": free})
                return
            primary = self._primary()
            key = primary.key_name(slot)
            shown = [d.label for d in self.daemon.devices
                     if slot < len(d.pool)]
            self.daemon.publish("start", sid, slot)
            self.daemon.notify()
            self._send(200, {"slot": slot, "key": key,
                             "devices": shown})
            return

        if path == "/session/state":
            sid = data.get("sessionID")
            if not isinstance(sid, str) or not sid.strip():
                self._send(400, {"error": "sessionID is required and must be a non-empty string"})
                return
            state = (data.get("state") or "").lower()
            if state not in STATES:
                self._send(400, {"error": f"unknown state {state!r}", "known": sorted(STATES)})
                return
            ok = lanes.set_state(sid.strip(), state)
            if ok:
                self.daemon.publish("state", sid.strip(), lanes.slot.get(sid.strip(), -1))
                self.daemon.notify()
            self._send(200 if ok else 404, {"ok": ok})
            return

        if path == "/session/info":
            sid = data.get("sessionID")
            if not isinstance(sid, str) or not sid.strip():
                self._send(400, {"error": "sessionID is required and must be a non-empty string"})
                return
            # accept {"info": {...}} or loose top-level fields, because agents in
            # a hurry send both
            fields = dict(data.get("info") or {})
            fields.update({k: v for k, v in data.items() if k not in ("sessionID", "info")})
            if fields.get("identifier"):
                fields["ident"] = fields.pop("identifier")

            # the identifier is a first-class field, so setting only that is a
            # perfectly good request and must not be answered with "nothing to record"
            recorded = False
            if isinstance(fields.get("ident"), str):
                recorded = lanes.set_ident(sid.strip(), fields.pop("ident")) or recorded
            if fields:
                recorded = lanes.set_info(sid.strip(), fields) or recorded

            if not recorded:
                if sid.strip() not in lanes.slot:
                    self._send(404, {"error": "no lane held by that sessionID"})
                else:
                    self._send(400, {"error": "nothing to record: send an info object, "
                                              "an ident, or both"})
                return
            self.daemon.publish("info", sid.strip(), lanes.slot.get(sid.strip(), -1))
            self.daemon.notify()
            self._send(200, {"ok": True})
            return

        if path == "/session/end":
            sid = data.get("sessionID")
            if not isinstance(sid, str) or not sid.strip():
                self._send(400, {"error": "sessionID is required and must be a non-empty string"})
                return
            key_sid = sid.strip()
            slot = lanes.slot.get(key_sid)
            view = self.daemon.lane_view(key_sid, slot) if slot is not None else None
            lanes.release(key_sid)
            if slot is not None:
                self.daemon.publish("end", key_sid, slot, view)
            self.daemon.notify()
            self._send(200, {"ok": True})
            return

        if path == "/clear":
            for key_sid, slot in list(lanes.slot.items()):
                view = self.daemon.lane_view(key_sid, slot)
                self.daemon.publish("end", key_sid, slot, view)
            lanes.clear()
            self.daemon.notify()
            self._send(200, {"ok": True})
            return

        if path == "/session/ack":
            sid = data.get("sessionID")
            if not isinstance(sid, str) or not sid.strip():
                self._send(400, {"error": "sessionID is required and must be a non-empty string"})
                return
            key_sid = sid.strip()
            slot = lanes.slot.get(key_sid)
            state = lanes.ack(key_sid)
            if state is None:
                self._send(404, {"ok": False, "error": "no lane held by that sessionID"})
                return
            if slot is not None:
                self.daemon.publish("ack", key_sid, slot)
            self.daemon.notify()
            self._send(200, {"ok": True, "state": state,
                             "acked": key_sid in lanes.acked})
            return

        if path == "/ingest" or path.startswith("/hook/"):
            source = (path[len("/hook/"):] if path.startswith("/hook/")
                      else str(data.get("source") or "generic"))
            try:
                from . import ingest as ingest_module
            except Exception:
                self._send(503, ui_api.envelope(
                    "ingest_unavailable", "the ingest module is not available"))
                return
            try:
                actions = ingest_module.normalize(source, data, self.headers)
            except Exception as exc:
                self._send(400, ui_api.envelope("bad_event", str(exc)))
                return
            applied_count = self.daemon.apply_actions(actions, source=source)
            self._send(200, {"ok": True, "source": source, "actions": applied_count})
            return

        if path in ("/v1/metrics", "/v1/logs", "/v1/traces"):
            receiver = self.daemon.otlp
            if receiver is None:
                self._send(503, ui_api.envelope(
                    "otlp_disabled", "enable otlp in the config to accept telemetry"))
                return
            try:
                updates = receiver.handle(path, data)
            except Exception as exc:
                self._send(400, ui_api.envelope("bad_otlp", str(exc)))
                return
            applied_count = self.daemon.apply_otlp(updates)
            self._send(200, {"partialSuccess": {}, "applied": applied_count})
            return

        self._send(404, {"error": "not found"})

    def do_PUT(self):
        path = self.path.split("?")[0].rstrip("/")
        if not self._authorised():
            self._send(401, {"error": "missing or bad X-LED-Token"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > webconfig.BODY_MAX:
            self._send(413, ui_api.envelope(
                "body_too_large", f"body over {webconfig.BODY_MAX} bytes"))
            return
        data = self._body()
        if ui_api.handle_api_put(self, path, data):
            return
        self._send(404, ui_api.envelope("not_found", f"no such API route: PUT {path}"))

    def do_DELETE(self):
        path = self.path.split("?")[0].rstrip("/")
        if not self._authorised():
            self._send(401, {"error": "missing or bad X-LED-Token"})
            return
        if ui_api.handle_api_delete(self, path):
            return
        self._send(404, ui_api.envelope("not_found", f"no such API route: DELETE {path}"))


def make_server(host: str, port: int, daemon: Daemon, token: str) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"daemon": daemon, "token": token})
    return ThreadingHTTPServer((host, port), handler)


def build_backend(name: str, args: argparse.Namespace) -> Backend:
    kwargs: dict = {}
    if name == "openrgb":
        kwargs = {"host": args.openrgb_host, "port": args.openrgb_port,
                  "device": args.device, "leds": args.leds, "debug": args.debug}
    elif name == "dummy":
        kwargs = {"count": args.leds or 13, "verbose": args.verbose, "groups": "number-row"}
    elif name == "sysfs":
        kwargs = {"include": args.lamp or None}
    elif name == "evision":
        kwargs = {"leds": args.leds, "verbose": args.verbose}
    elif name == "qmk":
        kwargs = {"leds": args.leds, "verbose": args.verbose, "count": args.count}
    elif name == "gamesense":
        kwargs = {"verbose": args.verbose}
    elif name == "http-light":
        kwargs = {"url": getattr(args, "http_light_url", None),
                  "preset": getattr(args, "http_light_preset", None),
                  "body_template": getattr(args, "http_light_template", None),
                  "verbose": args.verbose}
    return load(name)(**kwargs)


def open_devices(args: argparse.Namespace) -> list[Device]:
    """Every requested backend that is present, opened and ready.

    --backend auto (the default) means *all* supported keyboards that are
    connected, so plugging one in is all the configuration there is.
    """
    wanted = args.backend
    if isinstance(wanted, str):
        wanted = [wanted]
    if not wanted or wanted == ["auto"]:
        wanted = auto_backends()
        for name in opt_in_backends():
            # Say so rather than opening it: a board that is plugged in is not
            # consent to be driven by a protocol we have not confirmed on it.
            print(f"[rgi] {name}: present, not opened automatically "
                  f"(start with --backend {name} to use it)")
        if not wanted:
            wanted = ["dummy"]

    devices: list[Device] = []
    for name in wanted:
        try:
            backend = build_backend(name, args)
        except SystemExit:
            continue
        try:
            backend.open()
        except BackendUnavailable as exc:
            print(f"[rgi] {name}: {exc}")
            continue
        except Exception as exc:
            print(f"[rgi] {name}: could not open ({exc})")
            continue
        pool = ([int(x) for x in args.lanes] if args.lanes
                else backend.default_lanes(args.count))
        pool = [p for p in pool if 0 <= p < len(backend.lamps())]
        if not pool:
            print(f"[rgi] {name}: no usable lamps, skipping")
            backend.close()
            continue
        devices.append(Device(backend, pool, label=name))
    return devices


def run(args: argparse.Namespace) -> int:
    devices = open_devices(args)
    if not devices:
        print("[rgi] no supported keyboard found. `rgi detect` lists what is visible.")
        return 1

    lanes = Lanes(count=args.count)
    lane_map = load_lane_map(getattr(args, "lane_map", None))
    try:
        raw_config = webconfig.load_raw()
        config = webconfig.deep_fill(webconfig.default_config(), raw_config)
    except webconfig.ConfigError as exc:
        print(f"[rgi] config: {exc.message}; using defaults")
        raw_config, config = {}, webconfig.default_config()
    daemon = Daemon(devices, lanes, quiet=not args.no_quiet,
                    quiet_ms=args.quiet_ms, verbose=args.verbose,
                    lane_map=lane_map, config=config, raw_config=raw_config,
                    quiet_configurable=not args.no_quiet)
    daemon.apply_config(config, pools=not args.lanes)
    if args.quiet_ms != 1500:               # an explicit CLI flag wins at startup
        daemon.quiet_ms = args.quiet_ms     # (later Applies are the user's choice)
    daemon.start_subsystems()

    token = resolve_token(args.token)
    server = make_server(args.host, args.port, daemon, token or "")
    daemon.log_event("info", "daemon", f"listening on http://{args.host}:{args.port}")

    for device in devices:
        lamps = device.backend.lamps()
        shown = ", ".join(device.key_name(i) for i in range(min(len(device.pool), 12)))
        print(f"[rgi] {device.label}: {len(lamps)} lamps"
              f"{'' if device.per_lamp else ' (single colour)'}; lanes on {shown}")
    print(f"[rgi] listening on http://{args.host}:{args.port}"
          + ("   (X-LED-Token required)" if token else "   (no auth - localhost only!)"))

    threading.Thread(target=daemon.run, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[rgi] stopping")
    finally:
        daemon.stop_subsystems()
        for device in devices:
            device.backend.close()
    return 0
