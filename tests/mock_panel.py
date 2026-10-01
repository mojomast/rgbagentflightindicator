"""A stand-in daemon for tests: enough API, over real localhost HTTP.

Hardware-free and dependency-free. Tests can also make it misbehave on purpose:
demand a token, run out of lanes, or go away entirely (stop() is a connection
refused, which is what a dead daemon looks like from a reporter).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockPanel:
    """A tiny panel. Use it as a context manager, then point a reporter at .url."""

    def __init__(self, *, token: str | None = None, lanes: int = 12):
        self.token = token
        self.lanes = lanes
        self.sessions: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict]] = []
        self.offline = False
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "MockPanel":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- inspection --------------------------------------------------------
    def free(self) -> list[int]:
        used = {s["slot"] for s in self.sessions.values()}
        return [i for i in range(self.lanes) if i not in used]

    def requests_for(self, path: str) -> list[tuple[str, str, dict]]:
        return [r for r in self.requests if r[1] == path]

    def state_of(self, sid: str) -> str | None:
        return (self.sessions.get(sid) or {}).get("state")

    def info_of(self, sid: str) -> dict:
        return (self.sessions.get(sid) or {}).get("info") or {}

    # -- the HTTP side -----------------------------------------------------
    def _handler(self):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # keep the test output clean
                pass

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    return json.loads(self.rfile.read(length))
                except ValueError:
                    return {}

            def _auth(self) -> bool:
                if panel.token and self.headers.get("X-LED-Token") != panel.token:
                    self._send(401, {"error": "token"})
                    return False
                return True

            def _record(self, payload: dict) -> None:
                with panel.lock:
                    panel.requests.append((self.command, self.path, payload))

            def _session_id(self, payload: dict) -> str:
                return str(payload.get("sessionID") or "")

            def do_GET(self):                    # noqa: N802
                if panel.offline:
                    self._send(503, {"error": "offline"})
                    return
                self._record({})
                if not self._auth():
                    return
                if self.path == "/status":
                    with panel.lock:
                        self._send(200, {
                            "devices": [{"label": "mock", "backend": "mock",
                                         "lamps": panel.lanes, "per_lamp": True}],
                            "free": panel.free(),
                            "sessions": {sid: dict(s) for sid, s in panel.sessions.items()},
                        })
                    return
                if self.path.startswith("/session/"):
                    sid = self.path[len("/session/"):]
                    from urllib.parse import unquote
                    sid = unquote(sid)
                    with panel.lock:
                        session = panel.sessions.get(sid)
                    if session:
                        self._send(200, {"found": True, **session})
                    else:
                        self._send(404, {"found": False})
                    return
                self._send(404, {"error": "unknown"})

            def do_POST(self):                   # noqa: N802
                if panel.offline:
                    self._send(503, {"error": "offline"})
                    return
                payload = self._read()
                self._record(payload)
                if not self._auth():
                    return
                sid = self._session_id(payload)
                if self.path == "/session/start":
                    with panel.lock:
                        if sid in panel.sessions:
                            session = panel.sessions[sid]
                        else:
                            want = payload.get("slot")
                            free = panel.free()
                            slot = int(want) if want is not None and int(want) in free \
                                else (free[0] if free else None)
                            if slot is None:
                                self._send(409, {"error": "no free lanes", "free": []})
                                return
                            session = {"slot": slot, "key": str(slot), "state": "idle",
                                       "label": payload.get("label"),
                                       "ident": payload.get("ident"),
                                       "agent": payload.get("agent"),
                                       "host": payload.get("host"), "info": {}}
                            panel.sessions[sid] = session
                    self._send(200, {"slot": session["slot"], "key": session["key"],
                                     "devices": ["mock"]})
                    return
                if not sid or sid not in panel.sessions:
                    self._send(404, {"found": False})
                    return
                if self.path == "/session/state":
                    with panel.lock:
                        panel.sessions[sid]["state"] = payload.get("state")
                    self._send(200, {"ok": True})
                    return
                if self.path == "/session/info":
                    with panel.lock:
                        info = panel.sessions[sid].setdefault("info", {})
                        for key, value in (payload.get("info") or {}).items():
                            if value is None:
                                info.pop(key, None)
                            elif isinstance(value, dict) and isinstance(info.get(key), dict):
                                info[key].update(value)
                            else:
                                info[key] = value
                    self._send(200, {"ok": True})
                    return
                if self.path == "/session/end":
                    with panel.lock:
                        panel.sessions.pop(sid, None)
                    self._send(200, {"ok": True})
                    return
                self._send(404, {"error": "unknown"})

        return Handler
