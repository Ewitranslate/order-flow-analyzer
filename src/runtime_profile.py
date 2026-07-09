"""Профиль исполнения: Render free tier (~512 MB) и локальная разработка."""

from __future__ import annotations

import os


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def is_render_host() -> bool:
    return _truthy(os.environ.get("RENDER"))


def low_memory_mode() -> bool:
    explicit = os.environ.get("LOW_MEMORY_MODE", "").strip().lower()
    if explicit in ("0", "false", "no", "off"):
        return False
    if _truthy(explicit):
        return True
    return is_render_host()


def oi_max_workers() -> int:
    default = 4 if low_memory_mode() else 14
    raw = os.environ.get("OI_MAX_WORKERS", "").strip()
    return max(1, int(raw or default))


def oi_max_illiquid_verify() -> int:
    default = 12 if low_memory_mode() else 80
    raw = os.environ.get("OI_MAX_ILLIQUID_VERIFY", "").strip()
    return max(0, int(raw or default))


def oi_top_symbols_cap() -> int:
    """0 — без ограничения (полный алгоритм). На Render — топ по объёму."""
    default = 50 if low_memory_mode() else 0
    raw = os.environ.get("OI_TOP_SYMBOLS_CAP", "").strip()
    return max(0, int(raw or default))


def oi_skip_cold_rebuild() -> bool:
    explicit = os.environ.get("OI_SKIP_COLD_REBUILD", "").strip().lower()
    if explicit in ("0", "false", "no", "off"):
        return False
    if _truthy(explicit):
        return True
    return low_memory_mode()


def klines_fetch_limit(default_full: int = 500) -> int:
    default = min(default_full, 300) if low_memory_mode() else default_full
    raw = os.environ.get("KLINES_FETCH_LIMIT", "").strip()
    return max(50, int(raw or default))


def scanner_default_workers() -> int:
    raw = os.environ.get("SCANNER_MAX_WORKERS", "").strip()
    if raw:
        return max(2, int(raw))
    return 4 if low_memory_mode() else 12


def scanner_default_top_n() -> int:
    raw = os.environ.get("SCANNER_DEFAULT_TOP_N", "").strip()
    if raw:
        return max(1, int(raw))
    return 50 if low_memory_mode() else 200


def scanner_pair_cap_max() -> int:
    raw = os.environ.get("SCANNER_PAIR_CAP_MAX", "").strip()
    if raw:
        return max(10, int(raw))
    return 200 if low_memory_mode() else 800


def cache_max_entries_light() -> int:
    return 4 if low_memory_mode() else 64


def cache_max_entries_medium() -> int:
    return 8 if low_memory_mode() else 128
