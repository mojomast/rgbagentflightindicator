"""The attention notifier: lane transitions in, one user command out.

The keyboard already says ``blocked`` to whoever is in the room. This module is
the out-of-room half: it watches the same :class:`~rgi.events.LaneEvent` stream
every other observer reads, keeps one small state machine per lane, and - only
when a lane *enters* an attention state - runs a command the user configured.
Notification routing is the user's job; rgi just decides *when* it is honest to
interrupt.

The rules are the ones the survey (and every noisy notification tool) says
matter:

* a notification is a **transition**, never a repeated state;
* it is **deduplicated by wait**: one notification per ``(session, wait)``, so a
  state that persists does not re-ring on every detail update;
* a **minimum duration** holds quick questions that answer themselves;
* a **global cooldown** caps bursts, and one optional **repeat** nudges a wait
  nobody answered;
* **quiet hours** silence everything except ``blocked`` (the one state that is
  always worth a night interruption);
* an **acknowledged** lane is silent while the ack is set;
* any later state change - working, done, error, end, clear - cancels pending
  work, so a notification can never arrive after the thing resolved.

Delivery is a plain argv list: the payload goes to stdin as JSON and the same
fields are exported as ``RGI_*`` environment variables, which is enough to wire
``ntfy``, ``curl``, ``notify-send``, ``osascript``, Power Automate or a shell
script without rgi learning any channel-specific code. The command runs in its
own daemon thread with a hard timeout; a broken command is logged and swallowed,
never raised into the event hub.

The notifier is driven by two entry points so it fits any cadence:

* ``handle(event)`` is the hub subscriber: it folds one event into the state
  machines and then calls ``poll()``;
* ``poll(now=None)`` is also safe to call from the daemon's existing tick, which
  is what lets the minimum duration and the repeat mature without a timer thread.

Usage::

    hub = EventHub()
    notifier = Notifier(hub, ["ntfy", "publish", "rgi", "needs you"])
    # hub publishes; the daemon also calls notifier.poll() on its tick
    ...
    notifier.stop()
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable

#: states that interrupt by default: a human gate and a failure
DEFAULT_STATES = ("blocked", "error")

#: a delivery gets this long before the worker is abandoned
COMMAND_TIMEOUT = 10.0

#: how long stop() waits for in-flight deliveries
STOP_TIMEOUT = 1.0


def parse_quiet_hours(text: object) -> list[tuple[int, int]]:
    """``"22:00-07:00"`` (or comma-separated ranges) -> minute pairs.

    A range whose end is not after its start wraps midnight. Anything that does
    not parse is ignored rather than fatal: a typo in a quiet-hours string must
    not stop a notification path.
    """
    ranges: list[tuple[int, int]] = []
    for chunk in str(text or "").split(","):
        chunk = chunk.strip()
        if "-" not in chunk:
            continue
        start_text, end_text = chunk.split("-", 1)
        start, end = _minutes(start_text), _minutes(end_text)
        if start is None or end is None:
            continue
        ranges.append((start, end))
    return ranges


def in_quiet_hours(ranges: list[tuple[int, int]], now: float) -> bool:
    """Whether local time ``now`` falls inside any of the parsed ranges."""
    if not ranges:
        return False
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    for start, end in ranges:
        if start <= end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:      # e.g. 22:00-07:00
            return True
    return False


def _minutes(value: object) -> int | None:
    try:
        hours, minutes = str(value).strip().split(":", 1)
        hours, minutes = int(hours), int(minutes)
    except (TypeError, ValueError):
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def _number(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return number


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


class Notifier:
    """Watch lane transitions and run one command when attention is needed.

    ``command`` is an argv list (or ``None`` for log-only operation). ``runner``
    is injected by tests and receives ``(argv, payload_json, env)``; the default
    runner executes the argv with :mod:`subprocess`, JSON on stdin and a hard
    :data:`COMMAND_TIMEOUT`. ``clock`` is :func:`time.time` by default and
    injected by tests.
    """

    def __init__(self, hub, command: list[str] | None, *,
                 states=DEFAULT_STATES, min_duration_s: float = 3.0,
                 cooldown_s: float = 30.0, repeat_s: float = 0.0,
                 quiet_hours: str = "", dry_run: bool = False,
                 log: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.time,
                 runner: Callable[..., object] | None = None):
        self.hub = hub
        self.command = self._clean_command(command)
        self.states = tuple(str(s).lower() for s in
                            (DEFAULT_STATES if states is None else states))
        self.min_duration_s = max(0.0, _number(min_duration_s, 3.0))
        self.cooldown_s = max(0.0, _number(cooldown_s, 30.0))
        self.repeat_s = max(0.0, _number(repeat_s, 0.0))
        self.quiet_ranges = parse_quiet_hours(quiet_hours)
        self.dry_run = bool(dry_run)
        self._log = log
        self._clock = clock
        self._runner = runner
        self._lock = threading.RLock()
        self._stopped = False
        self._seen: dict[str, str] = {}
        self._pending: dict[str, dict] = {}
        self._fired: set[tuple] = set()
        self._last_fire: float | None = None
        self._said: set[str] = set()
        self._threads: set[threading.Thread] = set()
        if hub is not None:
            hub.subscribe(self.handle)

    # -- construction helpers ----------------------------------------------
    @staticmethod
    def _clean_command(command) -> list[str] | None:
        if command is None:
            return None
        if isinstance(command, str):
            parts = [part for part in shlex.split(command) if part]
            return parts or None
        if isinstance(command, (list, tuple)):
            parts = [str(part) for part in command if str(part)]
            return parts or None
        return None

    # -- logging ------------------------------------------------------------
    def _say(self, level: str, message: str) -> None:
        if self._log is None:
            return
        try:
            try:
                self._log(level, message)
            except TypeError:                      # a one-argument logger
                self._log(f"{level}: {message}")
        except Exception:                          # logging must not raise
            pass

    def _say_once(self, kind: str, level: str, message: str) -> None:
        if kind in self._said:
            return
        self._said.add(kind)
        self._say(level, message)

    # -- event intake -------------------------------------------------------
    def handle(self, event) -> None:
        """One hub event. Never raises, and advances ``poll()`` afterwards."""
        try:
            self._handle(event)
        except Exception as exc:                   # isolation on purpose
            self._say("error", f"notify: event failed: {exc}")
        finally:
            self.poll()

    def _handle(self, event) -> None:
        session = str(getattr(event, "session_id", "") or "")
        kind = str(getattr(event, "kind", "") or "")
        with self._lock:
            if self._stopped:
                return
            if not session:
                if kind in ("end", "clear"):        # a global clear
                    self._pending.clear()
                    self._seen.clear()
                    self._fired.clear()
                return
        lane = getattr(event, "lane", None)
        lane = lane if isinstance(lane, dict) else {}
        with self._lock:
            if kind in ("end", "clear"):
                self._forget(session)
                self._seen.pop(session, None)
                return
            state = str(lane.get("state") or "").lower()
            previous = self._seen.get(session)
            edge = state != previous
            self._seen[session] = state
            key = self._dedup_key(session, lane, event)
            if state not in self.states:
                self._forget(session)              # any other state cancels
                return
            if edge:
                self._forget(session)              # a fresh entry is a fresh wait
            if bool(lane.get("acked")):
                self._pending.pop(session, None)
                self._fired.add(key)
                self._say("info", f"notify: {session} {state} acknowledged; suppressed")
                return
            candidate = self._pending.get(session)
            if candidate is None:
                if key in self._fired and not edge:
                    return                         # the same wait, already handled
                entered = lane.get("changed_at")
                if entered is None:
                    entered = getattr(event, "at", None)
                entered = _number(entered, self._clock())
                self._pending[session] = {
                    "session": session,
                    "state": state,
                    "key": key,
                    "lane": dict(lane),
                    "entered": entered,
                    "due": entered + self.min_duration_s,
                    "repeat": 0,
                    "kind": kind or "state",
                    "slot": getattr(event, "slot", lane.get("slot")),
                }
            else:
                candidate["lane"] = dict(lane)     # refresh label/what
                candidate["state"] = state
                candidate["slot"] = getattr(event, "slot", candidate.get("slot"))

    @staticmethod
    def _dedup_key(session: str, lane: dict, event) -> tuple:
        """One wait, one key: the lane's change stamp, else the request id."""
        changed = lane.get("changed_at")
        if changed:
            return (session, changed)
        info = lane.get("info") if isinstance(lane.get("info"), dict) else {}
        blocked_on = info.get("blocked_on")
        blocked_on = blocked_on if isinstance(blocked_on, dict) else {}
        resources = blocked_on.get("resources")
        request = resources[0] if isinstance(resources, list) and resources else None
        if request is None:
            pending = info.get("pending_requests")
            if isinstance(pending, list) and pending:
                request = pending[0]
        if request is None:
            request = getattr(event, "at", None)
        return (session, request)

    def _forget(self, session: str) -> None:
        """Drop a session's pending candidate and fired keys (lock held)."""
        self._pending.pop(session, None)
        self._fired = {key for key in self._fired if key[0] != session}

    # -- the clockwork ------------------------------------------------------
    def poll(self, now: float | None = None) -> None:
        """Fire whatever is due. Safe on the daemon tick; never raises."""
        try:
            self._poll(self._clock() if now is None else float(now))
        except Exception as exc:                   # isolation on purpose
            self._say("error", f"notify: poll failed: {exc}")

    def _poll(self, now: float) -> None:
        ready: list[dict] = []
        with self._lock:
            if self._stopped:
                return
            for session, candidate in list(self._pending.items()):
                if now < candidate["due"]:
                    continue
                lane = candidate["lane"]
                if bool(lane.get("acked")):
                    self._pending.pop(session, None)
                    self._fired.add(candidate["key"])
                    self._say("info",
                              f"notify: {session} {candidate['state']} "
                              "acknowledged; suppressed")
                    continue
                if (candidate["state"] != "blocked"
                        and in_quiet_hours(self.quiet_ranges, now)):
                    # suppress rather than queue: nobody wants the backlog at 07:00
                    self._pending.pop(session, None)
                    self._fired.add(candidate["key"])
                    self._say("info",
                              f"notify: {session} {candidate['state']} "
                              "suppressed by quiet hours")
                    continue
                if (self.cooldown_s and self._last_fire is not None
                        and now - self._last_fire < self.cooldown_s):
                    # still a real wait: hold it, deliver once the burst passes
                    candidate["due"] = self._last_fire + self.cooldown_s
                    continue
                self._pending.pop(session, None)
                self._fired.add(candidate["key"])
                self._last_fire = now
                ready.append(candidate)
                self._arm_repeat(candidate, now)
        for candidate in ready:
            try:
                self._deliver(candidate, now)
            except Exception as exc:               # one delivery must not block the rest
                self._say("error", f"notify: delivery failed: {exc}")

    def _arm_repeat(self, candidate: dict, now: float) -> None:
        """At most one repeat, and only while the state has not changed."""
        if self.repeat_s <= 0 or candidate.get("repeat"):
            return
        repeat = dict(candidate)
        repeat["repeat"] = candidate.get("repeat", 0) + 1
        repeat["due"] = now + self.repeat_s
        self._pending[candidate["session"]] = repeat

    # -- delivery -----------------------------------------------------------
    def _what(self, lane: dict) -> str:
        info = lane.get("info") if isinstance(lane.get("info"), dict) else {}
        blocked_on = info.get("blocked_on")
        blocked_on = blocked_on if isinstance(blocked_on, dict) else {}
        message = _text(blocked_on.get("message"))
        action = _text(blocked_on.get("action"))
        if message and action:
            return f"{action}: {message}"
        return message or action

    def _deliver(self, candidate: dict, now: float) -> None:
        lane = candidate["lane"]
        session = candidate["session"]
        state = candidate["state"]
        what = self._what(lane)
        payload = {
            "at": round(now, 3),
            "event": candidate.get("kind") or "state",
            "session": session,
            "state": state,
            "slot": candidate.get("slot"),
            "ident": _text(lane.get("ident")),
            "label": _text(lane.get("label")),
            "host": _text(lane.get("host")),
            "what": what,
        }
        description = (f"notify: {state} {session} task={payload['label']!r} "
                       f"host={payload['host']!r}"
                       + (f" what={what!r}" if what else ""))
        if self.command is None:
            self._say_once("no_command", "warn",
                           "notify: no command configured; decisions are logged only")
            self._say("info", description)
            return
        if self.dry_run:
            self._say("info", f"{description} (dry-run; command not run)")
            return
        self._say("info", description)
        env = dict(os.environ)
        env.update({
            "RGI_EVENT": payload["event"],
            "RGI_SESSION": session,
            "RGI_STATE": state,
            "RGI_SLOT": "" if payload["slot"] is None else str(payload["slot"]),
            "RGI_IDENT": payload["ident"],
            "RGI_LABEL": payload["label"],
            "RGI_HOST": payload["host"],
            "RGI_WHAT": payload["what"],
        })
        thread = threading.Thread(
            target=self._run,
            args=(list(self.command), json.dumps(payload, default=str), env),
            name=f"rgi-notify-{session}", daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()

    def _run(self, argv: list[str], payload: str, env: dict) -> None:
        try:
            if self._runner is not None:
                result = self._runner(argv, payload, env)
            else:
                result = subprocess.run(
                    argv, input=payload, text=True, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=COMMAND_TIMEOUT, check=False)
            code = getattr(result, "returncode", 0)
            if code not in (0, None):
                self._say("warn", f"notify: {argv[0]} exited {code}")
        except subprocess.TimeoutExpired:
            self._say("warn",
                      f"notify: {argv[0]} timed out after {COMMAND_TIMEOUT:g}s")
        except Exception as exc:                   # a command must not escape
            self._say("error", f"notify: {argv[0]} failed: {exc}")
        finally:
            with self._lock:
                self._threads.discard(threading.current_thread())

    def join(self, timeout: float | None = None) -> None:
        """Wait for in-flight deliveries; tests use this instead of sleeping."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                threads = list(self._threads)
            if not threads:
                return
            for thread in threads:
                if deadline is None:
                    thread.join(0.05)
                else:
                    thread.join(max(0.0, min(0.05, deadline - time.monotonic())))
            if deadline is not None and time.monotonic() >= deadline:
                return

    def stop(self) -> None:
        """Stop delivering and wait briefly for in-flight commands."""
        with self._lock:
            self._stopped = True
            self._pending.clear()
        self.join(STOP_TIMEOUT)
