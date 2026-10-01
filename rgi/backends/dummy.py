"""A backend with no hardware, for tests, demos and dry runs.

    rgi daemon --backend dummy --verbose

It records every frame it is given, so the tests can assert on exactly what
would have been sent to a real device, and `--verbose` prints each change.
"""

from __future__ import annotations

from typing import Sequence

from .base import RGB, Backend, Lamp


class DummyBackend(Backend):
    name = "dummy"
    min_interval = 0.0

    def __init__(self, count: int = 12, verbose: bool = False, groups: str = "number-row"):
        self.count = count
        self.verbose = verbose
        self.groups = groups
        self.frames: list[list[RGB]] = []
        self.opened = False

    @classmethod
    def available(cls) -> bool:
        return True

    def open(self) -> None:
        self.opened = True
        if self.verbose:
            print(f"[dummy] opened with {self.count} lamps")

    def close(self) -> None:
        self.opened = False

    def lamps(self) -> list[Lamp]:
        labels = ["`", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "="]
        out = []
        for i in range(self.count):
            label = labels[i] if i < len(labels) else f"lamp{i}"
            out.append(Lamp(index=i, label=label, group=self.groups))
        return out

    def write(self, colours: Sequence[RGB]) -> None:
        frame = list(colours)
        self.frames.append(frame)
        if self.verbose:
            lit = " ".join(
                f"{self.lamps()[i].label}={c}" for i, c in enumerate(frame) if c != (0, 0, 0)
            )
            print(f"[dummy] frame {len(self.frames):>4}: {lit or '(all off)'}")
