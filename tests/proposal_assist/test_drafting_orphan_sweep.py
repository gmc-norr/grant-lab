from datetime import UTC, datetime

from sqlalchemy import select

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.drafting.orphan_sweep import (
    sweep_orphaned_turn_jobs,
)
from tests.proposal_assist.conftest import make_workspace


def test_sweep_marks_running_turn_jobs_as_failed(pg_session_factory):
    """A turn_jobs row left in status='running' for this workspace is
    transitioned to 'failed' with error='orphaned'. Returns the count
    of swept rows."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            TurnJob(
                workspace_id=ws.id,
                status="running",
                started_at=datetime.now(UTC),
            )
        )
        s.commit()
        wsid = ws.id

        swept = sweep_orphaned_turn_jobs(s, wsid)
        s.commit()

        assert swept == 1
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
        assert job.error == "orphaned"
        assert job.completed_at is not None


def test_sweep_leaves_other_statuses_alone(pg_session_factory):
    """queued / completed / failed / cancelled rows are not touched."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        for status in ("queued", "completed", "failed", "cancelled"):
            s.add(TurnJob(workspace_id=ws.id, status=status))
        s.commit()
        wsid = ws.id

        swept = sweep_orphaned_turn_jobs(s, wsid)
        s.commit()

        assert swept == 0
        # Verify each status row is unchanged.
        for status in ("queued", "completed", "failed", "cancelled"):
            row = s.scalar(
                select(TurnJob).where(
                    TurnJob.workspace_id == wsid,
                    TurnJob.status == status,
                )
            )
            assert row is not None, f"row with status {status} vanished"


def test_sweep_is_workspace_scoped(pg_session_factory):
    """A 'running' row on workspace_A doesn't affect a sweep on
    workspace_B."""
    from call_manager.db.models import User

    with pg_session_factory() as s:
        # Two workspaces — each with their own owner to avoid UNIQUE
        # email collisions on the default 'owner@ki.se'.
        owner_a = User(email="orphan-sweep-a@ki.se")
        owner_b = User(email="orphan-sweep-b@ki.se")
        s.add_all([owner_a, owner_b])
        s.flush()
        ws_a = make_workspace(s, owner=owner_a, title="A")
        ws_b = make_workspace(s, owner=owner_b, title="B")
        s.add(TurnJob(workspace_id=ws_a.id, status="running"))
        s.add(TurnJob(workspace_id=ws_b.id, status="running"))
        s.commit()
        a_id, b_id = ws_a.id, ws_b.id

        swept_b = sweep_orphaned_turn_jobs(s, b_id)
        s.commit()
        assert swept_b == 1

        # A's row is unchanged.
        a_row = s.scalar(select(TurnJob).where(TurnJob.workspace_id == a_id))
        assert a_row.status == "running"


def test_sweep_returns_zero_when_nothing_to_do(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        assert sweep_orphaned_turn_jobs(s, ws.id) == 0
