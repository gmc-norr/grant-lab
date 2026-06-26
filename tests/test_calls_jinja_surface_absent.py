"""Route-table guard: the Jinja call-detail surface must not exist.

After the calls-detail Jinja teardown (Task 7), the bare /calls/{call_id} and
/calls/{call_id}/hide routes must be absent from the app route table.  The /api
equivalents must still be present.
"""


def test_no_jinja_calls_route_remains(monkeypatch, pg_url):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    from call_manager.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/calls/{call_id}" not in paths
    assert "/calls/{call_id}/hide" not in paths
    # the /api detail + hide ARE present
    assert "/api/calls/{call_id}" in paths
    assert "/api/calls/{call_id}/hide" in paths
