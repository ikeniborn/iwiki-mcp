"""Bounded retry timing and transient bot liveness state."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Backoff:
    initial: float = 1.0
    maximum: float = 30.0
    jitter_ratio: float = 0.2
    _attempt: int = 0

    def next_delay(self, random_value: float) -> float:
        base = min(self.maximum, self.initial * (2 ** self._attempt))
        self._attempt += 1
        return base * (
            1 - self.jitter_ratio + 2 * self.jitter_ratio * random_value
        )

    def reset(self) -> None:
        self._attempt = 0


class Heartbeat:
    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self._path = path
        self._clock = clock

    def touch(self) -> None:
        self._path.write_text(f"{self._clock():.6f}\n", encoding="ascii")
