"""
Price Compression — сужение цены по подтверждённым Pivot High/Low и линейной регрессии границ.

Без ATR / Bollinger / ZigZag. Pivot не перерисовывается после подтверждения (bar + pivot_right).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from compression_filters import (
    CompressionFilterContext,
    channel_bounds_at,
    passes_quality_filters,
)
from compression_types import (
    CompressionParams,
    CompressionZone,
    RECOMMENDED_COMPRESSION_HELP_RU,
    apply_recommended_compression_session_state,
    compression_params_from_session,
    compression_session_defaults,
    default_compression_params_for_tf,
    init_compression_session_state,
    recommended_compression_params,
)

PivotKind = Literal["high", "low"]

# Re-export public types for backward compatibility.
__all__ = [
    "CompressionParams",
    "CompressionZone",
    "CompressionEvaluator",
    "PivotKind",
    "RECOMMENDED_COMPRESSION_HELP_RU",
    "active_compression_at_bar",
    "add_compression_traces",
    "apply_recommended_compression_session_state",
    "compression_params_from_session",
    "compression_session_defaults",
    "compression_zone_for_scanner",
    "default_compression_params_for_tf",
    "detect_compression_zones",
    "fractal_pivot_high_indices",
    "fractal_pivot_low_indices",
    "init_compression_session_state",
    "latest_compression_at_end",
    "recommended_compression_params",
]


def fractal_pivot_high_indices(high: np.ndarray, left: int, right: int) -> list[int]:
    if left < 1 or right < 1:
        return []
    n = int(high.shape[0])
    out: list[int] = []
    for i in range(left, n - right):
        h = float(high[i])
        if h > float(np.max(high[i - left : i])) and h > float(np.max(high[i + 1 : i + right + 1])):
            out.append(i)
    return out


def fractal_pivot_low_indices(low: np.ndarray, left: int, right: int) -> list[int]:
    if left < 1 or right < 1:
        return []
    n = int(low.shape[0])
    out: list[int] = []
    for i in range(left, n - right):
        ln = float(low[i])
        if ln < float(np.min(low[i - left : i])) and ln < float(np.min(low[i + 1 : i + right + 1])):
            out.append(i)
    return out


def _linreg(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float]:
    """slope, intercept, R²."""
    if xs.size < 2 or ys.size < 2:
        v = float(ys[0]) if ys.size else 0.0
        return 0.0, v, 0.0
    x = xs.astype(np.float64)
    y = ys.astype(np.float64)
    xm = float(x.mean())
    ym = float(y.mean())
    denom = float(np.sum((x - xm) ** 2))
    if denom <= 1e-18:
        return 0.0, ym, 0.0
    slope = float(np.sum((x - xm) * (y - ym)) / denom)
    intercept = ym - slope * xm
    y_hat = slope * x + intercept
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - ym) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else 1.0
    return slope, intercept, float(np.clip(r2, 0.0, 1.0))


def _count_boundary_touches(
    pivot_idx: list[int],
    prices: np.ndarray,
    *,
    hi_slope: float,
    hi_intercept: float,
    lo_slope: float,
    lo_intercept: float,
    kind: PivotKind,
    touch_tolerance: float,
) -> int:
    touches = 0
    for i in pivot_idx:
        upper, lower, width = channel_bounds_at(
            i,
            hi_slope=hi_slope,
            hi_intercept=hi_intercept,
            lo_slope=lo_slope,
            lo_intercept=lo_intercept,
        )
        tol = float(touch_tolerance) * width
        if kind == "high":
            if abs(float(prices[i]) - upper) <= tol:
                touches += 1
        else:
            if abs(float(prices[i]) - lower) <= tol:
                touches += 1
    return touches


def compression_score(
    *,
    compression_ratio: float,
    upper_touches: int,
    lower_touches: int,
    mid_slope_pct: float,
    formation_bars: int,
    max_compression_ratio: float,
    max_mid_slope_pct_per_bar: float,
    min_touches: int,
    duration_cap_bars: int,
) -> float:
    """
    Итоговая оценка 0–100:
    сжатие 40%, касания 25%, горизонтальность 20%, длительность 15%.
    """
    thr = max(0.05, float(max_compression_ratio))
    compress_part = float(np.clip((thr - compression_ratio) / thr, 0.0, 1.0))

    touch_cap = max(3, int(min_touches) + 4)
    touch_part = float(np.clip((upper_touches + lower_touches) / (2.0 * touch_cap), 0.0, 1.0))

    slope_cap = max(1e-6, float(max_mid_slope_pct_per_bar))
    horiz_part = float(np.clip(1.0 - abs(mid_slope_pct) / slope_cap, 0.0, 1.0))

    dur_cap = max(12, int(duration_cap_bars))
    duration_part = float(np.clip(int(formation_bars) / dur_cap, 0.0, 1.0))

    raw = (
        0.40 * compress_part
        + 0.25 * touch_part
        + 0.20 * horiz_part
        + 0.15 * duration_part
    )
    return float(np.clip(raw * 100.0, 0.0, 100.0))


@dataclass
class CompressionEvaluator:
    """
    Кэширует pivot и OHLC для быстрой оценки на одном или нескольких барах.
    Используется сканером (тысячи пар) без полного прохода по всей истории.
    """

    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    params: CompressionParams
    ph_all: list[int]
    pl_all: list[int]
    warm: int
    last_bar: int

    @classmethod
    def from_dataframe(
        cls,
        df: pd.DataFrame,
        params: CompressionParams | None = None,
        *,
        use_closed_bar: bool = True,
    ) -> CompressionEvaluator:
        p = params or CompressionParams()
        work = df.sort_values("timestamp" if "timestamp" in df.columns else "open_time").reset_index(
            drop=True
        )
        high = pd.to_numeric(work["high"], errors="coerce").to_numpy(dtype=np.float64)
        low = pd.to_numeric(work["low"], errors="coerce").to_numpy(dtype=np.float64)
        close = pd.to_numeric(work["close"], errors="coerce").to_numpy(dtype=np.float64)
        n = len(work)
        warm = int(p.pivot_left) + int(p.pivot_right) + max(int(p.lookback_bars), int(p.min_pivots) * 2)
        last_bar = n - 2 if use_closed_bar and n >= 2 else n - 1
        ph_all = fractal_pivot_high_indices(high, int(p.pivot_left), int(p.pivot_right))
        pl_all = fractal_pivot_low_indices(low, int(p.pivot_left), int(p.pivot_right))
        return cls(
            high=high,
            low=low,
            close=close,
            params=p,
            ph_all=ph_all,
            pl_all=pl_all,
            warm=warm,
            last_bar=last_bar,
        )

    def evaluate_at(self, t: int) -> CompressionZone | None:
        return _eval_bar(
            int(t),
            high=self.high,
            low=self.low,
            close=self.close,
            ph_all=self.ph_all,
            pl_all=self.pl_all,
            params=self.params,
        )

    def best_for_scanner(self, *, active_only: bool) -> CompressionZone | None:
        end_t = int(self.last_bar)
        if end_t < self.warm:
            return None
        if active_only:
            return self.evaluate_at(end_t)
        best: CompressionZone | None = None
        start = max(self.warm, end_t - int(self.params.analysis_bars))
        for bar in range(end_t, start - 1, -1):
            hit = self.evaluate_at(bar)
            if hit is None:
                continue
            if best is None or float(hit.score) > float(best.score):
                best = hit
        return best


def _eval_bar(
    t: int,
    *,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    ph_all: list[int],
    pl_all: list[int],
    params: CompressionParams,
) -> CompressionZone | None:
    pr = int(params.pivot_right)
    mp = max(2, int(params.min_pivots))
    lb = max(5, int(params.lookback_bars))

    ph = [i for i in ph_all if i + pr <= t][-mp:]
    pl = [i for i in pl_all if i + pr <= t][-mp:]
    if len(ph) < mp or len(pl) < mp:
        return None

    hi_slope, hi_intercept, hi_r2 = _linreg(
        np.array(ph, dtype=np.float64),
        high[np.array(ph, dtype=np.int64)],
    )
    lo_slope, lo_intercept, lo_r2 = _linreg(
        np.array(pl, dtype=np.float64),
        low[np.array(pl, dtype=np.int64)],
    )

    upper, lower, w_now = channel_bounds_at(
        t, hi_slope=hi_slope, hi_intercept=hi_intercept, lo_slope=lo_slope, lo_intercept=lo_intercept
    )
    t_prev = t - lb
    if t_prev < 0:
        return None
    _, _, w_prev = channel_bounds_at(
        t_prev,
        hi_slope=hi_slope,
        hi_intercept=hi_intercept,
        lo_slope=lo_slope,
        lo_intercept=lo_intercept,
    )
    if w_prev <= 1e-12:
        return None
    compression_ratio = float(w_now / w_prev)
    if compression_ratio >= float(params.max_compression_ratio):
        return None

    mid_now = (upper + lower) / 2.0
    mid_prev = (
        channel_bounds_at(
            t_prev,
            hi_slope=hi_slope,
            hi_intercept=hi_intercept,
            lo_slope=lo_slope,
            lo_intercept=lo_intercept,
        )[0]
        + channel_bounds_at(
            t_prev,
            hi_slope=hi_slope,
            hi_intercept=hi_intercept,
            lo_slope=lo_slope,
            lo_intercept=lo_intercept,
        )[1]
    ) / 2.0
    ref_px = abs(float(close[t])) if np.isfinite(close[t]) and abs(close[t]) > 1e-12 else abs(mid_now)
    mid_slope_pct = (mid_now - mid_prev) / max(lb, 1) / ref_px * 100.0
    if abs(mid_slope_pct) > float(params.max_mid_slope_pct_per_bar):
        return None

    win_start = max(0, t - int(params.analysis_bars))
    ph_win = [i for i in ph_all if win_start <= i <= t and i + pr <= t]
    pl_win = [i for i in pl_all if win_start <= i <= t and i + pr <= t]

    upper_touches = _count_boundary_touches(
        ph_win,
        high,
        hi_slope=hi_slope,
        hi_intercept=hi_intercept,
        lo_slope=lo_slope,
        lo_intercept=lo_intercept,
        kind="high",
        touch_tolerance=params.touch_tolerance,
    )
    lower_touches = _count_boundary_touches(
        pl_win,
        low,
        hi_slope=hi_slope,
        hi_intercept=hi_intercept,
        lo_slope=lo_slope,
        lo_intercept=lo_intercept,
        kind="low",
        touch_tolerance=params.touch_tolerance,
    )
    if upper_touches < int(params.min_touches) or lower_touches < int(params.min_touches):
        return None

    formation_start = min(ph_win + pl_win) if (ph_win and pl_win) else min(ph + pl)
    formation_bars = max(1, int(t) - int(formation_start))

    score = compression_score(
        compression_ratio=compression_ratio,
        upper_touches=upper_touches,
        lower_touches=lower_touches,
        mid_slope_pct=mid_slope_pct,
        formation_bars=formation_bars,
        max_compression_ratio=params.max_compression_ratio,
        max_mid_slope_pct_per_bar=params.max_mid_slope_pct_per_bar,
        min_touches=params.min_touches,
        duration_cap_bars=params.duration_cap_bars,
    )
    if score < float(params.min_score):
        return None

    zone = CompressionZone(
        start_idx=int(formation_start),
        end_idx=int(t),
        score=score,
        compression_ratio=compression_ratio,
        upper_slope=hi_slope,
        lower_slope=lo_slope,
        mid_slope_pct=mid_slope_pct,
        upper_touches=upper_touches,
        lower_touches=lower_touches,
        upper_r2=hi_r2,
        lower_r2=lo_r2,
        upper_price=float(upper),
        lower_price=float(lower),
        formation_bars=formation_bars,
    )

    ctx = CompressionFilterContext(
        t=t,
        high=high,
        low=low,
        close=close,
        formation_start=int(formation_start),
        upper=float(upper),
        lower=float(lower),
        width=float(w_now),
        hi_slope=hi_slope,
        lo_slope=lo_slope,
        hi_intercept=hi_intercept,
        lo_intercept=lo_intercept,
        mid_slope_pct=mid_slope_pct,
    )
    if not passes_quality_filters(zone, ctx, params):
        return None
    return zone


def detect_compression_zones(
    df: pd.DataFrame,
    params: CompressionParams | None = None,
    *,
    use_closed_bar: bool = True,
) -> list[CompressionZone]:
    """Ищет участки сжатия на OHLC. Возвращает зоны (слитые подряд идущие бары)."""
    if df is None or df.empty:
        return []
    ev = CompressionEvaluator.from_dataframe(df, params, use_closed_bar=use_closed_bar)
    n = len(ev.high)
    if n < max(ev.params.pivot_left + ev.params.pivot_right + ev.params.lookback_bars + ev.params.min_pivots * 3, 40):
        return []

    active_flags: list[CompressionZone | None] = [None] * n
    for t in range(ev.warm, ev.last_bar + 1):
        active_flags[t] = ev.evaluate_at(t)

    zones: list[CompressionZone] = []
    i = ev.warm
    while i <= ev.last_bar:
        hit = active_flags[i]
        if hit is None:
            i += 1
            continue
        start = int(hit.start_idx)
        best = hit
        j = i + 1
        while j <= ev.last_bar and active_flags[j] is not None:
            cur = active_flags[j]
            if cur is not None:
                start = min(start, int(cur.start_idx))
                if cur.score > best.score:
                    best = cur
            j += 1
        zones.append(
            CompressionZone(
                start_idx=start,
                end_idx=j - 1,
                score=best.score,
                compression_ratio=best.compression_ratio,
                upper_slope=best.upper_slope,
                lower_slope=best.lower_slope,
                mid_slope_pct=best.mid_slope_pct,
                upper_touches=best.upper_touches,
                lower_touches=best.lower_touches,
                upper_r2=best.upper_r2,
                lower_r2=best.lower_r2,
                upper_price=best.upper_price,
                lower_price=best.lower_price,
                formation_bars=max(best.formation_bars, j - 1 - start),
            )
        )
        i = j

    if not zones:
        return []
    cap = max(1, min(8, len(zones)))
    return zones[-cap:]


def compression_zone_for_scanner(
    df: pd.DataFrame,
    params: CompressionParams | None = None,
    *,
    use_closed_bar: bool = True,
    active_only: bool = False,
) -> CompressionZone | None:
    """Быстрая оценка для сканера — без полного detect по всем барам."""
    if df is None or df.empty:
        return None
    ev = CompressionEvaluator.from_dataframe(df, params, use_closed_bar=use_closed_bar)
    return ev.best_for_scanner(active_only=active_only)


def active_compression_at_bar(
    df: pd.DataFrame,
    params: CompressionParams | None = None,
    *,
    use_closed_bar: bool = True,
) -> CompressionZone | None:
    """Активная зона сжатия на последнем (закрытом) баре — для сканера."""
    return compression_zone_for_scanner(df, params, use_closed_bar=use_closed_bar, active_only=True)


def add_compression_traces(
    fig: go.Figure,
    df: pd.DataFrame,
    zones: list[CompressionZone],
    *,
    row: int = 1,
    high: np.ndarray | None = None,
    low: np.ndarray | None = None,
    pivot_left: int = 5,
    pivot_right: int = 5,
    min_pivots: int = 3,
) -> None:
    """Подсветка канала, границы, начало формирования и Score на OHLC."""
    if df is None or df.empty or not zones:
        return
    if "timestamp" not in df.columns:
        return

    h = high if high is not None else pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=np.float64)
    l = low if low is not None else pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=np.float64)
    ts = df["timestamp"]
    ph_all = fractal_pivot_high_indices(h, pivot_left, pivot_right)
    pl_all = fractal_pivot_low_indices(l, pivot_left, pivot_right)

    for zi, zone in enumerate(zones):
        start = int(zone.start_idx)
        end = int(zone.end_idx)
        mp = max(2, int(min_pivots))
        ph = [i for i in ph_all if i + pivot_right <= end][-mp:]
        pl = [i for i in pl_all if i + pivot_right <= end][-mp:]
        if len(ph) < 2 or len(pl) < 2:
            continue
        hi_slope, hi_intercept, _ = _linreg(np.array(ph, dtype=np.float64), h[np.array(ph, dtype=np.int64)])
        lo_slope, lo_intercept, _ = _linreg(np.array(pl, dtype=np.float64), l[np.array(pl, dtype=np.int64)])

        idx_slice = list(range(start, end + 1))
        x_line = ts.iloc[idx_slice]
        xs = np.array(idx_slice, dtype=np.float64)
        y_upper = hi_slope * xs + hi_intercept
        y_lower = lo_slope * xs + lo_intercept

        fill_color = "rgba(168,85,247,0.14)" if zi == len(zones) - 1 else "rgba(100,116,139,0.10)"
        line_color = "#c084fc" if zi == len(zones) - 1 else "#94a3b8"

        fig.add_trace(
            go.Scatter(
                x=x_line,
                y=y_upper,
                mode="lines",
                line=dict(color=line_color, width=1.2, dash="dot"),
                name="Compression upper" if zi == 0 else f"_comp_up_{zi}",
                showlegend=(zi == 0),
                hoverinfo="skip",
            ),
            row=row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=x_line,
                y=y_lower,
                mode="lines",
                line=dict(color=line_color, width=1.2, dash="dot"),
                fill="tonexty",
                fillcolor=fill_color,
                name="Price Compression" if zi == 0 else f"_comp_lo_{zi}",
                showlegend=(zi == 0),
                hovertemplate=(
                    f"Сжатие · Score {zone.score:.0f}<br>"
                    f"ratio {zone.compression_ratio:.2f}<br>"
                    f"касания ↑{zone.upper_touches} ↓{zone.lower_touches}<br>"
                    f"формирование {zone.formation_bars} бар."
                    "<extra></extra>"
                ),
            ),
            row=row,
            col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=[ts.iloc[start]],
                y=[(y_upper[0] + y_lower[0]) / 2.0],
                mode="markers+text",
                marker=dict(symbol="triangle-right", size=10, color="#e879f9"),
                text=["▶"],
                textposition="middle right",
                textfont=dict(size=11, color="#e879f9"),
                name="_comp_start",
                showlegend=False,
                hovertemplate=f"Начало накопления<br>бар {start}<extra></extra>",
            ),
            row=row,
            col=1,
        )

        ann_y = float(y_upper[-1])
        fig.add_trace(
            go.Scatter(
                x=[ts.iloc[end]],
                y=[ann_y],
                mode="text",
                text=[f"Score {zone.score:.0f} · r={zone.compression_ratio:.2f}"],
                textfont=dict(size=10, color="#f5d0fe"),
                textposition="top left",
                showlegend=False,
                hovertemplate=(
                    f"Score {zone.score:.1f}<br>"
                    f"верх {zone.upper_price:.4g} · низ {zone.lower_price:.4g}<br>"
                    f"сжатие {zone.compression_ratio:.3f}"
                    "<extra></extra>"
                ),
            ),
            row=row,
            col=1,
        )


def latest_compression_at_end(
    df: pd.DataFrame,
    params: CompressionParams | None = None,
    *,
    use_closed_bar: bool = True,
) -> CompressionZone | None:
    zones = detect_compression_zones(df, params, use_closed_bar=use_closed_bar)
    if not zones:
        return None
    return zones[-1]
