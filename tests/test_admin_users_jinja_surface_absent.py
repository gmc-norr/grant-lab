def test_no_jinja_admin_routes_remain(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from call_manager.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    # Jinja user-mgmt routes (Slice 1) gone:
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
    # Jinja Slice-2 routes gone:
    for p in [
        "/admin",
        "/admin/drafting",
        "/admin/rerank",
        "/admin/digest/send",
        "/admin/digest/reenable",
        "/admin/sources/{slug}/fetch",
        "/admin/golden-set",
        "/admin/golden-set/add",
        "/admin/golden-set/delete",
    ]:
        assert p not in paths, p
    # /api routes present:
    for p in [
        "/api/admin/users",
        "/api/admin/users/pending",
        "/api/admin/dashboard",
        "/api/admin/drafting",
        "/api/admin/rerank",
        "/api/admin/digest/send",
        "/api/admin/digest/reenable",
        "/api/admin/sources/{slug}/fetch",
        "/api/admin/golden-set",
        "/api/admin/golden-set/{triple_id}",
    ]:
        assert p in paths, p
