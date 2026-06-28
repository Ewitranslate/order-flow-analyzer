from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Dict, Optional

import websockets
from src.config.settings import Settings, get_settings
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Trade:
    timestamp: int
    price: float
    volume: float
    side: str  # "buy" | "sell"

    def as_dict(self) -> Dict[str, object]:
        return {
            "timestamp": int(self.timestamp),
            "price": float(self.price),
            "volume": float(self.volume),
            "side": str(self.side),
        }


class BinanceTradeWebSocketClient:
    """
    Streams normalized trades from Binance `{symbol}@trade`.
    """

    def __init__(
        self,
        symbol: str,
        *,
        settings: Optional[Settings] = None,
        ping_interval: float = 20.0,
        ping_timeout: float = 20.0,
        reconnect_delay: float = 2.0,
        max_reconnect_delay: float = 30.0,
    ) -> None:
        self.symbol = symbol.lower()
        self.settings = settings or get_settings()
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay

        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _normalize_trade(self, raw: dict) -> Trade:
        # Binance trade payload fields:
        #  p: price (string), q: qty (string), T: trade time (ms), m: isBuyerMaker (bool)
        price = float(raw["p"])
        volume = float(raw["q"])
        timestamp = int(raw["T"])
        is_buyer_maker = bool(raw["m"])
        side = "sell" if is_buyer_maker else "buy"
        return Trade(timestamp=timestamp, price=price, volume=volume, side=side)

    async def trades(self) -> AsyncIterator[Dict[str, object]]:
        """
        Async iterator of normalized trades dict:
        { timestamp, price, volume, side }
        """
        url = self.settings.stream_url(self.symbol)
        backoff = self.reconnect_delay

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    url,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                    close_timeout=5,
                ) as ws:
                    logger.info("WS connected: %s", url)
                    backoff = self.reconnect_delay
                    async for msg in ws:
                        if self._stop_event.is_set():
                            break
                        raw = json.loads(msg)
                        trade = self._normalize_trade(raw)
                        yield trade.as_dict()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.warning("WS error for %s: %s (reconnect in %.1fs)", self.symbol, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(self.max_reconnect_delay, backoff * 1.6)

