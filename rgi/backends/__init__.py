"""Backend registry.

Adding a keyboard means writing one Backend subclass and listing it here.
`rgi detect` walks these in order and reports what it finds.
"""

from __future__ import annotations

from .base import OFF, RGB, Backend, BackendUnavailable, Lamp, fill

__all__ = ["OFF", "RGB", "Backend", "BackendUnavailable", "Lamp", "fill",
           "available_backends", "load", "auto_backends", "opt_in_backends",
           "REGISTRY"]

# Backends that must be named explicitly with --backend, never picked by
# auto-detection. A driver that talks to a board it has not been confirmed
# against can blank it or interfere with typing, and "it is plugged in" is not
# consent for that. See docs/backends.md.
#
# Empty at the moment: sinowealth was opt-in while its mode entry was
# unconfirmed (2026-10-01), and graduated back to auto once the vendor's game
# commit was recovered and verified on hardware (2026-10-02). The machinery
# stays so the next unknown board gets the same caution.
OPT_IN = frozenset()


def _registry():
    """Imported lazily so a missing optional dependency cannot break the rest."""
    from .dummy import DummyBackend
    from .openrgb import OpenRGBBackend
    from .sinowealth import SinowealthBackend
    from .sysfs import SysfsBackend
    from .wled import WledBackend

    out = {
        "dummy": DummyBackend,
        "openrgb": OpenRGBBackend,
        "sinowealth": SinowealthBackend,
        # a configured WLED strip is an explicit choice for a pixel panel, so it
        # beats the generic sysfs fallback (true whenever any LED-class device
        # exists) but not a real keyboard detected by its own protocol
        "wled": WledBackend,
        "sysfs": SysfsBackend,
    }
    try:                                    # optional, see docs/backends.md
        from .lamparray import LampArrayBackend
        out["lamparray"] = LampArrayBackend
    except Exception:                       # pragma: no cover - optional module
        pass
    try:
        from .evision import EvisionBackend
        out["evision"] = EvisionBackend
    except Exception:                       # pragma: no cover - optional module
        pass
    try:
        from .qmk import QmkBackend
        out["qmk"] = QmkBackend
    except Exception:                       # pragma: no cover - optional module
        pass
    return out


REGISTRY = None


def load(name: str) -> type[Backend]:
    global REGISTRY
    if REGISTRY is None:
        REGISTRY = _registry()
    try:
        return REGISTRY[name]
    except KeyError:
        raise SystemExit(f"unknown backend {name!r}; try one of: {', '.join(sorted(REGISTRY))}")


def available_backends() -> list[tuple[str, bool]]:
    """(name, available) for every known backend, in detection order."""
    global REGISTRY
    if REGISTRY is None:
        REGISTRY = _registry()
    out = []
    for name, cls in REGISTRY.items():
        try:
            ok = bool(cls.available())
        except Exception:
            ok = False
        out.append((name, ok))
    return out


def opt_in_backends() -> list[str]:
    """Present, but never opened unless asked for by name."""
    return [name for name, ok in available_backends() if ok and name in OPT_IN]


def auto_backends() -> list[str]:
    """What auto-detection may open: available, minus opt-in and the dummy."""
    return [name for name, ok in available_backends()
            if ok and name not in OPT_IN and name != "dummy"]
