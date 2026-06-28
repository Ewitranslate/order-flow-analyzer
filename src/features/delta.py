from __future__ import annotations

from typing import Mapping


def calculate_delta(trade: Mapping[str, object]) -> float:
    volume = float(trade["volume"])
    side = str(trade["side"]).lower()
    return volume if side == "buy" else -volume

