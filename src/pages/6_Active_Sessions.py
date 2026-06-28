"""Активные сессии пользователя."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st

from auth import PAGE_SESSIONS, get_logged_in_user, render_auth_gate, render_auth_sidebar, require_page_access
from auth_sessions import list_user_sessions, revoke_other_sessions, session_inactivity_days
from app import apply_dark_shell

_STATUS_LABELS = {
    "active": "Активна",
    "revoked": "Завершена",
    "expired": "Истекла",
}


def _fmt_time(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        import pandas as pd

        return pd.to_datetime(raw, utc=True).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(raw)


def _location_label(sess: dict) -> str:
    parts = [x for x in (sess.get("country"), sess.get("city")) if x]
    return ", ".join(parts) if parts else "—"


def main() -> None:
    st.set_page_config(page_title="Активные сессии", layout="wide")
    auth_user = render_auth_gate()
    require_page_access(PAGE_SESSIONS, auth_user)
    apply_dark_shell()

    user = get_logged_in_user() or auth_user
    if not user:
        st.error("Требуется вход в аккаунт.")
        st.stop()

    current_sid = str(st.session_state.get("auth_session_id") or "")

    st.title("Активные сессии")
    st.caption(
        f"Пользователь **{user}** · одновременно допускается **одна** активная сессия. "
        f"Неактивные сессии автоматически завершаются через **{session_inactivity_days()}** дней."
    )

    with st.sidebar:
        render_auth_sidebar(auth_user)

    sessions = list_user_sessions(user, limit=50)
    active_count = sum(1 for s in sessions if s.get("status") == "active")

    if active_count > 1:
        st.warning("Обнаружено несколько активных сессий — это нештатная ситуация. Завершите лишние вручную.")

    if st.button(
        "Завершить все остальные сессии",
        type="primary",
        disabled=not current_sid or active_count <= 1,
        help="Текущая сессия в этом браузере останется активной.",
    ):
        n = revoke_other_sessions(user, current_sid)
        st.success(f"Завершено сессий: **{n}**.")
        st.rerun()

    if not sessions:
        st.info("Сессий пока нет.")
        return

    for sess in sessions:
        sid = str(sess.get("session_id", ""))
        is_current = sid == current_sid and sess.get("status") == "active"
        status = str(sess.get("status") or "revoked")
        title = str(sess.get("device_name") or "Сессия")
        if is_current:
            title += " · **Current**"

        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.caption(f"Статус: **{_STATUS_LABELS.get(status, status)}** · ID: `{sid[:8]}…`")
            cols = st.columns(2)
            with cols[0]:
                st.markdown(f"**Браузер:** {sess.get('browser') or '—'}")
                st.markdown(f"**ОС:** {sess.get('os_name') or '—'}")
                st.markdown(f"**IP:** {sess.get('ip_address') or '—'}")
            with cols[1]:
                st.markdown(f"**Местоположение:** {_location_label(sess)}")
                st.markdown(f"**Вход:** {_fmt_time(sess.get('created_at'))}")
                st.markdown(f"**Последняя активность:** {_fmt_time(sess.get('last_active_at'))}")


if __name__ == "__main__":
    main()
