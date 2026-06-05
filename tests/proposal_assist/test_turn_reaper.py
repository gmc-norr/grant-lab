from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from call_manager.db.models import TurnJob
from call_manager.proposal_assist.turns.reaper import (
    DEFAULT_REAP_TTL_SECONDS,
    reap_stuck_turns,
)
from tests.proposal_assist.conftest import make_workspace


def test_reaper_marks_old_running_turn_as_failed(pg_session_factory):
    """A turn_jobs row stuck status='running' with a started_at older than
    the TTL is transitioned to 'failed' so the workspace unwedges."""
    old = datetime.now(UTC) - timedelta(seconds=DEFAULT_REAP_TTL_SECONDS + 60)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="running", started_at=old))
        s.commit()
        wsid = ws.id

    reaped = reap_stuck_turns(session_factory=pg_session_factory)
    assert reaped == 1

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
        assert "reaped" in job.error
        assert job.completed_at is not None


def test_reaper_leaves_fresh_running_turn_alone(pg_session_factory):
    """A turn that is legitimately still streaming within the TTL is NOT
    reaped — started_at is recent."""
    fresh = datetime.now(UTC) - timedelta(seconds=30)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="running", started_at=fresh))
        s.commit()
        wsid = ws.id

    reaped = reap_stuck_turns(session_factory=pg_session_factory)
    assert reaped == 0

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "running"
        assert job.error is None


def test_reaper_ignores_non_running_and_null_started_at(pg_session_factory):
    """Old rows in other statuses, and running rows with NULL started_at,
    are left alone."""
    old = datetime.now(UTC) - timedelta(seconds=DEFAULT_REAP_TTL_SECONDS + 60)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        for status in ("queued", "completed", "failed", "cancelled"):
            s.add(TurnJob(workspace_id=ws.id, status=status, started_at=old))
        # Running but no started_at stamped yet (mid-claim race guard).
        s.add(TurnJob(workspace_id=ws.id, status="running", started_at=None))
        s.commit()
        wsid = ws.id

    reaped = reap_stuck_turns(session_factory=pg_session_factory)
    assert reaped == 0

    with pg_session_factory() as s:
        running = s.scalar(
            select(TurnJob).where(TurnJob.workspace_id == wsid, TurnJob.status == "running")
        )
        assert running is not None
        assert running.started_at is None


def test_reaper_respects_custom_ttl(pg_session_factory):
    """A custom (short) TTL reaps a row that the default TTL would spare."""
    age = datetime.now(UTC) - timedelta(seconds=120)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(TurnJob(workspace_id=ws.id, status="running", started_at=age))
        s.commit()
        wsid = ws.id

    # Default TTL (15 min) would NOT reap a 2-minute-old turn.
    assert reap_stuck_turns(session_factory=pg_session_factory) == 0
    # A 60s TTL does.
    assert reap_stuck_turns(session_factory=pg_session_factory, ttl_seconds=60) == 1

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.workspace_id == wsid))
        assert job.status == "failed"
