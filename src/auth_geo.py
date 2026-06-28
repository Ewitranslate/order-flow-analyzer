"""Геолокация по IP (только для журнала входов)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from functools import lru_cache


@lru_cache(maxsize=256)
def lookup_geo(ip: str) -> tuple[str, str]:
    addr = (ip or "").strip()
    if not addr or addr in ("—", "127.0.0.1", "::1") or addr.startswith("192.168.") or addr.startswith("10."):
        return "", ""
    url = f"http://ip-api.com/json/{addr}?fields=status,country,city"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "orderflow-auth/1"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = json.loads(resp.read().decode())
        if not isinstance(raw, dict) or raw.get("status") != "success":
            return "", ""
        return str(raw.get("country") or ""), str(raw.get("city") or "")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return "", ""
