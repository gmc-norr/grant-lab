"""GET /api/admin/workspaces/deleted — the admin list of soft-deleted workspaces.

Retention Slice B. Reads only; this endpoint can remove nothing.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from call_manager.db.models import User, Workspace
from call_manager.main import create_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(monkeypatch, pg_url, tmp_path):
    """Same shape as tests/test_api_admin_users.py:_setup, plus a workspaces_dir.

    Task 3 reads files under it; setting it here too keeps both test modules
    identical rather than having one silently point at the real configured
    directory."""
    for k, v in {
        "DATABASE_URL": pg_url,
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
    }.items():
        monkeypatch.setenv(k, v)
    app = create_app()
    app.state.settings.workspaces_dir = str(tmp_path)
    return app


def _client_for(app, sf, *, email, is_admin):
    """Cookie-authenticated TestClient, matching test_api_admin_users.py:_admin."""
    with sf() as s:
        u = User(email=email, status="active", is_admin=is_admin)
        s.add(u)
        s.commit()
        s.refresh(u)
        uid = u.id
    c = TestClient(app)
    c.cookies.set("session", app.state.session_signer.make(user_id=uid))
    return c, uid


def _delete(session, workspace_id):
    ws = session.get(Workspace, workspace_id)
    ws.soft_deleted_at = datetime.now(UTC)
    session.commit()


def test_lists_only_deleted_workspaces(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        live = make_workspace(s, owner=owner, title="still here")
        dead = make_workspace(s, owner=owner, title="gone")
        s.commit()
        live_id, dead_id = live.id, dead.id
        _delete(s, dead_id)

    r = admin.get("/api/admin/workspaces/deleted")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["items"]]
    assert str(dead_id) in ids
    assert str(live_id) not in ids, (
        "a live workspace reachable through the admin retention surface would "
        "make it a back door around ownership"
    )


def test_non_admin_is_403(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    member, _ = _client_for(app, pg_session_factory, email="member@ki.se", is_admin=False)
    r = member.get("/api/admin/workspaces/deleted")
    assert r.status_code == 403


def test_pagination_caps_the_limit(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    r = admin.get("/api/admin/workspaces/deleted?limit=5000")
    assert r.status_code == 422, "an uncapped limit lets one request read the whole table"


def test_returns_total_count_and_honours_offset(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = _setup(monkeypatch, pg_url, tmp_path)
    admin, _ = _client_for(app, pg_session_factory, email="admin@ki.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="o2@ki.se")
        made = [make_workspace(s, owner=owner, title=f"w{i}") for i in range(3)]
        s.commit()
        ids = [w.id for w in made]
        for wid in ids:
            _delete(s, wid)

    first = admin.get("/api/admin/workspaces/deleted?limit=2&offset=0").json()
    second = admin.get("/api/admin/workspaces/deleted?limit=2&offset=2").json()
    assert first["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    # Disjoint pages, not merely different first rows: an off-by-one in the
    # offset would still satisfy "first id differs".
    assert not ({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})


def test_the_route_is_documented_with_the_shared_error_envelope(
    monkeypatch, pg_url, tmp_path
):
    """These routes must go through web/api/__init__.py's router, which declares
    responses={code: ApiError} for every status the /api surface emits. Mounting
    them directly on the app instead advertises FastAPI's stock
    HTTPValidationError for the 422 and breaks the OpenAPI -> TS codegen.

    This asserts the SCHEMA, not a runtime body, and that distinction matters:
    the runtime envelope comes from app-level exception handlers gated on
    is_api_path() (main.py:637), so a directly-mounted route still RETURNS the
    envelope. Only the generated document tells the two mountings apart —
    verified: /api/admin/users documents $ref ApiError and has 11 documented
    statuses, while a directly-mounted route documents HTTPValidationError and
    has 2."""
    app = _setup(monkeypatch, pg_url, tmp_path)
    responses = app.openapi()["paths"]["/api/admin/workspaces/deleted"]["get"]["responses"]
    ref = responses["422"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/ApiError", (
        f"422 documents {ref} — the router is mounted outside web/api/__init__.py"
    )
    assert {"401", "403", "404", "429"} <= set(responses)
