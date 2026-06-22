from call_manager.db.models import ReviewRun
from tests.proposal_assist.conftest import make_workspace


def test_soft_delete_cancels_active_review_runs(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(ReviewRun(workspace_id=ws.id, status="queued", draft_sha="a" * 64))
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/workspaces/{ws_id}/delete", follow_redirects=False)
    assert r.status_code in (200, 303)
    with pg_session_factory() as s:
        run = s.query(ReviewRun).filter_by(workspace_id=ws_id).one()
        assert run.status == "cancelled"
