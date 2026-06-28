"""Binance USDT-M perpetual: symbols list and klines (fapi) — без Streamlit."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd


FAPI_BASE = "https://fapi.binance.com"


def fetch_usdt_perpetual_symbols() -> list[str]:
    url = f"{FAPI_BASE}/fapi/v1/exchangeInfo"
    req = urllib.request.Request(url, headers={"User-Agent": "orderflow-reversal/1"})
    with urllib.request.urlopen(req, timeout=35) as resp:
        data = json.loads(resp.read().decode())
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


def fetch_futures_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    """`GET /fapi/v1/klines` — те же поля, что spot klines."""
    sym = symbol.upper().replace("/", "")
    lim = max(10, min(1500, int(limit)))
    q = urllib.parse.urlencode({"symbol": sym, "interval": interval, "limit": str(lim)})
    url = f"{FAPI_BASE}/fapi/v1/klines?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "orderflow-reversal/1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode())
    rows = []
    for k in raw:
        vol = float(k[5])
        taker_buy = float(k[9]) if len(k) > 9 else 0.0
        rows.append(
            {
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume_base": vol,
                "taker_buy_base": taker_buy,
            }
        )
    return pd.DataFrame(rows)


def fetch_futures_24hr_quote_volume() -> dict[str, float]:
    """`GET /fapi/v1/ticker/24hr` — quoteVolume по символу (один запрос на все пары)."""
    url = f"{FAPI_BASE}/fapi/v1/ticker/24hr"
    req = urllib.request.Request(url, headers={"User-Agent": "orderflow-reversal/1"})
    with urllib.request.urlopen(req, timeout=35) as resp:
        raw = json.loads(resp.read().decode())
    out: dict[str, float] = {}
    if not isinstance(raw, list):
        return out
    for row in raw:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).upper()
        if not sym.endswith("USDT"):
            continue
        try:
            out[sym] = float(row.get("quoteVolume", 0) or 0)
        except (TypeError, ValueError):
            out[sym] = 0.0
    return out


def fetch_open_interest_hist(symbol: str, period: str, limit: int = 5) -> pd.DataFrame:
    """`GET /futures/data/openInterestHist` — без Streamlit (для проверки наличия OI)."""
    sym = symbol.upper().replace("/", "")
    lim = max(5, min(500, int(limit)))
    q = urllib.parse.urlencode({"symbol": sym, "period": period, "limit": str(lim)})
    url = f"{FAPI_BASE}/futures/data/openInterestHist?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "orderflow-reversal/1"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = json.loads(resp.read().decode())
    if not isinstance(raw, list) or not raw:
        return pd.DataFrame()
    rows = []
    for row in raw:
        if not isinstance(row, dict):
            continue
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
        period, rebuild=rebuild, max_workers=max(8, int(max_workers))
    )
    return syms
