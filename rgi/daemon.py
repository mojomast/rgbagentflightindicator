"""The daemon: owns a backend, holds the lane table, serves the HTTP API.

    rgi daemon --backend openrgb

Agents claim a lane, report transitions, and release it:

    POST /session/start   {"agent","sessionID","label","host","slot"} -> {"slot","key"}
    POST /session/state   {"sessionID","state"}      working|done|blocked|error|idle
    POST /session/end     {"sessionID"}
    GET  /status          every lane
    GET  /slots           every lamp, with its occupant
    GET  /session/<sid>   which lane a session holds
    GET  /files/<name>    the OpenCode plugin and the agent prompts

Behaviour worth knowing:

* a lane keeps its lamp until the session releases it, is evicted as
  least-recently-used, or goes quiet for ``--stale`` seconds. Lamps are the
  scarce resource: a keyboard has a dozen, not a thousand.
* the frame is only written when the *rendered result* changes. Most controllers
  repaint everything on any write, so a needless write is a visible flash.
* while the human is typing the frame is frozen on steady colours, because some
  firmware drops keypresses while it is busy repainting (see docs/troubleshooting.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backends import OFF, RGB, BackendUnavailable, load
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

TICK = 0.12              # render loop interval
BLINK_PERIOD = 0.28      # half period
DONE_BLINKS = 10         # white blinks when a session lands, then hold
BLOCK_BLINKS = 0         # 0 = blink until the agent says otherwise

DEFAULT_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".config", "rgi", "token")


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
    """Who holds which lamp, and what they are doing."""

    def __init__(self, pool: list[int]):
        self.lock = threading.RLock()
        self.pool = list(pool)
        self.slot: dict[str, int] = {}
        self.state: dict[str, str] = {}
        self.agent: dict[str, str] = {}
        self.label: dict[str, str] = {}
        self.host: dict[str, str] = {}
        self.since: dict[str, float] = {}
        self.changed: dict[str, float] = {}
        self.updated: dict[str, float] = {}

    def free(self) -> list[int]:
        with self.lock:
            used = set(self.slot.values())
            return [s for s in self.pool if s not in used]

    def claim(self, sid: str, agent: str, label: str | None, host: str | None,
              want: int | None = None) -> int | None:
        with self.lock:
            if sid in self.slot:
                return self.slot[sid]
            free = self.free()
            if want is not None:
                if want not in free:
                    return None
                slot = want
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
                          self.since, self.changed, self.updated):
                table.pop(sid, None)

    def clear(self) -> None:
        with self.lock:
            for sid in list(self.slot):
                self.release(sid)

    def evict_lru(self) -> str | None:
        """Give up the least recently used lamp that is not in flight."""
        with self.lock:
            candidates = [(self.updated.get(sid, 0), sid) for sid, s in self.slot.items()
                          if self.state.get(sid) != "working"]
            if not candidates:
                return None
            candidates.sort()
            self.release(candidates[0][1])
            return candidates[0][1]


class Renderer:
    """Turns the lane table into one colour per lamp."""

    def __init__(self, backend, lanes: Lanes, quiet_ms: int = 900, quiet: bool = True):
        self.backend = backend
        self.lanes = lanes
        self.quiet_ms = quiet_ms
        self.quiet = quiet

    def frame(self, now: float) -> list[RGB]:
        lamps = self.backend.lamps()
        values = [OFF] * len(lamps)
        quiet = self.quiet and ms_since_input() < self.quiet_ms

        with self.lanes.lock:
            for sid, slot in self.lanes.slot.items():
                if not (0 <= slot < len(values)):
                    continue
                state = self.lanes.state.get(sid, "idle")
                colour = PALETTE.get(state, PALETTE["idle"])
                started = self.lanes.changed.get(sid, now)

                if quiet:
                    values[slot] = colour
                    continue

                if state == "done":
                    if (now - started) < DONE_BLINKS * 2 * BLINK_PERIOD:
                        on = int((now - started) / BLINK_PERIOD) % 2 == 0
                        values[slot] = colour if on else OFF
                    else:
                        values[slot] = colour
                elif state == "blocked":
                    if BLOCK_BLINKS == 0 or (now - started) < BLOCK_BLINKS * 2 * BLINK_PERIOD:
                        on = int((now - started) / BLINK_PERIOD) % 2 == 0
                        values[slot] = colour if on else OFF
                    else:
                        values[slot] = colour
                else:
                    values[slot] = colour
        return values


class Daemon:
    def __init__(self, backend, lanes: Lanes, renderer: Renderer, verbose: bool = False):
        self.backend = backend
        self.lanes = lanes
        self.renderer = renderer
        self.verbose = verbose
        self.last: tuple | None = None

    def run(self) -> None:
        while True:
            try:
                now = time.monotonic()
                colours = self.renderer.frame(now)
                key = tuple(colours)
                if key != self.last:
                    self.backend.write(colours)
                    self.last = key
                    if self.verbose:
                        lit = " ".join(
                            f"{self.backend.lamps()[i].label}={c}"
                            for i, c in enumerate(colours) if c != OFF
                        )
                        print(f"[rgi] {lit or '(all off)'}", flush=True)
            except BackendUnavailable as exc:
                print(f"[rgi] device lost: {exc}")
                try:
                    self.backend.close()
                    self.backend.open()
                    self.last = None
                except Exception as exc2:
                    print(f"[rgi] reconnect failed: {exc2}")
            except Exception as exc:                       # keep the panel alive
                print(f"[rgi] write failed: {exc}")
            time.sleep(TICK)


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    daemon: Daemon = None                     # set by serve()
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

    def _lane(self, sid: str) -> dict:
        lanes = self.daemon.lanes
        return {
            "slot": lanes.slot[sid],
            "key": self.daemon.backend.lamps()[lanes.slot[sid]].label,
            "agent": lanes.agent.get(sid),
            "label": lanes.label.get(sid),
            "host": lanes.host.get(sid),
            "state": lanes.state.get(sid),
            "age": round(time.time() - lanes.since.get(sid, time.time()), 1),
        }

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._authorised():
            self._send(401, {"error": "missing or bad X-LED-Token"})
            return
        path = self.path.split("?")[0].rstrip("/")
        lanes = self.daemon.lanes
        lamps = self.daemon.backend.lamps()

        if path == "/status":
            with lanes.lock:
                self._send(200, {
                    "backend": self.daemon.backend.name,
                    "lamps": len(lamps),
                    "free": lanes.free(),
                    "sessions": {sid: self._lane(sid) for sid in lanes.slot},
                })
            return

        if path == "/slots":
            with lanes.lock:
                owner = {slot: sid for sid, slot in lanes.slot.items()}
                slots = []
                for lamp in lamps:
                    sid = owner.get(lamp.index)
                    entry = {"slot": lamp.index, "key": lamp.label,
                             "group": lamp.group, "free": sid is None}
                    if sid:
                        entry.update(sessionID=sid, **self._lane(sid))
                    slots.append(entry)
                self._send(200, {"backend": self.daemon.backend.name,
                                 "free": lanes.free(), "slots": slots})
            return

        if path.startswith("/session/"):
            sid = path[len("/session/"):]
            with lanes.lock:
                if sid not in lanes.slot:
                    self._send(404, {"found": False, "sessionID": sid,
                                     "hint": "not holding a lamp - POST /session/start"})
                    return
                self._send(200, dict(found=True, sessionID=sid, **self._lane(sid)))
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
            slot = lanes.claim(sid, data.get("agent", "agent"), data.get("label"), host, want)
            if slot is None and want is None:
                # Everything is taken and nothing specific was asked for: retire
                # the least recently used lamp that is not in flight. An explicit
                # slot request is never resolved by evicting someone else - that
                # gets an honest 409 instead.
                if lanes.evict_lru() is not None:
                    slot = lanes.claim(sid, data.get("agent", "agent"),
                                       data.get("label"), host, want)
            if slot is None:
                free = lanes.free()
                reason = (f"slot {want} is not available" if want is not None
                          else "no free lamps")
                self._send(409, {"error": reason, "free": free})
                return
            lamp = self.daemon.backend.lamps()[slot]
            self._send(200, {"slot": slot, "key": lamp.label, "group": lamp.group})
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


def run(args: argparse.Namespace) -> int:
    backend_cls = load(args.backend)
    kwargs = {}
    if args.backend == "openrgb":
        kwargs = {"host": args.openrgb_host, "port": args.openrgb_port,
                  "device": args.device, "leds": args.leds, "debug": args.debug}
    elif args.backend == "dummy":
        kwargs = {"count": args.leds or 13, "verbose": args.verbose,
                  "groups": "number-row"}
    elif args.backend == "sysfs":
        kwargs = {"include": args.lamp or None}

    backend = backend_cls(**kwargs)
    try:
        backend.open()
    except BackendUnavailable as exc:
        print(f"[rgi] {args.backend}: {exc}")
        return 1

    lamps = backend.lamps()
    pool = [int(x) for x in args.lanes] if args.lanes else backend.default_lanes(args.count)
    lanes = Lanes(pool)
    renderer = Renderer(backend, lanes, quiet_ms=args.quiet_ms, quiet=not args.no_quiet)
    daemon = Daemon(backend, lanes, renderer, verbose=args.verbose)

    token = resolve_token(args.token)
    server = make_server(args.host, args.port, daemon, token or "")

    print(f"[rgi] backend {backend.name}: {len(lamps)} lamps; "
          f"lanes on {', '.join(lamps[i].label for i in pool if i < len(lamps))}")
    print(f"[rgi] listening on http://{args.host}:{args.port}"
          + ("   (X-LED-Token required)" if token else "   (no auth - localhost only!)"))

    threading.Thread(target=daemon.run, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[rgi] stopping")
    finally:
        backend.close()
    return 0
