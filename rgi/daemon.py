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

Behaviour worth knowing:

* a lane keeps its lamp until the session releases it, is evicted as
  least-recently-used, or goes quiet for ``--stale`` seconds. Lamps are scarce: a
  keyboard has a dozen, not a thousand.
* each device's frame is only written when *that device's* rendered result
  changes. Most controllers repaint everything on any write, so a needless write
  is a visible flash.
* while the human is typing the frame is frozen on steady colours, because some
  firmware drops keypresses while it is busy repainting (see
  docs/troubleshooting.md).
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backends import OFF, RGB, Backend, BackendUnavailable, load, available_backends
from .files import published_files, read_published

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
            self.state[sid] = state
            self.updated[sid] = time.time()
            return True

    def release(self, sid: str) -> None:
        with self.lock:
            for table in (self.slot, self.state, self.agent, self.label, self.host,
                          self.ident, self.info, self.since, self.changed, self.updated):
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
            for key, value in fields.items():
                if value is None:
                    current.pop(key, None)
                elif isinstance(value, dict) and isinstance(current.get(key), dict):
                    current[key].update(value)
                else:
                    current[key] = value
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

    @property
    def per_lamp(self) -> bool:
        return bool(getattr(self.backend, "per_lamp", True))

    def key_name(self, slot: int) -> str:
        lamps = self.backend.lamps()
        if 0 <= slot < len(self.pool) and self.pool[slot] < len(lamps):
            return lamps[self.pool[slot]].label
        return f"slot{slot}"

    def describe(self) -> dict:
        info = self.backend.describe()
        info.update(label=self.label, per_lamp=self.per_lamp,
                    lanes=[self.key_name(i) for i in range(min(len(self.pool), 12))])
        return info

    def frame(self, lane_state, now: float, quiet: bool) -> list[RGB]:
        """Colours for this device's lamps, one per lamp, in lamps() order."""
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

            colour = PALETTE[winner_state]
            if quiet:
                return [colour] * len(lamps)

            started = lane_state.get(("changed", winner_slot), now)
            if winner_state == "done" and (now - started) < DONE_BLINKS * 2 * BLINK_PERIOD:
                on = int((now - started) / BLINK_PERIOD) % 2 == 0
                return [colour if on else OFF] * len(lamps)
            if winner_state == "blocked" and (
                    BLOCK_BLINKS == 0 or (now - started) < BLOCK_BLINKS * 2 * BLINK_PERIOD):
                on = int((now - started) / BLINK_PERIOD) % 2 == 0
                return [colour if on else OFF] * len(lamps)
            return [colour] * len(lamps)

        for sid_slot, state in lane_state.items():
            if not isinstance(sid_slot, int) or sid_slot >= len(self.pool):
                continue
            lamp = self.pool[sid_slot]
            if not (0 <= lamp < len(values)):
                continue
            colour = PALETTE.get(state, PALETTE["idle"])
            started = lane_state.get(("changed", sid_slot), now)

            if quiet:
                values[lamp] = colour
                continue
            if state == "done":
                if (now - started) < DONE_BLINKS * 2 * BLINK_PERIOD:
                    values[lamp] = colour if int((now - started) / BLINK_PERIOD) % 2 == 0 else OFF
                else:
                    values[lamp] = colour
            elif state == "blocked":
                if BLOCK_BLINKS == 0 or (now - started) < BLOCK_BLINKS * 2 * BLINK_PERIOD:
                    values[lamp] = colour if int((now - started) / BLINK_PERIOD) % 2 == 0 else OFF
                else:
                    values[lamp] = colour
            else:
                values[lamp] = colour
        return values


class Daemon:
    def __init__(self, devices: list[Device], lanes: Lanes,
                 quiet: bool = True, quiet_ms: int = 900, verbose: bool = False,
                 lane_map: dict[str, int] | None = None):
        self.devices = devices
        self.lanes = lanes
        self.quiet = quiet
        self.quiet_ms = quiet_ms
        self.verbose = verbose
        self.lane_map = dict(lane_map or {})

    def snapshot(self) -> dict:
        """Slot -> state, plus the blink timestamps, taken under the lane lock."""
        out: dict = {}
        with self.lanes.lock:
            for sid, slot in self.lanes.slot.items():
                out[slot] = self.lanes.state.get(sid, "idle")
                out[("changed", slot)] = self.lanes.changed.get(sid, time.monotonic())
        return out

    def run(self) -> None:
        while True:
            try:
                now = time.monotonic()
                quiet = self.quiet and ms_since_input() < self.quiet_ms
                state = self.snapshot()
                for device in self.devices:
                    colours = device.frame(state, now, quiet)
                    key = tuple(colours)
                    if key != device.last:
                        device.backend.write(colours)
                        device.last = key
                        if self.verbose:
                            lit = " ".join(
                                f"{device.backend.lamps()[i].label}={c}"
                                for i, c in enumerate(colours) if c != OFF
                            )
                            print(f"[rgi] {device.label}: {lit or '(all off)'}", flush=True)
            except BackendUnavailable as exc:
                for device in self.devices:
                    print(f"[rgi] {device.label} lost: {exc}")
                    try:
                        device.backend.close()
                        device.backend.open()
                        device.last = None
                    except Exception as exc2:
                        print(f"[rgi] {device.label} reconnect failed: {exc2}")
            except Exception as exc:                       # keep the panel alive
                print(f"[rgi] write failed: {exc}")
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
        lanes = self.daemon.lanes
        state = lanes.state.get(sid)
        changed = lanes.changed.get(sid)
        updated = lanes.updated.get(sid)
        # "in flight" belongs to the action, not to the lane: an agent that has
        # landed is not flying, so this is None for done, error and idle - and it
        # starts again from zero when the next turn begins.
        flying = state in ("working", "blocked", "stopping")
        return {
            "slot": slot,
            "key": self._primary().key_name(slot),
            "agent": lanes.agent.get(sid),
            "label": lanes.label.get(sid),
            "host": lanes.host.get(sid),
            "ident": lanes.ident.get(sid),
            "state": state,
            "age": round(time.time() - lanes.since.get(sid, time.time()), 1),
            "in_flight_s": round(time.monotonic() - changed, 1) if (flying and changed) else None,
            # how long since this lane last reported anything at all
            "idle_s": round(time.time() - updated, 1) if updated else None,
            "info": lanes.info.get(sid) or {},
        }

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._authorised():
            self._send(401, {"error": "missing or bad X-LED-Token"})
            return
        path = self.path.split("?")[0].rstrip("/")
        lanes = self.daemon.lanes

        if path == "/status":
            with lanes.lock:
                self._send(200, {
                    "devices": [d.describe() for d in self.daemon.devices],
                    "backend": self._primary().backend.name,
                    "lamps": len(self._primary().backend.lamps()),
                    "lanes": lanes.count,
                    "lane_map": self.daemon.lane_map,
                    "free": lanes.free(),
                    "sessions": {sid: self._lane(sid, slot)
                                 for sid, slot in lanes.slot.items()},
                })
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
            text = read_published(name)
            if text is None:
                self._send(404, {"error": f"not published: {name!r}",
                                 "published": [f["name"] for f in published_files()]})
                return
            self._send_text(200, text)
            return

        self._send(404, {"error": "not found",
                         "known": ["/status", "/slots", "/session/<id>", "/files/<name>"]})

    def do_POST(self):
        if not self._authorised():
            self._send(401, {"error": "missing or bad X-LED-Token"})
            return
        data = self._body()
        path = self.path.rstrip("/")
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
            self._send(200, {"ok": True})
            return

        if path == "/session/end":
            sid = data.get("sessionID")
            if not isinstance(sid, str) or not sid.strip():
                self._send(400, {"error": "sessionID is required and must be a non-empty string"})
                return
            lanes.release(sid.strip())
            self._send(200, {"ok": True})
            return

        if path == "/clear":
            lanes.clear()
            self._send(200, {"ok": True})
            return

        self._send(404, {"error": "not found"})


def make_server(host: str, port: int, daemon: Daemon, token: str) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"daemon": daemon, "token": token})
    return ThreadingHTTPServer((host, port), handler)


def resolve_token(explicit: str | None) -> str | None:
    """Token from --token, env, or ~/.config/rgi/token. None = no auth."""
    if explicit:
        return explicit
    env = os.environ.get("RGI_TOKEN")
    if env:
        return env
    try:
        with open(DEFAULT_TOKEN_FILE, encoding="utf-8") as fh:
            value = fh.read().strip()
        return value or None
    except OSError:
        return None


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
        wanted = [name for name, ok in available_backends() if ok and name != "dummy"]
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
    daemon = Daemon(devices, lanes, quiet=not args.no_quiet,
                    quiet_ms=args.quiet_ms, verbose=args.verbose, lane_map=lane_map)

    token = resolve_token(args.token)
    server = make_server(args.host, args.port, daemon, token or "")

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
        for device in devices:
            device.backend.close()
    return 0
