from datetime import UTC, datetime, timedelta

import pytest

from grant_lab.db.models import ClaimAnalysisJob, User, Workspace
from grant_lab.proposal_assist.analysis.queue import (
    claim_queued_jobs,
    enqueue_section_job,
    enqueue_uncovered_sections,
    mark_done,
    mark_failed,
    mark_superseded,
    requeue_stuck_running,
    touch_running_job,
)
from grant_lab.proposal_assist.editing.sectionizer import split_sections


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


def test_mark_failed_truncates_error(pg_session_factory):
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
        mark_failed(s, job, error="x" * 5000)
        s.commit()
        assert len(s.get(ClaimAnalysisJob, job.id).last_error) == 2000


def test_claim_respects_limit_and_order(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        # Same-transaction enqueues share queued_at (transaction now()); the
        # id tiebreaker keeps claim order == insertion order deterministically.
        for i, sha in enumerate(("a" * 64, "b" * 64, "c" * 64)):
            enqueue_section_job(
                s,
                workspace_id=ws.id,
                section_index=i,
                section_heading=str(i),
                section_sha=sha,
                commit_sha="c",
            )
        s.commit()
        first = claim_queued_jobs(s, limit=2)
        assert len(first) == 2
        assert [j.section_sha for j in first] == ["a" * 64, "b" * 64]
        s.commit()


def test_mark_failed_dead_letters_at_max_attempts(pg_session_factory):
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
        job.attempts = 3
        mark_failed(s, job, error="boom")
        s.commit()
        assert s.get(ClaimAnalysisJob, job.id).status == "failed"


def test_reenqueue_allowed_after_done(pg_session_factory):
    """Invalidation/reconcile depend on enqueueing content that already has a
    done row — the partial index must not block it."""
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
        mark_done(s, job)
        s.commit()
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
        s.commit()


def test_touch_running_job_refreshes_updated_at(pg_session_factory):
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
        touch_running_job(s, job.id)
        s.commit()
        assert requeue_stuck_running(s, older_than_minutes=15) == 0
        assert s.get(ClaimAnalysisJob, job.id).status == "running"


# --- enqueue_uncovered_sections (3b-5d.D3) --------------------------------- #
# Shared coverage loop: one job per uncovered non-empty section of a supplied
# markdown doc. Callers: the collab checkpoint hook (immediacy) and the
# reconcile tick (safety net).

# Empty preamble (section 0 is "") + Alpha + Beta + a byte-identical duplicate
# of Alpha (same text → same sha → must dedup to ONE job).
UNCOVERED_DOC = (
    "## Alpha\nAlpha makes a factual claim.\n"
    "## Beta\nBeta makes another claim.\n"
    "## Alpha\nAlpha makes a factual claim.\n"
)


def _live_unique_shas(doc):
    return {s.sha for s in split_sections(doc) if s.text.strip()}


def test_uncovered_sections_enqueued_once_each(pg_session_factory):
    """One job per unique non-empty section: the empty preamble is skipped,
    the duplicated Alpha section gets ONE row, the insert count is returned,
    and every row is queued pinned to the supplied commit_sha."""
    with pg_session_factory() as s:
        ws = _ws(s)
        n = enqueue_uncovered_sections(
            s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c1"
        )
        s.commit()
        assert n == 2  # Alpha, Beta — dup Alpha deduped, preamble skipped
        jobs = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws.id).all()
        assert {j.section_sha for j in jobs} == _live_unique_shas(UNCOVERED_DOC)
        assert all(j.status == "queued" and j.commit_sha == "c1" for j in jobs)


@pytest.mark.parametrize("status", ["queued", "running", "done"])
def test_covered_section_is_skipped(pg_session_factory, status):
    """covered = queued/running/done (_COVERED_STATUSES): a pre-existing job in
    any of those states keeps its section out; only Beta is enqueued."""
    alpha_sha = split_sections(UNCOVERED_DOC)[1].sha
    beta_sha = split_sections(UNCOVERED_DOC)[2].sha
    with pg_session_factory() as s:
        ws = _ws(s)
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=1,
                section_heading="Alpha",
                section_sha=alpha_sha,
                commit_sha="seed",
                status=status,
            )
        )
        s.flush()
        n = enqueue_uncovered_sections(
            s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c2"
        )
        assert n == 1
        new = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws.id, commit_sha="c2").all()
        assert [j.section_sha for j in new] == [beta_sha]


def test_failed_is_not_covered(pg_session_factory):
    """DELIBERATE SEMANTIC: FAILED is absent from _COVERED_STATUSES — a
    dead-lettered job's content gets a fresh row with a fresh attempt budget."""
    alpha_sha = split_sections(UNCOVERED_DOC)[1].sha
    beta_sha = split_sections(UNCOVERED_DOC)[2].sha
    with pg_session_factory() as s:
        ws = _ws(s)
        s.add(
            ClaimAnalysisJob(
                workspace_id=ws.id,
                section_index=1,
                section_heading="Alpha",
                section_sha=alpha_sha,
                commit_sha="seed",
                status="failed",
            )
        )
        s.flush()
        n = enqueue_uncovered_sections(
            s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c2"
        )
        assert n == 2  # Alpha re-enqueued despite the failed row, plus Beta
        new = s.query(ClaimAnalysisJob).filter_by(workspace_id=ws.id, commit_sha="c2").all()
        assert {j.section_sha for j in new} == {alpha_sha, beta_sha}


def test_second_identical_call_is_idempotent(pg_session_factory):
    with pg_session_factory() as s:
        ws = _ws(s)
        assert (
            enqueue_uncovered_sections(
                s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c1"
            )
            == 2
        )
        assert (
            enqueue_uncovered_sections(
                s, workspace_id=ws.id, doc=UNCOVERED_DOC, commit_sha="c1"
            )
            == 0
        )


def test_does_not_commit(pg_session_factory):
    """The helper stages inserts on the caller's transaction and never commits:
    rows are visible in-transaction and GONE after rollback."""
    with pg_session_factory() as s:
        ws = _ws(s)
        s.commit()  # persist the workspace so the enqueue is the only pending work
        ws_id = ws.id
        n = enqueue_uncovered_sections(
            s, workspace_id=ws_id, doc=UNCOVERED_DOC, commit_sha="c1"
        )
        assert n == 2
        assert s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).count() == 2
        s.rollback()
        assert s.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).count() == 0
