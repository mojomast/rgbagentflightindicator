"""Lab tool for the Sinowealth 258A:0049 vendor interfaces.

Read-only by default. Writing needs --paint, --hold or --send, so nothing
surprising happens by accident.

    python tools/sinowealth_probe.py                 list the vendor interfaces
    python tools/sinowealth_probe.py --caps          add report sizes, via hid.dll
    python tools/sinowealth_probe.py --red 0-12      unlock, commit, paint red
    python tools/sinowealth_probe.py --hold 3        keep repainting for 3 seconds
    python tools/sinowealth_probe.py --frame-len 512 try a different report size
    python tools/sinowealth_probe.py --send "06 09 BC 00 40" --iface Col06

Every write prints how many bytes the firmware accepted, because a write that is
accepted and ignored is exactly the failure this board is known for: the Col03
interface takes bytes and does nothing with them.

WARNING: opening the board sends its own unlock and mode commit, which takes it
away from a running daemon - the daemon is then left holding a handle the
firmware no longer honours, and the panel silently stops updating. Stop the
daemon (`rgi daemon` is the process to kill) before using --paint/--send, and
start it again afterwards.
"""

from __future__ import annotations

import argparse
import sys
import time

import hid

VENDOR_ID = 0x258A
PRODUCT_ID = 0x0049
USAGE_PAGE = 0xFF00
USAGE = 0x0001

DEFAULT_FRAME_LEN = 1032
BLOCK = 126
B_START, G_START, R_START = 29, 155, 281

# Col08's feature report is 382 bytes, and 4 + 126 * 3 = 382: a short header
# followed by one interleaved RGB triple per key. Unconfirmed - see docs.
RGB_FRAME_LEN = 382
RGB_HEADER = bytes([0x06, 0x09, 0xBC, 0x00])

HEADER_PERKEY_1 = bytes([0x06, 0x09, 0xBC, 0x00, 0x40, 0x00, 0x00, 0x00])
HEADER_PERKEY_2 = bytes([0x06, 0x09, 0xC0, 0x00, 0x40, 0x00, 0x00, 0x00])
MODE_COMMIT = bytes([0x06, 0x03, 0xB6, 0x00, 0x00, 0x00, 0x00, 0x00])
UNLOCK = bytes([0x05, 0x83, 0xB6, 0x00, 0x00, 0x00])

COLOURS = {
    "off": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
}


def _collection(path: str) -> str:
    """`Col06` out of a Windows HID path, or `?` when it has no collection.

    Windows writes these as `Col06#7`, where the number is the collection id.
    """
    for part in str(path).split("&"):
        if part.startswith("Col"):
            return part.split("#", 1)[0]
    return "?"


def interfaces():
    out = []
    for dev in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        if (dev.get("usage_page") or 0) != USAGE_PAGE or (dev.get("usage") or 0) != USAGE:
            continue
        raw = dev["path"]
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        out.append({"raw": raw, "path": text, "col": _collection(text),
                    "iface": dev.get("interface_number")})
    return out


def caps_for(path: str):
    """(usage_page, usage, in_len, out_len, feature_len), or None.

    Borrowed from the EVision probe: the Windows HID API is the only thing here
    that knows how long a feature report is allowed to be.
    """
    try:
        from evision_probe import caps_for as _caps      # same directory
    except ImportError:
        try:
            from tools.evision_probe import caps_for as _caps
        except ImportError:
            return None
    return _caps(path)


def parse_slots(text: str) -> set[int]:
    """`0-12`, `0,4,8` or `all` -> a set of slot numbers."""
    text = (text or "").strip().lower()
    if text in ("all", "*"):
        return set(range(BLOCK))
    slots: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            slots.update(range(int(low), int(high) + 1))
        else:
            slots.add(int(part))
    return {s for s in slots if 0 <= s < BLOCK}


def frame(header: bytes, colours: dict[int, tuple[int, int, int]],
          length: int = DEFAULT_FRAME_LEN) -> bytearray:
    out = bytearray(length)
    out[0:len(header)] = header[:length]
    for slot, (r, g, b) in colours.items():
        if slot >= BLOCK or R_START + slot >= length:
            continue
        out[R_START + slot] = r
        out[G_START + slot] = g
        out[B_START + slot] = b
    return out


def frame_rgb(header: bytes, colours: dict[int, tuple[int, int, int]],
              length: int = RGB_FRAME_LEN) -> bytearray:
    """Interleaved R,G,B per slot - the layout Col08's size implies.

    4 + 126 * 3 = 382, which is exactly Col08's feature report length, so this
    is a hypothesis about that interface and not a recovered fact.
    """
    out = bytearray(length)
    out[0:len(header)] = header[:length]
    for slot, (r, g, b) in colours.items():
        base = 4 + slot * 3
        if slot >= BLOCK or base + 3 > length:
            continue
        out[base] = r
        out[base + 1] = g
        out[base + 2] = b
    return out


def listen(found: list[dict], args) -> int:
    """Print input reports from one interface, or from every readable one.

    Read-only: this never writes, so it cannot disturb the board's lighting or
    its typing. What it is for is letting the board describe itself - a status
    byte that changes with the lighting mode is often the clue that explains
    why a frame is accepted and ignored.
    """
    chosen = [i for i in found if not args.iface or i["col"] == args.iface]
    if not chosen:
        print(f"no interface called {args.iface}")
        return 1
    print(f"\nlistening {args.read} ms on "
          f"{', '.join(i['col'] for i in chosen)} (type a key if you want to "
          f"see input flow)")
    for item in chosen:
        device = hid.device()
        try:
            device.open_path(item["raw"])
        except Exception as exc:
            print(f"  {item['col']}: cannot open ({exc})")
            continue
        try:
            device.set_nonblocking(1)
            deadline = time.time() + args.read / 1000.0
            seen = 0
            while time.time() < deadline:
                try:
                    data = device.read(64)
                except OSError as exc:
                    # An interface with no input endpoint refuses reads on some
                    # stacks. That is an answer, not a failure.
                    print(f"  {item['col']}: no input endpoint ({exc})")
                    break
                if data:
                    seen += 1
                    print(f"  {item['col']}: {bytes(data).hex(' ')}")
            if not seen:
                print(f"  {item['col']}: no input reports")
        finally:
            try:
                device.close()
            except Exception:
                pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--caps", action="store_true",
                    help="also read report sizes from the Windows HID API")
    ap.add_argument("--iface", default="", help="which collection to write to")
    for name in COLOURS:
        ap.add_argument(f"--{name}", metavar="SLOTS",
                        help=f"paint SLOTS ({name}); e.g. 0-12, 0,4,8, all")
    ap.add_argument("--hold", type=float, default=0.0, metavar="SECONDS",
                    help="repaint this often for this long")
    ap.add_argument("--frame-len", type=int, default=DEFAULT_FRAME_LEN,
                    help=f"report size to build (default {DEFAULT_FRAME_LEN})")
    ap.add_argument("--bank", choices=("1", "2", "both"), default="both",
                    help="which LED bank to paint (1 = header 06 09 BC, "
                         "2 = header 06 09 C0)")
    ap.add_argument("--layout", choices=("planar", "rgb"), default="planar",
                    help="planar = B/G/R blocks (Col06); rgb = one interleaved "
                         "triple per key (Col08, unconfirmed)")
    ap.add_argument("--once", action="store_true",
                    help="write a single frame and stop - safer on a board whose "
                         "firmware is unknown, and it cannot drop keystrokes")
    ap.add_argument("--pace", type=float, default=0.0, metavar="MS",
                    help="milliseconds to wait between writes (the driver uses 13)")
    ap.add_argument("--read", type=int, default=0, metavar="MS",
                    help="listen for input reports this many milliseconds "
                         "(only interfaces that report input can do this)")
    ap.add_argument("--send", help="raw hex bytes to write instead of painting")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--delay", type=float, default=0.05, help="seconds between sends")
    args = ap.parse_args()

    found = interfaces()
    if not found:
        print(f"no 258A:0049 interface on usage page {USAGE_PAGE:04X}; "
              "is the board plugged in over wired USB?")
        return 1

    print(f"{len(found)} vendor interface(s) on {VENDOR_ID:04X}:{PRODUCT_ID:04X}:")
    for item in found:
        line = f"  {item['col']:<6} iface {item['iface']}"
        if args.caps:
            caps = caps_for(item["path"])
            if caps:
                line += (f"  usage {caps[1]:04X}  in {caps[2]}B out {caps[3]}B"
                         f" feature {caps[4]}B")
            else:
                line += "  (caps unavailable - is the board in use?)"
        print(line)

    if args.read:
        return listen(found, args)

    wanted = [(name, parse_slots(getattr(args, name)))
              for name in COLOURS if getattr(args, name)]
    if not wanted and not args.send:
        print("\nread-only. add --red 0-12 (or --send) to write something.")
        return 0

    cmd = next((i for i in found if i["col"] == "Col05"), None)
    data = next((i for i in found if i["col"] == "Col06"), None)
    target = data
    if args.iface:
        target = next((i for i in found if i["col"] == args.iface), None)
        if target is None:
            print(f"\nno interface called {args.iface}; nothing written")
            return 1
    if cmd is None or data is None:
        print("\nCol05/Col06 not both present; nothing written")
        return 1

    # The frame has to be exactly as long as the report the firmware declares,
    # or it refuses the write: Col06 takes 1032 and rejects 382.
    frame_len = {"rgb": RGB_FRAME_LEN}.get(args.layout, args.frame_len)
    if args.frame_len != DEFAULT_FRAME_LEN and args.layout != "rgb":
        frame_len = args.frame_len
    print(f"\nwriting through {target['col']}, frame {frame_len} bytes "
          f"({args.layout})")
    cmd_handle = hid.device()
    data_handle = hid.device()
    cmd_handle.open_path(cmd["raw"])
    data_handle.open_path(target["raw"])
    data_handle.set_nonblocking(1)

    def send(handle, payload, label):
        if args.pace > 0:
            time.sleep(args.pace / 1000.0)
        accepted = handle.send_feature_report(bytes(payload))
        print(f"  {label}: {accepted}/{len(payload)} bytes accepted"
              f"{'' if accepted == len(payload) else '   <-- REJECTED'}")
        return accepted == len(payload)

    def banks(colours):
        """(label, header, colours) for the requested --bank, in write order."""
        out = []
        if args.bank in ("1", "both"):
            out.append(("bank1", HEADER_PERKEY_1, colours))
        if args.bank in ("2", "both"):
            # bank 2 carries the rest of the keys; paint it too when asked for
            # one bank alone, otherwise clear it as the driver does
            out.append(("bank2", HEADER_PERKEY_2,
                        colours if args.bank == "2" else {}))
        return out

    def build_frame(header, colours):
        if args.layout == "rgb":
            return frame_rgb(header, colours)
        return frame(header, colours, args.frame_len)

    try:
        send(cmd_handle, UNLOCK, "unlock   ")
        send(data_handle, build_frame(MODE_COMMIT, {}), "commit   ")
        colours: dict[int, tuple[int, int, int]] = {}
        for name, slots in wanted:
            for slot in slots:
                colours[slot] = COLOURS[name]
        if args.send:
            payload = bytes(int(b, 16) for b in args.send.replace(",", " ").split())
            for index in range(max(1, args.repeat)):
                send(data_handle, payload, f"send {index + 1}")
                if index + 1 < args.repeat:
                    time.sleep(args.delay)
        elif colours:
            for index in range(max(1, args.repeat)):
                for label, header, payload_colours in banks(colours):
                    send(data_handle, build_frame(header, payload_colours),
                         f"{label} {index + 1}")
                if index + 1 < args.repeat:
                    time.sleep(args.delay)
            if args.once:
                print("  (--once: single frame written, stopping)")
            deadline = time.time() + max(0.0, args.hold)
            while time.time() < deadline:
                for label, header, payload_colours in banks(colours):
                    send(data_handle, build_frame(header, payload_colours),
                         f"hold {label}")
                time.sleep(0.1)
    finally:
        for handle in (cmd_handle, data_handle):
            try:
                handle.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
