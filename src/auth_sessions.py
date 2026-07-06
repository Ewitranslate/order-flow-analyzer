"""Активные сессии, refresh-токены и проверка access JWT."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from auth_client import ClientContext
from auth_geo import lookup_geo
from auth_jwt import (
    create_access_token,
    decode_access_token,
    hash_token,
    new_jti,
    new_refresh_token,
    refresh_token_ttl_sec,
)
from auth_store import db_conn, init_auth_db

SessionStatus = Literal["active", "revoked", "expired"]

SESSION_SUPERSEDED_MESSAGE = (
    "Ваш аккаунт был открыт на другом устройстве. Выполните вход снова."
)
SESSION_REVOKED_MESSAGE = "Сессия завершена. Войдите снова."
SESSION_EXPIRED_MESSAGE = "Сессия истекла из-за длительного отсутствия активности. Войдите снова."


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_session_id() -> str:
    return str(uuid.uuid4())


def session_inactivity_days() -> int:
    try:
        import streamlit as st

        raw = st.secrets.get("auth", {}).get("session_inactivity_days", 30)
    except Exception:
        raw = os.environ.get("AUTH_SESSION_INACTIVITY_DAYS", "30")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 30


def expire_inactive_sessions(*, days: int | None = None) -> int:
    """Завершает сессии без активности дольше `days` (по умолчанию из конфига)."""
    init_auth_db()
    idle_days = days if days is not None else session_inactivity_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=idle_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = _now_iso()
    with db_conn() as conn:
        cur = conn.execute(
            """
            UPDATE user_sessions
            SET status = 'expired', is_active = 0, revoked_at = ?, revoked_reason = 'inactivity'
            WHERE status = 'active' AND last_active_at < ?
            """,
            (now, cutoff_iso),
        )
        return int(cur.rowcount or 0)


def revoke_all_user_sessions(username: str, *, reason: str) -> int:
    init_auth_db()
    name = username.strip().lower()
    now = _now_iso()
    with db_conn() as conn:
        cur = conn.execute(
            """
            UPDATE user_sessions
            SET status = 'revoked', is_active = 0, revoked_at = ?, revoked_reason = ?
            WHERE username = ? AND status = 'active'
            """,
            (now, reason, name),
        )
        return int(cur.rowcount or 0)


def revoke_other_sessions(username: str, keep_session_id: str) -> int:
    init_auth_db()
    name = username.strip().lower()
    keep = keep_session_id.strip()
    now = _now_iso()
    with db_conn() as conn:
        cur = conn.execute(
            """
            UPDATE user_sessions
            SET status = 'revoked', is_active = 0, revoked_at = ?, revoked_reason = 'manual_other'
            WHERE username = ? AND session_id != ? AND status = 'active'
            """,
            (now, name, keep),
        )
        return int(cur.rowcount or 0)


def get_session(session_id: str) -> dict[str, Any] | None:
    init_auth_db()
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def list_user_sessions(username: str, *, limit: int = 50) -> list[dict[str, Any]]:
    init_auth_db()
    name = username.strip().lower()
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT session_id, username, status, created_at, last_active_at, revoked_at, revoked_reason,
                   ip_address, browser, os_name, device_name, country, city
            FROM user_sessions
            WHERE username = ?
            ORDER BY
                CASE status WHEN 'active' THEN 0 ELSE 1 END,
                last_active_at DESC
            LIMIT ?
            """,
            (name, max(1, int(limit))),
        ).fetchall()
    return [dict(r) for r in rows]


def create_user_session(username: str, client: ClientContext) -> dict[str, str]:
    """
    Завершает другие активные сессии пользователя и создаёт единственную активную сессию.
    """
    init_auth_db()
    expire_inactive_sessions()
    revoke_all_user_sessions(username, reason="superseded")

    session_id = _new_session_id()
    refresh_raw = new_refresh_token()
    refresh_hash = hash_token(refresh_raw)
    access_jti = new_jti()
    now = _now_iso()
    country, city = lookup_geo(client.ip_address)
    exp_epoch = int(datetime.now(timezone.utc).timestamp()) + refresh_token_ttl_sec()

    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_sessions (
                session_id, username, refresh_token_hash, access_jti,
                status, is_active, created_at, last_active_at,
                ip_address, browser, os_name, device_name, country, city, user_agent
            ) VALUES (?, ?, ?, ?, 'active', 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                username.strip().lower(),
                refresh_hash,
                access_jti,
                now,
                now,
                client.ip_address or None,
                client.browser,
                client.os_name,
                client.device_name,
                country or None,
                city or None,
                client.user_agent or None,
            ),
        )

    access = create_access_token(
        username=username,
        session_id=session_id,
        jti=access_jti,
    )
    return {
        "access_token": access,
        "refresh_token": refresh_raw,
        "session_id": session_id,
        "username": username.strip().lower(),
        "refresh_expires_at": str(exp_epoch),
    }


def session_error_message(session: dict[str, Any]) -> str:
    reason = str(session.get("revoked_reason") or "")
    status = str(session.get("status") or "")
    if reason == "superseded":
        return SESSION_SUPERSEDED_MESSAGE
    if status == "expired" or reason == "inactivity":
        return SESSION_EXPIRED_MESSAGE
    return SESSION_REVOKED_MESSAGE


def validate_access_token(token: str) -> tuple[dict[str, Any] | None, str | None]:
    """
    Проверяет JWT и активную сессию в БД.
    Возвращает (payload, fatal_error).
    payload is None и fatal_error is None — access устарел, нужен refresh (не выход).
    """
    payload = decode_access_token(token)
    if not payload:
        return None, None

    session = get_session(payload["session_id"])
    if not session:
        return None, SESSION_REVOKED_MESSAGE
    if str(session.get("status") or "") != "active":
        return None, session_error_message(session)
    if str(session.get("access_jti") or "") != payload["jti"]:
        return None, None
    if str(session.get("username") or "") != payload["username"]:
        return None, SESSION_REVOKED_MESSAGE

    touch_session(payload["session_id"])
    return payload, None


def touch_session(session_id: str) -> None:
    init_auth_db()
    now = _now_iso()
    try:
        with db_conn() as conn:
            conn.execute(
                "UPDATE user_sessions SET last_active_at = ? WHERE session_id = ? AND status = 'active'",
                (now, session_id),
            )
    except Exception:
        pass


def refresh_user_session(refresh_token: str, session_id: str) -> dict[str, str] | None:
    """
    Продлевает access JWT. Refresh-токен не ротируем — иначе при быстрых rerun
    Streamlit второй запрос получает 401 и выкидывает на вход.
    """
    init_auth_db()
    token_hash = hash_token(refresh_token)
    sid = session_id.strip()
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM user_sessions
            WHERE refresh_token_hash = ? AND session_id = ? AND status = 'active'
            """,
            (token_hash, sid),
        ).fetchone()
        if not row:
            return None

        username = str(row["username"])
        new_jti_val = new_jti()
        now = _now_iso()

        conn.execute(
            """
            UPDATE user_sessions
            SET access_jti = ?, last_active_at = ?
            WHERE session_id = ?
            """,
            (new_jti_val, now, sid),
        )

    access = create_access_token(
        username=username,
        session_id=sid,
        jti=new_jti_val,
    )
    return {
        "access_token": access,
        "refresh_token": refresh_token,
        "session_id": sid,
        "username": username,
    }


def revoke_session(session_id: str, *, reason: str = "logout") -> None:
    init_auth_db()
    now = _now_iso()
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE user_sessions
            SET status = 'revoked', is_active = 0, revoked_at = ?, revoked_reason = ?
            WHERE session_id = ?
            """,
            (now, reason, session_id),
        )


def revoke_session_by_refresh(refresh_token: str) -> None:
    init_auth_db()
    token_hash = hash_token(refresh_token)
    with db_conn() as conn:
        row = conn.execute(
            "SELECT session_id FROM user_sessions WHERE refresh_token_hash = ?",
            (token_hash,),
        ).fetchone()
    if row:
        revoke_session(str(row["session_id"]), reason="logout")
