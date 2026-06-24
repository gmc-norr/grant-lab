"""JSON /api workspace settings + collaborator endpoints (Cockpit Slice D)."""

from sqlalchemy import func, select

from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(pg_session_factory):
    """Return (ws_id, owner_id, member_id, stranger_id). member is a viewer."""
    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        return ws.id, owner.id, member.id, stranger.id


def test_settings_get_owner_returns_fields(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"title", "status", "deadline", "citation_style"}


def test_settings_get_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/settings")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_settings_patch_owner_updates(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import Workspace

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={
            "title": "Renamed",
            "status": "submitted",
            "deadline": "2026-09-01",
            "citation_style": "author-year",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Renamed"
    assert r.json()["deadline"] == "2026-09-01"
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.title == "Renamed" and ws.status == "submitted"
        assert ws.citation_style == "author-year" and ws.deadline is not None


def test_settings_patch_clears_deadline(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    c = authed_client(app, user_id=owner_id)
    c.patch(
        f"/api/workspaces/{ws_id}/settings",
        json={
            "title": "T",
            "status": "drafting",
            "deadline": "2026-09-01",
            "citation_style": "vancouver",
        },
    )
    r = c.patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deadline"] is None


def test_settings_patch_bad_status_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "bogus", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_settings_patch_bad_citation_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "mla"},
    )
    assert r.status_code == 400


def test_settings_patch_empty_title_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={
            "title": "   ",
            "status": "drafting",
            "deadline": None,
            "citation_style": "vancouver",
        },  # noqa: E501
    )
    assert r.status_code == 400


def test_settings_patch_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        json={"title": "T", "status": "drafting", "deadline": None, "citation_style": "vancouver"},
    )
    assert r.status_code == 403


def _add_user(pg_session_factory, email):
    with pg_session_factory() as s:
        u = make_user(s, email=email)
        s.commit()
        return u.id


def test_members_add_owner_201_and_event(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import WorkspaceEvent, WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    new_id = _add_user(pg_session_factory, "newcollab@ki.se")
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "newcollab@ki.se"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {"user_id": new_id, "email": "newcollab@ki.se", "role": "member"}
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceViewer)
                .where(WorkspaceViewer.workspace_id == ws_id, WorkspaceViewer.user_id == new_id)
            )
            == 1
        )
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_added"
            )
        )
        assert ev is not None and ev.actor_id == owner_id
        assert ev.payload["target_user_id"] == new_id
        assert ev.payload["target_email"] == "newcollab@ki.se"


def test_members_add_unknown_email_400_generic(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "nobody@ki.se"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"


def test_members_add_duplicate_idempotent_200(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "member@ki.se"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == member_id


def test_members_add_owner_email_idempotent_200(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "owner@ki.se"}
    )
    assert r.status_code == 200
    assert r.json()["role"] == "owner"


def test_members_add_non_owner_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/members", json={"email": "member@ki.se"}
    )
    assert r.status_code == 403


def test_members_delete_owner_204_and_event(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import WorkspaceEvent, WorkspaceViewer

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, member_id, _ = _setup(pg_session_factory)
    c = authed_client(app, user_id=owner_id)
    r = c.delete(f"/api/workspaces/{ws_id}/members/{member_id}")
    assert r.status_code == 204
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceViewer)
                .where(WorkspaceViewer.workspace_id == ws_id, WorkspaceViewer.user_id == member_id)
            )
            == 0
        )
        ev = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_removed"
            )
        )
        assert ev is not None and ev.payload["target_user_id"] == member_id
    # idempotent: deleting again is still 204, writes no second event
    assert c.delete(f"/api/workspaces/{ws_id}/members/{member_id}").status_code == 204
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceEvent)
                .where(
                    WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "member_removed"
                )
            )
            == 1
        )


def test_members_delete_non_owner_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, stranger_id = _setup(pg_session_factory)
    r = authed_client(app, user_id=stranger_id).delete(
        f"/api/workspaces/{ws_id}/members/{member_id}"
    )
    assert r.status_code == 403
