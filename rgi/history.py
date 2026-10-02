"""A bounded transition history, and the digest that reads it back.

``/status`` is in-memory and dies with the daemon; the Home Assistant bridge's
events are transient. This module is the cheap durable answer: one JSON line per
lane event, rotated once it grows past ``max_bytes``, plus a pure ``digest()``
that answers "what happened while I was away?" without any service running.

The log is deliberately boring:

* one self-contained line per event, so a torn tail can only lose the last line;
* every write is append-only and every I/O error is swallowed - a full disk must
  never take the panel down;
* rotation keeps exactly one backup (``<path>.1``), so the total footprint is
  bounded at roughly two files;
* cost, token and context numbers are taken from the lane's ``info`` if the
  producing integration published them, and are ``null`` otherwise.

``digest()`` is a pure function over the files. It reads line by line, skips
malformed lines, and aggregates:

``lanes``
    distinct sessions that produced at least one event in the window;
``done`` / ``error`` / ``blocked``
    transitions *into* those states (a repeated state is not a new event);
``blocked_open``
    sessions whose last observed state is ``blocked`` - the ones still waiting;
``spend``
    the latest cumulative cost reported per session, summed (never a sum over
    every line, which would multiply a running total);
``longest_wait_s``
    the longest blocked interval: closed by the next state change or an ``end``,
    or still open at ``now``;
``by_host``
    sessions per host, which is the fleet breakdown without polling anything.

Use :func:`text` to turn a summary into one human line::

    rgi digest: 3 lanes | 2 done, 0 error, 1 blocked (1 open) | $4.21 |
    longest wait 12m30s | hosts workstation=2, laptop=1
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator

#: the keys of one persisted line, in order
FIELDS = ("at", "kind", "session", "slot", "state", "host", "ident", "label",
          "cost", "tokens_in", "tokens_out", "context_percent")

#: the states the digest counts transitions into
TRACKED = ("done", "error", "blocked")

_TERMINAL_KINDS = ("end", "clear")


def _number(value: object) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(*values: object) -> int | float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _slot(lane: dict, event) -> int | None:
    value = lane.get("slot", getattr(event, "slot", None))
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record(event, at: float) -> dict:
    """One event reduced to the bounded, persisted shape."""
    lane = event.lane if isinstance(getattr(event, "lane", None), dict) else {}
    info = lane.get("info") if isinstance(lane.get("info"), dict) else {}
    tokens = info.get("tokens") if isinstance(info.get("tokens"), dict) else {}
    context = info.get("context") if isinstance(info.get("context"), dict) else {}
    return {
        "at": at,
        "kind": str(getattr(event, "kind", "") or ""),
        "session": str(getattr(event, "session_id", "") or ""),
        "slot": _slot(lane, event),
        "state": lane.get("state"),
        "host": lane.get("host"),
        "ident": lane.get("ident"),
        "label": lane.get("label"),
        "cost": _first_number(tokens.get("cost"), info.get("cost")),
        "tokens_in": _first_number(tokens.get("input"), tokens.get("in"),
                                   info.get("tokens_in")),
        "tokens_out": _first_number(tokens.get("output"), tokens.get("out"),
                                    info.get("tokens_out")),
        "context_percent": _first_number(context.get("percent"),
                                        info.get("context_percent")),
    }


class HistoryLog:
    """Append lane events to ``path`` as JSON lines, rotating once.

    Subscribes when given a hub, but ``handle`` is equally usable directly
    (``hub.subscribe(history.handle)``). Every failure is swallowed and the
    subscriber never raises into the hub.
    """

    def __init__(self, path: str, *, clock: Callable[[], float] = time.time,
                 max_bytes: int = 2_000_000, hub=None):
        self.path = str(path)
        self.backup_path = self.path + ".1"
        self._clock = clock
        try:
            self._max_bytes = max(256, int(max_bytes))
        except (TypeError, ValueError):
            self._max_bytes = 2_000_000
        self._lock = threading.Lock()
        if hub is not None:
            hub.subscribe(self.handle)

    def handle(self, event) -> None:
        """Persist one event. Never raises; a failed write is simply lost."""
        try:
            at = getattr(event, "at", None)
            at = _number(at)
            if at is None:
                at = self._clock()
            blob = (json.dumps(record(event, float(at)), separators=(",", ":"),
                               default=str) + "\n").encode("utf-8")
            self._append(blob)
        except Exception:                          # isolation on purpose
            pass

    def _append(self, blob: bytes) -> None:
        with self._lock:
            try:
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                try:
                    size = os.path.getsize(self.path)
                except OSError:
                    size = 0
                if size > 0 and size + len(blob) > self._max_bytes:
                    self._rotate()
                with open(self.path, "ab") as fh:
                    fh.write(blob)
            except Exception:                      # a broken log is not fatal
                pass

    def _rotate(self) -> None:
        """Keep one backup: ``path`` -> ``path.1``, replacing the old backup."""
        try:
            os.replace(self.path, self.backup_path)
        except OSError:
            try:
                os.remove(self.path)
            except OSError:
                pass


def _read(path: str) -> Iterator[dict]:
    """Yield every well-formed record, oldest file first, line by line."""
    for candidate in (str(path) + ".1", str(path)):
        try:
            handle = open(candidate, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue                        # a torn or edited line
                if isinstance(data, dict):
                    yield data


def digest(path: str, since_epoch: float | None = None,
           *, now: float | None = None) -> dict:
    """Summarise the log since ``since_epoch`` (``None`` means all of it).

    ``now`` anchors still-open waits; it defaults to :func:`time.time` and is
    injectable so tests (and scripts replaying a window) stay deterministic.
    """
    sessions: dict[str, dict] = {}
    latest: float | None = None
    for row in _read(path):
        at = _number(row.get("at"))
        if at is None:
            continue
        if since_epoch is not None and at < since_epoch:
            continue
        latest = at if latest is None else max(latest, at)
        session = row.get("session")
        session = session if isinstance(session, str) and session else "?"
        entry = sessions.setdefault(session, {
            "host": "", "state": None, "open": False,
            "block_since": None, "cost": None,
        })
        host = row.get("host")
        if isinstance(host, str) and host:
            entry["host"] = host
        state = row.get("state")
        state = state if isinstance(state, str) and state else None
        kind = row.get("kind")
        if kind in _TERMINAL_KINDS:
            # the lane is gone: close any open wait and forget its state
            if entry["block_since"] is not None:
                entry["wait"] = max(entry.get("wait") or 0.0,
                                    at - entry["block_since"])
            entry["block_since"] = None
            entry["state"] = None
            entry["open"] = False
        elif state is not None:
            previous = entry["state"]
            if state == "blocked" and previous != "blocked":
                entry["block_since"] = at
            elif previous == "blocked" and state != "blocked":
                if entry["block_since"] is not None:
                    entry["wait"] = max(entry.get("wait") or 0.0,
                                        at - entry["block_since"])
                entry["block_since"] = None
            if state in TRACKED and state != previous:
                entry[f"count_{state}"] = entry.get(f"count_{state}", 0) + 1
            entry["state"] = state
            entry["open"] = True
        cost = _number(row.get("cost"))
        if cost is not None:
            entry["cost"] = cost

    horizon = now if now is not None else (latest if latest is not None
                                           else time.time())
    longest = 0.0
    blocked_open = 0
    spend = 0.0
    by_host: dict[str, int] = {}
    for entry in sessions.values():
        if entry.get("wait"):
            longest = max(longest, float(entry["wait"]))
        if entry["state"] == "blocked" and entry["open"]:
            blocked_open += 1
            if entry["block_since"] is not None:
                longest = max(longest, max(0.0, horizon - entry["block_since"]))
        if entry["cost"] is not None:
            spend += float(entry["cost"])
        host = entry["host"] or "unknown"
        by_host[host] = by_host.get(host, 0) + 1

    return {
        "lanes": len(sessions),
        "done": sum(int(e.get("count_done") or 0) for e in sessions.values()),
        "error": sum(int(e.get("count_error") or 0) for e in sessions.values()),
        "blocked": sum(int(e.get("count_blocked") or 0) for e in sessions.values()),
        "blocked_open": blocked_open,
        "spend": round(spend, 6),
        "longest_wait_s": round(longest, 3),
        "by_host": by_host,
    }


def _duration(seconds: float) -> str:
    seconds = int(max(0.0, float(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def text(summary: dict) -> str:
    """One short human line for a :func:`digest` result."""
    if not isinstance(summary, dict):
        return "rgi digest: no data"
    lanes = int(summary.get("lanes") or 0)
    if lanes <= 0:
        return "rgi digest: no lane events in this window"
    parts = [f"{lanes} lane{'s' if lanes != 1 else ''}"]
    counts = []
    for state in TRACKED:
        number = int(summary.get(state) or 0)
        if state == "blocked" and int(summary.get("blocked_open") or 0):
            counts.append(f"{number} blocked ({int(summary['blocked_open'])} open)")
        else:
            counts.append(f"{number} {state}")
    parts.append(", ".join(counts))
    spend = summary.get("spend")
    if spend:
        parts.append(f"${float(spend):.2f}")
    wait = float(summary.get("longest_wait_s") or 0)
    if wait:
        parts.append(f"longest wait {_duration(wait)}")
    hosts = summary.get("by_host") or {}
    if isinstance(hosts, dict) and hosts:
        parts.append("hosts " + ", ".join(
            f"{name}={hosts[name]}" for name in sorted(hosts)))
    return "rgi digest: " + " | ".join(parts)
