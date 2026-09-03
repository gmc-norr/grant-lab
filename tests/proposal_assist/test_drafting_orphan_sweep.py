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
        # email collisions on the default 'owner@umu.se'.
        owner_a = User(email="orphan-sweep-a@umu.se")
        owner_b = User(email="orphan-sweep-b@umu.se")
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


def test_orphan_sweep_writes_outcome_row_and_event(pg_session_factory):
    from sqlalchemy import select

    from call_manager.db.models import ChatTurn, TurnJob, WorkspaceEvent
    from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace

    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ut = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "hi"})
        s.add(TurnJob(workspace_id=ws.id, user_turn_id=ut.id, submitted_by_user_id=owner.id,
                      status="running", started_at=datetime.now(UTC)))
        s.commit()
        wsid = ws.id

        swept = sweep_orphaned_turn_jobs(s, wsid)
        s.commit()

        assert swept == 1
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
        row = s.scalar(
            select(ChatTurn).where(ChatTurn.role == "system", ChatTurn.workspace_id == wsid)
        )
        assert row.content["text"] == "This turn was interrupted. Please try again."
        event = s.scalar(
            select(WorkspaceEvent).where(WorkspaceEvent.kind == "cloud_turn_failed")
        )
        assert event is not None
