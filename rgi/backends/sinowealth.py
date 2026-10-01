"""Sinowealth 258A:0049 (white-label READSON) - the board this project started on.

No vendor software exists for it. The protocol below was recovered by probing;
see docs/protocols.md for how, and what is still unknown.

Two vendor HID interfaces matter, both usage page 0xFF00 / usage 1:

    Col05   6-byte command reports   - carries the mode unlock
    Col06   1032-byte feature reports - carries LED data

The obvious-looking Col03 interface accepts writes and silently ignores them.
That single detail is why several existing tools fail on this board.

LED data is stored planar, not interleaved: one 126-byte block each for B, G and
R, at fixed offsets inside a 1032-byte report. Writing a colour to block offset
*i* colours a key; the key's identity is fixed by the firmware. Only the number
row's positions have been confirmed (slot 0 is the backtick key, then 1-9, 0,
minus, equals), which is exactly what an indicator panel needs.

Two banks exist - 06 09 BC and 06 09 C0 - and both must be written or keys in
the second bank keep their old colour.
"""

from __future__ import annotations

from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

VENDOR_ID = 0x258A
PRODUCT_ID = 0x0049

FRAME_LEN = 1032
BLOCK = 126

# Offsets of the three colour planes inside a 1032-byte report. The vendor
# re-applies them after the mode commit, and every non-LED byte stays zero.
B_START = 29
G_START = B_START + BLOCK
R_START = G_START + BLOCK

# Report headers, first bytes of gk_frames.json from the vendor protocol.
HEADER_PERKEY_1 = bytes([0x06, 0x09, 0xBC, 0x00, 0x40, 0x00, 0x00, 0x00])
HEADER_PERKEY_2 = bytes([0x06, 0x09, 0xC0, 0x00, 0x40, 0x00, 0x00, 0x00])
MODE_COMMIT = bytes([0x06, 0x03, 0xB6, 0x00, 0x00, 0x00, 0x00, 0x00])
UNLOCK = bytes([0x05, 0x83, 0xB6, 0x00, 0x00, 0x00])

MIN_WRITE_INTERVAL = 0.013

# Only these slot positions are confirmed on hardware. Everything else in the
# 126-slot bank is a real key, but which one is unverified.
NUMBER_ROW = {
    0: "`", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
    7: "7", 8: "8", 9: "9", 10: "0", 11: "-", 12: "=",
}


def new_frame(header: bytes) -> bytearray:
    frame = bytearray(FRAME_LEN)
    frame[0:len(header)] = header
    return frame


def _hid():
    try:
        import hid  # hidapi
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise BackendUnavailable(
            "hidapi is not installed (pip install hidapi, or hidapi-libusb on Linux)"
        ) from exc
    return hid


class SinowealthBackend(Backend):
    name = "sinowealth"
    min_interval = MIN_WRITE_INTERVAL

    def __init__(self, vendor_id: int = VENDOR_ID, product_id: int = PRODUCT_ID):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.cmd = None
        self.data = None
        self._last_write = 0.0
        # Built here, not in open(), on purpose: `rgi detect` must be able to
        # describe the board without claiming it. Opening sends the unlock and
        # the mode commit, which takes the device away from a daemon that is
        # already driving it - and the daemon is left holding a handle the
        # firmware no longer honours, so the panel silently stops updating.
        self._lamps: list[Lamp] = [
            Lamp(index=i, label=NUMBER_ROW.get(i, f"slot{i}"),
                 group="number-row" if i in NUMBER_ROW else "unmapped")
            for i in range(BLOCK)
        ]

    # -- discovery --------------------------------------------------------
    @classmethod
    def available(cls) -> bool:
        try:
            hid = _hid()
        except BackendUnavailable:
            return False
        try:
            return bool(hid.enumerate(VENDOR_ID, PRODUCT_ID))
        except Exception:
            return False

    @staticmethod
    def find_paths(vendor_id: int = VENDOR_ID, product_id: int = PRODUCT_ID):
        """Return the (command, data) HID paths, or (None, None)."""
        hid = _hid()
        cmd = data = None
        for dev in hid.enumerate(vendor_id, product_id):
            if dev.get("usage_page") != 0xFF00 or dev.get("usage") != 0x0001:
                continue
            path = dev["path"]
            if isinstance(path, bytes):
                path = path.decode("utf-8", "replace")
            if "&Col05#" in path:
                cmd = dev["path"]
            elif "&Col06#" in path:
                data = dev["path"]
        return cmd, data

    def open(self) -> None:
        hid = _hid()
        cmd_path, data_path = self.find_paths(self.vendor_id, self.product_id)
        if not cmd_path or not data_path:
            raise BackendUnavailable(
                "258A:0049 interfaces Col05/Col06 not found - is the board plugged in "
                "over wired USB? Bluetooth and 2.4 GHz take it off the bus entirely."
            )

        # A stale handle after a replug accepts writes and sends them nowhere.
        self.cmd = hid.device()
        self.cmd.open_path(cmd_path)
        self.data = hid.device()
        self.data.open_path(data_path)
        self.data.set_nonblocking(1)

        # Unlock once, then commit once: the commit restarts the lighting engine,
        # so doing it per frame causes flicker and latency.
        self._send(self.cmd, UNLOCK, "unlock")
        self._send(self.data, new_frame(MODE_COMMIT), "commit")

    def close(self) -> None:
        for handle in (self.cmd, self.data):
            try:
                if handle is not None:
                    handle.close()
            except Exception:
                pass
        self.cmd = self.data = None

    def lamps(self) -> list[Lamp]:
        return list(self._lamps)

    # -- painting ---------------------------------------------------------
    def build(self, colours: Sequence[RGB], header: bytes) -> bytearray:
        """Pack a colour per slot into one 1032-byte report."""
        frame = new_frame(header)
        for slot, rgb in enumerate(colours):
            if slot >= BLOCK:
                break
            r, g, b = rgb
            frame[R_START + slot] = r
            frame[G_START + slot] = g
            frame[B_START + slot] = b
        return frame

    def write(self, colours: Sequence[RGB]) -> None:
        if self.data is None:
            raise BackendUnavailable("backend is not open")

        # The board cannot be updated key by key: bank 1 holds the number row,
        # bank 2 holds the rest, and both are rewritten every frame.
        self._send(self.data, self.build(colours, HEADER_PERKEY_1), "bank1")
        self._send(self.data, self.build([(0, 0, 0)] * BLOCK, HEADER_PERKEY_2), "bank2")

    # -- low level --------------------------------------------------------
    def _pace(self) -> None:
        import time
        delta = time.time() - self._last_write
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last_write = time.time()

    def _send(self, handle, payload, label="", attempts=2) -> bool:
        """Feature reports are not reliable here: retry, and pace them."""
        import time
        for attempt in range(attempts):
            self._pace()
            try:
                if handle.send_feature_report(bytes(payload)) == len(payload):
                    return True
            except Exception:
                pass
            time.sleep(0.02)
        return False
