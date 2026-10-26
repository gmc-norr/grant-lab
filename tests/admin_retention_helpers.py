"""Shared setup for the admin retention endpoint tests.

Retention Slice B shipped these three helpers copied verbatim into
tests/test_api_admin_retention_list.py and
tests/test_api_admin_retention_inspect.py. Two copies of an app-construction
helper drift: the inspect copy already set workspaces_dir for a different stated
reason than the list copy, and a future change to session setup would have to be
made twice or silently apply to only one endpoint's tests.

Not a conftest fixture: these take arguments (monkeypatch, pg_url, tmp_path) and
are called at different points in different tests, so a plain module keeps the
call sites explicit about what they are configuring.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from grant_lab.db.models import User, Workspace


def setup_app(make_app, monkeypatch, pg_url, tmp_path):
    """Build an app against the test database, rooted at a temporary
    workspaces_dir.

    Same shape as tests/test_api_admin_users.py:_setup. The workspaces_dir
    override is REQUIRED, not incidental: the inspect route resolves
    repositories under it, and without the override a test reads the real
    configured directory.
    """
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "umu.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = make_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def client_for(app, sf, *, email, is_admin):
    """Cookie-authenticated TestClient, matching test_api_admin_users.py:_admin.

    Returns (client, user_id).
    """
    with sf() as s:
        u = User(email=email, status="active", is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def soft_delete(session, workspace_id):
    """Stamp soft_deleted_at directly, without the delete route.

    These tests exercise the READ endpoints; going through the route would drag
    in retirement, job cancellation and the commit mutex, none of which the
    listing or inspection behaviour depends on.
    """
    ws = session.get(Workspace, workspace_id)
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()
