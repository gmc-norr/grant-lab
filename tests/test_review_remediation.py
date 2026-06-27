"""Tests for review-remediation fixes 1-6."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from call_manager.main import create_app

# ── helpers ─────────────────────────────────────────────────────────────────


def _app(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        **extra,
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


# ── Fix 1: password field max_length=1024 ───────────────────────────────────


def test_login_oversized_password_422(pg_url, monkeypatch, pg_session_factory):
    """POST /api/auth/login with a 1025-char password must 422 before argon2."""
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post(
            "/api/auth/login",
            json={"email": "a@ki.se", "password": "x" * 1025},
        )
    assert r.status_code == 422


def test_login_max_password_accepted_at_validation(pg_url, monkeypatch, pg_session_factory):
    """A 1024-char password passes schema validation (may still fail auth)."""
    app = _app(monkeypatch, pg_url)
    with TestClient(app) as c:
        r = c.post(
            "/api/auth/login",
            json={"email": "a@ki.se", "password": "x" * 1024},
        )
    # 400 from login failure (not 422 from schema) = validation passed
    assert r.status_code == 400


def test_change_password_oversized_new_password_422(pg_url, monkeypatch):
    """POST /api/me/password with a 1025-char new_password must 422."""
    from call_manager.auth.passwords import hash_password
    from call_manager.db.models import User

    app = _app(monkeypatch, pg_url)
    with app.state.session_factory() as s:
        u = User(email="pw@ki.se", status="active", password_hash=hash_password("correct-horse-1"))
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        r = c.post(
            "/api/me/password",
            json={"current_password": "correct-horse-1", "new_password": "x" * 1025},
        )
    assert r.status_code == 422


def test_change_password_oversized_current_password_422(pg_url, monkeypatch):
    """POST /api/me/password with a 1025-char current_password must 422."""
    from call_manager.auth.passwords import hash_password
    from call_manager.db.models import User

    app = _app(monkeypatch, pg_url)
    with app.state.session_factory() as s:
        u = User(email="pw2@ki.se", status="active", password_hash=hash_password("correct-horse-1"))
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        r = c.post(
            "/api/me/password",
            json={"current_password": "x" * 1025, "new_password": "new-valid-pass-1"},
        )
    assert r.status_code == 422


# ── Fix 2: verify_password rehash ───────────────────────────────────────────


def test_verify_password_returns_tuple():
    """verify_password now returns (bool, bool)."""
    from call_manager.auth.passwords import hash_password, verify_password

    h = hash_password("correct horse battery")
    ok, needs_rehash = verify_password(h, "correct horse battery")
    assert ok is True
    assert isinstance(needs_rehash, bool)


def test_verify_password_wrong_returns_false_false():
    from call_manager.auth.passwords import hash_password, verify_password

    h = hash_password("correct horse battery")
    ok, needs_rehash = verify_password(h, "wrong-password-long")
    assert ok is False
    assert needs_rehash is False


def test_verify_password_none_returns_false_false():
    from call_manager.auth.passwords import verify_password

    ok, needs_rehash = verify_password(None, "anything-long-enough")
    assert ok is False
    assert needs_rehash is False


def test_login_rehashes_on_param_upgrade(pg_url, monkeypatch, pg_session_factory):
    """When verify_password signals needs_rehash=True, login persists a new hash."""
    from unittest.mock import patch

    import call_manager.web.api.auth as apiauth
    from call_manager.auth.passwords import hash_password
    from call_manager.db.models import User

    app = _app(monkeypatch, pg_url)
    original_hash = hash_password("correct-horse-1")
    with app.state.session_factory() as s:
        u = User(email="rh@ki.se", status="active", password_hash=original_hash)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id

    # Patch verify_password in the auth module to simulate a rehash-needed result.
    # We can't patch PasswordHasher.check_needs_rehash directly (C-extension attr
    # is read-only), so patch the public verify_password function instead.
    real_vp = apiauth.verify_password

    def _fake_vp(h, p):
        ok, _ = real_vp(h, p)
        return ok, True  # force needs_rehash=True

    with patch.object(apiauth, "verify_password", _fake_vp), TestClient(app) as c:
        r = c.post("/api/auth/login", json={"email": "rh@ki.se", "password": "correct-horse-1"})
    assert r.status_code == 200

    with app.state.session_factory() as s:
        u = s.get(User, uid)
        assert u.password_hash != original_hash  # hash was updated


# ── Fix 3: link-mode Google domain check ────────────────────────────────────


def test_connect_google_off_domain_rejected(pg_url, monkeypatch, pg_session_factory):
    """Link-mode callback with a Google email from outside ALLOWED_EMAIL_DOMAINS
    must redirect to /auth/error and NOT link the identity."""
    import time
    from urllib.parse import parse_qs, urlparse

    from authlib.jose import JsonWebKey, jwt

    from call_manager.db.models import User

    _KEY = JsonWebKey.generate_key("RSA", 2048, is_private=True)

    class FakeOIDC:
        def __init__(self):
            self._codes = {}

        def set_code(self, code, claims):
            self._codes[code] = claims

        def build_authorization_url(self, *, redirect_uri, state, nonce, code_challenge):
            return f"https://accounts.google.com/auth?state={state}&nonce={nonce}"

        def exchange_code(self, *, code, code_verifier, redirect_uri):
            return {
                "id_token": jwt.encode(
                    {"alg": "RS256", "kid": _KEY.kid}, self._codes[code], _KEY
                ).decode()
            }

        def validate_id_token(self, id_token, *, expected_nonce):
            claims = jwt.decode(id_token, {"keys": [_KEY.as_dict(is_private=False)]})
            claims.validate()
            return dict(claims)

    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        "GOOGLE_OIDC_ENABLED": "true",
        "GOOGLE_CLIENT_ID": "test-cid",
        "GOOGLE_CLIENT_SECRET": "secret",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    fake = FakeOIDC()
    app.state.google_oidc_client = fake

    with pg_session_factory() as s:
        u = User(email="link2@ki.se", status="active")
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id

    now = int(time.time())
    with TestClient(app) as c:
        c.cookies.set("session", app.state.session_signer.make(user_id=uid))
        start = c.post("/auth/google/start", data={"link": "1"}, follow_redirects=False)
        q = parse_qs(urlparse(start.headers["location"]).query)
        nonce = q["nonce"][0]
        state = q["state"][0]
        fake.set_code(
            "OFF",
            {
                "iss": "https://accounts.google.com",
                "aud": "test-cid",
                "sub": "off-domain-sub",
                "email": "personal@gmail.com",
                "email_verified": True,
                "name": "Off Domain",
                "iat": now,
                "exp": now + 600,
                "nonce": nonce,
            },
        )
        r = c.get(
            f"/auth/google/callback?code=OFF&state={state}",
            follow_redirects=False,
        )
        assert r.headers["location"] == "/auth/error"

    with pg_session_factory() as s:
        u = s.get(User, uid)
        assert u.oidc_sub is None  # identity NOT linked


# ── Fix 4: _send_error security headers ─────────────────────────────────────


def test_413_body_cap_has_nosniff_header(pg_url, monkeypatch, pg_session_factory):
    """Oversized body → 413; response must carry X-Content-Type-Options: nosniff."""
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1000")
    app = create_app()
    with TestClient(app) as c:
        r = c.post("/api/auth/login", content=b"x" * 2000)
    assert r.status_code == 413
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "same-origin"


# ── Fix 5: duplicate distinct Content-Length ─────────────────────────────────


def test_duplicate_distinct_content_length_rejected():
    """Two DIFFERENT Content-Length values → 400."""
    from call_manager.web.middleware import MaxBodySizeMiddleware

    async def _run():
        received = []

        async def app_stub(scope, receive, send):
            received.append(True)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = MaxBodySizeMiddleware(
            app_stub, max_bytes=1_000_000, csp="default-src 'self'", hsts=False
        )

        responses = []

        async def collect_send(msg):
            responses.append(msg)

        async def dummy_receive():
            return {"type": "http.request", "body": b""}

        scope = {
            "type": "http",
            "path": "/api/auth/login",
            "headers": [
                (b"content-length", b"10"),
                (b"content-length", b"20"),  # different value → should reject
            ],
        }
        await mw(scope, dummy_receive, collect_send)
        return responses, received

    responses, received = asyncio.run(_run())
    assert responses[0]["status"] == 400
    assert not received  # app_stub never ran


def test_duplicate_identical_content_length_passes():
    """Two IDENTICAL Content-Length values → passes through (RFC 7230 §3.3.2)."""
    from call_manager.web.middleware import MaxBodySizeMiddleware

    async def _run():
        received = []

        async def app_stub(scope, receive, send):
            received.append(True)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = MaxBodySizeMiddleware(
            app_stub, max_bytes=1_000_000, csp="default-src 'self'", hsts=False
        )

        responses = []

        async def collect_send(msg):
            responses.append(msg)

        async def dummy_receive():
            return {"type": "http.request", "body": b"hello"}

        scope = {
            "type": "http",
            "path": "/api/auth/login",
            "headers": [
                (b"content-length", b"5"),
                (b"content-length", b"5"),  # identical → OK
            ],
        }
        await mw(scope, dummy_receive, collect_send)
        return responses, received

    responses, received = asyncio.run(_run())
    assert responses[0]["status"] == 200
    assert received  # app_stub ran


# ── Fix 6: EMAIL_FROM validation in prod ─────────────────────────────────────


def test_transport_configured_rejects_example_com_email_from(monkeypatch):
    """With a real send transport (Resend) configured, the placeholder
    no-reply@example.com EMAIL_FROM must raise ValueError — every send would
    bounce from an IANA-controlled domain."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://app.ki.se")
    monkeypatch.setenv("RESEND_API_KEY", "re_live_key")
    # EMAIL_FROM left at default (no-reply@example.com) → must raise
    from call_manager.config import Settings

    with pytest.raises(ValueError, match="example.com"):
        Settings()


def test_prod_without_transport_allows_default_email_from(monkeypatch):
    """A prod deploy (https) with NO send transport is valid — it falls back to
    NoOpEmailSender — so the placeholder EMAIL_FROM must NOT block startup."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://app.ki.se")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    from call_manager.config import Settings

    s = Settings()
    assert "example.com" in s.email_from


def test_https_real_email_from_ok(monkeypatch):
    """In prod (https), a real EMAIL_FROM is accepted."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://app.ki.se")
    monkeypatch.setenv("EMAIL_FROM", "noreply@ki.se")
    from call_manager.config import Settings

    s = Settings()
    assert s.email_from == "noreply@ki.se"


def test_http_default_email_from_ok(monkeypatch):
    """In dev (http), the default example.com EMAIL_FROM is allowed."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    from call_manager.config import Settings

    s = Settings()
    assert "example.com" in s.email_from


# ── Gap-fix: EMBEDDING_DIM ↔ Vector(1024) startup assertion ──────────────────


def test_create_app_rejects_mismatched_embedding_dim(monkeypatch):
    """EMBEDDING_DIM != the hard-coded Vector(1024) column width must fail FAST
    at create_app() with a clear message — not late at the first INSERT."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("EMBEDDING_DIM", "768")
    from call_manager.main import create_app

    with pytest.raises(RuntimeError, match="EMBEDDING_DIM"):
        create_app()


def test_create_app_accepts_default_embedding_dim(monkeypatch):
    """The default EMBEDDING_DIM (1024) matches the column width → no assertion."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.delenv("EMBEDDING_DIM", raising=False)
    from call_manager.main import create_app

    app = create_app()
    try:
        assert app is not None
    finally:
        # App built without entering lifespan → dispose the lazy engine's pool
        # so the never-used connection doesn't surface a GC ResourceWarning.
        app.state.engine.dispose()
