"""HTTP-клиент Binance Spot: резервные эндпоинты и прокси (Render / geo-block)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import pandas as pd

# data-api.binance.vision — публичные рыночные данные, часто доступен при блокировке api.binance.com
SPOT_API_BASES: tuple[str, ...] = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)

FAPI_BASE = os.environ.get("BINANCE_FAPI_BASE", "https://fapi.binance.com").rstrip("/")

_USER_AGENT = "orderflow-analyzer/1"
_LAST_ERROR: str = ""


def last_binance_error() -> str:
    return _LAST_ERROR


def _set_error(msg: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = (msg or "").strip()


def _proxy_opener() -> urllib.request.OpenerDirector:
    proxy = (
        os.environ.get("BINANCE_HTTP_PROXY", "").strip()
        or os.environ.get("HTTPS_PROXY", "").strip()
        or os.environ.get("HTTP_PROXY", "").strip()
    )
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def http_get_json(url: str, *, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with _proxy_opener().open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _parse_klines_rows(raw: list) -> pd.DataFrame:
    rows = []
    for k in raw:
        if not isinstance(k, (list, tuple)) or len(k) < 6:
            continue
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


def fetch_spot_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    """Spot klines с перебором зеркал API."""
    sym = symbol.upper().replace("/", "")
    lim = max(10, min(1000, int(limit)))
    q = urllib.parse.urlencode({"symbol": sym, "interval": interval, "limit": str(lim)})
    errors: list[str] = []

    for base in SPOT_API_BASES:
        url = f"{base.rstrip('/')}/api/v3/klines?{q}"
        try:
            raw = http_get_json(url, timeout=28.0)
            if not isinstance(raw, list) or not raw:
                errors.append(f"{base}: пустой ответ")
                continue
            df = _parse_klines_rows(raw)
            if df.empty:
                errors.append(f"{base}: нет строк")
                continue
            _set_error("")
            return df
        except urllib.error.HTTPError as exc:
            errors.append(f"{base}: HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            errors.append(f"{base}: {exc}")

    msg = "; ".join(errors[:4]) if errors else "все spot-эндпоинты недоступны"
    _set_error(msg)
    raise urllib.error.URLError(msg)


def fetch_spot_exchange_info() -> dict[str, Any]:
    errors: list[str] = []
    for base in SPOT_API_BASES:
        url = f"{base.rstrip('/')}/api/v3/exchangeInfo"
        try:
            raw = http_get_json(url, timeout=35.0)
            if isinstance(raw, dict):
                _set_error("")
                return raw
            errors.append(f"{base}: неверный JSON")
        except urllib.error.HTTPError as exc:
            errors.append(f"{base}: HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{base}: {exc}")
    msg = "; ".join(errors[:4]) if errors else "exchangeInfo недоступен"
    _set_error(msg)
    raise urllib.error.URLError(msg)


def fetch_spot_24h_quote_volume() -> dict[str, float]:
    """Spot 24h ticker — quoteVolume по символу (один запрос на все пары)."""
    errors: list[str] = []
    for base in SPOT_API_BASES:
        url = f"{base.rstrip('/')}/api/v3/ticker/24hr"
        try:
            raw = http_get_json(url, timeout=35.0)
            out: dict[str, float] = {}
            if not isinstance(raw, list):
                errors.append(f"{base}: неверный JSON")
                continue
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
            _set_error("")
            return out
        except urllib.error.HTTPError as exc:
            errors.append(f"{base}: HTTP {exc.code}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{base}: {exc}")
    msg = "; ".join(errors[:4]) if errors else "ticker/24hr недоступен"
    _set_error(msg)
    raise urllib.error.URLError(msg)


def fetch_futures_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    sym = symbol.upper().replace("/", "")
    lim = max(10, min(1500, int(limit)))
    q = urllib.parse.urlencode({"symbol": sym, "interval": interval, "limit": str(lim)})
    url = f"{FAPI_BASE}/fapi/v1/klines?{q}"
    try:
        raw = http_get_json(url, timeout=30.0)
        if not isinstance(raw, list) or not raw:
            raise ValueError("пустой ответ fapi klines")
        df = _parse_klines_rows(raw)
        _set_error("")
        return df
    except urllib.error.HTTPError as exc:
        _set_error(f"fapi: HTTP {exc.code}")
        raise
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
        _set_error(f"fapi: {exc}")
        raise
