import uuid

from call_manager.db.models import EvaluatorJob, Workspace
from tests.proposal_assist.conftest import make_workspace


def _ws(_app, current_user, pg_session_factory, tmp_path, *, phase="active", ack=True):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=ack, current_phase=phase)
        s.flush()
        ws.rubric_criteria = []
        # NOTE: do NOT set rubric_updated_at — it's a non-null column with a server
        # default; the staleness test reads the real value to seed a matching marker.
        s.commit()
        return str(ws.id)


def test_post_setup_409(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path, phase="setup")
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    assert r.status_code == 409


def test_post_missing_ack_403(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path, ack=False)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    assert r.status_code == 403


def test_post_create_or_return_active(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    a = client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    b = client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_latest_null_then_job(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    assert client_authed_owner.get(f"/api/workspaces/{ws_id}/evaluator-runs/latest").json() is None
    client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    assert client_authed_owner.get(f"/api/workspaces/{ws_id}/evaluator-runs/latest").json()[
        "status"
    ] in ("queued", "running")


def test_scorecard_null_then_value_with_stale_flags(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    assert client_authed_owner.get(f"/api/workspaces/{ws_id}/scorecard").json() is None
    wid = uuid.UUID(ws_id)
    # seed a completed job whose evaluated markers MATCH the current state: the repo
    # has no proposal.md/proposal-spec.md (content_sha == "") and the rubric marker is
    # the workspace's real rubric_updated_at -> stale must be False.
    with pg_session_factory() as s:
        rubric_ts = s.get(Workspace, wid).rubric_updated_at
        s.add(
            EvaluatorJob(
                workspace_id=wid,
                status="completed",
                scorecard={"criteria": [], "overall_band": "gap", "summary": "x"},
                evaluated_draft_sha="",
                evaluated_spec_sha="",
                evaluated_rubric_updated_at=rubric_ts,
            )
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/scorecard").json()
    assert body["scorecard"]["overall_band"] == "gap"
    assert body["stale"] is False and body["stale_reasons"] == []


def test_non_member_403(
    client_authed_owner, client_unauth, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    # an unauthenticated client is rejected by get_current_user (401/403)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/scorecard")
    assert r.status_code in (401, 403)
