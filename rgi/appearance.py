"""Appearance: turning the config's states into LED colours.

The same bounded vocabulary renders in two places - here for the hardware
frames, and in ``rgi/webui/lib/effects.js`` for the browser preview - and both
are checked against ``rgi/webui/tests/fixtures/effects.json``. If preview and
board could disagree, the preview would be a lie.

The vocabulary is deliberately tiny: ``off | steady | blink | breathe`` with a
period, a duty, a cycle count and a "then". No expressions, no scripting: both
renderers stay verifiable by reading the same numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, pi

from .backends import OFF, RGB
from .webconfig import (PATTERNS, STATE_NAMES, THEN, default_states,
                        lamp_overrides_for, parse_colour, zone_overrides_for)


def _scale(colour: RGB, brightness: int) -> RGB:
    if brightness >= 255:
        return (colour[0], colour[1], colour[2])
    if brightness <= 0:
        return OFF
    factor = brightness / 255.0
    # round-half-up, not Python's round(): the JS mirror uses Math.round and the
    # cross-language fixture test must agree exactly.
    return (max(0, min(255, int(colour[0] * factor + 0.5))),
            max(0, min(255, int(colour[1] * factor + 0.5))),
            max(0, min(255, int(colour[2] * factor + 0.5))))


def _int(value, fallback: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return max(low, min(high, int(value)))


def _float(value, fallback: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return max(low, min(high, float(value)))


@dataclass(frozen=True)
class StateSpec:
    """One state's appearance. ``cycles == 0`` means blink or breathe forever."""

    color: RGB = OFF
    pattern: str = "steady"
    period_ms: int = 560
    duty: float = 0.5
    cycles: int = 0
    then: str = "steady"
    brightness: int = 255

    def render(self, elapsed_s: float, quiet: bool = False) -> RGB:
        base = _scale(self.color, self.brightness)
        if self.pattern == "off" or base == OFF:
            return OFF
        if quiet or self.pattern == "steady":
            # While the human types the frame holds a steady colour: no writes,
            # and nothing to gain from computing a phase nobody will see.
            return base
        period = max(0.04, self.period_ms / 1000.0)
        if self.cycles and elapsed_s >= self.cycles * period:
            return base if self.then == "steady" else OFF
        phase = (max(0.0, elapsed_s) % period) / period
        if self.pattern == "blink":
            return base if phase < max(0.0, min(1.0, self.duty)) else OFF
        if self.pattern == "breathe":
            level = 0.5 - 0.5 * cos(2.0 * pi * phase)
            return _scale(base, max(1, int(level * 255 + 0.5)))
        return base


class Appearance:
    """The compiled palette and effects for every state, plus lamp overrides."""

    def __init__(self, states: dict[str, StateSpec] | None = None,
                 overrides: dict[str, dict[int, RGB]] | None = None,
                 zone_overrides: dict[str, dict[str, RGB]] | None = None):
        self.states = dict(states or {})
        self.overrides = overrides or {}
        self.zone_overrides = zone_overrides or {}

    @classmethod
    def default(cls) -> "Appearance":
        return cls.from_config({"appearance": {"states": default_states()}})

    @classmethod
    def from_config(cls, config: dict) -> "Appearance":
        """Compile the config tolerantly: the HTTP layer validates, but a hand
        edited file must never stop the panel from rendering."""
        appearance = (config or {}).get("appearance") or {}
        blink = appearance.get("blink") or {}
        global_period = _int(blink.get("period_ms"), 560, 40, 10000)
        global_duty = _float(blink.get("duty"), 0.5, 0.0, 1.0)
        defaults = default_states()
        states: dict[str, StateSpec] = {}
        for name in STATE_NAMES:
            raw = (appearance.get("states") or {}).get(name)
            if not isinstance(raw, dict):
                raw = defaults.get(name, {})
            pattern = raw.get("pattern")
            colour = parse_colour(raw.get("color"))
            states[name] = StateSpec(
                color=colour if colour is not None else OFF,
                pattern=pattern if pattern in PATTERNS else "steady",
                period_ms=_int(raw.get("period_ms"), global_period, 40, 10000),
                duty=_float(raw.get("duty"), global_duty, 0.0, 1.0),
                cycles=_int(raw.get("cycles"), 0, 0, 1_000_000),
                then=raw.get("then") if raw.get("then") in THEN else "steady",
                brightness=_int(raw.get("brightness"), 255, 0, 255),
            )
        return cls(states, lamp_overrides_for(config or {}),
                   zone_overrides_for(config or {}))

    def spec(self, state: str) -> StateSpec:
        return self.states.get(state) or self.states.get("idle") or StateSpec()

    def render(self, state: str, elapsed_s: float, quiet: bool = False) -> RGB:
        return self.spec(state).render(elapsed_s, quiet)

    def lamp_override(self, device: str, lamp: int) -> RGB | None:
        """A static colour for one lamp, above every lane's state."""
        return (self.overrides.get(device) or {}).get(lamp)

    def zone_override(self, device: str, zone: str) -> RGB | None:
        """A static colour for a whole strip/zone; a lamp override wins."""
        return (self.zone_overrides.get(device) or {}).get(zone)
