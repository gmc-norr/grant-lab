import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import WorkspaceSetupJob
from tests.proposal_assist.conftest import make_workspace


def test_workspace_setup_job_insert_queued(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id))
        assert isinstance(job.id, uuid.UUID)
        assert job.status == "queued"
        assert job.attempts == 0


def test_workspace_setup_job_unique_per_workspace(pg_session_factory):
    """One setup job per workspace — UNIQUE on workspace_id."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_workspace_setup_job_status_check(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="not_a_status"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_workspace_setup_job_waiting_for_rubric_review_status_allowed(
    pg_session_factory,
):
    """The yield-to-user status is one of the 5 accepted values."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            WorkspaceSetupJob(
                workspace_id=ws.id,
                status="waiting_for_rubric_review",
            )
        )
        s.commit()  # should not raise
