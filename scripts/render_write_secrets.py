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
SECRETS_DIR = ROOT / ".streamlit"
SECRETS_PATH = SECRETS_DIR / "secrets.toml"
DATA_DIR = ROOT / "data"
USERS_FILE = DATA_DIR / "users.json"
USERS_EXAMPLE = DATA_DIR / "users.json.example"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _toml_bool(name: str, default: str = "true") -> str:
    val = _env(name, default).lower()
    return "true" if val in ("1", "true", "yes", "on") else "false"


def _toml_string(val: str) -> str:
    escaped = val.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
        'default_user_pages = ["main"]',
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if USERS_FILE.is_file():
        return
    if USERS_EXAMPLE.is_file():
        USERS_FILE.write_text(USERS_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Initialized {USERS_FILE} from example")
    else:
        USERS_FILE.write_text(json.dumps({"users": {}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Initialized empty {USERS_FILE}")


def main() -> None:
    _write_secrets()
    _ensure_data_dir()


if __name__ == "__main__":
    main()
