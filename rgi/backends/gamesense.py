"""SteelSeries GameSense backend - Engine does the device work over HTTP.

SteelSeries Engine exposes a documented JSON API on loopback: the running
Engine writes ``coreProps.json`` with its address, and any program can POST
events to ``{address}/game_event``. A ``rgb-per-key-zones`` handler with
``mode: bitmap`` takes 132 RGB colours interpreted as a 22x6 grid and maps
them to the nearest keys of the user's keyboard - per-key lighting with no SDK
binary to ship and no HID handle for rgi to own.

    open()   POST /bind_game_event, bind the LANES event to a bitmap handler
    write()  POST /game_event, {"data": {"frame": {"bitmap": [132 colours]}}}
    close()  POST /remove_game_event, so Engine stops lighting rgi's keys

Engine deactivates a game after ~15 s without an event, so a small keepalive
thread posts ``/game_heartbeat`` while rgi is open; without it a steady frame
would go dark after a quarter of a minute and the daemon has no reason to
write again.

``LANE_TO_BITMAP`` below is **rgi's own** 22x6 layout for a standard full-size
ANSI keyboard - it was not copied from any vendor table. Engine maps each grid
cell to the nearest key, so it is approximate by design; only cells rgi owns
ever carry a colour, and every other cell is black. The number row is the lane
pool (group ``number-row``), matching rgi's other keyboard backends.

Protocol from the public GameSense SDK documentation
(``sending-game-events.md``, ``writing-handlers-in-json.md``,
``json-handlers-full-keyboard-lighting.md``). Docs-only, mock-tested: no
SteelSeries hardware or Engine was available here. See docs/gamesense.md.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

GAME = "RGI"
EVENT = "LANES"
DEVICE_TYPE = "rgb-per-key-zones"
MODE = "bitmap"

GRID_COLUMNS = 22
GRID_ROWS = 6
GRID_CELLS = GRID_COLUMNS * GRID_ROWS                     # 132, per the SDK

BIND_PATH = "/bind_game_event"
EVENT_PATH = "/game_event"
HEARTBEAT_PATH = "/game_heartbeat"
REMOVE_PATH = "/remove_game_event"

#: loopback HTTP: a missing Engine must not stall the daemon for long
DEFAULT_TIMEOUT = 1.0
#: Engine deactivates a game after ~15 s without events; stay well inside it
DEFAULT_HEARTBEAT = 10.0
#: available() only proves something is listening, so keep it very short
AVAILABILITY_TIMEOUT = 0.4

#: Environment override for the Engine discovery file, useful where Engine is
#: installed somewhere unusual (and for tests).
CORE_PROPS_ENV = "RGI_GAMESENSE_CORE_PROPS"


def default_core_props_paths() -> list[str]:
    """The documented install locations, most likely first for this OS."""
    program_data = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    windows = os.path.join(program_data, "SteelSeries", "SteelSeries Engine 3",
                           "coreProps.json")
    macos = "/Library/Application Support/SteelSeries Engine 3/coreProps.json"
    linux = os.path.join(os.path.expanduser("~"), ".config",
                         "SteelSeries Engine 3", "coreProps.json")
    if sys.platform == "darwin":
        return [macos, windows, linux]
    if os.name == "nt":
        return [windows, macos, linux]
    return [linux, macos, windows]


def find_core_props(path: str | None = None) -> str | None:
    """The first existing coreProps.json, or None. Filesystem only."""
    env = os.environ.get(CORE_PROPS_ENV)
    if path:
        candidates = [path]
    elif env:
        candidates = [env]
    else:
        candidates = default_core_props_paths()
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def load_address(path: str) -> str:
    """Read ``address`` out of a coreProps.json. Raises ValueError/OSError."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("coreProps.json is not a JSON object")
    address = data.get("address")
    if not isinstance(address, str) or not address.strip():
        raise ValueError("coreProps.json has no usable \"address\"")
    return address.strip()


def engine_url(address: str) -> str:
    """``host:port`` (or a full URL) -> base URL; "" if unusable."""
    if not isinstance(address, str) or not address.strip():
        return ""
    address = address.strip()
    if "://" not in address:
        address = "http://" + address
    try:
        parts = urllib.parse.urlsplit(address)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host or any(
            char.isspace() for char in parts.netloc):
        return ""
    if port is not None and not 1 <= port <= 65535:
        return ""
    return address.rstrip("/")


def _reachable(url: str, timeout: float = AVAILABILITY_TIMEOUT) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((parts.hostname, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _clean_rgb(colour) -> tuple[int, int, int]:
    out = []
    for channel in tuple(colour)[:3]:
        try:
            value = int(channel)
        except (TypeError, ValueError):
            value = 0
        out.append(max(0, min(255, value)))
    while len(out) < 3:
        out.append(0)
    return out[0], out[1], out[2]


def _parse_layout(rows) -> tuple:
    """22 cells per row, split on whitespace; "." is an unmapped cell."""
    layout: list[str | None] = []
    for row in rows:
        cells = row.split()
        if len(cells) != GRID_COLUMNS:
            raise ValueError(
                f"GameSense layout row has {len(cells)} cells, "
                f"expected {GRID_COLUMNS}: {row!r}"
            )
        layout.extend(None if cell == "." else cell for cell in cells)
    if len(layout) != GRID_CELLS:
        raise ValueError(
            f"GameSense layout has {len(layout)} cells, expected {GRID_CELLS}"
        )
    return tuple(layout)


#: rgi's own approximate 22x6 map of a standard full-size ANSI keyboard.
#: Row 0 is the function row, row 1 the number row, ... ; "." is unmapped and
#: stays black. Engine maps each cell to the nearest key itself.
GAMESENSE_LAYOUT = _parse_layout((
    "esc F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 F11 F12 prtsc sclk pause . . . . . .",
    "` 1 2 3 4 5 6 7 8 9 0 - = bksp ins home pgup numlock num/ num* num- .",
    "tab q w e r t y u i o p [ ] \\ del end pgdn num7 num8 num9 num+ .",
    "caps a s d f g h j k l ; ' enter . . . num4 num5 num6 . . .",
    "lshift z x c v b n m , . / rshift up . num1 num2 num3 kpenter . . . .",
    "lctrl lalt space space space space space ralt menu rctrl left down right . num0 num. . . . . . .",
))

#: The number row in lane order - the same `` ` 1 ... 0 - = `` rgi uses
#: everywhere. Lane i lights this bitmap cell.
LANE_LABELS = ("`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "=")
LANE_TO_BITMAP = tuple(GAMESENSE_LAYOUT.index(label) for label in LANE_LABELS)
NUMBER_ROW_CELLS = frozenset(LANE_TO_BITMAP)


def _layout_cells() -> list[tuple[int, str]]:
    """(bitmap cell, label) for every mapped cell, in cell order."""
    return [(cell, label) for cell, label in enumerate(GAMESENSE_LAYOUT)
            if label is not None]


def _default_lamps() -> list[Lamp]:
    # Lamp.index is the position in lamps(), as the daemon's renderer assumes;
    # the bitmap cell is carried separately by GamesenseBackend._cells.
    return [
        Lamp(index=position, label=label,
             group="number-row" if cell in NUMBER_ROW_CELLS else "key")
        for position, (cell, label) in enumerate(_layout_cells())
    ]


class GamesenseBackend(Backend):
    name = "gamesense"
    min_interval = 0.1
    per_lamp = True

    def __init__(self, core_props: str | None = None,
                 heartbeat_interval: float = DEFAULT_HEARTBEAT,
                 timeout: float = DEFAULT_TIMEOUT,
                 verbose: bool = False):
        self.core_props = core_props
        self.heartbeat_interval = self._positive(heartbeat_interval, DEFAULT_HEARTBEAT)
        self.timeout = self._positive(timeout, DEFAULT_TIMEOUT)
        self.verbose = verbose
        self.address = ""
        self.http_url = ""
        self._cells = [cell for cell, _ in _layout_cells()]
        self._lamps = _default_lamps()
        self._open = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._failed_logged = False
        self._log_lock = threading.Lock()
        # loopback HTTP: environment proxies must not see the Engine address
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _positive(value, fallback: float) -> float:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    # -- discovery ---------------------------------------------------------
    @classmethod
    def available(cls) -> bool:
        """coreProps.json exists *and* something answers at its address."""
        try:
            path = find_core_props()
            if not path:
                return False
            url = engine_url(load_address(path))
            if not url:
                return False
            return _reachable(url)
        except Exception:
            return False

    # -- HTTP --------------------------------------------------------------
    def _post(self, path: str, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.http_url + path, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                response.read(1024)
        except urllib.error.HTTPError as exc:
            raise OSError(f"HTTP {exc.code} for {path}") from exc
        if not 200 <= int(status) < 300:
            raise OSError(f"HTTP {status} for {path}")

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self.close()
        path = find_core_props(self.core_props)
        if not path:
            raise BackendUnavailable(
                "SteelSeries Engine 3 is not running: coreProps.json was not "
                "found in " + "; ".join(default_core_props_paths())
                + " (install and start Engine, or pass core_props=...)"
            )
        try:
            address = load_address(path)
        except (OSError, ValueError) as exc:
            raise BackendUnavailable(f"{path}: {exc}") from exc
        url = engine_url(address)
        if not url:
            raise BackendUnavailable(f"{path}: address {address!r} is not usable")

        self.core_props = path
        self.address = address
        self.http_url = url

        try:
            self._post(BIND_PATH, {
                "game": GAME,
                "event": EVENT,
                "min_value": 0,
                "max_value": 0,
                "value_optional": True,
                "handlers": [{"device-type": DEVICE_TYPE, "mode": MODE}],
            })
        except Exception as exc:
            self.http_url = ""
            raise BackendUnavailable(
                f"SteelSeries Engine at {url} did not accept the {EVENT} "
                f"bitmap binding: {exc} (is Engine running, and is coreProps "
                "current? Engine rewrites the file and its port on restart)"
            ) from exc

        self._failed_logged = False
        self._stop.clear()
        self._open = True
        self._thread = threading.Thread(target=self._heartbeat_loop,
                                        name="gamesense-heartbeat", daemon=True)
        self._thread.start()
        if self.verbose:
            print(f"[gamesense] {url}: bound {GAME}/{EVENT} to "
                  f"{DEVICE_TYPE} {MODE}, {len(self._lamps)} lamps")

    def close(self) -> None:
        was_open = self._open
        self._open = False
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            thread.join(timeout=self.timeout + 0.5)
        if was_open and self.http_url:
            try:
                self._post(REMOVE_PATH, {"game": GAME, "event": EVENT})
            except Exception:
                pass                                   # best effort
        self.http_url = ""
        self.address = ""

    def lamps(self) -> list[Lamp]:
        return list(self._lamps)

    # -- painting ----------------------------------------------------------
    def bitmap_frame(self, colours: Sequence[RGB]) -> list[list[int]]:
        """The 132-cell frame write() would send; exposed for tests/docs."""
        bitmap = [[0, 0, 0] for _ in range(GRID_CELLS)]
        for cell, colour in zip(self._cells, list(colours)):
            bitmap[cell] = list(_clean_rgb(colour))
        return bitmap

    def write(self, colours: Sequence[RGB]) -> None:
        if not self._open or not self.http_url:
            raise BackendUnavailable("gamesense backend is not open")
        payload = {
            "game": GAME,
            "event": EVENT,
            "data": {"frame": {"bitmap": self.bitmap_frame(colours)}},
        }
        try:
            self._post(EVENT_PATH, payload)
        except Exception as exc:
            self._note_failure(exc)
            raise BackendUnavailable(
                f"SteelSeries Engine at {self.http_url}: {exc}"
            ) from exc
        self._note_success()

    # -- keepalive ---------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval):
            if not self._open or not self.http_url:
                return
            try:
                self._post(HEARTBEAT_PATH, {"game": GAME})
            except Exception as exc:
                self._note_failure(exc)
            else:
                self._note_success()

    def _note_failure(self, exc: Exception) -> None:
        with self._log_lock:
            if self._failed_logged or not self.http_url:
                return
            self._failed_logged = True
        print(f"[rgi] gamesense: {self.http_url}: {exc}", file=sys.stderr)

    def _note_success(self) -> None:
        with self._log_lock:
            recovered = self._failed_logged
            self._failed_logged = False
        if recovered and self.verbose:
            print(f"[rgi] gamesense: {self.http_url}: back online", file=sys.stderr)
