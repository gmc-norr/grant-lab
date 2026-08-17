"""Every deletion-cancellation path records the state it cancelled FROM.

Retention Slice C1 (spec 5.2.2). Restore maps the job back to a state that
progresses, and `cancelled` alone does not say which. There are THREE paths
that cancel a setup job on deletion; a restore that misses one silently
produces the permanently stuck workspace this column exists to prevent — the
setup page branches only on 'failed' and 'done' (WorkspaceSetup.tsx), so every
other status renders "Setting up…" forever.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

import call_manager.proposal_assist.jobs_retirement as jr
from call_manager.db.models import Workspace, WorkspaceSetupJob
from tests.proposal_assist.conftest import make_workspace


def _ws_with_job(pg_session_factory, *, status: str, generation: int = 1):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status=status))
        s.commit()
        ws_id = ws.id
        row = s.get(Workspace, ws_id)
        row.lifecycle_generation = generation
        row.soft_deleted_at = datetime.now(UTC)
        s.commit()
    return ws_id


def _job(pg_session_factory, ws_id) -> WorkspaceSetupJob:
    with pg_session_factory() as s:
        return s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one()


@pytest.mark.parametrize("source", ["queued", "running", "waiting_for_rubric_review"])
def test_the_retirement_hook_records_the_source_state(pg_session_factory, source):
    ws_id = _ws_with_job(pg_session_factory, status=source)
    with pg_session_factory() as s:
        jr.cancel_setup_jobs_for_workspace(s, ws_id, 1)
        s.commit()
    job = _job(pg_session_factory, ws_id)
    assert job.status == "cancelled"
    assert job.cancelled_from == source


@pytest.mark.parametrize("source", ["queued", "running", "waiting_for_rubric_review"])
def test_reconciliation_records_the_source_state(pg_session_factory, source):
    """The convergence path for rows the hook's SKIP LOCKED passed over. It
    cancels the same rows and must leave the same evidence, or a restore
    silently skips exactly the jobs that needed reconciling."""
    ws_id = _ws_with_job(pg_session_factory, status=source)
    with pg_session_factory() as s:
        jr.reconcile_cancelled_jobs(s)
        s.commit()
    job = _job(pg_session_factory, ws_id)
    assert job.status == "cancelled"
    assert job.cancelled_from == source


def test_a_cancelled_from_outside_the_three_states_is_rejected(pg_session_factory):
    """The CHECK, so the mapping in Task 4 cannot be fed a state it has no row
    for. 'done' is deliberately chosen: a real status, and one the mapping must
    never see."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(
            WorkspaceSetupJob(
                workspace_id=ws.id, status="cancelled", cancelled_from="done"
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_job_cancelled_by_nothing_carries_null(pg_session_factory):
    """NULL means "not cancelled by a deletion". Restore leaves those alone —
    a job cancelled some other way is not this feature's business."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
    assert _job(pg_session_factory, ws_id).cancelled_from is None
