import uuid

import pytest

from grant_lab.db.models import UncitedClaimsJob, WorkspaceEvent
from tests.proposal_assist.conftest import make_workspace


def _ws(_app, current_user, pg_session_factory, tmp_path, *, phase="active", ack=True):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=ack, current_phase=phase)
        s.commit()
        return str(ws.id)


def test_post_setup_409(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path, phase="setup")
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")
    assert r.status_code == 409


def test_post_missing_ack_403(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path, ack=False)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")
    assert r.status_code == 403


def test_post_create_or_return_active(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    a = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")
    b = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_trigger_event_written_once_on_new_run_only(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")  # creates a run
    client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")  # returns active
    with pg_session_factory() as s:
        events = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=uuid.UUID(ws_id), kind="uncited_claims_run_triggered")
            .all()
        )
        assert len(events) == 1  # only the first POST (new run) wrote the trigger event
        assert events[0].payload["job_id"]


def test_latest_null_then_running_claims_null(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    assert (
        client_authed_owner.get(f"/api/workspaces/{ws_id}/uncited-claims-runs/latest").json()
        is None
    )
    # seed a RUNNING job whose claims hold the RAW (unhashed) tool output
    with pg_session_factory() as s:
        s.add(
            UncitedClaimsJob(
                workspace_id=uuid.UUID(ws_id),
                status="running",
                claims=[{"claim_text": "a", "section_index": 0, "reason": "r"}],
            )
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/uncited-claims-runs/latest").json()
    assert body["status"] == "running"
    assert body["claims"] is None  # two-shape guard: raw claims never exposed
    assert body["stale"] is False


def test_latest_completed_returns_claims(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        s.add(
            UncitedClaimsJob(
                workspace_id=uuid.UUID(ws_id),
                status="completed",
                evaluated_draft_sha="",
                claims=[{"claim_text": "a", "section_index": 0, "reason": "r", "claim_hash": "h"}],
            )
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/uncited-claims-runs/latest").json()
    assert body["status"] == "completed"
    assert body["claims"][0]["claim_hash"] == "h"
    assert body["stale"] is False  # evaluated "" == current "" (no draft on disk)


def test_dismiss_writes_event_and_is_idempotent(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r1 = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims/abc123/dismiss")
    r2 = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims/abc123/dismiss")
    assert r1.status_code == 200 and r1.json() == {"ok": True}
    assert r2.status_code == 200
    with pg_session_factory() as s:
        events = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=uuid.UUID(ws_id), kind="uncited_claim_dismissed")
            .all()
        )
        assert len(events) == 2  # idempotent at the UX level; two dismiss events is harmless
        assert events[0].payload["claim_hash"] == "abc123"


def test_unauthenticated_rejected(
    client_authed_owner, client_unauth, _app, current_user, pg_session_factory, tmp_path
):
    # AUTHENTICATION only — see the membership test below for why that is not
    # the same thing. This was named test_non_member_rejected.
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/uncited-claims-runs/latest")
    assert r.status_code in (401, 403)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/workspaces/{ws}/uncited-claims-runs"),
        ("get", "/api/workspaces/{ws}/uncited-claims-runs/latest"),
        ("post", "/api/workspaces/{ws}/uncited-claims/abc123/dismiss"),
    ],
)
def test_an_authenticated_non_member_is_refused_by_every_route(
    method, path, client_authed_owner, client_authed_outsider,
    _app, current_user, pg_session_factory, tmp_path,
):
    # See the evaluator twin for the method. Measured here: only the `latest`
    # read was noticed by any test; the run-start POST and the dismiss POST —
    # which MUTATES another group's data — were both silent at 86 passed.
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = getattr(client_authed_outsider, method)(path.format(ws=ws_id))
    assert r.status_code == 403, r.text
    assert "not a workspace member" in r.text