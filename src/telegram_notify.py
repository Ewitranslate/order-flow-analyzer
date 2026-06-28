"""
Отправка уведомлений Cripto Scanner в Telegram Bot API.

Настройка: `.streamlit/secrets.toml` → секция `[telegram]` (см. secrets.toml.example)
или переменные окружения `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from williams_scanner import sort_scanner_results_by_age

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

TelegramDivFilter = Literal["any", "with", "without", "bearish", "bullish"]
TelegramSmaFilter = Literal["any", "above", "below"]
TelegramNotifyProfile = Literal["all_scan", "mirror_scan", "custom"]

TG_PARAM_ZONE = "zone"
TG_PARAM_TF = "timeframe"
TG_PARAM_DIV = "div"
TG_PARAM_SMA = "sma"
TG_PARAM_TICKER = "ticker"

TG_PARAM_LABELS_RU: dict[str, str] = {
    TG_PARAM_ZONE: "Зона EMA",
    TG_PARAM_TF: "Таймфрейм",
    TG_PARAM_DIV: "Дивергенция δ",
    TG_PARAM_SMA: "Цена vs SMA",
    TG_PARAM_TICKER: "Тикер",
}

_TELEGRAM_MSG_LIMIT = 4096
_SAFE_MSG_LIMIT = 3900
_TELEGRAM_API = "https://api.telegram.org"
_DEFAULT_TIMEOUT_SEC = 45.0
_MAX_RETRIES = 3


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def _strip_toml_value(raw: str) -> str:
    v = (raw or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1].strip()
    return v


def _load_telegram_from_secrets_file() -> dict[str, str]:
    """Fallback: читает `.streamlit/secrets.toml` напрямую (если st.secrets недоступен)."""
    path = _PROJECT_ROOT / ".streamlit" / "secrets.toml"
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    in_section = False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("[") and s.endswith("]"):
                in_section = s[1:-1].strip().lower() == "telegram"
                continue
            if not in_section or "=" not in line:
                continue
            key, val = line.split("=", 1)
            k = key.strip().lower()
            v = _strip_toml_value(val)
            if k == "bot_token" and v:
                out["bot_token"] = v
            elif k == "chat_id" and v:
                out["chat_id"] = v
            elif k == "enabled" and _truthy(v):
                out["enabled"] = "1"
            elif k == "proxy" and v:
                out["proxy"] = v
    except OSError:
        return {}
    return out


def load_telegram_config() -> dict[str, str]:
    """Токен, chat_id и proxy из Streamlit secrets, secrets.toml или env."""
    out: dict[str, str] = {}
    try:
        import streamlit as st

        raw = st.secrets.get("telegram", {})
        if isinstance(raw, dict):
            if str(raw.get("bot_token", "")).strip():
                out["bot_token"] = str(raw["bot_token"]).strip()
            if str(raw.get("chat_id", "")).strip():
                out["chat_id"] = str(raw["chat_id"]).strip()
            if str(raw.get("proxy", "")).strip():
                out["proxy"] = str(raw["proxy"]).strip()
            if _truthy(raw.get("enabled")):
                out["enabled"] = "1"
    except Exception:
        pass

    file_cfg = _load_telegram_from_secrets_file()
    for k, v in file_cfg.items():
        out.setdefault(k, v)

    if os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
        out["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    if os.environ.get("TELEGRAM_CHAT_ID", "").strip():
        out["chat_id"] = os.environ["TELEGRAM_CHAT_ID"].strip()
    if os.environ.get("TELEGRAM_PROXY", "").strip():
        out["proxy"] = os.environ["TELEGRAM_PROXY"].strip()
    if os.environ.get("TELEGRAM_ENABLED", "").strip():
        if _truthy(os.environ.get("TELEGRAM_ENABLED")):
            out["enabled"] = "1"
    return out


def _format_telegram_network_error(exc: Exception) -> str:
    msg = str(exc).strip()
    low = msg.lower()
    if "timed out" in low or isinstance(exc, TimeoutError):
        return (
            "таймаут api.telegram.org — нет доступа к Telegram API. "
            "Включите VPN или задайте `telegram.proxy` в secrets.toml "
            "(например `http://127.0.0.1:7890` для Clash / `socks5://127.0.0.1:1080`)."
        )
    if "connection refused" in low or "network is unreachable" in low:
        return f"нет соединения с Telegram API ({msg}). Проверьте VPN/прокси."
    return msg


@contextmanager
def _socks_proxy_context(proxy_url: str):
    """Временно маршрутизирует socket через SOCKS (PySocks)."""
    import socks

    parsed = urllib.parse.urlparse(proxy_url)
    scheme = (parsed.scheme or "").lower()
    if scheme == "socks4":
        ptype = socks.SOCKS4
    else:
        ptype = socks.SOCKS5
    rdns = scheme in ("socks5h",)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 1080)
    user = parsed.username or None
    password = parsed.password or None
    orig_socket = socket.socket
    try:
        socks.set_default_proxy(ptype, host, port, rdns=rdns, username=user, password=password)
        socket.socket = socks.socksocket
        yield
    finally:
        socket.socket = orig_socket
        socks.set_default_proxy()


def _telegram_urlopen(req: urllib.request.Request, *, proxy_url: str | None, timeout_sec: float):
    proxy = (proxy_url or "").strip()
    if proxy:
        parsed = urllib.parse.urlparse(proxy)
        scheme = (parsed.scheme or "").lower()
        if scheme.startswith("socks"):
            with _socks_proxy_context(proxy):
                return urllib.request.urlopen(req, timeout=timeout_sec)
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
        return opener.open(req, timeout=timeout_sec)
    return urllib.request.urlopen(req, timeout=timeout_sec)


def _telegram_api_post(
    bot_token: str,
    method: str,
    payload: dict[str, Any],
    *,
    proxy_url: str | None = None,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
) -> tuple[bool, str, dict[str, Any] | None]:
    token = (bot_token or "").strip()
    if not token:
        return False, "не задан bot_token", None
    url = f"{_TELEGRAM_API}/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "orderflow-williams-scan/1",
        },
    )
    last_err = "unknown error"
    for attempt in range(_MAX_RETRIES):
        try:
            with _telegram_urlopen(req, proxy_url=proxy_url, timeout_sec=timeout_sec) as resp:
                raw = json.loads(resp.read().decode())
            if not isinstance(raw, dict) or not raw.get("ok"):
                desc = raw.get("description", "unknown error") if isinstance(raw, dict) else "invalid response"
                return False, str(desc), raw if isinstance(raw, dict) else None
            return True, "ok", raw
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()
                err = json.loads(body).get("description", body)
            except Exception:
                err = str(e)
            return False, f"HTTP {e.code}: {err}", None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as e:
            last_err = _format_telegram_network_error(e)
            if attempt + 1 < _MAX_RETRIES:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False, last_err, None
    return False, last_err, None


def check_telegram_connection(
    *,
    bot_token: str | None = None,
    proxy_url: str | None = None,
) -> tuple[bool, str]:
    """Проверка getMe — быстрее, чем sendMessage."""
    cfg = load_telegram_config()
    token = (bot_token or cfg.get("bot_token") or "").strip()
    proxy = (proxy_url or cfg.get("proxy") or "").strip() or None
    if not token:
        return False, "не задан bot_token"
    ok, detail, raw = _telegram_api_post(token, "getMe", {}, proxy_url=proxy)
    if not ok:
        return False, detail
    username = ""
    if isinstance(raw, dict):
        res = raw.get("result")
        if isinstance(res, dict):
            username = str(res.get("username") or "")
    if username:
        return True, f"бот @{username} доступен"
    return True, "соединение с Telegram API OK"


def telegram_configured(cfg: dict[str, str] | None = None) -> bool:
    c = cfg if cfg is not None else load_telegram_config()
    return bool(c.get("bot_token") and c.get("chat_id"))


def escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@dataclass(frozen=True)
class TelegramNotifyFilters:
    """По каким параметрам поиска отбирать строки для Telegram."""

    zones: frozenset[str] | None = None  # overbought | oversold; None = все
    timeframes: frozenset[str] | None = None  # ключи 5m, 1h…; None = все
    div_filter: TelegramDivFilter = "any"
    sma_filter: TelegramSmaFilter = "any"
    sma_length: int | None = None  # период SMA(close); None = из сканера
    min_signals: int = 1
    send_if_empty: bool = False
    ticker_needle: str = ""
    active_params: frozenset[str] = frozenset()  # пусто = без отбора по параметрам


def build_mirror_scan_telegram_filters(
    *,
    zone_mode: str,
    tf_keys: list[str],
    div_mode: str,
    require_divergence: bool,
    check_divergence: bool,
    sma_filter_mode: str,
    sma_length: int,
    ticker_needle: str,
    min_signals: int = 1,
    send_if_empty: bool = False,
) -> TelegramNotifyFilters:
    """Параметры Telegram = текущие параметры сканера."""
    if zone_mode == "overbought":
        zones: list[str] = ["overbought"]
    elif zone_mode == "oversold":
        zones = ["oversold"]
    else:
        zones = ["overbought", "oversold"]

    div_filter: TelegramDivFilter = "any"
    if check_divergence:
        if require_divergence:
            div_filter = "with"
        elif div_mode == "bearish":
            div_filter = "bearish"
        elif div_mode == "bullish":
            div_filter = "bullish"

    sma_filter: TelegramSmaFilter = "any"
    if sma_filter_mode == "above":
        sma_filter = "above"
    elif sma_filter_mode == "below":
        sma_filter = "below"

    active: set[str] = {TG_PARAM_ZONE, TG_PARAM_TF}
    if check_divergence and div_filter != "any":
        active.add(TG_PARAM_DIV)
    if sma_filter != "any":
        active.add(TG_PARAM_SMA)
    if (ticker_needle or "").strip():
        active.add(TG_PARAM_TICKER)

    return TelegramNotifyFilters(
        zones=frozenset(zones),
        timeframes=frozenset(tf_keys),
        div_filter=div_filter,
        sma_filter=sma_filter,
        sma_length=int(sma_length) if sma_filter != "any" else None,
        min_signals=int(min_signals),
        send_if_empty=bool(send_if_empty),
        ticker_needle=str(ticker_needle or ""),
        active_params=frozenset(active),
    )


def apply_telegram_row_filters(
    df: pd.DataFrame | None,
    filters: TelegramNotifyFilters | None,
) -> pd.DataFrame:
    """Фильтры строк: зона, ТФ, δ, SMA, тикер."""
    if df is None or df.empty:
        return pd.DataFrame()
    if filters is None:
        return sort_scanner_results_by_age(df.copy())

    out = df.copy()
    active = filters.active_params
    if not active:
        return sort_scanner_results_by_age(out)

    if TG_PARAM_ZONE in active and filters.zones:
        if "zone" in out.columns:
            out = out[out["zone"].isin(filters.zones)]

    if TG_PARAM_TF in active and filters.timeframes:
        if "timeframe" in out.columns:
            out = out[out["timeframe"].isin(filters.timeframes)]

    if TG_PARAM_TICKER in active:
        needle = (filters.ticker_needle or "").strip().upper()
        if needle and "symbol" in out.columns:
            out = out[out["symbol"].astype(str).str.upper().str.contains(needle, na=False)]

    div_f = filters.div_filter
    if TG_PARAM_DIV in active and div_f != "any" and "div_kind" in out.columns:
        has_div = out["div_kind"].notna()
        if div_f == "with":
            out = out[has_div]
        elif div_f == "without":
            out = out[~has_div]
        elif div_f == "bearish":
            out = out[out["div_kind"] == "bearish"]
        elif div_f == "bullish":
            out = out[out["div_kind"] == "bullish"]

    sma_f = filters.sma_filter
    if TG_PARAM_SMA in active and sma_f != "any" and {"close", "close_sma"}.issubset(out.columns):
        close = pd.to_numeric(out["close"], errors="coerce")
        sma = pd.to_numeric(out["close_sma"], errors="coerce")
        valid = close.notna() & sma.notna()
        if sma_f == "above":
            out = out[valid & (close > sma)]
        elif sma_f == "below":
            out = out[valid & (close < sma)]

    return sort_scanner_results_by_age(out)


def apply_telegram_filters(df: pd.DataFrame | None, filters: TelegramNotifyFilters | None) -> pd.DataFrame:
    """Совместимость: только фильтры строк."""
    return apply_telegram_row_filters(df, filters)


def telegram_profile_filters_rows(profile: TelegramNotifyProfile) -> bool:
    """True, если для профиля нужен пост-фильтр строк (не all_scan)."""
    return profile in ("mirror_scan", "custom")


def resolve_scan_params_from_telegram(
    *,
    tg_enabled: bool,
    tg_profile: TelegramNotifyProfile,
    tg_filters: TelegramNotifyFilters,
    scanner_zones: frozenset[str],
    scanner_tf_keys: list[str],
    scanner_sma_filter_mode: str,
    scanner_check_divergence: bool,
    scanner_require_divergence: bool,
    scanner_div_kinds: frozenset[str],
    scanner_sma_length: int,
    align_scan: bool,
) -> tuple[
    frozenset[str],
    list[str],
    str,
    bool,
    bool,
    frozenset[str],
    int,
]:
    """
    Сузить параметры run_williams_scan под фильтры Telegram (режим custom + автоскан).

    Возвращает: zones, tf_keys, sma_filter_mode, check_divergence, require_divergence, div_kinds, sma_length.
    """
    zones = scanner_zones
    tf_keys = list(scanner_tf_keys)
    sma_mode = scanner_sma_filter_mode
    sma_len = int(scanner_sma_length)
    check_div = scanner_check_divergence
    require_div = scanner_require_divergence
    div_kinds = scanner_div_kinds

    if not (tg_enabled and tg_profile == "custom" and align_scan):
        return zones, tf_keys, sma_mode, check_div, require_div, div_kinds, sma_len

    active = tg_filters.active_params
    if TG_PARAM_ZONE in active and tg_filters.zones:
        zones = frozenset(tg_filters.zones)

    if TG_PARAM_TF in active and tg_filters.timeframes:
        matched = [t for t in scanner_tf_keys if t in tg_filters.timeframes]
        tf_keys = matched if matched else list(tg_filters.timeframes)

    if TG_PARAM_SMA in active:
        if tg_filters.sma_filter == "above":
            sma_mode = "above"
        elif tg_filters.sma_filter == "below":
            sma_mode = "below"
        if tg_filters.sma_length and int(tg_filters.sma_length) > 1:
            sma_len = int(tg_filters.sma_length)

    if TG_PARAM_DIV in active and check_div:
        div_f = tg_filters.div_filter
        if div_f == "with":
            require_div = True
        elif div_f == "bearish":
            div_kinds = frozenset({"bearish"})
            require_div = False
        elif div_f == "bullish":
            div_kinds = frozenset({"bullish"})
            require_div = False
        elif div_f == "without":
            require_div = False

    return zones, tf_keys, sma_mode, check_div, require_div, div_kinds, sma_len


def telegram_filters_summary(
    filters: TelegramNotifyFilters | None,
    *,
    profile: TelegramNotifyProfile = "all_scan",
) -> str:
    if profile == "all_scan" or filters is None:
        return "все результаты скана"
    parts: list[str] = []
    active = filters.active_params
    if TG_PARAM_ZONE in active and filters.zones:
        parts.append("зоны: " + ", ".join(sorted(filters.zones)))
    if TG_PARAM_TF in active and filters.timeframes:
        parts.append("ТФ: " + ", ".join(sorted(filters.timeframes)))
    if TG_PARAM_DIV in active and filters.div_filter != "any":
        parts.append(f"δ: {filters.div_filter}")
    if TG_PARAM_SMA in active and filters.sma_filter != "any":
        sma_note = f"SMA({filters.sma_length})" if filters.sma_length else "SMA"
        parts.append(f"{sma_note}: {filters.sma_filter}")
    if TG_PARAM_TICKER in active and filters.ticker_needle:
        parts.append(f"тикер ∋ {filters.ticker_needle}")
    if filters.min_signals > 0:
        parts.append(f"мин. {filters.min_signals} сигн.")
    if filters.send_if_empty:
        parts.append("слать даже если пусто")
    if profile == "mirror_scan" and not parts:
        return "как параметры скана"
    return " · ".join(parts) if parts else "свои параметры (без отбора)"


def scanner_results_signature(df: pd.DataFrame | None) -> str:
    """Стабильный отпечаток результатов — чтобы не слать дубликаты."""
    if df is None or df.empty:
        return "empty"
    cols = [c for c in ("symbol", "timeframe", "zone", "willy_ema", "close", "div_kind") if c in df.columns]
    if not cols:
        return f"rows:{len(df)}"
    part = df[cols].sort_values(cols).to_csv(index=False)
    return hashlib.sha256(part.encode("utf-8")).hexdigest()[:24]


def format_scanner_results_message(
    df: pd.DataFrame | None,
    *,
    tf_keys: list[str] | None = None,
    pool_size: int | None = None,
    max_rows: int = 25,
    filter_note: str | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "<b>Cripto Scanner</b>",
        f"<i>{escape_html(now)}</i>",
    ]
    if tf_keys:
        lines.append(f"ТФ скана: {escape_html(', '.join(tf_keys))}")
    if pool_size is not None:
        lines.append(f"Пар в скане: {int(pool_size)}")
    if filter_note:
        lines.append(f"Фильтр TG: {escape_html(filter_note)}")

    if df is None or df.empty:
        lines.append("")
        lines.append("Сигналов по фильтру Telegram <b>нет</b>.")
        return "\n".join(lines)

    df = sort_scanner_results_by_age(df)

    n_ob = int((df["zone"] == "overbought").sum()) if "zone" in df.columns else 0
    n_os = int((df["zone"] == "oversold").sum()) if "zone" in df.columns else 0
    has_div = df["div_kind"].notna() if "div_kind" in df.columns else pd.Series(False, index=df.index)
    n_div = int(has_div.sum())

    lines.extend(
        [
            "",
            f"Всего: <b>{len(df)}</b> · перекуп {n_ob} · перепрод {n_os} · с δ {n_div}",
            "",
        ]
    )

    show = df.head(max(1, int(max_rows)))
    for _, row in show.iterrows():
        sym = escape_html(str(row.get("symbol", "—")))
        tf = escape_html(str(row.get("tf_ru") or row.get("timeframe", "—")))
        zone = escape_html(str(row.get("zone_ru") or row.get("zone", "—")))
        ema = row.get("willy_ema")
        close = row.get("close")
        close_sma = row.get("close_sma")
        bar_t = row.get("bar_time")
        ema_s = f"{float(ema):.2f}" if pd.notna(ema) else "—"
        close_s = f"{float(close):.6g}" if pd.notna(close) else "—"
        time_s = ""
        if pd.notna(bar_t):
            time_s = f" · {escape_html(pd.to_datetime(bar_t, utc=True).strftime('%m-%d %H:%M'))}"
        sma_s = ""
        if pd.notna(close_sma):
            sma_s = f" · SMA {float(close_sma):.6g}"
        div = row.get("div_ru") or row.get("div_kind")
        div_s = f" · δ {escape_html(str(div))}" if div and str(div) not in ("—", "nan", "None") else ""
        cd24 = row.get("cum_delta_24h_change")
        cd24_s = ""
        if pd.notna(cd24):
            cd24_s = f" · Δδ24ч {float(cd24):+.4g}"
        oi24 = row.get("oi_24h_change")
        oi24_s = ""
        if pd.notna(oi24):
            oi24_s = f" · ΔOI24ч {float(oi24):+.4g}"
        lines.append(
            f"• <b>{sym}</b> · {tf}{time_s} · {zone} · EMA {ema_s} · {close_s}{sma_s}{cd24_s}{oi24_s}{div_s}"
        )

    if len(df) > len(show):
        lines.append(f"… ещё <b>{len(df) - len(show)}</b> строк(и)")

    text = "\n".join(lines)
    if len(text) > _SAFE_MSG_LIMIT:
        text = text[: _SAFE_MSG_LIMIT - 24] + "\n… <i>(сообщение обрезано)</i>"
    return text


def send_telegram_message(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = True,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    proxy_url: str | None = None,
) -> tuple[bool, str]:
    """POST sendMessage. Возвращает (успех, описание)."""
    token = (bot_token or "").strip()
    cid = (chat_id or "").strip()
    if not token or not cid:
        return False, "не задан bot_token или chat_id"

    cfg = load_telegram_config()
    proxy = (proxy_url or cfg.get("proxy") or "").strip() or None
    payload = {
        "chat_id": cid,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    ok, detail, _ = _telegram_api_post(
        token,
        "sendMessage",
        payload,
        proxy_url=proxy,
        timeout_sec=timeout_sec,
    )
    if not ok:
        return False, detail
    return True, "отправлено"


def maybe_notify_scanner_results(
    df: pd.DataFrame | None,
    *,
    enabled: bool,
    notify_mode: str,
    tf_keys: list[str],
    pool_size: int,
    max_rows: int,
    notify_filters: TelegramNotifyFilters | None = None,
    notify_profile: TelegramNotifyProfile = "all_scan",
    proxy_url: str | None = None,
    last_signature_key: str = "williams_tg_last_sig",
    last_status_key: str = "williams_tg_last_status",
) -> tuple[bool, str]:
    """
    Отправить результаты, если включено и (при notify_mode=on_change) результат изменился.
    Использует session_state для дедупликации.
    """
    if not enabled:
        return False, "выкл"

    cfg = load_telegram_config()
    if not telegram_configured(cfg):
        return False, "нет bot_token/chat_id в secrets.toml"

    import streamlit as st

    filt = notify_filters or TelegramNotifyFilters()
    if notify_profile == "all_scan":
        filtered = df.copy() if df is not None and not df.empty else pd.DataFrame()
        note = telegram_filters_summary(filt, profile="all_scan")
        if filt.min_signals > 0 or filt.send_if_empty:
            extra = telegram_filters_summary(
                TelegramNotifyFilters(min_signals=filt.min_signals, send_if_empty=filt.send_if_empty),
                profile="custom",
            )
            if extra != "свои параметры (без отбора)":
                note = extra
    else:
        filtered = apply_telegram_row_filters(df, filt)
        note = telegram_filters_summary(filt, profile=notify_profile)

    if len(filtered) < max(0, int(filt.min_signals)):
        st.session_state[last_status_key] = (
            f"пропуск · меньше {int(filt.min_signals)} сигнал(ов) после фильтра"
        )
        return False, st.session_state[last_status_key]
    if filtered.empty and not filt.send_if_empty:
        st.session_state[last_status_key] = "пропуск · нет сигналов по фильтру TG"
        return False, "пропуск · пусто"

    sig = scanner_results_signature(filtered)
    if notify_mode == "on_change" and st.session_state.get(last_signature_key) == sig:
        return False, "без изменений"

    msg = format_scanner_results_message(
        filtered,
        tf_keys=tf_keys,
        pool_size=pool_size,
        max_rows=max_rows,
        filter_note=note,
    )
    proxy = (proxy_url or cfg.get("proxy") or "").strip() or None
    ok, detail = send_telegram_message(
        msg,
        bot_token=cfg["bot_token"],
        chat_id=cfg["chat_id"],
        proxy_url=proxy,
    )
    if ok:
        st.session_state[last_signature_key] = sig
    st.session_state[last_status_key] = f"{'OK' if ok else 'ошибка'} · {detail}"
    return ok, detail
