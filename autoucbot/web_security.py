"""Small, dependency-free helpers for the admin panel's origin and CSRF checks.

PUBLIC_URL is the canonical external origin. Forwarded host headers are never
an allowlist: only a configured origin, or the trusted ASGI request origin in
local development, is accepted. Login cookies are signed, short-lived and
reused across GET /login requests to avoid invalidating another open tab.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from urllib.parse import urlsplit

LOGIN_CSRF_TTL = 3600
_TOKEN = re.compile(r"v1\.([0-9]{1,12})\.([A-Za-z0-9_-]{43})\.([0-9a-f]{64})", re.ASCII)


def canonical_origin(value: str, *, allow_path: bool = False) -> str:
    """Return scheme://host[:non-default-port], rejecting ambiguous URLs.

    allow_path is only for Referer, never for Origin or PUBLIC_URL.
    Standard ports, host casing and a single trailing slash are equivalent.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("Пустой адрес")
    value = value.strip(" ")
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in value) or "\\" in value:
        raise ValueError("Адрес содержит пробелы или управляющие символы")
    try:
        u = urlsplit(value)
        port = u.port
        if (u.scheme not in ("http", "https") or not u.hostname or
                u.username is not None or u.password is not None or u.fragment):
            raise ValueError("Неверный адрес")
        if not allow_path and (u.path not in ("", "/") or "?" in value or "#" in value):
            raise ValueError("Origin не должен содержать путь или параметры")
        host = u.hostname.lower()
        if ":" in host:
            host = "[" + str(ipaddress.IPv6Address(host)) + "]"
        else:
            host = host.encode("idna").decode("ascii")
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError("Неверный домен")
        if u.netloc.endswith(":") or port == 0:
            raise ValueError("Неверный порт")
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Некорректный адрес панели") from exc
    suffix = "" if port is None or port == {"http": 80, "https": 443}[u.scheme] else f":{port}"
    return f"{u.scheme}://{host}{suffix}"


def source_error(headers, expected_origin: str) -> str | None:
    """Validate a form's source without accepting a forged forwarded host.

    A missing Origin falls back to Referer. If both are absent, a valid CSRF
    token is still mandatory at the caller (privacy clients / local scripts).
    An explicit null, foreign or malformed Origin is never silently ignored.
    """
    if headers.get("sec-fetch-site", "").lower() == "cross-site":
        return "CSRF_CROSS_SITE"
    origin = headers.get("origin")
    if origin is not None:
        try:
            if canonical_origin(origin) == expected_origin:
                return None
        except ValueError:
            pass
        return "CSRF_ORIGIN"
    referer = headers.get("referer")
    if referer is not None:
        try:
            if canonical_origin(referer, allow_path=True) == expected_origin:
                return None
        except ValueError:
            pass
        return "CSRF_REFERER"
    return None


def _login_signature(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), b"autoucbot/login-csrf/v1\0" + payload.encode("ascii"), hashlib.sha256).hexdigest()


def new_login_csrf(secret: str, *, now: float | None = None) -> str:
    stamp = int(time.time() if now is None else now)
    payload = f"v1.{stamp}.{secrets.token_urlsafe(32)}"
    return payload + "." + _login_signature(payload, secret)


def login_csrf_error(token: str, secret: str, *, now: float | None = None) -> str | None:
    if not token:
        return "CSRF_COOKIE_MISSING"
    match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
    if not match:
        return "CSRF_COOKIE_INVALID"
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(_login_signature(payload, secret), signature):
        return "CSRF_COOKIE_INVALID"
    age = (time.time() if now is None else now) - int(match[1])
    if age < -60 or age >= LOGIN_CSRF_TTL:
        return "CSRF_EXPIRED"
    return None


def login_csrf_for_page(cookie: str, secret: str) -> tuple[str, int]:
    now = time.time()
    # Preserve a valid browser token across tabs, retries and back navigation.
    token = cookie if login_csrf_error(cookie, secret, now=now) is None else new_login_csrf(secret, now=now)
    remaining = max(1, LOGIN_CSRF_TTL - max(0, int(now) - int(token.split(".")[1])))
    return token, remaining


def csrf_equal(expected: str, supplied: str) -> bool:
    # User input can contain Unicode. compare_digest(str, str) only accepts
    # ASCII; compare bytes so a malformed token is a rejection, not a 500.
    return bool(expected and supplied and len(supplied) <= 256 and
                hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8")))
