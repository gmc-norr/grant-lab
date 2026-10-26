"""GET /api/admin/workspaces/deleted — the admin list of soft-deleted workspaces.

Retention Slice B. Reads only; this endpoint can remove nothing.
"""

from datetime import UTC, datetime

from grant_lab.db.models import Workspace
from tests.admin_retention_helpers import client_for, setup_app, soft_delete
from tests.proposal_assist.conftest import make_user, make_workspace


def test_lists_only_deleted_workspaces(make_app, monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    admin, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="o@umu.se")
        live = make_workspace(s, owner=owner, title="still here")
        dead = make_workspace(s, owner=owner, title="gone")
        s.commit()
        live_id, dead_id = live.id, dead.id
        soft_delete(s, dead_id)

    r = admin.get("/api/admin/workspaces/deleted")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["items"]]
    assert str(dead_id) in ids
    assert str(live_id) not in ids, (
        "a live workspace reachable through the admin retention surface would "
        "make it a back door around ownership"
    )
    # `total` comes from a SEPARATE count query carrying its own filter, and it
    # is an int — so unlike `items` it survives serialization and catches the
    # filter being dropped from the count alone.
    assert r.json()["total"] == 1


def test_the_row_model_keeps_soft_deleted_at_required(monkeypatch, pg_url, tmp_path):
    """Today a live workspace cannot even be serialized by this route: its
    soft_deleted_at is NULL and DeletedWorkspaceRow declares the field required,
    so dropping the WHERE filter produces a 500 rather than a leak.

    That is a real second guard, but an accidental one — relaxing the annotation
    to `datetime | None` (an easy thing to do when Task 3's detail model is
    written beside it) would silently remove it and leave only the filter. Pin
    it, so removing the guard is a decision rather than a side effect.
    """
    from grant_lab.web.api.admin_retention import DeletedWorkspaceRow

    field = DeletedWorkspaceRow.model_fields["soft_deleted_at"]
    assert field.is_required(), (
        "soft_deleted_at became optional; a live workspace can now serialize "
        "through this route, so the WHERE filter is the only thing excluding it"
    )
    assert field.annotation is datetime


def test_newest_deletion_first(make_app, monkeypatch, pg_url, pg_session_factory, tmp_path):
    """Spec 3.1.1 mandates "newest `soft_deleted_at` first". The final review
    found this was the one spec-mandated constraint in the slice with zero
    coverage: flipping .desc() to .asc() left all 25 retention tests green.

    Distinct timestamps, set explicitly rather than by wall-clock, so the
    assertion cannot pass by accident on a fast machine."""
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    admin, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="ord@umu.se")
        made = [make_workspace(s, owner=owner, title=f"w{i}") for i in range(3)]
        s.commit()
        ids = [w.id for w in made]
        # w0 oldest, w2 newest — deliberately NOT the insertion order of the ids.
        for wid, day in zip(ids, (1, 3, 2), strict=True):
            ws = s.get(Workspace, wid)
            ws.soft_deleted_at = datetime(2026, 8, day, 12, 0, tzinfo=UTC)
        s.commit()

    items = admin.get("/api/admin/workspaces/deleted").json()["items"]
    titles = [row["title"] for row in items]
    assert titles == ["w1", "w2", "w0"], (
        "the list must be newest-deletion-first; an admin scanning for what was "
        "just deleted would otherwise have to page to the end"
    )


def test_equal_deletion_times_still_page_without_repeating_a_row(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """limit/offset over an unstable sort can repeat or skip rows between pages.
    Two workspaces deleted in the same instant is the case that exposes it, so
    the query carries an id tiebreak."""
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    admin, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    same = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)
    with pg_session_factory() as s:
        owner = make_user(s, email="tie@umu.se")
        made = [make_workspace(s, owner=owner, title=f"t{i}") for i in range(4)]
        s.commit()
        for w in made:
            s.get(Workspace, w.id).soft_deleted_at = same
        s.commit()

    seen = []
    for offset in (0, 2):
        page = admin.get(f"/api/admin/workspaces/deleted?limit=2&offset={offset}").json()
        seen += [row["id"] for row in page["items"]]
    assert len(seen) == 4
    assert len(set(seen)) == 4, "a row appeared on two pages, or one was skipped"


def test_non_admin_is_403(make_app, monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    member, _ = client_for(app, pg_session_factory, email="member@umu.se", is_admin=False)
    r = member.get("/api/admin/workspaces/deleted")
    assert r.status_code == 403


def test_pagination_caps_the_limit(make_app, monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    admin, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    r = admin.get("/api/admin/workspaces/deleted?limit=5000")
    assert r.status_code == 422, "an uncapped limit lets one request read the whole table"


def test_returns_total_count_and_honours_offset(
    make_app, monkeypatch, pg_url, pg_session_factory, tmp_path
):
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    admin, _ = client_for(app, pg_session_factory, email="admin@umu.se", is_admin=True)
    with pg_session_factory() as s:
        owner = make_user(s, email="o2@umu.se")
        made = [make_workspace(s, owner=owner, title=f"w{i}") for i in range(3)]
        s.commit()
        ids = [w.id for w in made]
        for wid in ids:
            soft_delete(s, wid)

    first = admin.get("/api/admin/workspaces/deleted?limit=2&offset=0").json()
    second = admin.get("/api/admin/workspaces/deleted?limit=2&offset=2").json()
    assert first["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    # Disjoint pages, not merely different first rows: an off-by-one in the
    # offset would still satisfy "first id differs".
    assert not ({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})


def test_the_route_is_documented_with_the_shared_error_envelope(
    make_app, monkeypatch, pg_url, tmp_path
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
    app = setup_app(make_app, monkeypatch, pg_url, tmp_path)
    responses = app.openapi()["paths"]["/api/admin/workspaces/deleted"]["get"]["responses"]
    ref = responses["422"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/ApiError", (
        f"422 documents {ref} — the router is mounted outside web/api/__init__.py"
    )
    assert {"401", "403", "404", "429"} <= set(responses)
