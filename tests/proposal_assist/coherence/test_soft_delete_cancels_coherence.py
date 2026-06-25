from call_manager.db.models import CoherenceJob
from tests.proposal_assist.conftest import make_workspace


def test_soft_delete_cancels_active_coherence_jobs(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = str(ws.id)
    # Soft-delete now lives at DELETE /api/workspaces/{id} (cockpit gap-closer).
    r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    assert r.status_code == 204
    with pg_session_factory() as s:
        job = s.query(CoherenceJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "failed"  # cancelled on delete
