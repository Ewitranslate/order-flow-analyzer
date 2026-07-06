"""SQLite-хранилище сессий и журнала входов."""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _PROJECT_ROOT / "data" / "auth.sqlite3"

_SCHEMA_VERSION = 3

_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_sessions (
    session_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    refresh_token_hash TEXT NOT NULL,
    access_jti TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT,
    ip_address TEXT,
    browser TEXT,
    os_name TEXT,
    device_name TEXT,
    country TEXT,
    city TEXT,
    user_agent TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_user_sessions_username ON user_sessions(username);
CREATE INDEX IF NOT EXISTS idx_user_sessions_refresh ON user_sessions(refresh_token_hash);

CREATE TABLE IF NOT EXISTS login_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    device_id TEXT,
    occurred_at TEXT NOT NULL,
    device_name TEXT,
    browser TEXT,
    os_name TEXT,
    country TEXT,
    city TEXT,
    ip_address TEXT,
    status TEXT NOT NULL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_events_username ON login_events(username);
CREATE INDEX IF NOT EXISTS idx_login_events_time ON login_events(occurred_at);
"""

_lock = threading.Lock()
_initialized = False


def auth_db_path() -> Path:
    raw = os.environ.get("AUTH_DB_FILE", "").strip()
    if not raw:
        try:
            import streamlit as st

            raw = str(st.secrets.get("auth", {}).get("auth_db", "")).strip()
        except Exception:
            raw = ""
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else _PROJECT_ROOT / p
    return _DEFAULT_DB


def _connect() -> sqlite3.Connection:
    path = auth_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Миграция с v1 (устройства + сессии с device_id) на v2 (сессии с метаданными)."""
    if not _table_columns(conn, "user_sessions"):
        return
    cols = _table_columns(conn, "user_sessions")
    additions: tuple[tuple[str, str], ...] = (
        ("status", "TEXT"),
        ("ip_address", "TEXT"),
        ("browser", "TEXT"),
        ("os_name", "TEXT"),
        ("device_name", "TEXT"),
        ("country", "TEXT"),
        ("city", "TEXT"),
        ("user_agent", "TEXT"),
    )
    for name, typ in additions:
        if name not in cols:
            conn.execute(f"ALTER TABLE user_sessions ADD COLUMN {name} {typ}")

    conn.execute(
        """
        UPDATE user_sessions
        SET status = CASE
            WHEN status IS NOT NULL AND status != '' THEN status
            WHEN is_active = 1 THEN 'active'
            ELSE 'revoked'
        END
        """
    )

    if _table_columns(conn, "user_devices"):
        conn.execute("DROP TABLE IF EXISTS user_devices")


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    """Убирает legacy-колонку device_id (NOT NULL в старой схеме v1)."""
    cols = _table_columns(conn, "user_sessions")
    if "device_id" not in cols:
        return
    conn.executescript(
        """
        CREATE TABLE user_sessions_v3 (
            session_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            refresh_token_hash TEXT NOT NULL,
            access_jti TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            revoked_at TEXT,
            revoked_reason TEXT,
            ip_address TEXT,
            browser TEXT,
            os_name TEXT,
            device_name TEXT,
            country TEXT,
            city TEXT,
            user_agent TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO user_sessions_v3 (
            session_id, username, refresh_token_hash, access_jti,
            status, created_at, last_active_at, revoked_at, revoked_reason,
            ip_address, browser, os_name, device_name, country, city, user_agent, is_active
        )
        SELECT
            session_id, username, refresh_token_hash, access_jti,
            CASE
                WHEN status IS NOT NULL AND status != '' THEN status
                WHEN is_active = 1 THEN 'active'
                ELSE 'revoked'
            END,
            created_at, last_active_at, revoked_at, revoked_reason,
            ip_address, browser, os_name, device_name, country, city, user_agent, is_active
        FROM user_sessions;
        DROP TABLE user_sessions;
        ALTER TABLE user_sessions_v3 RENAME TO user_sessions;
        """
    )


def init_auth_db() -> None:
    global _initialized
    with _lock:
        if _initialized:
            return
        with db_conn() as conn:
            conn.executescript(_DDL)
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current = int(row["v"] or 0) if row else 0
            if current < 2:
                _migrate_to_v2(conn)
            if current < 3:
                _migrate_to_v3(conn)
            if current < _SCHEMA_VERSION:
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                for ver in range(current + 1, _SCHEMA_VERSION + 1):
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (ver, now),
                    )
            _ensure_session_indexes(conn)
        _initialized = True


def _ensure_session_indexes(conn: sqlite3.Connection) -> None:
    if "status" in _table_columns(conn, "user_sessions"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_sessions_status ON user_sessions(username, status)"
        )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}
