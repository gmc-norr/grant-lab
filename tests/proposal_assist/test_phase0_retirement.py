"""Phase-0 retirement: cancelled status, the jobs hook, and its reconciliation."""

from datetime import UTC, datetime

import pytest
from alembic.config import Config

from alembic import command
from call_manager.db.models import WorkspaceEvent, WorkspaceSetupJob
from call_manager.proposal_assist.phases.setup import claim_next_setup_job
from tests.proposal_assist.conftest import make_workspace


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
    pg_session_factory, current_user, pg_url
):
    """The ordering IS the finding: restoring the old CheckConstraint while a
    'cancelled' row exists fails. Runs the migration's own statements (via
    `command.downgrade`, which calls 0061's `downgrade()`, which iterates
    `downgrade_statements()`), so the test cannot drift from the migration.

    `pg_session_factory` is session-scoped-DB-backed: the downgrade mutates
    the shared schema (drops the CHECK constraint's 'cancelled' branch and the
    partial unique index), so every later test in this pytest session would
    see the corrupted schema unless it's restored. The try/finally guarantees
    restoration to head even if the assertion below fails (mirrors the
    precedent in test_migration_0050_magic_link.py).
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job = WorkspaceSetupJob(workspace_id=ws.id, status="cancelled")
        s.add(job)
        s.commit()
        job_id = job.id

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    try:
        command.downgrade(cfg, "0060")
        with pg_session_factory() as s:
            assert s.get(WorkspaceSetupJob, job_id).status == "failed"
    finally:
        command.upgrade(cfg, "head")


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
