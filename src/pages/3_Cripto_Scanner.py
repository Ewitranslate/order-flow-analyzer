"""
Cripto Scanner — USDT spot пары Binance (Williams %R + EMA и другие критерии).

Запуск: `streamlit run src/app.py` → страница в боковом меню.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pandas as pd
import streamlit as st

from app import _FALLBACK_PAIRS_USDT, apply_dark_shell, cached_spot_usdt_symbol_list
from auth import render_auth_gate, render_auth_sidebar, require_page_access, PAGE_WILLIAMS
from telegram_notify import (
    TG_PARAM_DIV,
    TG_PARAM_LABELS_RU,
    TG_PARAM_SMA,
    TG_PARAM_TF,
    TG_PARAM_TICKER,
    TG_PARAM_ZONE,
    TelegramNotifyFilters,
    TelegramNotifyProfile,
    apply_telegram_row_filters,
    build_mirror_scan_telegram_filters,
    check_telegram_connection,
    format_scanner_results_message,
    load_telegram_config,
    maybe_notify_scanner_results,
    resolve_scan_params_from_telegram,
    send_telegram_message,
    telegram_configured,
    telegram_filters_summary,
    telegram_profile_filters_rows,
)
from williams_scanner import (
    CUM_DELTA_24H_INTERVAL,
    CUM_DELTA_24H_LABEL_RU,
    DIV_LABEL_RU,
    OI_24H_INTERVAL,
    OI_24H_LABEL_RU,
    SCANNER_TF_API,
    SCANNER_TF_LABELS_RU,
    ZONE_LABEL_RU,
    CumDelta24hFilterMode,
    DivKind,
    Oi24hFilterMode,
    ScannerSearchCriteria,
    WilliamsZone,
    search_criteria_summary_ru,
    fetch_spot_24h_quote_volume,
    format_timedelta_ru,
    run_williams_scan,
    sort_scanner_results_by_age,
)

SCAN_PAIR_CAP_MAX = 800
TELEGRAM_AUTO_SCAN_INTERVAL = timedelta(minutes=30)


def _load_spot_symbol_universe() -> tuple[list[str], bool]:
    """Список USDT spot; is_fallback=True если загружен только запасной список (~7 пар)."""
    syms = list(cached_spot_usdt_symbol_list())
    is_fb = len(syms) <= len(_FALLBACK_PAIRS_USDT) and set(syms) <= set(_FALLBACK_PAIRS_USDT)
    if is_fb and not st.session_state.get("spot_sym_auto_retry"):
        cached_spot_usdt_symbol_list.clear()
        st.session_state["spot_sym_auto_retry"] = True
        syms = list(cached_spot_usdt_symbol_list())
        is_fb = len(syms) <= len(_FALLBACK_PAIRS_USDT) and set(syms) <= set(_FALLBACK_PAIRS_USDT)
    return syms, is_fb


def _filter_symbols(symbols: list[str], needle: str) -> list[str]:
    q = (needle or "").strip().upper()
    if not q:
        return list(symbols)
    return [s for s in symbols if q in s.upper()]


def _rank_symbols_by_volume(symbols: list[str], vol_map: dict[str, float]) -> list[str]:
    return sorted(symbols, key=lambda s: float(vol_map.get(s.upper(), 0.0)), reverse=True)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_spot_volumes() -> dict[str, float]:
    try:
        return fetch_spot_24h_quote_volume()
    except Exception:
        return {}


def _show_results(
    df_show: pd.DataFrame,
    *,
    filter_note: str | None = None,
    df_full: pd.DataFrame | None = None,
    show_sma_column: bool = True,
) -> None:
    if filter_note:
        st.caption(f"Таблица по фильтру Telegram: **{filter_note}**")

    if df_show is None or df_show.empty:
        if df_full is not None and not df_full.empty:
            st.warning(
                f"По фильтру Telegram сигналов нет (в полном скане — **{len(df_full)}**)."
            )
            with st.expander(f"Все результаты скана ({len(df_full)})"):
                _show_results(df_full)
        else:
            st.success("Сигналов по выбранным зонам нет.")
        return

    n_ob = int((df_show["zone"] == "overbought").sum())
    n_os = int((df_show["zone"] == "oversold").sum())
    has_div = df_show["div_kind"].notna() if "div_kind" in df_show.columns else pd.Series(False, index=df_show.index)
    n_div = int(has_div.sum())
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Всего сигналов", len(df_show))
    m2.metric("Перекуп (EMA > −20)", n_ob)
    m3.metric("Перепрод (EMA < −80)", n_os)
    m4.metric("С дивергенцией δ", n_div)
    m5.metric("Уникальных пар", df_show["symbol"].nunique())

    display = df_show.copy()
    display["bar_time"] = pd.to_datetime(display["bar_time"], utc=True).dt.strftime("%Y-%m-%d %H:%M UTC")
    if "div_confirm_time" in display.columns:
        display["div_confirm_time"] = pd.to_datetime(display["div_confirm_time"], utc=True, errors="coerce").dt.strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    display["quote_vol_24h"] = pd.to_numeric(display["quote_vol_24h"], errors="coerce")
    display = display.rename(
        columns={
            "symbol": "Пара",
            "tf_ru": "Таймфрейм",
            "zone_ru": "Зона",
            "willy_ema": "EMA %R",
            "willy": "%R",
            "close": "Close",
            "close_sma": "SMA Close",
            "bar_time": "Бар Williams (UTC)",
            "quote_vol_24h": "Объём 24h USDT",
            "div_ru": "Дивергенция δ",
            "div_confirm_time": "Подтв. див. (UTC)",
            "div_bars_ago": "Див. баров назад",
            "cum_delta_24h_change": "Δкум. 24ч",
            "oi_24h_change": "ΔOI 24ч",
        }
    )
    show_cols = [
        "Пара",
        "Таймфрейм",
        "Зона",
        "Δкум. 24ч",
        "ΔOI 24ч",
        "Дивергенция δ",
        "Подтв. див. (UTC)",
        "Див. баров назад",
        "EMA %R",
        "%R",
        "Close",
    ]
    if show_sma_column:
        show_cols.append("SMA Close")
    show_cols.extend(["Бар Williams (UTC)", "Объём 24h USDT"])
    show_cols = [c for c in show_cols if c in display.columns]
    st.dataframe(
        display[show_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "EMA %R": st.column_config.NumberColumn(format="%.2f"),
            "%R": st.column_config.NumberColumn(format="%.2f"),
            "Close": st.column_config.NumberColumn(format="%.6f"),
            "SMA Close": st.column_config.NumberColumn(format="%.6f"),
            "Δкум. 24ч": st.column_config.NumberColumn(format="%.4g"),
            "ΔOI 24ч": st.column_config.NumberColumn(format="%.4g"),
            "Объём 24h USDT": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.download_button(
        "Скачать CSV",
        data=df_show.to_csv(index=False).encode("utf-8"),
        file_name="cripto_scanner.csv",
        mime="text/csv",
        use_container_width=True,
    )
    if df_full is not None and len(df_full) != len(df_show):
        with st.expander(f"Все результаты скана без фильтра Telegram ({len(df_full)})"):
            full_display = df_full.copy()
            full_display["bar_time"] = pd.to_datetime(full_display["bar_time"], utc=True).dt.strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            st.dataframe(full_display, use_container_width=True, hide_index=True)

    with st.expander("Легенда"):
        st.markdown("**Williams %R + EMA**")
        for _z, lbl in ZONE_LABEL_RU.items():
            st.markdown(f"- {lbl}")
        st.markdown("**Дивергенция цена ↔ кум. δ** (как на главном графике)")
        for _d, lbl in DIV_LABEL_RU.items():
            st.markdown(f"- {lbl}")
        st.markdown(
            f"**Кум. δ за 24 ч** — прирост на **{CUM_DELTA_24H_INTERVAL}** "
            "(δ = 2×taker buy − volume); фильтр по знаку прироста."
        )
        st.markdown(
            f"**Open Interest за 24 ч** — прирост OI на **{OI_24H_INTERVAL}** "
            "(USDT-M perpetual); фильтр по росту / падению."
        )


def _execute_scan(
    pool: list[str],
    tf_keys: list[str],
    *,
    willy_length: int,
    willy_ema_length: int,
    sma_length: int,
    use_closed: bool,
    max_workers: int,
    pivot_left: int,
    pivot_right: int,
    min_bars_between: int,
    div_max_age_bars: int,
    search_criteria: ScannerSearchCriteria,
) -> pd.DataFrame:
    progress = st.progress(0.0, text="Подготовка…")

    def _prog(p: float, msg: str) -> None:
        progress.progress(min(1.0, max(0.0, p)), text=msg[:120])

    with st.spinner("Сканирование Binance spot…"):
        df = run_williams_scan(
            pool,
            list(tf_keys),
            length=int(willy_length),
            ema_length=int(willy_ema_length),
            sma_length=int(sma_length),
            use_closed_bar=bool(use_closed),
            max_workers=int(max_workers),
            progress=_prog,
            criteria=search_criteria,
            pivot_left=pivot_left,
            pivot_right=pivot_right,
            min_bars_between=min_bars_between,
            div_max_age_bars=div_max_age_bars,
        )
    progress.empty()
    return df


def _default_tg_zones(zone_mode: str) -> list[WilliamsZone]:
    if zone_mode == "overbought":
        return ["overbought"]
    if zone_mode == "oversold":
        return ["oversold"]
    return ["overbought", "oversold"]


@dataclass(frozen=True)
class _TelegramPanelState:
    tg_ready: bool
    tg_enabled: bool
    tg_auto_scan: bool
    tg_notify_mode: str
    tg_max_rows: int
    tg_on_manual: bool
    tg_min_signals: int
    tg_send_if_empty: bool
    tg_profile: TelegramNotifyProfile
    tg_proxy: str
    tg_filter_zones: list[WilliamsZone]
    tg_filter_tf: list[str]
    tg_div_filter: str
    tg_sma_filter: str
    tg_sma_length: int
    tg_ticker_needle: str
    tg_param_zone: bool
    tg_param_tf: bool
    tg_param_div: bool
    tg_param_sma: bool
    tg_param_ticker: bool


def _render_telegram_panel(
    *,
    zone_mode: str,
    tf_keys: list[str],
    sma_length: int,
) -> _TelegramPanelState:
    """Настройки Telegram на основной странице (не в сайдбаре)."""
    tg_cfg = load_telegram_config()
    tg_ready = telegram_configured(tg_cfg)
    tg_param_zone = True
    tg_param_tf = True
    tg_param_div = False
    tg_param_sma = False
    tg_param_ticker = False
    tg_filter_zones: list[WilliamsZone] = _default_tg_zones(zone_mode)
    tg_filter_tf: list[str] = list(tf_keys)
    tg_div_filter = "any"
    tg_sma_filter = "any"
    tg_sma_length = int(sma_length)
    tg_ticker_needle = ""
    tg_proxy = ""
    tg_enabled = False
    tg_auto_scan = True
    tg_notify_mode = "on_change"
    tg_max_rows = 25
    tg_on_manual = False
    tg_min_signals = 1
    tg_send_if_empty = False
    tg_profile: TelegramNotifyProfile = "all_scan"

    with st.expander("Telegram · уведомления", expanded=False):
        if tg_ready:
            st.caption("Бот настроен · `secrets.toml`")
        else:
            st.warning(
                "Укажите `telegram.bot_token` и `telegram.chat_id` в "
                "`.streamlit/secrets.toml` (см. secrets.toml.example)."
            )
        tg_proxy = st.text_input(
            "Прокси для Telegram API (опционально)",
            value=str(tg_cfg.get("proxy") or ""),
            placeholder="http://127.0.0.1:7890 или socks5://127.0.0.1:1080",
            key="ti_tg_proxy",
            disabled=not tg_ready,
            help="Нужен, если `api.telegram.org` недоступен (таймаут). Можно также задать `telegram.proxy` в secrets.toml.",
        )
        tg_enabled = st.checkbox(
            "Включить Telegram-режим",
            value=False,
            key="cb_tg_enabled",
            disabled=not tg_ready,
            help="Автоскан по параметрам сканера и уведомления в бот при сигналах.",
        )
        tg_auto_scan = st.checkbox(
            "Автоскан каждые 30 мин",
            value=True,
            key="cb_tg_auto_scan",
            disabled=not tg_ready or not tg_enabled,
            help="Сканирует по настройкам сканера; при сигналах (по фильтрам ниже) — шлёт в Telegram.",
        )
        if tg_enabled and tg_auto_scan:
            st.caption(
                f"Интервал: **{format_timedelta_ru(TELEGRAM_AUTO_SCAN_INTERVAL)}** · "
                "уведомление только при **≥ мин. сигналов** (см. ниже)."
            )
        tg_notify_mode = st.radio(
            "Когда слать",
            options=["on_change", "always"],
            index=0,
            format_func=lambda x: {
                "on_change": "Только при изменении результатов",
                "always": "После каждого автоскана",
            }[x],
            key="rb_tg_notify_mode",
            disabled=not tg_ready,
        )
        tg_max_rows = int(
            st.slider(
                "Макс. строк в сообщении",
                min_value=5,
                max_value=50,
                value=25,
                key="sl_tg_max_rows",
                disabled=not tg_ready,
            )
        )
        tg_on_manual = st.checkbox(
            "Также при ручном скане",
            value=False,
            key="cb_tg_on_manual",
            disabled=not tg_ready,
        )
        tg_min_signals = int(
            st.number_input(
                "Мин. сигналов для отправки",
                min_value=0,
                max_value=100,
                value=1,
                step=1,
                key="ni_tg_min_signals",
                disabled=not tg_ready,
            )
        )
        tg_send_if_empty = st.checkbox(
            "Слать, даже если сигналов нет",
            value=False,
            key="cb_tg_send_empty",
            disabled=not tg_ready,
        )
        st.markdown("**По каким параметрам слать**")
        tg_profile = st.radio(
            "Режим отбора",
            options=["all_scan", "mirror_scan", "custom"],
            index=0,
            format_func=lambda x: {
                "all_scan": "Все результаты скана",
                "mirror_scan": "Как параметры скана",
                "custom": "Выбрать параметры поиска",
            }[x],
            key="rb_tg_profile",
            disabled=not tg_ready,
        )
        if tg_profile == "custom" and tg_ready:
            st.caption("Отметьте критерии — в Telegram попадут только подходящие строки.")
            tg_param_zone = st.checkbox(
                TG_PARAM_LABELS_RU[TG_PARAM_ZONE],
                value=True,
                key="cb_tg_param_zone",
            )
            if tg_param_zone:
                tg_filter_zones = st.multiselect(
                    "Зоны EMA",
                    options=["overbought", "oversold"],
                    default=_default_tg_zones(zone_mode),
                    format_func=lambda z: ZONE_LABEL_RU.get(z, z),
                    key="ms_tg_zones",
                )
            tg_param_tf = st.checkbox(
                TG_PARAM_LABELS_RU[TG_PARAM_TF],
                value=True,
                key="cb_tg_param_tf",
            )
            if tg_param_tf:
                tg_filter_tf = st.multiselect(
                    "Таймфреймы",
                    options=list(tf_keys),
                    default=list(tf_keys),
                    format_func=lambda k: SCANNER_TF_LABELS_RU.get(k, k),
                    key="ms_tg_tf",
                )
            tg_param_div = st.checkbox(
                TG_PARAM_LABELS_RU[TG_PARAM_DIV],
                value=False,
                key="cb_tg_param_div",
            )
            if tg_param_div:
                tg_div_filter = st.radio(
                    "Дивергенция δ",
                    options=["any", "with", "without", "bearish", "bullish"],
                    index=0,
                    format_func=lambda x: {
                        "any": "Любая",
                        "with": "Только с дивергенцией",
                        "without": "Только без дивергенции",
                        "bearish": "Только медвежья",
                        "bullish": "Только бычья",
                    }[x],
                    key="rb_tg_div_filter",
                )
            tg_param_sma = st.checkbox(
                TG_PARAM_LABELS_RU[TG_PARAM_SMA],
                value=True,
                key="cb_tg_param_sma",
            )
            if tg_param_sma:
                tg_sma_length = int(
                    st.slider(
                        "SMA · период (Telegram)",
                        min_value=2,
                        max_value=200,
                        value=int(sma_length),
                        key="sl_tg_sma_length",
                    )
                )
                tg_sma_filter = st.radio(
                    "Цена vs SMA",
                    options=["any", "above", "below"],
                    index=0,
                    format_func=lambda x: {
                        "any": "Не фильтровать",
                        "above": "Close выше SMA",
                        "below": "Close ниже SMA",
                    }[x],
                    key="rb_tg_sma_filter",
                )
            tg_param_ticker = st.checkbox(
                TG_PARAM_LABELS_RU[TG_PARAM_TICKER],
                value=False,
                key="cb_tg_param_ticker",
            )
            if tg_param_ticker:
                tg_ticker_needle = st.text_input(
                    "Тикер содержит",
                    value="",
                    placeholder="BTC, SOL…",
                    key="ti_tg_ticker",
                )
        if tg_ready and st.button("Проверить Telegram", key="btn_tg_test"):
            proxy_use = (tg_proxy or "").strip() or None
            ok_conn, conn_detail = check_telegram_connection(
                bot_token=tg_cfg["bot_token"],
                proxy_url=proxy_use,
            )
            if not ok_conn:
                st.error(f"Связь с API: {conn_detail}")
            else:
                st.success(f"Связь с API: {conn_detail}")
                test_msg = format_scanner_results_message(
                    None,
                    tf_keys=tf_keys,
                    pool_size=0,
                    max_rows=5,
                )
                test_msg = test_msg.replace(
                    "Сигналов по фильтру Telegram <b>нет</b>.",
                    "<b>Тест</b> · связь с ботом работает.",
                )
                ok, detail = send_telegram_message(
                    test_msg,
                    bot_token=tg_cfg["bot_token"],
                    chat_id=tg_cfg["chat_id"],
                    proxy_url=proxy_use,
                )
                if ok:
                    st.success(f"Сообщение: {detail}")
                else:
                    st.error(f"Сообщение: {detail}")

    return _TelegramPanelState(
        tg_ready=tg_ready,
        tg_enabled=bool(tg_enabled),
        tg_auto_scan=bool(tg_auto_scan),
        tg_notify_mode=str(tg_notify_mode),
        tg_max_rows=int(tg_max_rows),
        tg_on_manual=bool(tg_on_manual),
        tg_min_signals=int(tg_min_signals),
        tg_send_if_empty=bool(tg_send_if_empty),
        tg_profile=tg_profile,
        tg_proxy=str(tg_proxy or ""),
        tg_filter_zones=list(tg_filter_zones),
        tg_filter_tf=list(tg_filter_tf),
        tg_div_filter=str(tg_div_filter),
        tg_sma_filter=str(tg_sma_filter),
        tg_sma_length=int(tg_sma_length),
        tg_ticker_needle=str(tg_ticker_needle or ""),
        tg_param_zone=bool(tg_param_zone),
        tg_param_tf=bool(tg_param_tf),
        tg_param_div=bool(tg_param_div),
        tg_param_sma=bool(tg_param_sma),
        tg_param_ticker=bool(tg_param_ticker),
    )


def _notify_telegram_if_needed(
    df: pd.DataFrame,
    *,
    tg_enabled: bool,
    tg_notify_mode: str,
    tg_max_rows: int,
    tg_profile: TelegramNotifyProfile,
    tg_filters: TelegramNotifyFilters,
    tg_proxy: str,
    tf_keys: list[str],
    pool_size: int,
    source: str,
) -> None:
    """Отправка в Telegram после скана (`source`: auto | manual)."""
    if not tg_enabled:
        return
    ok, detail = maybe_notify_scanner_results(
        df,
        enabled=True,
        notify_mode=tg_notify_mode,
        tf_keys=tf_keys,
        pool_size=pool_size,
        max_rows=tg_max_rows,
        notify_filters=tg_filters,
        notify_profile=tg_profile,
        proxy_url=(tg_proxy or "").strip() or None,
    )
    if not ok and detail not in ("выкл", "без изменений") and not str(detail).startswith("пропуск"):
        st.session_state["williams_tg_last_status"] = f"ошибка · {detail}"


def _run_scanner_body(
    pool: list[str],
    tf_keys: list[str],
    *,
    willy_length: int,
    willy_ema_length: int,
    sma_length: int,
    use_closed: bool,
    max_workers: int,
    pivot_left: int,
    pivot_right: int,
    min_bars_between: int,
    div_max_age_bars: int,
    search_criteria: ScannerSearchCriteria,
    scan_btn: bool,
    tg_auto_scan: bool,
    tg_enabled: bool,
    tg_notify_mode: str,
    tg_max_rows: int,
    tg_on_manual: bool,
    tg_profile: TelegramNotifyProfile,
    tg_filters: TelegramNotifyFilters,
    tg_proxy: str,
    show_sma_column: bool,
) -> None:
    if scan_btn:
        df = _execute_scan(
            pool,
            tf_keys,
            willy_length=willy_length,
            willy_ema_length=willy_ema_length,
            sma_length=sma_length,
            use_closed=use_closed,
            max_workers=max_workers,
            pivot_left=pivot_left,
            pivot_right=pivot_right,
            min_bars_between=min_bars_between,
            div_max_age_bars=div_max_age_bars,
            search_criteria=search_criteria,
        )
        st.session_state["williams_scan_df"] = df
        if tg_on_manual:
            _notify_telegram_if_needed(
                df,
                tg_enabled=tg_enabled,
                tg_notify_mode=tg_notify_mode,
                tg_max_rows=tg_max_rows,
                tg_profile=tg_profile,
                tg_filters=tg_filters,
                tg_proxy=tg_proxy,
                tf_keys=tf_keys,
                pool_size=len(pool),
                source="manual",
            )

    tg_auto_active = bool(tg_enabled and tg_auto_scan)

    frag = getattr(st, "fragment", None)
    if tg_auto_active and frag is not None:

        @frag(run_every=TELEGRAM_AUTO_SCAN_INTERVAL)
        def _auto() -> None:
            df = _execute_scan(
                pool,
                tf_keys,
                willy_length=willy_length,
                willy_ema_length=willy_ema_length,
                sma_length=sma_length,
                use_closed=use_closed,
                max_workers=max_workers,
                pivot_left=pivot_left,
                pivot_right=pivot_right,
                min_bars_between=min_bars_between,
                div_max_age_bars=div_max_age_bars,
                search_criteria=search_criteria,
            )
            st.session_state["williams_scan_df"] = df
            _notify_telegram_if_needed(
                df,
                tg_enabled=tg_enabled,
                tg_notify_mode=tg_notify_mode,
                tg_max_rows=tg_max_rows,
                tg_profile=tg_profile,
                tg_filters=tg_filters,
                tg_proxy=tg_proxy,
                tf_keys=tf_keys,
                pool_size=len(pool),
                source="auto",
            )

        if not scan_btn:
            _auto()
    elif tg_auto_active and not scan_btn:
        st.caption("Для автоскана Telegram нужен Streamlit с `st.fragment`.")

    if "williams_scan_df" not in st.session_state:
        if tg_enabled and tg_auto_scan:
            st.info("Ожидание первого **автоскана Telegram** (каждые 30 мин)…")
        else:
            st.info("Нажмите **Сканировать** или включите **автоскан Telegram** в блоке ниже.")
        return

    df_all = st.session_state["williams_scan_df"]
    if tg_enabled and telegram_profile_filters_rows(tg_profile):
        df_show = apply_telegram_row_filters(df_all, tg_filters)
        df_show = sort_scanner_results_by_age(df_show)
        _show_results(
            df_show,
            filter_note=telegram_filters_summary(tg_filters, profile=tg_profile),
            df_full=df_all if len(df_show) != len(df_all) else None,
            show_sma_column=show_sma_column,
        )
    else:
        _show_results(sort_scanner_results_by_age(df_all), show_sma_column=show_sma_column)
    tg_status = st.session_state.get("williams_tg_last_status")
    if tg_enabled and tg_status:
        st.caption(f"Telegram: **{tg_status}**")


def main() -> None:
    st.set_page_config(page_title="Cripto Scanner", layout="wide")
    auth_user = render_auth_gate()
    require_page_access(PAGE_WILLIAMS, auth_user)
    apply_dark_shell()

    if auth_user:
        st.caption(f"Пользователь: **{auth_user}**")
    st.title("Cripto Scanner")
    st.markdown(
        "Поиск по **всем USDT spot** парам Binance. В сайдбаре отметьте **нужные критерии** "
        "(Williams, SMA, Δкум. 24ч, ΔOI 24ч, дивергенция) — в результат попадут строки, где выполнены **все** отмеченные."
    )

    with st.sidebar:
        render_auth_sidebar(auth_user)
        st.markdown("**Параметры сканера**")
        st.caption("Отметьте критерии поиска — в результат попадут строки, где выполнены **все** отмеченные.")

        tf_keys = st.multiselect(
            "Таймфреймы",
            options=list(SCANNER_TF_API.keys()),
            default=["15m", "1h", "4h"],
            format_func=lambda k: SCANNER_TF_LABELS_RU.get(k, k),
            help="Для Williams, SMA и дивергенции. При поиске только по Δкум./ΔOI 24ч используется первый выбранный ТФ.",
        )

        use_williams = st.checkbox(
            "Williams %R + EMA",
            value=True,
            key="cb_use_williams",
        )
        zone_mode = "both"
        willy_length = 21
        willy_ema_length = 13
        if use_williams:
            zone_mode = st.radio(
                "Зона EMA",
                options=["both", "overbought", "oversold"],
                format_func=lambda x: {
                    "both": "Обе (−20 и −80)",
                    "overbought": "Только перекуп (EMA > −20)",
                    "oversold": "Только перепрод (EMA < −80)",
                }[x],
                index=0,
                key="rb_zone_mode",
            )
            willy_length = st.slider("%R · период (length)", 5, 80, 21, key="sl_willy_length")
            willy_ema_length = st.slider("EMA · длина (over %R)", 2, 60, 13, key="sl_willy_ema_length")

        use_sma = st.checkbox("SMA (close)", value=False, key="cb_use_sma")
        sma_length = 20
        sma_filter_mode: str = "below"
        if use_sma:
            sma_length = st.slider(
                "SMA · период",
                min_value=2,
                max_value=200,
                value=20,
                key="sl_sma_length",
            )
            sma_filter_mode = st.radio(
                "Цена vs SMA",
                options=["above", "below"],
                index=1,
                format_func=lambda x: {
                    "above": "Close выше SMA",
                    "below": "Close ниже SMA",
                }[x],
                key="rb_sma_filter_mode",
            )
        show_sma_in_table = st.checkbox(
            "Колонка SMA в таблице",
            value=True,
            key="cb_show_sma_col",
        )

        use_cum_delta_24h = st.checkbox(
            f"Кум. δ за 24 ч ({CUM_DELTA_24H_INTERVAL})",
            value=False,
            key="cb_use_cum_delta_24h",
        )
        cum_delta_24h_filter: CumDelta24hFilterMode = "up"
        if use_cum_delta_24h:
            cum_delta_24h_filter = st.radio(
                "Направление Δкум. 24ч",
                options=["up", "down"],
                index=0,
                format_func=lambda x: CUM_DELTA_24H_LABEL_RU.get(x, x),  # type: ignore[arg-type]
                key="rb_cum_delta_24h_filter",
                help="δ = 2×taker buy − volume; один запрос 15m на пару.",
            )

        use_oi_24h = st.checkbox(
            f"Open Interest за 24 ч ({OI_24H_INTERVAL})",
            value=False,
            key="cb_use_oi_24h",
        )
        oi_24h_filter: Oi24hFilterMode = "up"
        if use_oi_24h:
            oi_24h_filter = st.radio(
                "Направление ΔOI 24ч",
                options=["up", "down"],
                index=0,
                format_func=lambda x: OI_24H_LABEL_RU.get(x, x),  # type: ignore[arg-type]
                key="rb_oi_24h_filter",
                help="USDT-M perpetual; один запрос 15m на пару.",
            )

        use_divergence = st.checkbox(
            "Дивергенция цена ↔ кум. δ",
            value=False,
            key="cb_use_divergence",
        )
        compute_divergence = st.checkbox(
            "Считать дивергенцию для таблицы",
            value=True,
            key="cb_compute_divergence",
            disabled=use_divergence,
            help="Если критерий δ в поиске выключен — всё равно можно считать δ для колонки.",
        )
        div_mode = "both"
        div_max_age_bars = 30
        pivot_left = 5
        pivot_right = 5
        min_bars_between = 10
        if use_divergence or compute_divergence:
            if use_divergence:
                div_mode = st.radio(
                    "Тип дивергенции в поиске",
                    options=["both", "bearish", "bullish"],
                    format_func=lambda x: {
                        "both": "Любая",
                        "bearish": "Медвежья",
                        "bullish": "Бычья",
                    }[x],
                    index=0,
                    key="rb_div_mode_search",
                )
            div_max_age_bars = st.slider(
                "Див. подтверждена не позже (баров назад)",
                3,
                80,
                30,
                key="sl_div_max_age",
            )
            with st.expander("Pivot дивергенции", expanded=False):
                pivot_left = st.slider("Pivot слева (L)", 2, 12, 5, key="sl_pivot_left")
                pivot_right = st.slider("Pivot справа (R)", 2, 12, 5, key="sl_pivot_right")
                min_bars_between = st.slider(
                    "Мин. расстояние между свингами",
                    5,
                    80,
                    10,
                    key="sl_min_bars_between",
                )

        use_closed = st.checkbox("Последняя **закрытая** свеча", value=True)
        max_workers = st.slider("Потоков загрузки", 4, 20, 12)

        st.markdown("---")
        all_syms, is_fallback = _load_spot_symbol_universe()
        vol_map = _cached_spot_volumes()
        ranked = _rank_symbols_by_volume(all_syms, vol_map)

        n_syms = max(1, len(all_syms))
        st.metric("Пар USDT spot в базе", n_syms)
        if is_fallback:
            st.error(
                "Загружен **запасной** список (~7 пар): Binance `exchangeInfo` недоступен или в кэше старая ошибка. "
                "Нажмите **«Обновить список пар»** — для лимита 400 нужно **400+** пар в базе."
            )
        if st.button("Обновить список пар", use_container_width=True):
            cached_spot_usdt_symbol_list.clear()
            st.session_state.pop("spot_sym_auto_retry", None)
            st.rerun()

        universe = st.radio(
            "Сканировать",
            options=["all", "top"],
            format_func=lambda x: "Все USDT spot" if x == "all" else "Топ по объёму 24h",
        )
        cap_ceil = min(SCAN_PAIR_CAP_MAX, n_syms)
        top_max = cap_ceil
        top_default = min(200, cap_ceil)
        top_step = 50 if cap_ceil >= 50 else 1

        top_n = cap_ceil
        if universe == "top":
            top_n = int(
                st.number_input(
                    "Топ N пар",
                    min_value=1,
                    max_value=top_max,
                    value=top_default,
                    step=top_step,
                    help=f"Максимум сейчас: {top_max} (сколько пар в базе).",
                )
            )
        pair_max = cap_ceil
        pair_default = pair_max
        pair_step = 50 if pair_max >= 50 else 1
        pair_cap = int(
            st.number_input(
                "Лимит пар",
                min_value=1,
                max_value=pair_max,
                value=pair_default,
                step=pair_step,
                help=(
                    f"По умолчанию — все **{pair_max}** пар из базы. "
                    "Можно уменьшить, чтобы сканировать меньший набор."
                ),
            )
        )
        needle = st.text_input("Фильтр тикера", value="", placeholder="BTC, SOL…")

    tg = _render_telegram_panel(zone_mode=zone_mode, tf_keys=list(tf_keys), sma_length=int(sma_length))
    tg_ready = tg.tg_ready
    tg_enabled = tg.tg_enabled
    tg_auto_scan = tg.tg_auto_scan
    tg_notify_mode = tg.tg_notify_mode
    tg_max_rows = tg.tg_max_rows
    tg_on_manual = tg.tg_on_manual
    tg_min_signals = tg.tg_min_signals
    tg_send_if_empty = tg.tg_send_if_empty
    tg_profile = tg.tg_profile
    tg_proxy = tg.tg_proxy
    tg_param_zone = tg.tg_param_zone
    tg_param_tf = tg.tg_param_tf
    tg_param_div = tg.tg_param_div
    tg_param_sma = tg.tg_param_sma
    tg_param_ticker = tg.tg_param_ticker
    tg_filter_zones = tg.tg_filter_zones
    tg_filter_tf = tg.tg_filter_tf
    tg_div_filter = tg.tg_div_filter
    tg_sma_filter = tg.tg_sma_filter
    tg_sma_length = tg.tg_sma_length
    tg_ticker_needle = tg.tg_ticker_needle

    pool = ranked[:top_n] if universe == "top" else ranked[:pair_cap]
    pool = _filter_symbols(pool, needle)
    if not pool:
        st.warning("Нет пар после фильтра.")
        return
    if not tf_keys:
        st.warning("Выберите хотя бы один таймфрейм.")
        return

    if not (use_williams or use_sma or use_cum_delta_24h or use_oi_24h or use_divergence):
        st.warning("Отметьте **хотя бы один** критерий поиска в сайдбаре.")
        return

    if zone_mode == "overbought":
        williams_zones: frozenset[WilliamsZone] = frozenset({"overbought"})
    elif zone_mode == "oversold":
        williams_zones = frozenset({"oversold"})
    else:
        williams_zones = frozenset({"overbought", "oversold"})

    if div_mode == "bearish":
        div_kinds: frozenset[DivKind] = frozenset({"bearish"})
    elif div_mode == "bullish":
        div_kinds = frozenset({"bullish"})
    else:
        div_kinds = frozenset({"bearish", "bullish"})

    search_criteria = ScannerSearchCriteria(
        use_williams=bool(use_williams),
        williams_zones=williams_zones,
        use_sma=bool(use_sma),
        sma_filter_mode=sma_filter_mode if use_sma else "none",  # type: ignore[arg-type]
        use_cum_delta_24h=bool(use_cum_delta_24h),
        cum_delta_24h_filter=cum_delta_24h_filter if use_cum_delta_24h else "none",
        use_oi_24h=bool(use_oi_24h),
        oi_24h_filter=oi_24h_filter if use_oi_24h else "none",
        use_divergence=bool(use_divergence),
        div_kinds=div_kinds,
        compute_divergence=bool(use_divergence or compute_divergence),
    )

    tg_auto_scan_on = bool(tg_enabled and tg_ready and tg_auto_scan)
    refresh_note = ""
    if tg_auto_scan_on:
        refresh_note = (
            f" · Telegram-автоскан: **{format_timedelta_ru(TELEGRAM_AUTO_SCAN_INTERVAL)}**"
        )

    if tg_profile == "mirror_scan":
        tg_filters = build_mirror_scan_telegram_filters(
            zone_mode=zone_mode if use_williams else "both",
            tf_keys=list(tf_keys),
            div_mode=div_mode,
            require_divergence=use_divergence,
            check_divergence=search_criteria.compute_divergence,
            sma_filter_mode=str(sma_filter_mode) if use_sma else "none",
            sma_length=int(sma_length),
            ticker_needle=needle,
            min_signals=int(tg_min_signals),
            send_if_empty=bool(tg_send_if_empty),
        )
    elif tg_profile == "custom":
        active: set[str] = set()
        if tg_param_zone:
            active.add(TG_PARAM_ZONE)
        if tg_param_tf:
            active.add(TG_PARAM_TF)
        if tg_param_div:
            active.add(TG_PARAM_DIV)
        if tg_param_sma and tg_sma_filter != "any":
            active.add(TG_PARAM_SMA)
        if tg_param_ticker:
            active.add(TG_PARAM_TICKER)
        tg_filters = TelegramNotifyFilters(
            zones=frozenset(tg_filter_zones) if tg_param_zone and tg_filter_zones else None,
            timeframes=frozenset(tg_filter_tf) if tg_param_tf and tg_filter_tf else None,
            div_filter=tg_div_filter,  # type: ignore[arg-type]
            sma_filter=tg_sma_filter,  # type: ignore[arg-type]
            sma_length=int(tg_sma_length) if tg_param_sma and tg_sma_filter != "any" else None,
            min_signals=int(tg_min_signals),
            send_if_empty=bool(tg_send_if_empty),
            ticker_needle=str(tg_ticker_needle or "") if tg_param_ticker else "",
            active_params=frozenset(active),
        )
    else:
        tg_filters = TelegramNotifyFilters(
            min_signals=int(tg_min_signals),
            send_if_empty=bool(tg_send_if_empty),
        )

    tg_profile_note = ""
    if tg_enabled and tg_ready:
        tg_profile_note = f" · TG: {telegram_filters_summary(tg_filters, profile=tg_profile)}"

    tg_align_scan = bool(tg_enabled and tg_ready and tg_profile == "custom" and tg_auto_scan)
    scan_criteria = search_criteria
    scan_tf_keys = list(tf_keys)
    scan_sma_len = int(sma_length)
    if tg_align_scan:
        tg_active = tg_filters.active_params
        aligned_zones, scan_tf_keys, aligned_sma, aligned_div, _aligned_req, aligned_div_kinds, scan_sma_len = (
            resolve_scan_params_from_telegram(
                tg_enabled=bool(tg_enabled and tg_ready),
                tg_profile=tg_profile if tg_enabled else "all_scan",
                tg_filters=tg_filters,
                scanner_zones=williams_zones,
                scanner_tf_keys=list(tf_keys),
                scanner_sma_filter_mode=str(sma_filter_mode) if use_sma else "none",
                scanner_check_divergence=search_criteria.compute_divergence,
                scanner_require_divergence=use_divergence,
                scanner_div_kinds=div_kinds,
                scanner_sma_length=int(sma_length),
                align_scan=tg_align_scan,
            )
        )
        scan_criteria = ScannerSearchCriteria(
            use_williams=True if TG_PARAM_ZONE in tg_active else search_criteria.use_williams,
            williams_zones=aligned_zones if TG_PARAM_ZONE in tg_active else search_criteria.williams_zones,  # type: ignore[arg-type]
            use_sma=True if TG_PARAM_SMA in tg_active else search_criteria.use_sma,
            sma_filter_mode=(
                aligned_sma  # type: ignore[arg-type]
                if TG_PARAM_SMA in tg_active and aligned_sma in ("above", "below")
                else search_criteria.sma_filter_mode
            ),
            use_cum_delta_24h=search_criteria.use_cum_delta_24h,
            cum_delta_24h_filter=search_criteria.cum_delta_24h_filter,
            use_oi_24h=search_criteria.use_oi_24h,
            oi_24h_filter=search_criteria.oi_24h_filter,
            use_divergence=True if TG_PARAM_DIV in tg_active else search_criteria.use_divergence,
            div_kinds=aligned_div_kinds if TG_PARAM_DIV in tg_active else search_criteria.div_kinds,  # type: ignore[arg-type]
            compute_divergence=bool(search_criteria.compute_divergence or aligned_div),
        )
        tg_profile_note += " · **скан = фильтр TG**"
    show_sma_column = bool(
        show_sma_in_table
        or use_sma
        or (
            tg_enabled
            and tg_ready
            and TG_PARAM_SMA in tg_filters.active_params
            and tg_filters.sma_filter != "any"
        )
    )

    cd24_req_note = (
        f" · +**{len(pool)}** × {CUM_DELTA_24H_INTERVAL} "
        f"(Δкум./ΔOI 24ч в таблице)"
    )

    st.caption(
        f"Пар: **{len(pool)}** · ТФ: **{len(scan_tf_keys)}** · klines: **~{len(pool) * len(scan_tf_keys)}**"
        + cd24_req_note
        + f" · поиск: **{search_criteria_summary_ru(search_criteria)}**"
        + refresh_note
        + tg_profile_note
    )
    scan_btn = st.button("Сканировать", type="primary")

    _run_scanner_body(
        pool,
        scan_tf_keys,
        willy_length=willy_length,
        willy_ema_length=willy_ema_length,
        sma_length=scan_sma_len,
        use_closed=use_closed,
        max_workers=max_workers,
        pivot_left=pivot_left,
        pivot_right=pivot_right,
        min_bars_between=min_bars_between,
        div_max_age_bars=div_max_age_bars,
        search_criteria=scan_criteria,
        scan_btn=scan_btn,
        tg_auto_scan=tg_auto_scan_on,
        tg_enabled=tg_enabled if tg_ready else False,
        tg_notify_mode=tg_notify_mode,
        tg_max_rows=tg_max_rows,
        tg_on_manual=tg_on_manual,
        tg_profile=tg_profile if tg_enabled else "all_scan",
        tg_filters=tg_filters,
        tg_proxy=tg_proxy if tg_ready else "",
        show_sma_column=show_sma_column,
    )


main()
