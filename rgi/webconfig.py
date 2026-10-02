"""The web UI configuration: appearance, layouts, mapping, agents, endpoints.

One versioned JSON file at ``~/.config/rgi/config.json`` is the single source of
truth for everything the web UI edits. It is deliberately a plain document a
human can read, diff and keep in version control; every field has a default, so
a hand-written file with one key still works.

Three rules shape this module:

* **Reads never fail.** A missing file is the defaults. A malformed one is
  reported at startup and treated as defaults, because a broken config must not
  take the panel down.
* **Unknown fields survive.** This config is data we intend to evolve. Writes
  merge over the existing document instead of replacing it, so keys a newer
  daemon added are not deleted by an older browser tab.
* **Writes are atomic and versioned.** ``revision`` is a monotonic integer for
  optimistic concurrency (``If-Match``); every successful write keeps a
  ``.bak`` chain and a history snapshot, so "revert to last night" is possible.

The runtime never reads this file directly: the daemon loads it once, and the
HTTP layer validates every change before it is written.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import time
from typing import Any

SCHEMA_VERSION = 1

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "rgi")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LEGACY_LANES_PATH = os.path.join(CONFIG_DIR, "lanes.json")
HISTORY_DIR = os.path.join(CONFIG_DIR, "history")
HISTORY_CAP = 50
BODY_MAX = 1_000_000            # bytes: a config is small; refuse surprises

STATE_NAMES = ("working", "done", "blocked", "error", "idle", "off")
PATTERNS = ("off", "steady", "blink", "breathe")
THEN = ("steady", "off")
_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class ConfigError(ValueError):
    """A write was refused. ``field`` names the offending path for the UI."""

    def __init__(self, message: str, field: str | None = None,
                 code: str = "validation_failed", hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code
        self.hint = hint

    def envelope(self) -> dict:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            out["field"] = self.field
        if self.hint:
            out["hint"] = self.hint
        return out


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_states() -> dict:
    """The palette from the daemon: green in flight, white complete, red blocked.

    ``done`` and ``blocked`` blink by default; ``cycles: 0`` means forever.
    These are the values the LEDs actually get, not a lookalike preview.
    """
    return {
        "working": {"label": "Working", "icon": "play", "pattern": "steady",
                    "color": "#00ff00", "brightness": 255},
        "done": {"label": "Done", "icon": "check", "pattern": "blink",
                 "color": "#ffffff", "brightness": 255, "cycles": 10,
                 "then": "steady"},
        "blocked": {"label": "Blocked", "icon": "alert", "pattern": "blink",
                    "color": "#ff0000", "brightness": 255, "cycles": 0,
                    "then": "steady"},
        "error": {"label": "Error", "icon": "x", "pattern": "steady",
                  "color": "#ff0000", "brightness": 255},
        "idle": {"label": "Idle", "icon": "dot", "pattern": "steady",
                 "color": "#282828", "brightness": 255},
        "off": {"label": "Off", "icon": "off", "pattern": "off",
                "color": "#000000", "brightness": 0},
    }


def default_config() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "updated_at": None,
        "settings": {
            "host": "127.0.0.1",
            "port": 8730,
            "count": 12,
            "quiet": True,
            "quiet_ms": 1500,
            "reduce_motion": False,
        },
        "appearance": {
            "blink": {"period_ms": 560, "duty": 0.5},
            "states": default_states(),
        },
        "devices": {},          # backend name -> {label, enabled, lane_pool, layout}
        "layouts": {},          # layout id -> {format_version, source, keys}
        "lanes": {"overrides": []},
        "lamp_overrides": [],   # [{device, lamp, mode: static, color}]
        "agents": [],           # [{id, label, match, enabled, notes}]
        "endpoints": [],        # [{id, kind, url, ...}]
    }


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------
def deep_fill(default: Any, data: Any) -> Any:
    """Every missing key from ``default`` filled into ``data``; data wins."""
    if isinstance(default, dict):
        out = copy.deepcopy(data) if isinstance(data, dict) else {}
        for key, value in default.items():
            out[key] = deep_fill(value, data[key]) if key in out else copy.deepcopy(value)
        return out
    if isinstance(default, list):
        return copy.deepcopy(data) if isinstance(data, list) else copy.deepcopy(default)
    return copy.deepcopy(data)


def deep_merge(base: Any, patch: Any) -> Any:
    """``patch`` over ``base``, keeping keys of ``base`` that patch does not name."""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = copy.deepcopy(base)
        for key, value in patch.items():
            out[key] = deep_merge(base[key], value) if key in base else copy.deepcopy(value)
        return out
    return copy.deepcopy(patch)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def migrate(raw: dict) -> dict:
    """Bring a document up to ``SCHEMA_VERSION``. Unknown newer versions refuse."""
    version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int):
        version = SCHEMA_VERSION
    if version > SCHEMA_VERSION:
        raise ConfigTooNew(version)
    # v1 is the first schema; future migrations are registered here as functions
    # from old version -> new version, and must never be deleted.
    out = copy.deepcopy(raw)
    out["schema_version"] = SCHEMA_VERSION
    return out


class ConfigTooNew(ConfigError):
    def __init__(self, version: int):
        super().__init__(
            f"config schema version {version} is newer than this daemon "
            f"understands ({SCHEMA_VERSION})",
            code="config_too_new",
            hint="update rgbafi, or restore a backup of ~/.config/rgi/config.json")


def _import_legacy() -> list[dict]:
    """Turn the old ``lanes.json`` (``{"hermes-3": 5}``) into lane overrides once.

    The legacy file is left on disk untouched: it is the user's data, and some
    other tool may still read it.
    """
    try:
        with open(LEGACY_LANES_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for key, lane in data.items():
        try:
            out.append({"id": str(key), "match": {"ident": str(key)},
                        "lane": int(lane), "enabled": True})
        except (TypeError, ValueError):
            continue
    return out


def load_raw(path: str | None = None) -> dict:
    """The document as it is (migrated), without defaults filled in."""
    path = path or CONFIG_PATH
    fresh = not os.path.exists(path)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw = migrate(raw)
    if fresh:
        imported = _import_legacy()
        if imported:
            raw.setdefault("lanes", {}).setdefault("overrides", [])
            raw["lanes"]["overrides"] = imported + list(raw["lanes"]["overrides"])
    return raw


def load_config(path: str | None = None) -> dict:
    """The effective config: the file merged over every default."""
    return deep_fill(default_config(), load_raw(path))


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def _rotate_baks(path: str) -> None:
    for i in range(4, 0, -1):
        src, dst = f"{path}.bak.{i}", f"{path}.bak.{i + 1}"
        if os.path.exists(src):
            os.replace(src, dst)


def _snapshot(directory: str, doc: dict) -> None:
    history = os.path.join(directory, "history") if (
        os.path.basename(directory) != "history") else directory
    try:
        os.makedirs(history, exist_ok=True)
        name = "%04d.json" % int(doc.get("revision") or 0)
        with open(os.path.join(history, name), "w", encoding="utf-8",
                  newline="\n") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
        kept = sorted(os.listdir(history))
        for old in kept[:-HISTORY_CAP]:
            os.remove(os.path.join(history, old))
    except OSError:
        pass                            # a missing history is not a failed write


def save_config(config: dict, path: str | None = None,
                existing: dict | None = None) -> dict:
    """Merge over the existing document, bump the revision, write atomically.

    ``existing`` is the raw document already on disk (as returned by
    ``load_raw``); passing it lets unknown fields survive. The returned document
    is what was written; the caller should hand it back to the client.
    """
    path = path or CONFIG_PATH
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)

    base = existing if isinstance(existing, dict) else {}
    doc = deep_merge(base, config) if base else copy.deepcopy(config)
    doc["schema_version"] = SCHEMA_VERSION
    doc["revision"] = int(base.get("revision") or 0) + 1
    doc["updated_at"] = _now_iso()

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.exists(path):
        _rotate_baks(path)
        try:
            shutil.copy2(path, path + ".bak.1", follow_symlinks=False)
        except OSError:
            pass
    os.replace(tmp, path)
    _snapshot(directory, doc)
    return doc


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_config(config: dict) -> tuple[list[dict], list[dict]]:
    """Return ``(errors, warnings)`` with field paths the UI can attach.

    Errors mean "cannot render"; they block Apply. Warnings mean "will look
    bad or confusing" and are advisory only.
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    def err(path: str, message: str) -> None:
        errors.append({"path": path, "message": message})

    def warn(path: str, message: str) -> None:
        warnings.append({"path": path, "message": message})

    if not isinstance(config, dict):
        err("", "the config must be an object")
        return errors, warnings

    settings = config.get("settings") or {}
    port = settings.get("port")
    if not _is_int(port) or not 1 <= port <= 65535:
        err("settings.port", "port must be between 1 and 65535")
    count = settings.get("count")
    if not _is_int(count) or not 1 <= count <= 512:
        err("settings.count", "count must be between 1 and 512")
    quiet_ms = settings.get("quiet_ms")
    if not _is_int(quiet_ms) or not 0 <= quiet_ms <= 60000:
        err("settings.quiet_ms", "quiet_ms must be 0..60000")
    for key in ("quiet", "reduce_motion"):
        if not isinstance(settings.get(key), bool):
            err(f"settings.{key}", f"{key} must be true or false")

    appearance = config.get("appearance") or {}
    blink = appearance.get("blink") or {}
    period = blink.get("period_ms", 560)
    if not _is_int(period) or not 40 <= period <= 10000:
        err("appearance.blink.period_ms", "period_ms must be 40..10000")
    duty = blink.get("duty", 0.5)
    if not isinstance(duty, (int, float)) or not 0.0 <= float(duty) <= 1.0:
        err("appearance.blink.duty", "duty must be between 0 and 1")

    states = appearance.get("states") or {}
    for name in STATE_NAMES:
        spec = states.get(name)
        path = f"appearance.states.{name}"
        if not isinstance(spec, dict):
            err(path, "missing")
            continue
        colour = spec.get("color")
        if not isinstance(colour, str) or not _COLOR.match(colour):
            err(path + ".color", "color must be #rgb or #rrggbb")
        pattern = spec.get("pattern")
        if pattern not in PATTERNS:
            err(path + ".pattern", "pattern must be one of " + ", ".join(PATTERNS))
        brightness = spec.get("brightness", 255)
        if not _is_int(brightness) or not 0 <= brightness <= 255:
            err(path + ".brightness", "brightness must be 0..255")
        cycles = spec.get("cycles", 0)
        if not _is_int(cycles) or cycles < 0:
            err(path + ".cycles", "cycles must be 0 (forever) or a positive integer")
        then = spec.get("then", "steady")
        if then not in THEN:
            err(path + ".then", "then must be steady or off")
        if pattern in ("blink", "breathe"):
            state_period = spec.get("period_ms", period)
            if not _is_int(state_period) or not 40 <= state_period <= 10000:
                err(path + ".period_ms", "period_ms must be 40..10000")
            state_duty = spec.get("duty", duty)
            if not isinstance(state_duty, (int, float)) or not 0.0 <= float(state_duty) <= 1.0:
                err(path + ".duty", "duty must be between 0 and 1")

    colours = [states.get(n, {}).get("color") for n in STATE_NAMES]
    if len(set(c for c in colours if c)) != len([c for c in colours if c]):
        warn("appearance.states", "two states share a colour; the icons and "
                                  "labels are doing all the work")

    layouts = config.get("layouts") or {}
    for layout_id, layout in layouts.items():
        path = f"layouts.{layout_id}"
        if not isinstance(layout, dict):
            err(path, "layout must be an object")
            continue
        seen: set[int] = set()
        keys = layout.get("keys")
        if not isinstance(keys, list):
            err(path + ".keys", "keys must be a list")
            continue
        for i, key in enumerate(keys):
            kp = f"{path}.keys[{i}]"
            if not isinstance(key, dict):
                err(kp, "key must be an object")
                continue
            lamp = key.get("lamp")
            if not _is_int(lamp) or lamp < 0:
                err(kp + ".lamp", "lamp must be a non-negative integer")
            elif lamp in seen:
                err(kp + ".lamp", f"lamp {lamp} appears twice in this layout")
            else:
                seen.add(lamp)
            for dim in ("x", "y", "w", "h"):
                value = key.get(dim)
                if value is not None and (not isinstance(value, (int, float))
                                          or float(value) < 0):
                    err(f"{kp}.{dim}", f"{dim} must be a non-negative number")
            if "label" in key and not isinstance(key["label"], str):
                err(kp + ".label", "label must be a string")

    devices = config.get("devices") or {}
    for name, device in devices.items():
        path = f"devices.{name}"
        if not isinstance(device, dict):
            err(path, "device must be an object")
            continue
        pool = device.get("lane_pool")
        if pool is not None:
            if not isinstance(pool, list) or any(
                    not _is_int(p) or p < 0 for p in pool):
                err(path + ".lane_pool", "lane_pool must be a list of lamp indices")
            elif len(set(pool)) != len(pool):
                err(path + ".lane_pool", "lane_pool contains duplicates")
        layout = device.get("layout")
        if layout is not None and layout not in layouts:
            err(path + ".layout", f"layout {layout!r} does not exist")
        if "enabled" in device and not isinstance(device["enabled"], bool):
            err(path + ".enabled", "enabled must be true or false")
        if "label" in device and not isinstance(device["label"], str):
            err(path + ".label", "label must be a string")

    overrides = (config.get("lanes") or {}).get("overrides") or []
    if not isinstance(overrides, list):
        err("lanes.overrides", "overrides must be a list")
        overrides = []
    seen_ids: set[str] = set()
    seen_matches: dict[str, int] = {}
    for i, rule in enumerate(overrides):
        path = f"lanes.overrides[{i}]"
        if not isinstance(rule, dict):
            err(path, "override must be an object")
            continue
        rid = rule.get("id")
        if not isinstance(rid, str) or not rid.strip():
            err(path + ".id", "id must be a non-empty string")
        elif rid in seen_ids:
            err(path + ".id", f"duplicate id {rid!r}")
        else:
            seen_ids.add(rid)
        lane = rule.get("lane")
        if not _is_int(lane) or lane < 0:
            err(path + ".lane", "lane must be a non-negative integer")
        elif _is_int(count) and lane >= count:
            err(path + ".lane", f"lane must be below settings.count ({count})")
        match = rule.get("match") or {}
        if not any(isinstance(match.get(k), str) and match[k].strip()
                   for k in ("agent", "ident")):
            err(path + ".match", "match needs an agent or ident name")
        else:
            for key in ("agent", "ident"):
                value = match.get(key)
                if isinstance(value, str) and value.strip():
                    if value in seen_matches:
                        warn(path + ".match",
                             f"{key} {value!r} is matched by more than one "
                             "override; the later one wins")
                    seen_matches[value] = i

    lamp_overrides = config.get("lamp_overrides") or []
    if not isinstance(lamp_overrides, list):
        err("lamp_overrides", "lamp_overrides must be a list")
    else:
        for i, override in enumerate(lamp_overrides):
            path = f"lamp_overrides[{i}]"
            if not isinstance(override, dict):
                err(path, "override must be an object")
                continue
            if not _is_int(override.get("lamp")) or override["lamp"] < 0:
                err(path + ".lamp", "lamp must be a non-negative integer")
            if not isinstance(override.get("device"), str) or not override["device"]:
                err(path + ".device", "device must be a backend name")
            colour = override.get("color")
            if not isinstance(colour, str) or not _COLOR.match(colour):
                err(path + ".color", "color must be #rgb or #rrggbb")

    for section in ("agents", "endpoints"):
        entries = config.get(section) or []
        if not isinstance(entries, list):
            err(section, f"{section} must be a list")
            continue
        ids: set[str] = set()
        for i, entry in enumerate(entries):
            path = f"{section}[{i}]"
            if not isinstance(entry, dict):
                err(path, "entry must be an object")
                continue
            eid = entry.get("id")
            if not isinstance(eid, str) or not eid.strip():
                err(path + ".id", "id must be a non-empty string")
            elif eid in ids:
                err(path + ".id", f"duplicate id {eid!r}")
            else:
                ids.add(eid)
            if not isinstance(entry.get("enabled", True), bool):
                err(path + ".enabled", "enabled must be true or false")
            if section == "agents":
                match = entry.get("match") or {}
                if not any(isinstance(match.get(k), str) and match[k].strip()
                           for k in ("agent", "ident")):
                    err(path + ".match", "match needs an agent or ident name")
            if section == "endpoints" and not isinstance(entry.get("kind"), str):
                err(path + ".kind", "kind must be a string (openrgb, wled, ...)")

    return errors, warnings


def compile_lane_map(config: dict) -> dict[str, int]:
    """The daemon's ``lane_for`` policy, compiled from the enabled overrides.

    ``ident`` and ``agent`` both become keys, exactly like the legacy
    ``lanes.json``, so a rule can pin an individual machine or a whole harness.
    """
    out: dict[str, int] = {}
    for rule in (config.get("lanes") or {}).get("overrides") or []:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        lane = rule.get("lane")
        if not _is_int(lane) or lane < 0:
            continue
        match = rule.get("match") or {}
        for key in ("ident", "agent"):
            value = match.get(key)
            if isinstance(value, str) and value.strip():
                out[value.strip()] = lane
    return out


def lamp_overrides_for(config: dict) -> dict[str, dict[int, tuple[int, int, int]]]:
    """Device name -> {lamp index: RGB}, parsed once for the render loop."""
    out: dict[str, dict[int, tuple[int, int, int]]] = {}
    for override in config.get("lamp_overrides") or []:
        if not isinstance(override, dict):
            continue
        device = override.get("device")
        lamp = override.get("lamp")
        colour = parse_colour(override.get("color"))
        if not isinstance(device, str) or not _is_int(lamp) or colour is None:
            continue
        if override.get("mode", "static") != "static":
            continue
        out.setdefault(device, {})[lamp] = colour
    return out


def parse_colour(value: Any) -> tuple[int, int, int] | None:
    """``#abc``/``#aabbcc`` -> RGB, or None when it is not a colour."""
    if not isinstance(value, str) or not _COLOR.match(value):
        return None
    text = value[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
