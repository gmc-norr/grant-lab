from sqlalchemy import select

from call_manager.db.models import CoherenceJob, WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def test_post_create_or_return_active(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    _app.state.settings.drafting_enabled = True
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)  # active phase + ack
        s.commit()
        ws_id = str(ws.id)

    r1 = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r1.status_code == 200, r1.text
    job_id = r1.json()["id"]
    r2 = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r2.json()["id"] == job_id  # returned active, not stacked


def test_setup_phase_returns_409(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="setup", ack_disclosure=True)
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r.status_code == 409


def test_drafting_disabled_returns_503(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    _app.state.settings.drafting_enabled = False
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r.status_code == 503


def test_missing_disclosure_returns_403(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    _app.state.settings.drafting_enabled = True
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=False)
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r.status_code == 403


def test_latest_returns_null_then_job(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = str(ws.id)
    r0 = client_authed_owner.get(f"/api/workspaces/{ws_id}/coherence-runs/latest")
    assert r0.status_code == 200 and r0.json() is None
    with pg_session_factory() as s:
        s.add(CoherenceJob(workspace_id=ws_id, status="completed", n_proposals=2))  # type: ignore[arg-type]
        s.commit()
    r1 = client_authed_owner.get(f"/api/workspaces/{ws_id}/coherence-runs/latest")
    assert r1.json()["status"] == "completed" and r1.json()["n_proposals"] == 2


def test_non_member_403(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger2@ki.se")
        ws = make_workspace(s, owner=stranger, ack_disclosure=True)
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r.status_code == 403


def test_enqueue_writes_actor_attributed_trigger_event(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    """Enqueuing a coherence run must emit a coherence_run_triggered WorkspaceEvent
    carrying the triggering actor (mirrors review_run_triggered), and NOT duplicate
    it on the active-first re-POST."""
    _app.state.settings.workspaces_dir = str(tmp_path)
    _app.state.settings.drafting_enabled = True
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        ws_id, uid = str(ws.id), current_user.id

    r1 = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r1.status_code == 200, r1.text
    job_id = r1.json()["id"]
    # active-first re-POST returns the same job and must NOT stack a 2nd event
    r2 = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r2.json()["id"] == job_id

    with pg_session_factory() as s:
        evs = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,  # type: ignore[arg-type]
                WorkspaceEvent.kind == "coherence_run_triggered",
            )
        ).all()
    assert len(evs) == 1
    assert evs[0].actor_id == uid
    assert evs[0].payload["job_id"] == job_id
