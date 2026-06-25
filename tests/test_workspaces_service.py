"""Unit test for the extracted soft_delete_workspace service (shared by the
JSON DELETE /api/workspaces/{id} endpoint and the doomed Jinja route)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import (
    ChatTurn,
    CoherenceJob,
    EvaluatorJob,
    ReviewRun,
    TurnJob,
    UncitedClaimsJob,
    Workspace,
    WorkspaceEvent,
)
from call_manager.workspaces import soft_delete_workspace
from tests.proposal_assist.conftest import make_workspace


def test_soft_delete_workspace_cascades_and_scrubs(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        # ReviewRun.draft_sha is NOT NULL with no server_default — seed it (matches
        # tests/proposal_assist/review/test_soft_delete_cancels_review.py).
        s.add(ReviewRun(workspace_id=ws.id, status="queued", draft_sha="a" * 64))
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="queued"))
        ut = ChatTurn(workspace_id=ws.id, role="user", content={"type": "text", "text": "hi"})
        s.add(ut)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=ut.id,
                submitted_by_user_id=ws.owner_id,
                status="queued",
                queued_at=datetime.now(UTC),
            )
        )
        s.commit()
        ws_id = ws.id

    # Seed an on-disk attachment byte tree so we can assert it is scrubbed.
    attach_dir = tmp_path / str(ws_id) / "attachments"
    attach_dir.mkdir(parents=True)
    (attach_dir / "blob.bin").write_bytes(b"x")

    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None
        assert s.scalar(select(CoherenceJob.status).where(CoherenceJob.workspace_id == ws_id)) == "failed"
        assert s.scalar(select(EvaluatorJob.status).where(EvaluatorJob.workspace_id == ws_id)) == "failed"
        # NB: review's cancel helper sets "cancelled" (not "failed" like the other three).
        assert s.scalar(select(ReviewRun.status).where(ReviewRun.workspace_id == ws_id)) == "cancelled"
        assert s.scalar(select(UncitedClaimsJob.status).where(UncitedClaimsJob.workspace_id == ws_id)) == "failed"
        assert s.scalar(select(TurnJob.status).where(TurnJob.workspace_id == ws_id)) == "cancelled"
        # emit_chat=False → audit event but NO system chat row.
        assert s.scalar(select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled")) is not None
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
    assert not attach_dir.exists()  # bytes scrubbed
