"""Member-access tests for reference endpoints (Task 8).

Verifies that a WorkspaceViewer (member) can access reference routes and
that a true non-member (no WorkspaceViewer row) is still denied with 403.
"""

from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def test_member_can_view_panel(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """A WorkspaceViewer can GET the references panel."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    with pg_session_factory() as s:
        owner = make_user(s, email="own@ki.se")
        member = make_user(s, email="mem@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, member_id = ws.id, member.id
        add_member(s, workspace_id=wsid, user=member)
    resp = authed_client(app, user_id=member_id).get(f"/workspaces/{wsid}/references")
    assert resp.status_code == 200, resp.text


def test_member_sees_upload_form(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    with pg_session_factory() as s:
        owner = make_user(s, email="ow-up@ki.se")
        member = make_user(s, email="mem-up@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, member_id = ws.id, member.id
        add_member(s, workspace_id=wsid, user=member)
    resp = authed_client(app, user_id=member_id).get(f"/workspaces/{wsid}/references")
    assert resp.status_code == 200
    # The upload form posts to the references endpoint; a member must see it.
    assert "enctype=\"multipart/form-data\"" in resp.text
    assert "read-only" not in resp.text.lower()


def test_owner_sees_no_readonly_notice(monkeypatch, pg_url, tmp_path, pg_session_factory):
    # Owner always sees the upload UI, so no read-only notice should appear.
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    with pg_session_factory() as s:
        owner = make_user(s, email="ow-ro@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, owner_id = ws.id, owner.id
    resp = authed_client(app, user_id=owner_id).get(f"/workspaces/{wsid}/references")
    assert resp.status_code == 200
    assert "read-only" not in resp.text.lower()


def test_stranger_gets_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """A user who is neither owner nor WorkspaceViewer is denied with 403."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    with pg_session_factory() as s:
        owner = make_user(s, email="own2@ki.se")
        stranger = make_user(s, email="str2@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, sid = ws.id, stranger.id
    resp = authed_client(app, user_id=sid).get(f"/workspaces/{wsid}/references")
    assert resp.status_code == 403
