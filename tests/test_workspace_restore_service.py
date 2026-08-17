"""Tests for restore_workspace_db in src/call_manager/workspaces.py.

The restore function is the DB half of workspace un-deletion: it clears
soft_deleted_at, allocates a lifecycle generation, repairs the setup job, and
writes the audit event — all in a single transaction under the commit mutex.

Setup jobs are NOT cancelled by soft_delete_workspace_db (that is done by the
async retirement hook). Tests that need a cancelled setup job create one directly
with status='cancelled' and cancelled_from=<source_state>.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from call_manager.db.models import (
    TurnJob,
    Workspace,
    WorkspaceEvent,
    WorkspaceSetupJob,
)
from call_manager.workspaces import restore_workspace_db
from tests.proposal_assist.conftest import make_user, make_workspace
from tests.workspace_delete_helper import soft_delete_workspace_for_test

# ---------------------------------------------------------------------------
# Test 1: Happy path — clears soft_deleted_at and increments generation
# ---------------------------------------------------------------------------


def test_restore_clears_soft_deleted_at_and_increments_generation(pg_session_factory):
    """Restoring a deleted workspace clears soft_deleted_at, increments generation
    by exactly 1, and returns transitioned=True. Both the RestoreOutcome and the DB
    are asserted."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.commit()
        ws_id = ws.id
        admin_id = owner.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    # Capture pre-restore generation.
    with pg_session_factory() as s:
        pre_gen = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    # Assert returned RestoreOutcome.
    assert outcome.transitioned is True
    assert outcome.generation == pre_gen + 1

    # Assert DB state.
    with pg_session_factory() as s:
        row = s.get(Workspace, ws_id)
        assert row.soft_deleted_at is None
        db_gen = row.lifecycle_generation
    assert db_gen == pre_gen + 1
    assert outcome.generation == db_gen


# ---------------------------------------------------------------------------
# Test 2: Parametrized setup job mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cancelled_from, expected_status, started_at_cleared",
    [
        ("queued", "queued", True),
        ("running", "queued", True),
        ("waiting_for_rubric_review", "waiting_for_rubric_review", False),
    ],
)
def test_the_setup_job_is_restored_to_a_state_that_progresses(
    pg_session_factory, cancelled_from, expected_status, started_at_cleared
):
    """_SETUP_RESTORE_MAP: each deletion-cancelled job is restored to a state
    that progresses. 'running' must map to 'queued' (not 'running'), because no
    worker will own it after restore — see spec 5.2.1."""
    started = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.flush()
        ws_id = ws.id
        admin_id = owner.id
        # Create a setup job pre-cancelled as if the retirement hook ran.
        # attempts=2: burned retries must survive the restore (spec 5.2.1).
        # started_at=started: preserved for waiting_for_rubric_review, cleared for queued.
        job = WorkspaceSetupJob(
            workspace_id=ws_id,
            status="cancelled",
            cancelled_from=cancelled_from,
            completed_at=datetime.now(UTC),
            last_error="cancelled: workspace deleted",
            started_at=started,
            attempts=2,
        )
        s.add(job)
        s.commit()
        job_id = job.id

    # Soft-delete the workspace (simulates the DB half of deletion).
    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    assert outcome.transitioned is True
    assert outcome.setup_job_restored_to == expected_status

    with pg_session_factory() as s:
        job_row = s.get(WorkspaceSetupJob, job_id)
        assert job_row.status == expected_status
        assert job_row.cancelled_from is None
        assert job_row.completed_at is None
        assert job_row.last_error is None
        assert (job_row.started_at is None) is started_at_cleared
        # NOT reset: a job that burned retries before the deletion does not get
        # a fresh budget for having been deleted and restored.
        assert job_row.attempts == 2


# ---------------------------------------------------------------------------
# Test 3: Job with cancelled_from=None is left alone
# ---------------------------------------------------------------------------


def test_cancelled_job_with_no_cancelled_from_is_left_alone(pg_session_factory):
    """A job with status='cancelled' but cancelled_from=None was not cancelled by
    a deletion, so restore leaves it untouched."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.flush()
        ws_id = ws.id
        admin_id = owner.id
        job = WorkspaceSetupJob(
            workspace_id=ws_id,
            status="cancelled",
            cancelled_from=None,  # not a deletion-cancel
            completed_at=datetime.now(UTC),
            last_error="cancelled: some other reason",
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    assert outcome.setup_job_restored_to is None

    with pg_session_factory() as s:
        job_row = s.get(WorkspaceSetupJob, job_id)
        assert job_row.status == "cancelled"
        assert job_row.cancelled_from is None


# ---------------------------------------------------------------------------
# Test 3b: Uncancelled running job (SKIP LOCKED survivor) is normalized to queued
# ---------------------------------------------------------------------------


def test_uncancelled_running_job_is_normalized_to_queued(pg_session_factory):
    """A setup job still 'running' with cancelled_from=None is an interrupted
    source: the retirement hook's SKIP LOCKED passed over a row a live worker held,
    and that worker died with the deletion. Restore treats it the same as a
    cancelled-from-running job: normalized to 'queued'.
    This is distinct from a *cancelled* job with cancelled_from=None, which is
    left alone (covered by test_cancelled_job_with_no_cancelled_from_is_left_alone).
    """
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.flush()
        ws_id = ws.id
        admin_id = owner.id
        job = WorkspaceSetupJob(
            workspace_id=ws_id,
            status="running",
            cancelled_from=None,
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    assert outcome.transitioned is True
    assert outcome.setup_job_restored_to == "queued"

    with pg_session_factory() as s:
        job_row = s.get(WorkspaceSetupJob, job_id)
        assert job_row.status == "queued"
        assert job_row.cancelled_from is None
        assert job_row.completed_at is None
        assert job_row.last_error is None
        assert job_row.started_at is None


# ---------------------------------------------------------------------------
# Test 4: Audit event committed in the same transaction
# ---------------------------------------------------------------------------


def test_audit_event_committed_in_same_transaction(pg_session_factory):
    """The admin_deleted_workspace_restored event is written in the same
    transaction as the flag clear — both committed atomically."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.commit()
        ws_id = ws.id
        admin_id = owner.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    with pg_session_factory() as s:
        restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    with pg_session_factory() as s:
        events = s.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "admin_deleted_workspace_restored",
            )
        ).scalars().all()

        assert len(events) == 1
        ev = events[0]
        assert ev.actor_id == admin_id
        assert ev.payload["workspace_id"] == str(ws_id)
        assert ev.payload["admin_user_id"] == admin_id

        # Confirm the flag is also cleared.
        row = s.get(Workspace, ws_id)
        assert row.soft_deleted_at is None


# ---------------------------------------------------------------------------
# Test 6: Failed commit leaves nothing committed
# ---------------------------------------------------------------------------


def test_failed_commit_leaves_nothing(pg_session_factory):
    """If the commit raises, the workspace stays deleted, no audit event is
    written, the generation is unchanged, and the setup job stays cancelled."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.flush()
        ws_id = ws.id
        admin_id = owner.id
        job = WorkspaceSetupJob(
            workspace_id=ws_id,
            status="cancelled",
            cancelled_from="queued",
            completed_at=datetime.now(UTC),
            last_error="cancelled: workspace deleted",
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    # Record generation after soft-delete.
    with pg_session_factory() as s:
        gen_before = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()
        deleted_at_before = s.execute(
            select(Workspace.soft_deleted_at).where(Workspace.id == ws_id)
        ).scalar_one_or_none()
    assert deleted_at_before is not None

    # Attempt restore with a failing commit.
    with pg_session_factory() as s, patch.object(
        s, "commit", side_effect=RuntimeError("forced")
    ), pytest.raises(RuntimeError, match="forced"):
        restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    # Nothing should have been persisted.
    with pg_session_factory() as s:
        row = s.get(Workspace, ws_id)
        assert row.soft_deleted_at is not None, "workspace should still be deleted"

        gen_after = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()
        assert gen_after == gen_before, "generation must not have incremented"

        event_count = s.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "admin_deleted_workspace_restored",
            )
        ).scalars().all()
        assert len(event_count) == 0, "no audit event should exist"

        job_row = s.get(WorkspaceSetupJob, job_id)
        assert job_row.status == "cancelled"
        assert job_row.cancelled_from == "queued"


# ---------------------------------------------------------------------------
# Test 7: Already-live workspace returns transitioned=False
# ---------------------------------------------------------------------------


def test_already_live_workspace_returns_transitioned_false(pg_session_factory):
    """Calling restore on a workspace that is already live is idempotent: it
    returns transitioned=False without writing an audit event or changing state."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.commit()
        ws_id = ws.id
        admin_id = owner.id

    with pg_session_factory() as s:
        gen_before = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    assert outcome.transitioned is False
    assert outcome.setup_job_restored_to is None
    assert outcome.generation == gen_before

    with pg_session_factory() as s:
        # No audit event.
        events = s.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "admin_deleted_workspace_restored",
            )
        ).scalars().all()
        assert len(events) == 0

        # soft_deleted_at still None.
        row = s.get(Workspace, ws_id)
        assert row.soft_deleted_at is None

        # Generation unchanged.
        gen_after = row.lifecycle_generation
    assert gen_after == gen_before


# ---------------------------------------------------------------------------
# Test 8: Cancelled TurnJobs stay cancelled after restore
# ---------------------------------------------------------------------------


def test_cancelled_turns_stay_cancelled(pg_session_factory):
    """Cancelled TurnJobs are not altered by restore — restore touches only
    setup jobs and workspace metadata."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.flush()
        ws_id = ws.id
        admin_id = owner.id
        from call_manager.db.models import ChatTurn

        ut = ChatTurn(
            workspace_id=ws_id,
            role="user",
            content={"type": "text", "text": "hello"},
        )
        s.add(ut)
        s.flush()
        tj = TurnJob(
            workspace_id=ws_id,
            user_turn_id=ut.id,
            submitted_by_user_id=owner.id,
            status="queued",
            queued_at=datetime.now(UTC),
        )
        s.add(tj)
        s.commit()
        tj_id = tj.id

    # soft_delete_workspace_for_test cancels queued turn jobs.
    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    assert outcome.transitioned is True

    with pg_session_factory() as s:
        tj_row = s.get(TurnJob, tj_id)
        assert tj_row.status == "cancelled", "cancelled TurnJob must remain cancelled"


# ---------------------------------------------------------------------------
# Test 8: Returned generation is the one bump_lifecycle_generation produced
# ---------------------------------------------------------------------------


def test_returned_generation_is_the_one_bump_produced(pg_session_factory):
    """The generation in RestoreOutcome is exactly what bump_lifecycle_generation
    returned, not a re-read. Mocking it to return a sentinel proves the wiring."""
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        s.commit()
        ws_id = ws.id
        admin_id = owner.id

    with pg_session_factory() as s:
        soft_delete_workspace_for_test(s, workspace_id=ws_id)

    with pg_session_factory() as s, patch(
        "call_manager.workspaces.bump_lifecycle_generation", return_value=4242
    ):
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    assert outcome.generation == 4242
