"""Lab tool for an EVision-family keyboard's vendor interface.

Read-only by default. Writing requires an explicit --send, so nothing surprising
happens by accident.

    python evision-probe.py                          list interfaces and capabilities
    python evision-probe.py --read 2000              listen for input reports
    python evision-probe.py --send "01 02 03"        send one output report
    python evision-probe.py --send "01 02" --repeat 3 --delay 20

Bytes are written exactly as given (plus the report id the interface expects, if
it uses report ids). This tool knows nothing about the protocol; that is the
point - it is how a protocol gets confirmed.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time

import hid

USAGE_PAGE = 0xFF1C          # EVision vendor page


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


def caps_for(path: str):
    """(usage_page, usage, in_len, out_len, feature_len) or None."""
    kernel32 = ctypes.windll.kernel32
    hid_dll = ctypes.WinDLL("hid.dll")
    kernel32.CreateFileW.restype = wt.HANDLE
    handle = kernel32.CreateFileW(path, 0, 3, None, 3, 0, None)
    if handle == wt.HANDLE(-1).value or not handle:
        return None
    try:
        preparsed = ctypes.c_void_p()
        caps = HIDP_CAPS()
        if hid_dll.HidD_GetPreparsedData(wt.HANDLE(handle), ctypes.byref(preparsed)) and \
           hid_dll.HidP_GetCaps(preparsed, ctypes.byref(caps)) == 0x00110000:
            hid_dll.HidD_FreePreparsedData(preparsed)
            return (caps.UsagePage, caps.Usage, caps.InputReportByteLength,
                    caps.OutputReportByteLength, caps.FeatureReportByteLength)
    finally:
        kernel32.CloseHandle(wt.HANDLE(handle))
    return None


def vendor_interfaces():
    out = []
    for d in hid.enumerate():
        if (d.get("usage_page") or 0) != USAGE_PAGE:
            continue
        raw = d["path"]
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        out.append((d, raw, text, caps_for(text)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", help="hex bytes to write, e.g. \"01 02 FF\"")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--delay", type=int, default=50, help="ms between repeats")
    ap.add_argument("--read", type=int, default=0, help="listen this many ms")
    ap.add_argument("--index", type=int, default=0, help="which interface if several")
    args = ap.parse_args()

    found = vendor_interfaces()
    if not found:
        print("no interface with usage page 0x%04X found" % USAGE_PAGE)
        return 1

    print(f"{len(found)} interface(s) on usage page 0x{USAGE_PAGE:04X}:")
    for i, (d, raw, path, caps) in enumerate(found):
        desc = f"  [{i}] {d.get('vendor_id'):04X}:{d.get('product_id'):04X} iface {d.get('interface_number')}"
        if caps:
            desc += (f"  usage 0x{caps[1]:04X}  in {caps[2]}B out {caps[3]}B feature {caps[4]}B")
        print(desc)

    if not args.send and not args.read:
        return 0

    _, raw, path, caps = found[args.index]
    device = hid.device()
    device.open_path(raw)
    device.set_nonblocking(0)
    print(f"\nopened interface {args.index}")

    try:
        if args.send:
            payload = bytes(int(b, 16) for b in args.send.replace(",", " ").split())
            print(f"sending {len(payload)} bytes x{args.repeat}: {payload.hex(' ')}")
            for i in range(args.repeat):
                written = device.write(payload)
                print(f"  write {i + 1}: {written} bytes accepted")
                if i + 1 < args.repeat:
                    time.sleep(args.delay / 1000.0)
        if args.read:
            print(f"listening {args.read} ms ...")
            device.set_nonblocking(1)
            deadline = time.time() + args.read / 1000.0
            seen = 0
            while time.time() < deadline:
                data = device.read(64)
                if data:
                    seen += 1
                    print("  in:", bytes(data).hex(" "))
            print(f"  {seen} input report(s)")
    finally:
        device.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
