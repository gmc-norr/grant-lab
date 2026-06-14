from sqlalchemy import select

from call_manager.db.models import Reference, WorkspaceEvent
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist._refs_helpers import FakeEmbedClient, authed_client, build_app
from tests.proposal_assist.conftest import make_workspace
from tests.proposal_assist.references.test_reprocess import _insert_failed_ref


def test_reprocess_flips_failed_done(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()
    app.state.citation_service = None
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nbody text here")
    resp = authed_client(app, user_id=owner_id).post(
        f"/workspaces/{wsid}/references/{rid}/reprocess"
    )
    assert resp.status_code == 200, resp.text
    with pg_session_factory() as s:
        assert s.get(Reference, rid).text_status == "done"
        kinds = {
            e.kind
            for e in s.scalars(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == wsid))
        }
        assert "reference.reprocessed" in kinds


def test_reprocess_done_ref_returns_409(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    app.state.embed_client = FakeEmbedClient()
    app.state.citation_service = None
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nbody")
        ref = s.get(Reference, rid)
        ref.text_status = "done"  # not failed → not reprocessable
        s.commit()
    resp = authed_client(app, user_id=owner_id).post(
        f"/workspaces/{wsid}/references/{rid}/reprocess"
    )
    assert resp.status_code == 409


def test_reprocess_non_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        from tests.proposal_assist.conftest import make_user

        owner = make_user(s, email="orp@ki.se")
        stranger = make_user(s, email="srp@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        wsid, sid = ws.id, stranger.id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nbody")
    resp = authed_client(app, user_id=sid).post(f"/workspaces/{wsid}/references/{rid}/reprocess")
    assert resp.status_code == 403
