"""
Order Flow Analyzer — OHLC и order-flow по **REST /api/v3/klines** (без тикового графика).

Кумулятивная δ: прокси `2×taker_buy_base − volume` по всем видимым свечам выбранной **пары δ**.
OHLC можно строить по **любой другой** паре при том же таймфрейме; линии привязаны ко временам открытия свечи графика.

Open Interest (опционально): **USDT-M futures** `GET /futures/data/openInterestHist` по **паре графика** (не spot),
тот же `period`, что и у свечей; панель под кум. δ.

Объём по свечам: **базовый актив** из тех же spot klines, что и OHLC; панель под графиком цены.

**VWAP** (опционально): типичная цена **(H+L+C)/3**, вес **объём базового актива**; накопление со сбросом на **каждый календарный день UTC** (как у Binance klines).

Включение индикаторов: сайдбар → раскрывающийся блок **«Индикаторы»**.

Run:

  python3 -m streamlit run src/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import timedelta
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from auth import render_auth_gate, render_auth_sidebar, require_page_access, PAGE_MAIN
from main_settings_presets import apply_pending_preset_before_widgets, render_main_presets_sidebar
from binance_http import (
    fetch_spot_exchange_info,
    fetch_spot_klines,
    fetch_futures_open_interest_hist as _fetch_futures_oi_hist_raw,
    last_binance_error,
)
from futures_market import fetch_futures_klines
from atr_indicator import add_atr_panel
from price_compression import (
    CompressionParams,
    CompressionZone,
    RECOMMENDED_COMPRESSION_HELP_RU,
    add_compression_traces,
    apply_recommended_compression_session_state,
    compression_params_from_session,
    default_compression_params_for_tf,
    detect_compression_zones,
    init_compression_session_state,
)
from williams_r import add_williams_panel
from oi_symbol_cache import (
    list_symbols_with_open_interest_fast,
    load_oi_symbol_cache_stale,
    oi_cache_age_sec,
    clear_oi_symbol_cache,
)

KLINES_API: dict[str, str] = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "1d": "1d",
}

TF_TITLES_RU: dict[str, str] = {
    "5m": "5 минут",
    "15m": "15 минут",
    "1h": "1 час",
    "2h": "2 часа",
    "4h": "4 часа",
    "1d": "1 день",
}

# Высота графика под типичный экран при масштабе браузера 100% (без лишнего скролла)
CHART_HEIGHT_PX: int = 700

# Индикаторные панели под OHLC: пользователь может менять порядок (см. сайдбар).
DEFAULT_PANEL_ORDER: list[str] = ["volume", "cum_delta", "open_interest", "atr", "williams"]
PANEL_LABELS_RU: dict[str, str] = {
    "volume": "Объём",
    "cum_delta": "Кумулятивная δ",
    "open_interest": "Open Interest",
    "atr": "ATR",
    "williams": "Williams %R",
}

# Как часто перерисовывать главную область (Streamlit fragment)
REFRESH_SCREEN = timedelta(minutes=5)


def _norm_sym(s: str) -> str:
    x = (s or "btcusdt").strip().lower().replace("/", "")
    return x if x else "btcusdt"


# ── REST klines ───────────────────────────────────────────────────────────────


@st.cache_data(ttl=60, show_spinner=False)
def cached_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    try:
        return fetch_spot_klines(symbol, interval, limit)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return pd.DataFrame()


_FALLBACK_PAIRS_USDT = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
]


def fetch_spot_usdt_symbol_list() -> list[str]:
    data = fetch_spot_exchange_info()
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


@st.cache_data(ttl=3600, show_spinner=False)
def cached_spot_usdt_symbol_list(reload_token: int = 0) -> list[str]:
    """
    USDT spot TRADING. `reload_token` — сброс кэша (кнопка «Обновить список пар»).
    При ошибке сети — короткий fallback (~7 пар); после восстановления API увеличьте reload_token.
    """
    try:
        found = fetch_spot_usdt_symbol_list()
        return found if found else list(_FALLBACK_PAIRS_USDT)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError, KeyError):
        return list(_FALLBACK_PAIRS_USDT)


@st.cache_data(ttl=6 * 3600, show_spinner="Список пар с OI…")
def cached_usdtm_symbols_with_oi(period: str, rebuild_token: int = 0) -> tuple[str, ...]:
    """USDT-M perpetual с OI: файл на диске + быстрая пересборка (топ по объёму)."""
    from oi_symbol_cache import _FALLBACK_OI_SYMBOLS, list_symbols_with_open_interest_fast, load_oi_symbol_cache_stale

    try:
        syms, _src = list_symbols_with_open_interest_fast(
            period, rebuild=bool(rebuild_token), max_workers=14
        )
        if len(syms) >= 2:
            return tuple(syms)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        pass
    stale = load_oi_symbol_cache_stale(period)
    if stale:
        return tuple(stale)
    return tuple(_FALLBACK_OI_SYMBOLS)


@st.cache_data(ttl=60, show_spinner=False)
def cached_futures_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    try:
        return fetch_futures_klines(symbol, interval, limit)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError, KeyError):
        return pd.DataFrame()


def klines_with_proxy_delta(klines: pd.DataFrame) -> pd.DataFrame:
    out = klines.copy()
    v = out["volume_base"].to_numpy(dtype=np.float64)
    tb = out["taker_buy_base"].to_numpy(dtype=np.float64)
    out["delta"] = 2.0 * tb - v
    out["cum_delta"] = out["delta"].cumsum()
    out["timestamp"] = pd.to_datetime(out["open_time"], unit="ms")
    return out


def load_klines_frame(sym: str, tf_key: str, *, limit: int = 500) -> tuple[pd.DataFrame, str]:
    """
    Сначала Spot `api/v3/klines`; если пары нет или пусто — USDT-M `fapi/v1/klines`
    (некоторые perpetual-only тикеры, например EDGEUSDT, на spot не торгуются).
    """
    api_iv = KLINES_API.get(tf_key)
    if not api_iv:
        return pd.DataFrame(), "—"
    lim = max(20, min(1000, int(limit)))
    sym_u = sym.upper()
    klines = pd.DataFrame()
    try:
        klines = cached_klines(sym_u, api_iv, lim)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        klines = pd.DataFrame()

    venue = "Spot"
    if klines.empty or len(klines) < 2:
        klines = cached_futures_klines(sym_u, api_iv, lim)
        if not klines.empty and len(klines) >= 2:
            venue = "USDT-M (fapi)"

    if klines.empty or len(klines) < 2:
        return pd.DataFrame(), "нет данных (нет пары на Spot и на USDT-M)"

    if "taker_buy_base" not in klines.columns:
        klines["taker_buy_base"] = 0.0

    return (
        klines_with_proxy_delta(klines),
        f"REST klines ({venue}) + δ (2×taker_buy − volume)",
    )


def overlay_cum_delta_on_chart(df_chart: pd.DataFrame, df_cum: pd.DataFrame) -> pd.DataFrame:
    """
    OHLC и время — от свечной пары; кумулятивная δ — от второй пары, по ключу timestamp (open свечи).
    """
    if df_chart.empty:
        return df_chart.copy()
    base_cols = ["timestamp", "open", "high", "low", "close"]
    if "volume_base" in df_chart.columns:
        base_cols.append("volume_base")
    base = df_chart[base_cols].sort_values("timestamp").drop_duplicates("timestamp").copy()
    if "volume_base" not in base.columns:
        base["volume_base"] = float("nan")
    if df_cum.empty:
        base["cum_delta_pick"] = float("nan")
        return base
    cum_side = df_cum[["timestamp", "cum_delta"]].rename(columns={"cum_delta": "cum_delta_pick"})
    cum_side = cum_side.sort_values("timestamp").drop_duplicates("timestamp")
    m = pd.merge(base, cum_side, on="timestamp", how="left")
    ser = pd.to_numeric(m["cum_delta_pick"], errors="coerce")
    ser = ser.interpolate(limit_area="inside").ffill().bfill()
    m["cum_delta_pick"] = ser
    return m


def fetch_futures_open_interest_hist(symbol: str, period: str, limit: int = 500) -> pd.DataFrame:
    """USDT-M futures: история open interest (`/futures/data/openInterestHist`)."""
    raw = _fetch_futures_oi_hist_raw(symbol, period, limit)
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


@st.cache_data(ttl=90, show_spinner=False)
def cached_futures_open_interest_hist(symbol: str, period: str, limit: int) -> pd.DataFrame:
    try:
        df = fetch_futures_open_interest_hist(symbol, period, limit)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return pd.DataFrame()
    if df.empty:
        return df
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.sort_values("timestamp").drop_duplicates("timestamp")


def load_open_interest_frame(sym: str, tf_key: str, *, limit: int = 500) -> tuple[pd.DataFrame, str]:
    period = KLINES_API.get(tf_key)
    if not period:
        return pd.DataFrame(), "—"
    try:
        df = cached_futures_open_interest_hist(sym.upper(), period, limit)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return pd.DataFrame(), "ошибка сети / API"
    if df.empty:
        return pd.DataFrame(), "нет OI (нет контракта или пустой ответ)"
    return df[["timestamp", "open_interest"]], "fapi openInterestHist (USDT-M)"


def overlay_open_interest(blended: pd.DataFrame, df_oi: pd.DataFrame) -> pd.DataFrame:
    """Добавляет колонку `open_interest_pick` по `timestamp` (как кум. δ)."""
    out = blended.copy()
    if df_oi.empty or "timestamp" not in out.columns:
        out["open_interest_pick"] = float("nan")
        return out
    oi_side = df_oi[["timestamp", "open_interest"]].rename(columns={"open_interest": "open_interest_pick"})
    oi_side = oi_side.sort_values("timestamp").drop_duplicates("timestamp")
    base_ts = out[["timestamp"]].copy()
    m = pd.merge(base_ts, oi_side, on="timestamp", how="left")
    ser = pd.to_numeric(m["open_interest_pick"], errors="coerce")
    ser = ser.interpolate(limit_area="inside").ffill().bfill()
    out["open_interest_pick"] = ser.to_numpy(dtype=np.float64, copy=False)
    return out


def attach_daily_utc_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """
    VWAP по барам пары графика: typical = (high+low+close)/3, вес volume_base.
    Кумулятив внутри календарного дня **UTC** (`timestamp` в UTC), затем сброс.
    """
    out = df.copy()
    if df.empty or "timestamp" not in df.columns:
        out["vwap"] = float("nan")
        return out
    d = df.sort_values("timestamp")
    h = d["high"].to_numpy(dtype=np.float64)
    l = d["low"].to_numpy(dtype=np.float64)
    cl = d["close"].to_numpy(dtype=np.float64)
    v = (
        pd.to_numeric(d["volume_base"], errors="coerce").to_numpy(dtype=np.float64)
        if "volume_base" in d.columns
        else np.zeros(len(d), dtype=np.float64)
    )
    v = np.where(np.isfinite(v) & (v > 0.0), v, 0.0)
    tp = (h + l + cl) / 3.0
    tpv = tp * v
    ts = pd.to_datetime(d["timestamp"], errors="coerce", utc=True)
    dk = ts.dt.floor("D")
    work = pd.DataFrame({"timestamp": d["timestamp"].values, "tpv": tpv, "vol": v, "dk": dk})
    work["cs_tpv"] = work.groupby("dk", sort=False)["tpv"].cumsum()
    work["cs_v"] = work.groupby("dk", sort=False)["vol"].cumsum()
    with np.errstate(divide="ignore", invalid="ignore"):
        work["vwap"] = np.where(work["cs_v"] > 0.0, work["cs_tpv"] / work["cs_v"], np.nan)
    work_u = work.drop_duplicates("timestamp", keep="last")[["timestamp", "vwap"]]
    merged = df[["timestamp"]].merge(work_u, on="timestamp", how="left")
    out["vwap"] = merged["vwap"].to_numpy(dtype=np.float64, copy=False)
    return out


def _filter_symbols(symbols: list[str], needle: str) -> list[str]:
    needle = needle.strip().upper()
    if not needle:
        return symbols
    return sorted([x for x in symbols if needle in x.upper()])


def _select_index(options: list[str], preferred: str) -> int:
    try:
        return options.index(preferred)
    except ValueError:
        return 0


def _selectbox_options_with_saved(
    options: list[str],
    *,
    session_key: str,
    fallback: str,
    universe: list[str],
) -> list[str]:
    """Добавляет сохранённую пару в список, если она есть в universe."""
    opts = list(options)
    pref = str(st.session_state.get(session_key, fallback))
    if pref not in opts:
        if pref in universe:
            opts = sorted(set(opts) | {pref})
        elif opts:
            st.session_state[session_key] = opts[0]
        else:
            st.session_state[session_key] = fallback
    return opts


def _init_main_sidebar_defaults() -> None:
    defaults: dict[str, object] = {
        "main_tf_key": "5m",
        "filt_ohlc": "",
        "sb_chart_pair": "BTCUSDT",
        "cb_cum_same_chart": False,
        "filt_cum": "",
        "sb_cum_pair": "BTCUSDT",
        "panel_order": list(DEFAULT_PANEL_ORDER),
        "cb_show_volume": True,
        "cb_show_vwap": True,
        "cb_show_price_ma": True,
        "sl_price_ma_length": 20,
        "cb_show_oi": True,
        "cb_show_willy": False,
        "sl_willy_length": 21,
        "sl_willy_ema_length": 13,
        "cb_show_atr": False,
        "sl_atr_period": 14,
        "cb_div_enabled": False,
        "cb_div_show_lines": True,
        "sl_div_pivot_left": 5,
        "sl_div_pivot_right": 5,
        "sl_div_min_bars": 10,
        "cb_show_compression": False,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val
    init_compression_session_state(st.session_state, "sl_pc", only_missing=True)


# ── Дивергенции: цена (фрактальные экстремумы) vs кумулятивная δ ───────────────


def _fractal_pivot_high_indices(high: np.ndarray, left: int, right: int) -> list[int]:
    """Строгий локальный максимум: выше всех баров слева (L) и справа (R), без плато."""
    if left < 1 or right < 1:
        return []

    n = int(high.shape[0])
    out: list[int] = []
    for i in range(left, n - right):
        h = float(high[i])
        if not (h > float(np.max(high[i - left : i])) and h > float(np.max(high[i + 1 : i + right + 1]))):
            continue

        out.append(i)

    return out


def _fractal_pivot_low_indices(low: np.ndarray, left: int, right: int) -> list[int]:
    if left < 1 or right < 1:
        return []

    n = int(low.shape[0])
    out: list[int] = []
    for i in range(left, n - right):
        ln = float(low[i])
        if not (
            ln < float(np.min(low[i - left : i]))
            and ln < float(np.min(low[i + 1 : i + right + 1]))
        ):
            continue

        out.append(i)

    return out


@dataclass(frozen=True)
class PriceCumDeltaDivergence:
    kind: str  # "bearish" | "bullish"

    i1: int

    i2: int


def detect_price_vs_cum_divergences(
    high: np.ndarray,
    low: np.ndarray,
    cum_delta: np.ndarray,
    *,
    pivot_left: int,
    pivot_right: int,
    min_bars_between: int,
) -> list[PriceCumDeltaDivergence]:
    ph = _fractal_pivot_high_indices(high, pivot_left, pivot_right)
    pl = _fractal_pivot_low_indices(low, pivot_left, pivot_right)

    sig: list[PriceCumDeltaDivergence] = []

    for k in range(1, len(ph)):
        i1, i2 = ph[k - 1], ph[k]
        if i2 - i1 < min_bars_between:
            continue

        cd1 = float(cum_delta[i1])
        cd2 = float(cum_delta[i2])

        if not (np.isfinite(cd1) and np.isfinite(cd2)):
            continue

        h1 = float(high[i1])
        h2 = float(high[i2])

        # Медвежья классическая: HH по цене, LH по кум. δ между двумя swing high.

        if h2 > h1 and cd2 < cd1:
            sig.append(PriceCumDeltaDivergence(kind="bearish", i1=i1, i2=i2))

    for k in range(1, len(pl)):
        i1, i2 = pl[k - 1], pl[k]
        if i2 - i1 < min_bars_between:
            continue

        cd1 = float(cum_delta[i1])
        cd2 = float(cum_delta[i2])

        if not (np.isfinite(cd1) and np.isfinite(cd2)):
            continue

        l1 = float(low[i1])
        l2 = float(low[i2])

        # Бычья: LL по цене, HL по кум. δ между двумя swing low.

        if l2 < l1 and cd2 > cd1:
            sig.append(PriceCumDeltaDivergence(kind="bullish", i1=i1, i2=i2))

    # Стабильный порядок: по времени подтверждения.

    sig.sort(key=lambda d: (d.i2, 0 if d.kind == "bearish" else 1))

    return sig


def _apply_divergence_traces(
    fig: go.Figure,
    df: pd.DataFrame,
    divs: list[PriceCumDeltaDivergence],
    *,
    show_lines: bool,
    cum_delta_row: int,
) -> None:
    if not divs:
        return

    high = df["high"].to_numpy(dtype=np.float64)
    low = df["low"].to_numpy(dtype=np.float64)
    cumv = pd.to_numeric(df["cum_delta_pick"], errors="coerce").to_numpy(dtype=np.float64)
    tcol = df["timestamp"]

    bear_xl: list = []
    bear_y_px: list = []
    bear_y_d: list = []
    bull_xl: list = []
    bull_y_px: list = []
    bull_y_d: list = []

    bear_mx: list = []
    bear_my: list = []
    bear_mxd: list = []
    bear_myd: list = []

    bull_mx: list = []
    bull_my: list = []
    bull_mxd: list = []
    bull_myd: list = []

    for d in divs:
        i1, i2 = d.i1, d.i2
        if np.isnan(high[i2]) or np.isnan(low[i2]):
            continue

        cd1 = cumv[i1]
        cd2 = cumv[i2]

        if not (np.isfinite(cd1) and np.isfinite(cd2)):
            continue

        t1 = tcol.iloc[i1]
        t2 = tcol.iloc[i2]

        if show_lines:

            if d.kind == "bearish":
                bear_xl.extend([t1, t2, None])
                bear_y_px.extend([float(high[i1]), float(high[i2]), None])
                bear_y_d.extend([float(cd1), float(cd2), None])

            else:

                bull_xl.extend([t1, t2, None])
                bull_y_px.extend([float(low[i1]), float(low[i2]), None])
                bull_y_d.extend([float(cd1), float(cd2), None])

        pad = float(max(high[i2], low[i2])) * 0.007
        pad = max(pad, 1e-9)

        if d.kind == "bearish":
            bear_mx.append(t2)

            bear_my.append(float(high[i2]) + pad * 4)

            bear_mxd.append(t2)

            bear_myd.append(float(cd2))

        else:

            bull_mx.append(t2)

            bull_my.append(float(low[i2]) - pad * 4)

            bull_mxd.append(t2)

            bull_myd.append(float(cd2))

    clr_bear = "#fb7185"
    clr_bull = "#34d399"

    ln = dict(width=2, dash="dash")

    if show_lines and bear_xl:
        fig.add_trace(
            go.Scatter(
                x=bear_xl,
                y=bear_y_px,
                mode="lines",
                line=dict(color=clr_bear, **ln),
                hoverinfo="skip",
                showlegend=False,
                name="_div_px_bear",
            ),
            row=1,
            col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=bear_xl,
                y=bear_y_d,
                mode="lines",
                line=dict(color=clr_bear, **ln),
                hoverinfo="skip",
                showlegend=False,
                name="_div_d_bear",
            ),
            row=cum_delta_row,
            col=1,
        )

    if show_lines and bull_xl:

        fig.add_trace(
            go.Scatter(
                x=bull_xl,
                y=bull_y_px,
                mode="lines",
                line=dict(color=clr_bull, **ln),
                hoverinfo="skip",
                showlegend=False,
                name="_div_px_bull",
            ),
            row=1,
            col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=bull_xl,
                y=bull_y_d,
                mode="lines",
                line=dict(color=clr_bull, **ln),
                hoverinfo="skip",
                showlegend=False,
                name="_div_d_bull",
            ),
            row=cum_delta_row,
            col=1,
        )

    if bear_mx:

        mk_bear = dict(
            symbol="triangle-down",
            size=13,
            color=clr_bear,
            line=dict(width=1, color="rgba(255,255,255,0.35)"),
        )

        fig.add_trace(
            go.Scatter(
                x=bear_mx,
                y=bear_my,
                mode="markers",
                marker=mk_bear,
                hovertemplate="медвежья · цена / HH + LH по кум. δ<br>%{x}<extra></extra>",
                showlegend=False,
                name="_div_m_bear_px",
            ),
            row=1,
            col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=bear_mxd,
                y=bear_myd,
                mode="markers",
                marker={**mk_bear, "size": 11},
                hovertemplate="медвежья · кум. δ на подтверждении<br>%{x}<extra></extra>",
                showlegend=False,
                name="_div_m_bear_d",
            ),
            row=cum_delta_row,
            col=1,
        )

    if bull_mx:

        mk_bull = dict(
            symbol="triangle-up",
            size=13,
            color=clr_bull,
            line=dict(width=1, color="rgba(255,255,255,0.35)"),
        )

        fig.add_trace(
            go.Scatter(
                x=bull_mx,
                y=bull_my,
                mode="markers",
                marker=mk_bull,
                hovertemplate="бычья · цена / LL + HL по кум. δ<br>%{x}<extra></extra>",
                showlegend=False,
                name="_div_m_bull_px",
            ),
            row=1,
            col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=bull_mxd,
                y=bull_myd,
                mode="markers",
                marker={**mk_bull, "size": 11},
                hovertemplate="бычья · кум. δ на подтверждении<br>%{x}<extra></extra>",
                showlegend=False,
                name="_div_m_bull_d",
            ),
            row=cum_delta_row,
            col=1,
        )


def _median_bar_duration(df: pd.DataFrame) -> pd.Timedelta:
    ts = pd.to_datetime(df["timestamp"], errors="coerce", utc=True).sort_values()
    if len(ts) < 2:
        return pd.Timedelta(minutes=15)
    d = ts.diff().dropna()
    if d.empty:
        return pd.Timedelta(minutes=15)
    med = d.median()
    if pd.isna(med) or med <= pd.Timedelta(0):
        return pd.Timedelta(minutes=15)
    return min(max(med, pd.Timedelta(minutes=1)), pd.Timedelta(days=7))


# ── Figures ───────────────────────────────────────────────────────────────────


def build_figure_bars(
    df: pd.DataFrame,
    *,
    chart_pair: str,
    cum_pair: str,
    tf_key: str,
    divergence_divs: list[PriceCumDeltaDivergence] | None = None,
    divergence_show_lines: bool = True,
    show_volume: bool = True,
    show_futures_oi: bool = True,
    show_vwap: bool = True,
    show_price_ma: bool = True,
    price_ma_length: int = 20,
    show_willy: bool = False,
    willy_length: int = 21,
    willy_ema_length: int = 13,
    show_atr: bool = False,
    atr_period: int = 14,
    show_compression: bool = False,
    compression_zones: list[CompressionZone] | None = None,
    compression_pivot_left: int = 5,
    compression_pivot_right: int = 5,
    compression_min_pivots: int = 3,
    panel_order: list[str] | None = None,
) -> go.Figure:
    pch = chart_pair.upper()
    pcd = cum_pair.upper()
    lbl = TF_TITLES_RU.get(tf_key, tf_key)
    if df.empty or "cum_delta_pick" not in df.columns:
        return _empty_fig(pch, f"нет данных для «{lbl}»")

    dfp = df.copy()
    if "open_interest_pick" not in dfp.columns:
        dfp["open_interest_pick"] = float("nan")
    if "volume_base" not in dfp.columns:
        dfp["volume_base"] = float("nan")
    if "vwap" not in dfp.columns:
        dfp["vwap"] = float("nan")

    show_oi = bool(show_futures_oi)
    show_vol = bool(show_volume)
    show_w = bool(show_willy)
    show_a = bool(show_atr)

    enabled_map = {
        "volume": show_vol,
        "cum_delta": True,
        "open_interest": show_oi,
        "atr": show_a,
        "williams": show_w,
    }
    order = list(panel_order) if panel_order else list(DEFAULT_PANEL_ORDER)
    for k in DEFAULT_PANEL_ORDER:
        if k not in order:
            order.append(k)
    active = [k for k in order if enabled_map.get(k, False)]

    def _title_for(key: str) -> str:
        if key == "volume":
            return f"Объём · {pch} · базовый актив ({lbl})"
        if key == "cum_delta":
            return f"Кумулятивная δ • {pcd} ({lbl}) · taker buy − sell proxy"
        if key == "open_interest":
            return f"Open Interest · {pch} · USDT-M ({lbl})"
        if key == "williams":
            return (
                f"Williams %R ({int(willy_length)}) + EMA {int(willy_ema_length)} · {pch}"
            )
        if key == "atr":
            return f"ATR ({int(atr_period)}) · {pch} ({lbl})"
        return key

    layout_titles: list[str] = [f"{pch} · OHLC ({lbl})"]
    row_map: dict[str, int] = {"price": 1}
    for k in active:
        layout_titles.append(_title_for(k))
        row_map[k] = len(layout_titles)

    vol_row = row_map.get("volume")
    cum_delta_row = row_map.get("cum_delta", 2)
    oi_row = row_map.get("open_interest")
    atr_row = row_map.get("atr")
    willy_row = row_map.get("williams")

    weights = {
        "price": 0.55,
        "volume": 0.16,
        "cum_delta": 0.32,
        "open_interest": 0.22,
        "atr": 0.20,
        "williams": 0.22,
    }
    selected = ["price"] + active
    total_w = sum(weights[k] for k in selected) or 1.0
    row_heights = [weights[k] / total_w for k in selected]
    nrows = len(layout_titles)
    vspace = 0.034 if nrows >= 4 else (0.042 if nrows == 3 else 0.06)
    subplot_titles = tuple(layout_titles)

    fig = make_subplots(
        rows=nrows,
        cols=1,
        row_heights=row_heights,
        vertical_spacing=vspace,
        shared_xaxes=True,
        subplot_titles=subplot_titles,
    )
    fig.update_annotations(font_size=11)
    fig.add_trace(
        go.Candlestick(
            x=dfp["timestamp"],
            open=dfp["open"],
            high=dfp["high"],
            low=dfp["low"],
            close=dfp["close"],
            name="OHLC",
            increasing_line_color="#26d0a8",
            increasing_fillcolor="rgba(38,208,168,0.55)",
            decreasing_line_color="#ff6b7a",
            decreasing_fillcolor="rgba(255,107,122,0.5)",
        ),
        row=1,
        col=1,
    )
    if show_vwap:
        vwap_y = pd.to_numeric(dfp["vwap"], errors="coerce")
        fig.add_trace(
            go.Scatter(
                x=dfp["timestamp"],
                y=vwap_y,
                mode="lines",
                name="VWAP",
                line=dict(color="#fbbf24", width=1.45),
                hovertemplate="VWAP (UTC день): %{y:,.4f}<extra></extra>",
            ),
            row=1,
            col=1,
        )
    if show_price_ma and price_ma_length and price_ma_length > 1:
        # SMA по close — дополнительно к VWAP (если тот включен).
        close_y = pd.to_numeric(dfp["close"], errors="coerce")
        ma_y = close_y.rolling(int(price_ma_length), min_periods=int(price_ma_length)).mean()
        fig.add_trace(
            go.Scatter(
                x=dfp["timestamp"],
                y=ma_y,
                mode="lines",
                name=f"SMA {int(price_ma_length)}",
                line=dict(color="#60a5fa", width=1.6),
                hovertemplate="SMA: %{y:,.4f}<extra></extra>",
            ),
            row=1,
            col=1,
        )
    if show_compression and compression_zones:
        hi = pd.to_numeric(dfp["high"], errors="coerce").to_numpy(dtype=np.float64)
        lo = pd.to_numeric(dfp["low"], errors="coerce").to_numpy(dtype=np.float64)
        add_compression_traces(
            fig,
            dfp,
            compression_zones,
            row=1,
            high=hi,
            low=lo,
            pivot_left=int(compression_pivot_left),
            pivot_right=int(compression_pivot_right),
            min_pivots=int(compression_min_pivots),
        )
    # Candlestick почти не отдаёт hit-test для вертикали x unified; невидимые точки по центру свечи.
    nb = len(dfp)
    hover_pt = float(max(8.0, min(22.0, 5600.0 / max(nb, 120))))
    y_mid = ((dfp["high"] + dfp["low"]) / 2.0).astype(np.float64)
    fig.add_trace(
        go.Scatter(
            x=dfp["timestamp"],
            y=y_mid,
            mode="markers",
            name="_ohlc_hover",
            marker=dict(opacity=0, size=hover_pt, color="rgba(0,0,0,0)", symbol="square"),
            showlegend=False,
            hovertemplate="<extra></extra>",
        ),
        row=1,
        col=1,
    )

    if show_vol and vol_row is not None:
        vol = pd.to_numeric(dfp["volume_base"], errors="coerce").to_numpy(dtype=np.float64)
        o_arr = dfp["open"].to_numpy(dtype=np.float64)
        c_arr = dfp["close"].to_numpy(dtype=np.float64)
        bar_colors = np.where(c_arr >= o_arr, "rgba(38,208,168,0.82)", "rgba(255,107,122,0.82)").tolist()
        bar_w_ms = max(60_000, int(_median_bar_duration(dfp).total_seconds() * 1000 * 0.82))
        fig.add_trace(
            go.Bar(
                x=dfp["timestamp"],
                y=vol,
                name="Volume",
                marker=dict(color=bar_colors, line=dict(width=0)),
                width=bar_w_ms,
                hovertemplate="Объём: %{y:,.6f}<extra></extra>",
            ),
            row=vol_row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=dfp["timestamp"],
                y=vol,
                mode="markers",
                name="_vol_hover",
                marker=dict(opacity=0, size=max(6.0, hover_pt * 0.85), color="rgba(0,0,0,0)", symbol="square"),
                showlegend=False,
                hovertemplate="Объём: %{y:,.6f}<extra></extra>",
            ),
            row=vol_row,
            col=1,
        )

    if cum_delta_row is not None:
        fig.add_trace(
            go.Scatter(
                x=dfp["timestamp"],
                y=dfp["cum_delta_pick"],
                mode="lines",
                name="Cum Δ",
                line=dict(color="#d4bfff", width=1.6),
                fill="tozeroy",
                fillcolor="rgba(212,191,255,0.08)",
                hovertemplate="Cum Δ: %{y:,.4f}<extra></extra>",
            ),
            row=cum_delta_row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=dfp["timestamp"],
                y=dfp["cum_delta_pick"],
                mode="markers",
                name="_delta_hover",
                marker=dict(opacity=0, size=max(6.0, hover_pt * 0.85), color="rgba(0,0,0,0)", symbol="square"),
                showlegend=False,
                hovertemplate="<extra></extra>",
            ),
            row=cum_delta_row,
            col=1,
        )
    if show_oi and oi_row is not None:
        oi_y = pd.to_numeric(dfp["open_interest_pick"], errors="coerce")
        fig.add_trace(
            go.Scatter(
                x=dfp["timestamp"],
                y=oi_y,
                mode="lines",
                name="Open Interest",
                line=dict(color="#67e8f9", width=1.45),
                fill="tozeroy",
                fillcolor="rgba(103,232,249,0.07)",
                hovertemplate="Open Interest: %{y:,.4f}<extra></extra>",
            ),
            row=oi_row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=dfp["timestamp"],
                y=oi_y,
                mode="markers",
                name="_oi_hover",
                marker=dict(opacity=0, size=max(6.0, hover_pt * 0.85), color="rgba(0,0,0,0)", symbol="square"),
                showlegend=False,
                hovertemplate="<extra></extra>",
            ),
            row=oi_row,
            col=1,
        )
    if show_a and atr_row is not None:
        add_atr_panel(
            fig,
            dfp,
            row=atr_row,
            period=int(atr_period),
        )
    if show_w and willy_row is not None:
        add_williams_panel(
            fig,
            dfp,
            row=willy_row,
            length=int(willy_length),
            ema_length=int(willy_ema_length),
        )
    if divergence_divs:
        _apply_divergence_traces(
            fig,
            dfp,
            divergence_divs,
            show_lines=divergence_show_lines,
            cum_delta_row=cum_delta_row,
        )
    extra_panels = max(0, nrows - 2)
    fig_height = CHART_HEIGHT_PX + extra_panels * 130
    order_sig = "_".join(active) or "none"
    _style_fig(
        fig,
        f"{pch}-{pcd}-{tf_key}-blend-{order_sig}",
        height=fig_height,
    )
    # После общих правил осей — скрыть дубли времени сверху (shared X уже связывает pan).
    for r in range(1, nrows):
        fig.update_xaxes(showticklabels=False, row=r, col=1)
    _apply_unified_x_crosshair(fig)
    return fig


# Заливки Williams не трогаем — иначе ломаются шкалы и hover остальных панелей.
_WILLIAMS_FILL_TRACE_NAMES = frozenset(
    {
        "_w_base_top",
        "_w_base_bot",
        "_w_overbought_fill",
        "_w_oversold_fill",
    }
)


def _trace_bind_master_xaxis(name: str, tr) -> bool:
    """Нужно ли привязать трассу к xaxis=\"x\" для единого hover/spike."""
    if name in _WILLIAMS_FILL_TRACE_NAMES:
        return False
    # Линии Williams %R и EMA рисуем на своей subplot-оси (x4, …) — иначе пропадают.
    if name.startswith("Willy %R") or name.startswith("EMA "):
        return False
    # Линия кум. δ — на своей subplot-оси; иначе пропадает.
    if name == "Cum Δ":
        return False
    if name.endswith("_hover"):
        return True
    if getattr(tr, "hoverinfo", None) == "skip" and name.startswith("_"):
        return False
    return True


def _apply_unified_x_crosshair(fig: go.Figure) -> None:
    """
    Единая вертикальная линия и tooltip по времени на всех панелях.

    К xaxis=\"x\" привязываем OHLC, объём, δ, OI и невидимые *_hover.
    Линии Willy %R / EMA и заливки зон остаются на своих subplot-x — иначе
    линии индикатора не видны, шкалы ломаются.
    """
    fig.update_layout(
        hovermode="x unified",
        hoverdistance=-1,
        spikedistance=-1,
    )
    fig.update_xaxes(
        showspikes=True,
        spikesnap="cursor",
        spikecolor="rgba(148,163,184,0.55)",
        spikethickness=1,
        spikemode="across",
    )
    fig.update_yaxes(showspikes=False)
    for tr in fig.data:
        name = tr.name or ""
        if _trace_bind_master_xaxis(name, tr):
            tr.update(xaxis="x")


def _empty_fig(pair: str, subtitle: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        template="plotly_dark",
        title=dict(text=f"{pair.upper()} — {subtitle}", font=dict(color="#e8edf7", size=14)),
        paper_bgcolor="#0e1117",
        plot_bgcolor="#161b22",
        height=min(CHART_HEIGHT_PX, 420),
        font=dict(color="#e8edf7", size=12),
    )
    return fig


def _style_fig(fig: go.Figure, uirev: str, *, height: int = CHART_HEIGHT_PX) -> None:
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0e1117",
        plot_bgcolor="#161b22",
        font=dict(color="#e8edf7", size=11),
        height=int(height),
        margin=dict(l=48, r=16, t=44, b=36),
        showlegend=False,
        hovermode="x unified",
        dragmode="pan",
        uirevision=uirev,
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor="rgba(148,163,184,0.18)",
        zeroline=False,
        rangeslider_visible=False,
    )
    fig.update_yaxes(showgrid=True, gridcolor="rgba(148,163,184,0.18)", zeroline=False)


def apply_dark_shell() -> None:
    st.markdown(
        f"""
<style>
  html, body, [data-testid="stAppViewContainer"], .main {{
    background:#0e1117 !important;
    color:#ecf2f9;
  }}
  [data-testid="stSidebar"] {{
    background: #f8fafc !important;
    border-right: 1px solid #e2e8f0;
    color: #0f172a !important;
  }}
  [data-testid="stSidebar"] [data-testid="stSidebarContent"] {{
    color: #0f172a !important;
  }}
  /* Сайдбар: тёмный текст на светлом фоне */
  [data-testid="stSidebar"] h1,
  [data-testid="stSidebar"] h2,
  [data-testid="stSidebar"] h3,
  [data-testid="stSidebar"] h4,
  [data-testid="stSidebar"] p,
  [data-testid="stSidebar"] li,
  [data-testid="stSidebar"] strong,
  [data-testid="stSidebar"] .stMarkdown,
  [data-testid="stSidebar"] .stMarkdown p,
  [data-testid="stSidebar"] span[data-testid="stMarkdownContainer"],
  [data-testid="stSidebar"] span[data-testid="stMarkdownContainer"] p {{
    color: #0f172a !important;
  }}
  [data-testid="stSidebar"] [data-testid="stWidgetLabel"] p,
  [data-testid="stSidebar"] [data-testid="stWidgetLabel"] span,
  [data-testid="stSidebar"] label,
  [data-testid="stSidebar"] label p,
  [data-testid="stSidebar"] label span,
  [data-testid="stSidebar"] .stRadio label,
  [data-testid="stSidebar"] .stRadio div[role="radiogroup"] label,
  [data-testid="stSidebar"] .stRadio div[role="radiogroup"] label span,
  [data-testid="stSidebar"] .stCheckbox label,
  [data-testid="stSidebar"] .stCheckbox label span,
  [data-testid="stSidebar"] .stSlider label p,
  [data-testid="stSidebar"] [data-testid="stMetricLabel"],
  [data-testid="stSidebar"] [data-testid="stMetricValue"] {{
    color: #1e293b !important;
  }}
  [data-testid="stSidebar"] .stCaption,
  [data-testid="stSidebar"] [data-testid="stCaption"],
  [data-testid="stSidebar"] small {{
    color: #475569 !important;
  }}
  [data-testid="stSidebar"] input,
  [data-testid="stSidebar"] textarea {{
    color: #0f172a !important;
    caret-color: #0f172a !important;
    background-color: #ffffff !important;
    border-color: #cbd5e1 !important;
  }}
  [data-testid="stSidebar"] [data-baseweb="select"] span,
  [data-testid="stSidebar"] [data-baseweb="select"] > div,
  [data-testid="stSidebar"] [data-baseweb="tag"] {{
    color: #0f172a !important;
  }}
  [data-testid="stSidebar"] button {{
    color: #0f172a !important;
  }}
  [data-testid="stSidebar"] [data-testid="stExpander"] summary,
  [data-testid="stSidebar"] [data-testid="stExpander"] summary p {{
    color: #0f172a !important;
  }}
  /* Компактная сетка: всё видно при масштабе 100% */
  .block-container {{
    padding-top: 0.5rem !important;
    padding-bottom: 0.35rem !important;
    max-width: min(1180px, 100%) !important;
  }}
  [data-testid="stHeader"] {{ background: transparent; }}
  h1 {{
    font-size: clamp(1.15rem, 2vw, 1.45rem) !important;
    font-weight: 600 !important;
    margin: 0 0 0.35rem 0 !important;
    padding: 0 !important;
    line-height: 1.2 !important;
  }}
  [data-testid="stVerticalBlock"] > div {{
    gap: 0.35rem;
  }}
  div[data-testid="stMetric"] {{
    padding: 0.3rem 0.5rem;
    background: rgba(22,27,34,0.5);
    border-radius: 6px;
    border: 1px solid rgba(55,65,81,0.5);
  }}
  div[data-testid="stMetricValue"] {{
    font-size: 1.05rem !important;
  }}
  div[data-testid="stMetricLabel"] {{
    font-size: 0.72rem !important;
  }}
  .stCaption, [data-testid="stCaption"] {{
    font-size: 0.78rem !important;
    margin-top: 0.15rem !important;
    margin-bottom: 0.1rem !important;
  }}
  .stAlert {{ padding: 0.5rem 0.65rem; }}
</style>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard(
    chart_pair: str,
    cum_pair: str,
    tf_key: str,
    *,
    div_enabled: bool,
    div_pivot_left: int,
    div_pivot_right: int,
    div_min_bars: int,
    div_show_lines: bool,
    show_futures_oi: bool,
    show_vwap: bool,
    show_volume: bool,
    show_price_ma: bool = True,
    price_ma_length: int = 20,
    show_willy: bool = False,
    willy_length: int = 21,
    willy_ema_length: int = 13,
    show_atr: bool = False,
    atr_period: int = 14,
    show_compression: bool = False,
    compression_params: CompressionParams | None = None,
    panel_order: list[str] | None = None,
) -> None:
    ch_sym = _norm_sym(chart_pair)
    cd_sym = _norm_sym(cum_pair)

    df_ch, _ch = load_klines_frame(ch_sym, tf_key)
    if ch_sym == cd_sym:
        df_cd, _cd_src = df_ch, _ch
    else:
        df_cd, _cd_src = load_klines_frame(cd_sym, tf_key)

    c_h = chart_pair.upper()
    c_d = cum_pair.upper()
    same_cum_as_chart = ch_sym == cd_sym
    c1, c2, c3, c4, c5, c6 = st.columns(6)

    if df_ch.empty:
        c1.metric(f"Close ({c_h})", "—")
        c2.metric(f"Объём · бар ({c_h})", "—")
        c3.metric("VWAP", "—")
        c4.metric(f"Кумулятив δ ({c_d})", "—")
        c5.metric("OI (USDT-M)", "—")
        c6.metric("Баров (график)", "0")
        st.warning(f"Не удалось загрузить свечи для **{c_h}**. Выберите другую пару графика.")
        err = last_binance_error()
        if err:
            st.caption(
                f"Binance API: `{err}`. На Render часто нужен прокси — задайте "
                "`BINANCE_HTTP_PROXY` или `HTTPS_PROXY` в Environment."
            )
        fig = build_figure_bars(
            pd.DataFrame(),
            chart_pair=chart_pair,
            cum_pair=cum_pair,
            tf_key=tf_key,
            divergence_divs=None,
            divergence_show_lines=div_show_lines,
            show_futures_oi=False,
            show_vwap=False,
            show_volume=False,
            show_price_ma=False,
            price_ma_length=int(price_ma_length),
            show_willy=False,
            panel_order=panel_order,
        )
    else:
        blended = overlay_cum_delta_on_chart(df_ch, df_cd)
        if df_cd.empty:
            st.warning(f"Не удалось загрузить серию δ для **{c_d}** — линия кумулятивной δ временно без данных.")

        df_oi = pd.DataFrame()
        oi_src = "—"
        if show_futures_oi:
            df_oi, oi_src = load_open_interest_frame(ch_sym, tf_key)
            if df_oi.empty:
                st.warning(f"Open Interest (USDT-M) для **{c_h}**: {oi_src}")
        blended = overlay_open_interest(blended, df_oi if show_futures_oi else pd.DataFrame())
        if show_vwap:
            blended = attach_daily_utc_vwap(blended)
        else:
            blended = blended.copy()
            blended["vwap"] = float("nan")

        cd_last = blended["cum_delta_pick"].iloc[-1]
        try:
            cd_last_f = float(cd_last)
        except (TypeError, ValueError):
            cd_last_f = float("nan")

        oi_last = blended["open_interest_pick"].iloc[-1]
        try:
            oi_last_f = float(oi_last)
        except (TypeError, ValueError):
            oi_last_f = float("nan")

        c1.metric(f"Close · {c_h}", f"{float(blended['close'].iloc[-1]):,.2f}")
        vol_last = blended["volume_base"].iloc[-1]
        try:
            vol_last_f = float(vol_last)
        except (TypeError, ValueError):
            vol_last_f = float("nan")
        vol_label = "—" if vol_last_f != vol_last_f else f"{vol_last_f:,.4f}"
        c2.metric(
            f"Объём · бар · {c_h}" if show_volume else "Объём (выкл.)",
            vol_label if show_volume else "—",
        )
        vw_last = blended["vwap"].iloc[-1]
        try:
            vw_last_f = float(vw_last)
        except (TypeError, ValueError):
            vw_last_f = float("nan")
        vw_label = "—" if (not show_vwap or vw_last_f != vw_last_f) else f"{vw_last_f:,.2f}"
        c3.metric("VWAP · UTC день" if show_vwap else "VWAP (выкл.)", vw_label)
        cd_label = "—" if cd_last_f != cd_last_f else f"{cd_last_f:+.2f}"
        c4.metric(f"Кумулятив δ · {c_d}", cd_label)
        oi_label = "—" if (not show_futures_oi or oi_last_f != oi_last_f) else f"{oi_last_f:,.2f}"
        c5.metric(f"OI · {c_h}" if show_futures_oi else "OI (выкл.)", oi_label)
        c6.metric("Баров (ось времени)", len(blended))
        div_result: list[PriceCumDeltaDivergence] | None = None
        div_short_bars = False
        if div_enabled:
            need = div_pivot_left + div_pivot_right + max(div_min_bars, 3) + 2
            if len(blended) > need:
                nh = blended["high"].to_numpy(dtype=np.float64)
                nl = blended["low"].to_numpy(dtype=np.float64)
                nc = pd.to_numeric(blended["cum_delta_pick"], errors="coerce").to_numpy(dtype=np.float64)
                div_result = detect_price_vs_cum_divergences(
                    nh,
                    nl,
                    nc,
                    pivot_left=div_pivot_left,
                    pivot_right=div_pivot_right,
                    min_bars_between=div_min_bars,
                )
            else:

                div_short_bars = True

        compression_zones: list[CompressionZone] = []
        if show_compression and compression_params is not None:
            compression_zones = detect_compression_zones(blended, compression_params, use_closed_bar=True)

        fig = build_figure_bars(
            blended,
            chart_pair=chart_pair,
            cum_pair=cum_pair,
            tf_key=tf_key,
            divergence_divs=div_result,
            divergence_show_lines=div_show_lines,
            show_futures_oi=show_futures_oi,
            show_vwap=show_vwap,
            show_volume=show_volume,
            show_price_ma=show_price_ma,
            price_ma_length=int(price_ma_length),
            show_willy=show_willy,
            willy_length=willy_length,
            willy_ema_length=willy_ema_length,
            show_atr=show_atr,
            atr_period=int(atr_period),
            show_compression=show_compression,
            compression_zones=compression_zones if show_compression else None,
            compression_pivot_left=int(compression_params.pivot_left) if compression_params else 5,
            compression_pivot_right=int(compression_params.pivot_right) if compression_params else 5,
            compression_min_pivots=int(compression_params.min_pivots) if compression_params else 3,
            panel_order=panel_order,
        )
        cap = (
            f"Свечи: **{c_h}**"
            + (" · **объём** (базовый актив)" if show_volume else "")
            + f" · линия кумулятивной δ (та же ось времени): **{c_d}**. "
            "δ = прокси `2×taker_buy_base − volume` по каждому бару второй пары; до **500** свечей каждому запросу."
        )
        if show_vwap:
            cap += " **VWAP:** типичная цена (H+L+C)/3, вес объёма базового актива, **сброс в 00:00 UTC** каждый день."
        if show_futures_oi:
            cap += (
                f" **Open Interest** по **{c_h}** (USDT-M perpetual): `fapi.binance.com/futures/data/openInterestHist`, "
                f"период **{TF_TITLES_RU.get(tf_key, tf_key)}** — {oi_src}."
            )
        if same_cum_as_chart:
            cap += " **Режим:** кум. δ по **той же паре**, что и OHLC (один запрос klines)."
        if div_enabled:

            if div_short_bars:
                cap += (
                    f" Дивергенции: недостаточно баров (нужно >**{need}** при L/R={div_pivot_left}/{div_pivot_right}, "
                    f"min Δi={div_min_bars})."
                )
            elif div_result:
                n_br = sum(1 for x in div_result if x.kind == "bearish")
                n_bl = sum(1 for x in div_result if x.kind == "bullish")
                cap += (
                    f" Дивергенции (swing high/low vs кум. δ): медвежьих **{n_br}**, бычьих **{n_bl}** "
                    f"(L/R pivot {div_pivot_left}/{div_pivot_right}, min Δi≥{div_min_bars})."
                )
            else:

                cap += " Дивергенции: по текущим параметрам сигналов нет."

        if show_compression:
            if compression_zones:
                last_z = compression_zones[-1]
                cap += (
                    f" **Price Compression:** зон **{len(compression_zones)}**; "
                    f"последняя Score **{last_z.score:.0f}**, ratio **{last_z.compression_ratio:.2f}**, "
                    f"↑{last_z.upper_price:.4g} ↓{last_z.lower_price:.4g}, "
                    f"формирование **{last_z.formation_bars}** бар., "
                    f"касания ↑{last_z.upper_touches} ↓{last_z.lower_touches}."
                )
            else:
                cap += " **Price Compression:** по текущим параметрам зон сжатия нет."

        st.caption(cap)

    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Свечи: сначала Binance Spot `GET /api/v3/klines` (~60 с кэш); если пары **нет на spot** "
        "(часто у perpetual-only тикеров) — **USDT-M** `GET /fapi/v1/klines`. "
        "Open Interest: `openInterestHist` · список пар: файл `data/cache/` (6 ч) + 24h ticker. "
        "VWAP — на клиенте по тем же свечам при включении. Индикаторы: сайдбар → **Индикаторы**. "
        "Страница автообновляется каждые **5 минут**."
    )


def main() -> None:
    st.set_page_config(
        page_title="Order Flow Analyzer",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    auth_user = render_auth_gate()
    require_page_access(PAGE_MAIN, auth_user)

    apply_dark_shell()
    if auth_user:
        st.caption(f"Добро пожаловать, **{auth_user}**")
    st.title("Order Flow Analyzer")

    with st.sidebar:
        render_auth_sidebar(auth_user)
        _init_main_sidebar_defaults()
        apply_pending_preset_before_widgets()
        st.markdown("**Binance Spot · USDT**")
        st.caption("**Пары и таймфрейм** — инструменты; **Индикаторы** — что показать на графике (вкл./выкл.).")

        symbols = cached_spot_usdt_symbol_list()

        with st.expander("Пары и таймфрейм", expanded=True):
            tf_key = st.radio(
                "Таймфрейм (обе серии и период Open Interest)",
                options=list(KLINES_API.keys()),
                format_func=lambda k: TF_TITLES_RU[k],
                key="main_tf_key",
                help="Интервал klines у обеих пар и `period` для fapi openInterestHist. Источник свечей: Spot, при необходимости USDT-M.",
            )
            period_oi = KLINES_API[tf_key]
            if "oi_rebuild_token" not in st.session_state:
                st.session_state["oi_rebuild_token"] = 0
            oi_rebuild_col, oi_age_col = st.columns(2)
            with oi_rebuild_col:
                if st.button("Обновить список OI", key=f"oi_rebuild_{tf_key}", use_container_width=True):
                    clear_oi_symbol_cache(period_oi)
                    cached_usdtm_symbols_with_oi.clear()
                    st.session_state["oi_rebuild_token"] = int(st.session_state["oi_rebuild_token"]) + 1
                    st.rerun()
            with oi_age_col:
                _age = oi_cache_age_sec(period_oi)
                if _age is not None:
                    st.caption(f"Кэш OI: **{_age / 3600:.1f} ч** назад")
                else:
                    st.caption("Кэш OI: ещё не сохранён")
            oi_syms = list(
                cached_usdtm_symbols_with_oi(period_oi, int(st.session_state["oi_rebuild_token"]))
            )
            if not oi_syms:
                st.warning(
                    "Не удалось получить список пар с OI (сеть или Binance fapi). "
                    "Используется полный список spot USDT для выбора пар."
                )
                err = last_binance_error()
                if err:
                    st.caption(
                        f"Binance fapi: `{err}`. Для Open Interest на Render часто нужен "
                        "`BINANCE_HTTP_PROXY` или `HTTPS_PROXY` в Environment."
                    )
            if "cb_restrict_to_oi" not in st.session_state:
                st.session_state["cb_restrict_to_oi"] = bool(oi_syms)
            restrict_to_oi = st.checkbox(
                "Только пары с Open Interest (USDT-M)",
                disabled=not oi_syms,
                key="cb_restrict_to_oi",
                help=(
                    "Выпадающие списки OHLC и δ — только символы, по которым для выбранного таймфрейма "
                    "есть непустая история `openInterestHist`. Свечи: сначала **Spot**, при отсутствии пары — **USDT-M fapi**."
                ),
            )
            pair_universe = oi_syms if (restrict_to_oi and oi_syms) else symbols

            show_oi_table = st.checkbox(
                f"Показать все пары с OI для {TF_TITLES_RU.get(tf_key, tf_key)} ({period_oi}) — {len(oi_syms)}",
                value=False,
                key="cb_show_oi_symbol_table",
            )
            if show_oi_table:
                with st.container(border=True):
                    if not oi_syms:
                        st.caption("Список пуст — проверьте соединение или попробуйте позже.")
                    else:
                        oi_needle = st.text_input(
                            "Поиск по тикеру",
                            value="",
                            key="oi_main_list_filt",
                            placeholder="BTC, 1000…",
                        )
                        oi_show = _filter_symbols(oi_syms, oi_needle)
                        cap_n = 800
                        if len(oi_show) > cap_n:
                            st.caption(f"Показаны **{cap_n}** из **{len(oi_show)}** — уточните поиск.")
                            oi_show = oi_show[:cap_n]
                        st.dataframe(
                            pd.DataFrame({"Символ": oi_show}),
                            use_container_width=True,
                            height=min(420, 48 + 28 * min(len(oi_show), 24)),
                            hide_index=True,
                        )

            st.markdown("**Свечной график (OHLC)**")
            filt_chart = st.text_input(
                "Фильтр · пара графика",
                value="",
                placeholder="BTC, SOL…",
                key="filt_ohlc",
                help="Список символов сужается по подстроке.",
            )
            opt_chart = _filter_symbols(pair_universe, filt_chart)
            if not opt_chart:
                opt_chart = pair_universe
                st.warning("Фильтр графика: ничего не найдено — показан полный список для текущего режима.")
            opt_chart = _selectbox_options_with_saved(
                opt_chart,
                session_key="sb_chart_pair",
                fallback="BTCUSDT",
                universe=pair_universe,
            )
            chart_pair = st.selectbox(
                "Пара OHLC",
                options=opt_chart,
                key="sb_chart_pair",
                help="Свечи и цена только по этой паре.",
            )

            cum_delta_same_as_chart = st.checkbox(
                "Кум. δ автоматически по выбранной паре графика",
                key="cb_cum_same_chart",
                help="Если включено, линия кумулятивной δ строится по **той же** USDT-паре, что и OHLC; отдельный выбор пары δ скрыт.",
            )

            if cum_delta_same_as_chart:
                cum_pair = chart_pair
                st.caption("Пара δ = **та же**, что OHLC · один REST-запрос klines на символ.")
            else:
                st.markdown("**Кумулятивная δ** (другая пара)")
                filt_cum = st.text_input(
                    "Фильтр · пара δ",
                    value="",
                    placeholder="ETH, BTC…",
                    key="filt_cum",
                    help="Можно выбрать другую монету для линии кумулятива при том же таймфрейме.",
                )
                opt_cum = _filter_symbols(pair_universe, filt_cum)
                if not opt_cum:
                    opt_cum = pair_universe
                    st.warning("Фильтр δ: ничего не найдено — показан полный список для текущего режима.")
                opt_cum = _selectbox_options_with_saved(
                    opt_cum,
                    session_key="sb_cum_pair",
                    fallback="BTCUSDT",
                    universe=pair_universe,
                )
                cum_pair = st.selectbox(
                    "Пара кумулятивной δ",
                    options=opt_cum,
                    key="sb_cum_pair",
                    help="Прокси-дельта суммируется по свечам этой пары и выводится по временным меткам графика.",
                )

            mode_note = (
                f"пары с OI: **{len(oi_syms)}** · в выборе: **{len(pair_universe)}**"
                if restrict_to_oi and oi_syms
                else f"spot USDT: **{len(symbols)}** пар в базе"
            )
            st.caption(
                f"{mode_note} · автообновление экрана: **5 мин** · "
                "кэш OI на диске: **6 ч** (повторный запуск — мгновенно)"
            )

            st.caption("Обе серии — `GET /api/v3/klines`; объединение по `open_time` свечей графика.")

        with st.expander("Индикаторы", expanded=True):
            st.caption("Каждый пункт независимо включает или скрывает элемент на графике.")

            if "panel_order" not in st.session_state:
                st.session_state["panel_order"] = list(DEFAULT_PANEL_ORDER)
            st.session_state["panel_order"] = [
                k for k in st.session_state["panel_order"] if k in PANEL_LABELS_RU
            ]
            for k in DEFAULT_PANEL_ORDER:
                if k not in st.session_state["panel_order"]:
                    st.session_state["panel_order"].append(k)

            st.markdown("**Порядок панелей** · ↑ выше, ↓ ниже")
            # Перестановка выполняется в on_click-колбэке, который Streamlit
            # вызывает ДО следующего прохода скрипта. Это даёт два эффекта:
            # 1) к моменту отрисовки виджетов session_state["panel_order"]
            #    уже актуален → подписи списка и порядок панелей графика
            #    синхронизированы (нет «отставания на один клик»);
            # 2) мы не зовём st.rerun() из тела скрипта, поэтому Streamlit
            #    не «обрывает» текущий прогон до отрисовки нижних виджетов
            #    (чекбоксы/слайдеры Williams %R и дивергенций) и не сбрасывает
            #    их session_state в дефолтные значения.
            def _panel_swap(i: int, j: int) -> None:
                ord_ = st.session_state.get("panel_order")
                if not ord_:
                    return
                if 0 <= i < len(ord_) and 0 <= j < len(ord_):
                    ord_[i], ord_[j] = ord_[j], ord_[i]

            def _panel_reset() -> None:
                st.session_state["panel_order"] = list(DEFAULT_PANEL_ORDER)

            _order = st.session_state["panel_order"]
            for _i, _k in enumerate(list(_order)):
                _c1, _c2, _c3 = st.columns([5, 1, 1])
                _c1.write(f"{_i + 1}. {PANEL_LABELS_RU.get(_k, _k)}")
                _c2.button(
                    "↑",
                    key=f"pn_up_{_k}",
                    disabled=(_i == 0),
                    use_container_width=True,
                    on_click=_panel_swap,
                    args=(_i, _i - 1),
                )
                _c3.button(
                    "↓",
                    key=f"pn_dn_{_k}",
                    disabled=(_i == len(_order) - 1),
                    use_container_width=True,
                    on_click=_panel_swap,
                    args=(_i, _i + 1),
                )
            st.button(
                "Сбросить порядок",
                key="pn_reset",
                use_container_width=True,
                on_click=_panel_reset,
            )
            st.markdown("---")

            show_volume = st.checkbox(
                "Объём по свечам (гистограмма под OHLC)",
                key="cb_show_volume",
                help="Объём базового актива по барам пары графика; данные из тех же spot klines, что и свечи.",
            )
            show_vwap = st.checkbox(
                "VWAP на OHLC",
                key="cb_show_vwap",
                help="Типичная цена (high+low+close)/3, вес — объём базового актива; накопление внутри календарного дня **UTC**, затем сброс.",
            )
            show_price_ma = st.checkbox(
                "Скользящая средняя по цене (SMA close)",
                key="cb_show_price_ma",
                help="Линия SMA по close на верхней ценовой панели.",
            )
            price_ma_length = st.slider(
                "SMA · период",
                min_value=2,
                max_value=200,
                key="sl_price_ma_length",
                disabled=not show_price_ma,
            )
            show_futures_oi = st.checkbox(
                "Open Interest (Binance USDT-M)",
                key="cb_show_oi",
                help="Нижняя панель: `fapi.binance.com/futures/data/openInterestHist` по **той же паре**, что OHLC; "
                "`period` совпадает с таймфреймом свечей.",
            )

            st.markdown("---")
            st.markdown("**ATR (Average True Range)**")
            show_atr = st.checkbox(
                "ATR (нижняя панель)",
                key="cb_show_atr",
                help="Средний истинный диапазон (Wilder RMA). Показывает волатильность в единицах цены.",
            )
            atr_period = st.slider(
                "ATR · период",
                min_value=2,
                max_value=100,
                key="sl_atr_period",
            )

            st.markdown("---")
            st.markdown("**Williams %R + EMA**")
            show_willy = st.checkbox(
                "Williams %R с EMA (нижняя панель)",
                key="cb_show_willy",
                help="Период %R по умолчанию 21, EMA 13. Уровни −20 / −50 / −80; "
                "заливка верхней зоны при EMA > −20, нижней — при EMA < −80.",
            )
            willy_length = st.slider(
                "%R · период (length)",
                min_value=5,
                max_value=80,
                key="sl_willy_length",
            )
            willy_ema_length = st.slider(
                "EMA · длина (over %R)",
                min_value=2,
                max_value=60,
                key="sl_willy_ema_length",
            )

            st.markdown("---")
            st.markdown("**Дивергенции** · цена ↔ кумулятивная δ")
            div_enabled = st.checkbox(
                "Включить дивергенции (медвежья: HH + LH по δ; бычья: LL + HL по δ)",
                key="cb_div_enabled",
                help="Фрактальные swing high/low; маркер на баре подтверждения.",
            )
            div_show_lines = st.checkbox(
                "Пунктир между двумя свингами (цена и δ)",
                key="cb_div_show_lines",
            )
            div_pivot_left = st.slider("Сила pivot · слева (баров)", 2, 12, key="sl_div_pivot_left")
            div_pivot_right = st.slider("Сила pivot · справа (баров)", 2, 12, key="sl_div_pivot_right")
            div_min_bars = st.slider("Мин. расстояние между соседними свингами", 5, 80, key="sl_div_min_bars")

            st.markdown("---")
            st.markdown("**Price Compression** · сужение перед импульсом")
            show_compression = st.checkbox(
                "Показать зоны сжатия (Pivot + регрессия)",
                key="cb_show_compression",
                help="Подтверждённые Pivot High/Low, границы по МНК, без ATR/Bollinger. "
                "Рекомендуемые pivot: 15m → 3/3, 1h → 5/5.",
            )
            pc_tf_hint = default_compression_params_for_tf(tf_key)
            if st.button("Pivot по таймфрейму", key="btn_pc_tf_pivot", use_container_width=True):
                st.session_state["sl_pc_pivot_left"] = pc_tf_hint.pivot_left
                st.session_state["sl_pc_pivot_right"] = pc_tf_hint.pivot_right
                st.rerun()
            st.caption(
                f"Для **{TF_TITLES_RU.get(tf_key, tf_key)}**: Pivot L/R = "
                f"**{pc_tf_hint.pivot_left}** / **{pc_tf_hint.pivot_right}**"
            )
            show_pc_params = show_compression and st.checkbox(
                "Показать параметры Price Compression",
                value=bool(show_compression),
                key="cb_show_pc_params",
            )
            if show_pc_params:
                with st.container(border=True):
                    if st.button(
                        "Рекомендуемые параметры",
                        key="btn_pc_recommended",
                        use_container_width=True,
                        help="Сброс к рекомендуемым: pivot 3, касания 3, ratio 0.65, slope 0.08, окно 120, Score 75.",
                    ):
                        apply_recommended_compression_session_state(
                            st.session_state,
                            "sl_pc",
                            pivot_left=pc_tf_hint.pivot_left,
                            pivot_right=pc_tf_hint.pivot_right,
                        )
                        st.rerun()
                    st.slider("Pivot Left", 2, 12, key="sl_pc_pivot_left")
                    st.slider("Pivot Right", 2, 12, key="sl_pc_pivot_right")
                    st.slider(
                        "Мин. Pivot на границу",
                        2,
                        8,
                        key="sl_pc_min_pivots",
                        help=RECOMMENDED_COMPRESSION_HELP_RU["min_pivots"],
                    )
                    st.slider(
                        "Мин. касаний (верх и низ)",
                        2,
                        8,
                        key="sl_pc_min_touches",
                        help=RECOMMENDED_COMPRESSION_HELP_RU["min_touches"],
                    )
                    st.slider(
                        "Период сравнения ширины (баров)",
                        8,
                        60,
                        key="sl_pc_lookback",
                        help=RECOMMENDED_COMPRESSION_HELP_RU["lookback_bars"],
                    )
                    st.slider(
                        "Макс. коэффициент сжатия (Current/Previous)",
                        min_value=0.40,
                        max_value=0.95,
                        step=0.01,
                        key="sl_pc_max_ratio",
                        help=RECOMMENDED_COMPRESSION_HELP_RU["max_compression_ratio"],
                    )
                    st.slider(
                        "Макс. наклон середины (%/бар)",
                        min_value=0.06,
                        max_value=0.15,
                        step=0.01,
                        key="sl_pc_max_slope",
                        help=RECOMMENDED_COMPRESSION_HELP_RU["max_mid_slope_pct_per_bar"],
                    )
                    st.slider(
                        "Допуск касания (% ширины)",
                        min_value=0.04,
                        max_value=0.25,
                        step=0.01,
                        key="sl_pc_touch_tol",
                        help=RECOMMENDED_COMPRESSION_HELP_RU["touch_tolerance"],
                    )
                    st.slider(
                        "Окно анализа (баров)",
                        60,
                        300,
                        key="sl_pc_analysis",
                        help=RECOMMENDED_COMPRESSION_HELP_RU["analysis_bars"],
                    )
                    st.slider(
                        "Мин. Compression Score",
                        30,
                        90,
                        key="sl_pc_min_score",
                        help=RECOMMENDED_COMPRESSION_HELP_RU["min_score"],
                    )

            compression_params = compression_params_from_session(st.session_state, "sl_pc")

        render_main_presets_sidebar(auth_user)
    frag = getattr(st, "fragment", None)
    if frag is not None:
        @frag(run_every=REFRESH_SCREEN)
        def _live() -> None:
            render_dashboard(
                chart_pair,
                cum_pair,
                tf_key,
                div_enabled=div_enabled,
                div_pivot_left=div_pivot_left,
                div_pivot_right=div_pivot_right,
                div_min_bars=div_min_bars,
                div_show_lines=div_show_lines,
                show_futures_oi=show_futures_oi,
                show_vwap=show_vwap,
                show_volume=show_volume,
                show_price_ma=show_price_ma,
                price_ma_length=int(price_ma_length),
                show_willy=show_willy,
                willy_length=int(willy_length),
                willy_ema_length=int(willy_ema_length),
                show_atr=show_atr,
                atr_period=int(atr_period),
                show_compression=show_compression,
                compression_params=compression_params,
                panel_order=list(st.session_state.get("panel_order", DEFAULT_PANEL_ORDER)),
            )

        _live()
    else:
        st.warning("Обновите Streamlit до версии с `st.fragment` для автообновления.")
        render_dashboard(
            chart_pair,
            cum_pair,
            tf_key,
            div_enabled=div_enabled,
            div_pivot_left=div_pivot_left,
            div_pivot_right=div_pivot_right,
            div_min_bars=div_min_bars,
            div_show_lines=div_show_lines,
            show_futures_oi=show_futures_oi,
            show_vwap=show_vwap,
            show_volume=show_volume,
            show_price_ma=show_price_ma,
            price_ma_length=int(price_ma_length),
            show_willy=show_willy,
            willy_length=int(willy_length),
            willy_ema_length=int(willy_ema_length),
            show_atr=show_atr,
            atr_period=int(atr_period),
            show_compression=show_compression,
            compression_params=compression_params,
            panel_order=list(st.session_state.get("panel_order", DEFAULT_PANEL_ORDER)),
        )


if __name__ == "__main__":
    main()
