"""Packaged device profiles: honest starting points, never measured facts.

A profile ships key geometry (and, when someone has measured it, a lamp map)
for a board family. Because several boards share a VID:PID, a profile match is
a *suggestion*: it is only applied when the user enables it or selects it for
the device, and every lamp it does not map stays unclaimed.

The epistemic rule, enforced by the schema and the UI:

* protocol facts and hand-authored geometry may ship;
* a claim someone else made may ship with ``assumptions`` and ``provenance``;
* an invented lamp order or position may not ship at all.

``data/*.json`` is read-only input; user edits live in the config, never here.
"""

from __future__ import annotations

import copy
import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("id"), str) else None


def all_profiles() -> list[dict]:
    """Every packaged profile, sorted by id. Small files; no caching needed."""
    out: list[dict] = []
    try:
        names = sorted(os.listdir(DATA_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        profile = _read(os.path.join(DATA_DIR, name))
        if profile:
            out.append(profile)
    return out


def load(profile_id: str) -> dict | None:
    for profile in all_profiles():
        if profile.get("id") == profile_id:
            return profile
    return None


def summaries() -> list[dict]:
    """Profile metadata for the UI: everything except the layout."""
    out = []
    for profile in all_profiles():
        info = {key: profile.get(key) for key in
                ("id", "label", "match", "source", "verified", "confidence",
                 "assumptions", "provenance", "notes")}
        layout = profile.get("layout") or {}
        info["keys"] = len(layout.get("keys") or [])
        info["lamps"] = len(layout.get("lamps") or [])
        info["zones"] = len(layout.get("zones") or [])
        out.append(info)
    return out


def match(backend: str, lamp_count: int | None = None) -> list[dict]:
    """Profiles whose match block names this backend and (when known) count."""
    out = []
    for profile in all_profiles():
        criteria = profile.get("match") or {}
        if criteria.get("backend") not in (None, backend):
            continue
        wanted = criteria.get("lamp_count")
        if lamp_count is not None and isinstance(wanted, int) and wanted != lamp_count:
            continue
        out.append(profile)
    return out


def resolve_layout(config: dict, device_name: str, backend: str,
                   lamp_count: int | None = None) -> tuple[dict | None, str | None]:
    """The effective layout for a device: (layout, origin).

    Order: the device's own layout -> an enabled/selected packaged profile's
    layout -> None. A profile is never applied silently: it must be selected on
    the device (``devices.<name>.profile``) or enabled in ``profiles``.
    """
    layouts = config.get("layouts") or {}
    device = (config.get("devices") or {}).get(device_name) or {}
    layout_id = device.get("layout")
    if isinstance(layout_id, str) and layout_id in layouts:
        return copy.deepcopy(layouts[layout_id]), f"layout:{layout_id}"

    wanted = device.get("profile")
    candidates = []
    if isinstance(wanted, str):
        found = load(wanted)
        if found:
            candidates = [found]
    else:
        for entry_id, entry in (config.get("profiles") or {}).items():
            if isinstance(entry, dict) and entry.get("enabled"):
                found = load(entry_id)
                if found:
                    candidates.append(found)
        if not candidates:
            candidates = match(backend, lamp_count)

    for profile in candidates:
        entry = (config.get("profiles") or {}).get(profile["id"]) or {}
        override = entry.get("layout") if isinstance(entry, dict) else None
        if isinstance(override, str) and override in layouts:
            return copy.deepcopy(layouts[override]), f"profile:{profile['id']}"
        layout = profile.get("layout")
        if isinstance(layout, dict) and layout.get("keys"):
            return copy.deepcopy(layout), f"profile:{profile['id']}"
    return None, None
