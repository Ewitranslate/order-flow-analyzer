"""
Williams %R with EMA — порт Pine Script-индикатора «Willy_mid_TRI».

  out  = -100 * (highest(high, length) - close) / (highest(high, length) - lowest(low, length))
  out2 = EMA(out, ema_len)

Уровни: 0 / -20 (верхняя зона) / -50 (середина) / -80 (нижняя зона) / -100.
Заливка всей верхней полосы (0…−20, lime) при out2 > -20 и всей нижней полосы
(−80…−100, red) при out2 < -80 — как stupid-overbought/oversold в Pine.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go


def williams_percent_r(
    df: pd.DataFrame,
    *,
    length: int = 21,
    ema_length: int = 13,
) -> pd.DataFrame:
    """
    Возвращает DataFrame с колонками `willy` и `willy_ema`.
    Требует наличия `high`, `low`, `close` в df.
    """
    if df is None or df.empty:
        return pd.DataFrame({"willy": [], "willy_ema": []}, dtype=np.float64)

    n = max(1, int(length))
    m = max(1, int(ema_length))

    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")

    upper = high.rolling(n, min_periods=n).max()
    lower = low.rolling(n, min_periods=n).min()
    rng = (upper - lower).where(lambda s: s.abs() > 1e-12)
    willy = -100.0 * (upper - close) / rng
    willy_ema = willy.ewm(span=m, adjust=False, min_periods=m).mean()

    out = pd.DataFrame(
        {
            "willy": willy.to_numpy(dtype=np.float64),
            "willy_ema": willy_ema.to_numpy(dtype=np.float64),
        },
        index=df.index,
    )
    return out


def add_williams_panel(
    fig: go.Figure,
    df: pd.DataFrame,
    *,
    row: int,
    length: int = 21,
    ema_length: int = 13,
) -> None:
    """Рисует панель Williams %R + EMA с зонами −20 / −50 / −80 на нужной row фигуры."""
    if df is None or df.empty:
        return

    w = williams_percent_r(df, length=length, ema_length=ema_length)
    if w.empty:
        return

    x = df["timestamp"]
    ema = pd.to_numeric(w["willy_ema"], errors="coerce")
    raw = pd.to_numeric(w["willy"], errors="coerce")

    ema_arr = ema.to_numpy(dtype=np.float64)
    # Полосы, как в Pine: верхняя зона −20…0 закрашивается, когда EMA > −20;
    # нижняя зона −100…−80 — когда EMA < −80. Используем np.where (без NaN),
    # чтобы fill="tonexty" всегда давал валидный полигон между двумя линиями.
    upper_band_top = np.where(ema_arr > -20.0, 0.0, -20.0)
    lower_band_bot = np.where(ema_arr < -80.0, -100.0, -80.0)

    fig.add_trace(
        go.Scatter(
            x=x,
            y=[-20.0] * len(x),
            mode="lines",
            line=dict(color="rgba(148,163,184,0.0)", width=0),
            hoverinfo="skip",
            showlegend=False,
            name="_w_base_top",
        ),
        row=row,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=upper_band_top,
            mode="lines",
            line=dict(color="rgba(0,0,0,0)", width=0),
            fill="tonexty",
            fillcolor="rgba(50,205,50,0.30)",
            hoverinfo="skip",
            showlegend=False,
            name="_w_overbought_fill",
        ),
        row=row,
        col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=x,
            y=[-80.0] * len(x),
            mode="lines",
            line=dict(color="rgba(148,163,184,0.0)", width=0),
            hoverinfo="skip",
            showlegend=False,
            name="_w_base_bot",
        ),
        row=row,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=lower_band_bot,
            mode="lines",
            line=dict(color="rgba(0,0,0,0)", width=0),
            fill="tonexty",
            fillcolor="rgba(220,38,38,0.32)",
            hoverinfo="skip",
            showlegend=False,
            name="_w_oversold_fill",
        ),
        row=row,
        col=1,
    )

    for level, dash, color in (
        (-20.0, "solid", "rgba(148,163,184,0.55)"),
        (-50.0, "dot", "rgba(148,163,184,0.75)"),
        (-80.0, "solid", "rgba(148,163,184,0.55)"),
    ):
        fig.add_hline(
            y=level,
            line=dict(color=color, width=1, dash=dash),
            row=row,
            col=1,
        )

    fig.add_trace(
        go.Scatter(
            x=x,
            y=raw,
            mode="lines",
            name=f"Willy %R ({length})",
            line=dict(color="#22d3ee", width=1.6),
            hovertemplate="Willy: %{y:.2f}<extra></extra>",
        ),
        row=row,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=ema,
            mode="lines",
            name=f"EMA {ema_length}",
            line=dict(color="#ef4444", width=1.8),
            hovertemplate="EMA: %{y:.2f}<extra></extra>",
        ),
        row=row,
        col=1,
    )
    # Невидимая точка для x unified / spike (заливки с hoverinfo=skip не участвуют).
    hover_y = np.where(np.isfinite(ema_arr), ema_arr, -50.0)
    fig.add_trace(
        go.Scatter(
            x=x,
            y=hover_y,
            mode="markers",
            name="_willy_hover",
            marker=dict(opacity=0, size=10, color="rgba(0,0,0,0)", symbol="square"),
            showlegend=False,
            hovertemplate="<extra></extra>",
        ),
        row=row,
        col=1,
    )

    fig.update_yaxes(
        range=[-105, 5],
        tickvals=[0, -20, -50, -80, -100],
        row=row,
        col=1,
    )
