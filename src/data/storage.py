from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

from src.config.settings import Settings, get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


FIELDNAMES = ("timestamp", "price", "volume", "side")


@dataclass
class CsvTradeStorage:
    """
    Appends normalized trades to per-symbol CSV files.
    """

    settings: Settings = get_settings()
    suffix: str = "_trades.csv"

    def __post_init__(self) -> None:
        self.settings.ensure_data_path()
        self._paths: Dict[str, Path] = {}

    def _path_for(self, symbol: str) -> Path:
        symbol = symbol.lower()
        if symbol not in self._paths:
            self._paths[symbol] = self.settings.data_path / f"{symbol}{self.suffix}"
        return self._paths[symbol]

    def append_trade(self, symbol: str, trade: Dict[str, object]) -> None:
        path = self._path_for(symbol)
        exists = path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)

        # "Safe writing" here = atomic append with flush+fsync to reduce data loss.
        with path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if not exists:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp": int(trade["timestamp"]),
                    "price": float(trade["price"]),
                    "volume": float(trade["volume"]),
                    "side": str(trade["side"]),
                }
            )
            f.flush()
            os.fsync(f.fileno())

