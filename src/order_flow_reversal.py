"""
Order Flow Reversal Scanner — rolling-window метрики, пороговые сигналы, скоринг,
форвард-доходности и max adverse excursion (исследовательский слой без Streamlit).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ReversalThresholds:
    """Пороги кандидата (после расчёта rolling-признаков)."""

    min_cum_delta_change: float
    """Мин. прирост кум. δ за N баров (абсолютный)."""

    max_oi_pct: float
    """OI % за окно <= этого порога (отрицательное = отток, напр. -0.15)."""

    max_price_pct: float
    """Изменение цены за окно <= этого % (слабая цена; 0 или отрицательное)."""


def klines_to_frame_with_delta(klines: pd.DataFrame) -> pd.DataFrame:
    out = klines.copy()
    if "taker_buy_base" not in out.columns:
        out["taker_buy_base"] = 0.0
    v = out["volume_base"].to_numpy(dtype=np.float64)
    tb = out["taker_buy_base"].to_numpy(dtype=np.float64)
    out["delta"] = 2.0 * tb - v
    out["cum_delta"] = out["delta"].cumsum()
    out["timestamp"] = pd.to_datetime(out["open_time"], unit="ms", utc=True)
    return out.sort_values("timestamp").reset_index(drop=True)


def merge_open_interest_on_klines(df_k: pd.DataFrame, df_oi: pd.DataFrame) -> pd.DataFrame:
    """Стыковка OI по timestamp (как overlay_open_interest в app)."""
    out = df_k.copy()
    if df_oi.empty or "timestamp" not in df_k.columns:
        out["open_interest"] = np.nan
        return out
    # klines часто UTC-aware, OI из fapi — naive ms; merge требует одинаковой tz.
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    oi = df_oi[["timestamp", "open_interest"]].copy()
    oi["timestamp"] = pd.to_datetime(oi["timestamp"], utc=True)
    oi = oi.sort_values("timestamp").drop_duplicates("timestamp")
    m = out[["timestamp"]].merge(oi, on="timestamp", how="left")
    ser = pd.to_numeric(m["open_interest"], errors="coerce")
    ser = ser.interpolate(limit_area="inside").ffill().bfill()
    out["open_interest"] = ser.to_numpy(dtype=np.float64, copy=False)
    return out


def compute_rolling_features(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """
    Для бара i: окно из N свечей, сравнение с баром i−N (N баров назад).
    price_pct, Δкумδ, % OI, сжатие тел, vol spike, нижняя тень, struct break.
    """
    if n < 3:
        raise ValueError("n must be >= 3")
    out = df.copy()
    o = out["open"].to_numpy(dtype=np.float64)
    h = out["high"].to_numpy(dtype=np.float64)
    l = out["low"].to_numpy(dtype=np.float64)
    c = out["close"].to_numpy(dtype=np.float64)
    cd = out["cum_delta"].to_numpy(dtype=np.float64)
    oi = out["open_interest"].to_numpy(dtype=np.float64)
    vol = out["volume_base"].to_numpy(dtype=np.float64)
    k = n

    prev_c = np.roll(c, k)
    prev_c[:k] = np.nan
    out["price_pct_N"] = (c / (prev_c + 1e-12) - 1.0) * 100.0

    prev_cd = np.roll(cd, k)
    prev_cd[:k] = np.nan
    out["cum_delta_change_N"] = cd - prev_cd

    prev_oi = np.roll(oi, k)
    prev_oi[:k] = np.nan
    out["oi_pct_N"] = np.where(np.abs(prev_oi) > 1e-9, (oi - prev_oi) / (np.abs(prev_oi) + 1e-12) * 100.0, np.nan)

    body = np.abs(c - o)
    bod_roll = pd.Series(body).rolling(n, min_periods=n).mean()
    long_w = min(2 * n, len(out))
    bod_long = pd.Series(body).rolling(long_w, min_periods=long_w).mean()
    out["body_compression"] = (1.0 - (bod_roll / (bod_long + 1e-12))).clip(-3.0, 3.0)

    vma = pd.Series(vol).rolling(n, min_periods=n).mean()
    out["vol_spike"] = (vol / (vma + 1e-12)).replace([np.inf, -np.inf], np.nan)

    lw = (np.minimum(o, c) - l) / (h - l + 1e-12)
    out["lower_wick_ratio"] = lw
    out["lower_wick_roll"] = pd.Series(lw).rolling(n, min_periods=n).mean()

    s_low = pd.Series(l)
    rl = s_low.rolling(n, min_periods=n).min()
    rl_prev = rl.shift(n)
    out["struct_break"] = ((rl < rl_prev * 0.998) & rl_prev.notna()).astype(np.float64)

    bear = (c < o).astype(np.float64)
    out["bear_frac_N"] = pd.Series(bear).rolling(n, min_periods=n).mean()

    return out


def detect_candidates(df: pd.DataFrame, thr: ReversalThresholds) -> pd.Series:
    ddc = pd.to_numeric(df["cum_delta_change_N"], errors="coerce")
    oip = pd.to_numeric(df["oi_pct_N"], errors="coerce")
    px = pd.to_numeric(df["price_pct_N"], errors="coerce")
    ok = (
        (ddc > float(thr.min_cum_delta_change))
        & oip.notna()
        & (oip <= float(thr.max_oi_pct))
        & px.notna()
        & (px <= float(thr.max_price_pct))
    )
    return ok.fillna(False)


# Пресеты силы: локальные rolling-квантили (адаптивно к рынку), без ручных порогов.
_STRENGTH_CFG: dict[str, dict[str, float | int]] = {
    "weak": {"roll": 72, "cum_q": 0.56, "oi_q": 0.42, "px_q": 0.62, "cum_mult": 0.97},
    "normal": {"roll": 96, "cum_q": 0.64, "oi_q": 0.34, "px_q": 0.52, "cum_mult": 1.0},
    "strong": {"roll": 120, "cum_q": 0.72, "oi_q": 0.26, "px_q": 0.42, "cum_mult": 1.03},
    "extreme": {"roll": 144, "cum_q": 0.80, "oi_q": 0.18, "px_q": 0.32, "cum_mult": 1.06},
}


def default_window_bars(tf_key: str) -> int:
    """Длина окна N (скрыта от пользователя): привязка к ТФ."""
    return {"5m": 48, "15m": 24, "2h": 18, "1h": 20, "4h": 14, "1d": 6}.get(tf_key, 20)


_TF_BAR_HOURS: dict[str, float] = {
    "5m": 5.0 / 60.0,
    "15m": 0.25,
    "2h": 2.0,
    "1h": 1.0,
    "4h": 4.0,
    "1d": 24.0,
}


def divergence_duration_bars(tf_key: str, hours: float = 24.0) -> int:
    """
    Сколько баров выбранного ТФ покрывают не менее `hours` часов (для проверки «расхождение ≥24ч»).
    Напр. 1h → 24 бара, 15m → 96, 4h → 6, 1d → 1.
    """
    bar_h = _TF_BAR_HOURS.get(tf_key, 1.0)
    if bar_h <= 0:
        return max(1, int(round(float(hours))))
    return max(1, int(round(float(hours) / bar_h)))


def adaptive_candidate_mask(
    df: pd.DataFrame,
    strength: str,
    *,
    require_weak_price: bool = True,
) -> pd.Series:
    """
    Кандидат, если на баре:
    - Δкум.δ выше rolling-квантиля (рост относительно недавней базы),
    - OI % не выше rolling-квантиля (левый хвост — отток),
    - при require_weak_price: цена % не выше rolling-квантиля (слабая цена),
    плюс базовые знаки: Δкум > 0, OI % < 0.

    require_weak_price=False — для режима «OI↓ + кум.δ↑» при **росте** цены (short covering и т.п.).
    """
    key = (strength or "normal").strip().lower()
    cfg = _STRENGTH_CFG.get(key, _STRENGTH_CFG["normal"])
    W = int(cfg["roll"])
    minp = max(20, W // 3)

    ddc = pd.to_numeric(df["cum_delta_change_N"], errors="coerce")
    oip = pd.to_numeric(df["oi_pct_N"], errors="coerce")
    px = pd.to_numeric(df["price_pct_N"], errors="coerce")

    rc = ddc.rolling(W, min_periods=minp).quantile(float(cfg["cum_q"]))
    ro = oip.rolling(W, min_periods=minp).quantile(float(cfg["oi_q"]))
    rp = px.rolling(W, min_periods=minp).quantile(float(cfg["px_q"]))
    mult = float(cfg["cum_mult"])

    base = (
        ddc.notna()
        & oip.notna()
        & rc.notna()
        & ro.notna()
        & (ddc > rc * mult)
        & (oip <= ro)
        & (ddc > 0)
        & (oip < 0)
    )
    if require_weak_price:
        ok = base & px.notna() & rp.notna() & (px <= rp)
    else:
        ok = base
    return ok.fillna(False)


def assign_signal_types(df: pd.DataFrame, mask: pd.Series) -> pd.Series:
    """
    Метки для отфильтрованных кандидатов (остальные — пустая строка).
    """
    m = mask.fillna(False).to_numpy(dtype=bool)
    oip = pd.to_numeric(df["oi_pct_N"], errors="coerce").to_numpy(dtype=np.float64)
    vs = pd.to_numeric(df["vol_spike"], errors="coerce").to_numpy(dtype=np.float64)
    bd = pd.to_numeric(df["body_compression"], errors="coerce").to_numpy(dtype=np.float64)
    wk = pd.to_numeric(df["lower_wick_roll"], errors="coerce").to_numpy(dtype=np.float64)
    px = pd.to_numeric(df["price_pct_N"], errors="coerce").to_numpy(dtype=np.float64)
    sb = pd.to_numeric(df["struct_break"], errors="coerce").to_numpy(dtype=np.float64)

    typ = np.full(len(df), "", dtype=object)
    short_c = m & np.isfinite(oip) & np.isfinite(wk) & (oip < -0.10) & (wk > 0.36)
    typ[short_c] = "Short Covering"
    absorp = m & ~short_c & np.isfinite(vs) & np.isfinite(px) & (vs > 1.30) & (px <= 0.06)
    typ[absorp] = "Absorption"
    exh = m & ~short_c & ~absorp & np.isfinite(bd) & np.isfinite(sb) & (bd > 0.12) & (sb >= 0.99)
    typ[exh] = "Exhaustion"
    pot = m & ~short_c & ~absorp & ~exh
    typ[pot] = "Potential Reversal"
    return pd.Series(typ, index=df.index, dtype=object)


def _norm01(s: pd.Series) -> pd.Series:
    s = pd.to_numeric(s, errors="coerce")
    lo = s.quantile(0.05)
    hi = s.quantile(0.95)
    return ((s - lo) / (hi - lo + 1e-12)).clip(0.0, 1.0)


def compute_quality_score(df: pd.DataFrame, candidates: pd.Series) -> pd.Series:
    """0..100 по компонентам; нормализация по квантилям всего ряда."""
    if not candidates.any():
        return pd.Series(0.0, index=df.index, dtype=np.float64)
    s_ddc = _norm01(df["cum_delta_change_N"])
    s_oip = _norm01(-df["oi_pct_N"])
    s_body = _norm01(df["body_compression"])
    s_vol = _norm01(df["vol_spike"])
    s_wick = _norm01(df["lower_wick_roll"])
    s_struct = pd.to_numeric(df["struct_break"], errors="coerce").fillna(0.0).clip(0, 1)
    s_bear = _norm01(df["bear_frac_N"])

    score = (
        22.0 * s_ddc
        + 22.0 * s_oip
        + 14.0 * s_body
        + 14.0 * s_vol
        + 14.0 * s_wick
        + 8.0 * s_struct
        + 6.0 * s_bear
    )
    out = pd.Series(0.0, index=df.index, dtype=np.float64)
    out.loc[candidates] = score.loc[candidates].clip(0.0, 100.0)
    return out


def max_adverse_excursion_long(low: np.ndarray, close: np.ndarray, i: int, h: int) -> float:
    """Лонг по close[i]: max_{t=1..h} (close[i] - low[i+t]) / close[i]."""
    n = len(close)
    if i + 1 >= n:
        return float("nan")
    end = min(n - 1, i + h)
    if i + 1 > end:
        return float("nan")
    seg = low[i + 1 : end + 1]
    return float(np.max((close[i] - seg) / (close[i] + 1e-12)))


def add_forward_columns(df: pd.DataFrame, horizons: tuple[int, ...]) -> pd.DataFrame:
    out = df.copy()
    c = out["close"].to_numpy(dtype=np.float64)
    low = out["low"].to_numpy(dtype=np.float64)
    n = len(c)
    for h in horizons:
        fwd = np.full(n, np.nan, dtype=np.float64)
        for i in range(n):
            j = i + h
            if j < n and np.isfinite(c[i]) and c[i] != 0:
                fwd[i] = (c[j] - c[i]) / c[i] * 100.0
        out[f"fwd_ret_{h}"] = fwd
    for h in horizons:
        mae = np.full(n, np.nan, dtype=np.float64)
        for i in range(n):
            mae[i] = max_adverse_excursion_long(low, c, i, h)
        out[f"mae_long_{h}"] = mae
    return out


def summarize_forward_stats(df: pd.DataFrame, sig: pd.Series, horizons: tuple[int, ...]) -> pd.DataFrame:
    rows: list[dict] = []
    if not sig.any():
        return pd.DataFrame(rows)
    sub = df.loc[sig].copy()
    for h in horizons:
        col = f"fwd_ret_{h}"
        if col not in sub.columns:
            continue
        r = pd.to_numeric(sub[col], errors="coerce")
        r = r[np.isfinite(r)]
        mae_c = f"mae_long_{h}"
        mae = pd.to_numeric(sub[mae_c], errors="coerce") if mae_c in sub.columns else pd.Series(dtype=float)
        mae = mae[np.isfinite(mae)]
        rows.append(
            {
                "H": h,
                "n": int(len(r)),
                "win_rate_%": float((r > 0).mean() * 100.0) if len(r) else float("nan"),
                "avg_ret_%": float(r.mean()) if len(r) else float("nan"),
                "median_ret_%": float(r.median()) if len(r) else float("nan"),
                "median_MAE_%": float(mae.median() * 100.0) if len(mae) else float("nan"),
                "mean_MAE_%": float(mae.mean() * 100.0) if len(mae) else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def rolling_hours_divergence_mask(
    df: pd.DataFrame,
    tf_key: str,
    hours: float = 24.0,
) -> pd.Series:
    """
    На баре t: за последние K баров (K = часы / длительность бара) OI упал, кум. δ выросла.
    K подбирается по `tf_key` так, чтобы окно покрывало не меньше `hours` часов.
    """
    if df.empty or hours <= 0:
        return pd.Series(True, index=df.index, dtype=bool)
    K = divergence_duration_bars(tf_key, hours)
    if len(df) <= K:
        return pd.Series(False, index=df.index, dtype=bool)
    cd = pd.to_numeric(df["cum_delta"], errors="coerce").to_numpy(dtype=np.float64)
    oi = pd.to_numeric(df["open_interest"], errors="coerce").to_numpy(dtype=np.float64)
    cd_k = cd - np.roll(cd, K)
    oi_k = oi - np.roll(oi, K)
    cd_k[:K] = np.nan
    oi_k[:K] = np.nan
    eps = 1e-9
    ok = np.isfinite(cd_k) & np.isfinite(oi_k) & (cd_k > eps) & (oi_k < -eps)
    return pd.Series(ok, index=df.index, dtype=bool)


def combined_divergence_hours_mask(
    df: pd.DataFrame,
    tf_key: str,
    hours_spec: float | tuple[float, ...] | None,
) -> pd.Series:
    """
    Пересечение масок «OI↓ + кум.δ↑» для каждого указанного горизонта в часах.
    `hours_spec`: одно число или кортеж, напр. (24.0, 48.0) на 1h → 24 и 48 баров.
    """
    if hours_spec is None:
        return pd.Series(True, index=df.index, dtype=bool)
    if isinstance(hours_spec, (int, float)):
        h = float(hours_spec)
        if h <= 0:
            return pd.Series(True, index=df.index, dtype=bool)
        return rolling_hours_divergence_mask(df, tf_key, h)
    seq = tuple(float(x) for x in hours_spec if float(x) > 0)
    if not seq:
        return pd.Series(True, index=df.index, dtype=bool)
    out = rolling_hours_divergence_mask(df, tf_key, seq[0])
    for h in seq[1:]:
        out = out & rolling_hours_divergence_mask(df, tf_key, h)
    return out


def run_single_symbol_pipeline(
    df_k: pd.DataFrame,
    df_oi: pd.DataFrame,
    *,
    n: int,
    thr: ReversalThresholds | None = None,
    strength: str | None = None,
    horizons: tuple[int, ...] = (1, 3, 5, 10),
    tf_key: str | None = None,
    min_divergence_hours: float | tuple[float, ...] | None = None,
    require_weak_price: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """
    Если задано `strength` (weak/normal/strong/extreme) — адаптивные пороги.
    Иначе — фиксированные `thr`.

    `min_divergence_hours`: одно значение или кортеж часов; для каждого — маска
    «OI вниз, кум. δ вверх» на окне не короче этого интервала; результат — пересечение масок.

    `require_weak_price`: для расхождения OI/кум.δ при растущей цене поставьте False.
    """
    df0 = klines_to_frame_with_delta(df_k)
    df1 = merge_open_interest_on_klines(df0, df_oi)
    df2 = compute_rolling_features(df1, n)
    if strength is not None:
        cand = adaptive_candidate_mask(df2, strength, require_weak_price=require_weak_price)
    elif thr is not None:
        cand = detect_candidates(df2, thr)
    else:
        cand = adaptive_candidate_mask(df2, "normal", require_weak_price=require_weak_price)
    if min_divergence_hours is not None and tf_key:
        div = combined_divergence_hours_mask(df2, tf_key, min_divergence_hours)
        cand = cand & div
    df2["signal_candidate"] = cand.to_numpy(dtype=bool)
    df2["signal_type"] = assign_signal_types(df2, cand).to_numpy(dtype=object)
    df2["quality_score"] = compute_quality_score(df2, cand).to_numpy(dtype=np.float64)
    df3 = add_forward_columns(df2, horizons)
    max_h = max(horizons)
    idx = np.arange(len(df3), dtype=np.int32)
    valid_end = idx <= (len(df3) - 1 - max_h)
    sig = df3["signal_candidate"].to_numpy(dtype=bool) & valid_end
    df3["signal"] = sig
    sig_table = df3.loc[sig].copy()
    return df3, sig_table, pd.Series(sig, index=df3.index)
