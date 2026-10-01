"""Publish RGI panel state to Home Assistant, through Home Assistant's own API.

Home Assistant's REST API can *represent* things without owning them. A ``POST``
to ``/api/states/<entity_id>`` updates or creates that state representation; it
does not talk to any device. Operating a device is a different call:
``POST /api/services/<domain>/<service>``. This bridge only ever writes state
and events, so a Home Assistant restart cannot make it fight an automation:

* it never reads back ``sensor.rgi_panel`` or listens for ``rgi_status_changed``;
* an automation triggered by that state or event is free to call a service
  (turn a light on, send a notification) without the bridge reacting to it.

The published surface is deliberately small and bounded: one aggregate entity,
counts per panel state, and at most :data:`MAX_SESSIONS` session entries with
exactly ``key``, ``name``, ``state``, ``slot`` and ``label``. Panel metadata
such as prompts, transcripts or tool arguments is never forwarded, and the
long-lived access token never reaches a log line.

Everything here is standard library. Panel I/O goes through
:class:`rgi.report.Reporter`; Home Assistant I/O uses urllib with a short
timeout, no redirects (a bearer token must not be forwarded to another host),
exponential backoff and a hard guarantee that no exception escapes.

Run it as ``python -m rgi.integrations.home_assistant``; see
``docs/home-assistant.md`` for setup and the entity/event contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from ..config import CONFIG_DIR
from ..report import Reporter, canonical_state, scrub

DEFAULT_ENTITY = "sensor.rgi_panel"
DEFAULT_EVENT = "rgi_status_changed"
DEFAULT_INTERVAL = 5.0
DEFAULT_TIMEOUT = 2.0
MAX_BACKOFF = 60.0
MAX_SESSIONS = 20

#: The bridge's own config file: outside the repository, never committed.
CONFIG_FILE = os.path.join(CONFIG_DIR, "home-assistant.json")

#: The panel's vocabulary, in the order the counts are published.
PANEL_STATES = ("idle", "working", "blocked", "done", "error")
ATTENTION_STATES = ("blocked", "error")
IN_FLIGHT_STATES = ("working", "stopping")

_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_EVENT_RE = re.compile(r"^[a-z0-9_]+$")

__all__ = [
    "CONFIG_FILE", "DEFAULT_ENTITY", "DEFAULT_EVENT", "HomeAssistantBridge",
    "MAX_SESSIONS", "main", "resolve_credentials", "summary",
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A bearer token is for one host only: never follow a redirect with it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# -- the bounded representation --------------------------------------------
def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry(session_id: str, row: object) -> dict | None:
    """One session, reduced to the five fields Home Assistant may see."""
    if not isinstance(row, dict):
        return None
    key = scrub(session_id, 80) or "session"
    state = canonical_state(row.get("state"))
    if state is None:
        state = "other" if _text(row.get("state")) else "idle"
    label = scrub(row.get("label") or "", 80)
    name = label or scrub(row.get("ident") or row.get("agent") or key, 80) or key
    return {"key": key, "name": name, "state": state,
            "slot": _integer(row.get("slot")), "label": label}


def _order(item: tuple[str, object]) -> tuple[int, str]:
    row = item[1]
    slot = _integer(row.get("slot")) if isinstance(row, dict) else None
    return (slot if slot is not None else 1 << 30, str(item[0]))


def summary(status: dict | None) -> dict:
    """Turn the panel's ``/status`` body into one bounded HA state payload.

    The aggregate state is ``offline`` when the panel did not answer,
    ``attention`` when any lane is blocked or failed, ``working`` while any lane
    is in flight, and ``idle`` otherwise. Counts cover every session; the list
    is truncated to :data:`MAX_SESSIONS` and sorted by lane.
    """
    counts = {state: 0 for state in PANEL_STATES}
    counts["other"] = 0
    sessions: list[dict] = []
    total = 0
    state = "offline"
    rows = status.get("sessions") if isinstance(status, dict) else None
    if isinstance(rows, dict):
        ordered = sorted(rows.items(), key=_order)
        total = len(ordered)
        for session_id, row in ordered:
            entry = _entry(str(session_id), row)
            if entry is None:
                counts["other"] += 1
                continue
            counts[entry["state"]] += 1
            if len(sessions) < MAX_SESSIONS:
                sessions.append(entry)
        words = []
        for row in rows.values():
            if isinstance(row, dict):
                words.append(canonical_state(row.get("state"))
                              or _text(row.get("state")).lower())
        if any(word in ATTENTION_STATES for word in words):
            state = "attention"
        elif any(word in IN_FLIGHT_STATES for word in words):
            state = "working"
        else:
            state = "idle"
    return {"state": state, "attributes": {
        "friendly_name": "RGI panel",
        "total": total,
        "counts": counts,
        "sessions": sessions,
    }}


def _digest(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                     default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# -- credentials ------------------------------------------------------------
def _valid_url(value: object) -> str | None:
    url = _text(value).rstrip("/")
    if not url:
        return None
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


def resolve_credentials(url: str | None = None, token: str | None = None,
                        config_path: str | None = None) -> tuple[str | None, str | None]:
    """URL and token from arguments, ``RGI_HA_URL``/``RGI_HA_TOKEN``, or a file.

    The file defaults to ``~/.config/rgi/home-assistant.json`` (``RGI_HA_CONFIG``
    overrides the path). It lives outside the repository and must never be
    committed. A malformed or missing file resolves to ``(None, None)``; the
    token is returned but never logged.
    """
    explicit_url = _text(url)
    explicit_token = _text(token)
    if explicit_url and explicit_token:
        return _valid_url(explicit_url), explicit_token
    path = (_text(config_path) or _text(os.environ.get("RGI_HA_CONFIG"))
            or CONFIG_FILE)
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        pass
    resolved_url = (explicit_url or _text(os.environ.get("RGI_HA_URL"))
                    or _text(data.get("url")))
    resolved_token = (explicit_token or _text(os.environ.get("RGI_HA_TOKEN"))
                      or _text(data.get("token")))
    return _valid_url(resolved_url), (resolved_token or None)


# -- the bridge -------------------------------------------------------------
class HomeAssistantBridge:
    """One poll loop: panel ``/status`` in, HA state and events out.

    ``poll_once()`` publishes at most one state update and one transition event
    and returns whether the panel was read and the state is current. Identical
    payloads are compared by SHA-256 and skipped, and after a failed state
    publication the next poll re-sends the same payload even if the panel did
    not change, so a Home Assistant restart heals itself.
    """

    def __init__(self, *, entity: str = DEFAULT_ENTITY, event: str = DEFAULT_EVENT,
                 url: str | None = None, token: str | None = None,
                 config_path: str | None = None,
                 interval: float = DEFAULT_INTERVAL, timeout: float = DEFAULT_TIMEOUT,
                 reporter: Reporter | None = None,
                 log: Callable[[str], None] | None = None):
        resolved_url, resolved_token = resolve_credentials(url, token, config_path)
        self.url = resolved_url or ""
        self._token = resolved_token or ""
        self.entity = _text(entity) or DEFAULT_ENTITY
        self.event = _text(event) or DEFAULT_EVENT
        self.interval = self._bounded(interval, DEFAULT_INTERVAL, 0.5, 3600.0)
        self.timeout = self._bounded(timeout, DEFAULT_TIMEOUT, 0.1, 10.0)
        self._reporter = reporter or Reporter("home-assistant", "bridge",
                                              label="Home Assistant")
        self._log = log or self._default_log
        self._quiet: set[str] = set()
        self._stop = threading.Event()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                   _NoRedirect())
        self._last_hash: str | None = None   # last state this HA accepted
        self._seen_hash: str | None = None   # last transition we published
        self._republish = False              # a state post failed; send it again
        self._online = False                 # did the last HA request work?
        self._failures = 0

    # -- configuration ------------------------------------------------------
    @staticmethod
    def _bounded(value: object, default: float, low: float, high: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return min(high, max(low, number))

    @property
    def configured(self) -> bool:
        return bool(self.url and self._token
                    and _ENTITY_RE.match(self.entity)
                    and _EVENT_RE.match(self.event))

    def _display_url(self) -> str:
        """The HA address with any credentials and path removed, safe to log."""
        try:
            parts = urllib.parse.urlsplit(self.url)
        except ValueError:
            return "Home Assistant"
        if not parts.hostname:
            return "Home Assistant"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname}{port}"

    # -- logging ------------------------------------------------------------
    @staticmethod
    def _default_log(message: str) -> None:
        # Diagnostics go to stderr; stdout belongs to whatever runs this module.
        print(f"[rgi-ha] {message}", file=sys.stderr, flush=True)

    def _clean(self, message: object) -> str:
        text = str(message)
        if self._token:
            text = text.replace(self._token, "[redacted]")
        return scrub(text, 200)

    def _say_once(self, kind: str, message: str) -> None:
        if kind in self._quiet:
            return
        self._quiet.add(kind)
        self._log(self._clean(message))

    # -- Home Assistant HTTP ------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None):
        """One HA call. Returns ``(status, body)``; body is empty on failure."""
        data = None
        headers = {"Accept": "application/json",
                   "Authorization": f"Bearer {self._token}"}
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":"),
                              default=str).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data,
                                         headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1 << 20)
                code = response.status
            self._online = True
            return code, raw
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            self._online = False
            if code == 401:
                self._say_once("ha_auth", "Home Assistant rejected the token "
                               "(401); check RGI_HA_TOKEN or the config file")
            elif code in (301, 302, 303, 307, 308):
                self._say_once("ha_redirect", f"{self._display_url()} redirected; "
                               "refusing to forward the token - use the final URL")
            else:
                self._say_once("ha_http", f"{method} {path} answered HTTP {code}")
            return code, b""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._online = False
            reason = self._clean(getattr(exc, "reason", exc))
            self._say_once("ha_unreachable",
                           f"{self._display_url()} did not answer ({reason}); retrying")
            return None, b""

    def verify(self) -> bool:
        """GET /api/ once: proves the URL is an HA API and the token is valid."""
        try:
            if not self.configured:
                self._say_once("ha_unconfigured", "Home Assistant is not configured; "
                               "set RGI_HA_URL and RGI_HA_TOKEN or write "
                               "~/.config/rgi/home-assistant.json")
                return False
            code, _ = self._request("GET", "/api/")
            if code == 200:
                self._log(self._clean(f"connected to {self._display_url()}"))
                return True
            if code is not None:
                self._say_once("ha_verify", f"GET /api/ answered {code}; "
                               "check the URL and token")
            return False
        except Exception as exc:                     # never raise outwards
            self._log(self._clean(f"verify failed: {exc}"))
            return False

    # -- publication --------------------------------------------------------
    def _publish_state(self, payload: dict) -> bool:
        code, _ = self._request("POST", f"/api/states/{self.entity}", payload)
        if code in (200, 201):
            self._last_hash = _digest(payload)
            return True
        if code is not None:
            self._say_once("ha_state", f"POST /api/states/{self.entity} "
                           f"answered {code}; will retry")
        return False

    def _publish_event(self, payload: dict) -> bool:
        code, _ = self._request("POST", f"/api/events/{self.event}", payload)
        if code == 200:
            return True
        if code is not None:
            self._say_once("ha_event", f"POST /api/events/{self.event} "
                           f"answered {code}; will retry")
        return False

    def poll_once(self) -> bool:
        """Read the panel once and publish whatever changed. Never raises."""
        try:
            if not self.configured:
                self._say_once("ha_unconfigured", "Home Assistant is not configured; "
                               "set RGI_HA_URL and RGI_HA_TOKEN or write "
                               "~/.config/rgi/home-assistant.json")
                return False
            status = self._reporter.status()
            payload = summary(status)
            digest = _digest(payload)
            # `_last_hash` only advances when Home Assistant accepted the state,
            # so a failed publication (including a restart) is sent again.
            if digest != self._last_hash or self._republish:
                if not self._publish_state(payload):
                    self._republish = True
                    return False
                self._republish = False
            if self._seen_hash is None:
                self._seen_hash = digest           # first look is not a transition
            elif digest != self._seen_hash:
                if self._publish_event(payload):   # retried until it lands
                    self._seen_hash = digest
            return True
        except Exception as exc:                   # never raise outwards
            self._log(self._clean(f"poll failed: {exc}"))
            return False

    # -- the loop -----------------------------------------------------------
    def run(self, *, iterations: int | None = None,
            sleep: Callable[[float], None] | None = None) -> int:
        """Poll forever (or ``iterations`` times), backing off on failures.

        Return the number of polls performed. A successful poll resets the
        backoff; failures grow it towards :data:`MAX_BACKOFF`. Tests inject
        ``sleep`` to stay fast and deterministic.
        """
        polls = 0
        try:
            while not self._stop.is_set():
                if iterations is not None and polls >= iterations:
                    break
                ok = self.poll_once()
                if ok:
                    if self._failures:
                        self._log(self._clean("Home Assistant is reachable again"))
                    self._failures = 0
                    self._quiet.clear()
                    delay = self.interval
                else:
                    self._failures += 1
                    delay = min(MAX_BACKOFF, self.interval
                                * (2 ** min(self._failures, 5)))
                polls += 1
                if iterations is not None and polls >= iterations:
                    break
                if sleep is not None:
                    sleep(delay)
                elif self._stop.wait(delay):
                    break
        except Exception as exc:                   # never raise outwards
            self._log(self._clean(f"bridge stopped: {exc}"))
        return polls

    def close(self) -> None:
        """Stop the loop and release the reporter (the bridge holds no lane)."""
        self._stop.set()
        try:
            self._reporter.close()
        except Exception:                          # never raise outwards
            pass


# -- command line -----------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rgi.integrations.home_assistant",
        description="Publish RGI panel state to Home Assistant's REST API.")
    parser.add_argument("--url", metavar="URL",
                        help="Home Assistant base URL; default RGI_HA_URL or "
                             "~/.config/rgi/home-assistant.json")
    parser.add_argument("--config", metavar="PATH",
                        help="credential file (default RGI_HA_CONFIG or "
                             "~/.config/rgi/home-assistant.json)")
    parser.add_argument("--entity", default=DEFAULT_ENTITY,
                        help=f"aggregate entity id (default {DEFAULT_ENTITY})")
    parser.add_argument("--event", default=DEFAULT_EVENT,
                        help=f"transition event type (default {DEFAULT_EVENT})")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"seconds between polls (default {DEFAULT_INTERVAL:g})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"seconds per request (default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--once", action="store_true",
                        help="poll once and exit; useful under a system timer")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse, verify, poll. Always returns 0 - never raises."""
    try:
        args = _parser().parse_args(argv)
        bridge = HomeAssistantBridge(entity=args.entity, event=args.event,
                                     url=args.url, config_path=args.config,
                                     interval=args.interval, timeout=args.timeout)
        if not bridge.configured:
            bridge.verify()                        # logs why leaving is fine
            return 0
        bridge.verify()
        if args.once:
            bridge.poll_once()
            return 0
        bridge._log(f"publishing {bridge.entity} every {bridge.interval:g}s")
        try:
            bridge.run()
        except KeyboardInterrupt:
            pass
        finally:
            bridge.close()
        return 0
    except SystemExit:
        raise
    except Exception as exc:                       # never raise outwards
        print(f"[rgi-ha] fatal: {scrub(exc, 200)}", file=sys.stderr)
        return 0


if __name__ == "__main__":                         # pragma: no cover
    sys.exit(main())
