"""
Average True Range (ATR) — Wilder smoothing, отдельная панель на главном графике.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go


def average_true_range(df: pd.DataFrame, *, period: int = 14) -> pd.Series:
    """
    ATR по Уайлдеру (RMA от True Range).
    Требует колонки high, low, close.
    """
    if df is None or df.empty:
        return pd.Series(dtype=np.float64)

    h = pd.to_numeric(df["high"], errors="coerce")
    l = pd.to_numeric(df["low"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    n = max(1, int(period))
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def compute_atr_peak_ratio(
    df: pd.DataFrame,
    *,
    period: int = 14,
    peak_lookback: int = 30,
    use_closed_bar: bool = True,
) -> float | None:
    """
    ATR[бар] / max(ATR) за последние peak_lookback баров.
    Низкое значение — волатильность сжалась от недавнего пика (как на панели ATR после импульса).
    """
    if df is None or df.empty:
        return None
    lb = max(int(peak_lookback), int(period) + 2)
    if len(df) < lb:
        return None
    atr = average_true_range(df, period=period)
    if atr.empty:
        return None
    eval_idx = len(df) - 2 if use_closed_bar and len(df) >= 2 else len(df) - 1
    if eval_idx < lb - 1:
        return None
    window = pd.to_numeric(atr.iloc[eval_idx - lb + 1 : eval_idx + 1], errors="coerce")
    cur = float(window.iloc[-1]) if len(window) else float("nan")
    peak = float(window.max()) if len(window) else float("nan")
    if not np.isfinite(cur) or not np.isfinite(peak) or peak <= 1e-12:
        return None
    return float(cur / peak)


def add_atr_panel(
    fig: go.Figure,
    df: pd.DataFrame,
    *,
    row: int,
    period: int = 14,
) -> None:
    """Линия ATR на указанной row subplot-фигуры."""
    if df is None or df.empty or "timestamp" not in df.columns:
        return

    atr = average_true_range(df, period=period)
    if atr.empty or not atr.notna().any():
        return

    x = df["timestamp"]
    y = pd.to_numeric(atr, errors="coerce")
    y_arr = y.to_numpy(dtype=np.float64)

    fig.add_trace(
        go.Scatter(
            x=x,
            y=y,
            mode="lines",
            name=f"ATR ({int(period)})",
            line=dict(color="#fbbf24", width=1.7),
            fill="tozeroy",
            fillcolor="rgba(251,191,36,0.10)",
            hovertemplate="ATR: %{y:,.6f}<extra></extra>",
        ),
        row=row,
        col=1,
    )
    hover_y = np.where(np.isfinite(y_arr), y_arr, 0.0)
    fig.add_trace(
        go.Scatter(
            x=x,
            y=hover_y,
            mode="markers",
            name="_atr_hover",
            marker=dict(opacity=0, size=10, color="rgba(0,0,0,0)", symbol="square"),
            showlegend=False,
            hovertemplate="ATR: %{y:,.6f}<extra></extra>",
        ),
        row=row,
        col=1,
    )
