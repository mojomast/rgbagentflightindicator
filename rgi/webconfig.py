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
import math
import os
import re
import shutil
import time
from typing import Any

SCHEMA_VERSION = 2

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "rgi")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LEGACY_LANES_PATH = os.path.join(CONFIG_DIR, "lanes.json")
HISTORY_DIR = os.path.join(CONFIG_DIR, "history")
HISTORY_CAP = 50
BODY_MAX = 1_000_000            # bytes: a config is small; refuse surprises

STATE_NAMES = ("working", "done", "blocked", "error", "idle", "off")
PATTERNS = ("off", "steady", "blink", "breathe")
THEN = ("steady", "off")
LAMP_KINDS = ("key", "perimeter", "logo", "indicator", "accent", "unknown")
ZONE_KINDS = ("keys", "perimeter", "logo", "indicator", "accent", "custom")
SOURCES = ("firmware", "measured", "webcam", "press", "hand", "import:kle",
           "import:qmk", "import:vial", "builtin-profile", "auto", "mixed")
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
        "devices": {},          # backend name -> {label, enabled, lane_pool, layout, profile}
        "layouts": {},          # layout id -> {format_version, source, keys, lamps, zones}
        "profiles": {},         # packaged profile id -> {enabled, layout}
        "lanes": {"overrides": []},
        "lamp_overrides": [],   # [{device, lamp, mode: static, color}]
        "zone_overrides": [],   # [{device, zone, mode: static, color}]
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
    """Bring a document up to ``SCHEMA_VERSION``. Unknown newer versions refuse.

    Migration is pure-add: no field is deleted, no legacy value changes, and
    unknown fields survive. v2 splits a layout into ``keys`` (physical identity
    and geometry), ``lamps`` (addressable slots with a kind and provenance) and
    ``zones`` (ordered groups, including perimeter paths).
    """
    version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        version = SCHEMA_VERSION
    if version > SCHEMA_VERSION:
        raise ConfigTooNew(version)
    out = copy.deepcopy(raw)
    # Layouts migrate independently of the top-level version: importing a v1
    # layout into a v2 config must not leave un-migrated geometry behind.
    _migrate_v1_v2(out)
    out["schema_version"] = SCHEMA_VERSION
    return out


def _migrate_v1_v2(doc: dict) -> None:
    layouts = doc.get("layouts")
    if isinstance(layouts, dict):
        for layout in layouts.values():
            if isinstance(layout, dict):
                _migrate_layout_v1_v2(layout)
    doc.setdefault("profiles", {})


def _migrate_layout_v1_v2(layout: dict) -> None:
    version = layout.get("format_version")
    if _is_int(version) and version >= 2:
        return
    layout["format_version"] = 2
    keys = layout.get("keys")
    if isinstance(keys, list):
        lamps: list[dict] = []
        used_ids: set[str] = set()
        for i, key in enumerate(keys):
            if not isinstance(key, dict):
                continue
            kid = key.get("id")
            if not (isinstance(kid, str) and kid.strip()):
                kid = f"k{i}"
                while kid in used_ids:
                    kid += "_"
                key["id"] = kid
            used_ids.add(kid)
            if not isinstance(key.get("geometry"), dict):
                key["geometry"] = {"type": "rect",
                                   "x": key.get("x", 0), "y": key.get("y", 0),
                                   "w": key.get("w", 1), "h": key.get("h", 1)}
            lamp = key.get("lamp")
            if _is_int(lamp) and lamp >= 0:
                record: dict = {"index": lamp, "kind": "key", "key": kid}
                source = key.get("source") or layout.get("source")
                if isinstance(source, str) and source:
                    record["source"] = source
                for field in ("confidence", "px", "py"):
                    if field in key:
                        record[field] = key[field]
                lamps.append(record)
        if not isinstance(layout.get("lamps"), list):
            layout["lamps"] = lamps
    layout.setdefault("lamps", [])
    layout.setdefault("zones", [])
    layout.setdefault("canvas", {})
    layout.setdefault("meta", {})


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
    doc = migrate(doc)
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


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _validate_geometry(geometry: Any, path: str, err) -> None:
    """Rect, point, path and union shapes; coordinates may be negative."""
    if not isinstance(geometry, dict):
        err(path, "geometry must be an object")
        return
    kind = geometry.get("type")
    if kind == "rect":
        for dim in ("x", "y"):
            value = geometry.get(dim, 0)
            if not _is_number(value):
                err(f"{path}.{dim}", f"{dim} must be a finite number")
        for dim in ("w", "h"):
            value = geometry.get(dim, 1)
            if not _is_number(value) or float(value) <= 0:
                err(f"{path}.{dim}", f"{dim} must be a positive number")
    elif kind == "point":
        for dim in ("x", "y"):
            if not _is_number(geometry.get(dim)):
                err(f"{path}.{dim}", f"{dim} must be a finite number")
        size = geometry.get("size", 0.12)
        if not _is_number(size) or float(size) <= 0:
            err(f"{path}.size", "size must be a positive number")
    elif kind == "path":
        points = geometry.get("points")
        if not isinstance(points, list) or len(points) < 2:
            err(f"{path}.points", "a path needs at least two points")
        else:
            for j, point in enumerate(points):
                if (not isinstance(point, (list, tuple)) or len(point) != 2
                        or not all(_is_number(v) for v in point)):
                    err(f"{path}.points[{j}]", "point must be two finite numbers")
        width = geometry.get("width", 0.18)
        if not _is_number(width) or float(width) <= 0:
            err(f"{path}.width", "width must be a positive number")
    elif kind == "union":
        shapes = geometry.get("shapes")
        if not isinstance(shapes, list) or not shapes:
            err(f"{path}.shapes", "a union needs at least one shape")
    else:
        err(f"{path}.type", "type must be rect, point, path or union")


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

    try:
        config = migrate(config)        # validate what would actually be written
    except ConfigError as exc:
        err("", exc.message)
        return errors, warnings
    config = deep_fill(default_config(), config)   # partial bodies validate too

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
        if not _is_int(layout.get("format_version")) or layout["format_version"] > 2:
            err(path + ".format_version", "format_version must be 1 or 2")

        # -- keys: physical identity and geometry -------------------------
        keys = layout.get("keys")
        if not isinstance(keys, list):
            err(path + ".keys", "keys must be a list")
            keys = []
        if len(keys) > 512:
            err(path + ".keys", "over 512 keys is almost certainly wrong")
        key_ids: set[str] = set()
        for i, key in enumerate(keys):
            kp = f"{path}.keys[{i}]"
            if not isinstance(key, dict):
                err(kp, "key must be an object")
                continue
            kid = key.get("id")
            if not isinstance(kid, str) or not kid.strip():
                err(kp + ".id", "id must be a non-empty string")
            elif kid in key_ids:
                err(kp + ".id", f"key id {kid!r} is not unique")
            else:
                key_ids.add(kid)
            if "label" in key and not isinstance(key["label"], str):
                err(kp + ".label", "label must be a string")
            if key.get("code") is not None and not isinstance(key.get("code"), str):
                err(kp + ".code", "code must be a string or null")
            geometry = key.get("geometry")
            if geometry is not None:
                _validate_geometry(geometry, kp + ".geometry", err)
            elif not all(dim in key for dim in ("x", "y", "w", "h")):
                err(kp + ".geometry", "geometry is required (or legacy x/y/w/h)")

        # -- lamps: addressable slots -------------------------------------
        lamps = layout.get("lamps")
        if not isinstance(lamps, list):
            err(path + ".lamps", "lamps must be a list")
            lamps = []
        if len(lamps) > 4096:
            err(path + ".lamps", "over 4096 lamps is almost certainly wrong")
        lamp_indexes: set[int] = set()
        for i, lamp in enumerate(lamps):
            lp = f"{path}.lamps[{i}]"
            if not isinstance(lamp, dict):
                err(lp, "lamp must be an object")
                continue
            index = lamp.get("index")
            if not _is_int(index) or not 0 <= index <= 4095:
                err(lp + ".index", "index must be 0..4095")
            elif index in lamp_indexes:
                err(lp + ".index", f"lamp {index} appears twice in this layout")
            else:
                lamp_indexes.add(index)
            kind = lamp.get("kind")
            if kind is not None and kind not in LAMP_KINDS:
                err(lp + ".kind", "kind must be one of " + ", ".join(LAMP_KINDS))
            key_ref = lamp.get("key")
            if kind == "key" and (not isinstance(key_ref, str) or key_ref not in key_ids):
                err(lp + ".key", f"kind is key but key {key_ref!r} does not exist")
            if "present" in lamp and not isinstance(lamp["present"], bool):
                err(lp + ".present", "present must be true or false")
            source = lamp.get("source")
            if source is not None and source not in SOURCES:
                err(lp + ".source", "source must be one of " + ", ".join(SOURCES))
            confidence = lamp.get("confidence")
            if confidence is not None and (not _is_number(confidence)
                                           or not 0 <= float(confidence) <= 1):
                err(lp + ".confidence", "confidence must be between 0 and 1")
            for field in ("geometry", "pixel", "path"):
                if field in lamp and not isinstance(lamp[field], dict):
                    err(f"{lp}.{field}", f"{field} must be an object")
            path_ref = lamp.get("path")
            if isinstance(path_ref, dict):
                t = path_ref.get("t")
                if t is not None and (not _is_number(t) or not 0 <= float(t) <= 1):
                    err(lp + ".path.t", "t must be between 0 and 1")
            pixel = lamp.get("pixel")
            if isinstance(pixel, dict):
                points = pixel.get("points")
                if not isinstance(points, list):
                    err(lp + ".pixel.points", "points must be a list of [x, y]")
                else:
                    for j, point in enumerate(points):
                        if (not isinstance(point, (list, tuple)) or len(point) != 2
                                or not all(_is_number(v) for v in point)):
                            err(f"{lp}.pixel.points[{j}]", "point must be [x, y]")

        # -- zones: ordered groups, including perimeter paths -------------
        zones = layout.get("zones")
        if not isinstance(zones, list):
            err(path + ".zones", "zones must be a list")
            zones = []
        zone_ids: set[str] = set()
        for i, zone in enumerate(zones):
            zp = f"{path}.zones[{i}]"
            if not isinstance(zone, dict):
                err(zp, "zone must be an object")
                continue
            zid = zone.get("id")
            if not isinstance(zid, str) or not zid.strip():
                err(zp + ".id", "id must be a non-empty string")
            elif zid in zone_ids:
                err(zp + ".id", f"zone id {zid!r} is not unique")
            else:
                zone_ids.add(zid)
            kind = zone.get("kind")
            if kind is not None and kind not in ZONE_KINDS:
                err(zp + ".kind", "kind must be one of " + ", ".join(ZONE_KINDS))
            lamp_list = zone.get("lamps")
            if lamp_list is not None:
                if not isinstance(lamp_list, list):
                    err(zp + ".lamps", "lamps must be a list")
                else:
                    for j, lamp in enumerate(lamp_list):
                        if not _is_int(lamp) or lamp not in lamp_indexes:
                            err(f"{zp}.lamps[{j}]",
                                f"lamp {lamp!r} is not defined in lamps[]")
            geometry = zone.get("geometry")
            if geometry is not None:
                _validate_geometry(geometry, zp + ".geometry", err)
            segments = zone.get("segments")
            if segments is not None:
                if not isinstance(segments, list):
                    err(zp + ".segments", "segments must be a list")
                else:
                    seg_ids: set[str] = set()
                    spans: list[tuple[float, float]] = []
                    for j, segment in enumerate(segments):
                        sp = f"{zp}.segments[{j}]"
                        if not isinstance(segment, dict):
                            err(sp, "segment must be an object")
                            continue
                        sid = segment.get("id")
                        if not isinstance(sid, str) or not sid.strip():
                            err(sp + ".id", "id must be a non-empty string")
                        elif sid in seg_ids:
                            err(sp + ".id", f"segment id {sid!r} is not unique")
                        else:
                            seg_ids.add(sid)
                        t0, t1 = segment.get("t0"), segment.get("t1")
                        if (not _is_number(t0) or not _is_number(t1)
                                or not 0 <= float(t0) < float(t1) <= 1):
                            err(sp, "t0 and t1 must satisfy 0 <= t0 < t1 <= 1")
                        else:
                            for a, b in spans:
                                if float(t0) < b and float(t1) > a:
                                    err(sp, "segments must not overlap")
                            spans.append((float(t0), float(t1)))

        # -- cross-references ---------------------------------------------
        for i, lamp in enumerate(lamps):
            if not isinstance(lamp, dict):
                continue
            refs = [lamp.get("zone")]
            if isinstance(lamp.get("path"), dict):
                refs.append(lamp["path"].get("zone"))
            for ref in refs:
                if isinstance(ref, str) and ref and ref not in zone_ids:
                    err(f"{path}.lamps[{i}]", f"zone {ref!r} does not exist")

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
        profile = device.get("profile")
        if profile is not None and (not isinstance(profile, str) or not profile):
            err(path + ".profile", "profile must be a packaged profile id")
        zone_lanes = device.get("zone_lanes")
        if zone_lanes is not None:
            if not isinstance(zone_lanes, dict):
                err(path + ".zone_lanes", "zone_lanes must be an object")
            else:
                for zone_id, policy in zone_lanes.items():
                    lp = f"{path}.zone_lanes.{zone_id}"
                    if not isinstance(policy, dict):
                        err(lp, "policy must be an object")
                        continue
                    if policy.get("mode") not in ("segments", "urgent", "single",
                                                  "cycle", "off"):
                        err(lp + ".mode",
                            "mode must be segments, urgent, single, cycle or off")
                    slots = policy.get("slots", 12)
                    if not _is_int(slots) or not 1 <= slots <= 512:
                        err(lp + ".slots", "slots must be 1..512")

    profiles = config.get("profiles") or {}
    if not isinstance(profiles, dict):
        err("profiles", "profiles must be an object")
    else:
        for profile_id, entry in profiles.items():
            path = f"profiles.{profile_id}"
            if not isinstance(entry, dict):
                err(path, "profile entry must be an object")
                continue
            if not re.match(r"^[a-z0-9][a-z0-9._-]*$", str(profile_id)):
                err(path, "profile id must be lowercase letters, digits, . _ -")
            if "enabled" in entry and not isinstance(entry["enabled"], bool):
                err(path + ".enabled", "enabled must be true or false")
            layout = entry.get("layout")
            if layout is not None and layout not in layouts:
                err(path + ".layout", f"layout {layout!r} does not exist")

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

    zone_overrides = config.get("zone_overrides") or []
    if not isinstance(zone_overrides, list):
        err("zone_overrides", "zone_overrides must be a list")
    else:
        for i, override in enumerate(zone_overrides):
            path = f"zone_overrides[{i}]"
            if not isinstance(override, dict):
                err(path, "override must be an object")
                continue
            if not isinstance(override.get("device"), str) or not override["device"]:
                err(path + ".device", "device must be a backend name")
            if not isinstance(override.get("zone"), str) or not override["zone"]:
                err(path + ".zone", "zone must be a zone id")
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


def zone_overrides_for(config: dict) -> dict[str, dict[str, tuple[int, int, int]]]:
    """Device name -> {zone id: RGB}; a static colour for a whole strip/zone."""
    out: dict[str, dict[str, tuple[int, int, int]]] = {}
    for override in config.get("zone_overrides") or []:
        if not isinstance(override, dict):
            continue
        device = override.get("device")
        zone = override.get("zone")
        colour = parse_colour(override.get("color"))
        if not isinstance(device, str) or not isinstance(zone, str) or colour is None:
            continue
        if override.get("mode", "static") != "static":
            continue
        out.setdefault(device, {})[zone] = colour
    return out


def layout_zone_of(layout: dict | None) -> dict[int, str]:
    """Lamp index -> zone id, from both the lamp records and the zone lists."""
    out: dict[int, str] = {}
    if not isinstance(layout, dict):
        return out
    for lamp in layout.get("lamps") or []:
        if isinstance(lamp, dict) and _is_int(lamp.get("index")):
            ref = lamp.get("zone") or ((lamp.get("path") or {}).get("zone")
                                       if isinstance(lamp.get("path"), dict) else None)
            if isinstance(ref, str) and ref:
                out[lamp["index"]] = ref
    for zone in layout.get("zones") or []:
        if not isinstance(zone, dict) or not isinstance(zone.get("id"), str):
            continue
        for lamp in zone.get("lamps") or []:
            if _is_int(lamp):
                out[lamp] = zone["id"]
    return out


def layout_labels(layout: dict | None) -> dict[int, str]:
    """Lamp index -> display label, from keys and lamp records."""
    out: dict[int, str] = {}
    if not isinstance(layout, dict):
        return out
    key_labels = {}
    for key in layout.get("keys") or []:
        if isinstance(key, dict) and isinstance(key.get("id"), str):
            key_labels[key["id"]] = key.get("label") or key.get("code") or key["id"]
    for lamp in layout.get("lamps") or []:
        if not isinstance(lamp, dict) or not _is_int(lamp.get("index")):
            continue
        label = lamp.get("label")
        if not isinstance(label, str) or not label:
            label = key_labels.get(lamp.get("key"))
        if isinstance(label, str) and label:
            out[lamp["index"]] = label
    return out


def parse_colour(value: Any) -> tuple[int, int, int] | None:
    """``#abc``/``#aabbcc`` -> RGB, or None when it is not a colour."""
    if not isinstance(value, str) or not _COLOR.match(value):
        return None
    text = value[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
