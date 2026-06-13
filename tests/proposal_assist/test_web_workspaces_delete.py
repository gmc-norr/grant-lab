"""Tests for POST /workspaces/{id}/delete — B1 Task 9.

Verifies that soft-delete routes queued turns through record_terminal_outcome
as 'cancelled' (owner-initiated, never started) with emit_chat=False, so each
queued turn gets a cloud_turn_cancelled audit event but NO system chat row.
"""

from __future__ import annotations


def test_soft_delete_cancels_queued_turn_audit_only(
    client_authed_owner, pg_session_factory, current_user
):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, Workspace, WorkspaceEvent
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

    resp = client_authed_owner.post(f"/workspaces/{wsid}/delete", follow_redirects=False)
    assert resp.status_code in (200, 303)

    with pg_session_factory() as s:
        assert s.get(Workspace, wsid).soft_deleted_at is not None
        assert s.get(TurnJob, jid).status == "cancelled"  # not 'failed'
        # audit event closes the cloud_turn_sent chain, but NO viewer row
        evt = s.scalar(select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_cancelled"))
        assert evt is not None
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
