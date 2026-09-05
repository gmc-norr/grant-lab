def test_no_jinja_me_routes_remain(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from grant_lab.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/me" not in paths  # Jinja settings page gone
    assert "/me/name" not in paths
    assert "/me/password" not in paths
    assert "/me/subscriptions" not in paths
    # the /api/me/* endpoints remain
    assert "/api/me/settings" in paths
    assert "/api/me/subscriptions" in paths
