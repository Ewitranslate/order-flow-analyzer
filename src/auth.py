"""
Доступ по логину для Streamlit: файл пользователей, хеш пароля, сессия.

Настройка: `.streamlit/secrets.toml` → секция `[auth]` (см. secrets.toml.example).
Создание пользователя (CLI, без подтверждения почты): `python src/auth_manage.py add USER PASSWORD --email user@mail.com`
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Literal

import streamlit as st

from email_service import send_verification_email

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_USERS_FILE = _PROJECT_ROOT / "data" / "users.json"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,64}$")
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
_PBKDF2_ROUNDS = 260_000
_VERIFY_TTL_SEC = 48 * 3600

LoginResult = Literal["ok", "invalid", "unverified", "inactive"]
Role = Literal["admin", "user"]

PAGE_MAIN = "main"
PAGE_WILLIAMS = "williams_scanner"
PAGE_ADMIN = "admin"
PAGE_ACCOUNT = "account"
PAGE_SESSIONS = "sessions"

PAGE_CATALOG: dict[str, str] = {
    PAGE_MAIN: "График (Order Flow)",
    PAGE_WILLIAMS: "Cripto Scanner",
    PAGE_ADMIN: "Администрирование",
    PAGE_ACCOUNT: "Аккаунт",
    PAGE_SESSIONS: "Активные сессии",
}
USER_ASSIGNABLE_PAGES: tuple[str, ...] = (PAGE_MAIN, PAGE_WILLIAMS)


def _auth_cfg() -> dict[str, Any]:
    try:
        raw = st.secrets.get("auth", {})
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        pass
    out: dict[str, Any] = {}
    if os.environ.get("AUTH_ENABLED", "").strip():
        out["enabled"] = os.environ.get("AUTH_ENABLED", "")
    if os.environ.get("AUTH_SECRET_KEY", "").strip():
        out["secret_key"] = os.environ.get("AUTH_SECRET_KEY", "")
    if os.environ.get("AUTH_ALLOW_REGISTRATION", "").strip():
        out["allow_registration"] = os.environ.get("AUTH_ALLOW_REGISTRATION", "")
    if os.environ.get("AUTH_REGISTRATION_KEY", "").strip():
        out["registration_key"] = os.environ.get("AUTH_REGISTRATION_KEY", "")
    if os.environ.get("AUTH_USERS_FILE", "").strip():
        out["users_file"] = os.environ.get("AUTH_USERS_FILE", "")
    return out


def _truthy(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def auth_enabled() -> bool:
    return _truthy(_auth_cfg().get("enabled"), default=True)


def require_email_verification() -> bool:
    cfg = _auth_cfg()
    if "require_email_verification" in cfg:
        return _truthy(cfg.get("require_email_verification"), default=False)
    return False


def allow_registration() -> bool:
    cfg = _auth_cfg()
    if "allow_registration" in cfg or os.environ.get("AUTH_ALLOW_REGISTRATION", "").strip():
        return _truthy(cfg.get("allow_registration") or os.environ.get("AUTH_ALLOW_REGISTRATION"), default=False)
    return True


def allow_guest_access() -> bool:
    cfg = _auth_cfg()
    if "allow_guest" in cfg or os.environ.get("AUTH_ALLOW_GUEST", "").strip():
        return _truthy(cfg.get("allow_guest") or os.environ.get("AUTH_ALLOW_GUEST"), default=False)
    return False


def _pages_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",") if p.strip()]
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        pid = str(item).strip()
        if pid in USER_ASSIGNABLE_PAGES and pid not in out:
            out.append(pid)
    return out


def default_user_pages() -> list[str]:
    cfg = _auth_cfg()
    pages = _pages_list(cfg.get("default_user_pages"))
    return pages or [PAGE_MAIN]


def default_guest_pages() -> list[str]:
    cfg = _auth_cfg()
    pages = _pages_list(cfg.get("guest_pages"))
    return pages or [PAGE_MAIN]


_SESSION_ENTERED = "app_entered"
_SESSION_GUEST = "auth_guest"


def enter_application() -> None:
    st.session_state[_SESSION_ENTERED] = True


def enter_as_guest() -> None:
    """Вход в приложение без аккаунта (демо / гость)."""
    st.session_state[_SESSION_GUEST] = True
    for key in (
        "auth_token",
        "auth_user",
        "auth_access_token",
        "auth_refresh_token",
        "auth_session_id",
    ):
        st.session_state.pop(key, None)
    enter_application()


def is_guest_session() -> bool:
    return bool(st.session_state.get(_SESSION_GUEST))


def application_entered() -> bool:
    return bool(st.session_state.get(_SESSION_ENTERED))


def leave_application() -> None:
    st.session_state.pop(_SESSION_ENTERED, None)
    st.session_state.pop(_SESSION_GUEST, None)


def users_file_path() -> Path:
    raw = _auth_cfg().get("users_file")
    if raw:
        p = Path(str(raw))
        return p if p.is_absolute() else _PROJECT_ROOT / p
    return _DEFAULT_USERS_FILE


def _secret_key() -> str:
    key = str(_auth_cfg().get("secret_key") or os.environ.get("AUTH_SECRET_KEY", "")).strip()
    if not key:
        key = "dev-insecure-change-me-in-secrets"
    return key


def _registration_key_expected() -> str:
    return str(_auth_cfg().get("registration_key") or "").strip()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        _PBKDF2_ROUNDS,
    )
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt, hex_digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        expect = bytes.fromhex(hex_digest)
        got = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            rounds,
        )
        return hmac.compare_digest(got, expect)
    except (ValueError, TypeError):
        return False


def _load_users_db() -> dict[str, Any]:
    path = users_file_path()
    if not path.is_file():
        return {"users": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("users"), dict):
            return raw
    except (OSError, json.JSONDecodeError):
        pass
    return {"users": {}}


def _save_users_db(db: dict[str, Any]) -> None:
    path = users_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def user_exists(username: str) -> bool:
    u = username.strip().lower()
    return u in _load_users_db().get("users", {})


def get_user_record(username: str | None) -> dict[str, Any] | None:
    if not username:
        return None
    rec = _load_users_db().get("users", {}).get(username.strip().lower())
    return dict(rec) if isinstance(rec, dict) else None


def is_admin(username: str | None) -> bool:
    rec = get_user_record(username)
    return bool(rec and str(rec.get("role", "user")).lower() == "admin")


def user_role(username: str | None) -> Role:
    rec = get_user_record(username)
    if rec and str(rec.get("role", "user")).lower() == "admin":
        return "admin"
    return "user"


def user_allowed_pages(username: str | None) -> set[str]:
    if is_admin(username):
        return set(PAGE_CATALOG)
    rec = get_user_record(username)
    if not rec:
        return set()
    custom = _pages_list(rec.get("pages"))
    if custom:
        return set(custom)
    return set(default_user_pages())


def can_access_page(page_id: str, user: str | None, *, guest: bool = False) -> bool:
    pid = str(page_id).strip()
    if pid in (PAGE_ACCOUNT, PAGE_SESSIONS):
        return bool(user) and not guest
    if pid == PAGE_ADMIN:
        return is_admin(user)
    if is_admin(user):
        return True
    if guest:
        return pid in set(default_guest_pages())
    if not user:
        return False
    return pid in user_allowed_pages(user)


def require_page_access(page_id: str, user: str | None) -> None:
    guest = is_guest_session()
    if can_access_page(page_id, user, guest=guest):
        return
    title = PAGE_CATALOG.get(page_id, page_id)
    if guest:
        st.error(f"Гостевой доступ к разделу «{title}» закрыт. Войдите или зарегистрируйтесь.")
    elif not user:
        st.error(f"Для раздела «{title}» нужен вход в аккаунт.")
    else:
        st.error(f"У вас нет доступа к разделу «{title}». Обратитесь к администратору.")
    if st.button("На главную", key=f"denied_home_{page_id}"):
        leave_application()
        st.rerun()
    st.stop()


def list_users_for_admin() -> list[dict[str, Any]]:
    users = _load_users_db().get("users", {})
    rows: list[dict[str, Any]] = []
    for name, rec in sorted(users.items()):
        if not isinstance(rec, dict):
            continue
        pages = _pages_list(rec.get("pages"))
        rows.append(
            {
                "username": name,
                "active": bool(rec.get("active", True)),
                "role": str(rec.get("role", "user")),
                "email": rec.get("email"),
                "email_verified": _user_email_verified(rec),
                "pages": pages or list(default_user_pages()),
                "created_at": rec.get("created_at", ""),
            }
        )
    return rows


def set_user_active(username: str, active: bool) -> None:
    name = username.strip().lower()
    db = _load_users_db()
    users = db.setdefault("users", {})
    if name not in users:
        raise ValueError(f"Пользователь «{name}» не найден")
    users[name]["active"] = bool(active)
    _save_users_db(db)


def set_user_role(username: str, role: Role) -> None:
    name = username.strip().lower()
    if role not in ("admin", "user"):
        raise ValueError("Роль: admin или user")
    db = _load_users_db()
    users = db.setdefault("users", {})
    if name not in users:
        raise ValueError(f"Пользователь «{name}» не найден")
    users[name]["role"] = role
    _save_users_db(db)


def set_user_pages(username: str, pages: list[str]) -> None:
    name = username.strip().lower()
    clean = _pages_list(pages)
    if not clean:
        raise ValueError("Нужна хотя бы одна страница")
    db = _load_users_db()
    users = db.setdefault("users", {})
    if name not in users:
        raise ValueError(f"Пользователь «{name}» не найден")
    users[name]["pages"] = clean
    _save_users_db(db)


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _validate_email(email: str) -> None:
    em = _normalize_email(email)
    if not em or not _EMAIL_RE.match(em):
        raise ValueError("Укажите корректный email")


def _user_email_verified(rec: dict[str, Any]) -> bool:
    if "email_verified" not in rec:
        return True
    return bool(rec.get("email_verified"))


def _new_verification_token() -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    exp = int(time.time()) + _VERIFY_TTL_SEC
    return token, exp


def create_user(
    username: str,
    password: str,
    *,
    email: str | None = None,
    active: bool = True,
    email_verified: bool | None = None,
    role: Role = "user",
    pages: list[str] | None = None,
) -> None:
    """Создание пользователя (CLI / админ). По умолчанию email считается подтверждённым."""
    name = username.strip().lower()
    if not _USERNAME_RE.match(name):
        raise ValueError("Логин: 3–64 символа, латиница, цифры, . _ -")
    if len(password) < 8:
        raise ValueError("Пароль не короче 8 символов")
    if role not in ("admin", "user"):
        raise ValueError("Роль: admin или user")
    em = _normalize_email(email) if email else ""
    if em:
        _validate_email(em)
    verified = bool(email_verified) if email_verified is not None else True
    page_list = _pages_list(pages) if pages is not None else list(default_user_pages())
    if role != "admin" and not page_list:
        raise ValueError("Нужна хотя бы одна страница")
    db = _load_users_db()
    users = db.setdefault("users", {})
    if name in users:
        raise ValueError(f"Пользователь «{name}» уже существует")
    if em and any(
        _normalize_email(str(r.get("email", ""))) == em for r in users.values() if r.get("email")
    ):
        raise ValueError("Этот email уже зарегистрирован")
    rec: dict[str, Any] = {
        "password_hash": hash_password(password),
        "active": bool(active),
        "role": role,
        "email": em or None,
        "email_verified": verified,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if role != "admin":
        rec["pages"] = page_list
    users[name] = rec
    _save_users_db(db)


def register_user(username: str, password: str) -> None:
    """Регистрация через UI: логин и пароль, вход сразу после создания аккаунта."""
    create_user(username, password, email_verified=True)


def verify_email_token(token: str) -> tuple[bool, str]:
    tok = (token or "").strip()
    if not tok:
        return False, "Пустая ссылка подтверждения."
    db = _load_users_db()
    users = db.get("users", {})
    now = int(time.time())
    for name, rec in users.items():
        if str(rec.get("verification_token", "")) != tok:
            continue
        exp = int(rec.get("verification_expires") or 0)
        if exp < now:
            return False, "Ссылка подтверждения истекла. Зарегистрируйтесь снова или обратитесь к администратору."
        rec["email_verified"] = True
        rec.pop("verification_token", None)
        rec.pop("verification_expires", None)
        _save_users_db(db)
        return True, f"Email подтверждён. Можно войти как **{name}**."
    return False, "Ссылка недействительна или уже использована."


def resend_verification_email(username: str) -> tuple[bool, str]:
    name = username.strip().lower()
    rec = _load_users_db().get("users", {}).get(name)
    if not rec:
        return False, "Пользователь не найден."
    if _user_email_verified(rec):
        return False, "Email уже подтверждён — войдите с паролем."
    em = _normalize_email(str(rec.get("email") or ""))
    if not em:
        return False, "У аккаунта нет email."
    token, exp = _new_verification_token()
    rec["verification_token"] = token
    rec["verification_expires"] = exp
    db = _load_users_db()
    db.setdefault("users", {})[name] = rec
    _save_users_db(db)
    return send_verification_email(to_email=em, username=name, token=token)


def check_login(username: str, password: str) -> LoginResult:
    name = username.strip().lower()
    rec = _load_users_db().get("users", {}).get(name)
    if not rec:
        return "invalid"
    if not rec.get("active", True):
        return "inactive"
    stored = str(rec.get("password_hash", ""))
    if not verify_password(password, stored):
        return "invalid"
    if require_email_verification() and not _user_email_verified(rec):
        return "unverified"
    return "ok"


def authenticate(username: str, password: str) -> bool:
    return check_login(username, password) == "ok"


def _login_failure_reason(result: LoginResult) -> str:
    return {
        "invalid": "invalid_credentials",
        "inactive": "inactive",
        "unverified": "unverified",
    }.get(result, result)


def login_user(username: str, password: str) -> LoginResult:
    from auth_audit import record_login_event
    from auth_client import get_client_context
    from auth_guard import store_login_bundle
    from auth_sessions import create_user_session
    from auth_store import init_auth_db

    init_auth_db()
    client = get_client_context()
    result = check_login(username, password)
    if result != "ok":
        record_login_event(
            username=username,
            session_id=None,
            client=client,
            status="failed",
            reason=_login_failure_reason(result),
        )
        return result

    bundle = create_user_session(username, client)
    st.session_state.pop(_SESSION_GUEST, None)
    store_login_bundle(bundle)
    record_login_event(
        username=username,
        session_id=bundle["session_id"],
        client=client,
        status="success",
        device_name=client.device_name,
    )
    return "ok"


def get_logged_in_user() -> str | None:
    from auth_guard import require_valid_session
    from auth_store import init_auth_db

    init_auth_db()
    user = require_valid_session()
    if not user:
        return None
    rec = get_user_record(user)
    if not rec or not rec.get("active", True):
        logout_user()
        return None
    return user


def logout_user() -> None:
    from auth_guard import _clear_auth_state
    from auth_sessions import revoke_session_by_refresh
    from auth_store import init_auth_db

    init_auth_db()
    refresh = st.session_state.get("auth_refresh_token")
    if isinstance(refresh, str) and refresh:
        revoke_session_by_refresh(refresh)
    _clear_auth_state()
    leave_application()


def _handle_email_verification_query() -> None:
    raw = st.query_params.get("verify")
    token = (raw[0] if isinstance(raw, list) else raw) or ""
    if not str(token).strip():
        return
    ok, msg = verify_email_token(str(token))
    if ok:
        st.success(msg)
    else:
        st.error(msg)
    try:
        del st.query_params["verify"]
    except Exception:
        pass


def render_auth_gate() -> str | None:
    """
    Пока пользователь не вошёл в приложение — главная (описание + вход/регистрация).
    Возвращает логин или None (если auth выключен).
    """
    if not auth_enabled():
        enter_application()
        return None

    user: str | None = get_logged_in_user()
    if user:
        enter_application()

    if application_entered():
        if not user and not is_guest_session():
            # Не сбрасывать вход при кратковременном сбое проверки токена в том же сеансе
            sid = st.session_state.get("auth_session_id")
            if isinstance(sid, str) and sid:
                from auth_guard import trust_active_db_session

                recovered = trust_active_db_session(sid)
                if recovered:
                    return recovered
            leave_application()
        else:
            return user

    from home_page import render_home_landing

    _handle_email_verification_query()
    render_home_landing()
    st.stop()
    return None


def render_auth_sidebar(user: str | None) -> None:
    if not application_entered():
        return
    st.sidebar.divider()
    if user:
        role_label = "администратор" if is_admin(user) else "пользователь"
        st.sidebar.caption(f"Вошли как **{user}** ({role_label})")
        if is_admin(user):
            st.sidebar.page_link("pages/4_Admin.py", label="Администрирование")
        st.sidebar.page_link("pages/5_Account.py", label="Аккаунт")
        st.sidebar.page_link("pages/6_Active_Sessions.py", label="Активные сессии")
        if st.sidebar.button("Выйти", key="auth_logout", use_container_width=True):
            logout_user()
            st.rerun()
    elif is_guest_session():
        st.sidebar.caption("Режим **гостя** (без регистрации)")
        if st.sidebar.button("На главную", key="auth_home_guest", use_container_width=True):
            leave_application()
            st.rerun()
    elif st.sidebar.button("На главную", key="auth_home", use_container_width=True):
        leave_application()
        st.rerun()
