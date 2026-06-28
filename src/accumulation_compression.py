"""
Accumulation / Compression scanner — rolling N-bar metrics, divergence efficiency,
forward breakout/vol stats (USDT-M OHLC + OI + cum delta). Без Streamlit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from order_flow_reversal import klines_to_frame_with_delta, merge_open_interest_on_klines


def _true_range(h: np.ndarray, l: np.ndarray, pc: np.ndarray) -> np.ndarray:
    n = len(h)
    tr = np.full(n, np.nan, dtype=np.float64)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        a = h[i] - l[i]
        b = abs(h[i] - pc[i - 1])
        c = abs(l[i] - pc[i - 1])
        tr[i] = max(a, b, c)
    return tr


def build_accumulation_features(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """
    Rolling window N: price %, cum-delta change, OI %, ATR & compression,
    candle overlap ratio, return-vol contraction. Requires columns from merge OI + klines+delta.
    """
    if n < 5:
        raise ValueError("n must be >= 5")
    out = df.copy()
    o = out["open"].to_numpy(dtype=np.float64)
    h = out["high"].to_numpy(dtype=np.float64)
    l = out["low"].to_numpy(dtype=np.float64)
    c = out["close"].to_numpy(dtype=np.float64)
    cd = out["cum_delta"].to_numpy(dtype=np.float64)
    oi = out["open_interest"].to_numpy(dtype=np.float64)
    rng = h - l

    k = n
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = _true_range(h, l, prev_c)
    out["tr"] = tr
    atr = pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy(dtype=np.float64)
    out["atr_N"] = atr
    atr_long = pd.Series(tr).rolling(min(3 * n, len(out)), min_periods=n).mean().to_numpy(dtype=np.float64)
    out["atr_compression"] = 1.0 - (atr / (atr_long + 1e-12))
    out["atr_compression"] = np.clip(out["atr_compression"], -3.0, 3.0)

    prev_close_n = np.roll(c, k)
    prev_close_n[:k] = np.nan
    out["price_pct_N"] = (c / (prev_close_n + 1e-12) - 1.0) * 100.0

    prev_cd = np.roll(cd, k)
    prev_cd[:k] = np.nan
    d_cd = cd - prev_cd
    out["cum_delta_change_N"] = d_cd
    prev_cd_safe = np.where(np.abs(prev_cd) > 1e-6, prev_cd, np.sign(prev_cd) * 1e-6)
    out["cum_delta_pct_N"] = (d_cd / (np.abs(prev_cd_safe) + 1e-12)) * 100.0

    prev_oi = np.roll(oi, k)
    prev_oi[:k] = np.nan
    out["oi_pct_N"] = np.where(np.abs(prev_oi) > 1e-9, (oi - prev_oi) / (np.abs(prev_oi) + 1e-12) * 100.0, np.nan)

    eps_p = 0.02
    out["divergence_efficiency"] = d_cd / (np.maximum(np.abs(out["price_pct_N"].to_numpy(dtype=np.float64)), eps_p))

    hl_mean = pd.Series(rng).rolling(n, min_periods=n).mean().to_numpy(dtype=np.float64)
    hl_long = pd.Series(rng).rolling(min(3 * n, len(out)), min_periods=n).mean().to_numpy(dtype=np.float64)
    out["range_compression"] = 1.0 - (hl_mean / (hl_long + 1e-12))
    out["range_compression"] = np.clip(out["range_compression"], -3.0, 3.0)

    # Overlap ratio: consecutive bars in window [i-n+1, i] — vectorized approximation via rolling min/max span
    roll_hi = pd.Series(h).rolling(n, min_periods=n).max().to_numpy(dtype=np.float64)
    roll_lo = pd.Series(l).rolling(n, min_periods=n).min().to_numpy(dtype=np.float64)
    window_span = roll_hi - roll_lo
    sum_ranges = pd.Series(rng).rolling(n, min_periods=n).sum().to_numpy(dtype=np.float64)
    out["candle_overlap_ratio"] = np.where(
        sum_ranges > 1e-12,
        np.clip(window_span / (sum_ranges + 1e-12), 0.0, 1.5),
        np.nan,
    )

    rets = np.diff(np.log(np.maximum(c, 1e-12)), prepend=np.nan)
    half = max(n // 2, 2)
    v1 = pd.Series(rets).rolling(half, min_periods=2).std()
    v2 = pd.Series(rets).shift(half).rolling(half, min_periods=2).std()
    out["vol_contraction"] = 1.0 - (v1 / (v2 + 1e-12))
    out["vol_contraction"] = out["vol_contraction"].replace([np.inf, -np.inf], np.nan)

    return out


def detect_accumulation_mask(
    df: pd.DataFrame,
    *,
    flat_abs_price_pct: float = 0.45,
    min_cum_delta_pct: float = 0.08,
    max_oi_pct: float = -0.04,
    min_atr_compression: float = 0.02,
    min_range_compression: float = 0.02,
    min_overlap: float = 0.22,
    min_vol_contraction: float = -0.5,
) -> pd.Series:
    """Boolean mask: flat price, strong cum delta, OI outflow, compressing ATR/ranges."""
    px = pd.to_numeric(df["price_pct_N"], errors="coerce")
    ddp = pd.to_numeric(df["cum_delta_pct_N"], errors="coerce")
    oip = pd.to_numeric(df["oi_pct_N"], errors="coerce")
    ac = pd.to_numeric(df["atr_compression"], errors="coerce")
    rc = pd.to_numeric(df["range_compression"], errors="coerce")
    ov = pd.to_numeric(df["candle_overlap_ratio"], errors="coerce")
    vc = pd.to_numeric(df["vol_contraction"], errors="coerce")

    ok = (
        px.notna()
        & ddp.notna()
        & oip.notna()
        & (px.abs() <= float(flat_abs_price_pct))
        & (ddp >= float(min_cum_delta_pct))
        & (oip <= float(max_oi_pct))
        & ac.notna()
        & (ac >= float(min_atr_compression))
        & rc.notna()
        & (rc >= float(min_range_compression))
        & ov.notna()
        & (ov >= float(min_overlap))
        & (vc.fillna(0.0) >= float(min_vol_contraction))
    )
    return ok.fillna(False)


def assign_accumulation_labels(df: pd.DataFrame, mask: pd.Series) -> pd.Series:
    m = mask.fillna(False).to_numpy(dtype=bool)
    ddp = pd.to_numeric(df["cum_delta_pct_N"], errors="coerce").to_numpy(dtype=np.float64)
    oip = pd.to_numeric(df["oi_pct_N"], errors="coerce").to_numpy(dtype=np.float64)
    rc = pd.to_numeric(df["range_compression"], errors="coerce").to_numpy(dtype=np.float64)
    ov = pd.to_numeric(df["candle_overlap_ratio"], errors="coerce").to_numpy(dtype=np.float64)
    eff = pd.to_numeric(df["divergence_efficiency"], errors="coerce").to_numpy(dtype=np.float64)
    ac = pd.to_numeric(df["atr_compression"], errors="coerce").to_numpy(dtype=np.float64)

    lab = np.full(len(df), "", dtype=object)
    if m.any():
        thr_d = float(np.nanpercentile(ddp[m], 65))
        thr_e = float(np.nanpercentile(eff[m], 60))
    else:
        thr_d, thr_e = 0.0, 0.0
    hi_d = m & np.isfinite(ddp) & (ddp > thr_d)
    hi_comp = m & np.isfinite(rc) & (rc > 0.12)
    hi_ov = m & np.isfinite(ov) & (ov > 0.38)
    hi_eff = m & np.isfinite(eff) & (eff > thr_e)

    hidden = m & hi_eff & (oip < -0.08) & hi_d
    lab[hidden] = "Hidden Buying"
    empty = np.array([str(x) == "" for x in lab])
    rest = m & empty
    accum = rest & hi_comp & (ddp > 0) & (oip < -0.05)
    lab[accum] = "Accumulation"
    empty = np.array([str(x) == "" for x in lab])
    rest2 = m & empty
    comp = rest2 & hi_ov & hi_comp
    lab[comp] = "Compression"
    empty = np.array([str(x) == "" for x in lab])
    rest3 = m & empty
    expn = rest3 & hi_comp & np.isfinite(ac) & (ac > 0.08)
    lab[expn] = "Potential Expansion"
    empty = np.array([str(x) == "" for x in lab])
    rest4 = m & empty
    lab[rest4] = "Accumulation"
    return pd.Series(lab, index=df.index, dtype=object)


def _norm_pos(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    if not x.notna().any():
        return pd.Series(0.0, index=s.index)
    lo = x.quantile(0.05)
    hi = x.quantile(0.95)
    return ((x - lo) / (hi - lo + 1e-12)).clip(0.0, 1.0).fillna(0.0)


def compute_accumulation_score(df: pd.DataFrame, mask: pd.Series) -> pd.Series:
    """0..100: delta strength, OI decline, compression, efficiency (low price movement)."""
    m = mask.fillna(False)
    if not m.any():
        return pd.Series(0.0, index=df.index, dtype=np.float64)
    ddp = _norm_pos(df["cum_delta_pct_N"])
    oi_s = _norm_pos(-pd.to_numeric(df["oi_pct_N"], errors="coerce"))
    comp = (
        _norm_pos(pd.to_numeric(df["range_compression"], errors="coerce"))
        + _norm_pos(pd.to_numeric(df["atr_compression"], errors="coerce"))
        + _norm_pos(pd.to_numeric(df["candle_overlap_ratio"], errors="coerce"))
    ) / 3.0
    eff = _norm_pos(pd.to_numeric(df["divergence_efficiency"], errors="coerce").clip(lower=0))
    px_abs = pd.to_numeric(df["price_pct_N"], errors="coerce").abs()
    low_px = (1.0 - _norm_pos(px_abs)).clip(0, 1)

    score = 28.0 * ddp + 24.0 * oi_s + 22.0 * comp + 16.0 * eff + 10.0 * low_px
    out = pd.Series(0.0, index=df.index, dtype=np.float64)
    out.loc[m] = score.loc[m].clip(0.0, 100.0)
    return out


def forward_breakout_vol_stats(
    df: pd.DataFrame,
    sig: pd.Series,
    *,
    horizon: int = 12,
    range_lookback: int = 24,
    breakout_mult: float = 1.15,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    На каждом сигнальном баре: пробой диапазона за horizon баров, доходность close,
    расширение волатильности (std лог-доходов после vs до).
    Возвращает (df с новыми колонками, summary DataFrame одной строкой или по сигналам).
    """
    out = df.copy()
    c = out["close"].to_numpy(dtype=np.float64)
    h = out["high"].to_numpy(dtype=np.float64)
    l = out["low"].to_numpy(dtype=np.float64)
    n = len(c)
    m = sig.fillna(False).to_numpy(dtype=bool)

    fwd_ret = np.full(n, np.nan, dtype=np.float64)
    brk = np.full(n, np.nan, dtype=np.float64)
    vol_exp = np.full(n, np.nan, dtype=np.float64)

    for i in range(n):
        if not m[i]:
            continue
        j1 = min(n - 1, i + int(horizon))
        if j1 <= i:
            continue
        lo = i - int(range_lookback)
        if lo < 0:
            continue
        rh = float(np.max(h[lo : i + 1]))
        rl = float(np.min(l[lo : i + 1]))
        mid = (rh + rl) * 0.5
        span = max(rh - rl, 1e-9 * max(1.0, abs(mid)))
        seg_h = h[i + 1 : j1 + 1]
        seg_l = l[i + 1 : j1 + 1]
        broke_up = np.any(seg_h > rh + (float(breakout_mult) - 1.0) * span)
        broke_dn = np.any(seg_l < rl - (float(breakout_mult) - 1.0) * span)
        brk[i] = 1.0 if (broke_up or broke_dn) else 0.0
        fwd_ret[i] = (c[j1] - c[i]) / (c[i] + 1e-12) * 100.0

        pre = c[max(0, i - int(range_lookback)) : i + 1]
        post = c[i : j1 + 1]
        if len(pre) > 2 and len(post) > 2:
            r0 = np.diff(np.log(np.maximum(pre, 1e-12)))
            r1 = np.diff(np.log(np.maximum(post, 1e-12)))
            s0 = float(np.std(r0)) if len(r0) else 0.0
            s1 = float(np.std(r1)) if len(r1) else 0.0
            vol_exp[i] = (s1 / (s0 + 1e-12)) - 1.0 if s0 > 1e-12 else np.nan

    out["fwd_ret_H"] = fwd_ret
    out["fwd_breakout_H"] = brk
    out["fwd_vol_expansion_H"] = vol_exp

    sub = out.loc[m, ["fwd_ret_H", "fwd_breakout_H", "fwd_vol_expansion_H"]].copy()
    sub = sub[np.isfinite(sub["fwd_ret_H"])]
    rows: list[dict] = []
    if len(sub):
        br = pd.to_numeric(sub["fwd_breakout_H"], errors="coerce")
        rr = pd.to_numeric(sub["fwd_ret_H"], errors="coerce")
        ve = pd.to_numeric(sub["fwd_vol_expansion_H"], errors="coerce")
        rows.append(
            {
                "n": int(len(sub)),
                "breakout_prob_%": float(br.mean() * 100.0),
                "avg_fwd_ret_%": float(rr.mean()),
                "median_fwd_ret_%": float(rr.median()),
                "median_vol_expansion": float(ve.median()) if ve.notna().any() else float("nan"),
                "mean_vol_expansion": float(ve.mean()) if ve.notna().any() else float("nan"),
            }
        )
    summ = pd.DataFrame(rows)
    return out, summ


def run_accumulation_scan(
    df_k: pd.DataFrame,
    df_oi: pd.DataFrame,
    *,
    n: int,
    flat_abs_price_pct: float = 0.45,
    min_cum_delta_pct: float = 0.08,
    max_oi_pct: float = -0.04,
    forward_horizon: int = 12,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.DataFrame]:
    """
    Полный прогон: merged frame → features → mask → labels → score → forward stats.
    Возвращает (full_df, sig_table, mask, forward_summary).
    """
    df0 = klines_to_frame_with_delta(df_k)
    df1 = merge_open_interest_on_klines(df0, df_oi)
    df2 = build_accumulation_features(df1, n)
    mask = detect_accumulation_mask(
        df2,
        flat_abs_price_pct=flat_abs_price_pct,
        min_cum_delta_pct=min_cum_delta_pct,
        max_oi_pct=max_oi_pct,
    )
    df2["signal_candidate"] = mask.to_numpy(dtype=bool)
    df2["signal_type"] = assign_accumulation_labels(df2, mask).to_numpy(dtype=object)
    df2["quality_score"] = compute_accumulation_score(df2, mask).to_numpy(dtype=np.float64)
    df3, summ = forward_breakout_vol_stats(df2, mask, horizon=forward_horizon)
    valid = mask.fillna(False).to_numpy(dtype=bool) & (np.arange(len(df3)) <= len(df3) - 1 - int(forward_horizon))
    df3["signal"] = valid
    sig_tab = df3.loc[valid].copy()
    return df3, sig_tab, pd.Series(valid, index=df3.index), summ
