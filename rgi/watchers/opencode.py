"""Watch OpenCode sessions and report them as lanes.

    rgi watch --url http://127.0.0.1:8730 --token <token>

OpenCode's HTTP API is reached through its own CLI (``opencode api get ...``),
which handles authentication, so no credentials are read or stored here.

    session running            -> working   (green)
    session stopped            -> done      (blinks white, then holds)
    permission prompt pending  -> blocked   (blinks red)

A lane is only given up when the session has been idle for ``--stale`` seconds,
or when another session needs the lamp and it is the least recently used. That
is deliberate: the point of the indicator is that a number keeps meaning the same
session. (An early version freed the lamp 45 s after every turn and never sent
``working`` again for a session that had already landed - so a lane lit once and
then vanished. Both are fixed here.)

Everything it decides is appended to ``~/.config/rgi/watcher.log``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8730"
ACTIVE_POLL = 1.5        # how often to ask OpenCode what is running
SESSION_POLL = 20.0      # how often to refresh titles and last-used times
DONE_GRACE = 3.0         # a stop this short is not a finished turn
STALE_DEFAULT = 2 * 3600

LOG_PATH = os.path.join(os.path.expanduser("~"), ".config", "rgi", "watcher.log")


def say(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def opencode_exe() -> str:
    """Resolve the CLI once.

    Never pass shell=True with an argument list: on POSIX that runs only the
    first element, so ``opencode api get <path>`` becomes a bare ``opencode``
    that hangs. Windows hides that because cmd.exe joins the list - and it needs
    this lookup because the npm shim is ``opencode.cmd``, which CreateProcess
    will not find by bare name.
    """
    return shutil.which("opencode") or "opencode"


OPENCODE = opencode_exe()


class Watcher:
    def __init__(self, url: str = DEFAULT_URL, token: str | None = None,
                 include_subagents: bool = False, stale: float = STALE_DEFAULT):
        self.url = url.rstrip("/")
        self.token = token
        self.include_subagents = include_subagents
        self.stale = stale
        self.host = socket.gethostname()
        self.bound: dict[str, dict] = {}
        self.parents: dict[str, str | None] = {}
        self.titles: dict[str, str] = {}
        self.updated: dict[str, float] = {}
        self.last_meta = 0.0
        self.warned = False
        self.last_error = ""

    # -- HTTP -------------------------------------------------------------
    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-LED-Token"] = self.token
        return h

    def get(self, path: str) -> dict | None:
        try:
            req = urllib.request.Request(self.url + path, headers=self.headers())
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read() or b"{}")
        except Exception:
            return None

    def post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url + path, data=body,
                                     headers=self.headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            self.last_error = f"HTTP {exc.code}"
            if exc.code == 401:
                self.last_error = "HTTP 401 - the panel wants X-LED-Token"
            return {"error": self.last_error}
        except Exception as exc:
            self.last_error = str(exc)
            return {"error": self.last_error}

    # -- OpenCode ---------------------------------------------------------
    def cli(self, path: str, timeout: float = 25.0):
        try:
            proc = subprocess.run([OPENCODE, "api", "get", path],
                                  capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if proc.returncode != 0:
            return None, (proc.stderr or "").strip()[:200] or f"exit {proc.returncode}"
        text = (proc.stdout or "").strip()
        if not text:
            return None, "empty response"
        try:
            return json.loads(text), None
        except Exception as exc:
            return None, f"bad json: {exc}"

    # -- lanes ------------------------------------------------------------
    def bind(self, sid: str, want: int | None = None) -> dict | None:
        payload = {"agent": "opencode", "sessionID": sid,
                   "label": self.titles.get(sid) or sid, "host": self.host}
        if want is not None:
            payload["slot"] = want
        res = self.post("/session/start", payload)
        if "slot" not in res and want is not None:
            payload.pop("slot", None)          # someone took it; take any free lamp
            res = self.post("/session/start", payload)
        if "slot" not in res:
            return None
        return {"slot": res["slot"], "key": res.get("key"), "state": None,
                "stopped_at": None, "done_at": None, "ignored": False}

    def set_state(self, sid: str, state: str) -> bool:
        res = self.post("/session/state", {"sessionID": sid, "state": state})
        if res.get("ok"):
            return True
        info = self.bound.get(sid)
        fresh = self.bind(sid, want=info.get("slot") if info else None)
        if fresh is None:
            return False
        if info is not None:
            info.update(slot=fresh["slot"], key=fresh["key"])
        return bool(self.post("/session/state", {"sessionID": sid, "state": state}).get("ok"))

    def land(self, sid: str, reason: str) -> None:
        info = self.bound[sid]
        state = info.get("state")
        if state in ("working", "blocked", None):
            info["stopped_at"] = time.monotonic()
            info["state"] = "stopping"
            return
        if state != "stopping":
            return
        if time.monotonic() - (info["stopped_at"] or 0) < DONE_GRACE:
            return
        if self.set_state(sid, "done"):
            info.update(state="done", done_at=time.monotonic(), stopped_at=None)
            say(f"[land] {sid[-12:]} -> {info.get('key')}  ({reason})")

    def free(self, sid: str, reason: str) -> None:
        self.post("/session/end", {"sessionID": sid})
        info = self.bound.pop(sid, None)
        if info:
            say(f"[free] {sid[-12:]} -> {info.get('key')}  ({reason})")

    def refresh_metadata(self) -> None:
        data, err = self.cli("/api/session")
        if data is None:
            if not self.warned:
                say(f"[warn] could not read /api/session: {err}")
            return
        for s in data.get("data", []):
            sid = s.get("id")
            if not sid:
                continue
            self.parents[sid] = s.get("parentID")
            self.titles[sid] = s.get("title")
            self.updated[sid] = ((s.get("time") or {}).get("updated") or 0) / 1000.0

    def attention(self) -> set[str]:
        """Sessions with an unanswered permission prompt (OpenCode's own API)."""
        data, _ = self.cli("/api/permission/request")
        if data is None:
            return set()
        return {item.get("sessionID") for item in (data.get("data") or []) if item.get("sessionID")}

    def stale_sessions(self) -> list[str]:
        now = time.time()
        out = []
        for sid, info in self.bound.items():
            if info.get("ignored") or info.get("state") in ("working", "stopping", "blocked"):
                continue
            last = self.updated.get(sid) or 0.0
            idle = (now - last) if last else (time.monotonic() - (info.get("done_at") or time.monotonic()))
            if idle > self.stale:
                out.append((idle, sid))
        out.sort(reverse=True)
        return [sid for _, sid in out]

    def reconcile(self) -> None:
        status = self.get("/status")
        if status is None:
            return
        live = set((status.get("sessions") or {}).keys())
        for sid, info in list(self.bound.items()):
            if info.get("ignored") or info.get("slot") is None or sid in live:
                continue
            fresh = self.bind(sid, want=info.get("slot"))
            if fresh is None:
                say(f"[warn] could not re-claim {sid[-12:]}: the panel refused a lamp")
                continue
            if fresh["slot"] != info.get("slot"):
                say(f"[slot] {sid[-12:]} was {info.get('slot')}, now {fresh['slot']}")
            info.update(slot=fresh["slot"], key=fresh["key"])
            state = info.get("state") or "working"
            if state == "stopping":
                state = "working"
            if self.set_state(sid, state):
                info["state"] = state
                say(f"[again] {sid[-12:]} -> {info.get('key')}  ({state})")

    def run(self) -> None:
        say(f"watching OpenCode; panel at {self.url}")
        say(f"subagents: {'included' if self.include_subagents else 'ignored'}"
            f"   lanes freed after {self.stale / 60:.0f} min idle")
        say(f"host reported as {self.host}; opencode CLI: {OPENCODE}")

        while True:
            if time.time() - self.last_meta > SESSION_POLL:
                self.refresh_metadata()
                self.reconcile()
                self.last_meta = time.time()

            active, err = self.cli("/api/session/active")
            if active is None:
                if not self.warned:
                    say(f"[warn] could not read /api/session/active: {err}")
                    self.warned = True
                time.sleep(ACTIVE_POLL)
                continue
            self.warned = False

            running = set((active.get("data") or {}).keys())
            attention = self.attention()

            for sid in sorted(running):
                if sid in self.bound:
                    continue
                if self.parents.get(sid) and not self.include_subagents:
                    self.bound[sid] = {"slot": None, "key": None, "state": None,
                                       "ignored": True, "stopped_at": None, "done_at": None}
                    continue
                info = self.bind(sid)
                if info is None:
                    # never silent: a refused lane is usually a missing token or a
                    # full panel, and both are worth knowing about
                    say(f"[warn] could not claim a lane for {sid[-12:]} "
                        f"(panel refused it: {self.last_error or 'no free lanes'})")
                    continue
                self.bound[sid] = info
                if self.set_state(sid, "working"):
                    info["state"] = "working"
                    say(f"[bind] {sid[-12:]} -> {info.get('key')}  (in flight)")

            for sid, info in list(self.bound.items()):
                if info.get("ignored"):
                    if sid not in running:
                        self.bound.pop(sid, None)
                    continue

                if sid in attention:
                    if info.get("state") != "blocked" and self.set_state(sid, "blocked"):
                        info.update(state="blocked", stopped_at=None)
                        say(f"[hold] {sid[-12:]} -> {info.get('key')}  (needs you)")
                    continue

                if sid in running:
                    if info.get("state") != "working":
                        was = info.get("state")
                        if self.set_state(sid, "working"):
                            if was == "done":
                                say(f"[go]   {sid[-12:]} -> {info.get('key')}  (working again)")
                            elif was == "blocked":
                                say(f"[work] {sid[-12:]} -> {info.get('key')}  (unblocked)")
                            info["state"] = "working"
                    info["stopped_at"] = None
                    info["done_at"] = None
                    continue

                self.land(sid, "turn finished")

            for sid in self.stale_sessions():
                self.free(sid, f"idle > {self.stale / 60:.0f} min")

            time.sleep(ACTIVE_POLL)
