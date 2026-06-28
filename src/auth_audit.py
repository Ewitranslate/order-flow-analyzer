"""Журнал входов."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from auth_client import ClientContext
from auth_geo import lookup_geo
from auth_store import db_conn, init_auth_db

LoginEventStatus = Literal["success", "failed"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_login_event(
    *,
    username: str | None,
    session_id: str | None,
    client: ClientContext,
    status: LoginEventStatus,
    reason: str | None = None,
    device_name: str | None = None,
) -> None:
    init_auth_db()
    country, city = lookup_geo(client.ip_address)
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO login_events (
                username, device_id, occurred_at, device_name, browser, os_name,
                country, city, ip_address, status, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (username or "").strip().lower() or None,
                session_id,
                _now_iso(),
                device_name or client.device_name,
                client.browser,
                client.os_name,
                country or None,
                city or None,
                client.ip_address or None,
                status,
                reason,
            ),
        )


def list_login_events(username: str, *, limit: int = 50) -> list[dict[str, Any]]:
    init_auth_db()
    name = username.strip().lower()
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, username, device_id, occurred_at, device_name, browser, os_name,
                   country, city, ip_address, status, reason
            FROM login_events
            WHERE username = ?
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (name, max(1, int(limit))),
        ).fetchall()
    return [dict(r) for r in rows]
