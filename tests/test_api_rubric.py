"""JSON /api rubric + setup-status + activation endpoints (Cockpit Slice E)."""

from tests.proposal_assist._refs_helpers import add_member, authed_client, build_app
from tests.proposal_assist.conftest import make_user, make_workspace


def _setup(
    pg_session_factory,
    *,
    phase="setup",
    job_status="waiting_for_rubric_review",
    criteria=None,
    last_error=None,
):
    """Return (ws_id, owner_id, member_id, stranger_id)."""
    from call_manager.db.models import WorkspaceSetupJob

    with pg_session_factory() as s:
        owner = make_user(s, email="owner@ki.se")
        member = make_user(s, email="member@ki.se")
        stranger = make_user(s, email="stranger@ki.se")
        ws = make_workspace(s, owner=owner)
        ws.current_phase = phase
        ws.rubric_criteria = (
            criteria
            if criteria is not None
            else [
                {
                    "name": "Impact",
                    "description": "d",
                    "weight": 0.6,
                    "scale_max": 5,
                    "subcriteria": [{"name": "reach", "description": "x"}],
                },
                {"name": "Feasibility", "description": "d", "weight": 0.4, "scale_max": 5},
            ]
        )
        ws.rubric_funder_notes = "notes"
        if job_status is not None:
            s.add(WorkspaceSetupJob(workspace_id=ws.id, status=job_status, last_error=last_error))
        s.commit()
        add_member(s, workspace_id=ws.id, user=member)
        return ws.id, owner.id, member.id, stranger.id


def test_get_rubric_owner_returns_criteria_with_subcriteria(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):  # noqa: E501
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/rubric")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {c["name"] for c in body["criteria"]} == {"Impact", "Feasibility"}
    impact = next(c for c in body["criteria"] if c["name"] == "Impact")
    assert impact["subcriteria"] == [{"name": "reach", "description": "x"}]  # passthrough preserved
    assert body["funder_notes"] == "notes"


def test_get_rubric_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/rubric")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_setup_status_member_sees_status_no_error(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):  # noqa: E501
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory, job_status="failed", last_error="boom")
    r = authed_client(app, user_id=member_id).get(f"/api/workspaces/{ws_id}/setup-status")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "failed", "last_error": None}  # member never sees the error


def test_setup_status_owner_sees_error(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, job_status="failed", last_error="boom")
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/setup-status")
    assert r.json() == {"status": "failed", "last_error": "boom"}


def test_setup_status_missing_job_is_queued(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, job_status=None)
    r = authed_client(app, user_id=owner_id).get(f"/api/workspaces/{ws_id}/setup-status")
    assert r.json()["status"] == "queued"


def test_setup_status_stranger_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, _, stranger_id = _setup(pg_session_factory)
    r = authed_client(app, user_id=stranger_id).get(f"/api/workspaces/{ws_id}/setup-status")
    assert r.status_code == 403
