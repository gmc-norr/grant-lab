"""Security-response headers + request-body-size cap (internet-exposure hardening)."""

from fastapi.testclient import TestClient

from call_manager.main import create_app


def _set_http_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def _set_https_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "https://app.ki.se")
    # https deploy requires a real EMAIL_FROM (example.com placeholder is rejected).
    monkeypatch.setenv("EMAIL_FROM", "noreply@ki.se")
    # https deploy requires a real email transport (else create_app refuses).
    monkeypatch.setenv("RESEND_API_KEY", "re_secret")


def test_security_headers_present_on_response(migrated_engine, pg_url, monkeypatch):
    _set_http_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    # same-origin (not no-referrer): no-referrer would null the Origin
    # header on browser form POSTs and trip the CSRF middleware.
    assert r.headers["referrer-policy"] == "same-origin"
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


def test_hsts_absent_over_http(migrated_engine, pg_url, monkeypatch):
    _set_http_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert "strict-transport-security" not in r.headers


def test_hsts_present_over_https(migrated_engine, pg_url, monkeypatch):
    _set_https_env(monkeypatch, pg_url)
    app = create_app()
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert "max-age=" in r.headers["strict-transport-security"]


def test_oversized_body_rejected_with_413(migrated_engine, pg_url, monkeypatch):
    _set_http_env(monkeypatch, pg_url)
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1000")
    app = create_app()
    with TestClient(app) as client:
        # 2 KB body to a POST endpoint — rejected by the outermost middleware
        # before routing / CSRF / the handler ever runs.
        r = client.post("/api/auth/login", content=b"x" * 2000)
    assert r.status_code == 413


def test_normal_body_passes_under_cap(pg_session_factory, pg_url, monkeypatch):
    # Probe the body-size cap via POST /api/auth/login. A normal-sized JSON body
    # reaches the handler (unknown email → generic 400); the point is it was NOT
    # rejected by the size cap. (pg_session_factory kept for DB setup.)
    _set_http_env(monkeypatch, pg_url)
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1000000")
    app = create_app()
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/login",
            json={"email": "bodycap-probe@ki.se", "password": "x" * 16},
        )
    # Login failure is a 400 (enumeration-safe); the point is it was NOT rejected by the size cap.
    assert r.status_code == 400
