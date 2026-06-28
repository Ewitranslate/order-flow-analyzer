"""Аккаунт: журнал входов."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pandas as pd
import streamlit as st

from auth import PAGE_ACCOUNT, get_logged_in_user, render_auth_gate, render_auth_sidebar, require_page_access
from auth_audit import list_login_events
from app import apply_dark_shell


def _fmt_time(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        return pd.to_datetime(raw, utc=True).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(raw)


def main() -> None:
    st.set_page_config(page_title="Аккаунт", layout="wide")
    auth_user = render_auth_gate()
    require_page_access(PAGE_ACCOUNT, auth_user)
    apply_dark_shell()

    user = get_logged_in_user() or auth_user
    if not user:
        st.error("Требуется вход в аккаунт.")
        st.stop()

    st.title("Аккаунт")
    st.caption(f"Пользователь **{user}** · журнал входов. Управление сессиями — на странице **Активные сессии**.")

    with st.sidebar:
        render_auth_sidebar(auth_user)

    st.markdown("### Журнал входов")
    events = list_login_events(user, limit=100)
    if not events:
        st.info("Записей пока нет.")
    else:
        rows = []
        for ev in events:
            loc = "—"
            if ev.get("country") or ev.get("city"):
                loc = ", ".join(x for x in (ev.get("country"), ev.get("city")) if x)
            status = "Успешно" if ev.get("status") == "success" else "Неудача"
            rows.append(
                {
                    "Время": _fmt_time(ev.get("occurred_at")),
                    "Статус": status,
                    "Устройство": ev.get("device_name") or "—",
                    "Браузер": ev.get("browser") or "—",
                    "ОС": ev.get("os_name") or "—",
                    "Местоположение": loc,
                    "Причина": ev.get("reason") or "—",
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
