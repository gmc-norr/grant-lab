import pytest

from grant_lab.config import Settings
from grant_lab.main import create_app


def test_spa_mode_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("SPA_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "disabled"


def test_spa_mode_from_env(monkeypatch):
    monkeypatch.setenv("SPA_MODE", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "prod"


def _env(monkeypatch, pg_url, **extra):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
        **extra,
    }.items():
        monkeypatch.setenv(k, v)


def test_disabled_mode_serves_no_spa(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    # The SPA route is `/` (Plan 3 Task 9 flip). In disabled mode nothing serves
    # it, and with the Jinja calls_index handler retired there is no `GET /`
    # handler either — so the root is a real 404 (no SPA shell, no dashboard).
    _env(monkeypatch, pg_url, SPA_MODE="disabled")
    c = test_client_factory(create_app())
    assert c.get("/").status_code == 404


def test_prod_mode_missing_build_fails_fast(monkeypatch, pg_url, pg_session_factory, tmp_path):
    import grant_lab.web.spa_serving as spa_serving

    monkeypatch.setattr(spa_serving, "SPA_DIR", tmp_path / "spa")
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    with pytest.raises(RuntimeError, match="SPA build missing"):
        create_app()


def test_prod_mode_serves_index_for_spa_routes(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    import grant_lab.web.spa_serving as spa_serving

    spa_dir = tmp_path / "spa"
    (spa_dir / "assets").mkdir(parents=True)
    (spa_dir / "assets" / "main.js").write_text("console.log('hi');")
    (spa_dir / "index.html").write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    c = test_client_factory(create_app())
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
    # The raw schema route is gated off in non-dev modes (no unauth API map);
    # but the internal SPA catch-all must still never leak into the in-process
    # schema (checked via app.openapi(), which is mode-independent).
    assert c.get("/openapi.json").status_code == 404
    assert "/{full_path}" not in c.app.openapi()["paths"]
    # PROD must keep the STRICT CSP — the relaxed dev policy must NOT leak to
    # prod. This exercises the prod branch of the
    # build_csp(dev=settings.spa_mode == "dev") wiring (the security-critical
    # gate); a regression flipping it would relax script-src/connect-src here.
    prod_csp = c.get("/").headers["content-security-policy"]
    assert "'unsafe-inline'" not in prod_csp.split("script-src")[1].split(";")[0]
    # 3b-5d.D4: prod's connect-src now carries a narrow same-origin WS token
    # (derived from APP_BASE_URL, here "http://testserver" -> "ws://testserver")
    # for the Yjs socket — but must NOT carry dev's broad, bare `ws:` scheme
    # (which would allow a WebSocket to ANY origin).
    connect_src = prod_csp.split("connect-src")[1].split(";")[0]
    assert connect_src.strip() == "'self' ws://testserver"
    assert "ws:" not in connect_src.replace("ws://testserver", "")


def test_dev_mode_relaxes_csp(monkeypatch, pg_url, pg_session_factory, test_client_factory):
    _env(monkeypatch, pg_url, SPA_MODE="dev")
    c = test_client_factory(create_app())
    csp = c.get("/healthz").headers.get("content-security-policy", "")
    # Dev must allow BOTH Vite relaxations: the HMR WebSocket (connect-src ws:)
    # and the injected inline dev bootstrap (script-src 'unsafe-inline').
    assert "ws:" in csp or "ws://localhost" in csp
    assert "script-src 'self' 'unsafe-inline'" in csp


def test_disabled_mode_keeps_strict_script_src(
    monkeypatch, pg_url, pg_session_factory, test_client_factory
):
    _env(monkeypatch, pg_url, SPA_MODE="disabled")
    c = test_client_factory(create_app())
    csp = c.get("/healthz").headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp and "script-src 'self' 'unsafe-inline'" not in csp


# ---------------------------------------------------------------------------
# Defect 8b: immutable Cache-Control for /assets/*
# ---------------------------------------------------------------------------


def test_assets_have_immutable_cache_control(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    """Vite-hashed assets under /assets/ must carry Cache-Control: public,
    max-age=31536000, immutable so browsers and CDNs cache them for 1 year."""
    import grant_lab.web.spa_serving as spa_serving

    spa_dir = tmp_path / "spa"
    (spa_dir / "assets").mkdir(parents=True)
    (spa_dir / "assets" / "main.AbCdEfGh.js").write_text("console.log('prod');")
    (spa_dir / "index.html").write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    c = test_client_factory(create_app())

    resp = c.get("/assets/main.AbCdEfGh.js")
    assert resp.status_code == 200
    cc = resp.headers.get("cache-control", "")
    assert "immutable" in cc, f"Expected 'immutable' in Cache-Control, got: {cc!r}"
    assert "max-age=31536000" in cc, f"Expected 'max-age=31536000' in Cache-Control, got: {cc!r}"
    assert "public" in cc, f"Expected 'public' in Cache-Control, got: {cc!r}"


def test_index_html_does_not_have_immutable_cache_control(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    """index.html is NOT content-hashed so must NOT receive the immutable header.
    It should be served without a long-lived cache directive."""
    import grant_lab.web.spa_serving as spa_serving

    spa_dir = tmp_path / "spa"
    (spa_dir / "assets").mkdir(parents=True)
    (spa_dir / "index.html").write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    c = test_client_factory(create_app())

    resp = c.get("/")
    assert resp.status_code == 200
    cc = resp.headers.get("cache-control", "")
    assert "immutable" not in cc, f"index.html must NOT have 'immutable', got: {cc!r}"


# ---------------------------------------------------------------------------
# Defect 9: SPA fallback short-circuit for backend paths
# ---------------------------------------------------------------------------


def test_backend_path_404_does_not_walk_routes(
    monkeypatch, pg_url, pg_session_factory, tmp_path, test_client_factory
):
    """An unknown /api/* path must return 404 without walking all app routes
    (the /api/ short-circuit fires first). Unknown non-SPA, non-/api paths
    (e.g. /workspaces/unknown) also 404 via the route walk finding no match."""
    import grant_lab.web.spa_serving as spa_serving

    spa_dir = tmp_path / "spa"
    (spa_dir / "assets").mkdir(parents=True)
    (spa_dir / "index.html").write_text("<!doctype html><title>spa</title>")
    monkeypatch.setattr(spa_serving, "SPA_DIR", spa_dir)
    _env(monkeypatch, pg_url, SPA_MODE="prod")
    c = test_client_factory(create_app())

    # Unknown /api/ subpath must be 404, not 405 or the SPA shell.
    resp = c.get("/api/totally-unknown-endpoint")
    assert resp.status_code == 404
    assert "<title>spa</title>" not in resp.text

    # Unknown /workspaces/* path also a backend prefix → 404.
    resp = c.get("/workspaces/totally-unknown")
    assert resp.status_code == 404
