"""Polling interval with jitter and exponential back-off."""

from __future__ import annotations

import random
from typing import Callable, Optional


class Backoff:
    """10 s (+1-3 s jitter) normally; doubles on each consecutive failure up to a cap.

    failures=0 -> 10 s, 1 -> 20 s, 2 -> 40 s, 3 -> 80 s, 4+ -> 120 s (cap, jitter included).
    One successful check resets it to the base interval.
    """

    def __init__(self, base_s: float = 10, jitter_min_s: float = 1, jitter_max_s: float = 3,
                 max_s: float = 120, rng: Optional[Callable[[float, float], float]] = None) -> None:
        self.base_s = base_s
        self.jitter_min_s = jitter_min_s
        self.jitter_max_s = jitter_max_s
        self.max_s = max_s
        self.failures = 0
        self._rng = rng or random.uniform

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> None:
        self.failures += 1

    @property
    def interval_s(self) -> float:
        return min(self.base_s * (2 ** self.failures), self.max_s)

    def next_delay(self) -> float:
        return min(self.interval_s + self._rng(self.jitter_min_s, self.jitter_max_s), self.max_s)
