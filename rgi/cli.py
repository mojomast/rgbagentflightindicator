"""Command line entry point.

    rgi detect                      what backends can see, and what lights up
    rgi map [--backend X]           walk the lamps one at a time (calibration)
    rgi daemon [--backend X]        run the panel
    rgi watch                       report OpenCode sessions to the panel
    rgi push <state> [--lane N]     set one lane by hand (testing, scripts)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

from . import __version__
from .backends import available_backends, load
from .backends.base import BackendUnavailable

DEFAULT_URL = "http://127.0.0.1:8730"


def _add_backend_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--backend", default="auto",
                    help="keyboard driver: auto, sinowealth, openrgb, sysfs, lamparray, dummy")
    ap.add_argument("--device", type=int, default=0, help="OpenRGB device index")
    ap.add_argument("--openrgb-host", default="127.0.0.1")
    ap.add_argument("--openrgb-port", type=int, default=6742)
    ap.add_argument("--leds", type=int, default=None,
                    help="override the lamp count (use when a parse fails)")
    ap.add_argument("--lamp", action="append", default=[],
                    help="backend-specific lamp filter, repeatable (sysfs globs)")
    ap.add_argument("--debug", action="store_true")


def pick_backend(name: str):
    """Resolve --backend auto to the first backend that reports itself available."""
    if name != "auto":
        return name
    for candidate, ok in available_backends():
        if ok and candidate != "dummy":
            return candidate
    return "dummy"


def cmd_detect(args: argparse.Namespace) -> int:
    print(f"rgi {__version__}\n")
    print("backends:")
    for name, ok in available_backends():
        print(f"  {'YES' if ok else ' no'}  {name}")

    chosen = pick_backend(args.backend)
    print(f"\nwould use: {chosen}")
    if chosen == "dummy":
        return 0

    cls = load(chosen)
    kwargs = {}
    if chosen == "openrgb":
        kwargs = {"host": args.openrgb_host, "port": args.openrgb_port,
                  "device": args.device, "leds": args.leds, "debug": True}
    backend = cls(**kwargs)
    try:
        backend.open()
    except BackendUnavailable as exc:
        print(f"\ncould not open: {exc}")
        return 1

    lamps = backend.lamps()
    print(f"\n{backend.name}: {len(lamps)} lamps, {backend.min_interval * 1000:.0f} ms min interval")
    groups: dict[str, int] = {}
    for lamp in lamps:
        groups[lamp.group] = groups.get(lamp.group, 0) + 1
    for group, n in sorted(groups.items()):
        print(f"  {group or '(unlabelled)'}: {n}")
    print("  first lamps: " + ", ".join(f"{l.index}={l.label}" for l in lamps[:16]))
    lanes = backend.default_lanes(min(12, len(lamps)))
    print("  default lanes: " + ", ".join(str(i) for i in lanes))
    backend.close()
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    """Light one lamp at a time so you can label your own board."""
    cls = load(pick_backend(args.backend))
    backend = cls()
    try:
        backend.open()
    except BackendUnavailable as exc:
        print(f"could not open: {exc}")
        return 1

    lamps = backend.lamps()
    print(f"{backend.name}: walking {len(lamps)} lamps, 0.6 s each. Note what lights up.")
    try:
        for lamp in lamps:
            colours = [(0, 0, 0)] * len(lamps)
            colours[lamp.index] = (255, 255, 255)
            backend.write(colours)
            print(f"  lamp {lamp.index:>4}  ({lamp.label})")
            time.sleep(0.6)
    except KeyboardInterrupt:
        pass
    finally:
        backend.write([(0, 0, 0)] * len(lamps))
        backend.close()
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from .daemon import run
    args.backend = pick_backend(args.backend)
    return run(args)


def cmd_watch(args: argparse.Namespace) -> int:
    from .watchers.opencode import Watcher
    watcher = Watcher(url=args.url, token=args.token,
                      include_subagents=args.include_subagents, stale=args.stale)
    try:
        watcher.run()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """Set one lane by hand - handy for testing an indicator or scripting."""
    payload = {"agent": args.agent, "sessionID": args.session, "label": args.label}
    if args.slot is not None:
        payload["slot"] = args.slot
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-LED-Token"] = args.token

    def call(path: str, body: dict) -> dict:
        req = urllib.request.Request(args.url + path, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read() or b"{}")

    started = call("/session/start", payload)
    print(f"lane: {started}")
    if args.state:
        print(f"state: {call('/session/state', {'sessionID': args.session, 'state': args.state})}")
    if args.release:
        print(f"end: {call('/session/end', {'sessionID': args.session})}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="rgi", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"rgi {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("detect", help="show what backends and lamps are visible")
    _add_backend_args(d)
    d.set_defaults(func=cmd_detect)

    m = sub.add_parser("map", help="walk the lamps one at a time (calibration)")
    m.add_argument("--backend", default="auto")
    m.add_argument("--device", type=int, default=0)
    m.add_argument("--openrgb-host", default="127.0.0.1")
    m.add_argument("--openrgb-port", type=int, default=6742)
    m.add_argument("--leds", type=int, default=None)
    m.add_argument("--lamp", action="append", default=[])
    m.set_defaults(func=cmd_map)

    s = sub.add_parser("daemon", help="run the panel")
    _add_backend_args(s)
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address; 0.0.0.0 to accept agents from other machines")
    s.add_argument("--port", type=int, default=8730)
    s.add_argument("--token", default=None, help="shared secret; also RGI_TOKEN")
    s.add_argument("--count", type=int, default=12, help="how many lanes to offer")
    s.add_argument("--lanes", nargs="*", default=None, help="explicit lamp indices")
    s.add_argument("--no-quiet", action="store_true",
                   help="do not freeze the frame while the human types")
    s.add_argument("--quiet-ms", type=int, default=900)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_daemon)

    w = sub.add_parser("watch", help="report OpenCode sessions to the panel")
    w.add_argument("--url", default=DEFAULT_URL)
    w.add_argument("--token", default=None)
    w.add_argument("--include-subagents", action="store_true")
    w.add_argument("--stale", type=float, default=2 * 3600,
                   help="seconds of inactivity before a lamp is reused")
    w.set_defaults(func=cmd_watch)

    p = sub.add_parser("push", help="set one lane by hand")
    p.add_argument("state", nargs="?", default="working",
                   choices=["working", "done", "blocked", "error", "idle"])
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--token", default=None)
    p.add_argument("--session", default="rgi-manual")
    p.add_argument("--agent", default="manual")
    p.add_argument("--label", default="manual test")
    p.add_argument("--slot", type=int, default=None)
    p.add_argument("--release", action="store_true")
    p.set_defaults(func=cmd_push)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
