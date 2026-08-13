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


def test_put_rubric_saves_and_preserves_subcriteria_and_bumps(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):  # noqa: E501
    from sqlalchemy import func, select

    from call_manager.db.models import Workspace, WorkspaceEvent

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, phase="active")
    with pg_session_factory() as s:
        before = s.get(Workspace, ws_id).rubric_updated_at
    body = {
        "criteria": [
            {
                "name": "Impact!",
                "description": "d2",
                "weight": 0.7,
                "scale_max": 5,
                "subcriteria": [{"name": "reach", "description": "x"}],
            },
            {"name": "Feasibility", "description": "d", "weight": 0.4, "scale_max": 5},
        ],
        "funder_notes": "notes",
    }
    r = authed_client(app, user_id=owner_id).put(f"/api/workspaces/{ws_id}/rubric", json=body)
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.rubric_criteria[0]["name"] == "Impact!"
        assert ws.rubric_criteria[0]["subcriteria"] == [{"name": "reach", "description": "x"}]
        assert ws.rubric_updated_at > before  # changed → bumped
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "rubric_edited")
            )
            == 1
        )


def test_put_rubric_unchanged_is_noop(monkeypatch, pg_url, tmp_path, pg_session_factory):
    """Round-tripping the current rubric (incl. subcriteria) writes no event."""
    from sqlalchemy import func, select

    from call_manager.db.models import Workspace, WorkspaceEvent

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, phase="active")
    with pg_session_factory() as s:
        current = list(s.get(Workspace, ws_id).rubric_criteria)
    r = authed_client(app, user_id=owner_id).put(
        f"/api/workspaces/{ws_id}/rubric", json={"criteria": current, "funder_notes": "notes"}
    )
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "rubric_edited")
            )
            == 0
        )


def test_put_rubric_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory, phase="active")
    r = authed_client(app, user_id=member_id).put(
        f"/api/workspaces/{ws_id}/rubric",
        json={
            "criteria": [{"name": "A", "description": "", "weight": 1, "scale_max": 5}],
            "funder_notes": None,
        },
    )
    assert r.status_code == 403


def test_put_rubric_validation_400(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, phase="active")
    c = authed_client(app, user_id=owner_id)
    base = {"name": "A", "description": "", "weight": 1.0, "scale_max": 5}
    url = f"/api/workspaces/{ws_id}/rubric"
    fn = {"funder_notes": None}
    assert c.put(url, json={"criteria": [], **fn}).status_code == 400
    assert c.put(url, json={"criteria": [{**base, "name": "  "}], **fn}).status_code == 400
    assert c.put(url, json={"criteria": [{**base, "weight": -1}], **fn}).status_code == 400
    assert c.put(url, json={"criteria": [{**base, "scale_max": 0}], **fn}).status_code == 400
    r = c.put(url, json={"criteria": [], "funder_notes": None})
    assert r.json()["error"]["code"] == "bad_request"


def test_activate_owner_advances_phase_and_audits(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):  # noqa: E501
    from sqlalchemy import select

    from call_manager.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory)  # setup phase, waiting job
    with pg_session_factory() as s:
        current = list(s.get(Workspace, ws_id).rubric_criteria)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/activate", json={"criteria": current, "funder_notes": "notes"}
    )
    assert r.status_code == 200, r.text
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        assert ws.current_phase == "active"
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        assert job.status == "done" and job.completed_at is not None
        activated = s.scalar(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "workspace_activated"
            )
        )
        assert activated is not None
        # unedited activate wrote NO rubric_edited (subcriteria round-tripped)
        assert (
            s.scalar(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == ws_id, WorkspaceEvent.kind == "rubric_edited"
                )
            )
            is None
        )


def test_activate_when_not_waiting_409(monkeypatch, pg_url, tmp_path, pg_session_factory):
    from call_manager.db.models import Workspace

    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, owner_id, _, _ = _setup(pg_session_factory, phase="active", job_status="done")
    with pg_session_factory() as s:
        current = list(s.get(Workspace, ws_id).rubric_criteria)
    r = authed_client(app, user_id=owner_id).post(
        f"/api/workspaces/{ws_id}/activate", json={"criteria": current, "funder_notes": "notes"}
    )
    assert r.status_code == 409


def test_activate_member_403(monkeypatch, pg_url, tmp_path, pg_session_factory):
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/activate",
        json={
            "criteria": [{"name": "A", "description": "", "weight": 1, "scale_max": 5}],
            "funder_notes": None,
        },
    )
    assert r.status_code == 403


def test_put_rubric_authorization_precedes_validation(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """A non-owner sending an INVALID rubric must get 403, not 400. Validating
    first tells someone with no access that their body shape was wrong."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory, phase="active")
    r = authed_client(app, user_id=member_id).put(
        f"/api/workspaces/{ws_id}/rubric",
        json={"criteria": [], "funder_notes": None},  # empty criteria -> 400 if validated first
    )
    assert r.status_code == 403, (
        f"expected 403 (authorization decides first), got {r.status_code}: {r.text}"
    )
