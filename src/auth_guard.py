"""Проверка авторизации (middleware для Streamlit-страниц)."""

from __future__ import annotations

from typing import Any

import streamlit as st

from auth_sessions import (
    SESSION_EXPIRED_MESSAGE,
    SESSION_REVOKED_MESSAGE,
    SESSION_SUPERSEDED_MESSAGE,
    validate_access_token,
)


def _clear_auth_state(*, flash: str | None = None) -> None:
    for key in (
        "auth_access_token",
        "auth_refresh_token",
        "auth_session_id",
        "auth_user",
        "auth_token",
        "auth_device_id",
    ):
        st.session_state.pop(key, None)
    if flash:
        st.session_state["auth_session_flash"] = flash


def _store_session_bundle(bundle: dict[str, str]) -> None:
    st.session_state["auth_access_token"] = bundle["access_token"]
    st.session_state["auth_refresh_token"] = bundle["refresh_token"]
    st.session_state["auth_session_id"] = bundle["session_id"]
    st.session_state["auth_user"] = bundle["username"]
    st.session_state.pop("auth_token", None)
    st.session_state.pop("auth_device_id", None)


def require_valid_session() -> str | None:
    """
    Проверяет access/refresh токены и активную сессию в БД.
    Возвращает username или None.
    """
    flash = st.session_state.pop("auth_session_flash", None)
    if flash:
        st.warning(flash)

    access = st.session_state.get("auth_access_token")
    refresh = st.session_state.get("auth_refresh_token")
    session_id = st.session_state.get("auth_session_id")

    if isinstance(access, str) and access:
        payload, err = validate_access_token(access)
        if payload:
            return str(payload["username"])
        if err in (SESSION_SUPERSEDED_MESSAGE, SESSION_REVOKED_MESSAGE, SESSION_EXPIRED_MESSAGE):
            _clear_auth_state(flash=err)

    if isinstance(refresh, str) and refresh and isinstance(session_id, str) and session_id:
        from auth_sessions import get_session, refresh_user_session

        renewed = refresh_user_session(refresh, session_id)
        if renewed:
            _store_session_bundle(renewed)
            return renewed["username"]

        session = get_session(str(session_id))
        if session and str(session.get("status") or "") != "active":
            from auth_sessions import session_error_message

            _clear_auth_state(flash=session_error_message(session))
        else:
            _clear_auth_state(flash=SESSION_REVOKED_MESSAGE)

    if st.session_state.get("auth_token"):
        _clear_auth_state(flash="Сессия устарела. Войдите снова.")

    return None


def store_login_bundle(bundle: dict[str, str]) -> None:
    _store_session_bundle(bundle)
