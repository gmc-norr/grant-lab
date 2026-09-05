"""G2 teardown guard: no FastAPI route is registered under the bare Jinja
`/workspaces` HTML prefix. All workspace routes now live under `/api/workspaces`.

Inspect the route TABLE, not an HTTP response: a GET /workspaces could be
swallowed by the SPA catch-all and look like 200, masking a stale route."""

from __future__ import annotations

from grant_lab.main import create_app


def test_no_bare_workspaces_route_registered(monkeypatch, pg_url):
    # Use pg_url (the real testcontainer URL, psycopg3 driver) — a bare
    # "postgresql://" selects the psycopg2 dialect, which is NOT installed, so
    # create_app() would fail at engine creation before the route assertion.
    # pg_url is defined in tests/conftest.py and available to this root test.
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    app = create_app()
    bare = [
        r.path
        for r in app.routes
        if getattr(r, "path", "").startswith("/workspaces")
    ]
    assert bare == [], f"stale Jinja /workspaces routes still registered: {bare}"
