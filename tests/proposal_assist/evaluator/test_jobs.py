from datetime import UTC, datetime, timedelta

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob
from call_manager.proposal_assist.evaluator import jobs
from tests.proposal_assist.conftest import make_user, make_workspace


def test_enqueue_create_or_return_active(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        a = jobs.enqueue_evaluator_run(s, workspace_id=ws.id, requested_by=None)
        s.commit()
        b = jobs.enqueue_evaluator_run(s, workspace_id=ws.id, requested_by=None)
        s.commit()
        assert a.id == b.id  # returns the existing active job


def test_claim_skips_running_and_picks_a_queued_in_another_workspace(pg_session_factory):
    # The active-unique index forbids queued+running in ONE workspace, so use two:
    # ws has only a running job (not claimable); ws2 has a queued job (claimable).
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="running"))
        ws2 = make_workspace(s, owner=make_user(s, email="ws2-owner@umu.se"))
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws2.id, status="queued"))
        s.commit()
        claimed = jobs.claim_next_evaluator_job(s)
        assert claimed is not None and claimed.workspace_id == ws2.id
        assert claimed.status == "running" and claimed.started_at is not None


def test_claim_returns_none_when_only_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="running"))
        s.commit()
        assert jobs.claim_next_evaluator_job(s) is None


def test_claim_skips_soft_deleted_workspace(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        s.commit()
        assert jobs.claim_next_evaluator_job(s) is None


def test_cancel_atomic_leaves_completed_untouched(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        done = EvaluatorJob(workspace_id=ws.id, status="completed", scorecard={"criteria": []})
        running = EvaluatorJob(workspace_id=ws.id, status="running")
        s.add_all([done, running])
        s.commit()
        n = jobs.cancel_evaluator_jobs_for_workspace(s, workspace_id=ws.id)
        s.refresh(done)
        s.refresh(running)
        assert n == 1
        assert done.status == "completed"  # not clobbered
        assert running.status == "failed"


def test_reaper_fails_stale_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = EvaluatorJob(
            workspace_id=ws.id,
            status="running",
            started_at=datetime.now(UTC) - timedelta(seconds=600),
        )
        s.add(job)
        s.commit()
        n = jobs.reap_stuck_evaluator_jobs(s, timeout_seconds=300)
        s.refresh(job)
        assert n == 1 and job.status == "failed"


def test_cap_blocks_over_budget(pg_session_factory, _settings_env):
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(
            EvaluatorJob(
                workspace_id=ws.id,
                status="completed",
                input_tokens=settings.evaluator_daily_token_cap,
                output_tokens=0,
            )
        )
        s.commit()
        assert jobs.evaluator_cap_exceeded(s, workspace_id=ws.id, settings=settings) is True


def test_latest_completed_returns_completed_only(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="completed", scorecard={"criteria": []}))
        s.commit()
        s.add(EvaluatorJob(workspace_id=ws.id, status="failed"))
        s.commit()
        job = jobs.latest_completed_evaluator_job(s, workspace_id=ws.id)
        assert job is not None and job.status == "completed"
