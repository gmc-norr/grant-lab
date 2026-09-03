import uuid
from datetime import UTC, datetime, timedelta

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, ProposedChange
from call_manager.proposal_assist.coherence import jobs
from tests.proposal_assist.conftest import make_user, make_workspace


def _pending_coherence(s, ws_id, job_id, section=0):
    pc = ProposedChange(
        workspace_id=ws_id,
        target_artifact="draft",
        section_index=section,
        base_commit_sha="deadbeef",
        base_section_sha="cafe",
        kind="prose_edit",
        payload={"new_text": "x"},
        origin_kind="coherence_session",
        origin_job_id=job_id,
        status="pending",
    )
    s.add(pc)
    s.flush()
    return pc


def test_enqueue_create_or_return_active(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        u = make_user(s, email="req@umu.se")
        s.flush()
        first = jobs.enqueue_coherence_run(s, workspace_id=ws.id, requested_by=u.id)
        s.flush()
        again = jobs.enqueue_coherence_run(s, workspace_id=ws.id, requested_by=u.id)
        assert again.id == first.id  # active job returned, not stacked
        assert s.query(CoherenceJob).filter_by(workspace_id=ws.id).count() == 1


def test_claim_skips_workspace_with_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="running"))
        # Different workspace has a queued job that SHOULD be claimable.
        # Distinct owner: make_user's default email is fixed (not unique-per-call),
        # so a second make_workspace(s) with the default owner UNIQUE-collides.
        ws2 = make_workspace(s, owner=make_user(s, email="ws2-owner@umu.se"))
        s.flush()
        q = CoherenceJob(workspace_id=ws2.id, status="queued")
        s.add(q)
        s.flush()
        claimed = jobs.claim_next_coherence_job(s)
        assert claimed is not None and claimed.workspace_id == ws2.id
        assert claimed.status == "running" and claimed.started_at is not None


def test_supersede_success_targets_prior_only(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        old_job, new_job = uuid.uuid4(), uuid.uuid4()
        prior = _pending_coherence(s, ws.id, old_job, section=0)
        current = _pending_coherence(s, ws.id, new_job, section=1)
        jobs.supersede_prior_coherence(s, workspace_id=ws.id, job_id=new_job)
        s.refresh(prior)
        s.refresh(current)
        assert prior.status == "superseded"
        assert current.status == "pending"


def test_failure_cleanup_targets_own_only(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        old_job, this_job = uuid.uuid4(), uuid.uuid4()
        prior = _pending_coherence(s, ws.id, old_job, section=0)
        mine = _pending_coherence(s, ws.id, this_job, section=1)
        jobs.supersede_own_coherence(s, workspace_id=ws.id, job_id=this_job)
        s.refresh(prior)
        s.refresh(mine)
        assert mine.status == "superseded"
        assert prior.status == "pending"  # prior assessment preserved


def test_daily_cap_blocks_over_budget(pg_session_factory, _settings_env):
    # _settings_env populates the env vars Settings() requires (DATABASE_URL etc).
    settings = Settings()
    settings.coherence_daily_token_cap = 100
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(
            CoherenceJob(workspace_id=ws.id, status="completed", input_tokens=80, output_tokens=40)
        )
        s.flush()
        assert jobs.coherence_cap_exceeded(s, workspace_id=ws.id, settings=settings) is True


def test_reaper_fails_stale_running_and_supersedes_its_rows(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        stale = CoherenceJob(
            workspace_id=ws.id,
            status="running",
            started_at=datetime.now(UTC) - timedelta(seconds=999),
        )
        s.add(stale)
        s.flush()
        partial = _pending_coherence(s, ws.id, stale.id)
        n = jobs.reap_stuck_coherence_jobs(s, timeout_seconds=300)
        s.refresh(stale)
        s.refresh(partial)
        assert n == 1 and stale.status == "failed"
        assert partial.status == "superseded"


def test_claim_skips_soft_deleted_workspace(pg_session_factory):
    from datetime import UTC
    from datetime import datetime as _dt

    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.soft_deleted_at = _dt.now(UTC)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        s.flush()
        assert jobs.claim_next_coherence_job(s) is None  # deleted ws not claimable


def test_cancel_for_workspace_fails_active_and_supersedes(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        q = CoherenceJob(workspace_id=ws.id, status="queued")
        s.add(q)
        s.flush()
        partial = _pending_coherence(s, ws.id, q.id)
        n = jobs.cancel_coherence_jobs_for_workspace(s, workspace_id=ws.id)
        s.refresh(q)
        s.refresh(partial)
        assert n == 1 and q.status == "failed"
        assert partial.status == "superseded"


def test_cancel_leaves_completed_job_and_its_proposals_untouched(pg_session_factory):
    # The atomic UPDATE ... WHERE status IN ('queued','running') guard is what makes
    # cancel race-safe against the success finalizer: a job already 'completed' must
    # NOT be flipped to failed nor have its (now-pending, member-visible) proposals
    # superseded. (A plain ORM load-then-write keyed on id would clobber it.)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        done = CoherenceJob(workspace_id=ws.id, status="completed")
        running = CoherenceJob(workspace_id=ws.id, status="running")
        s.add_all([done, running])
        s.flush()
        done_row = _pending_coherence(s, ws.id, done.id, section=0)
        running_row = _pending_coherence(s, ws.id, running.id, section=1)
        n = jobs.cancel_coherence_jobs_for_workspace(s, workspace_id=ws.id)
        s.refresh(done)
        s.refresh(running)
        s.refresh(done_row)
        s.refresh(running_row)
        # only the running job is cancelled; the completed one and its proposals survive
        assert n == 1
        assert done.status == "completed"
        assert done_row.status == "pending"
        assert running.status == "failed"
        assert running_row.status == "superseded"
