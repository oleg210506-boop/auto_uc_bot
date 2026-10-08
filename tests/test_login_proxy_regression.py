"""Regression tests for Railway login. No real credentials or external calls."""
from __future__ import annotations

from dataclasses import replace
import runpy
import time

from bs4 import BeautifulSoup
from fastapi import Request
from fastapi.testclient import TestClient
import pytest

from autoucbot import __version__
from autoucbot.config import Config
from autoucbot.web import create_app
from autoucbot.web_security import (
    LOGIN_CSRF_TTL, canonical_origin, csrf_equal, login_csrf_error,
    new_login_csrf, source_error,
)

PUBLIC = "https://autoucbot-production.up.railway.app"
PASSWORD = "Test-password-12!"


def csrf(response):
    return BeautifulSoup(response.text, "html.parser").find("input", {"name": "csrf"})["value"]


def form(token, password=PASSWORD):
    return {"csrf": token, "username": "owner", "password": password}


class InternalHTTP:
    """Model TLS termination; the client sees HTTPS, the ASGI server HTTP."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "scheme": "http", "server": ("0.0.0.0", 8080),
                     "client": ("10.23.0.5", 12345)}
        await self.app(scope, receive, send)


@pytest.fixture
def deployed(config):
    return create_app(replace(config, public_url=PUBLIC, secure_cookie=True, forwarded_allow_ips="*"))


@pytest.mark.parametrize("raw,expected", [
    ("https://EXAMPLE.COM/", "https://example.com"),
    (" HTTPS://Example.com:443/ ", "https://example.com"),
    ("http://EXAMPLE.COM:80", "http://example.com"),
    ("https://example.com:8443", "https://example.com:8443"),
    ("https://[::1]:443", "https://[::1]"),
    ("https://[0:0:0:0:0:0:0:1]:8443/", "https://[::1]:8443"),
    ("http://localhost:8080", "http://localhost:8080"),
    (PUBLIC, PUBLIC),
])
def test_origin_normalization(raw, expected):
    assert canonical_origin(raw) == expected


@pytest.mark.parametrize("raw", [
    "null", "", "//example.com", "https://", "https://user@example.com",
    "https://user:pass@example.com", "https://example.com/login", "https://example.com/?x=1",
    "https://example.com#fragment", "https://example.com?", "https://example.com#",
    "https://example.com:garbage", "https://example.com:65536", "https://example.com:0",
    "https://example.com:", "https://example.com\\@evil.test", "https://ex ample.com",
    "https://example.com\r\n", "https://example.com,https://evil.test", "file:///login.html",
])
def test_reject_ambiguous_origin(raw):
    with pytest.raises(ValueError):
        canonical_origin(raw)


def test_referrer_extracts_origin_only():
    assert canonical_origin(PUBLIC + "/login?notice=example", allow_path=True) == PUBLIC


def test_config_normalizes_public_url(config):
    cfg = replace(config, public_url=PUBLIC.upper() + ":443/")
    assert cfg.public_url == PUBLIC


@pytest.mark.parametrize("value", ["http://example.com", "https://example.com/login", "https://user@example.com"])
def test_config_rejects_bad_public_url(config, value):
    with pytest.raises(ValueError):
        replace(config, public_url=value)


def test_env_railway_proxy_default(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_SECRET", "only-a-local-test-secret-" * 3)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "local-simulated-railway")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    monkeypatch.setenv("PUBLIC_URL", " " + PUBLIC.upper() + ":443/ ")
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    config = Config.from_env()
    assert config.public_url == PUBLIC and config.forwarded_allow_ips == "*"


def test_env_non_railway_trusts_loopback_only(monkeypatch):
    monkeypatch.setenv("APP_SECRET", "only-a-local-test-secret-" * 3)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    assert Config.from_env().forwarded_allow_ips == "127.0.0.1,::1"


def test_proxy_trust_can_be_restricted(monkeypatch):
    monkeypatch.setenv("APP_SECRET", "only-a-local-test-secret-" * 3)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/8,127.0.0.1")
    assert Config.from_env().forwarded_allow_ips == "10.0.0.0/8,127.0.0.1"


def test_https_login_through_http_upstream(deployed):
    with TestClient(InternalHTTP(deployed), base_url=PUBLIC, follow_redirects=False) as c:
        token = csrf(c.get("/login", headers={"X-Forwarded-Proto": "https"}))
        r = c.post("/login", data=form(token), headers={"Origin": PUBLIC, "X-Forwarded-Proto": "https"})
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert c.get("/", headers={"X-Forwarded-Proto": "https"}).status_code == 200
        cookie = r.headers.get_list("set-cookie")[0]
        assert "Secure" in cookie and "HttpOnly" in cookie
        assert deployed.state.db.one("SELECT COUNT(*) n FROM sessions")["n"] == 1


def test_public_url_wins_even_without_forwarded_headers(deployed):
    with TestClient(InternalHTTP(deployed), base_url=PUBLIC, follow_redirects=False) as c:
        token = csrf(c.get("/login"))
        r = c.post("/login", data=form(token), headers={"Origin": PUBLIC, "Host": "0.0.0.0:8080"})
        assert r.status_code == 303


def test_proxy_scheme_is_interpreted_inside_app(deployed):
    @deployed.get("/_test_scheme")
    async def probe(request: Request):
        return {"scheme": request.url.scheme, "client": request.client.host}
    with TestClient(InternalHTTP(deployed), base_url=PUBLIC, follow_redirects=False) as c:
        token = csrf(c.get("/login"))
        assert c.post("/login", data=form(token), headers={"Origin": PUBLIC}).status_code == 303
        r = c.get("/_test_scheme", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.42"})
        assert r.json() == {"scheme": "https", "client": "192.0.2.42"}


def test_untrusted_peer_cannot_change_scheme(config):
    app = create_app(replace(config, public_url=PUBLIC, secure_cookie=True, forwarded_allow_ips="127.0.0.1"))
    @app.get("/_test_scheme")
    async def probe(request: Request):
        return {"scheme": request.url.scheme, "client": request.client.host}
    with TestClient(InternalHTTP(app), base_url=PUBLIC, follow_redirects=False) as c:
        token = csrf(c.get("/login"))
        assert c.post("/login", data=form(token), headers={"Origin": PUBLIC}).status_code == 303
        r = c.get("/_test_scheme", headers={"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.42"})
        assert r.json() == {"scheme": "http", "client": "10.23.0.5"}


def test_two_tabs_do_not_invalidate_first_form(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        first = c.get("/login")
        second = c.get("/login")
        third = c.get("/login")
        assert csrf(first) == csrf(second) == csrf(third)
        assert c.post("/login", data=form(csrf(first)), headers={"Origin": PUBLIC}).status_code == 303


def test_refresh_does_not_invalidate_token_after_15_minutes(deployed, monkeypatch):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        old_time = time.time() - 1200
        token = new_login_csrf(deployed.state.engine.config.secret, now=old_time)
        c.cookies.set("auc_login_csrf", token, domain="autoucbot-production.up.railway.app", path="/")
        assert csrf(c.get("/login")) == token
        assert c.post("/login", data=form(token), headers={"Origin": PUBLIC}).status_code == 303


def test_get_sets_safe_browser_headers_and_cookie(deployed):
    with TestClient(deployed, base_url=PUBLIC) as c:
        r = c.get("/login")
        assert r.headers["referrer-policy"] == "same-origin"
        assert "no-store" in r.headers["cache-control"]
        assert r.headers["pragma"] == "no-cache"
        assert 'name="referrer" content="same-origin"' in r.text
        cookie = r.headers["set-cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"):
            assert attribute in cookie
        assert "Domain=" not in cookie
        assert __version__ in r.text


@pytest.mark.parametrize("origin", [PUBLIC.upper(), PUBLIC + ":443", PUBLIC + "/"])
def test_equivalent_origin_accepted(deployed, origin):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        assert c.post("/login", data=form(csrf(c.get("/login"))), headers={"Origin": origin}).status_code == 303


@pytest.mark.parametrize("origin", [
    "null", "https://evil.test", "http://autoucbot-production.up.railway.app",
    PUBLIC + ".evil.test", PUBLIC + ":8443", PUBLIC + "/login",
])
def test_foreign_opaque_or_malformed_origin_rejected(deployed, origin):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        r = c.post("/login", data=form(csrf(c.get("/login"))), headers={"Origin": origin, "X-Forwarded-Host": "evil.test"})
        assert r.status_code == 403 and "CSRF_ORIGIN" in r.text
        assert deployed.state.db.one("SELECT COUNT(*) n FROM sessions")["n"] == 0
        assert r.headers["referrer-policy"] == "same-origin"
        assert c.post("/login", data=form(csrf(r)), headers={"Origin": PUBLIC}).status_code == 303


def test_missing_origin_uses_referrer(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        r = c.post("/login", data=form(csrf(c.get("/login"))), headers={"Referer": PUBLIC + "/login?notice=test"})
        assert r.status_code == 303


def test_foreign_referrer_rejected(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        r = c.post("/login", data=form(csrf(c.get("/login"))), headers={"Referer": "https://evil.test/login"})
        assert r.status_code == 403 and "CSRF_REFERER" in r.text


def test_explicit_bad_origin_not_overridden_by_good_referrer(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        r = c.post("/login", data=form(csrf(c.get("/login"))), headers={"Origin": "null", "Referer": PUBLIC + "/login"})
        assert r.status_code == 403


def test_cross_site_fetch_rejected_even_with_token(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        r = c.post("/login", data=form(csrf(c.get("/login"))), headers={"Origin": PUBLIC, "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403 and "CSRF_CROSS_SITE" in r.text


def test_missing_cookie_gives_recoverable_form(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        first = c.get("/login")
        c.cookies.clear()
        r = c.post("/login", data=form(csrf(first)), headers={"Origin": PUBLIC})
        assert r.status_code == 403 and "CSRF_COOKIE_MISSING" in r.text
        assert PASSWORD not in r.text
        assert c.post("/login", data=form(csrf(r)), headers={"Origin": PUBLIC}).status_code == 303


def test_expired_signed_cookie_recovers(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        token = new_login_csrf(deployed.state.engine.config.secret, now=time.time() - LOGIN_CSRF_TTL - 1)
        c.cookies.set("auc_login_csrf", token, domain="autoucbot-production.up.railway.app", path="/")
        r = c.post("/login", data=form(token), headers={"Origin": PUBLIC})
        assert r.status_code == 403 and "CSRF_EXPIRED" in r.text and csrf(r) != token
        assert c.post("/login", data=form(csrf(r)), headers={"Origin": PUBLIC}).status_code == 303


def test_legacy_cookie_migrates_on_get_without_database_change(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        c.cookies.set("auc_login_csrf", "old-unsigned-token", domain="autoucbot-production.up.railway.app", path="/")
        r = c.get("/login")
        assert csrf(r).startswith("v1.")
        assert c.post("/login", data=form(csrf(r)), headers={"Origin": PUBLIC}).status_code == 303


def test_legacy_post_recovers_without_clear_all_cookies(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        c.cookies.set("auc_login_csrf", "old-unsigned-token", domain="autoucbot-production.up.railway.app", path="/")
        r = c.post("/login", data=form("old-unsigned-token"), headers={"Origin": PUBLIC})
        assert r.status_code == 403 and "CSRF_COOKIE_INVALID" in r.text
        assert c.post("/login", data=form(csrf(r)), headers={"Origin": PUBLIC}).status_code == 303


@pytest.mark.parametrize("bad_token", ["", "incorrect", "юникод", "x" * 300])
def test_bad_form_token_rejected_not_500(deployed, bad_token):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        c.get("/login")
        assert c.post("/login", data=form(bad_token), headers={"Origin": PUBLIC}).status_code == 403


def test_duplicate_csrf_fields_rejected(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        token = csrf(c.get("/login"))
        r = c.post("/login", content=f"csrf={token}&csrf={token}&username=owner&password={PASSWORD}",
                   headers={"Origin": PUBLIC, "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 403


def test_wrong_password_keeps_usable_login_form(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        token = csrf(c.get("/login"))
        r = c.post("/login", data=form(token, "wrong"), headers={"Origin": PUBLIC})
        assert r.status_code == 400 and csrf(r) == token and "LOGIN_REJECTED" in r.text
        assert c.post("/login", data=form(csrf(r)), headers={"Origin": PUBLIC}).status_code == 303


def test_failure_log_does_not_contain_credentials_or_tokens(deployed, caplog):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as c:
        token = csrf(c.get("/login"))
        c.post("/login", data=form(token + "bad"), headers={"Origin": PUBLIC})
        assert "CSRF_TOKEN_MISMATCH" in caplog.text
        assert token not in caplog.text and PASSWORD not in caplog.text


def test_authenticated_forms_still_require_csrf_and_origin(deployed):
    with TestClient(InternalHTTP(deployed), base_url=PUBLIC, follow_redirects=False) as c:
        assert c.post("/login", data=form(csrf(c.get("/login"))), headers={"Origin": PUBLIC}).status_code == 303
        token = csrf(c.get("/"))
        assert c.post("/control", data={"action": "pause"}, headers={"Origin": PUBLIC}).status_code == 403
        r = c.post("/control", data={"csrf": token, "action": "pause"}, headers={"Origin": "https://evil.test"})
        assert r.status_code == 403 and "no-store" in r.headers["cache-control"]
        assert c.post("/control", data={"csrf": "юникод", "action": "pause"}, headers={"Origin": PUBLIC}).status_code == 403
        assert c.post("/control", data={"csrf": token, "action": "pause"}, headers={"Origin": PUBLIC}).status_code == 303


def test_health_reports_build_without_secrets(deployed):
    with TestClient(deployed, base_url=PUBLIC) as c:
        assert c.get("/healthz").json() == {"status": "ok", "version": __version__}
        assert deployed.state.engine.config.secret not in c.get("/healthz").text


def test_entrypoint_does_not_apply_forwarded_headers_twice(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_SECRET", "module-test-only-secret-" * 3)
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("ADMIN_USERNAME", "owner")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("START_WORKER", "false")
    monkeypatch.setenv("PUBLIC_URL", PUBLIC)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("RESTORE_BACKUP_FILE", raising=False)
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(kwargs))
    runpy.run_module("autoucbot", run_name="__main__")
    assert captured["proxy_headers"] is False and captured["workers"] == 1


def test_login_token_signature_expiry_and_clock_skew(config):
    token = new_login_csrf(config.secret, now=1000)
    assert login_csrf_error(token, config.secret, now=1000) is None
    assert login_csrf_error(token, config.secret, now=999) is None
    assert login_csrf_error(token, config.secret, now=939) == "CSRF_EXPIRED"
    assert login_csrf_error(token, config.secret, now=1000 + LOGIN_CSRF_TTL) == "CSRF_EXPIRED"
    assert login_csrf_error(token, "wrong-secret", now=1000) == "CSRF_COOKIE_INVALID"
    assert not csrf_equal(token, "юникод")
    assert csrf_equal(token, token)


def test_login_cookie_and_session_isolation(deployed):
    with TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as a, TestClient(deployed, base_url=PUBLIC, follow_redirects=False) as b:
        ta = csrf(a.get("/login")); tb = csrf(b.get("/login"))
        assert ta != tb
        assert b.post("/login", data=form(ta), headers={"Origin": PUBLIC}).status_code == 403
        assert a.post("/login", data=form(ta), headers={"Origin": PUBLIC}).status_code == 303
        assert b.post("/login", data=form(tb), headers={"Origin": PUBLIC}).status_code == 303
        assert a.cookies.get("auc_session") != b.cookies.get("auc_session")
        assert len(deployed.state.db.rows("SELECT * FROM sessions")) == 2
