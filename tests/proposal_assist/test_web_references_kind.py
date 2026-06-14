from sqlalchemy import select

from call_manager.db.models import Reference, WorkspaceEvent
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.proposal_assist.references.test_reprocess import _insert_failed_ref


def test_member_patches_kind(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="ok@ki.se")
        member = make_user(s, email="mk@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, member_id = ws.id, member.id
        add_member(s, workspace_id=wsid, user=member)
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nx")
    resp = authed_client(app, user_id=member_id).patch(
        f"/workspaces/{wsid}/references/{rid}/kind", data={"kind": "data"}
    )
    assert resp.status_code == 200, resp.text
    with pg_session_factory() as s:
        assert s.get(Reference, rid).kind == "data"
        kinds = {
            e.kind
            for e in s.scalars(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == wsid))
        }
        assert "reference.kind_changed" in kinds


def test_patch_kind_rejects_invalid(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nx")
    resp = authed_client(app, user_id=owner_id).patch(
        f"/workspaces/{wsid}/references/{rid}/kind", data={"kind": "bogus"}
    )
    assert resp.status_code == 400


def test_patch_kind_non_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        owner = make_user(s, email="ok2@ki.se")
        stranger = make_user(s, email="sk2@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, sid = ws.id, stranger.id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nx")
    resp = authed_client(app, user_id=sid).patch(
        f"/workspaces/{wsid}/references/{rid}/kind", data={"kind": "data"}
    )
    assert resp.status_code == 403
