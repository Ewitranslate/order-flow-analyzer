"""Контекст клиента (User-Agent, IP) — IP только для журнала, не для ограничений."""

from __future__ import annotations

import re
from dataclasses import dataclass

import streamlit as st


@dataclass(frozen=True)
class ClientContext:
    ip_address: str
    user_agent: str
    browser: str
    os_name: str
    device_name: str


def _header_get(headers: dict[str, str] | None, name: str) -> str:
    if not headers:
        return ""
    lower = name.lower()
    for key, val in headers.items():
        if str(key).lower() == lower:
            return str(val).strip()
    return ""


def _extract_ip(headers: dict[str, str] | None) -> str:
    xff = _header_get(headers, "X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    xri = _header_get(headers, "X-Real-Ip")
    if xri:
        return xri
    return _header_get(headers, "Remote-Addr")


def parse_user_agent(ua: str) -> tuple[str, str, str]:
    text = (ua or "").strip()
    if not text:
        return "Неизвестный браузер", "Неизвестная ОС", "Устройство"

    browser = "Браузер"
    if "Edg/" in text or "Edge/" in text:
        browser = "Microsoft Edge"
    elif "Chrome/" in text and "Chromium" not in text:
        browser = "Chrome"
    elif "Firefox/" in text:
        browser = "Firefox"
    elif "Safari/" in text and "Chrome" not in text:
        browser = "Safari"
    elif "OPR/" in text or "Opera" in text:
        browser = "Opera"

    os_name = "ОС"
    if "Windows NT" in text:
        os_name = "Windows"
    elif "Mac OS X" in text or "Macintosh" in text:
        os_name = "macOS"
    elif "Android" in text:
        os_name = "Android"
    elif "iPhone" in text or "iPad" in text:
        os_name = "iOS"
    elif "Linux" in text:
        os_name = "Linux"

    device_name = f"{browser} · {os_name}"
    if "Mobile" in text or "Android" in text or "iPhone" in text:
        device_name = f"Мобильное · {browser} · {os_name}"
    elif re.search(r"iPad|Tablet", text):
        device_name = f"Планшет · {browser} · {os_name}"
    else:
        device_name = f"Компьютер · {browser} · {os_name}"

    return browser, os_name, device_name


def get_client_context() -> ClientContext:
    headers: dict[str, str] | None = None
    try:
        headers = dict(st.context.headers) if hasattr(st, "context") else None
    except Exception:
        headers = None
    ua = _header_get(headers, "User-Agent")
    ip = _extract_ip(headers) or "—"
    browser, os_name, device_name = parse_user_agent(ua)
    return ClientContext(
        ip_address=ip,
        user_agent=ua,
        browser=browser,
        os_name=os_name,
        device_name=device_name,
    )
