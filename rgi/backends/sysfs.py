"""Linux LED-class backend - laptops, keyboards and anything the kernel exposes.

    /sys/class/leds/*/

Most laptops expose one LED for the whole keyboard (``*::kbd_backlight``), which
can only do brightness, not colour: an indicator panel needs *distinct* lamps.
Devices that expose several LEDs (some Logitech/ThinkPad/ASUS models, and any
board with a registered ``*:rgb:*`` LED) work properly. Multi-colour LEDs are
written through ``multi_intensity`` when the kernel provides it.

Because brightness is the only channel for a single-colour LED, a colour is
reduced to perceived luminance, and "needs attention" is expressed by the
daemon's blink - which needs no colour at all.
"""

from __future__ import annotations

import glob
import os
from typing import Sequence

from .base import RGB, Backend, BackendUnavailable, Lamp

LED_ROOT = "/sys/class/leds"

# anything matching these is a candidate indicator lamp
PATTERNS = ("*kbd_backlight*", "*keyboard*", "*:rgb:*", "*:rgb-*",
            "*scrolllock*", "*numlock*", "*capslock*")


def luminance(rgb: RGB) -> int:
    """Perceived brightness 0-255, used when only intensity is available."""
    r, g, b = rgb
    return min(255, int(0.2126 * r + 0.7152 * g + 0.0722 * b))


class SysfsLed:
    def __init__(self, path: str):
        self.path = path
        self.name = os.path.basename(path)
        self.max_brightness = self._read_int("max_brightness", 1)
        self.has_multi = os.path.exists(os.path.join(path, "multi_intensity"))

    def _read_int(self, leaf: str, default: int) -> int:
        try:
            with open(os.path.join(self.path, leaf), encoding="ascii") as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return default

    def set_colour(self, rgb: RGB) -> None:
        if self.has_multi:
            # multi_intensity takes "R G B" scaled to max_brightness
            scale = self.max_brightness / 255.0
            value = " ".join(str(min(self.max_brightness, int(c * scale))) for c in rgb)
            self._write("multi_intensity", value)
            self._write("brightness", str(self.max_brightness if any(rgb) else 0))
        else:
            self._write("brightness", str(int(luminance(rgb) / 255.0 * self.max_brightness)))

    def _write(self, leaf: str, value: str) -> None:
        try:
            with open(os.path.join(self.path, leaf), "w", encoding="ascii") as fh:
                fh.write(value)
        except OSError as exc:
            raise BackendUnavailable(f"cannot write {self.name}/{leaf}: {exc}") from exc


class SysfsBackend(Backend):
    name = "sysfs"
    min_interval = 0.02
    per_lamp = True

    def __init__(self, include: Sequence[str] | None = None):
        self.include = list(include or [])
        self.leds: list[SysfsLed] = []

    @classmethod
    def available(cls) -> bool:
        if not os.path.isdir(LED_ROOT):
            return False
        return bool(glob.glob(os.path.join(LED_ROOT, "*")))

    def open(self) -> None:
        if not os.path.isdir(LED_ROOT):
            raise BackendUnavailable(f"{LED_ROOT} not present (not Linux, or no LED class)")

        found: list[SysfsLed] = []
        for pattern in (self.include or PATTERNS):
            for path in sorted(glob.glob(os.path.join(LED_ROOT, pattern))):
                if os.path.basename(path) in {led.name for led in found}:
                    continue
                found.append(SysfsLed(path))
        if not found:
            raise BackendUnavailable(
                f"no LEDs matched {self.include or PATTERNS} in {LED_ROOT}"
            )
        self.leds = found

    def close(self) -> None:
        for led in self.leds:
            try:
                led.set_colour((0, 0, 0))
            except BackendUnavailable:
                pass
        self.leds = []

    def lamps(self) -> list[Lamp]:
        return [
            Lamp(index=i, label=led.name,
                 group="zone" if "rgb" in led.name else "key")
            for i, led in enumerate(self.leds)
        ]

    def write(self, colours: Sequence[RGB]) -> None:
        if not self.leds:
            raise BackendUnavailable("backend is not open")
        for led, rgb in zip(self.leds, colours):
            led.set_colour(rgb)
