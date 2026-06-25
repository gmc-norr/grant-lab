"""POST /api/workspaces/{id}/turns/{job}/cancel — owner-only queued-turn cancel."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed_queued_turn(s, ws, user_id):
    ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
    s.add(ut)
    s.flush()
    job = TurnJob(
        workspace_id=ws.id,
        user_turn_id=ut.id,
        submitted_by_user_id=user_id,
        status="queued",
        queued_at=datetime.now(UTC),
    )
    s.add(job)
    s.flush()
    return job.id


def test_owner_cancels_queued_turn(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        jid = _seed_queued_turn(s, ws, current_user.id)
        s.commit()
        ws_id, jid = ws.id, jid
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/turns/{jid}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is True
    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "cancelled"
        assert (
            s.scalar(select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled"))
            is not None
        )
        # Interactive cancel emits a visible turn_outcome system row (emit_chat=True).
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is not None


def test_cancel_running_turn_is_too_late(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        jid = _seed_queued_turn(s, ws, current_user.id)
        s.get(TurnJob, jid).status = "running"  # already started
        s.commit()
        ws_id, jid = ws.id, jid
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/turns/{jid}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is False
    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "running"  # unchanged


def test_non_owner_cancel_403(client_authed_owner, current_user, pg_session_factory):
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger2@ki.se")
        ws = make_workspace(s, owner=stranger)
        jid = _seed_queued_turn(s, ws, stranger.id)
        s.commit()
        ws_id, jid = ws.id, jid
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/turns/{jid}/cancel")
    assert r.status_code == 403
    with pg_session_factory() as s:
        assert s.get(TurnJob, jid).status == "queued"  # untouched


def test_cross_workspace_job_id_no_op(client_authed_owner, current_user, pg_session_factory):
    # A job belonging to workspace B cannot be cancelled via workspace A (both owned
    # by the caller) — record_terminal_outcome's workspace_id predicate guards it.
    with pg_session_factory() as s:
        ws_a = make_workspace(s, owner=current_user)
        ws_b = make_workspace(s, owner=current_user)
        jid_b = _seed_queued_turn(s, ws_b, current_user.id)
        s.commit()
        ws_a_id, jid_b = ws_a.id, jid_b
    r = client_authed_owner.post(f"/api/workspaces/{ws_a_id}/turns/{jid_b}/cancel")
    assert r.status_code == 200
    assert r.json()["cancelled"] is False
    with pg_session_factory() as s:
        assert s.get(TurnJob, jid_b).status == "queued"  # B's job untouched
