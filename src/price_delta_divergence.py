"""
Расхождение цены и кумулятивной δ (USDT-M klines): без Streamlit.

Вариант A: цена **растёт** или **диапазон сужается**, при этом кум. δ **падает**.
Вариант B (**наоборот**): цена **падает** или **диапазон расширяется**, кум. δ **растёт**.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from order_flow_reversal import klines_to_frame_with_delta


def compute_price_delta_window(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """
    На баре i (после прогрева): окно N баров назад.
    - price_pct_N: % изменение close
    - cum_delta_change_N: прирост кум. δ за N баров
    - range_ratio: средний (H−L) на последних n/2 / на предыдущих n/2 барах окна (<1 — сужение)
    """
    if n < 6:
        raise ValueError("n must be >= 6")
    out = df.copy()
    c = out["close"].to_numpy(dtype=np.float64)
    cd = out["cum_delta"].to_numpy(dtype=np.float64)
    h = out["high"].to_numpy(dtype=np.float64)
    l = out["low"].to_numpy(dtype=np.float64)
    rng = h - l
    k = n
    prev_c = np.roll(c, k)
    prev_c[:k] = np.nan
    out["price_pct_N"] = (c / (prev_c + 1e-12) - 1.0) * 100.0

    prev_cd = np.roll(cd, k)
    prev_cd[:k] = np.nan
    out["cum_delta_change_N"] = cd - prev_cd

    half = max(k // 2, 2)
    ratio = np.full(len(rng), np.nan, dtype=np.float64)
    for i in range(k - 1, len(rng)):
        w = rng[i - k + 1 : i + 1]
        m1 = float(np.mean(w[:half]))
        m2 = float(np.mean(w[half:]))
        if m1 > 1e-12:
            ratio[i] = m2 / m1
    out["range_ratio"] = ratio
    # вспомогательно: средний range на всём окне N
    out["range_avg_k"] = pd.Series(rng).rolling(k, min_periods=k).mean().to_numpy(dtype=np.float64)
    return out


def detect_price_delta_divergence(
    df: pd.DataFrame,
    *,
    min_abs_price_pct: float = 0.08,
    min_abs_delta: float = 0.0,
    narrow_ratio: float = 0.92,
    wide_ratio: float = 1.08,
    use_range_narrow: bool = True,
    use_range_wide: bool = True,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Возвращает (mask_A, mask_B, label_series).
    A: (цена вверх ИЛИ сужение диапазона) И δ вниз.
    B: (цена вниз ИЛИ расширение диапазона) И δ вверх.
    min_abs_delta: мин. |Δкум.δ| за N (0 = только знак и ветка цены/range).
    """
    px = pd.to_numeric(df["price_pct_N"], errors="coerce")
    dd = pd.to_numeric(df["cum_delta_change_N"], errors="coerce")
    rr = pd.to_numeric(df["range_ratio"], errors="coerce")

    pmin = float(min_abs_price_pct)
    dmin = float(min_abs_delta)

    price_up = px > pmin
    price_dn = px < -pmin
    narrow = rr.notna() & (rr < float(narrow_ratio)) if use_range_narrow else pd.Series(False, index=df.index)
    wide = rr.notna() & (rr > float(wide_ratio)) if use_range_wide else pd.Series(False, index=df.index)

    branch_a_price = price_up | narrow
    delta_down = dd.notna() & (dd < -dmin)
    mask_a = branch_a_price & delta_down

    branch_b_price = price_dn | wide
    delta_up = dd.notna() & (dd > dmin)
    mask_b = branch_b_price & delta_up

    lab = pd.Series("", index=df.index, dtype=object)
    lab.loc[mask_a & ~mask_b] = "Цена↑/сужение vs δ↓"
    lab.loc[mask_b & ~mask_a] = "Цена↓/расширение vs δ↑"
    lab.loc[mask_a & mask_b] = "Оба типа"
    return mask_a.fillna(False), mask_b.fillna(False), lab


def divergence_score(df: pd.DataFrame, mask_a: pd.Series, mask_b: pd.Series) -> pd.Series:
    """0..100: сила расхождения |px| + нормированный |Δδ|."""
    px = pd.to_numeric(df["price_pct_N"], errors="coerce").abs()
    dd = pd.to_numeric(df["cum_delta_change_N"], errors="coerce").abs()
    px_n = (px - px.quantile(0.05)) / (px.quantile(0.95) - px.quantile(0.05) + 1e-9)
    dd_n = (dd - dd.quantile(0.05)) / (dd.quantile(0.95) - dd.quantile(0.05) + 1e-9)
    px_n = px_n.clip(0, 1).fillna(0)
    dd_n = dd_n.clip(0, 1).fillna(0)
    m = (mask_a | mask_b).fillna(False)
    sc = (45 * px_n + 55 * dd_n).clip(0, 100)
    out = pd.Series(0.0, index=df.index, dtype=np.float64)
    out.loc[m] = sc.loc[m]
    return out


def run_price_delta_scan(
    df_k: pd.DataFrame,
    *,
    n: int,
    min_abs_price_pct: float = 0.08,
    min_abs_delta: float = 0.0,
    narrow_ratio: float = 0.92,
    wide_ratio: float = 1.08,
    use_range_narrow: bool = True,
    use_range_wide: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """
    Полный прогон. Возвращает (full_df с колонками сигнальных, таблица сигналов, mask OR).
    """
    df0 = klines_to_frame_with_delta(df_k)
    df1 = compute_price_delta_window(df0, n)
    ma, mb, lab = detect_price_delta_divergence(
        df1,
        min_abs_price_pct=min_abs_price_pct,
        min_abs_delta=min_abs_delta,
        narrow_ratio=narrow_ratio,
        wide_ratio=wide_ratio,
        use_range_narrow=use_range_narrow,
        use_range_wide=use_range_wide,
    )
    df1["signal_a"] = ma.to_numpy(dtype=bool)
    df1["signal_b"] = mb.to_numpy(dtype=bool)
    df1["signal"] = (ma | mb).to_numpy(dtype=bool)
    df1["signal_type"] = lab.to_numpy(dtype=object)
    df1["quality_score"] = divergence_score(df1, ma, mb).to_numpy(dtype=np.float64)
    sig = df1.loc[df1["signal"]].copy()
    return df1, sig, pd.Series(df1["signal"].to_numpy(dtype=bool), index=df1.index)
