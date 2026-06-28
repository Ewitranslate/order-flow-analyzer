"""Панель администратора: пользователи, бан, доступ к страницам."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import streamlit as st

from auth import (
    PAGE_ADMIN,
    PAGE_CATALOG,
    USER_ASSIGNABLE_PAGES,
    get_logged_in_user,
    is_admin,
    list_users_for_admin,
    render_auth_gate,
    render_auth_sidebar,
    require_page_access,
    set_user_active,
    set_user_pages,
)
from app import apply_dark_shell


def _page_label(page_id: str) -> str:
    return PAGE_CATALOG.get(page_id, page_id)


def _render_user_card(row: dict, *, current_user: str) -> None:
    username = str(row["username"])
    active = bool(row["active"])
    role = str(row.get("role", "user"))
    pages: list[str] = list(row.get("pages") or [])
    email = row.get("email") or "—"
    created = row.get("created_at") or "—"

    with st.container(border=True):
        head_l, head_r = st.columns([2.2, 1])
        with head_l:
            status = "активен" if active else "заблокирован"
            st.markdown(f"**{username}** · {status}")
            st.caption(f"Роль: **{role}** · email: {email} · создан: {created}")
            if role != "admin":
                labels = ", ".join(_page_label(p) for p in pages) or "—"
                st.caption(f"Страницы: {labels}")
            else:
                st.caption("Страницы: все (администратор)")

        with head_r:
            if username == current_user:
                st.info("Это вы")
            elif role == "admin":
                st.caption("Администратор")
            else:
                ban_key = f"admin_ban_{username}"
                if active:
                    if st.button("Заблокировать", key=ban_key, use_container_width=True):
                        try:
                            set_user_active(username, False)
                            st.success(f"«{username}» заблокирован.")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                else:
                    if st.button("Разблокировать", key=ban_key, use_container_width=True):
                        try:
                            set_user_active(username, True)
                            st.success(f"«{username}» разблокирован.")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

        if role != "admin" and username != current_user:
            selected = st.multiselect(
                "Доступ к страницам",
                options=list(USER_ASSIGNABLE_PAGES),
                default=[p for p in pages if p in USER_ASSIGNABLE_PAGES],
                format_func=_page_label,
                key=f"admin_pages_{username}",
            )
            if st.button("Сохранить доступ", key=f"admin_save_pages_{username}", use_container_width=True):
                try:
                    set_user_pages(username, selected)
                    st.success(f"Доступ для «{username}» обновлён.")
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))


def main() -> None:
    st.set_page_config(page_title="Администрирование", layout="wide")
    auth_user = render_auth_gate()
    require_page_access(PAGE_ADMIN, auth_user)
    apply_dark_shell()

    if not is_admin(auth_user):
        st.error("Только администратор может открыть эту страницу.")
        st.stop()

    current = get_logged_in_user() or auth_user or ""
    st.title("Администрирование")
    st.caption("Управление пользователями: блокировка и доступ к разделам сайта.")

    users = list_users_for_admin()
    active_n = sum(1 for u in users if u["active"])
    st.metric("Пользователей", len(users), delta=f"{active_n} активных")

    st.markdown("### Зарегистрированные пользователи")
    if not users:
        st.info("Пока нет пользователей.")
        return

    for row in users:
        _render_user_card(row, current_user=current)

    with st.sidebar:
        render_auth_sidebar(auth_user)


if __name__ == "__main__":
    main()
