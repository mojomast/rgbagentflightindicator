"""WLED backend - any WLED strip or matrix as an rgi panel.

WLED (https://kno.wled.ge) drives addressable LED strips from an ESP board and
exposes a complete HTTP JSON API. This backend reserves *one* configured segment
and turns its pixels into lamps, one lamp per pixel:

    open()   GET /json/info and /json/state, remember the reserved segment
    write()  POST /json/state {"seg":[{"id":N,"on":true,"bri":255,"i":[...]}]}
    close()  POST the segment state captured at open back to the device

``/json/info`` carries the LED count, firmware version and live flag;
``/json/state`` carries the segment geometry and state (its ``seg`` array holds
every active segment with its ``id``, so ids with gaps are matched exactly).

The ``i`` array is WLED's per-segment individual-LED control, documented at
https://kno.wled.ge/interfaces/json-api/#per-segment-individual-led-control
and implemented in wled00/json.cpp. Its LED indices are *segment-relative*:
``i[0]`` is the segment's first LED, not the strip's. Hex colour strings
("FFAA00") are used because WLED recommends them for larger sets - they are
about a third smaller than [r,g,b] arrays. The first ``i`` write freezes the
segment and clears it, and later ``i`` writes do not clear again, so every
frame must carry every owned pixel, black ones included.

Two firmware behaviours shape the code:

* WLED ignores individual colours while the strip is off or global brightness
  is 0 (the docs are explicit: turning on and setting individual LEDs in the
  same request does not work). ``open()`` therefore turns the strip on and
  gives it brightness only if needed, remembering the old values so close()
  can put them back.
* A POST's response only says the JSON parser accepted it. A controller that
  is off or busy can hold a request for the whole timeout, so ``write()`` never
  talks to the network: it hands the newest frame to a sender thread. That
  thread keeps one POST in flight, coalesces frames that arrive meanwhile,
  drops stale ones rather than queueing them, holds the documented gap between
  requests, and logs a failure once per outage while retrying in the
  background.

Configuration, resolved in this order: explicit arguments, then
``RGI_WLED_URL`` / ``RGI_WLED_SEGMENT`` / ``RGI_WLED_PIXELS``, then
``~/.config/rgi/wled.json``:

    {"url": "http://wled.local", "segment": 1, "pixels": "0-11"}

``RGI_WLED_PIXELS`` is optional; without it every pixel of the reserved
segment is a lamp. ``available()`` never touches the network - it reports
whether a URL is configured, so ``rgi daemon`` can auto-detect a configured
strip without probing every WLED-shaped address on the LAN; ``open()`` does
the discovery and raises BackendUnavailable when the box is not what it says.

Verified against the JSON API documentation (kno.wled.ge, fetched 2026-10-01)
and wled00/json.cpp of WLED 16.0.1
(https://github.com/wled/WLED/blob/v16.0.1/wled00/json.cpp); the fields used
here are unchanged since 0.13. Simulated against a mock server, not tested on
hardware. See docs/wled.md.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

#: a WLED box on a LAN answers in milliseconds; a missing one must not stall us
DEFAULT_TIMEOUT = 1.0
#: when the controller is away, retry the newest frame this often
DEFAULT_RETRY_INTERVAL = 2.0
#: config file under ~/.config/rgi (overridable with RGI_WLED_CONFIG)
DEFAULT_CONFIG_NAME = "wled.json"
#: refuse absurd responses from something that is not a WLED box
MAX_RESPONSE_BYTES = 512 * 1024
#: state fields that are UI / read-only and must never be written back
RESTORE_KEEP_OUT = frozenset({"sel", "len", "lc"})

DEFAULT_SEGMENT = 0


def _first(*values):
    """First value that is neither None nor an empty/whitespace string."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        return value
    return None


def _env(name: str):
    return _first(os.environ.get(name))


def config_path() -> str:
    return _env("RGI_WLED_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".config", "rgi", DEFAULT_CONFIG_NAME
    )


def load_config_file(path: str | None = None) -> dict:
    """The JSON config file, or {}; never raises - a bad file is just absent."""
    try:
        with open(path or config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_config(url=None, segment=None, pixels=None, config_file=None) -> dict:
    """Explicit argument, environment, then ~/.config/rgi/wled.json."""
    cfg = load_config_file(config_file)
    return {
        "url": _first(url, _env("RGI_WLED_URL"), cfg.get("url")),
        "segment": _first(segment, _env("RGI_WLED_SEGMENT"), cfg.get("segment")),
        "pixels": _first(pixels, _env("RGI_WLED_PIXELS"), cfg.get("pixels")),
    }


def normalise_url(raw) -> str:
    """A usable base URL, or "" if ``raw`` cannot be one. Adds http:// if bare."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    raw = raw.strip()
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return raw.rstrip("/")


def parse_pixels(spec) -> list[int]:
    """'0-11' / '2,4-5' / [0, 1] -> sorted unique indices; [] means all.

    Raises ValueError for anything that is not index-list syntax, so open()
    can report a misconfiguration instead of silently painting odd pixels.
    """
    if spec is None:
        return []
    if isinstance(spec, (list, tuple)):
        out: set[int] = set()
        for item in spec:
            out.update(parse_pixels(str(item)))
        return sorted(out)
    text = str(spec).strip()
    if not text:
        return []
    out = set()
    for chunk in text.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, _, high = chunk.partition("-")
            low, high = int(low), int(high)
            if low > high:
                low, high = high, low
            out.update(range(low, high + 1))
        else:
            out.add(int(chunk))
    return sorted(out)


def _hex_colour(rgb: RGB) -> str:
    return "%02X%02X%02X" % (rgb[0] & 0xFF, rgb[1] & 0xFF, rgb[2] & 0xFF)


class WledBackend(Backend):
    """One WLED segment as a panel.

    The constructor never talks to the network and never raises: it only
    resolves configuration, so ``available()``/registry walks stay cheap.
    """

    name = "wled"
    #: one small JSON POST at a time; WLED explicitly asks callers to serialise
    min_interval = 0.04
    per_lamp = True

    def __init__(self, url=None, segment=None, pixels=None,
                 timeout: float = DEFAULT_TIMEOUT, retry_interval: float = DEFAULT_RETRY_INTERVAL,
                 debug: bool = False, config_file: str | None = None):
        cfg = resolve_config(url, segment, pixels, config_file)
        self.url = normalise_url(cfg["url"])
        self._configured = bool(cfg["url"])
        self._config_error: str | None = None
        if self._configured and not self.url:
            self._config_error = f"WLED URL {cfg['url']!r} is not a valid http(s) address"

        raw_segment = cfg["segment"]
        self.segment_id = DEFAULT_SEGMENT
        if raw_segment is not None:
            try:
                self.segment_id = int(raw_segment)
                if self.segment_id < 0:
                    raise ValueError("negative")
            except (TypeError, ValueError):
                self._config_error = (f"WLED segment {raw_segment!r} is not a "
                                      "non-negative integer")

        raw_pixels = cfg["pixels"]
        self.pixels: list[int] = []
        if raw_pixels is not None:
            try:
                self.pixels = parse_pixels(raw_pixels)
            except (TypeError, ValueError):
                self._config_error = (f"WLED pixels {raw_pixels!r} is not a list "
                                      "like '0-11' or '0,2,4'")

        self.timeout = self._positive(timeout, DEFAULT_TIMEOUT)
        self.retry_interval = self._positive(retry_interval, DEFAULT_RETRY_INTERVAL)
        self.debug = debug

        self.version = "unknown"
        self.strip_leds = 0
        self._lamps: list[Lamp] = []
        self._captured: dict | None = None
        self._power_patch: dict | None = None
        self._open = False

        self._pending: list[RGB] | None = None
        self._pending_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_frame: list[RGB] | None = None
        self._next_attempt = 0.0
        self._failed = False
        self._failed_logged = False
        self._log_lock = threading.Lock()

        # a LAN device is talked to directly; environment HTTP proxies would
        # only get in the way (and leak the address to them)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _positive(value, fallback: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    # -- detection ---------------------------------------------------------
    @classmethod
    def available(cls) -> bool:
        """Configured or not. Deliberately no network: the daemon walks this."""
        return bool(resolve_config()["url"])

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self.close()                                   # safe on a fresh instance
        if not self._configured:
            raise BackendUnavailable(
                "WLED is not configured: set RGI_WLED_URL, or write "
                "~/.config/rgi/wled.json (see docs/wled.md)"
            )
        if self._config_error:
            raise BackendUnavailable(self._config_error)

        info = self._get_json("/json/info")
        leds = info.get("leds")
        if not isinstance(leds, dict) or not isinstance(leds.get("count"), int) \
                or leds["count"] <= 0:
            raise BackendUnavailable(f"{self.url} does not answer like a WLED /json/info")
        if info.get("live"):
            raise BackendUnavailable(
                f"{self.url} is in realtime mode (UDP/E1.31/DDP) and paints every "
                "LED itself. Stop the live source, then start rgi."
            )
        self.version = str(info.get("ver") or "unknown")
        self.strip_leds = leds["count"]

        state = self._get_json("/json/state")
        if not isinstance(state, dict):
            raise BackendUnavailable(f"{self.url} does not answer like a WLED /json/state")
        segment = self._find_segment(state.get("seg"))
        if segment is None:
            active = [s.get("id") for s in state.get("seg") or []
                      if isinstance(s, dict)]
            raise BackendUnavailable(
                f"WLED segment {self.segment_id} does not exist (active: {active}); "
                "create or select the segment rgi may use (docs/wled.md)"
            )
        start = int(segment.get("start") or 0)
        stop = int(segment.get("stop") or 0)
        length = int(segment.get("len") or (stop - start) or 0)
        if length <= 0:
            raise BackendUnavailable(
                f"WLED segment {self.segment_id} is empty (start={start}, stop={stop})"
            )
        self.pixels = self._choose_pixels(length, start, stop)

        name = str(segment.get("n") or "").strip()
        self._lamps = [
            Lamp(index=i, label=f"px{pixel}",
                 group=f"segment{self.segment_id}" if name else "wled")
            for i, pixel in enumerate(self.pixels)
        ]

        # WLED will not show individual pixels on an off strip (or at 0
        # brightness); turn it on for the duration, remember what it was.
        on = bool(state.get("on"))
        bri = state.get("bri") if isinstance(state.get("bri"), (int, float)) else 0
        if not on or bri <= 0:
            payload: dict = {"on": True}
            if bri <= 0:
                payload["bri"] = 255
            try:
                self._post(payload)
            except Exception as exc:
                raise BackendUnavailable(
                    f"{self.url}: could not turn the strip on ({exc})"
                ) from exc
            self._power_patch = {"on": state.get("on", False), "bri": state.get("bri", 0)}

        self._captured = segment
        self._pending = None
        self._last_frame = None
        self._failed = False
        self._failed_logged = False
        self._next_attempt = 0.0
        self._stop.clear()
        self._open = True
        self._worker = threading.Thread(target=self._loop, name="wled-send", daemon=True)
        self._worker.start()

        if self.debug:
            print(f"[wled] {self.url}: WLED {self.version}, {self.strip_leds} LEDs, "
                  f"segment {self.segment_id} pixels {start}..{stop}, "
                  f"{len(self._lamps)} lamps", file=sys.stderr)

    def _find_segment(self, segments) -> dict | None:
        """State reports every *active* segment, with gaps in the ids."""
        if not isinstance(segments, list):
            return None
        for index, seg in enumerate(segments):
            if not isinstance(seg, dict):
                continue
            try:
                seg_id = int(seg.get("id", index))
            except (TypeError, ValueError):
                continue
            if seg_id == self.segment_id:
                return dict(seg)
        return None

    def _choose_pixels(self, length: int, start: int, stop: int) -> list[int]:
        if not self.pixels:                            # unset: the whole segment
            return list(range(length))
        chosen = [p for p in self.pixels if 0 <= p < length]
        if len(chosen) != len(self.pixels):
            raise BackendUnavailable(
                f"WLED segment {self.segment_id} has {length} pixels (strip "
                f"{start}..{stop}); RGI_WLED_PIXELS {self.pixels} points outside it"
            )
        return chosen

    def close(self) -> None:
        """Stop sending, then put the reserved segment back. Safe twice."""
        self._open = False
        self._stop.set()
        self._wake.set()
        thread, self._worker = self._worker, None
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(timeout=self.timeout + 0.5)
        with self._pending_lock:
            self._pending = None
        self._last_frame = None

        captured, self._captured = self._captured, None
        patch, self._power_patch = self._power_patch, None
        if captured is None and patch is None:
            return
        payload: dict = {}
        if patch is not None:
            payload["on"] = bool(patch.get("on"))
            try:
                payload["bri"] = int(patch.get("bri") or 0)
            except (TypeError, ValueError):
                payload["bri"] = 0
        if captured is not None:
            payload["seg"] = [self._restore_segment(captured)]
        try:
            self._post(payload)
        except Exception as exc:
            self._note_failure(exc)                     # once per outage

    def _restore_segment(self, captured: dict) -> dict:
        """The captured segment, minus fields we never changed or cannot write."""
        out = {key: value for key, value in captured.items()
               if key not in RESTORE_KEEP_OUT}
        out["id"] = self.segment_id
        return out

    def lamps(self) -> list[Lamp]:
        return list(self._lamps)

    # -- painting ----------------------------------------------------------
    def write(self, colours: Sequence[RGB]) -> None:
        """Hand the newest frame to the sender thread; never blocks, never raises."""
        try:
            if not self._open or self._stop.is_set():
                return
            frame: list[RGB] = []
            for rgb in colours:
                frame.append((int(rgb[0]) & 0xFF, int(rgb[1]) & 0xFF, int(rgb[2]) & 0xFF))
            if len(frame) != len(self._lamps):
                return                                 # wrong shape: drop, do not paint
            with self._pending_lock:
                self._pending = frame                  # replaces any older frame
            self._wake.set()
        except Exception:
            return

    def frame_body(self, colours: Sequence[RGB]) -> dict:
        """The ``seg`` object one frame becomes (exposed for tests/docs)."""
        if self.pixels == list(range(len(self.pixels))):
            pixels = [_hex_colour(rgb) for rgb in colours]
        else:                                          # explicit segment-relative indices
            pixels = []
            for index, rgb in zip(self.pixels, colours):
                pixels.append(index)
                pixels.append(_hex_colour(rgb))
        return {"id": self.segment_id, "on": True, "bri": 255, "i": pixels}

    # -- the sender thread -------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            frame = None
            while not self._stop.is_set():             # wait for work
                frame = self._take_pending()
                if frame is not None:
                    break
                timeout = 0.25
                if self._failed and self._last_frame is not None:
                    remaining = self._next_attempt - time.monotonic()
                    if remaining <= 0:
                        frame = self._last_frame        # retry the newest frame
                        break
                    timeout = min(timeout, remaining)
                self._wake.wait(timeout)
                self._wake.clear()
            if self._stop.is_set() or frame is None:
                continue

            delay = self._next_attempt - time.monotonic()
            if delay > 0 and self._stop.wait(delay):
                break
            latest = self._take_pending()              # frames that arrived meanwhile
            if latest is not None:
                frame = latest

            ok = self._post_frame(frame)
            now = time.monotonic()
            if ok:
                self._last_frame = None
                self._failed = False
                self._next_attempt = now + self.min_interval
            else:
                self._last_frame = frame
                self._failed = True
                self._next_attempt = now + max(self.retry_interval, self.min_interval)

    def _take_pending(self) -> list[RGB] | None:
        with self._pending_lock:
            frame, self._pending = self._pending, None
        return frame

    def _post_frame(self, frame: list[RGB]) -> bool:
        try:
            self._post({"seg": [self.frame_body(frame)]})
        except Exception as exc:
            self._note_failure(exc)
            return False
        self._note_success()
        return True

    def _note_failure(self, exc: Exception) -> None:
        with self._log_lock:
            if self._failed_logged:
                return
            self._failed_logged = True
        print(f"[rgi] wled: {self.url}: {exc}", file=sys.stderr)

    def _note_success(self) -> None:
        with self._log_lock:
            recovered = self._failed_logged
            self._failed_logged = False
        if recovered and self.debug:
            print(f"[rgi] wled: {self.url}: back online", file=sys.stderr)

    # -- HTTP --------------------------------------------------------------
    def _get_json(self, path: str) -> dict:
        request = urllib.request.Request(self.url + path,
                                         headers={"Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = self._status(response)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except Exception as exc:                       # urlopen raises many shapes
            raise BackendUnavailable(f"WLED at {self.url}{path}: {exc}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BackendUnavailable(f"WLED at {self.url}{path}: response too large")
        if not 200 <= status < 300:
            raise BackendUnavailable(f"WLED at {self.url}{path}: HTTP {status}")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendUnavailable(
                f"WLED at {self.url}{path}: not JSON ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise BackendUnavailable(f"WLED at {self.url}{path}: unexpected JSON shape")
        return data

    def _post(self, payload: dict) -> None:
        """One state POST. Raises; the worker turns that into a logged failure."""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.url + "/json/state", data=body, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with self._opener.open(request, timeout=self.timeout) as response:
            status = self._status(response)
            response.read(1024)                        # let the socket settle
        if not 200 <= status < 300:
            raise OSError(f"HTTP {status}")

    @staticmethod
    def _status(response) -> int:
        status = getattr(response, "status", None) or response.getcode()
        return int(status)
