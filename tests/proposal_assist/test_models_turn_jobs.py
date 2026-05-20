import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import ChatTurn, TurnJob
from tests.proposal_assist.conftest import make_workspace


def test_turn_job_insert_queued(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        user_turn = ChatTurn(
            workspace_id=ws.id,
            role="user",
            content={"text": "Help."},
        )
        s.add(user_turn)
        s.flush()
        s.add(
            TurnJob(
                workspace_id=ws.id,
                user_turn_id=user_turn.id,
                status="queued",
            )
        )
        s.commit()
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == ws.id))
        assert isinstance(job.id, uuid.UUID)
        assert job.status == "queued"
        assert job.error is None
        assert job.user_turn_id == user_turn.id


def test_turn_job_status_check(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="not_a_status"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_turn_job_unique_running_per_workspace(pg_session_factory):
    """The partial UNIQUE INDEX on (workspace_id) WHERE status='running'
    enforces at-most-one-running turn per workspace."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="running"))
        s.commit()
        s.add(TurnJob(workspace_id=ws.id, status="running"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_turn_job_multiple_queued_allowed(pg_session_factory):
    """The unique constraint is partial — it only applies to status='running'.
    Multiple queued jobs per workspace are fine (the FIFO drain pattern)."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="queued"))
        s.add(TurnJob(workspace_id=ws.id, status="queued"))
        s.commit()  # should not raise


def test_turn_job_completed_does_not_conflict_with_running(pg_session_factory):
    """A completed job + a new running job on the same workspace is fine."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="completed"))
        s.add(TurnJob(workspace_id=ws.id, status="running"))
        s.commit()  # should not raise
