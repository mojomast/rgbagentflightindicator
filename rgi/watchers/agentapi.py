"""Watch one AgentAPI-hosted agent and report it as a single lane.

    python -m rgi.watchers.agentapi --url http://127.0.0.1:3284 --name claude

`Coder AgentAPI <https://github.com/coder/agentapi>`_ is an adapter of
adapters: it drives 11+ coding CLIs through a PTY or ACP - Aider, Goose, Amp,
Auggie, Claude Code, Codex, Gemini, Amazon Q, OpenCode, Copilot and Cursor CLI
- and exposes every one of them through the same two endpoints:

    GET /status   {"status": "running" | "stable", "agent_type": "claude",
                   "transport": "pty" | "acp", ...}
    GET /events   Server-Sent Events: ``status_change``, ``message`` (or
                  ``message_update``), ``turn_completed`` and ``agent_error``

AgentAPI was archived in September 2026, but it still works, is MIT, and is
the cheapest way to cover the CLIs that have no hook surface of their own
(Aider and Goose especially). This watcher is optional and opt-in: it is used
only when the human points it at a running AgentAPI instance.

The mapping, and why:

* ``status_change running`` -> ``working``;
* ``turn_completed`` -> ``done``. When the event carries token usage it also
  lands as lane detail (``tokens``);
* ``status_change stable`` after a turn has run -> ``done`` briefly, then
  ``idle`` after a short grace: the lamp stays on the session, it just stops
  saying "this turn is still going";
* ``status_change stable`` with no turn seen -> ``idle`` (the instance is up
  and waiting);
* ``agent_error`` -> ``error``, unless the level is ``warning``, which is
  detail rather than a red lamp. The next state event recovers the lane;
* a lost connection changes *nothing*: it is logged once and the lane is left
  exactly as it was, then reconciled from ``/status`` and re-claimed when the
  connection comes back.

Lane identity is ``agentapi:<name>``, where ``<name>`` defaults to the URL's
host:port and an explicit ``--name`` overrides it. If the wire carries a
session id the lane becomes ``agentapi:<name>:<id>``, so an AgentAPI instance
that restarts with a fresh session gets a fresh lane instead of wearing the
old one's number.

Every call is best-effort with short timeouts; a panel or AgentAPI that is
down costs the indicator, never the agent. One watcher watches one AgentAPI
instance; run several (with distinct ``--name`` values) to cover several.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Iterator, NamedTuple

from ..config import CONFIG_DIR
from ..report import Reporter, scrub

DEFAULT_URL = "http://127.0.0.1:3284"
DEFAULT_POLL = 1.0          # control-loop tick while /events is quiet
DONE_GRACE = 3.0            # how long "done" holds before it becomes "idle"
HEARTBEAT = 10.0            # proves liveness, and recovers a lost lane
STALE_DEFAULT = 2 * 3600    # an idle lane is released after this long
BACKOFF_MIN = 1.0
BACKOFF_MAX = 30.0
HTTP_TIMEOUT = 5.0

LOG_PATH = os.path.join(CONFIG_DIR, "watcher.log")

# AgentAPI's event names have drifted between versions (the report names
# "message"/"turn_completed", the released schema says "message_update" and
# "agent_error"), so every known spelling is accepted.
_STATUS_EVENTS = {"status_change", "status", "status_update", "state_change", "state"}
_DONE_EVENTS = {"turn_completed", "turn_complete", "turn_finished", "turn_done",
                "completed", "completion"}
_MESSAGE_EVENTS = {"message", "message_update", "message_updated", "assistant_message"}
_ERROR_EVENTS = {"agent_error", "error", "failure"}

_RUNNING_WORDS = {"running", "busy", "active", "working", "in_progress"}
_STABLE_WORDS = {"stable", "idle", "ready", "waiting", "inactive", "paused"}

_USAGE_CONTAINERS = ("usage", "tokens", "token_usage", "tokenUsage", "usage_metadata")
_USAGE_KEYS = (
    ("input", ("input_tokens", "prompt_tokens", "input", "in", "prompt")),
    ("output", ("output_tokens", "completion_tokens", "output", "out", "completion")),
    ("cache_read", ("cache_read_input_tokens", "cached_tokens", "cache_read",
                    "cacheReads", "cache_read_tokens")),
    ("total", ("total_tokens", "totalTokens", "total")),
    ("cost", ("total_cost", "totalCost", "cost")),
)
_SESSION_KEYS = ("session_id", "sessionID", "sessionId", "session")


class Event(NamedTuple):
    """One event from the stream, after tolerant parsing."""

    name: str
    data: dict


def normalize_url(url: str) -> str:
    """A base URL with a scheme and no trailing slash."""
    value = str(url or "").strip().rstrip("/")
    if not value:
        return DEFAULT_URL
    if "://" not in value:
        value = "http://" + value
    return value


def lane_name(url: str, explicit: str | None = None) -> str:
    """The stable half of the lane key: ``--name`` or the URL's host:port."""
    value = str(explicit or "").strip()
    if value:
        return value
    parsed = urllib.parse.urlsplit(normalize_url(url))
    host = parsed.hostname or "agentapi"
    port = parsed.port
    return f"{host}:{port}" if port else host


class _FrameReader:
    """A tolerant line accumulator for SSE and NDJSON.

    Feed it lines as they arrive; it yields whole frames. Comments (``:``),
    ``id``/``retry``/unknown fields and blank lines are ignored, ``data``
    lines accumulate until the blank-line dispatch, and a line that is a JSON
    object on its own is treated as one NDJSON event. A malformed line can
    therefore never stop the stream; at worst one frame is skipped.
    """

    def __init__(self):
        self.name: str | None = None
        self.data: list[str] = []

    def feed(self, raw) -> Iterator[tuple[str, str]]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        line = raw.rstrip("\r\n")
        if not line.strip():
            if self.name or self.data:
                yield (self.name or "", "\n".join(self.data))
            self.name, self.data = None, []
            return
        if line.startswith(":"):
            return
        if line.lstrip().startswith("{"):
            yield ("", line.strip())
            return
        field, _, value = line.partition(":")
        field = field.strip().lower()
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self.name = value.strip()
        elif field == "data":
            self.data.append(value)
        # id, retry, and anything newer than this file: ignored

    def close(self) -> Iterator[tuple[str, str]]:
        if self.name or self.data:
            yield (self.name or "", "\n".join(self.data))
        self.name, self.data = None, []


def iter_events(lines) -> Iterator[tuple[str, str]]:
    """Yield ``(event_name, data_text)`` frames from SSE or NDJSON lines."""
    reader = _FrameReader()
    for raw in lines:
        yield from reader.feed(raw)
    yield from reader.close()


def parse_event(name: str, text: str) -> Event | None:
    """Turn one frame into an :class:`Event`, or ``None`` if it is junk.

    Handles both an SSE frame with the name on the ``event:`` line and a JSON
    payload that carries its own ``type``/``event`` field (NDJSON, or a newer
    AgentAPI), and unwraps a nested ``data`` object when there is one - so
    ``{"event": "status_change", "data": {"status": "running"}}`` and
    ``data: {"status": "running"}`` both work.
    """
    kind = str(name or "").strip()
    payload: dict = {}
    body = str(text or "").strip()
    if body:
        try:
            loaded = json.loads(body)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded
    inner = payload.get("event") or payload.get("type") or payload.get("event_type")
    if not kind and isinstance(inner, str):
        kind = inner
    kind = kind.strip().lower().replace("-", "_")
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    if not kind and not data:
        return None
    return Event(kind, data)


def status_of(data: dict) -> str | None:
    """``running`` or ``stable`` from whatever word an event used."""
    for key in ("status", "state", "phase"):
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("status") or value.get("state")
        if isinstance(value, str):
            word = value.strip().lower().replace("-", "_")
            if word in _RUNNING_WORDS:
                return "running"
            if word in _STABLE_WORDS:
                return "stable"
    return None


def session_id_of(data: dict) -> str:
    """A session id from an event, if this AgentAPI version sends one."""
    for key in _SESSION_KEYS:
        value = data.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            if str(value).strip():
                return str(value).strip()
    return ""


def usage_fields(data: dict) -> dict:
    """Token usage from a ``turn_completed`` payload, in the panel's words.

    Coalesces the spellings different agents/versions use, including usage
    nested under ``usage``/``tokens``/``token_usage``.
    """
    sources = [data]
    for key in _USAGE_CONTAINERS:
        nested = data.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    out: dict = {}
    for name, keys in _USAGE_KEYS:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    out[name] = value
                    break
            if name in out:
                break
    return out


def _log_line(message: str) -> None:
    """The standalone logger: stdout, and the shared watcher log."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _shutdown(response) -> None:
    """Wake a reader blocked inside ``recv`` without taking its buffer lock.

    ``BufferedReader.close`` waits for the lock that the blocked read holds, so
    closing first would deadlock; ``shutdown`` reaches the socket directly and
    makes the read return at once.
    """
    try:
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass


class AgentApiWatcher:
    """Follow one AgentAPI instance and drive one panel lane.

    Runs its control loop on one daemon thread (:meth:`start`, :meth:`stop`)
    or blocking (:meth:`run`). Each ``/events`` connection gets one short
    reader thread that feeds the control loop, because a stdlib HTTP stream
    cannot survive a socket read timeout. All panel work goes through the
    injected :class:`~rgi.report.Reporter`, which claims the lane, re-claims
    it after a daemon restart, and never evicts a blocked lane.
    """

    def __init__(self, url: str, *, reporter: Reporter, label: str | None = None,
                 host: str | None = None, poll_s: float = DEFAULT_POLL,
                 log: Callable[[str], None] | None = None,
                 done_grace: float = DONE_GRACE, stale: float = STALE_DEFAULT,
                 backoff: float = BACKOFF_MIN,
                 http_timeout: float = HTTP_TIMEOUT):
        self.url = normalize_url(url)
        self.reporter = reporter
        self.label = label
        if label:
            reporter.label = scrub(label, 80) or reporter.label
        self.name = lane_name(self.url)
        self.host = host or getattr(reporter, "host", None)
        self.poll_s = max(0.05, float(poll_s))
        self.done_grace = max(0.0, float(done_grace))
        self.stale = max(0.0, float(stale))
        self.backoff = max(0.05, float(backoff))
        self.http_timeout = max(0.2, min(HTTP_TIMEOUT, float(http_timeout)))
        self._log = log if callable(log) else print

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._resp_lock = threading.Lock()
        self._response = None
        self._said: set[str] = set()
        self._session_id = ""
        self._state: str | None = None
        self._done_at: float | None = None
        self._running_seen = False
        self._released = False
        self._last_activity = time.monotonic()
        self._last_beat = 0.0

    # -- logging -----------------------------------------------------------
    def say(self, message: str) -> None:
        try:
            self._log(f"[agentapi] {message}")
        except Exception:                              # a logger cannot break us
            pass

    def _say_once(self, kind: str, message: str) -> None:
        if kind in self._said:
            return
        self._said.add(kind)
        self.say(message)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Start the watcher loop on its one daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name=f"rgi-agentapi-{self.name}",
                                        daemon=True)
        self._thread.start()

    def run(self) -> None:
        """Watch until :meth:`stop` (used by ``main``)."""
        self.start()
        thread = self._thread
        if thread is not None:
            thread.join()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the loop to stop, unblocking any read, and wait briefly."""
        self._stop.set()
        with self._resp_lock:
            response = self._response
        if response is not None:
            _shutdown(response)
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)

    # -- HTTP --------------------------------------------------------------
    def _get_status(self) -> dict | None:
        """``GET /status`` with a short timeout; ``None`` means unreachable."""
        request = urllib.request.Request(self.url + "/status",
                                         headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.http_timeout) as response:
                raw = response.read(1 << 20)
            data = json.loads(raw or b"{}")
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _open_events(self):
        """``GET /events`` as a streaming response (SSE)."""
        request = urllib.request.Request(self.url + "/events", headers={
            "Accept": "text/event-stream, application/x-ndjson, application/json",
            "Cache-Control": "no-cache",
        })
        response = urllib.request.urlopen(request, timeout=self.http_timeout)
        try:
            # The reader blocks in readline(); a socket timeout poisons the
            # buffered reader ("cannot read from timed out object") and loses
            # the line, so once connected the stream is left blocking and the
            # control loop paces itself with its queue instead. If this
            # private handle ever moves, the reader just reconnects on the
            # timeout - slower, but still correct.
            response.fp.raw._sock.settimeout(None)
        except Exception:
            pass
        return response

    def _stream(self) -> str:
        """Read /events until it closes, errors, or stops."""
        try:
            response = self._open_events()
        except Exception as exc:
            self._say_once("stream", f"[warn] could not read /events: {exc}")
            return "error"
        if "stream" in self._said:
            self._said.discard("stream")
            self.say("[again] /events connected")
        with self._resp_lock:
            self._response = response

        lines: "queue.Queue" = queue.Queue()
        failure: list[BaseException] = []

        def reader() -> None:
            try:
                while True:
                    raw = response.readline()
                    if not raw:
                        break
                    lines.put(raw)
            except BaseException as exc:      # noqa: BLE001 - handed to the loop
                failure.append(exc)
            finally:
                lines.put(None)

        thread = threading.Thread(target=reader,
                                  name=f"rgi-agentapi-read-{self.name}",
                                  daemon=True)
        thread.start()
        frame = _FrameReader()
        try:
            while not self._stop.is_set():
                try:
                    raw = lines.get(timeout=self.poll_s)
                except queue.Empty:
                    self._tick()
                    continue
                if raw is None:
                    if failure:
                        self._say_once(
                            "read", f"[warn] /events stopped reading: {failure[0]}")
                        return "error"
                    return "closed"
                for name, text in frame.feed(raw):
                    event = parse_event(name, text)
                    if event is not None:
                        self._handle(event)
                    self._tick()
            return "stopped"
        finally:
            with self._resp_lock:
                self._response = None
            _shutdown(response)               # wake the reader, then close it
            thread.join(timeout=1.0)
            try:
                response.close()
            except Exception:
                pass

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(max(0.0, seconds))

    # -- the loop ----------------------------------------------------------
    def _run(self) -> None:
        self.say(f"[start] watching AgentAPI at {self.url} -> {self.reporter.session_key}")
        backoff = self.backoff
        try:
            while not self._stop.is_set():
                status = self._get_status()
                if status is None:
                    if "lost" not in self._said:
                        self._said.add("lost")
                        self.say("[warn] AgentAPI did not answer; leaving the lane "
                                 "as it was")
                    self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
                    continue
                if "lost" in self._said:
                    self._said.discard("lost")
                    self.say("[again] AgentAPI answered again")
                backoff = self.backoff
                self._reconcile(status)
                result = self._stream()
                if self._stop.is_set():
                    break
                if result in ("error", "closed"):
                    # a server that closes /events at once must not spin us
                    self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX)
        finally:
            self._release()

    def _reconcile(self, status: dict) -> None:
        """Bring the lane in line with ``/status``, and re-claim if needed."""
        session_id = session_id_of(status)
        if session_id:
            self._adopt_session(session_id)
        fields = {}
        for key in ("agent_type", "transport"):
            value = status.get(key)
            if isinstance(value, str) and value.strip():
                fields[key] = scrub(value.strip(), 40)
        if fields:
            self.reporter.info(fields)
        try:
            self.reporter.heartbeat()          # 404 -> claim the lane again
        except Exception:
            pass
        self._last_beat = time.monotonic()

        state = status_of(status)
        if state == "running":
            self._running("status says running")
        elif state == "stable":
            self._stable()

    # -- lane state --------------------------------------------------------
    def _handle(self, event: Event) -> None:
        data = event.data
        session_id = session_id_of(data)
        if session_id:
            self._adopt_session(session_id)
        kind = event.name
        if kind in _STATUS_EVENTS:
            state = status_of(data)
            if state == "running":
                self._running("status_change running")
            elif state == "stable":
                self._stable()
            return
        if kind in _DONE_EVENTS:
            self._finish("turn completed", data)
            return
        if kind in _MESSAGE_EVENTS:
            # Agent output is not lane state, and its content is never
            # forwarded; the status events already said "running".
            return
        if kind in _ERROR_EVENTS:
            self._failed(data)
            return
        # anything unknown is simply ignored

    def _running(self, reason: str) -> None:
        self._released = False
        self._running_seen = True
        self._done_at = None
        self._last_activity = time.monotonic()
        first = self._state != "working"
        # Always ask: the reporter dedupes, and after a panel restart this is
        # what repaints a lane that heartbeat() just re-claimed.
        if self.reporter.working():
            self._state = "working"
            if first:
                self.say(f"[go]   {self.reporter.session_key} (working: {reason})")

    def _stable(self) -> None:
        if self._state == "done":
            self._finish_grace()
            return
        if self._state in ("working",) or self._running_seen:
            self._finish("the turn ended")
        elif self._state is None:
            self._idle()
        elif self._state == "error":
            self._idle()

    def _finish(self, reason: str, data: dict | None = None) -> None:
        if self._state == "done":
            self._finish_grace()
            return
        if self._state == "idle" and not self._running_seen:
            # A replayed turn_completed from a previous connection. The lane
            # already settled to idle; do not blink "done" on every reconnect.
            return
        usage = usage_fields(data or {})
        if usage:
            self.reporter.info({"tokens": usage})
        self._released = False
        self._running_seen = False
        self._last_activity = time.monotonic()
        if self.reporter.done():
            self._state = "done"
            self._done_at = time.monotonic()
            self.say(f"[land] {self.reporter.session_key} (done: {reason})")

    def _idle(self) -> None:
        if self._released:
            return
        first = self._state != "idle"
        if self.reporter.idle():
            self._state = "idle"
            self._done_at = None
            if first:
                self.say(f"[idle] {self.reporter.session_key}")

    def _finish_grace(self) -> None:
        """``done`` holds for a moment, then settles to ``idle``."""
        if self._done_at is None:
            return
        if time.monotonic() - self._done_at >= self.done_grace:
            self._idle()

    def _failed(self, data: dict) -> None:
        if self._state == "idle" and not self._running_seen:
            # A replayed agent_error from before the lane settled: not news.
            return
        message = data.get("message") or data.get("error") or data.get("detail") or ""
        level = str(data.get("level") or "").strip().lower()
        if level == "warning":
            if message:
                self.reporter.info({"warning": scrub(message, 120)})
            return
        text = scrub(message, 120) or "AgentAPI reported an agent error"
        self._last_activity = time.monotonic()
        self.reporter.error(text)
        self._state = "error"

    # -- identity ----------------------------------------------------------
    def _adopt_session(self, session_id: str) -> None:
        """Move onto ``agentapi:<name>:<id>`` when the wire names a session.

        A restarted AgentAPI instance gets a fresh session id, and a fresh
        session should get a fresh lamp rather than inherit the one that still
        means the old conversation. A blocked lane is never released, so in
        that one case the switch waits.
        """
        session_id = str(session_id or "").strip()
        if not session_id or session_id == self._session_id:
            return
        if self.reporter.pending:
            self._say_once("switch_blocked",
                           "[warn] AgentAPI named a new session while the lane is "
                           "blocked; keeping the current lane until it resolves")
            return
        old = self._session_id
        if old or self.reporter.lane is not None:
            self.reporter.end()
        self._session_id = session_id
        session = f"{self.name}:{session_id}"
        reporter = self.reporter
        reporter.session = session
        reporter.session_key = f"{reporter.namespace}:{session}"
        reporter._sent_state = None
        reporter._last_info = None
        reporter.lane = None
        self._state = None
        self._done_at = None
        self._running_seen = False
        self._released = False
        self.say(f"[session] {old or self.name} -> {session}")

    # -- housekeeping ------------------------------------------------------
    def _tick(self) -> None:
        """Time-based work: done->idle grace, heartbeat, stale release."""
        now = time.monotonic()
        if self._state == "done":
            self._finish_grace()
        if (self._state in ("done", "idle") and not self._released
                and now - self._last_activity >= self.stale):
            self._release_stale()
        if now - self._last_beat >= HEARTBEAT:
            self._last_beat = now
            try:
                self.reporter.heartbeat()
            except Exception:
                pass

    def _release_stale(self) -> None:
        """Give back a lane that has been quiet for hours; never a blocked one."""
        if self.reporter.pending:
            return
        self.reporter.end()
        self._state = None
        self._running_seen = False
        self._released = True
        self.say(f"[free] {self.reporter.session_key} "
                 f"(idle > {self.stale / 3600:.0f}h)")

    def _release(self) -> None:
        """On shutdown, release the lane - unless a human still owes it input."""
        if self.reporter.pending:
            self.say("[warn] leaving the lane claimed: a blocked lane is never freed")
            return
        try:
            self.reporter.end()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    """Run one AgentAPI watcher with no package around it.

        python -m rgi.watchers.agentapi --url http://127.0.0.1:3284 --name claude

    The AgentAPI address comes from ``--url`` or ``RGI_AGENTAPI_URL``; the
    panel address and token resolve exactly as every other rgi client does
    (``RGI_URL``/``RGI_TOKEN`` or ``~/.config/rgi``).
    """
    parser = argparse.ArgumentParser(
        prog="python -m rgi.watchers.agentapi",
        description="report one AgentAPI-hosted agent to the panel")
    parser.add_argument("--url", default=None,
                        help="AgentAPI base url (default: RGI_AGENTAPI_URL, then "
                             + DEFAULT_URL + ")")
    parser.add_argument("--name", default=None,
                        help="lane name (default: the URL's host:port)")
    parser.add_argument("--label", default=None, help="lane label to display")
    parser.add_argument("--panel-url", default=None,
                        help="panel address (default: RGI_URL, then ~/.config/rgi/url)")
    parser.add_argument("--token", default=None,
                        help="panel token (default: RGI_TOKEN, then ~/.config/rgi/token)")
    parser.add_argument("--ident", default=None,
                        help="name this machine's lanes carry (default: RGI_IDENT, "
                             "then ~/.config/rgi/name, then the hostname)")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL,
                        help="seconds between control ticks while /events is quiet "
                             "(default: 1.0)")
    args = parser.parse_args(argv)

    url = normalize_url(args.url or os.environ.get("RGI_AGENTAPI_URL") or DEFAULT_URL)
    reporter = Reporter("agentapi", lane_name(url, args.name), label=args.label,
                        url=args.panel_url, token=args.token, ident=args.ident,
                        log=lambda message: _log_line(f"[agentapi] {message}"))
    watcher = AgentApiWatcher(url, reporter=reporter, label=args.label,
                              poll_s=args.poll,
                              log=lambda message: _log_line(message))
    try:
        watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
        print("\nstopped")
    return 0


if __name__ == "__main__":                     # pragma: no cover - entry point
    raise SystemExit(main())
