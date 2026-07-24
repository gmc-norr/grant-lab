"""Unit test for the extracted soft_delete_workspace service (shared by the
JSON DELETE /api/workspaces/{id} endpoint and the doomed Jinja route)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
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
        assert (
            s.scalar(select(CoherenceJob.status).where(CoherenceJob.workspace_id == ws_id))
            == "failed"
        )
        assert (
            s.scalar(select(EvaluatorJob.status).where(EvaluatorJob.workspace_id == ws_id))
            == "failed"
        )
        # NB: review's cancel helper sets "cancelled" (not "failed" like the other three).
        assert (
            s.scalar(select(ReviewRun.status).where(ReviewRun.workspace_id == ws_id))
            == "cancelled"
        )
        assert (
            s.scalar(
                select(UncitedClaimsJob.status).where(UncitedClaimsJob.workspace_id == ws_id)
            )
            == "failed"
        )
        assert (
            s.scalar(select(TurnJob.status).where(TurnJob.workspace_id == ws_id)) == "cancelled"
        )
        # emit_chat=False → audit event but NO system chat row.
        assert (
            s.scalar(
                select(WorkspaceEvent.id).where(WorkspaceEvent.kind == "cloud_turn_cancelled")
            )
            is not None
        )
        assert s.scalar(select(ChatTurn).where(ChatTurn.role == "system")) is None
    assert not attach_dir.exists()  # bytes scrubbed


def test_soft_delete_terminalizes_running_turn_and_finalize_is_noop(pg_session_factory, tmp_path):
    from call_manager.db.models import ChatTurn, TurnJob
    from call_manager.proposal_assist.turns.processor import _finalize_success
    from call_manager.workspaces import soft_delete_workspace
    from tests.proposal_assist.conftest import make_chat_turn, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = make_chat_turn(
            s, ws, role="user", content={"type": "text", "text": "q"}, tokens_in=1234
        )
        job = TurnJob(
            workspace_id=ws.id,
            status="running",
            user_turn_id=user_turn.id,
            submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id, ut_id = ws.id, job.id, user_turn.id

    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "running"  # terminalized in the delete txn
        assert s.get(ChatTurn, ut_id).tokens_in == 1234  # estimate retained

    # The in-flight processor now finalizes "successfully" — must be a NO-OP.
    _finalize_success(
        pg_session_factory,
        turn_job_id=job_id,
        workspace_id=ws_id,
        submitted_by_user_id=None,
        user_turn_id=ut_id,
        last_assistant_id=None,
        tokens_in=10,
        tokens_out=5,
        model="m",
        session_id=None,
    )

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "completed"  # never resurrected


def test_soft_delete_refuses_a_second_deletion(pg_session_factory, tmp_path):
    """The delete's own under-mutex liveness read: `ws` is loaded BEFORE we can
    block on the mutex, so a concurrent deletion that committed while we waited
    would be invisible to it. Sequential here; the concurrent form is the route
    test."""
    from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)
        s.rollback()


def test_soft_delete_propagates_commit_mutex_timeout_without_committing(
    pg_session_factory, tmp_path
):
    """A wedged writer holding the mutex must make the delete FAIL cleanly, not
    half-apply: nothing committed, the workspace still live."""
    import threading

    from call_manager.proposal_assist.drafting.locking import (
        CommitMutexTimeout,
        acquire_commit_mutex,
    )
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    try:
        holding.wait(timeout=5)
        with pg_session_factory() as s:
            with pytest.raises(CommitMutexTimeout):
                soft_delete_workspace(
                    s, workspace_id=ws_id, workspaces_dir=tmp_path, timeout_seconds=1
                )
            s.rollback()
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None  # nothing committed
