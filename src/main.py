from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import Optional

from src.config.settings import Settings, get_settings
from src.data.storage import CsvTradeStorage
from src.data.websocket_client import BinanceTradeWebSocketClient
from src.features.cum_delta import CumulativeDelta
from src.features.delta import calculate_delta
from src.utils.logger import get_logger, setup_logger

logger = get_logger(__name__)


@dataclass
class SymbolPipeline:
    symbol: str
    settings: Settings
    storage: CsvTradeStorage
    cum_delta: CumulativeDelta

    async def run(self) -> None:
        ws = BinanceTradeWebSocketClient(self.symbol, settings=self.settings)
        trades_seen = 0

        try:
            async for trade in ws.trades():
                d = calculate_delta(trade)
                cd = self.cum_delta.update(d)

                # Persist only the trade (as requested). Cum-delta can be rebuilt from CSV.
                self.storage.append_trade(self.symbol, trade)

                trades_seen += 1
                if trades_seen % 200 == 0:
                    logger.info(
                        "%s: trades=%d last_price=%.2f cum_delta=%.4f",
                        self.symbol,
                        trades_seen,
                        float(trade["price"]),
                        cd,
                    )
        except asyncio.CancelledError:
            raise


async def _run_all(settings: Settings) -> None:
    settings.ensure_data_path()
    storage = CsvTradeStorage(settings=settings)

    tasks = []
    for sym in settings.symbols:
        pipeline = SymbolPipeline(
            symbol=sym,
            settings=settings,
            storage=storage,
            cum_delta=CumulativeDelta(),
        )
        tasks.append(asyncio.create_task(pipeline.run(), name=f"pipeline:{sym}"))

    await asyncio.gather(*tasks)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    stop = asyncio.Event()

    def _request_stop() -> None:
        logger.info("Stop requested (Ctrl+C). Cancelling tasks...")
        stop.set()

    try:
        loop.add_signal_handler(signal.SIGINT, _request_stop)
        loop.add_signal_handler(signal.SIGTERM, _request_stop)
    except NotImplementedError:
        # Windows or restricted env
        pass

    async def _watch_stop() -> None:
        await stop.wait()
        for t in asyncio.all_tasks(loop=loop):
            if t is not asyncio.current_task(loop=loop):
                t.cancel()

    asyncio.create_task(_watch_stop())


def main() -> None:
    setup_logger()
    settings = get_settings()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _install_signal_handlers(loop)
    try:
        loop.run_until_complete(_run_all(settings))
    finally:
        loop.close()


if __name__ == "__main__":
    main()

