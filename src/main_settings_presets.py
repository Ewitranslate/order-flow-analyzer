"""Сохранённые настройки сайдбара главной страницы (по пользователю)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import streamlit as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PRESETS_PATH = _PROJECT_ROOT / "data" / "main_presets.json"
_PRESET_NAME_RE = re.compile(r"^[a-zA-Zа-яА-ЯёЁ0-9 _.\-]{1,64}$", re.UNICODE)
_FLASH_KEY = "main_preset_flash"

# Ключи session_state, которые входят в пресет
PRESET_STATE_KEYS: tuple[str, ...] = (
    "main_tf_key",
    "cb_restrict_to_oi",
    "filt_ohlc",
    "sb_chart_pair",
    "cb_cum_same_chart",
    "filt_cum",
    "sb_cum_pair",
    "panel_order",
    "cb_show_volume",
    "cb_show_vwap",
    "cb_show_price_ma",
    "sl_price_ma_length",
    "cb_show_oi",
    "cb_show_willy",
    "sl_willy_length",
    "sl_willy_ema_length",
    "cb_div_enabled",
    "cb_div_show_lines",
    "sl_div_pivot_left",
    "sl_div_pivot_right",
    "sl_div_min_bars",
)


def presets_file_path() -> Path:
    return _PRESETS_PATH


def _read_db() -> dict[str, dict[str, dict[str, Any]]]:
    path = presets_file_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw  # type: ignore[return-value]


def _write_db(db: dict[str, dict[str, dict[str, Any]]]) -> None:
    path = presets_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def list_preset_names(username: str) -> list[str]:
    user = username.strip().lower()
    presets = _read_db().get(user, {})
    return sorted(presets.keys(), key=lambda s: s.casefold())


def capture_current_settings() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in PRESET_STATE_KEYS:
        if key in st.session_state:
            val = st.session_state[key]
            if key == "panel_order" and isinstance(val, list):
                out[key] = list(val)
            else:
                out[key] = val
    return out


def apply_settings(data: dict[str, Any]) -> None:
    for key in PRESET_STATE_KEYS:
        if key not in data:
            continue
        val = data[key]
        if key == "panel_order":
            if isinstance(val, list):
                st.session_state[key] = [str(x) for x in val]
            continue
        st.session_state[key] = val


def save_preset(username: str, name: str, data: dict[str, Any]) -> str | None:
    """Сохранить пресет. None — успех, иначе текст ошибки."""
    user = username.strip().lower()
    title = (name or "").strip()
    if not title:
        return "Укажите название настроек."
    if not _PRESET_NAME_RE.match(title):
        return "Название: 1–64 символа (буквы, цифры, пробел, ._-)."
    db = _read_db()
    user_presets = db.setdefault(user, {})
    user_presets[title] = data
    try:
        _write_db(db)
    except OSError as e:
        return f"Не удалось сохранить: {e}"
    return None


def delete_preset(username: str, name: str) -> bool:
    user = username.strip().lower()
    title = (name or "").strip()
    if not title:
        return False
    db = _read_db()
    user_presets = db.get(user, {})
    if title not in user_presets:
        return False
    del user_presets[title]
    if user_presets:
        db[user] = user_presets
    else:
        db.pop(user, None)
    try:
        _write_db(db)
    except OSError:
        return False
    return True


def _set_flash(kind: str, message: str) -> None:
    st.session_state[_FLASH_KEY] = {"kind": kind, "message": message}


def _render_flash() -> None:
    raw = st.session_state.pop(_FLASH_KEY, None)
    if not isinstance(raw, dict):
        return
    msg = str(raw.get("message", ""))
    if not msg:
        return
    kind = str(raw.get("kind", "info"))
    if kind == "success":
        st.success(msg)
    elif kind == "error":
        st.error(msg)
    else:
        st.info(msg)


def render_main_presets_sidebar(username: str | None) -> None:
    if not username:
        st.caption("Войдите в аккаунт, чтобы сохранять настройки.")
        return

    with st.expander("Сохранённые настройки", expanded=False):
        _render_flash()
        names = list_preset_names(username)
        st.caption("Сохраните текущие параметры сайдбара под именем и загрузите позже.")

        preset_name = st.text_input(
            "Название",
            value="",
            key="ti_main_preset_name",
            placeholder="Например: BTC 1h · OI + Williams",
            help="До 64 символов. Повторное сохранение с тем же именем перезапишет пресет.",
        )

        save_col, del_col = st.columns(2)
        with save_col:
            if st.button("Сохранить", key="btn_main_preset_save", use_container_width=True, type="primary"):
                err = save_preset(username, preset_name, capture_current_settings())
                if err:
                    _set_flash("error", err)
                else:
                    _set_flash("success", f"Сохранено: **{preset_name.strip()}**")
                st.rerun()
        with del_col:
            delete_disabled = not names
            if st.button(
                "Удалить выбранный",
                key="btn_main_preset_delete",
                use_container_width=True,
                disabled=delete_disabled,
            ):
                picked = st.session_state.get("sb_main_preset_pick")
                if isinstance(picked, str) and picked and delete_preset(username, picked):
                    _set_flash("success", f"Удалено: **{picked}**")
                else:
                    _set_flash("error", "Не удалось удалить пресет.")
                st.rerun()

        if names:
            picked = st.selectbox(
                "Загрузить",
                options=names,
                key="sb_main_preset_pick",
                help="Выберите сохранённый набор и нажмите «Применить».",
            )
            if st.button("Применить", key="btn_main_preset_load", use_container_width=True):
                db = _read_db()
                data = db.get(username.strip().lower(), {}).get(picked)
                if not isinstance(data, dict):
                    _set_flash("error", "Пресет не найден.")
                else:
                    apply_settings(data)
                    _set_flash("success", f"Применено: **{picked}**")
                st.rerun()
        else:
            st.info("Пока нет сохранённых наборов.")
