from grant_lab.main import create_app


def _app(monkeypatch, pg_url):
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    return create_app()


def test_jinja_auth_routes_gone_and_api_auth_present(monkeypatch, pg_url):
    app = _app(monkeypatch, pg_url)
    paths = {getattr(r, "path", None) for r in app.routes}
    # The Jinja page GET routes are gone (SPA_MODE=disabled -> no catch-all here).
    for gone in ("/login", "/register", "/auth/pending", "/auth/no-access", "/auth/error"):
        assert gone not in paths
    # The typed /api surface is present.
    for present in ("/api/auth/config", "/api/auth/login", "/api/auth/register",
                    "/api/auth/logout"):
        assert present in paths
    # The OAuth routes survive.
    assert "/auth/google/start" in paths and "/auth/google/callback" in paths


def test_no_web_jinja_env_or_templates(monkeypatch, pg_url):
    app = _app(monkeypatch, pg_url)
    # The web Jinja env is gone (digest has its own env elsewhere).
    assert not hasattr(app.state, "templates")


def test_deleted_static_assets_absent():
    import grant_lab.main as m

    static = m.STATIC_DIR
    for gone in ("pico.min.css", "app.css", "app.js", "htmx.min.js", "htmx-sse.js", "dedup.js"):
        assert not (static / gone).exists()
    assert (static / "favicon.svg").exists()  # kept
