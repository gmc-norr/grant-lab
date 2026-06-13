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


# ---------------------------------------------------------------------------
# Compliance-chain audit-event tests (B6-residuals)
# ---------------------------------------------------------------------------


def test_cancel_writes_cloud_turn_cancelled_event(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """Successful cancel of a queued turn must write a cloud_turn_cancelled
    WorkspaceEvent so the cloud_turn_sent audit row has a matching outcome."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
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
        wsid, jid, uid = ws.id, job.id, current_user.id

    response = client_authed_owner.post(
        f"/workspaces/{wsid}/turns/{jid}/cancel",
    )
    assert response.status_code == 200
    assert b"cancelled" in response.content.lower()

    with pg_session_factory() as s:
        events = (
            s.execute(
                select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == wsid)
                .where(WorkspaceEvent.kind == "cloud_turn_cancelled")
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].actor_id == uid
        assert events[0].payload["turn_job_id"] == str(jid)


def test_cancel_writes_system_outcome_row(client_authed_owner, pg_session_factory, current_user):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        job = TurnJob(workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=current_user.id,
                      status="queued", queued_at=datetime.now(UTC))
        s.add(job)
        s.commit()
        wsid, jid = ws.id, job.id

    resp = client_authed_owner.post(f"/workspaces/{wsid}/turns/{jid}/cancel")
    assert resp.status_code == 200

    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "cancelled"
        row = s.scalar(
            select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == wsid)
        )
        assert row.content["outcome"] == "cancelled"
        assert row.content["text"] == "This turn was cancelled before it started."
        evt = s.scalar(
            select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_cancelled")
        )
        assert evt is not None


def test_cancel_noop_writes_no_event(
    client_authed_owner,
    pg_session_factory,
    current_user,
):
    """If the turn is already running/finished, no cloud_turn_cancelled
    event is written (the noop path must not pollute the audit log)."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
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
    assert response.status_code in (200, 404)

    with pg_session_factory() as s:
        events = (
            s.execute(
                select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == wsid)
                .where(WorkspaceEvent.kind == "cloud_turn_cancelled")
            )
            .scalars()
            .all()
        )
        assert len(events) == 0
