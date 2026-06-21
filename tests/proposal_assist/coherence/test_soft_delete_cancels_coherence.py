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
    # Real soft-delete route is mounted under /workspaces (not /api) and 303-redirects.
    r = client_authed_owner.post(f"/workspaces/{ws_id}/delete", follow_redirects=False)
    assert r.status_code in (200, 303)
    with pg_session_factory() as s:
        job = s.query(CoherenceJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "failed"  # cancelled on delete
