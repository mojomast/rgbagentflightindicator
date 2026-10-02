"""Lane events: one stream every observer subscribes to.

The daemon mutates lanes; the notifier, the MQTT publisher, the history ring and
the UI all need the same transitions. Instead of each subsystem polling /status
or re-deriving edges, every mutation publishes a :class:`LaneEvent` carrying the
lane view *after* the change. Observers keep their own previous state to detect
edges.

Two rules keep this safe:

* subscribers run synchronously and must be fast; anything slow (a command, a
  network publish) belongs in the subscriber's own thread or queue;
* a subscriber that raises is disabled for the rest of the process and the
  failure is reported once - an observer must never take the panel down.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: event kinds, kept explicit so subscribers can switch on them
KINDS = ("start", "state", "info", "end", "clear", "ack", "stale", "recovered")


@dataclass(frozen=True)
class LaneEvent:
    kind: str
    session_id: str
    slot: int
    lane: dict
    at: float = field(default_factory=time.time)


class EventHub:
    def __init__(self, on_error=None):
        self._subscribers: list = []
        self._lock = threading.Lock()
        self._on_error = on_error

    def subscribe(self, fn) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def publish(self, event: LaneEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        broken = []
        for fn in subscribers:
            try:
                fn(event)
            except Exception as exc:            # noqa: BLE001 - isolation on purpose
                broken.append(fn)
                if self._on_error is not None:
                    try:
                        self._on_error(fn, exc)
                    except Exception:
                        pass
        if broken:
            with self._lock:
                for fn in broken:
                    if fn in self._subscribers:
                        self._subscribers.remove(fn)
