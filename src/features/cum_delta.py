from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class CumulativeDelta:
    value: float = 0.0
    history: List[float] = field(default_factory=list)

    def update(self, delta: float) -> float:
        self.value += float(delta)
        self.history.append(self.value)
        return self.value

    def current(self) -> float:
        return float(self.value)

