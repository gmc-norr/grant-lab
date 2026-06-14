"""Template markup tests for the references panel and per-row partials.

Task 15: drop zone + native-HTMX kind select + delete/reprocess controls.
"""

from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def test_panel_shows_dropzone_for_member(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    with pg_session_factory() as s:
        owner = make_user(s, email="otpl@ki.se")
        member = make_user(s, email="mtpl@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, member_id = ws.id, member.id
        add_member(s, workspace_id=wsid, user=member)
    resp = authed_client(app, user_id=member_id).get(f"/workspaces/{wsid}/references")
    assert resp.status_code == 200
    assert "reference-dropzone" in resp.text
    assert 'data-max-files="25"' in resp.text
