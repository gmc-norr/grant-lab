"""Phase-0 retirement: cancelled status, the jobs hook, and its reconciliation."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from call_manager.db.models import WorkspaceEvent, WorkspaceSetupJob
from call_manager.proposal_assist.phases.setup import claim_next_setup_job
from tests.proposal_assist.conftest import make_workspace

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0061_setup_job_cancelled_status.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig0061", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cancelled_is_an_accepted_status(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="cancelled")
        s.add(job)
        s.commit()
        assert s.get(WorkspaceSetupJob, job.id).status == "cancelled"


def test_only_one_rubric_initialized_event_per_workspace(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="rubric_initialized", payload={}))
        s.commit()
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="rubric_initialized", payload={}))
        with pytest.raises(Exception):  # noqa: B017
            s.commit()
        s.rollback()
    # A different kind is unaffected by the partial index.
    with pg_session_factory() as s:
        ws2 = make_workspace(s, owner=current_user)
        s.flush()
        s.add(WorkspaceEvent(workspace_id=ws2.id, kind="member_added", payload={}))
        s.add(WorkspaceEvent(workspace_id=ws2.id, kind="member_added", payload={}))
        s.commit()


def test_downgrade_converts_cancelled_rows_before_restoring_the_constraint(
    pg_session_factory, current_user
):
    """The ordering IS the finding: restoring the old CheckConstraint while a
    'cancelled' row exists fails. Runs the migration's own statements, so the
    test cannot drift from the migration."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="cancelled")
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        for stmt in _load_migration().downgrade_statements():
            s.execute(text(stmt))
        s.commit()
        assert s.get(WorkspaceSetupJob, job_id).status == "failed"


def test_a_deleted_workspaces_job_is_never_claimed(pg_session_factory, current_user):
    with pg_session_factory() as s:
        live = make_workspace(s, owner=current_user)
        dead = make_workspace(s, owner=current_user)
        dead.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=dead.id, status="queued"))
        s.commit()
        live_id = live.id

    # Only the deleted workspace has a queued job -> nothing claimable.
    with pg_session_factory() as s:
        assert claim_next_setup_job(session=s) is None
        s.commit()

    with pg_session_factory() as s:
        s.add(WorkspaceSetupJob(workspace_id=live_id, status="queued"))
        s.commit()

    with pg_session_factory() as s:
        job = claim_next_setup_job(session=s)
        assert job is not None
        assert job.workspace_id == live_id
        s.commit()
