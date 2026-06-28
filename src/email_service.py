"""Отправка писем (подтверждение регистрации) через SMTP."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import quote

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SECRETS_PATH = _PROJECT_ROOT / ".streamlit" / "secrets.toml"


def _strip_toml_value(raw: str) -> str:
    v = (raw or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1].strip()
    return v


def _truthy(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _load_smtp_from_secrets_file() -> dict[str, Any]:
    """Читает `[auth.smtp]` и `auth.dev_log_verification` из secrets.toml."""
    if not _SECRETS_PATH.is_file():
        return {}
    out: dict[str, Any] = {}
    section = ""
    try:
        for line in _SECRETS_PATH.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1].strip().lower()
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            k = key.strip().lower()
            v = _strip_toml_value(val)
            if section == "auth.smtp":
                if k in ("host", "port", "user", "password", "from_email", "from_name", "base_url"):
                    out[k] = v
                elif k in ("use_tls", "dev_log_token") and v:
                    out[k] = v
            elif section == "auth" and k == "dev_log_verification" and v:
                out["dev_log_token"] = v
    except OSError:
        return {}
    return out


def _smtp_cfg() -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        import streamlit as st

        auth = st.secrets.get("auth", {})
        if isinstance(auth, dict):
            if _truthy(auth.get("dev_log_verification")):
                out["dev_log_token"] = True
            smtp = auth.get("smtp", {})
            if isinstance(smtp, dict):
                out.update(smtp)
        # Streamlit TOML: [auth.smtp] как st.secrets["auth.smtp"]
        try:
            smtp_top = st.secrets.get("auth.smtp", {})
            if isinstance(smtp_top, dict):
                out.update(smtp_top)
        except Exception:
            pass
    except Exception:
        pass

    file_cfg = _load_smtp_from_secrets_file()
    for k, v in file_cfg.items():
        out.setdefault(k, v)

    for key, env in (
        ("host", "SMTP_HOST"),
        ("port", "SMTP_PORT"),
        ("user", "SMTP_USER"),
        ("password", "SMTP_PASSWORD"),
        ("from_email", "SMTP_FROM"),
        ("from_name", "SMTP_FROM_NAME"),
        ("base_url", "APP_BASE_URL"),
        ("use_tls", "SMTP_USE_TLS"),
        ("dev_log_token", "AUTH_DEV_LOG_VERIFICATION"),
    ):
        if env in os.environ and os.environ[env].strip():
            out[key] = os.environ[env].strip()
    return out


def smtp_configured() -> bool:
    cfg = _smtp_cfg()
    return bool(str(cfg.get("host") or "").strip() and str(cfg.get("from_email") or "").strip())


def dev_log_verification_links() -> bool:
    cfg = _smtp_cfg()
    return _truthy(cfg.get("dev_log_token"), default=False)


def email_delivery_mode() -> str:
    """`smtp` | `dev_log` | `none`"""
    if smtp_configured():
        return "smtp"
    if dev_log_verification_links():
        return "dev_log"
    return "none"


def app_base_url() -> str:
    cfg = _smtp_cfg()
    base = str(cfg.get("base_url") or os.environ.get("APP_BASE_URL", "")).strip().rstrip("/")
    return base or "http://localhost:8501"


def build_verification_url(token: str) -> str:
    return f"{app_base_url()}?verify={quote(token, safe='')}"


def _remember_dev_verify_url(url: str) -> None:
    print(f"[auth] Подтверждение (dev): {url}")
    try:
        import streamlit as st

        st.session_state["auth_dev_verify_url"] = url
    except Exception:
        pass


def send_verification_email(*, to_email: str, username: str, token: str) -> tuple[bool, str]:
    """Отправить письмо с ссылкой подтверждения. Без SMTP — режим dev_log_token."""
    url = build_verification_url(token)
    if not smtp_configured():
        if dev_log_verification_links():
            _remember_dev_verify_url(url)
            return True, "dev_log"
        return (
            False,
            "SMTP не настроен. Добавьте `[auth.smtp]` в secrets.toml или "
            "`dev_log_verification = true` в `[auth]` для локальной разработки.",
        )

    cfg = _smtp_cfg()
    host = str(cfg["host"]).strip()
    port = int(cfg.get("port") or 587)
    user = str(cfg.get("user") or "").strip()
    password = str(cfg.get("password") or "").strip()
    from_email = str(cfg["from_email"]).strip()
    from_name = str(cfg.get("from_name") or "Order Flow Analyzer").strip()
    use_tls = _truthy(cfg.get("use_tls"), default=True)

    subject = "Подтвердите регистрацию — Order Flow Analyzer"
    body_text = (
        f"Здравствуйте, {username}!\n\n"
        f"Подтвердите регистрацию, перейдя по ссылке (действует 48 часов):\n{url}\n\n"
        "Если вы не регистрировались — проигнорируйте это письмо.\n"
    )
    body_html = (
        f"<p>Здравствуйте, <b>{username}</b>!</p>"
        f"<p>Подтвердите регистрацию (ссылка действует <b>48 часов</b>):</p>"
        f'<p><a href="{url}">{url}</a></p>'
        "<p>Если вы не регистрировались — проигнорируйте это письмо.</p>"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    try:
        if use_tls:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context()) as smtp:
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(msg)
    except OSError as e:
        return False, f"ошибка SMTP: {e}"
    except smtplib.SMTPException as e:
        return False, f"ошибка SMTP: {e}"

    return True, "письмо отправлено"
