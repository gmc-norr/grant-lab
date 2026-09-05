"""Security-response headers + request-body-size cap (internet-exposure hardening)."""

import pytest
from fastapi.testclient import TestClient

from grant_lab.main import create_app


def _connect_src_directive(csp: str) -> str:
    """Extract the full `connect-src ...` directive (prefix included) from a
    CSP header string. Used instead of a bare substring check so a match
    can't be fooled by junk appended after the expected token (e.g. an
    injected `; script-src * 'unsafe-inline'`)."""
    return next(d.strip() for d in csp.split("; ") if d.startswith("connect-src"))


def _set_http_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")


def _set_https_env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "prod-grade-secret-with-enough-entropy-xyz123")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "https://app.umu.se")
    # https deploy requires a real EMAIL_FROM (example.com placeholder is rejected).
    monkeypatch.setenv("EMAIL_FROM", "noreply@umu.se")
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
    # Permissions-Policy: powerful browser features the app never uses are
    # explicitly disabled (defense-in-depth against a compromised sub-resource).
    pp = r.headers["permissions-policy"]
    assert "camera=()" in pp
    assert "geolocation=()" in pp
    assert "microphone=()" in pp


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
    # The early-reject response must carry the SAME security headers every other
    # response does — including CSP (previously omitted on this path).
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "same-origin"
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "camera=()" in r.headers["permissions-policy"]
    # http deploy → no HSTS even on the early-reject path.
    assert "strict-transport-security" not in r.headers


def test_413_early_reject_carries_csp_and_hsts_over_https(migrated_engine, pg_url, monkeypatch):
    """In prod (https), the body-cap 413 must also carry HSTS — matching every
    other response. Regression guard for the early-reject CSP/HSTS omission."""
    _set_https_env(monkeypatch, pg_url)
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1000")
    app = create_app()
    with TestClient(app) as client:
        r = client.post("/api/auth/login", content=b"x" * 2000)
    assert r.status_code == 413
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "max-age=" in r.headers["strict-transport-security"]


def test_build_csp_prod_has_same_origin_ws_token():
    from grant_lab.web.middleware import build_csp

    csp = build_csp(dev=False, ws_origin="wss://grants.example.org")
    assert _connect_src_directive(csp) == "connect-src 'self' wss://grants.example.org"
    assert "ws:" not in csp.replace("wss://grants.example.org", "")  # narrow, no bare scheme
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]


def test_build_csp_http_base_gets_ws_token():
    from grant_lab.web.middleware import build_csp

    csp = build_csp(ws_origin="ws://lanhost:8000")
    assert _connect_src_directive(csp) == "connect-src 'self' ws://lanhost:8000"


def test_build_csp_dev_output_unchanged():
    from grant_lab.web.middleware import build_csp

    assert "connect-src 'self' ws: http://localhost:5173" in build_csp(dev=True)
    # dev ignores ws_origin (its broad ws: token already covers the Yjs socket)
    assert build_csp(dev=True) == build_csp(dev=True, ws_origin="wss://x")
    # Pinned golden: byte-for-byte dev CSP, captured pre-3b-5d.D4 review-#10 fix
    # via `uv run python -c "from grant_lab.web.middleware import build_csp;
    # print(repr(build_csp(dev=True)))"`. dev=True must NEVER change shape as a
    # side effect of the ws_origin-validation fix at the derivation site.
    assert build_csp(dev=True) == (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self' ws: http://localhost:5173; "
        "form-action 'self'; base-uri 'self'; frame-ancestors 'none'; "
        "object-src 'none'"
    )


def test_build_csp_no_origin_stays_strict():
    from grant_lab.web.middleware import build_csp

    assert "connect-src 'self';" in build_csp() + ";"


# ---------------------------------------------------------------------------
# 3b-5d.D4 review #10 (Critical #1): CSP directive injection via an
# unvalidated APP_BASE_URL netloc. `urlsplit().netloc` only stops at `/`,
# `?` or `#` — not `;` or whitespace — and config.py's APP_BASE_URL
# validator is a bare scheme-prefix check, so a value like
# "https://app.umu.se; script-src * 'unsafe-inline'" used to sail through to
# the derived ws_origin and get interpolated verbatim into the CSP header.
# _derive_ws_origin (extracted from create_app for exactly this) must now
# reject any netloc that isn't a plain host[:port].
# ---------------------------------------------------------------------------


def test_ws_origin_injection_rejected_at_startup():
    from grant_lab.main import _derive_ws_origin

    with pytest.raises(RuntimeError):
        _derive_ws_origin("https://app.umu.se; script-src * 'unsafe-inline'")


def test_ws_origin_whitespace_netloc_rejected_at_startup():
    from grant_lab.main import _derive_ws_origin

    with pytest.raises(RuntimeError):
        _derive_ws_origin("https://   ")


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
            json={"email": "bodycap-probe@umu.se", "password": "x" * 16},
        )
    # Login failure is a 400 (enumeration-safe); the point is it was NOT rejected by the size cap.
    assert r.status_code == 400
