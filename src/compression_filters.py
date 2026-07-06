"""
Дополнительные фильтры качества для Price Compression.

Модульная точка расширения: объём, OI, δ и другие метрики подключаются
отдельными фильтрами без изменения базового алгоритма сужения.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from compression_types import CompressionParams, CompressionZone


@dataclass(frozen=True)
class CompressionFilterContext:
    """Контекст бара оценки для фильтров качества."""

    t: int
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    formation_start: int
    upper: float
    lower: float
    width: float
    hi_slope: float
    lo_slope: float
    hi_intercept: float
    lo_intercept: float
    mid_slope_pct: float


class CompressionQualityFilter(Protocol):
    def passes(
        self, zone: CompressionZone, ctx: CompressionFilterContext, params: CompressionParams
    ) -> bool: ...


def channel_bounds_at(
    i: int,
    *,
    hi_slope: float,
    hi_intercept: float,
    lo_slope: float,
    lo_intercept: float,
) -> tuple[float, float, float]:
    upper = hi_slope * i + hi_intercept
    lower = lo_slope * i + lo_intercept
    width = max(upper - lower, 1e-12)
    return upper, lower, width


class MinFormationBarsFilter:
    """Отсекает слишком короткие «микрозоны» на шуме."""

    def passes(
        self, zone: CompressionZone, ctx: CompressionFilterContext, params: CompressionParams
    ) -> bool:
        min_bars = max(8, int(params.min_formation_bars))
        return int(zone.formation_bars) >= min_bars


class PriceContainmentFilter:
    """Цена должна преимущественно оставаться внутри канала накопления."""

    def passes(
        self, zone: CompressionZone, ctx: CompressionFilterContext, params: CompressionParams
    ) -> bool:
        win_start = max(0, int(ctx.formation_start))
        end = int(ctx.t)
        if end <= win_start:
            return False
        inside = 0
        total = 0
        tol_mult = max(0.05, float(params.touch_tolerance) * 1.5)
        for i in range(win_start, end + 1):
            up, lo, w = channel_bounds_at(
                i,
                hi_slope=ctx.hi_slope,
                hi_intercept=ctx.hi_intercept,
                lo_slope=ctx.lo_slope,
                lo_intercept=ctx.lo_intercept,
            )
            tol = tol_mult * w
            c = float(ctx.close[i])
            h = float(ctx.high[i])
            l = float(ctx.low[i])
            if not (np.isfinite(c) and np.isfinite(h) and np.isfinite(l)):
                continue
            total += 1
            if (l >= lo - tol) and (h <= up + tol):
                inside += 1
        if total < 5:
            return False
        return (inside / total) >= float(params.min_containment_ratio)


class RelativeWidthFilter:
    """Слишком узкий или широкий канал — шум или не накопление."""

    def passes(
        self, zone: CompressionZone, ctx: CompressionFilterContext, params: CompressionParams
    ) -> bool:
        ref = abs(float(ctx.close[ctx.t]))
        if ref <= 1e-12:
            ref = abs((ctx.upper + ctx.lower) / 2.0)
        if ref <= 1e-12:
            return False
        width_pct = float(ctx.width) / ref * 100.0
        return float(params.min_width_pct) <= width_pct <= float(params.max_width_pct)


class DivergingChannelFilter:
    """Исключает расходящиеся границы (трендовая коррекция, а не сжатие)."""

    def passes(
        self, zone: CompressionZone, ctx: CompressionFilterContext, params: CompressionParams
    ) -> bool:
        spread_slope = float(ctx.hi_slope) - float(ctx.lo_slope)
        if spread_slope > float(params.max_spread_slope):
            return False
        t_prev = max(0, int(ctx.t) - max(5, int(params.lookback_bars) // 2))
        _, _, w_prev = channel_bounds_at(
            t_prev,
            hi_slope=ctx.hi_slope,
            hi_intercept=ctx.hi_intercept,
            lo_slope=ctx.lo_slope,
            lo_intercept=ctx.lo_intercept,
        )
        if w_prev <= 1e-12:
            return True
        return float(ctx.width) <= w_prev * 1.02


DEFAULT_QUALITY_FILTERS: tuple[CompressionQualityFilter, ...] = (
    MinFormationBarsFilter(),
    RelativeWidthFilter(),
    PriceContainmentFilter(),
    DivergingChannelFilter(),
)


def passes_quality_filters(
    zone: CompressionZone,
    ctx: CompressionFilterContext,
    params: CompressionParams,
    filters: tuple[CompressionQualityFilter, ...] | None = None,
) -> bool:
    chain = filters if filters is not None else DEFAULT_QUALITY_FILTERS
    return all(f.passes(zone, ctx, params) for f in chain)
