#!/usr/bin/env python3
"""
Создаёт .streamlit/secrets.toml из переменных окружения (Render, Docker).

Streamlit требует файл secrets.toml; на PaaS секреты задают через Environment.
"""

from __future__ import annotations

import json
import os
import secrets as secmod
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SECRETS_DIR = ROOT / ".streamlit"
SECRETS_PATH = SECRETS_DIR / "secrets.toml"
DATA_DIR = ROOT / "data"
USERS_FILE = DATA_DIR / "users.json"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _toml_bool(name: str, default: str = "true") -> str:
    val = _env(name, default).lower()
    return "true" if val in ("1", "true", "yes", "on") else "false"


def _toml_string(val: str) -> str:
    escaped = val.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _users_file_path() -> Path:
    raw = _env("AUTH_USERS_FILE", "data/users.json")
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


def _write_secrets() -> None:
    secret_key = _env("AUTH_SECRET_KEY")
    if not secret_key:
        secret_key = secmod.token_urlsafe(48)
        print(
            "WARNING: AUTH_SECRET_KEY не задан — сгенерирован временный ключ; "
            "сессии сбросятся при рестарте. Задайте AUTH_SECRET_KEY в Render Environment.",
            file=sys.stderr,
        )

    users_file = _env("AUTH_USERS_FILE", "data/users.json")
    auth_db = _env("AUTH_DB_FILE", "data/auth.sqlite3")

    lines = [
        "# Сгенерировано scripts/render_write_secrets.py при старте контейнера",
        "",
        "[auth]",
        f"enabled = {_toml_bool('AUTH_ENABLED', 'true')}",
        f"secret_key = {_toml_string(secret_key)}",
        f"allow_registration = {_toml_bool('AUTH_ALLOW_REGISTRATION', 'true')}",
        f"allow_guest = {_toml_bool('AUTH_ALLOW_GUEST', 'false')}",
        f"require_email_verification = {_toml_bool('AUTH_REQUIRE_EMAIL_VERIFICATION', 'false')}",
        'default_user_pages = ["main", "williams_scanner"]',
        'guest_pages = ["main"]',
        f"registration_key = {_toml_string(_env('AUTH_REGISTRATION_KEY'))}",
        f"users_file = {_toml_string(users_file)}",
        f"auth_db = {_toml_string(auth_db)}",
        f"access_token_ttl_min = {int(_env('AUTH_ACCESS_TOKEN_TTL_MIN', '60') or '60')}",
        f"refresh_token_ttl_days = {int(_env('AUTH_REFRESH_TOKEN_TTL_DAYS', '7') or '7')}",
        f"session_inactivity_days = {int(_env('AUTH_SESSION_INACTIVITY_DAYS', '30') or '30')}",
    ]

    bot_token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    if bot_token or chat_id or _env("TELEGRAM_ENABLED"):
        lines.extend(["", "[telegram]"])
        if bot_token:
            lines.append(f"bot_token = {_toml_string(bot_token)}")
        if chat_id:
            lines.append(f"chat_id = {_toml_string(chat_id)}")
        proxy = _env("TELEGRAM_PROXY")
        if proxy:
            lines.append(f"proxy = {_toml_string(proxy)}")
        if _toml_bool("TELEGRAM_ENABLED", "false") == "true":
            lines.append("enabled = true")

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {SECRETS_PATH}")


def _ensure_data_dir() -> None:
    users_path = _users_file_path()
    users_path.parent.mkdir(parents=True, exist_ok=True)
    if users_path.is_file():
        return
    users_path.write_text(
        json.dumps({"users": {}}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Initialized empty {users_path}")


def _count_users() -> int:
    users_path = _users_file_path()
    if not users_path.is_file():
        return 0
    try:
        raw = json.loads(users_path.read_text(encoding="utf-8"))
        users = raw.get("users", {}) if isinstance(raw, dict) else {}
        return len(users) if isinstance(users, dict) else 0
    except (OSError, json.JSONDecodeError):
        return 0


def _sanitize_users_file() -> None:
    """Удаляет пользователей с битым password_hash (старый шаблон users.json.example)."""
    users_path = _users_file_path()
    if not users_path.is_file():
        return
    try:
        raw = json.loads(users_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict) or not isinstance(raw.get("users"), dict):
        return
    users = raw["users"]
    removed: list[str] = []
    for name, rec in list(users.items()):
        if not isinstance(rec, dict):
            users.pop(name, None)
            removed.append(str(name))
            continue
        stored = str(rec.get("password_hash", ""))
        if not stored.startswith("pbkdf2_sha256$"):
            users.pop(name, None)
            removed.append(str(name))
    if removed:
        users_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Removed invalid users from {users_path}: {', '.join(removed)}")


def _bootstrap_admin() -> None:
    """Создаёт первого админа из AUTH_BOOTSTRAP_USER / AUTH_BOOTSTRAP_PASSWORD."""
    user = _env("AUTH_BOOTSTRAP_USER")
    password = _env("AUTH_BOOTSTRAP_PASSWORD")
    if not user or not password:
        return
    if len(password) < 8:
        print("WARNING: AUTH_BOOTSTRAP_PASSWORD короче 8 символов — пропуск bootstrap", file=sys.stderr)
        return

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    from auth import create_user, set_user_password, set_user_role, user_exists  # noqa: WPS433

    name = user.strip().lower()
    if user_exists(name):
        set_user_password(name, password)
        set_user_role(name, "admin")
        print(f"Bootstrap: обновлён пароль администратора «{name}» ({_count_users()} пользователей)")
        return
    create_user(name, password, role="admin", email_verified=True)
    print(f"Bootstrap: создан администратор «{name}»")


def _log_storage_status() -> None:
    users_path = _users_file_path()
    auth_db = _env("AUTH_DB_FILE", "data/auth.sqlite3")
    db_path = Path(auth_db) if Path(auth_db).is_absolute() else ROOT / auth_db
    writable = os.access(users_path.parent, os.W_OK)
    print(
        f"Auth storage: users={users_path} (exists={users_path.is_file()}, "
        f"count={_count_users()}, writable={writable}), db={db_path}"
    )
    if not writable:
        print(
            "ERROR: каталог data не доступен для записи — подключите Persistent Disk на Render.",
            file=sys.stderr,
        )


def main() -> None:
    _write_secrets()
    _ensure_data_dir()
    _sanitize_users_file()
    _bootstrap_admin()
    _log_storage_status()


if __name__ == "__main__":
    main()
