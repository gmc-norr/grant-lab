from call_manager.db.models import Reference
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.references.test_reprocess import _insert_failed_ref


def test_member_can_delete_uncited(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="od@ki.se")
        member = make_user(s, email="md@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, member_id = ws.id, member.id
        add_member(s, workspace_id=wsid, user=member)
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nbody")
    resp = authed_client(app, user_id=member_id).post(f"/workspaces/{wsid}/references/{rid}/delete")
    assert resp.status_code == 200, resp.text
    assert resp.text.strip() == ""  # empty body → HTMX removes the row
    with pg_session_factory() as s:
        assert s.get(Reference, rid) is None


def test_non_member_cannot_delete(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="od2@ki.se")
        stranger = make_user(s, email="st2@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, sid = ws.id, stranger.id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nbody")
    resp = authed_client(app, user_id=sid).post(f"/workspaces/{wsid}/references/{rid}/delete")
    assert resp.status_code == 403
    with pg_session_factory() as s:
        assert s.get(Reference, rid) is not None  # not deleted
