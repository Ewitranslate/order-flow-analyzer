"""Проверка авторизации (middleware для Streamlit-страниц)."""

from __future__ import annotations

import streamlit as st

from auth_sessions import (
    SESSION_REVOKED_MESSAGE,
    validate_access_token,
)

_AUTH_CACHE_KEY = "_auth_session_cache"


def _auth_token_signature() -> tuple[str | None, str | None, str | None]:
    return (
        st.session_state.get("auth_session_id") if isinstance(st.session_state.get("auth_session_id"), str) else None,
        st.session_state.get("auth_access_token") if isinstance(st.session_state.get("auth_access_token"), str) else None,
        st.session_state.get("auth_refresh_token") if isinstance(st.session_state.get("auth_refresh_token"), str) else None,
    )


def _cached_valid_user() -> str | None:
    sig = _auth_token_signature()
    cache = st.session_state.get(_AUTH_CACHE_KEY)
    if isinstance(cache, dict) and cache.get("sig") == sig:
        user = cache.get("user")
        if isinstance(user, str) and user:
            return user
    return None


def _set_cached_user(username: str) -> None:
    st.session_state[_AUTH_CACHE_KEY] = {"sig": _auth_token_signature(), "user": username}


def _clear_auth_cache() -> None:
    st.session_state.pop(_AUTH_CACHE_KEY, None)


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
    _clear_auth_cache()
    if flash:
        st.session_state["auth_session_flash"] = flash


def _store_session_bundle(bundle: dict[str, str]) -> None:
    st.session_state["auth_access_token"] = bundle["access_token"]
    st.session_state["auth_refresh_token"] = bundle["refresh_token"]
    st.session_state["auth_session_id"] = bundle["session_id"]
    st.session_state["auth_user"] = bundle["username"]
    st.session_state.pop("auth_token", None)
    st.session_state.pop("auth_device_id", None)
    _set_cached_user(bundle["username"])


def trust_active_db_session(session_id: str) -> str | None:
    """Сессия в БД active — не выкидывать при сбое refresh (гонка rerun / SQLite)."""
    from auth_sessions import get_session

    try:
        session = get_session(session_id)
    except Exception:
        session = None
    if not session or str(session.get("status") or "") != "active":
        return None

    username = str(session.get("username") or "")
    cached_user = st.session_state.get("auth_user")
    if isinstance(cached_user, str) and cached_user and cached_user == username:
        _set_cached_user(cached_user)
        return cached_user

    new_access = st.session_state.get("auth_access_token")
    if isinstance(new_access, str) and new_access:
        payload, fatal = validate_access_token(new_access)
        if payload and not fatal:
            user = str(payload["username"])
            _set_cached_user(user)
            return user

    return None


def require_valid_session() -> str | None:
    """
    Проверяет access/refresh токены и активную сессию в БД.
    Возвращает username или None.
    """
    cached = _cached_valid_user()
    if cached:
        return cached

    flash = st.session_state.pop("auth_session_flash", None)
    if flash:
        st.warning(flash)

    access = st.session_state.get("auth_access_token")
    refresh = st.session_state.get("auth_refresh_token")
    session_id = st.session_state.get("auth_session_id")
    access_str = access if isinstance(access, str) else None
    session_str = session_id if isinstance(session_id, str) else None

    if access_str:
        try:
            payload, fatal = validate_access_token(access_str)
        except Exception:
            payload, fatal = None, None
        if payload:
            user = str(payload["username"])
            _set_cached_user(user)
            return user
        if fatal:
            _clear_auth_state(flash=fatal)
            return None

    if isinstance(refresh, str) and refresh and session_str:
        from auth_sessions import get_session, refresh_user_session, session_error_message

        try:
            renewed = refresh_user_session(refresh, session_str)
        except Exception:
            renewed = None

        if renewed:
            _store_session_bundle(renewed)
            return renewed["username"]

        trusted = trust_active_db_session(session_str)
        if trusted:
            return trusted

        try:
            session = get_session(session_str)
        except Exception:
            session = None

        if session and str(session.get("status") or "") != "active":
            _clear_auth_state(flash=session_error_message(session))
        else:
            _clear_auth_state(flash=SESSION_REVOKED_MESSAGE)
        return None

    if st.session_state.get("auth_token"):
        _clear_auth_state(flash="Сессия устарела. Войдите снова.")

    return None


def store_login_bundle(bundle: dict[str, str]) -> None:
    _store_session_bundle(bundle)
