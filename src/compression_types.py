"""Типы и параметры Price Compression (без логики — для модульных фильтров)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CompressionParams:
    pivot_left: int = 5
    pivot_right: int = 5
    min_pivots: int = 3
    min_touches: int = 3
    lookback_bars: int = 20
    max_compression_ratio: float = 0.65
    max_mid_slope_pct_per_bar: float = 0.08
    touch_tolerance: float = 0.08
    analysis_bars: int = 120
    min_score: int = 75
    # Фильтры качества (снижение ложных сигналов)
    min_formation_bars: int = 10
    min_containment_ratio: float = 0.80
    min_width_pct: float = 0.12
    max_width_pct: float = 5.0
    max_spread_slope: float = 0.35
    duration_cap_bars: int = 48


# Рекомендуемые значения основных параметров (единый источник для UI и сканера).
RECOMMENDED_COMPRESSION_HELP_RU: dict[str, str] = {
    "min_pivots": "Три пивота формируют надёжную линию; два — слишком мало.",
    "min_touches": "Отсекает случайные диапазоны без подтверждения границ.",
    "lookback_bars": "Достаточно, чтобы увидеть реальное сужение без сильной задержки.",
    "max_compression_ratio": "Текущая ширина должна быть не более 65% от предыдущей.",
    "max_mid_slope_pct_per_bar": "0.08–0.10: горизонтальные диапазоны и небольшие треугольники.",
    "touch_tolerance": "Небольшой запас на крипте из-за длинных теней.",
    "analysis_bars": "15m ≈ 30 ч, 1h ≈ 5 суток — компромисс для накопления.",
    "min_score": "Ниже 75 — слишком много слабых сигналов.",
}


def recommended_compression_params() -> CompressionParams:
    """Копия параметров с рекомендуемыми значениями по умолчанию."""
    return CompressionParams()


def compression_session_key_map(key_prefix: str) -> dict[str, str]:
    """Имена ключей session_state для слайдеров Streamlit."""
    return {
        "min_pivots": f"{key_prefix}_min_pivots",
        "min_touches": f"{key_prefix}_min_touches",
        "lookback_bars": f"{key_prefix}_lookback",
        "max_compression_ratio": f"{key_prefix}_max_ratio",
        "max_mid_slope_pct_per_bar": f"{key_prefix}_max_slope",
        "touch_tolerance": f"{key_prefix}_touch_tol",
        "analysis_bars": f"{key_prefix}_analysis",
        "min_score": f"{key_prefix}_min_score",
    }


def compression_session_defaults(
    key_prefix: str,
    *,
    pivot_left: int = 5,
    pivot_right: int = 5,
) -> dict[str, object]:
    """Значения session_state для Price Compression (включая pivot L/R)."""
    p = recommended_compression_params()
    keys = compression_session_key_map(key_prefix)
    return {
        f"{key_prefix}_pivot_left": int(pivot_left),
        f"{key_prefix}_pivot_right": int(pivot_right),
        keys["min_pivots"]: p.min_pivots,
        keys["min_touches"]: p.min_touches,
        keys["lookback_bars"]: p.lookback_bars,
        keys["max_compression_ratio"]: p.max_compression_ratio,
        keys["max_mid_slope_pct_per_bar"]: p.max_mid_slope_pct_per_bar,
        keys["touch_tolerance"]: p.touch_tolerance,
        keys["analysis_bars"]: p.analysis_bars,
        keys["min_score"]: p.min_score,
    }


def init_compression_session_state(
    session_state: Mapping[str, Any],
    key_prefix: str,
    *,
    pivot_left: int = 5,
    pivot_right: int = 5,
    only_missing: bool = True,
) -> None:
    """Инициализирует session_state рекомендуемыми значениями."""
    for key, val in compression_session_defaults(
        key_prefix, pivot_left=pivot_left, pivot_right=pivot_right
    ).items():
        if only_missing and key in session_state:
            continue
        session_state[key] = val  # type: ignore[index]


def apply_recommended_compression_session_state(
    session_state: Mapping[str, Any],
    key_prefix: str,
    *,
    pivot_left: int | None = None,
    pivot_right: int | None = None,
) -> None:
    """Сбрасывает параметры сжатия к рекомендуемым (pivot L/R можно сохранить)."""
    pl = pivot_left
    pr = pivot_right
    if pl is None:
        pl = int(session_state.get(f"{key_prefix}_pivot_left", 5))  # type: ignore[arg-type]
    if pr is None:
        pr = int(session_state.get(f"{key_prefix}_pivot_right", 5))  # type: ignore[arg-type]
    for key, val in compression_session_defaults(key_prefix, pivot_left=pl, pivot_right=pr).items():
        session_state[key] = val  # type: ignore[index]


def compression_params_from_session(
    session_state: Mapping[str, Any],
    key_prefix: str,
) -> CompressionParams:
    """Собирает CompressionParams из session_state."""
    p = recommended_compression_params()
    keys = compression_session_key_map(key_prefix)
    return CompressionParams(
        pivot_left=int(session_state.get(f"{key_prefix}_pivot_left", p.pivot_left)),
        pivot_right=int(session_state.get(f"{key_prefix}_pivot_right", p.pivot_right)),
        min_pivots=int(session_state.get(keys["min_pivots"], p.min_pivots)),
        min_touches=int(session_state.get(keys["min_touches"], p.min_touches)),
        lookback_bars=int(session_state.get(keys["lookback_bars"], p.lookback_bars)),
        max_compression_ratio=float(session_state.get(keys["max_compression_ratio"], p.max_compression_ratio)),
        max_mid_slope_pct_per_bar=float(
            session_state.get(keys["max_mid_slope_pct_per_bar"], p.max_mid_slope_pct_per_bar)
        ),
        touch_tolerance=float(session_state.get(keys["touch_tolerance"], p.touch_tolerance)),
        analysis_bars=int(session_state.get(keys["analysis_bars"], p.analysis_bars)),
        min_score=int(session_state.get(keys["min_score"], p.min_score)),
    )


@dataclass(frozen=True)
class CompressionZone:
    """Участок сжатия / накопления на графике."""

    start_idx: int
    end_idx: int
    score: float
    compression_ratio: float
    upper_slope: float
    lower_slope: float
    mid_slope_pct: float
    upper_touches: int
    lower_touches: int
    upper_r2: float
    lower_r2: float
    upper_price: float = 0.0
    lower_price: float = 0.0
    formation_bars: int = 0


def default_compression_params_for_tf(tf_key: str) -> CompressionParams:
    """Дефолты по ТФ: 15m → L/R=3, 1h → L/R=5."""
    base = CompressionParams()
    if tf_key == "15m":
        return CompressionParams(pivot_left=3, pivot_right=3, min_formation_bars=12, duration_cap_bars=40)
    if tf_key == "1h":
        return CompressionParams(pivot_left=5, pivot_right=5, min_formation_bars=10, duration_cap_bars=48)
    return base
