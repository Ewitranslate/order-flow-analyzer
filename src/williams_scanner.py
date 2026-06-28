"""
Сканер Williams %R + EMA: EMA за границами −20 (перекуп) и −80 (перепрод).

Логика как на графике (williams_r.py / Pine Willy_mid_TRI):
  out2 = EMA(Williams %R, ema_len)
  перекуп: out2 > −20
  перепрод: out2 < −80
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Iterable, Literal

import numpy as np
import pandas as pd

from app import PriceCumDeltaDivergence, detect_price_vs_cum_divergences, fetch_futures_open_interest_hist
from williams_r import williams_percent_r

WilliamsZone = Literal["overbought", "oversold"]
HitZone = Literal["overbought", "oversold", "neutral"]

SCANNER_TF_API: dict[str, str] = {
    "5m": "5m",
    "15m": "15m",
    "2h": "2h",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

SCANNER_TF_LABELS_RU: dict[str, str] = {
    "5m": "5 минут",
    "15m": "15 минут",
    "2h": "2 часа",
    "1h": "1 час",
    "4h": "4 часа",
    "1d": "1 день",
}

# Длительность одной свечи — для автоскана «по таймфрейму».
SCANNER_TF_TIMEDELTA: dict[str, timedelta] = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}

AutoRefreshMode = Literal["fixed_5m", "by_tf"]


def scanner_auto_refresh_interval(
    tf_keys: list[str],
    mode: AutoRefreshMode,
    *,
    fixed: timedelta = timedelta(minutes=5),
) -> timedelta:
    """
    Интервал автоскана.
    `by_tf`: минимальный интервал среди выбранных ТФ (чтобы обновляться при закрытии
    самой «быстрой» свечи).
    """
    if mode != "by_tf":
        return fixed
    deltas = [SCANNER_TF_TIMEDELTA[k] for k in tf_keys if k in SCANNER_TF_TIMEDELTA]
    if not deltas:
        return fixed
    return min(deltas)


def format_timedelta_ru(td: timedelta) -> str:
    total_sec = max(0, int(td.total_seconds()))
    if total_sec >= 86_400 and total_sec % 86_400 == 0:
        d = total_sec // 86_400
        return f"{d} д" if d == 1 else f"{d} дн"
    if total_sec >= 3600 and total_sec % 3600 == 0:
        h = total_sec // 3600
        return f"{h} ч"
    if total_sec >= 60 and total_sec % 60 == 0:
        m = total_sec // 60
        return f"{m} мин"
    return f"{total_sec} с"

ZONE_LABEL_RU: dict[HitZone, str] = {
    "overbought": "Перекуп (EMA > −20)",
    "oversold": "Перепрод (EMA < −80)",
    "neutral": "Вне зон (−80…−20)",
}

OVERBOUGHT_LEVEL = -20.0
OVERSOLD_LEVEL = -80.0

DivKind = Literal["bearish", "bullish"]

MAFilterMode = Literal["none", "above", "below"]
CumDelta24hFilterMode = Literal["none", "up", "down"]
Oi24hFilterMode = Literal["none", "up", "down"]

CUM_DELTA_24H_INTERVAL = "15m"
CUM_DELTA_24H_KLINES_LIMIT = 100  # ~25 ч на 15m

OI_24H_INTERVAL = "15m"
OI_24H_LIMIT = 100

CUM_DELTA_24H_LABEL_RU: dict[CumDelta24hFilterMode, str] = {
    "none": "Не фильтровать",
    "up": "Рост кум. δ",
    "down": "Падение кум. δ",
}

OI_24H_LABEL_RU: dict[Oi24hFilterMode, str] = {
    "none": "Не фильтровать",
    "up": "Рост OI",
    "down": "Падение OI",
}

DIV_LABEL_RU: dict[DivKind, str] = {
    "bearish": "Медвежья (HH цены + LH кум. δ)",
    "bullish": "Бычья (LL цены + HL кум. δ)",
}


@dataclass(frozen=True)
class ScannerSearchCriteria:
    """Какие критерии участвуют в поиске (логика AND между отмеченными)."""

    use_williams: bool = True
    williams_zones: frozenset[WilliamsZone] = frozenset({"overbought", "oversold"})
    use_sma: bool = False
    sma_filter_mode: MAFilterMode = "none"
    use_cum_delta_24h: bool = False
    cum_delta_24h_filter: CumDelta24hFilterMode = "none"
    use_oi_24h: bool = False
    oi_24h_filter: Oi24hFilterMode = "none"
    use_divergence: bool = False
    div_kinds: frozenset[DivKind] = frozenset({"bearish", "bullish"})
    compute_divergence: bool = True

    def any_active(self) -> bool:
        return bool(
            self.use_williams
            or self.use_sma
            or self.use_cum_delta_24h
            or self.use_oi_24h
            or self.use_divergence
        )


def search_criteria_summary_ru(criteria: ScannerSearchCriteria) -> str:
    parts: list[str] = []
    if criteria.use_williams and criteria.williams_zones:
        parts.append("Williams: " + ", ".join(ZONE_LABEL_RU.get(z, z) for z in sorted(criteria.williams_zones)))
    if criteria.use_sma and criteria.sma_filter_mode != "none":
        lbl = {"above": "цена выше SMA", "below": "цена ниже SMA"}.get(criteria.sma_filter_mode, criteria.sma_filter_mode)
        parts.append(f"SMA ({lbl})")
    if criteria.use_cum_delta_24h and criteria.cum_delta_24h_filter != "none":
        parts.append(CUM_DELTA_24H_LABEL_RU.get(criteria.cum_delta_24h_filter, criteria.cum_delta_24h_filter))
    if criteria.use_oi_24h and criteria.oi_24h_filter != "none":
        parts.append(OI_24H_LABEL_RU.get(criteria.oi_24h_filter, criteria.oi_24h_filter))
    if criteria.use_divergence:
        if len(criteria.div_kinds) == 1:
            k = next(iter(criteria.div_kinds))
            parts.append(f"δ {DIV_LABEL_RU.get(k, k)}")
        else:
            parts.append("δ любая")
    return " · ".join(parts) if parts else "критерии не выбраны"


def passes_sma_filter(hit: WilliamsHit, mode: MAFilterMode) -> bool:
    if mode == "none":
        return True
    close = float(hit.close)
    sma = float(hit.close_sma)
    if not np.isfinite(close) or not np.isfinite(sma):
        return False
    if mode == "above":
        return close > sma
    if mode == "below":
        return close < sma
    return True


def passes_divergence_filter(hit: WilliamsHit, criteria: ScannerSearchCriteria) -> bool:
    if not criteria.use_divergence:
        return True
    if hit.div_kind is None:
        return False
    return hit.div_kind in criteria.div_kinds


def passes_hit_search_criteria(
    hit: WilliamsHit,
    cd24: float | None,
    oi24: float | None,
    criteria: ScannerSearchCriteria,
) -> bool:
    if not criteria.any_active():
        return False
    if criteria.use_williams:
        if hit.zone not in criteria.williams_zones:
            return False
    if criteria.use_sma:
        if not passes_sma_filter(hit, criteria.sma_filter_mode):
            return False
    if criteria.use_cum_delta_24h:
        if not passes_cum_delta_24h_filter(cd24, criteria.cum_delta_24h_filter):
            return False
    if criteria.use_oi_24h:
        if not passes_oi_24h_filter(oi24, criteria.oi_24h_filter):
            return False
    if criteria.use_divergence:
        if not passes_divergence_filter(hit, criteria):
            return False
    return True


@dataclass(frozen=True)
class WilliamsHit:
    symbol: str
    tf_key: str
    zone: HitZone
    willy_ema: float
    willy: float
    close: float
    close_sma: float
    bar_time: pd.Timestamp
    div_kind: DivKind | None = None
    div_confirm_time: pd.Timestamp | None = None
    div_bars_ago: int | None = None
    cum_delta_24h_change: float = float("nan")
    oi_24h_change: float = float("nan")


def fetch_spot_klines(symbol: str, interval: str, limit: int = 120) -> pd.DataFrame:
    sym = symbol.upper().replace("/", "")
    lim = max(20, min(1000, int(limit)))
    q = urllib.parse.urlencode({"symbol": sym, "interval": interval, "limit": str(lim)})
    url = f"https://api.binance.com/api/v3/klines?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "orderflow-williams-scan/1"})
    with urllib.request.urlopen(req, timeout=20) as resp:
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


def klines_with_proxy_delta(klines: pd.DataFrame) -> pd.DataFrame:
    """δ = 2×taker_buy − volume; кумулятив — как на главном графике."""
    out = klines.copy()
    v = pd.to_numeric(out["volume_base"], errors="coerce").to_numpy(dtype=np.float64)
    tb = pd.to_numeric(out["taker_buy_base"], errors="coerce").to_numpy(dtype=np.float64)
    out["delta"] = 2.0 * tb - v
    out["cum_delta"] = out["delta"].cumsum()
    return out


def compute_cum_delta_change_24h(kl: pd.DataFrame, *, use_closed_bar: bool = True) -> float | None:
    """
    Прирост кум. δ за последние 24 ч: cum[eval] − cum[бар ≤ eval−24h].
    δ = 2×taker_buy − volume (как на главном графике).
    """
    if kl is None or kl.empty:
        return None
    df = klines_with_proxy_delta(kl.sort_values("open_time").reset_index(drop=True))
    if len(df) < 5:
        return None
    eval_idx = len(df) - 2 if use_closed_bar and len(df) >= 2 else len(df) - 1
    open_ms = pd.to_numeric(df["open_time"], errors="coerce").to_numpy(dtype=np.float64)
    if not np.isfinite(open_ms[eval_idx]):
        return None
    ref_ms = float(open_ms[eval_idx]) - 24.0 * 60.0 * 60.0 * 1000.0
    idx_arr = np.arange(len(open_ms), dtype=np.int64)
    valid = open_ms <= ref_ms
    if not valid.any():
        return None
    ref_idx = int(idx_arr[valid].max())
    cum = pd.to_numeric(df["cum_delta"], errors="coerce").to_numpy(dtype=np.float64)
    change = float(cum[eval_idx] - cum[ref_idx])
    if not np.isfinite(change):
        return None
    return change


def fetch_cum_delta_24h_change(symbol: str, *, use_closed_bar: bool = True) -> float | None:
    sym = symbol.upper()
    try:
        kl = fetch_spot_klines(sym, CUM_DELTA_24H_INTERVAL, CUM_DELTA_24H_KLINES_LIMIT)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    return compute_cum_delta_change_24h(kl, use_closed_bar=use_closed_bar)


def prefetch_cum_delta_24h_map(
    symbols: list[str],
    *,
    use_closed_bar: bool = True,
    max_workers: int = 12,
) -> dict[str, float | None]:
    """Кэш прироста кум. δ за 24 ч по символам (один запрос 15m на пару)."""
    syms = sorted({s.upper() for s in symbols if s})
    out: dict[str, float | None] = {s: None for s in syms}
    if not syms:
        return out
    workers = max(4, min(20, int(max_workers)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_cum_delta_24h_change, sym, use_closed_bar=use_closed_bar): sym for sym in syms}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                out[sym] = fut.result()
            except Exception:
                out[sym] = None
    return out


def passes_cum_delta_24h_filter(
    change: float | None,
    mode: CumDelta24hFilterMode,
) -> bool:
    if mode == "none":
        return True
    if change is None or not np.isfinite(change):
        return False
    if mode == "up":
        return float(change) > 0.0
    if mode == "down":
        return float(change) < 0.0
    return True


def compute_oi_change_24h(df: pd.DataFrame, *, use_closed_bar: bool = True) -> float | None:
    """
    Прирост Open Interest за последние 24 ч: OI[eval] − OI[бар ≤ eval−24h].
    Данные USDT-M `openInterestHist`.
    """
    if df is None or df.empty:
        return None
    out = df.sort_values("open_time").reset_index(drop=True)
    if len(out) < 5:
        return None
    eval_idx = len(out) - 2 if use_closed_bar and len(out) >= 2 else len(out) - 1
    open_ms = pd.to_numeric(out["open_time"], errors="coerce").to_numpy(dtype=np.float64)
    if not np.isfinite(open_ms[eval_idx]):
        return None
    ref_ms = float(open_ms[eval_idx]) - 24.0 * 60.0 * 60.0 * 1000.0
    idx_arr = np.arange(len(open_ms), dtype=np.int64)
    valid = open_ms <= ref_ms
    if not valid.any():
        return None
    ref_idx = int(idx_arr[valid].max())
    oi = pd.to_numeric(out["open_interest"], errors="coerce").to_numpy(dtype=np.float64)
    change = float(oi[eval_idx] - oi[ref_idx])
    if not np.isfinite(change):
        return None
    return change


def fetch_oi_24h_change(symbol: str, *, use_closed_bar: bool = True) -> float | None:
    sym = symbol.upper()
    try:
        df = fetch_futures_open_interest_hist(sym, OI_24H_INTERVAL, OI_24H_LIMIT)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    return compute_oi_change_24h(df, use_closed_bar=use_closed_bar)


def prefetch_oi_24h_map(
    symbols: list[str],
    *,
    use_closed_bar: bool = True,
    max_workers: int = 12,
) -> dict[str, float | None]:
    """Кэш прироста OI за 24 ч по символам (USDT-M, один запрос 15m на пару)."""
    syms = sorted({s.upper() for s in symbols if s})
    out: dict[str, float | None] = {s: None for s in syms}
    if not syms:
        return out
    workers = max(4, min(20, int(max_workers)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_oi_24h_change, sym, use_closed_bar=use_closed_bar): sym for sym in syms}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                out[sym] = fut.result()
            except Exception:
                out[sym] = None
    return out


def passes_oi_24h_filter(
    change: float | None,
    mode: Oi24hFilterMode,
) -> bool:
    if mode == "none":
        return True
    if change is None or not np.isfinite(change):
        return False
    if mode == "up":
        return float(change) > 0.0
    if mode == "down":
        return float(change) < 0.0
    return True


def fetch_spot_usdt_symbols() -> list[str]:
    url = "https://api.binance.com/api/v3/exchangeInfo"
    req = urllib.request.Request(url, headers={"User-Agent": "orderflow-williams-scan/1"})
    with urllib.request.urlopen(req, timeout=35) as resp:
        data = json.loads(resp.read().decode())
    out: list[str] = []
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if str(s.get("quoteAsset", "")).upper() != "USDT":
            continue
        sym = str(s.get("symbol", "")).upper()
        if sym:
            out.append(sym)
    return sorted(set(out))


def fetch_spot_24h_quote_volume() -> dict[str, float]:
    """Один запрос: quoteVolume USDT spot 24h."""
    url = "https://api.binance.com/api/v3/ticker/24hr"
    req = urllib.request.Request(url, headers={"User-Agent": "orderflow-williams-scan/1"})
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


def klines_limit_for_williams(
    length: int,
    ema_length: int,
    *,
    sma_length: int = 20,
    extra: int = 12,
    check_divergence: bool = False,
    pivot_left: int = 5,
    pivot_right: int = 5,
    min_bars_between: int = 10,
    div_max_age_bars: int = 30,
) -> int:
    n = max(int(length), int(ema_length), int(sma_length))
    lim = max(60, n + int(ema_length) + int(extra))
    if check_divergence:
        need = int(pivot_left) + int(pivot_right) + max(int(min_bars_between), 3) + int(div_max_age_bars) + 10
        lim = max(lim, need)
    return max(60, min(500, lim))


def latest_divergence_near(
    divs: list[PriceCumDeltaDivergence],
    eval_idx: int,
    *,
    max_age_bars: int,
    kinds: frozenset[DivKind] | None = None,
) -> PriceCumDeltaDivergence | None:
    """Последняя дивергенция, подтверждённая не дальше max_age_bars от eval_idx."""
    allowed = kinds or frozenset({"bearish", "bullish"})
    best: PriceCumDeltaDivergence | None = None
    age = max(0, int(max_age_bars))
    for d in divs:
        if d.kind not in allowed:
            continue
        if d.i2 > eval_idx or d.i2 < eval_idx - age:
            continue
        if best is None or d.i2 > best.i2:
            best = d
    return best


def classify_willy_ema(ema: float) -> WilliamsZone | None:
    if not np.isfinite(ema):
        return None
    if ema > OVERBOUGHT_LEVEL:
        return "overbought"
    if ema < OVERSOLD_LEVEL:
        return "oversold"
    return None


def evaluate_symbol_williams(
    symbol: str,
    tf_key: str,
    *,
    length: int = 21,
    ema_length: int = 13,
    sma_length: int = 20,
    use_closed_bar: bool = True,
    overbought_level: float = OVERBOUGHT_LEVEL,
    oversold_level: float = OVERSOLD_LEVEL,
    pivot_left: int = 5,
    pivot_right: int = 5,
    min_bars_between: int = 10,
    div_max_age_bars: int = 30,
    div_kinds: frozenset[DivKind] | None = None,
    compute_divergence: bool = True,
) -> WilliamsHit | None:
    """
    Одна пара + один ТФ: метрики Williams, SMA, δ (без отбора — фильтр в ScannerSearchCriteria).
    """
    api_iv = SCANNER_TF_API.get(tf_key)
    if not api_iv:
        return None
    sym = symbol.upper()
    lim = klines_limit_for_williams(
        length,
        ema_length,
        sma_length=sma_length,
        check_divergence=compute_divergence,
        pivot_left=pivot_left,
        pivot_right=pivot_right,
        min_bars_between=min_bars_between,
        div_max_age_bars=div_max_age_bars,
    )
    try:
        kl = fetch_spot_klines(sym, api_iv, lim)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None
    if kl is None or len(kl) < max(length, ema_length) + 2:
        return None

    kl = kl.sort_values("open_time").reset_index(drop=True)
    w = williams_percent_r(kl, length=length, ema_length=ema_length)
    if w.empty:
        return None

    eval_idx = len(kl) - 2 if use_closed_bar and len(kl) >= 2 else len(kl) - 1
    ema_v = float(w["willy_ema"].iloc[eval_idx])
    raw_v = float(w["willy"].iloc[eval_idx])
    if not np.isfinite(ema_v):
        return None

    # SMA по close (для фильтра/колонки на “баре оценки”).
    close_s = pd.to_numeric(kl["close"], errors="coerce")
    sma = close_s.rolling(int(sma_length), min_periods=int(sma_length)).mean()
    close_sma_v = float(sma.iloc[eval_idx]) if sma_length and sma_length > 1 else float("nan")

    zone: HitZone
    if ema_v > float(overbought_level):
        zone = "overbought"
    elif ema_v < float(oversold_level):
        zone = "oversold"
    else:
        zone = "neutral"

    div_kind: DivKind | None = None
    div_confirm_time: pd.Timestamp | None = None
    div_bars_ago: int | None = None

    if compute_divergence:
        bar_df = klines_with_proxy_delta(kl)
        need = int(pivot_left) + int(pivot_right) + max(int(min_bars_between), 3) + 2
        if len(bar_df) >= need:
            high = bar_df["high"].to_numpy(dtype=np.float64)
            low = bar_df["low"].to_numpy(dtype=np.float64)
            cum = bar_df["cum_delta"].to_numpy(dtype=np.float64)
            divs = detect_price_vs_cum_divergences(
                high,
                low,
                cum,
                pivot_left=int(pivot_left),
                pivot_right=int(pivot_right),
                min_bars_between=int(min_bars_between),
            )
            latest = latest_divergence_near(
                divs,
                eval_idx,
                max_age_bars=int(div_max_age_bars),
                kinds=div_kinds,
            )
            if latest is not None:
                div_kind = latest.kind  # type: ignore[assignment]
                div_confirm_time = pd.to_datetime(
                    bar_df["open_time"].iloc[latest.i2], unit="ms", utc=True
                )
                div_bars_ago = int(eval_idx - latest.i2)

    ts = pd.to_datetime(kl["open_time"].iloc[eval_idx], unit="ms", utc=True)
    close_v = float(kl["close"].iloc[eval_idx])
    return WilliamsHit(
        symbol=sym,
        tf_key=tf_key,
        zone=zone,
        willy_ema=ema_v,
        willy=raw_v if np.isfinite(raw_v) else float("nan"),
        close=close_v,
        close_sma=close_sma_v,
        bar_time=ts,
        div_kind=div_kind,
        div_confirm_time=div_confirm_time,
        div_bars_ago=div_bars_ago,
        cum_delta_24h_change=float("nan"),
        oi_24h_change=float("nan"),
    )


def hits_to_dataframe(hits: Iterable[WilliamsHit], *, vol_map: dict[str, float] | None = None) -> pd.DataFrame:
    rows = []
    for h in hits:
        vol = (vol_map or {}).get(h.symbol, float("nan"))
        rows.append(
            {
                "symbol": h.symbol,
                "timeframe": h.tf_key,
                "tf_ru": SCANNER_TF_LABELS_RU.get(h.tf_key, h.tf_key),
                "zone": h.zone,
                "zone_ru": ZONE_LABEL_RU.get(h.zone, h.zone),
                "willy_ema": h.willy_ema,
                "willy": h.willy,
                "close": h.close,
                "close_sma": h.close_sma,
                "bar_time": h.bar_time,
                "quote_vol_24h": vol,
                "div_kind": h.div_kind,
                "div_ru": DIV_LABEL_RU.get(h.div_kind, "—") if h.div_kind else "—",
                "div_confirm_time": h.div_confirm_time,
                "div_bars_ago": h.div_bars_ago,
                "cum_delta_24h_change": h.cum_delta_24h_change,
                "oi_24h_change": h.oi_24h_change,
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "symbol",
                "timeframe",
                "tf_ru",
                "zone",
                "zone_ru",
                "willy_ema",
                "willy",
                "close",
                "close_sma",
                "bar_time",
                "quote_vol_24h",
                "div_kind",
                "div_ru",
                "div_confirm_time",
                "div_bars_ago",
                "cum_delta_24h_change",
                "oi_24h_change",
            ]
        )
    df = pd.DataFrame(rows)
    return sort_scanner_results_by_age(df)


def sort_scanner_results_by_age(df: pd.DataFrame | None, *, ascending: bool = True) -> pd.DataFrame:
    """Сортировка по времени бара сигнала: старые сверху, новые снизу."""
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()
    if "bar_time" not in df.columns:
        return df.reset_index(drop=True)
    out = df.copy()
    out["_sort_time"] = pd.to_datetime(out["bar_time"], utc=True, errors="coerce")
    out = out.sort_values(
        ["_sort_time", "symbol", "timeframe"],
        ascending=[ascending, True, True],
        na_position="last",
    )
    return out.drop(columns=["_sort_time"]).reset_index(drop=True)


def _scan_task(args: tuple) -> WilliamsHit | None:
    (
        sym,
        tf_key,
        length,
        ema_length,
        sma_length,
        use_closed_bar,
        pivot_left,
        pivot_right,
        min_bars_between,
        div_max_age_bars,
        div_kinds,
        criteria,
        cum_delta_24h_change,
        oi_24h_change,
    ) = args
    hit = evaluate_symbol_williams(
        sym,
        tf_key,
        length=length,
        ema_length=ema_length,
        sma_length=sma_length,
        use_closed_bar=use_closed_bar,
        compute_divergence=bool(criteria.compute_divergence),
        pivot_left=pivot_left,
        pivot_right=pivot_right,
        min_bars_between=min_bars_between,
        div_max_age_bars=div_max_age_bars,
        div_kinds=div_kinds,
    )
    if hit is None:
        return None
    cd24 = cum_delta_24h_change
    oi24 = oi_24h_change
    if not passes_hit_search_criteria(hit, cd24, oi24, criteria):
        return None
    cd_v = float(cd24) if cd24 is not None and np.isfinite(cd24) else float("nan")
    oi_v = float(oi24) if oi24 is not None and np.isfinite(oi24) else float("nan")
    return WilliamsHit(
        symbol=hit.symbol,
        tf_key=hit.tf_key,
        zone=hit.zone,
        willy_ema=hit.willy_ema,
        willy=hit.willy,
        close=hit.close,
        close_sma=hit.close_sma,
        bar_time=hit.bar_time,
        div_kind=hit.div_kind,
        div_confirm_time=hit.div_confirm_time,
        div_bars_ago=hit.div_bars_ago,
        cum_delta_24h_change=cd_v,
        oi_24h_change=oi_v,
    )


def run_williams_scan(
    symbols: list[str],
    tf_keys: list[str],
    *,
    length: int = 21,
    ema_length: int = 13,
    sma_length: int = 20,
    use_closed_bar: bool = True,
    max_workers: int = 12,
    progress: Callable[[float, str], None] | None = None,
    criteria: ScannerSearchCriteria | None = None,
    pivot_left: int = 5,
    pivot_right: int = 5,
    min_bars_between: int = 10,
    div_max_age_bars: int = 30,
) -> pd.DataFrame:
    """
    Параллельный скан: symbols × tf_keys.
    Отбор строк — по `ScannerSearchCriteria` (AND между отмеченными критериями).
    """
    crit = criteria or ScannerSearchCriteria()
    if not crit.any_active():
        return hits_to_dataframe([])

    tfs = [t for t in tf_keys if t in SCANNER_TF_API]
    syms = [s.upper() for s in symbols if s]
    if not syms or not tfs:
        return hits_to_dataframe([])

    cd24_map = prefetch_cum_delta_24h_map(
        syms,
        use_closed_bar=bool(use_closed_bar),
        max_workers=max_workers,
    )
    oi24_map = prefetch_oi_24h_map(
        syms,
        use_closed_bar=bool(use_closed_bar),
        max_workers=max_workers,
    )

    scan_tfs = list(tfs)
    if (
        (crit.use_cum_delta_24h or crit.use_oi_24h)
        and not crit.use_williams
        and not crit.use_sma
        and not crit.use_divergence
        and scan_tfs
    ):
        scan_tfs = [scan_tfs[0]]

    kinds = crit.div_kinds if crit.use_divergence or crit.compute_divergence else frozenset({"bearish", "bullish"})
    tasks = [
        (
            sym,
            tf,
            int(length),
            int(ema_length),
            int(sma_length),
            bool(use_closed_bar),
            int(pivot_left),
            int(pivot_right),
            int(min_bars_between),
            int(div_max_age_bars),
            kinds,
            crit,
            cd24_map.get(sym),
            oi24_map.get(sym),
        )
        for sym in syms
        for tf in scan_tfs
    ]
    total = len(tasks)
    hits: list[WilliamsHit] = []
    done = 0
    workers = max(4, min(20, int(max_workers)))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_scan_task, t): t for t in tasks}
        for fut in as_completed(futs):
            done += 1
            if progress is not None:
                sym, tf, *_ = futs[fut]
                progress(done / max(total, 1), f"{sym} · {tf}")
            try:
                h = fut.result()
                if h is not None:
                    hits.append(h)
            except Exception:
                pass

    vol_map: dict[str, float] = {}
    try:
        vol_map = fetch_spot_24h_quote_volume()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        pass
    return hits_to_dataframe(hits, vol_map=vol_map)
