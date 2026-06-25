"""Phase-0 setup gate (Theme 7 slice A) — post-Jinja migration.

While current_phase == 'setup': POST /turns is rejected (409), the main view
shows a static cockpit-redirect notice instead of the composer, and activation
is performed via JSON POST /api/workspaces/{id}/activate (the React path).
The superseded Jinja poll endpoint (GET /setup-status) and rubric-review form
(GET/POST /rubric-review) have been deleted in Slice E.
"""

from __future__ import annotations


def test_post_turn_during_setup_returns_409(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from sqlalchemy import select

    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True, current_phase="setup")
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "too early"},
    )
    assert response.status_code == 409
    assert "still setting up" in response.json()["detail"]
    with pg_session_factory() as s:
        assert s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid)) is None


def test_post_turn_after_setup_proceeds(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)  # active phase
        s.commit()
        wsid = ws.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns",
        data={"content": "hello"},
    )
    assert response.status_code in (200, 201, 303)


def _make_setup_workspace(pg_session_factory, owner, *, job_status=None, last_error=None):
    """A setup-phase workspace, optionally with a WorkspaceSetupJob row.

    make_workspace doesn't enqueue a setup job (the creation route does), so
    tests control the job row explicitly. job_status=None ⇒ no row.
    """
    from call_manager.db.models import WorkspaceSetupJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=owner, current_phase="setup")
        s.flush()
        if job_status is not None:
            s.add(
                WorkspaceSetupJob(
                    workspace_id=ws.id,
                    status=job_status,
                    last_error=last_error,
                )
            )
        s.commit()
        return ws.id


def test_main_view_during_setup_shows_card_not_composer(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert "Open it in the cockpit" in r.text
    assert f"/cockpit/{wsid}" in r.text
    assert 'id="composer"' not in r.text
    # Exact chip markup
    assert '<span class="phase-chip">Setting up</span>' in r.text


def test_main_view_waiting_shows_rubric_link(client_authed_owner, pg_session_factory, current_user):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="waiting_for_rubric_review"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert "Open it in the cockpit" in r.text
    assert f"/cockpit/{wsid}" in r.text
    assert 'id="composer"' not in r.text


def test_main_view_after_setup_shows_composer_not_card(
    client_authed_owner, pg_session_factory, current_user
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)  # active phase
        s.commit()
        wsid = ws.id
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="composer"' in r.text
    assert 'id="setup-status"' not in r.text
    assert '<span class="phase-chip">Active</span>' in r.text


def test_main_view_viewer_during_setup_sees_card_without_rubric_link(
    client_authed_user, pg_session_factory, current_user
):
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner2@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, owner, job_status="waiting_for_rubric_review")
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert "Open it in the cockpit" in r.text
    assert f"/cockpit/{wsid}" in r.text
    assert 'id="composer"' not in r.text


# --------------------------------------------------------------------------
# End-to-end closed loop: create → (Phase 0) → activate via /api → composer live.
# Stitches the whole setup lifecycle through the real HTTP routes: the gate
# rejects turns and hides the composer while in 'setup'; POSTing /api/activate
# advances to 'active'; the composer (and POST /turns) then go live.
# --------------------------------------------------------------------------


def test_create_confirm_composer_closed_loop(client_authed_owner, pg_session_factory, current_user):
    from sqlalchemy import select

    from call_manager.db.models import TurnJob, Workspace, WorkspaceSetupJob
    from tests.proposal_assist.conftest import make_workspace

    # Post-Phase-0 state: the setup worker has finished extraction and parked at
    # the rubric-review gate. (Phase 0 itself needs git + an LLM, so the test
    # constructs the state the worker leaves — same approach as the gate tests.)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True, current_phase="setup")
        ws.rubric_criteria = [
            {"name": "Excellence", "description": "x", "weight": 1.0, "scale_max": 5},
        ]
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="waiting_for_rubric_review"))
        s.commit()
        wsid = ws.id

    # 1. Gate engaged: cockpit notice shown, composer hidden, turns rejected.
    before = client_authed_owner.get(f"/workspaces/{wsid}")
    assert before.status_code == 200
    assert "Open it in the cockpit" in before.text
    assert f"/cockpit/{wsid}" in before.text
    assert 'id="composer"' not in before.text
    early = client_authed_owner.post(f"/workspaces/{wsid}/turns", data={"content": "too early"})
    assert early.status_code == 409

    # 2. Confirm the rubric via the JSON API → advance to 'active'.
    confirm = client_authed_owner.post(
        f"/api/workspaces/{wsid}/activate",
        json={
            "criteria": [
                {"name": "Excellence", "description": "x", "weight": 1.0, "scale_max": 5}
            ],
            "funder_notes": "",
        },
    )
    assert confirm.status_code == 200
    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        assert ws.current_phase == "active"
        job = s.execute(
            select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == wsid)
        ).scalar_one()
        assert job.status == "done"

    # 3. Gate lifted: composer shown, notice gone, turns accepted (TurnJob queued).
    after = client_authed_owner.get(f"/workspaces/{wsid}")
    assert after.status_code == 200
    assert 'id="composer"' in after.text
    assert 'id="setup-status"' not in after.text
    live = client_authed_owner.post(f"/workspaces/{wsid}/turns", data={"content": "hello"})
    assert live.status_code in (200, 201, 303)
    with pg_session_factory() as s:
        assert s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid)) is not None
