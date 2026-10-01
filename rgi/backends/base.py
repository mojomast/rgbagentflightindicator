"""The backend interface: one RGB device, presented as a list of lamps.

Everything device-specific lives behind this. The daemon, the renderer and the
agents only ever talk about *lamps* and RGB tuples, so supporting a new keyboard
means writing one class here and nothing else.

Two rules that came out of the hardware that started this project:

* ``write()`` is called with a colour for **every** lamp, in ``lamps()`` order.
  Most controllers cannot update a single key, and several repaint the whole
  panel on any write, so the daemon diffs the frame and calls this only when
  something actually changed.
* ``min_interval`` is a floor between writes. Firmware that cannot absorb
  back-to-back reports needs it; the Sinowealth board needs ~13 ms.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

RGB = tuple[int, int, int]
OFF: RGB = (0, 0, 0)


class BackendUnavailable(RuntimeError):
    """Device or dependency is not present. Another backend should be tried."""


@dataclass(frozen=True)
class Lamp:
    """One addressable light."""

    index: int
    label: str
    group: str = ""          # "number-row", "function-row", "zone", "key" ...
    x: float | None = None   # optional physical position, if the backend knows it
    y: float | None = None

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.index}:{self.label}"


class Backend(ABC):
    """A device we can paint."""

    name: str = "abstract"

    #: smallest sensible gap between two write() calls, in seconds
    min_interval: float = 0.0

    #: True when every lamp can hold its own colour
    per_lamp: bool = True

    @classmethod
    def available(cls) -> bool:
        """Cheap check for 'is this device/dependency here at all?'."""
        raise NotImplementedError

    @abstractmethod
    def open(self) -> None:
        """Claim the device. Raise BackendUnavailable if that fails."""

    @abstractmethod
    def close(self) -> None:
        """Release the device. Must be safe to call twice."""

    @abstractmethod
    def lamps(self) -> list[Lamp]:
        """The addressable lamps, in the order write() expects them."""

    @abstractmethod
    def write(self, colours: Sequence[RGB]) -> None:
        """Paint every lamp. len(colours) must equal len(lamps())."""

    # -- helpers with sensible defaults -----------------------------------
    def default_lanes(self, count: int) -> list[int]:
        """Pick which lamps should carry session lanes, best first.

        Prefers a labelled group (the number row on a keyboard), then anything
        else the device has. Devices with zones usually label those instead.
        """
        lamps = self.lamps()
        preferred = [l.index for l in lamps if l.group in ("number-row", "zone")]
        rest = [l.index for l in lamps if l.index not in preferred]
        return (preferred + rest)[:count]

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "lamps": len(self.lamps()),
            "per_lamp": self.per_lamp,
            "min_interval": self.min_interval,
        }

    def __enter__(self) -> "Backend":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def fill(colour: RGB, count: int) -> list[RGB]:
    return [colour] * count
