"""
Корреляция кумулятивной δ выбранной пары с кум. δ остальных (USDT-M klines). Без Streamlit.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from order_flow_reversal import klines_to_frame_with_delta


def cum_delta_from_klines(klines: pd.DataFrame) -> pd.Series:
    """Кум. δ, индекс — UTC timestamp (уникальные бары)."""
    if klines is None or klines.empty:
        return pd.Series(dtype=np.float64)
    df = klines_to_frame_with_delta(klines)
    s = pd.to_numeric(df["cum_delta"], errors="coerce")
    ts = pd.to_datetime(df["timestamp"], utc=True)
    out = pd.Series(s.to_numpy(dtype=np.float64), index=ts, dtype=np.float64)
    return out[~out.index.duplicated(keep="last")].sort_index()


def align_cum_delta_panel(series_map: dict[str, pd.Series]) -> pd.DataFrame:
    """Внутреннее пересечение по времени; колонки — символы."""
    valid = {k: v for k, v in series_map.items() if v is not None and len(v) > 0}
    if not valid:
        return pd.DataFrame()
    parts = []
    for sym, ser in valid.items():
        parts.append(ser.rename(sym))
    panel = pd.concat(parts, axis=1, join="inner").sort_index()
    return panel.dropna(how="any")


def _prepare_series(panel: pd.DataFrame, ref: str, *, mode: str) -> tuple[pd.Series, pd.DataFrame]:
    """
    mode:
      levels — уровни кум. δ
      bar_delta — дельта за бар (diff кум.)
      pct_change — % изменение кум. δ за бар
    """
    ref = ref.upper()
    if ref not in panel.columns:
        raise KeyError(f"reference {ref} not in panel")
    if mode == "levels":
        y = panel[ref].copy()
        x = panel.copy()
    elif mode == "bar_delta":
        d = panel.diff().iloc[1:]
        y = d[ref]
        x = d
    elif mode == "pct_change":
        d = panel.pct_change().replace([np.inf, -np.inf], np.nan).iloc[1:]
        y = d[ref]
        x = d
    else:
        raise ValueError(f"unknown mode: {mode}")
    return y, x


def correlate_cum_delta_panel(
    panel: pd.DataFrame,
    reference: str,
    *,
    mode: str = "bar_delta",
    method: str = "pearson",
    min_obs: int = 80,
) -> pd.DataFrame:
    """
    Для каждой колонки (кроме reference) — корреляция с reference на общих барах.
    Возвращает DataFrame: symbol, r, n_obs, method, mode (отсортировано по |r| убыв.).
    """
    ref = reference.upper()
    if panel.empty or ref not in panel.columns:
        return pd.DataFrame(columns=["symbol", "r", "n_obs", "method", "mode"])

    y, xdf = _prepare_series(panel, ref, mode=mode)
    meth = method.lower()
    if meth not in ("pearson", "spearman"):
        raise ValueError("method must be pearson or spearman")

    rows: list[dict] = []
    for sym in xdf.columns:
        if sym.upper() == ref:
            continue
        pair = pd.concat([y.rename("_ref"), xdf[sym].rename("_o")], axis=1).dropna()
        n = int(pair.shape[0])
        if n < int(min_obs):
            continue
        if pair["_ref"].std() < 1e-12 or pair["_o"].std() < 1e-12:
            continue
        if meth == "pearson":
            r = float(pair["_ref"].corr(pair["_o"], method="pearson"))
        else:
            r = float(pair["_ref"].corr(pair["_o"], method="spearman"))
        if not np.isfinite(r):
            continue
        rows.append({"symbol": sym.upper(), "r": r, "n_obs": n, "method": meth, "mode": mode})

    if not rows:
        return pd.DataFrame(columns=["symbol", "r", "n_obs", "method", "mode"])
    out = pd.DataFrame(rows)
    out["abs_r"] = out["r"].abs()
    return out.sort_values("abs_r", ascending=False).drop(columns=["abs_r"]).reset_index(drop=True)


def fetch_panel_cum_deltas(
    fetch_klines,
    symbols: list[str],
    *,
    reference: str,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """
    fetch_klines(sym) -> DataFrame; возвращает panel и errors[sym].
    reference всегда включается первым.
    """
    ref = reference.upper()
    syms = [ref] + [s.upper() for s in symbols if s.upper() != ref]
    series_map: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}
    for sym in syms:
        try:
            kl = fetch_klines(sym)
        except Exception as e:  # noqa: BLE001
            errors[sym] = str(e)[:120]
            continue
        if kl is None or kl.empty:
            errors[sym] = "нет свечей"
            continue
        series_map[sym] = cum_delta_from_klines(kl)
    panel = align_cum_delta_panel(series_map)
    return panel, errors
