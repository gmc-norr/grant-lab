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


def test_build_csp_dev_output_unchanged():
    from grant_lab.web.middleware import build_csp

    assert "connect-src 'self' ws: http://localhost:5173" in build_csp(dev=True)
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
# 3b-5d.D4 review #10 (Critical #1): `urlsplit().netloc` only stops at `/`,
# `?` or `#` — not `;` or whitespace — and config.py's APP_BASE_URL
# validator is a bare scheme-prefix check, so a value like
# "https://app.umu.se; script-src * 'unsafe-inline'" used to sail through.
# _validate_app_base_url (called from create_app) must reject any netloc that
# isn't a plain host[:port].
# ---------------------------------------------------------------------------


def test_app_base_url_injection_rejected_at_startup():
    from grant_lab.main import _validate_app_base_url

    with pytest.raises(RuntimeError):
        _validate_app_base_url("https://app.umu.se; script-src * 'unsafe-inline'")


def test_app_base_url_whitespace_netloc_rejected_at_startup():
    from grant_lab.main import _validate_app_base_url

    with pytest.raises(RuntimeError):
        _validate_app_base_url("https://   ")


def test_create_app_refuses_a_malformed_app_base_url(migrated_engine, pg_url, monkeypatch):
    # The wiring, not just the function: create_app must call the check.
    _set_http_env(monkeypatch, pg_url)
    monkeypatch.setenv("APP_BASE_URL", "http://app.umu.se; script-src * 'unsafe-inline'")
    with pytest.raises(RuntimeError, match="APP_BASE_URL host"):
        create_app()


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


# ---------------------------------------------------------------------------
# Security review 2026-10-31 S7: the login page submits a native form to
# POST /auth/google/start, which 303s to the identity provider. Chromium
# applies the DOCUMENT's form-action to that redirect, so `form-action 'self'`
# blocked Google sign-in outright. When Google sign-in is on, the policy
# admits exactly the provider's origin -- and nothing when it is off.
# ---------------------------------------------------------------------------


def _form_action_directive(csp: str) -> str:
    return next(d.strip() for d in csp.split("; ") if d.startswith("form-action"))


def _set_google_env(monkeypatch, database_url, **extra):
    _set_http_env(monkeypatch, database_url)
    monkeypatch.setenv("GOOGLE_OIDC_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_build_csp_form_action_admits_exactly_the_given_origin():
    from grant_lab.web.middleware import build_csp

    csp = build_csp(form_action_origin="https://accounts.google.com")
    assert _form_action_directive(csp) == "form-action 'self' https://accounts.google.com"
    assert _form_action_directive(build_csp()) == "form-action 'self'"


def test_google_sign_in_on_admits_googles_origin_as_a_form_target(
    migrated_engine, pg_url, monkeypatch
):
    _set_google_env(monkeypatch, pg_url)
    with TestClient(create_app()) as client:
        csp = client.get("/healthz").headers["content-security-policy"]
    assert _form_action_directive(csp) == "form-action 'self' https://accounts.google.com"


def test_the_form_target_follows_a_configured_discovery_url(migrated_engine, pg_url, monkeypatch):
    _set_google_env(
        monkeypatch,
        pg_url,
        GOOGLE_OIDC_DISCOVERY_URL="https://idp.umu.se/realms/x/.well-known/openid-configuration",
    )
    with TestClient(create_app()) as client:
        csp = client.get("/healthz").headers["content-security-policy"]
    assert _form_action_directive(csp) == "form-action 'self' https://idp.umu.se"


def test_google_sign_in_off_keeps_form_action_self(migrated_engine, pg_url, monkeypatch):
    _set_http_env(monkeypatch, pg_url)
    with TestClient(create_app()) as client:
        csp = client.get("/healthz").headers["content-security-policy"]
    assert _form_action_directive(csp) == "form-action 'self'"


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.google.com; script-src * 'unsafe-inline'/.well-known/x",
        "https://   /.well-known/x",
        "javascript:alert(1)",
        "accounts.google.com/.well-known/x",
    ],
)
def test_a_malformed_discovery_url_is_refused_at_startup(url):
    from grant_lab.main import _derive_form_action_origin

    with pytest.raises(RuntimeError):
        _derive_form_action_origin(url)
