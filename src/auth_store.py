"""SQLite-хранилище пользователей, сессий и журнала входов."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _PROJECT_ROOT / "data" / "auth.sqlite3"

_SCHEMA_VERSION = 4

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

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL DEFAULT 'user',
    email TEXT,
    email_verified INTEGER NOT NULL DEFAULT 1,
    pages_json TEXT,
    created_at TEXT,
    verification_token TEXT,
    verification_expires INTEGER
);
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


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    """Таблица users (миграция с users.json)."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            role TEXT NOT NULL DEFAULT 'user',
            email TEXT,
            email_verified INTEGER NOT NULL DEFAULT 1,
            pages_json TEXT,
            created_at TEXT,
            verification_token TEXT,
            verification_expires INTEGER
        );
        """
    )


def _user_row_to_rec(row: sqlite3.Row) -> dict[str, Any]:
    pages_json = row["pages_json"]
    pages: list[str] | None = None
    if pages_json:
        try:
            raw = json.loads(pages_json)
            if isinstance(raw, list):
                pages = [str(p) for p in raw]
        except (TypeError, json.JSONDecodeError):
            pages = None
    rec: dict[str, Any] = {
        "password_hash": str(row["password_hash"]),
        "active": bool(row["active"]),
        "role": str(row["role"] or "user"),
        "email": row["email"],
        "email_verified": bool(row["email_verified"]),
        "created_at": row["created_at"] or "",
    }
    if pages is not None:
        rec["pages"] = pages
    if row["verification_token"]:
        rec["verification_token"] = row["verification_token"]
    if row["verification_expires"] is not None:
        rec["verification_expires"] = int(row["verification_expires"])
    return rec


def _user_rec_to_row(username: str, rec: dict[str, Any]) -> dict[str, Any]:
    pages = rec.get("pages")
    pages_json = None
    if isinstance(pages, (list, tuple)):
        pages_json = json.dumps([str(p) for p in pages])
    return {
        "username": username.strip().lower(),
        "password_hash": str(rec.get("password_hash", "")),
        "active": 1 if rec.get("active", True) else 0,
        "role": str(rec.get("role", "user")),
        "email": rec.get("email"),
        "email_verified": 1 if rec.get("email_verified", True) else 0,
        "pages_json": pages_json,
        "created_at": rec.get("created_at"),
        "verification_token": rec.get("verification_token"),
        "verification_expires": rec.get("verification_expires"),
    }


def load_users_store() -> dict[str, Any]:
    init_auth_db()
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    users: dict[str, Any] = {}
    for row in rows:
        users[str(row["username"]).lower()] = _user_row_to_rec(row)
    return {"users": users}


def save_users_store(db: dict[str, Any]) -> None:
    init_auth_db()
    users = db.get("users", {})
    if not isinstance(users, dict):
        users = {}
    with db_conn() as conn:
        conn.execute("DELETE FROM users")
        for name, rec in users.items():
            if not isinstance(rec, dict):
                continue
            row = _user_rec_to_row(str(name), rec)
            if not row["password_hash"].startswith("pbkdf2_sha256$"):
                continue
            conn.execute(
                """
                INSERT INTO users (
                    username, password_hash, active, role, email, email_verified,
                    pages_json, created_at, verification_token, verification_expires
                ) VALUES (
                    :username, :password_hash, :active, :role, :email, :email_verified,
                    :pages_json, :created_at, :verification_token, :verification_expires
                )
                """,
                row,
            )


def count_users_store() -> int:
    init_auth_db()
    with db_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    return int(row["c"] or 0) if row else 0


def migrate_users_from_json(json_path: Path) -> int:
    """Импорт users.json в SQLite, если в БД ещё нет пользователей."""
    init_auth_db()
    if count_users_store() > 0:
        return 0
    if not json_path.is_file():
        return 0
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(raw, dict) or not isinstance(raw.get("users"), dict):
        return 0
    users_in: dict[str, Any] = {}
    for name, rec in raw["users"].items():
        if not isinstance(rec, dict):
            continue
        stored = str(rec.get("password_hash", ""))
        if not stored.startswith("pbkdf2_sha256$"):
            continue
        users_in[str(name).strip().lower()] = rec
    if not users_in:
        return 0
    save_users_store({"users": users_in})
    return len(users_in)


def auth_storage_writable() -> bool:
    db_path = auth_db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        probe = db_path.parent / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


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
            if current < 4:
                _migrate_to_v4(conn)
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
