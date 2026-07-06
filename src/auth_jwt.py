"""JWT Access Token и opaque Refresh Token."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from typing import Any

import jwt

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_BYTES = 48


def _secret_key() -> str:
    key = os.environ.get("AUTH_SECRET_KEY", "").strip()
    if key:
        return key
    try:
        import streamlit as st

        key = str(st.secrets.get("auth", {}).get("secret_key", "")).strip()
    except Exception:
        key = ""
    return key or "dev-insecure-change-me-in-secrets"


def access_token_ttl_sec() -> int:
    try:
        import streamlit as st

        raw = st.secrets.get("auth", {}).get("access_token_ttl_min", 60)
    except Exception:
        raw = os.environ.get("AUTH_ACCESS_TOKEN_TTL_MIN", "60")
    try:
        return max(1, int(raw)) * 60
    except (TypeError, ValueError):
        return 60 * 60


def refresh_token_ttl_sec() -> int:
    try:
        import streamlit as st

        raw = st.secrets.get("auth", {}).get("refresh_token_ttl_days", 7)
    except Exception:
        raw = os.environ.get("AUTH_REFRESH_TOKEN_TTL_DAYS", "7")
    try:
        return max(1, int(raw)) * 86400
    except (TypeError, ValueError):
        return 7 * 86400


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def new_jti() -> str:
    return secrets.token_urlsafe(16)


def create_access_token(
    *,
    username: str,
    session_id: str,
    jti: str | None = None,
) -> str:
    now = int(time.time())
    payload = {
        "typ": ACCESS_TOKEN_TYPE,
        "sub": username.strip().lower(),
        "sid": session_id,
        "jti": jti or new_jti(),
        "iat": now,
        "exp": now + access_token_ttl_sec(),
    }
    return jwt.encode(payload, _secret_key(), algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, _secret_key(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        return None
    sub = str(payload.get("sub", "")).strip().lower()
    sid = str(payload.get("sid", "")).strip()
    jti = str(payload.get("jti", "")).strip()
    if not sub or not sid or not jti:
        return None
    return {
        "username": sub,
        "session_id": sid,
        "jti": jti,
        "exp": int(payload.get("exp", 0)),
    }
