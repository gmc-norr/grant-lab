from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import Reference, WorkspaceEvent
from call_manager.proposal_assist.references.storage import ReferenceStorage
from tests.proposal_assist._refs_helpers import authed_client, build_app
from tests.proposal_assist.conftest import make_workspace
from tests.proposal_assist.references.test_reprocess import _insert_failed_ref


def _tombstone(pg_session_factory, rid):
    with pg_session_factory() as s:
        ref = s.get(Reference, rid)
        ref.tombstoned_at = datetime.now(UTC)
        s.commit()


def test_metadata_post_on_tombstoned_404(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nx")
    _tombstone(pg_session_factory, rid)
    resp = authed_client(app, user_id=owner_id).post(
        f"/workspaces/{wsid}/references/{rid}/metadata", data={"title": "X"}, follow_redirects=False
    )
    assert resp.status_code == 404


def test_waiver_post_on_tombstoned_404(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nx")
    _tombstone(pg_session_factory, rid)
    resp = authed_client(app, user_id=owner_id).post(
        f"/workspaces/{wsid}/references/{rid}/waiver", data={"waiver": "true"}
    )
    assert resp.status_code == 404


def test_metadata_approve_writes_audit(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    storage = ReferenceStorage(root=tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        wsid, owner_id = ws.id, ws.owner_id
        rid = _insert_failed_ref(s, storage, workspace_id=wsid, data=b"# H\n\nx")
    resp = authed_client(app, user_id=owner_id).post(
        f"/workspaces/{wsid}/references/{rid}/metadata",
        data={"title": "A Title"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with pg_session_factory() as s:
        kinds = {
            e.kind
            for e in s.scalars(select(WorkspaceEvent).where(WorkspaceEvent.workspace_id == wsid))
        }
        assert "reference.metadata_approved" in kinds
