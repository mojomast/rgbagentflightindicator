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
import os
import sys
import time
import urllib.request

from . import __version__
from .backends import available_backends, load
from .backends.base import BackendUnavailable
from .config import DEFAULT_URL, resolve_url     # noqa: F401  (public surface)

def cmd_mcp(args: argparse.Namespace) -> int:
    import asyncio
    from .mcp_server import PanelClient, serve

    try:
        client = PanelClient(args.url, namespace=args.namespace, timeout=args.timeout)
        asyncio.run(serve(client))
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
        print("Install MCP support with: python -m pip install -e '.[openai]'",
              file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"rgi mcp: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


def _add_backend_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--backend", action="append", default=None,
                    help="keyboard driver, repeatable. Default: every supported keyboard "
                         "that is connected (sinowealth, evision, openrgb, sysfs, dummy)")
    ap.add_argument("--device", type=int, default=0, help="OpenRGB device index")
    ap.add_argument("--openrgb-host", default="127.0.0.1")
    ap.add_argument("--openrgb-port", type=int, default=6742)
    ap.add_argument("--leds", type=int, default=None,
                    help="override the lamp count (use when a parse fails)")
    ap.add_argument("--lamp", action="append", default=[],
                    help="backend-specific lamp filter, repeatable (sysfs globs)")
    ap.add_argument("--debug", action="store_true")


def pick_backend(names) -> str:
    """Resolve --backend to a single name for the one-shot commands.

    Accepts a name, a list of names, or None (meaning auto). Detection order
    decides when several are possible.
    """
    if isinstance(names, str):
        names = [names]
    if names and names != ["auto"]:
        return names[0]
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
    lamps = cls(**({"count": 13} if chosen == "dummy" else {})).lamps()
    if lamps:
        print(f"\n{cls.name}: {len(lamps)} lamps, {cls.min_interval * 1000:.0f} ms min interval")
        groups: dict[str, int] = {}
        for lamp in lamps:
            groups[lamp.group] = groups.get(lamp.group, 0) + 1
        for group, n in sorted(groups.items()):
            print(f"  {group or '(unlabelled)'}: {n}")
        print("  first lamps: " + ", ".join(f"{l.index}={l.label}" for l in lamps[:16]))
        backend = cls(**({"count": 13} if chosen == "dummy" else {}))
        lanes = backend.default_lanes(min(12, len(lamps)))
        print("  default lanes: " + ", ".join(str(i) for i in lanes))
        return 0

    # Nothing to report without claiming the device. Say so rather than doing it
    # quietly: claiming it takes the board away from anything already driving it.
    print(f"\n{chosen}: needs the device to answer. Re-run with --open to claim it")
    print("  (this briefly takes the device over - do not do it while a daemon")
    print("   is driving the panel, or restart that daemon afterwards)")
    if not args.open:
        return 0

    kwargs = {}
    if chosen == "openrgb":
        kwargs = {"host": args.openrgb_host, "port": args.openrgb_port,
                  "device": args.device, "leds": args.leds, "debug": True}
    if chosen == "dummy":
        kwargs = {"count": 13}
    try:
        backend = cls(**kwargs)
        backend.open()
    except BackendUnavailable as exc:
        print(f"\ncould not open: {exc}")
        return 1

    lamps = backend.lamps()
    print(f"\n{backend.name}: {len(lamps)} lamps, {backend.min_interval * 1000:.0f} ms min interval")
    groups = {}
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


def cmd_hook(args: argparse.Namespace) -> int:
    from . import hooks

    if getattr(args, "debug", False):
        os.environ["RGI_HOOK_DEBUG"] = "1"
    return hooks.run(args.harness)


def cmd_watch(args: argparse.Namespace) -> int:
    from .daemon import resolve_token
    from .watchers.opencode import Watcher

    # the panel requires a token as soon as it is not localhost-only, and the
    # watcher has to find it the same way the daemon does or every call is a 401
    watcher = Watcher(url=args.url, token=resolve_token(args.token),
                      include_subagents=args.include_subagents, stale=args.stale,
                      ident=args.ident)
    try:
        watcher.run()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def _panel_get(url: str, path: str, token: str | None, timeout: float = 5.0) -> dict:
    headers = {}
    if token:
        headers["X-LED-Token"] = token
    req = urllib.request.Request(url.rstrip("/") + path, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def _render_status(data: dict, url: str) -> str:
    """The lane table, shaped so another agent can read it as easily as a human."""
    devices = data.get("devices") or []
    out = [f"rgbafi {__version__}  {url}"]
    for d in devices:
        per = "per-key" if d.get("per_lamp") else "single colour"
        mode = f" {d['mode']}" if d.get("mode") else ""
        out.append(f"  device {d.get('label','?'):12} {d.get('lamps',0):>4} lamps  "
                   f"{per}{mode}")
    sessions = data.get("sessions") or {}
    if not sessions:
        out.append("  no lanes claimed")
        return "\n".join(out)

    # lane -> {device label: lamp name}, so a lane on two keyboards shows both
    by_device: dict[int, dict[str, str]] = {}
    for d in devices:
        for slot, key in enumerate(d.get("lanes") or []):
            by_device.setdefault(slot, {})[d.get("label", "?")] = key

    out.append("")
    for sid, info in sorted(sessions.items(), key=lambda kv: kv[1].get("slot", 99)):
        slot = info.get("slot", "?")
        where = " ".join(f"{label}={key}" for label, key in by_device.get(slot, {}).items())
        label = (info.get("label") or sid)[:34]
        out.append(f"  lane {str(slot):>2}  {info.get('state','?'):8}  {label:34} "
                   f"[{info.get('host') or '?'}]  {where}  {sid[-12:]}")
    return "\n".join(out)


def cmd_lane_map(args: argparse.Namespace) -> int:
    """Which agent gets which lane: read it, or change it."""
    from .daemon import DEFAULT_LANE_MAP, load_lane_map

    path = args.file or DEFAULT_LANE_MAP
    policy = load_lane_map(path)
    changed = False

    for pair in args.set or []:
        name, _, lane = pair.partition("=")
        if not name or not lane.strip().lstrip("-").isdigit():
            print(f"  expected name=lane, got {pair!r}")
            return 1
        policy[name] = int(lane)
        changed = True
    for name in args.unset or []:
        if policy.pop(name, None) is not None:
            changed = True

    if changed:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(policy, fh, indent=2, sort_keys=True)
        print(f"  wrote {path}")

    if policy:
        print(f"  lane map ({path}):")
        for name in sorted(policy, key=lambda k: policy[k]):
            print(f"    lane {policy[name]:>3}  {name}")
    else:
        print(f"  no lane map at {path} - every agent takes the first free lane")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show every lane and every device - the observable side of the panel."""
    from .daemon import resolve_token

    token = resolve_token(args.token)
    if args.follow:
        last = None
        while True:
            try:
                data = _panel_get(args.url, "/status", token)
                signature = json.dumps(data, sort_keys=True)
                if signature != last:
                    last = signature
                    stamp = time.strftime("%H:%M:%S")
                    print(f"--- {stamp} ---")
                    print(_render_status(data, args.url), flush=True)
            except Exception as exc:
                print(f"--- {time.strftime('%H:%M:%S')} --- panel unreachable: {exc}",
                      flush=True)
                last = None
            time.sleep(args.interval)
    try:
        data = _panel_get(args.url, "/status", token)
    except Exception as exc:
        print(f"panel unreachable at {args.url}: {exc}")
        return 1
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(_render_status(data, args.url))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """What is installed on this machine, and what is fighting what.

    The question this answers: several agents share one machine, and none of them
    should install a second copy of anything.
    """
    import glob
    from .daemon import resolve_token
    from .watchers.opencode import LOCK_PATH

    problems = 0
    print(f"rgbafi {__version__} - this machine")

    # the panel
    token = resolve_token(args.token)
    try:
        data = _panel_get(args.url, "/status", token)
        devices = data.get("devices") or []
        print(f"  panel     reachable at {args.url}: "
              f"{len(devices)} device(s), {len(data.get('sessions') or {})} lane(s)")
        for device in devices:
            print(f"              {device.get('label')}: {device.get('lamps')} lamps")
    except Exception as exc:
        print(f"  panel     NOT reachable at {args.url} ({exc})")
        problems += 1

    # the token
    print(f"  token     {'found' if token else 'MISSING'} "
          f"(--token, RGI_TOKEN, or ~/.config/rgi/token)")

    # the watcher: one per machine, and the lock says who has it
    try:
        with open(LOCK_PATH, encoding="utf-8") as fh:
            holder = fh.read().strip()
        print(f"  watcher   running (pid {holder.split()[0]})")
    except OSError:
        print("  watcher   not running - start one with `rgi watch`")

    # the plugin: exactly one, or the sidebar draws two blocks
    config = os.path.join(os.path.expanduser("~"), ".config", "opencode")
    found = []
    for path in glob.glob(os.path.join(config, "plugins", "*")):
        if os.path.isdir(path) and glob.glob(os.path.join(path, "tui.ts")):
            found.append(os.path.basename(path))
    retired = [name for name in found if "retired" in name or "bak" in name]
    live = [name for name in found if name not in retired]
    print(f"  plugin    {len(live)} live plugin director{'y' if len(live) == 1 else 'ies'}"
          f"{': ' + ', '.join(live) if live else ''}")
    for name in retired:
        print(f"              {name}: retired (fine to keep, but it is not loaded)")
    if len(live) > 1:
        print("              WARNING: more than one plugin would draw a sidebar block each")
        problems += 1
    if not live:
        print("              none installed - see /files/PLUGIN_SETUP_PROMPT.md")
    deps = os.path.join(config, "node_modules", "@opentui", "solid")
    print(f"  deps      {'present' if os.path.isdir(deps) else 'MISSING'} "
          f"(@opentui/solid under {config})")
    if not os.path.isdir(deps) and live:
        problems += 1

    print()
    print("  nothing to do" if not problems else f"  {problems} thing(s) need attention")
    return 0 if not problems else 1


def cmd_push(args: argparse.Namespace) -> int:
    """Set one lane by hand - handy for testing an indicator or scripting."""
    payload = {"agent": args.agent, "sessionID": args.session, "label": args.label}
    if args.slot is not None:
        payload["slot"] = args.slot
    from .daemon import resolve_token

    token = resolve_token(args.token)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-LED-Token"] = token

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

    mc = sub.add_parser("mcp", help="private stdio MCP tools for ChatGPT and Codex")
    mc.add_argument("--url", default=os.environ.get("RGI_URL", DEFAULT_URL),
                    help="local panel origin; loopback IP only")
    mc.add_argument("--namespace", default=os.environ.get("RGI_MCP_NAMESPACE", "private"),
                    help="session namespace; use one per private tunnel")
    mc.add_argument("--timeout", type=float, default=2.0, help="local API timeout, 0.1-10 seconds")
    mc.set_defaults(func=cmd_mcp)

    hk = sub.add_parser("hook", help="one lifecycle hook for a harness (JSON on stdin)")
    hk.add_argument("harness", help="which harness to speak for, e.g. claude-code, "
                                    "gemini-cli (see docs/integrations.md)")
    hk.add_argument("--debug", action="store_true", help="log decisions to stderr")
    hk.set_defaults(func=cmd_hook)

    d = sub.add_parser("detect", help="show what backends and lamps are visible")
    _add_backend_args(d)
    d.add_argument("--open", action="store_true",
                   help="claim the device to query it (takes it over briefly)")
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
    s.add_argument("--lane-map", default=None,
                   help="JSON file mapping agent name or ident -> lane "
                        "(default ~/.config/rgi/lanes.json)")
    s.add_argument("--no-quiet", action="store_true",
                   help="do not freeze the frame while the human types")
    s.add_argument("--quiet-ms", type=int, default=900)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_daemon)

    dr = sub.add_parser("doctor", help="what is installed on this machine, and what conflicts")
    dr.add_argument("--url", default=None,
                    help="panel address (default: RGI_URL, then ~/.config/rgi/url, "
                         "then localhost:8730)")
    dr.add_argument("--token", default=None)
    dr.set_defaults(func=cmd_doctor)

    st = sub.add_parser("status", help="show every lane and every device")
    st.add_argument("--url", default=None,
                    help="panel address (default: RGI_URL, then ~/.config/rgi/url, "
                         "then localhost:8730)")
    st.add_argument("--token", default=None)
    st.add_argument("--json", action="store_true", help="raw /status for scripting")
    st.add_argument("--follow", action="store_true", help="keep watching, print on change")
    st.add_argument("--interval", type=float, default=2.0)
    st.set_defaults(func=cmd_status)

    lm = sub.add_parser("lane-map", help="which agent gets which lane")
    lm.add_argument("--file", default=None, help="default ~/.config/rgi/lanes.json")
    lm.add_argument("--set", action="append", metavar="NAME=LANE",
                    help="e.g. --set hermes-3=5 (repeatable; NAME is an ident or agent)")
    lm.add_argument("--unset", action="append", metavar="NAME", help="remove a mapping")
    lm.set_defaults(func=cmd_lane_map)

    w = sub.add_parser("watch", help="report OpenCode sessions to the panel")
    w.add_argument("--url", default=None,
                    help="panel address (default: RGI_URL, then ~/.config/rgi/url, "
                         "then localhost:8730)")
    w.add_argument("--token", default=None)
    w.add_argument("--include-subagents", action="store_true")
    w.add_argument("--stale", type=float, default=2 * 3600,
                   help="seconds of inactivity before a lamp is reused")
    w.add_argument("--ident", default=None,
                   help="name every lane this watcher claims (default: RGI_IDENT, "
                        "then ~/.config/rgi/name, then the hostname)")
    w.set_defaults(func=cmd_watch)

    p = sub.add_parser("push", help="set one lane by hand")
    p.add_argument("state", nargs="?", default="working",
                   choices=["working", "done", "blocked", "error", "idle"])
    p.add_argument("--url", default=None,
                    help="panel address (default: RGI_URL, then ~/.config/rgi/url, "
                         "then localhost:8730)")
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
    if hasattr(args, "url"):
        args.url = resolve_url(args.url)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
