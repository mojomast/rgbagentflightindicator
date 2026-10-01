"""Backend registry.

Adding a keyboard means writing one Backend subclass and listing it here.
`rgi detect` walks these in order and reports what it finds.
"""

from __future__ import annotations

from .base import OFF, RGB, Backend, BackendUnavailable, Lamp, fill

__all__ = ["OFF", "RGB", "Backend", "BackendUnavailable", "Lamp", "fill",
           "available_backends", "load", "REGISTRY"]


def _registry():
    """Imported lazily so a missing optional dependency cannot break the rest."""
    from .dummy import DummyBackend
    from .openrgb import OpenRGBBackend
    from .sinowealth import SinowealthBackend
    from .sysfs import SysfsBackend

    out = {
        "dummy": DummyBackend,
        "openrgb": OpenRGBBackend,
        "sinowealth": SinowealthBackend,
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
