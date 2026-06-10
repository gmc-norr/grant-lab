from datetime import UTC, datetime, timedelta

from call_manager.db.models import ClaimAnalysisJob, User, Workspace
from call_manager.proposal_assist.analysis.queue import (
    claim_queued_jobs,
    enqueue_section_job,
    mark_done,
    mark_failed,
    mark_superseded,
    requeue_stuck_running,
)


def _ws(session):
    user = User(email="o@example.org", is_admin=False)
    session.add(user)
    session.flush()
    ws = Workspace(owner_id=user.id, title="t")
    session.add(ws)
    session.flush()
    return ws


def test_enqueue_dedups_on_content(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        assert (
            enqueue_section_job(
                s,
                workspace_id=ws.id,
                section_index=1,
                section_heading="A",
                section_sha="a" * 64,
                commit_sha="c1",
            )
            is True
        )
        assert (
            enqueue_section_job(
                s,
                workspace_id=ws.id,
                section_index=2,
                section_heading="A2",
                section_sha="a" * 64,
                commit_sha="c2",
            )
            is False
        )  # same content → dedup
        assert (
            enqueue_section_job(
                s,
                workspace_id=ws.id,
                section_index=1,
                section_heading="A",
                section_sha="b" * 64,
                commit_sha="c2",
            )
            is True
        )  # new content → new job
        s.commit()
        assert s.query(ClaimAnalysisJob).count() == 2


def test_claim_marks_running_and_terminal_states(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(
            s,
            workspace_id=ws.id,
            section_index=0,
            section_heading="",
            section_sha="a" * 64,
            commit_sha="c",
        )
        enqueue_section_job(
            s,
            workspace_id=ws.id,
            section_index=1,
            section_heading="B",
            section_sha="b" * 64,
            commit_sha="c",
        )
        s.commit()
        jobs = claim_queued_jobs(s, limit=10)
        assert {j.status for j in jobs} == {"running"}
        mark_done(s, jobs[0])
        mark_failed(s, jobs[1], error="boom")  # attempts=1 < 3 → back to queued
        s.commit()
        statuses = sorted(j.status for j in s.query(ClaimAnalysisJob))
        assert statuses == ["done", "queued"]
        failed_job = s.query(ClaimAnalysisJob).filter_by(status="queued").one()
        assert failed_job.last_error == "boom"


def test_mark_failed_supersedes_on_duplicate_queued(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(
            s,
            workspace_id=ws.id,
            section_index=0,
            section_heading="",
            section_sha="a" * 64,
            commit_sha="c",
        )
        s.commit()
        (job,) = claim_queued_jobs(s, limit=1)
        s.flush()
        # same content re-enqueued while job is running
        assert (
            enqueue_section_job(
                s,
                workspace_id=ws.id,
                section_index=0,
                section_heading="",
                section_sha="a" * 64,
                commit_sha="c2",
            )
            is True
        )
        mark_failed(s, job, error="boom")
        s.commit()
        assert s.get(ClaimAnalysisJob, job.id).status == "superseded"


def test_mark_superseded(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(
            s,
            workspace_id=ws.id,
            section_index=0,
            section_heading="",
            section_sha="a" * 64,
            commit_sha="c",
        )
        s.commit()
        (job,) = claim_queued_jobs(s, limit=1)
        mark_superseded(s, job)
        s.commit()
        assert s.get(ClaimAnalysisJob, job.id).status == "superseded"


def test_requeue_stuck_running_bumps_attempts(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(
            s,
            workspace_id=ws.id,
            section_index=0,
            section_heading="",
            section_sha="a" * 64,
            commit_sha="c",
        )
        s.commit()
        (job,) = claim_queued_jobs(s, limit=1)
        job.updated_at = datetime.now(UTC) - timedelta(minutes=20)
        s.commit()
        n = requeue_stuck_running(s, older_than_minutes=15)
        s.commit()
        assert n == 1
        refreshed = s.get(ClaimAnalysisJob, job.id)
        assert refreshed.status == "queued"
        assert refreshed.attempts == 2  # 1 from claim + 1 from reap


def test_reaper_dead_letters_exhausted_job(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(
            s,
            workspace_id=ws.id,
            section_index=0,
            section_heading="",
            section_sha="a" * 64,
            commit_sha="c",
        )
        s.commit()
        (job,) = claim_queued_jobs(s, limit=1)
        job.attempts = 2  # next reap bump reaches MAX_ATTEMPTS=3
        job.updated_at = datetime.now(UTC) - timedelta(minutes=20)
        s.commit()
        assert requeue_stuck_running(s, older_than_minutes=15) == 0
        s.commit()
        assert s.get(ClaimAnalysisJob, job.id).status == "failed"


def test_reaper_supersedes_when_content_requeued(pg_session_factory):
    """A queued row for the same content already exists (invalidation
    re-enqueued) → reaping must not violate the partial unique index."""
    with pg_session_factory() as s:
        ws = _ws(s)
        enqueue_section_job(
            s,
            workspace_id=ws.id,
            section_index=0,
            section_heading="",
            section_sha="a" * 64,
            commit_sha="c",
        )
        s.commit()
        (job,) = claim_queued_jobs(s, limit=1)
        job.updated_at = datetime.now(UTC) - timedelta(minutes=20)
        enqueue_section_job(
            s,
            workspace_id=ws.id,
            section_index=0,
            section_heading="",
            section_sha="a" * 64,
            commit_sha="c2",
        )
        s.commit()
        assert requeue_stuck_running(s, older_than_minutes=15) == 0
        s.commit()
        assert s.get(ClaimAnalysisJob, job.id).status == "superseded"
