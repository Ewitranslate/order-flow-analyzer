"""
Кэш и быстрая сборка списка USDT-M пар с историей Open Interest.

- Файл на диске (мгновенная загрузка при повторном запуске).
- Один запрос 24h ticker для ранжирования по объёму.
- Проверка openInterestHist только для топа по объёму; остальные ликвидные — без лишних запросов.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from futures_market import (
    _symbol_has_oi,
    fetch_futures_24hr_quote_volume,
    fetch_usdt_perpetual_symbols,
)

_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
_DEFAULT_TTL_SEC = 6 * 3600
_MIN_QUOTE_VOL_USDT = 50_000.0
_MAX_ILLIQUID_OI_VERIFY = 80


def _cache_path(period: str) -> Path:
    safe = "".join(c if c.isalnum() else "_" for c in period.strip().lower())
    return _CACHE_DIR / f"oi_symbols_{safe}.json"


def load_oi_symbol_cache(period: str, *, ttl_sec: float = _DEFAULT_TTL_SEC) -> list[str] | None:
    path = _cache_path(period)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        updated = float(raw.get("updated_at", 0))
        if time.time() - updated > float(ttl_sec):
            return None
        syms = raw.get("symbols")
        if isinstance(syms, list) and len(syms) >= 2:
            return sorted({str(s).upper() for s in syms if s})
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return None


def save_oi_symbol_cache(period: str, symbols: list[str], *, note: str = "") -> None:
    path = _cache_path(period)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "period": period,
        "updated_at": time.time(),
        "symbols": sorted({s.upper() for s in symbols if s}),
        "note": note,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")


def oi_cache_age_sec(period: str) -> float | None:
    path = _cache_path(period)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return max(0.0, time.time() - float(raw.get("updated_at", 0)))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def clear_oi_symbol_cache(period: str | None = None) -> None:
    if period is not None:
        p = _cache_path(period)
        if p.is_file():
            p.unlink()
        return
    if _CACHE_DIR.is_dir():
        for p in _CACHE_DIR.glob("oi_symbols_*.json"):
            p.unlink(missing_ok=True)


def _verify_oi_parallel(symbols: list[str], period: str, *, max_workers: int) -> set[str]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    found: set[str] = set()
    if not symbols:
        return found
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut_map = {ex.submit(_symbol_has_oi, s, period): s for s in symbols}
        for fut in as_completed(fut_map):
            r = fut.result()
            if r:
                found.add(r)
    return found


def build_oi_symbol_list(
    period: str,
    *,
    max_workers: int = 14,
    min_quote_vol: float = _MIN_QUOTE_VOL_USDT,
    max_illiquid_verify: int = _MAX_ILLIQUID_OI_VERIFY,
) -> list[str]:
    """
    Быстрая сборка списка пар с OI:
    1) exchangeInfo — все USDT perpetual;
    2) ticker/24hr — объёмы (1 запрос);
    3) ликвидные (quoteVolume >= порога) — в список без openInterestHist;
    4) низколиквидные — точечная проверка openInterestHist (не более max_illiquid_verify).
    """
    all_perp = fetch_usdt_perpetual_symbols()
    if len(all_perp) < 2:
        return all_perp

    vols = fetch_futures_24hr_quote_volume()
    liquid: list[str] = []
    illiquid: list[str] = []
    for sym in all_perp:
        if vols.get(sym, 0.0) >= float(min_quote_vol):
            liquid.append(sym)
        else:
            illiquid.append(sym)

    out: set[str] = set(liquid)
    if illiquid:
        check = illiquid[: max(0, int(max_illiquid_verify))]
        out.update(_verify_oi_parallel(check, period, max_workers=max_workers))

    if len(out) < 2:
        out.update(all_perp[: min(30, len(all_perp))])

    return sorted(out)


def list_symbols_with_open_interest_fast(
    period: str,
    *,
    rebuild: bool = False,
    ttl_sec: float = _DEFAULT_TTL_SEC,
    max_workers: int = 14,
) -> tuple[list[str], str]:
    """
    Возвращает (symbols, source_label).
    source_label: file-cache | rebuilt | file-stale+rebuilt
    """
    if not rebuild:
        cached = load_oi_symbol_cache(period, ttl_sec=ttl_sec)
        if cached:
            age_h = (oi_cache_age_sec(period) or 0) / 3600.0
            return cached, f"файл ({age_h:.1f} ч)"

    syms = build_oi_symbol_list(period, max_workers=max_workers)
    if len(syms) >= 2:
        save_oi_symbol_cache(period, syms, note="fast_build")
    label = "пересборка" if rebuild else "файл устарел → пересборка"
    return syms, label
