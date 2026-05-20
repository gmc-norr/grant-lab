"""Tests for POST /workspaces/{id}/turns/{tj}/cancel — B6 Task 8.

H1: queued turn_job is marked 'cancelled' with completed_at set.
H2: running (or completed/failed) turn returns a 'too late' response,
    row status unchanged.
H3: non-owner gets 403.
"""

from __future__ import annotations


def test_cancel_queued_turn_marks_it_cancelled(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        user_turn = ChatTurn(
            workspace_id=ws.id,
            role="user",
            content={"type": "text", "text": "hi"},
        )
        s.add(user_turn)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=user_turn.id,
            submitted_by_user_id=current_user.id,
            status="queued",
            queued_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns/{jid}/cancel",
    )
    assert response.status_code == 200
    assert b"cancelled" in response.content.lower()

    with pg_session_factory() as s:
        j = s.get(TurnJob, jid)
        assert j.status == "cancelled"
        assert j.completed_at is not None


def test_cancel_running_turn_is_noop(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """If the turn is already running, cancel is a no-op (returns
    a 'too late' message, status stays 'running')."""
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        user_turn = ChatTurn(
            workspace_id=ws.id,
            role="user",
            content={"type": "text", "text": "hi"},
        )
        s.add(user_turn)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=user_turn.id,
            submitted_by_user_id=current_user.id,
            status="running",
            queued_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
        )
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns/{jid}/cancel",
    )
    # 200 with a "too late" message; OR 404. Either is acceptable as
    # long as the row's status is unchanged.
    assert response.status_code in (200, 404)

    with pg_session_factory() as s:
        j = s.get(TurnJob, jid)
        assert j.status == "running"


def test_cancel_non_owner_403(
    client_authed_user,
    pg_session_factory,
    current_user,
):
    from datetime import UTC, datetime

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s, email="o@ki.se")
        ws = make_workspace(s, owner=owner)
        user_turn = ChatTurn(
            workspace_id=ws.id,
            role="user",
            content={"type": "text", "text": "hi"},
        )
        s.add(user_turn)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=user_turn.id,
            submitted_by_user_id=owner.id,
            status="queued",
            queued_at=datetime.now(UTC),
        )
        s.add(job)
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=current_user.id))
        s.commit()
        wsid, jid = ws.id, job.id

    response = client_authed_user.post(
        f"/workspaces/{wsid}/turns/{jid}/cancel",
    )
    assert response.status_code == 403
