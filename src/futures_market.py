"""Binance USDT-M perpetual: symbols list and klines (fapi) — без Streamlit."""

from __future__ import annotations

import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from binance_http import (
    fetch_futures_24hr_quote_volume as _fetch_futures_24hr_quote_volume,
    fetch_futures_exchange_info,
    fetch_futures_klines as _fetch_futures_klines_impl,
    fetch_futures_open_interest_hist as _fetch_futures_open_interest_hist_raw,
)


def fetch_futures_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    return _fetch_futures_klines_impl(symbol, interval, limit)


def fetch_usdt_perpetual_symbols() -> list[str]:
    data = fetch_futures_exchange_info()
    out: list[str] = []
    for s in data.get("symbols", []):
        if str(s.get("contractType", "")).upper() != "PERPETUAL":
            continue
        if str(s.get("quoteAsset", "")).upper() != "USDT":
            continue
        if str(s.get("status", "")).upper() != "TRADING":
            continue
        sym = str(s.get("symbol", "")).upper()
        if sym:
            out.append(sym)
    return sorted(set(out))


def fetch_futures_24hr_quote_volume() -> dict[str, float]:
    """`GET /fapi/v1/ticker/24hr` — quoteVolume по символу (один запрос на все пары)."""
    return _fetch_futures_24hr_quote_volume()


def fetch_open_interest_hist(symbol: str, period: str, limit: int = 5) -> pd.DataFrame:
    """`GET /futures/data/openInterestHist` — без Streamlit (для проверки наличия OI)."""
    raw = _fetch_futures_open_interest_hist_raw(symbol, period, limit)
    if not raw:
        return pd.DataFrame()
    rows = []
    for row in raw:
        try:
            ts = int(row["timestamp"])
            oi = float(row.get("sumOpenInterest", 0) or 0)
        except (TypeError, ValueError, KeyError):
            continue
        rows.append({"open_time": ts, "open_interest": oi})
    return pd.DataFrame(rows)


def _symbol_has_oi(sym: str, period: str) -> str | None:
    try:
        df = fetch_open_interest_hist(sym, period, 5)
        if df is not None and len(df) > 0:
            return sym.upper()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def list_symbols_with_open_interest(
    period: str,
    *,
    max_workers: int = 8,
    rebuild: bool = False,
) -> list[str]:
    """
    USDT perpetual с историей OI. По умолчанию — быстрый путь (файловый кэш + топ по объёму).
    `rebuild=True` — принудительная пересборка.
    """
    from oi_symbol_cache import list_symbols_with_open_interest_fast

    syms, _ = list_symbols_with_open_interest_fast(
        period, rebuild=rebuild, max_workers=None
    )
    return syms
