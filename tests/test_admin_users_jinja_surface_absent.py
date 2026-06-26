def test_no_jinja_admin_user_routes_remain(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from call_manager.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    # Jinja user-mgmt routes gone:
    for p in [
        "/admin/users",
        "/admin/users/pending",
        "/admin/users/{user_id}/approve",
        "/admin/users/{user_id}/deny",
        "/admin/users/{user_id}/disable",
        "/admin/users/{user_id}/enable",
        "/admin/users/{user_id}/role",
        "/admin/users/{user_id}/reset-password",
    ]:
        assert p not in paths, p
    # /api routes present:
    assert "/api/admin/users" in paths
    assert "/api/admin/users/pending" in paths
    assert "/api/admin/users/{user_id}/approve" in paths
    # Slice-2 admin routes still present (we only removed user-mgmt):
    assert "/admin" in paths
    assert "/admin/rerank" in paths
    assert "/admin/golden-set" in paths
