"""Phase-0 setup gate (Theme 7 slice A).

While current_phase == 'setup': POST /turns is rejected (409), the main view
shows the setup-status card instead of the composer, and the card self-polls
GET /workspaces/{id}/setup-status until the WorkspaceSetupJob is terminal.
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
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)  # spec phase
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
    tests control the job row explicitly. job_status=None ⇒ no row (the
    endpoint falls back to the polling 'queued' card).
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


def test_setup_status_running_polls_and_has_no_link(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert 'id="setup-status"' in r.text
    assert "hx-trigger" in r.text  # still polling
    assert "aria-busy" in r.text
    assert "/rubric-review" not in r.text


def test_setup_status_missing_job_falls_back_to_polling_card(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status=None)
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "hx-trigger" in r.text
    assert "Setting up" in r.text


def test_setup_status_waiting_links_rubric_and_stops_polling(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="waiting_for_rubric_review"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert f"/workspaces/{wsid}/rubric-review" in r.text
    assert "hx-trigger" not in r.text  # owner has an action: polling stops


def test_setup_status_failed_owner_sees_error_and_stops_polling(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="failed", last_error="boom: no rubric"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup failed" in r.text
    assert "boom: no rubric" in r.text  # owner sees diagnostics
    assert "hx-trigger" not in r.text
    assert "/rubric-review" not in r.text


def test_setup_status_done_links_back_to_workspace(
    client_authed_owner, pg_session_factory, current_user
):
    # Cross-tab race: rubric confirmed elsewhere while this card still polls.
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="done")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup is complete" in r.text
    assert f'href="/workspaces/{wsid}"' in r.text
    assert "hx-trigger" not in r.text


def test_setup_status_viewer_waiting_keeps_polling_without_link(
    client_authed_user, pg_session_factory, current_user
):
    # P2: a viewer has no rubric action, so the waiting card must KEEP polling
    # to catch the owner's confirm (otherwise it's permanently stale).
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, owner, job_status="waiting_for_rubric_review")
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "/rubric-review" not in r.text  # owner-only action
    assert "hx-trigger" in r.text  # still polling (P2)
    assert "Waiting for the owner" in r.text


def test_setup_status_failed_viewer_hidden_error(
    client_authed_user, pg_session_factory, current_user
):
    # P3b: raw worker exception text is owner-only; viewers get generic copy.
    from call_manager.db.models import WorkspaceViewer
    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        owner = make_user(s, email="gate-owner-f@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(
        pg_session_factory, owner, job_status="failed", last_error="Traceback: secret path /srv/x"
    )
    with pg_session_factory() as s:
        s.add(WorkspaceViewer(workspace_id=wsid, user_id=current_user.id))
        s.commit()
    r = client_authed_user.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 200
    assert "Setup failed" in r.text
    assert "secret path" not in r.text  # raw diagnostics hidden from viewers


def test_setup_status_non_member_403_and_missing_404(
    client_authed_owner, pg_session_factory, current_user
):
    import uuid as _uuid

    from tests.proposal_assist.conftest import make_user

    with pg_session_factory() as s:
        other = make_user(s, email="gate-other@ki.se")
        s.flush()
        s.commit()
    wsid = _make_setup_workspace(pg_session_factory, other, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}/setup-status")
    assert r.status_code == 403
    r = client_authed_owner.get(f"/workspaces/{_uuid.uuid4()}/setup-status")
    assert r.status_code == 404


def test_main_view_during_setup_shows_card_not_composer(
    client_authed_owner, pg_session_factory, current_user
):
    wsid = _make_setup_workspace(pg_session_factory, current_user, job_status="running")
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="setup-status"' in r.text
    assert 'id="composer"' not in r.text
    # Exact chip markup (the card ALSO says "Setting up", so assert the chip).
    assert '<span class="phase-chip">Setting up</span>' in r.text


def test_main_view_waiting_shows_rubric_link(client_authed_owner, pg_session_factory, current_user):
    wsid = _make_setup_workspace(
        pg_session_factory, current_user, job_status="waiting_for_rubric_review"
    )
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert f"/workspaces/{wsid}/rubric-review" in r.text
    assert 'id="composer"' not in r.text


def test_main_view_after_setup_shows_composer_not_card(
    client_authed_owner, pg_session_factory, current_user
):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)  # spec phase
        s.commit()
        wsid = ws.id
    r = client_authed_owner.get(f"/workspaces/{wsid}")
    assert r.status_code == 200
    assert 'id="composer"' in r.text
    assert 'id="setup-status"' not in r.text
    assert '<span class="phase-chip">Drafting</span>' in r.text


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
    assert 'id="setup-status"' in r.text
    assert "/rubric-review" not in r.text
