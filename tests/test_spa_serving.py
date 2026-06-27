import pytest
from fastapi.testclient import TestClient

from call_manager.config import Settings
from call_manager.main import create_app


def test_spa_mode_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("SPA_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "disabled"


def test_spa_mode_from_env(monkeypatch):
    monkeypatch.setenv("SPA_MODE", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "prod"


def _env(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        **extra,
    }.items():
        monkeypatch.setenv(k, v)


def test_disabled_mode_serves_no_spa(monkeypatch, pg_url, pg_session_factory):
    # The SPA route is `/` (Plan 3 Task 9 flip). In disabled mode nothing serves
    # it, and with the Jinja calls_index handler retired there is no `GET /`
    # handler either — so the root is a real 404 (no SPA shell, no dashboard).
    _env(monkeypatch, pg_url, SPA_MODE="disabled")
    c = TestClient(create_app())
    assert c.get("/").status_code == 404


def test_prod_mode_missing_build_fails_fast(monkeypatch, pg_url, pg_session_factory, tmp_path):
    import call_manager.web.spa_serving as spa_serving

    monkeypatch.setattr(spa_serving, "SPA_DIR", tmp_path / "spa")
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    with pytest.raises(RuntimeError, match="SPA build missing"):
        create_app()


def test_prod_mode_serves_index_for_spa_routes(monkeypatch, pg_url, pg_session_factory, tmp_path):
    import call_manager.web.spa_serving as spa_serving

    spa_dir = tmp_path / "spa"
    (spa_dir / "assets").mkdir(parents=True)
    (spa_dir / "assets" / "main.js").write_text("console.log('hi');")
    (spa_dir / "index.html").write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    c = TestClient(create_app())
    # The SPA route is `/` (Plan 3 Task 9 flip): the catch-all serves index.html
    # for the root, while unknown paths stay real 404s.
    assert "<title>spa</title>" in c.get("/").text
    assert c.get("/api/does-not-exist").status_code == 404
    assert c.get("/totally-unknown").status_code == 404
    # /assets is served by StaticFiles and wins over the catch-all; a missing
    # asset 404s and is NOT masked by the SPA shell.
    asset = c.get("/assets/main.js")
    assert asset.status_code == 200
    assert asset.text == "console.log('hi');"
    missing = c.get("/assets/missing.js")
    assert missing.status_code == 404
    assert "<title>spa</title>" not in missing.text
    # HEAD on a servable SPA route returns 200 (empty body), not 405.
    assert c.head("/").status_code == 200
    assert c.head("/totally-unknown").status_code == 404
    # An UNKNOWN/typo path is a real 404 for EVERY method (not 405) — the
    # catch-all owns all methods but only matches SPA routes, so a non-existent
    # path never path-matches into a misleading 405 + Allow header. Matches the
    # SPA_MODE=disabled behavior and the Plan-2 "unknown -> real 404" contract.
    assert c.post("/totally-unknown").status_code == 404
    assert c.request("DELETE", "/totally-unknown").status_code == 404
    assert c.post("/api/does-not-exist").status_code == 404
    # A real SPA route exists but only serves the document on GET/HEAD: other
    # methods get a 405 with a correct Allow header.
    post_root = c.post("/")
    assert post_root.status_code == 405
    assert post_root.headers["allow"] == "GET, HEAD"
    # A REAL backend route hit with the WRONG method returns a correct 405 (not a
    # 404 masked by the all-methods catch-all): /healthz is GET-only, so POST gets
    # 405 with GET in Allow — matching the native 405 that dev/disabled return.
    wrong_method = c.post("/healthz")
    assert wrong_method.status_code == 405
    assert "GET" in wrong_method.headers.get("allow", "")
    # The internal SPA catch-all must not leak into the OpenAPI schema.
    assert "/{full_path}" not in c.get("/openapi.json").json()["paths"]
    # PROD must keep the STRICT CSP — the relaxed dev policy must NOT leak to
    # prod. This exercises the prod branch of the
    # build_csp(dev=settings.spa_mode == "dev") wiring (the security-critical
    # gate); a regression flipping it would relax script-src/connect-src here.
    prod_csp = c.get("/").headers["content-security-policy"]
    assert "'unsafe-inline'" not in prod_csp.split("script-src")[1].split(";")[0]
    assert "ws:" not in prod_csp


def test_dev_mode_relaxes_csp(monkeypatch, pg_url, pg_session_factory):
    _env(monkeypatch, pg_url, SPA_MODE="dev")
    c = TestClient(create_app())
    csp = c.get("/healthz").headers.get("content-security-policy", "")
    # Dev must allow BOTH Vite relaxations: the HMR WebSocket (connect-src ws:)
    # and the injected inline dev bootstrap (script-src 'unsafe-inline').
    assert "ws:" in csp or "ws://localhost" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp


def test_disabled_mode_keeps_strict_script_src(monkeypatch, pg_url, pg_session_factory):
    _env(monkeypatch, pg_url, SPA_MODE="disabled")
    c = TestClient(create_app())
    csp = c.get("/healthz").headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp and "script-src 'self' 'unsafe-inline'" not in csp
