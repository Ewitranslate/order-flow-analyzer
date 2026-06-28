from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class Settings:
    symbols: Sequence[str] = field(
        default_factory=lambda: ("btcusdt", "ethusdt", "solusdt")
    )
    websocket_url: str = "wss://stream.binance.com:9443/ws"
    data_path: Path = Path("./data/")

    def stream_url(self, symbol: str) -> str:
        return f"{self.websocket_url}/{symbol.lower()}@trade"

    def ensure_data_path(self) -> Path:
        self.data_path.mkdir(parents=True, exist_ok=True)
        return self.data_path


def get_settings() -> Settings:
    return Settings()

