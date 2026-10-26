def test_no_jinja_profiles_routes_remain(make_app, monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = make_app()
    paths = {r.path for r in app.routes}
    # Jinja HTML routes gone:
    assert "/profiles" not in paths
    assert "/profiles/new" not in paths
    assert "/profiles/{profile_id}" not in paths
    assert "/profiles/{profile_id}/edit" not in paths
    assert "/profiles/{profile_id}/bootstrap-status" not in paths
    # /api/profiles* remain:
    assert "/api/profiles" in paths
    assert "/api/profiles/{profile_id}" in paths
