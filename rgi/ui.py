"""The web configuration UI: static assets and the ``/ui/api`` surface.

Served by the daemon, same origin, same ``X-LED-Token``. The shell and its
assets carry no data and no secrets, so they are served without auth; every
``/ui/api/*`` call requires the token, which the browser keeps in
``sessionStorage`` after a one-shot ``#token=`` bootstrap. No cookies means no
CSRF surface, and no token in server logs.

Two invariants matter more than the routing:

* **The HTTP layer never writes hardware.** Hardware tests and mapping probes
  set a time-boxed overlay on a device; the existing render loop consumes it,
  paints it and restores lane rendering. There is exactly one writer thread.
* **Failures stay small.** One broken device, one malformed request or one
  dead SSE client must not take the panel or the API down.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from urllib.parse import parse_qs, urlsplit

from . import profiles
from . import webconfig

HERE = os.path.dirname(os.path.abspath(__file__))
UI_ROOT = os.path.join(HERE, "webui")

MAX_SSE_CLIENTS = 4
HEARTBEAT_S = 20.0
PAINT_MIN_INTERVAL = 0.06          # server-side pacing for mapping probes

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
}

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'")


def envelope(code: str, message: str, field: str | None = None,
             hint: str | None = None, details: list | None = None) -> dict:
    out: dict = {"code": code, "message": message}
    if field:
        out["field"] = field
    if hint:
        out["hint"] = hint
    if details:
        out["details"] = details
    return {"error": out}


# --------------------------------------------------------------------------
# static assets
# --------------------------------------------------------------------------
_asset_lock = threading.Lock()
_asset_cache: dict[str, tuple[float, int, bytes, str]] = {}


def _asset(relpath: str) -> tuple[bytes, str, str] | None:
    """Read one file under ``webui/``, confined and cached by mtime+size."""
    try:
        root = os.path.realpath(UI_ROOT)
        path = os.path.realpath(os.path.join(root, relpath))
        if os.path.commonpath([root, path]) != root:
            return None
    except (ValueError, OSError):
        return None
    ctype = CONTENT_TYPES.get(os.path.splitext(path)[1].lower())
    if ctype is None:
        return None                       # never guess an executable type
    try:
        stat = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    with _asset_lock:
        cached = _asset_cache.get(path)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2], ctype, cached[3]
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return None
    etag = hashlib.sha256(blob).hexdigest()[:16]
    with _asset_lock:
        _asset_cache[path] = (stat.st_mtime, stat.st_size, blob, etag)
    return blob, ctype, etag


def serve_static(handler, path: str) -> bool:
    """Serve ``/ui/<asset>``. False means 404 (never a fallback file)."""
    rel = path[len("/ui"):].lstrip("/") or "index.html"
    if "\x00" in rel:
        return False
    asset = _asset(rel)
    if asset is None:
        return False
    blob, ctype, etag = asset
    quoted = '"%s"' % etag
    if handler.headers.get("If-None-Match") == quoted:
        handler.send_response(304)
        handler.send_header("ETag", quoted)
        handler.end_headers()
        return True
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(blob)))
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("ETag", quoted)
    handler.send_header("X-Content-Type-Options", "nosniff")
    if ctype.startswith("text/html"):
        handler.send_header("Content-Security-Policy", CSP)
        handler.send_header("Referrer-Policy", "no-referrer")
    handler.end_headers()
    try:
        handler.wfile.write(blob)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    return True


# --------------------------------------------------------------------------
# log ring
# --------------------------------------------------------------------------
class LogRing:
    """A bounded, in-memory diagnostic log the UI can read without files."""

    def __init__(self, capacity: int = 500):
        self._lock = threading.Lock()
        self._lines: list[dict] = []
        self._capacity = capacity
        self._seq = 0

    def add(self, level: str, source: str, message: str) -> int:
        with self._lock:
            self._seq += 1
            self._lines.append({
                "seq": self._seq,
                "time": time.time(),
                "level": level,
                "source": source,
                "message": str(message)[:1000],
            })
            if len(self._lines) > self._capacity:
                del self._lines[:-self._capacity]
            return self._seq

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [line for line in self._lines if line["seq"] > seq]


# --------------------------------------------------------------------------
# SSE
# --------------------------------------------------------------------------
class Broadcaster:
    """One condition, many readers: bump() wakes every streaming tab."""

    def __init__(self, snapshot, max_clients: int = MAX_SSE_CLIENTS):
        self._snapshot = snapshot
        self._cond = threading.Condition()
        self.seq = 0
        self.clients = 0
        self.max_clients = max_clients

    def bump(self) -> None:
        with self._cond:
            self.seq += 1
            self._cond.notify_all()

    def stream(self, handler) -> None:
        with self._cond:
            if self.clients >= self.max_clients:
                handler._send(503, envelope(
                    "too_many_streams",
                    f"already streaming to {self.clients} tabs (max {self.max_clients})"))
                return
            self.clients += 1
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            handler.send_header("Cache-Control", "no-cache, no-transform")
            handler.send_header("Connection", "close")
            handler.send_header("X-Accel-Buffering", "no")
            handler.end_headers()
            handler.close_connection = True
            last = -1
            beat = time.monotonic()
            while True:
                with self._cond:
                    self._cond.wait(timeout=1.0)
                    seq = self.seq
                now = time.monotonic()
                if seq != last:
                    data = json.dumps(self._snapshot(), default=str)
                    handler.wfile.write(
                        f"id: {seq}\nevent: snapshot\ndata: {data}\n\n".encode())
                    handler.wfile.flush()
                    last = seq
                    beat = now
                elif now - beat >= HEARTBEAT_S:
                    handler.wfile.write(b": heartbeat\n\n")
                    handler.wfile.flush()
                    beat = now
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            with self._cond:
                self.clients -= 1


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def handle_api_get(handler, path: str) -> bool:
    daemon = handler.daemon
    if path == "/ui/api/status":
        handler._send(200, daemon.ui_status())
        return True
    if path == "/ui/api/config":
        errors, warnings = webconfig.validate_config(daemon.config)
        handler._send(200, {
            "revision": daemon.revision,
            "schema_version": webconfig.SCHEMA_VERSION,
            "config": daemon.config,
            "capabilities": daemon.capabilities(),
            "validation": {"errors": errors, "warnings": warnings},
        })
        return True
    if path == "/ui/api/defaults":
        handler._send(200, {"config": webconfig.default_config()})
        return True
    if path == "/ui/api/events":
        daemon.broadcaster.stream(handler)
        return True
    if path == "/ui/api/health":
        handler._send(200, daemon.health())
        return True
    if path == "/ui/api/profiles":
        handler._send(200, {"profiles": profiles.summaries()})
        return True
    if path.startswith("/ui/api/profiles/"):
        profile_id = path[len("/ui/api/profiles/"):]
        found = profiles.load(profile_id)
        if found is None:
            handler._send(404, envelope("unknown_profile",
                                        f"no packaged profile {profile_id!r}"))
        else:
            handler._send(200, found)
        return True
    if path == "/ui/api/logs":
        query = parse_qs(urlsplit(handler.path).query)
        try:
            since = int(query.get("since", ["0"])[0])
        except (TypeError, ValueError):
            since = 0
        lines = daemon.logs.since(since)
        handler._send(200, {
            "lines": lines,
            "next": (lines[-1]["seq"] + 1) if lines else since,
        })
        return True
    if path == "/ui/api/history":
        query = parse_qs(urlsplit(handler.path).query)
        try:
            since = float(query.get("since", ["0"])[0])
        except (TypeError, ValueError):
            since = 0.0
        handler._send(200, {"entries": daemon.history_tail(since or None)})
        return True
    if path == "/ui/api/aggregate":
        handler._send(200, daemon.aggregate())
        return True
    return False


def handle_api_post(handler, path: str, data: dict) -> bool:
    if path == "/ui/api/validate":
        errors, warnings = webconfig.validate_config(data)
        handler._send(200, {"errors": errors, "warnings": warnings})
        return True
    if path == "/ui/api/test":
        return _start_overlay(handler, data, default_ms=15000, label="test")
    if path == "/ui/api/paint":
        return _start_overlay(handler, data, default_ms=300, label="map")
    if path.startswith("/ui/api/agents/") and path.endswith("/test"):
        agent_id = path[len("/ui/api/agents/"):-len("/test")]
        return _test_agent(handler, agent_id, data)
    return False


def handle_api_put(handler, path: str, data: dict) -> bool:
    if path != "/ui/api/config":
        return False
    daemon = handler.daemon
    if not isinstance(data, dict):
        handler._send(400, envelope("invalid_config", "the config must be an object"))
        return True
    errors, warnings = webconfig.validate_config(data)
    if errors:
        handler._send(422, envelope(
            "validation_failed",
            f"{len(errors)} field(s) need attention",
            details=errors))
        return True

    if_match = (handler.headers.get("If-Match") or "").strip('" ')
    if if_match not in ("", "*") and if_match != str(daemon.revision):
        handler._send(409, envelope(
            "revision_conflict",
            "another tab changed the config since this one loaded it",
            hint=f"current revision is {daemon.revision}; reload or overwrite"))
        return True

    try:
        doc = webconfig.save_config(data, existing=daemon.raw_config)
    except webconfig.ConfigError as exc:
        handler._send(422, envelope(exc.code, exc.message, exc.field, exc.hint))
        return True
    applied, restart = daemon.apply_config(doc)
    daemon.raw_config = doc
    daemon.log_event("info", "config",
                     f"applied revision {doc.get('revision')} "
                     f"({len(applied)} live change(s))")
    handler._send(200, {"revision": doc.get("revision"), "applied": applied,
                        "restart_required": restart, "warnings": warnings})
    return True


def handle_api_delete(handler, path: str) -> bool:
    if path in ("/ui/api/test", "/ui/api/paint"):
        cleared = handler.daemon.clear_overlays()
        handler._send(200, {"ok": True, "cleared": cleared})
        return True
    return False


# -- helpers ---------------------------------------------------------------
def _find_device(daemon, wanted):
    if not wanted:
        return daemon.devices[0] if daemon.devices else None
    for device in daemon.devices:
        if wanted in (device.label, device.backend.name):
            return device
    return None


def _start_overlay(handler, data: dict, default_ms: int, label: str) -> bool:
    daemon = handler.daemon
    if not isinstance(data, dict):
        handler._send(400, envelope("bad_request", "expected a JSON object"))
        return True
    now = time.monotonic()
    if now - daemon.last_paint < PAINT_MIN_INTERVAL:
        handler._send(429, envelope("too_fast", "slow down: one probe at a time",
                                    hint=f"wait {PAINT_MIN_INTERVAL:.2f}s between paints"))
        return True
    device = _find_device(daemon, data.get("device"))
    if device is None:
        handler._send(404, envelope("unknown_device",
                                    f"no device named {data.get('device')!r}"))
        return True
    lamps = device.backend.lamps()
    frame = [(0, 0, 0)] * len(lamps)

    if isinstance(data.get("lamps"), list):
        for entry in data["lamps"]:
            if not isinstance(entry, dict):
                continue
            index, rgb = entry.get("index"), entry.get("rgb")
            if (isinstance(index, int) and not isinstance(index, bool)
                    and 0 <= index < len(frame)
                    and isinstance(rgb, list) and len(rgb) == 3):
                try:
                    frame[index] = tuple(max(0, min(255, int(c))) for c in rgb)
                except (TypeError, ValueError):
                    continue
    else:
        state = data.get("state") or "working"
        colour = webconfig.parse_colour(data.get("color"))
        if colour is None:
            colour = daemon.appearance.spec(state).color
        lamp = data.get("lamp")
        if lamp is None and data.get("lane") is not None:
            try:
                lane = int(data["lane"])
                lamp = device.pool[lane] if 0 <= lane < len(device.pool) else None
            except (TypeError, ValueError):
                lamp = None
        if lamp is None:
            lamp = device.pool[0] if device.pool else 0
        if isinstance(lamp, int) and 0 <= lamp < len(frame):
            frame[lamp] = colour

    duration_ms = data.get("duration_ms", default_ms)
    try:
        duration_ms = int(duration_ms)
    except (TypeError, ValueError):
        duration_ms = default_ms
    duration_ms = max(50, min(300_000, duration_ms))
    daemon.last_paint = now
    result = daemon.set_overlay(device, frame, duration_ms / 1000.0,
                                label=str(data.get("label") or label)[:60])
    handler._send(200, result)
    return True


def _test_agent(handler, agent_id: str, data: dict) -> bool:
    daemon = handler.daemon
    entry = None
    for candidate in daemon.config.get("agents") or []:
        if isinstance(candidate, dict) and candidate.get("id") == agent_id:
            entry = candidate
            break
    base = agent_id
    if isinstance(entry, dict):
        match = entry.get("match") or {}
        base = match.get("agent") or match.get("ident") or agent_id
    sid = f"ui:{agent_id}:{int(time.time() * 1000)}"
    slot = daemon.lanes.claim(sid, base, "UI test", None)
    if slot is None:
        handler._send(409, envelope("no_lane", "no free lane for a test"))
        return True
    daemon.lanes.set_state(sid, "working")
    daemon.notify()
    device = daemon.devices[0] if daemon.devices else None
    seconds = data.get("seconds", 3) if isinstance(data, dict) else 3
    try:
        seconds = max(0.5, min(60.0, float(seconds)))
    except (TypeError, ValueError):
        seconds = 3.0

    def land() -> None:
        daemon.lanes.set_state(sid, "done")
        daemon.notify()

        def end() -> None:
            daemon.lanes.release(sid)
            if device is not None:
                device.last = None
            daemon.notify()

        timer = threading.Timer(2.0, end)
        timer.daemon = True
        timer.start()

    timer = threading.Timer(seconds, land)
    timer.daemon = True
    timer.start()
    handler._send(200, {
        "sessionID": sid,
        "slot": slot,
        "key": device.key_name(slot) if device else None,
        "lands_in_s": seconds,
    })
    return True
