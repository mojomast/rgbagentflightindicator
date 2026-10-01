"""EVision backend - Magic Refiner, Redragon and many other SONiX-based boards.

Recovered from OpenRGB's EVisionKeyboardController and confirmed on hardware: a
Magic Refiner MK 17 (USB 320F:501D) answers the v2 capability query with
``aa 55`` and reports a 126-lamp map, and accepts v2 direct-colour packets.

The device speaks a *command* protocol, not a framebuffer. Every message is a
64-byte output report:

    [0]     report id, always 0x04
    [1..2]  checksum, little endian = sum(bytes[3..63])
    [3]     command
    [4]     payload size
    [5..6]  payload offset, little endian
    [7]     unused
    [8..63] payload

Direct colour is command 0x12: RGB triples at absolute byte offsets, chunked into
56-byte packets. The firmware expects to be refreshed roughly every 200 ms while
it is being driven, so the backend keeps a small thread alive that sends the
one-byte refresh (command 0x12, offset 18) - without it the board drifts back to
its own effect.

Lamp *identity* is unknown: the protocol addresses LEDs by index, and the vendor
key map is not published for this board. Lamps are therefore labelled led0..ledN
and `rgi map` is the way to find which index sits under which key.
"""

from __future__ import annotations

import threading
import time
from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

USAGE_PAGE = 0xFF1C          # the EVision vendor page; both OpenRGB detectors key on it
INTERFACE_HINT = 1           # 0xFF1C enumerates as interface 1 on every known board
REPORT_ID = 0x04
REPORT_LEN = 64
MAX_CHUNK = 56               # 64 minus the 8-byte header

CMD_READ_CAPABILITIES = 0x03
CMD_DYNAMIC_COLORS = 0x12
CMD_END_DYNAMIC = 0x13
REFRESH_OFFSET = 18
REFRESH_MS = 200

CAPABILITY_MAGIC = (0xAA, 0x55)


def build(command: int, payload: bytes = b"", offset: int = 0) -> bytes:
    """One 64-byte request, checksum included."""
    if len(payload) > MAX_CHUNK:
        raise ValueError("payload over 56 bytes; chunk it")
    buf = bytearray(REPORT_LEN)
    buf[0] = REPORT_ID
    buf[3] = command
    buf[4] = len(payload)
    buf[5] = offset & 0xFF
    buf[6] = (offset >> 8) & 0xFF
    buf[8:8 + len(payload)] = payload
    checksum = sum(buf[3:REPORT_LEN]) & 0xFFFF
    buf[1] = checksum & 0xFF
    buf[2] = checksum >> 8
    return bytes(buf)


class EvisionBackend(Backend):
    name = "evision"
    min_interval = 0.008

    def __init__(self, leds: int | None = None, refresh_ms: int = REFRESH_MS,
                 verbose: bool = False):
        self.forced_leds = leds
        self.refresh_ms = refresh_ms
        self.verbose = verbose
        self.device = None
        self.count = 0
        self.capabilities: bytes | None = None
        self._lamps: list[Lamp] = []
        self._keepalive: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # -- discovery --------------------------------------------------------
    @staticmethod
    def _hid():
        try:
            import hid
        except ImportError as exc:                            # pragma: no cover
            raise BackendUnavailable(
                "hidapi is not installed (pip install hidapi)"
            ) from exc
        return hid

    @classmethod
    def candidates(cls) -> list[dict]:
        hid = cls._hid()
        found = [d for d in hid.enumerate() if (d.get("usage_page") or 0) == USAGE_PAGE]
        # interface 1 is what the protocol documents; prefer it, keep the rest
        return sorted(found, key=lambda d: (d.get("interface_number") != INTERFACE_HINT,
                                            d.get("interface_number") or 0))

    @classmethod
    def available(cls) -> bool:
        try:
            return bool(cls.candidates())
        except BackendUnavailable:
            return False

    # -- lifecycle --------------------------------------------------------
    def open(self) -> None:
        hid = self._hid()
        found = self.candidates()
        if not found:
            raise BackendUnavailable(
                f"no HID interface on usage page 0x{USAGE_PAGE:04X} - "
                "is the keyboard on wired USB? (Bluetooth takes it off the bus)"
            )
        chosen = found[0]
        path = chosen["path"]
        if isinstance(path, str):
            path = path.encode()

        self.device = hid.device()
        self.device.open_path(path)
        self.device.set_nonblocking(1)

        self.capabilities = self._read_capabilities()
        if self.capabilities:
            magic = tuple(self.capabilities[:2])
            if magic != CAPABILITY_MAGIC and self.verbose:
                print(f"[evision] unexpected capability signature {magic}")
            self.count = self.forced_leds or self.capabilities[5] or 0
        if not self.count:
            self.count = self.forced_leds or 126          # the common EVision count
        if self.verbose:
            shown = self.capabilities.hex(" ") if self.capabilities else "(no reply)"
            print(f"[evision] {chosen.get('vendor_id'):04X}:{chosen.get('product_id'):04X}"
                  f" interface {chosen.get('interface_number')} capabilities: {shown}")
            print(f"[evision] {self.count} lamps")

        self._lamps = [Lamp(index=i, label=f"led{i}", group="unmapped")
                       for i in range(self.count)]
        self._start_keepalive()

    def close(self) -> None:
        self._stop.set()
        thread = self._keepalive
        if thread is not None:
            thread.join(timeout=1.0)
            self._keepalive = None
        if self.device is not None:
            try:
                self.device.write(build(CMD_END_DYNAMIC))     # hand control back
            except Exception:
                pass
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None

    def lamps(self) -> list[Lamp]:
        return list(self._lamps)

    # -- protocol ---------------------------------------------------------
    def _exchange(self, request: bytes, timeout: float = 0.3) -> bytes | None:
        with self._lock:
            if self.device is None:
                raise BackendUnavailable("backend is not open")
            self.device.write(request)
            deadline = time.time() + timeout
            while time.time() < deadline:
                reply = self.device.read(REPORT_LEN)
                if reply:
                    return bytes(reply)
                time.sleep(0.005)
        return None

    def _read_capabilities(self) -> bytes | None:
        reply = self._exchange(build(CMD_READ_CAPABILITIES, b"\0" * 7, 0), timeout=0.6)
        if not reply or reply[3] != CMD_READ_CAPABILITIES:
            return None
        size = reply[4]
        return reply[8:8 + size]

    def write(self, colours: Sequence[RGB]) -> None:
        if self.device is None:
            raise BackendUnavailable("backend is not open")
        payload = bytearray()
        for r, g, b in colours[:self.count]:
            payload += bytes((r & 0xFF, g & 0xFF, b & 0xFF))

        for offset in range(0, len(payload), MAX_CHUNK):
            self._exchange(build(CMD_DYNAMIC_COLORS, bytes(payload[offset:offset + MAX_CHUNK]),
                                 offset))

    # -- keepalive --------------------------------------------------------
    def _start_keepalive(self) -> None:
        """The firmware drops back to its own effect without a periodic refresh."""
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(self.refresh_ms / 1000.0):
                try:
                    self._exchange(build(CMD_DYNAMIC_COLORS, b"\0", REFRESH_OFFSET),
                                   timeout=0.05)
                except Exception:
                    return

        self._keepalive = threading.Thread(target=loop, name="evision-refresh", daemon=True)
        self._keepalive.start()
