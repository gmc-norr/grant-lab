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
