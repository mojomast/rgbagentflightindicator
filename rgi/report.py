"""The shared reporter: one small client every integration speaks through.

An integration should never need to know how the panel is addressed, how a
session is named, or what happens when the daemon is not there. It tells this
class what happened; this class does the rest, and never raises into the agent.

The rules it keeps, because each of them was a bug somewhere else first:

* A lane's identity is ``<namespace>:<session>``, so two harnesses cannot
  collide, and a resumed conversation lands on the same lane again.
* One lane per root workflow. Children are reported as metadata and never claim
  a lamp of their own.
* Approval waits are counted by id, so concurrent requests cannot cancel each
  other, and a quiet wait never looks stale - blocked lanes are never freed.
* Events carry an ``at`` timestamp and the newest accepted one is persisted per
  session, so an out-of-order hook cannot overwrite newer state.
* Slots are claimed explicitly from the free list, so a new adapter can never
  evict somebody else's blocked lane.
* Every HTTP call has a short timeout; every failure is logged once, swallowed,
  and reported as ``False``. An indicator that is down must not stop work.
* Published metadata is scrubbed: prompts, transcripts, tool arguments and
  credentials are never forwarded by default.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Callable, NamedTuple

from .config import CONFIG_DIR, resolve_ident, resolve_token, resolve_url

STATES = ("idle", "working", "blocked", "done", "error")

# harnesses spell the same states differently; one word reaches the panel
_ALIASES = {
    "running": "working", "active": "working", "in_progress": "working",
    "complete": "done", "completed": "done", "finished": "done", "success": "done",
    "succeeded": "done", "task_complete": "done", "settled": "done",
    "failed": "error", "failure": "error", "cancelled": "error", "canceled": "error",
    "aborted": "error",
    "waiting": "blocked", "attention": "blocked", "needs_input": "blocked",
    "requires_action": "blocked", "input_required": "blocked",
    "paused": "blocked",
    "inactive": "idle", "ready": "idle",
}

_SECRETS = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)\b(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+"),
)


def scrub(value: object, limit: int = 160) -> str:
    """One line, safe to publish: no secrets, no control characters, bounded."""
    clean = " ".join(str(value).split())
    for pattern in _SECRETS:
        clean = pattern.sub("[redacted]", clean)
    return clean[:limit]


def canonical_state(state: object) -> str | None:
    """Map a harness's word for a state to one of ours, or None."""
    word = str(state or "").strip().lower().replace("-", "_").replace(" ", "_")
    if word in STATES:
        return word
    return _ALIASES.get(word)


class Lane(NamedTuple):
    slot: int
    key: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        # A local service must not forward the panel credential somewhere else.
        return None


class Reporter:
    """Reports one root session to the panel, and never raises doing it."""

    def __init__(self, namespace: str, session: str, *, label: str | None = None,
                 url: str | None = None, token: str | None = None,
                 ident: str | None = None, timeout: float = 2.0,
                 host: str | None = None, log: Callable[[str], None] | None = None,
                 state_dir: str | None = None, release_grace: float = 0.0):
        session = str(session).strip()
        if not session:
            raise ValueError("session must be a stable, non-empty id")
        self.namespace = str(namespace).strip() or "agent"
        self.session = session
        self.session_key = f"{self.namespace}:{session}"
        self.label = scrub(label or session, 80) or session
        self.url = resolve_url(url)
        self.token = resolve_token(token)
        self.ident = resolve_ident(ident)
        self.host = host or socket.gethostname()
        self.timeout = min(10.0, max(0.1, float(timeout)))
        self._log = log or self._default_log
        self._state_dir = (state_dir or os.environ.get("RGI_STATE_DIR")
                           or os.path.join(CONFIG_DIR, "integrations"))
        self._release_grace = max(0.0, float(release_grace or 0.0))

        self.lane: Lane | None = None
        self._intent = "idle"
        self._sent_state: str | None = None
        self._pending: dict[str, dict] = {}
        self._children: dict[str, dict] = {}
        self._quiet: set[str] = set()
        self._last_info: str | None = None
        self._online = False
        self._timer: threading.Timer | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

    # -- logging -----------------------------------------------------------
    def _default_log(self, message: str) -> None:
        if os.environ.get("RGI_HOOK_DEBUG") or os.environ.get("RGI_DEBUG"):
            print(f"[rgi] {message}", file=sys.stderr)

    def _say_once(self, kind: str, message: str) -> None:
        if kind in self._quiet:
            return
        self._quiet.add(kind)
        self._log(f"{kind}: {message}")

    # -- HTTP --------------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None):
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-LED-Token"] = self.token
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data,
                                         headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1 << 20)
            self._online = True
            return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            self._online = code < 500
            if code == 401:
                self._say_once("panel_auth", "the panel wants X-LED-Token; "
                               "check RGI_TOKEN or ~/.config/rgi/token")
            return code, {}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._online = False
            self._say_once("panel_unreachable", f"{self.url} did not answer "
                           f"({exc}); carrying on without an indicator")
            return None, {}

    def _get(self, path: str):  # noqa: D401
        return self._request("GET", path)

    def _post(self, path: str, payload: dict):
        return self._request("POST", path, payload)

    def _key(self) -> str:
        return urllib.parse.quote(self.session_key, safe="")

    # -- event ordering ----------------------------------------------------
    def _stamp_path(self) -> str:
        digest = hashlib.sha1(self.session_key.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self._state_dir, f"{digest}.json")

    def _read_stamp(self) -> float | None:
        try:
            with open(self._stamp_path(), encoding="utf-8") as fh:
                return float(json.load(fh)["at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_stamp(self, at: float, state: str) -> None:
        path = self._stamp_path()
        try:
            os.makedirs(self._state_dir, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"at": at, "state": state, "session": self.session_key}, fh)
            os.replace(tmp, path)
        except OSError:
            pass

    def _accept(self, at: float | None) -> float | None:
        stamp = time.time() if at is None else float(at)
        stored = self._read_stamp()
        if stored is not None and stamp < stored:
            self._log(f"dropped an out-of-order event for {self.session_key} "
                      f"({stamp:.3f} < {stored:.3f})")
            return None
        return stamp

    # -- lane lifecycle ----------------------------------------------------
    def _effective(self) -> str:
        return "blocked" if self._pending else self._intent

    def start(self, *, label: str | None = None, slot: int | None = None) -> bool:
        """Adopt the lane for this session, or claim a free one. Never evicts."""
        with self._lock:
            status, body = self._get(f"/session/{self._key()}")
            if status == 200 and body.get("found"):
                self.lane = Lane(int(body.get("slot", 0)), str(body.get("key", "")))
                if body.get("state"):
                    self._sent_state = str(body["state"])
                return True
            if label:
                self.label = scrub(label, 80) or self.label
            if slot is None:
                status, body = self._get("/status")
                if status != 200:
                    return False
                free = [int(s) for s in (body.get("free") or [])]
                if not free:
                    self._say_once("no_lane", "no free lamps; reporting without "
                                   "an indicator rather than evicting someone")
                    return False
                slot = free[0]
            status, body = self._post("/session/start", {
                "sessionID": self.session_key, "label": self.label,
                "agent": self.namespace, "ident": self.ident,
                "host": self.host, "slot": int(slot),
            })
            if status == 200 and "slot" in body:
                self.lane = Lane(int(body["slot"]), str(body.get("key", "")))
                self._sent_state = None
                return True
            if status == 409:
                self._say_once("lane_taken", f"slot {slot} was taken; reporting "
                               "without an indicator")
            return False

    def _push_state(self, state: str, at: float) -> bool:
        if self.lane is None and not self.start():
            return False
        status, body = self._post("/session/state", {
            "sessionID": self.session_key, "state": state})
        if status == 404:
            # the daemon restarted, or the lane was freed while we waited
            self.lane = None
            if not self.start():
                return False
            status, body = self._post("/session/state", {
                "sessionID": self.session_key, "state": state})
        if status != 200 or not body.get("ok"):
            return False
        self._sent_state = state
        self._write_stamp(at, state)
        return True

    def state(self, state: str, *, at: float | None = None, force: bool = False) -> bool:
        """Report a state. `at` is the event's own timestamp when it has one.

        A repeated state is a no-op that returns True: there is nothing new to
        say. A *change* against an unreachable panel returns False.
        """
        canonical = canonical_state(state)
        if canonical is None:
            return False
        with self._lock:
            stamp = self._accept(at)
            if stamp is None:
                return False
            if canonical != "blocked":
                self._intent = canonical
            effective = self._effective()
            if not force and effective == self._sent_state and self._online:
                return True
            return self._push_state(effective, stamp)

    def working(self, *, at: float | None = None) -> bool:
        return self.state("working", at=at)

    def idle(self, *, at: float | None = None) -> bool:
        return self.state("idle", at=at)

    def error(self, message: str = "", *, at: float | None = None) -> bool:
        if message:
            self.info({"error": scrub(message, 120)})
        return self.state("error", at=at, force=True)

    def done(self, *, at: float | None = None, release_after: float | None = None) -> bool:
        """Complete, keeping the lane visible. Release explicitly, or time it."""
        with self._lock:
            ok = self.state("done", at=at, force=True)
            if self._pending:
                return ok          # a real wait outlives the completed result
            grace = self._release_grace if release_after is None else float(release_after)
            if grace > 0:
                self._arm_release(grace)
            return ok

    def _arm_release(self, grace: float) -> None:
        self._cancel_release()
        self._timer = threading.Timer(grace, self.end)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_release(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def end(self) -> bool:
        """Release the lane. Safe to call twice, and safe on a dead daemon."""
        with self._lock:
            self._cancel_release()
            if self.lane is None:
                return True
            status, _ = self._post("/session/end", {"sessionID": self.session_key})
            if status == 200 or status == 404:
                self.lane = None
                self._sent_state = None
                self._write_stamp(time.time(), "ended")
                return True
            return False

    def close(self) -> None:
        self._stop.set()
        self.end()

    # -- attention ---------------------------------------------------------
    def blocked(self, *, request: str | None = None, action: str = "",
                message: str = "", at: float | None = None) -> bool:
        """Record one unresolved human wait, by id. Concurrent ids all count."""
        with self._lock:
            rid = scrub(request or "attention", 64) or "attention"
            self._pending[rid] = {"action": scrub(action, 40),
                                  "message": scrub(message, 120)}
            self._push_attention()
            return self.state("blocked", at=at, force=True)

    def resolve(self, request: str | None = None, *, at: float | None = None) -> bool:
        """Resolve one wait (or all of them), returning to the last intent."""
        with self._lock:
            if request is None:
                self._pending.clear()
            else:
                self._pending.pop(scrub(request, 64), None)
            self._push_attention()
            return self.state(self._intent, at=at, force=True)

    @property
    def pending(self) -> list[str]:
        return sorted(self._pending)

    def _push_attention(self) -> bool:
        ids = sorted(self._pending)
        fields: dict = {"pending_requests": ids}
        if ids:
            first = self._pending[ids[0]]
            fields["blocked_on"] = {
                "action": first["action"] or "input",
                "message": first["message"] or f"{len(ids)} request(s) pending",
                "resources": [i for i in ids[:3]],
            }
        else:
            fields["blocked_on"] = None
        return self.info(fields)

    # -- metadata ----------------------------------------------------------
    def info(self, fields: dict, *, at: float | None = None) -> bool:
        """Publish detail. Strings are scrubbed; nothing else is inspected."""
        if not isinstance(fields, dict) or not fields:
            return False
        clean: dict = {}
        for key, value in fields.items():
            key = str(key)[:40]
            if isinstance(value, str):
                clean[key] = scrub(value)
            elif isinstance(value, (int, float, bool)) or value is None:
                clean[key] = value
            elif isinstance(value, (list, dict)):
                clean[key] = _scrub_deep(value)
            else:
                clean[key] = scrub(value)
        signature = json.dumps(clean, sort_keys=True, default=str)
        with self._lock:
            if signature == self._last_info and self._online:
                return True
            if self.lane is None and not self.start():
                return False
            status, _ = self._post("/session/info",
                                   {"sessionID": self.session_key, "info": clean})
            if status == 404:
                self.lane = None
                if not self.start():
                    return False
                status, _ = self._post("/session/info",
                                       {"sessionID": self.session_key, "info": clean})
            if status == 200:
                self._last_info = signature
                return True
            return False

    def child(self, child_id: str, *, state: str = "working", label: str = "",
              tokens: int | None = None) -> bool:
        """A subagent or task under this lane: metadata, never a lamp."""
        with self._lock:
            cid = scrub(child_id, 64)
            if not cid:
                return False
            canonical = canonical_state(state) or "working"
            entry: dict = {"id": cid, "state": canonical,
                           "label": scrub(label or cid, 60)}
            if tokens is not None:
                try:
                    entry["tokens"] = int(tokens)
                except (TypeError, ValueError):
                    pass
            self._children[cid] = entry
            children = list(self._children.values())[-12:]
            return self.info({"children": children})

    def child_done(self, child_id: str) -> bool:
        return self.child(child_id, state="done")

    def heartbeat(self) -> bool:
        """Prove liveness during a long quiet wait, and recover a lost lane."""
        with self._lock:
            if self.lane is None:
                return self.start()
            status, _ = self._post("/session/info", {
                "sessionID": self.session_key, "heartbeat": round(time.time(), 3)})
            if status == 404:
                self.lane = None
                return self.start()
            return status == 200

    @contextmanager
    def keepalive(self, interval: float = 30.0):
        """Heartbeat in the background for as long as the block is entered."""
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(interval):
                self.heartbeat()

        thread = threading.Thread(target=loop, name=f"rgi-keepalive-{self.session}",
                                  daemon=True)
        thread.start()
        try:
            yield self
        finally:
            stop.set()
            thread.join(timeout=1.0)

    def status(self) -> dict | None:
        code, body = self._get("/status")
        return body if code == 200 else None

    @property
    def online(self) -> bool:
        return self._online


def _scrub_deep(value, depth: int = 0):
    """Scrub strings inside the small structures we allow through."""
    if depth > 3:
        return None
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_deep(v, depth + 1) for v in value[:20]]
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:20]:
            if str(key).lower() in ("prompt", "prompts", "messages", "transcript",
                                    "content", "arguments", "text"):
                continue            # never forward raw conversation or tool input
            out[str(key)[:40]] = _scrub_deep(item, depth + 1)
        return out
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return scrub(value)
