from datetime import UTC, datetime, timedelta

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob, WorkspaceEvent
from call_manager.proposal_assist.uncited_claims import jobs
from tests.proposal_assist.conftest import make_user, make_workspace


def test_enqueue_create_or_return_active(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        a, a_created = jobs.enqueue_uncited_claims_run(s, workspace_id=ws.id, requested_by=None)
        s.commit()
        b, b_created = jobs.enqueue_uncited_claims_run(s, workspace_id=ws.id, requested_by=None)
        s.commit()
        assert a.id == b.id  # returns the existing active job
        # created is True only on the insert; the dedup return is False, so the endpoint
        # writes the one-shot trigger event exactly once even under concurrent POSTs.
        assert a_created is True and b_created is False


def test_claim_picks_queued_skips_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="running"))
        ws2 = make_workspace(s, owner=make_user(s, email="ws2-owner@umu.se"))
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws2.id, status="queued"))
        s.commit()
        claimed = jobs.claim_next_uncited_claims_job(s)
        assert claimed is not None and claimed.workspace_id == ws2.id
        assert claimed.status == "running" and claimed.started_at is not None


def test_claim_skips_soft_deleted_workspace(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="queued"))
        s.commit()
        assert jobs.claim_next_uncited_claims_job(s) is None


def test_cancel_atomic_leaves_completed_untouched(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        done = UncitedClaimsJob(workspace_id=ws.id, status="completed", claims=[])
        running = UncitedClaimsJob(workspace_id=ws.id, status="running")
        s.add_all([done, running])
        s.commit()
        n = jobs.cancel_uncited_claims_jobs_for_workspace(s, workspace_id=ws.id)
        s.refresh(done)
        s.refresh(running)
        assert n == 1 and done.status == "completed" and running.status == "failed"


def test_reaper_fails_stale_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = UncitedClaimsJob(
            workspace_id=ws.id,
            status="running",
            started_at=datetime.now(UTC) - timedelta(seconds=600),
        )
        s.add(job)
        s.commit()
        n = jobs.reap_stuck_uncited_claims_jobs(s, timeout_seconds=300)
        s.refresh(job)
        assert n == 1 and job.status == "failed"


def test_cap_blocks_over_budget(pg_session_factory, _settings_env):
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(
            UncitedClaimsJob(
                workspace_id=ws.id,
                status="completed",
                input_tokens=settings.uncited_claims_daily_token_cap,
                output_tokens=0,
            )
        )
        s.commit()
        assert jobs.uncited_claims_cap_exceeded(s, workspace_id=ws.id, settings=settings) is True


def test_latest_completed_returns_completed_only(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="completed", claims=[]))
        s.commit()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="failed"))
        s.commit()
        job = jobs.latest_completed_uncited_claims_job(s, workspace_id=ws.id)
        assert job is not None and job.status == "completed"


def test_dismissed_hashes_reads_only_the_uncited_kind(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        # the feature's own dismissal events
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id, kind="uncited_claim_dismissed", payload={"claim_hash": "aaa"}
            )
        )
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id, kind="uncited_claim_dismissed", payload={"claim_hash": "bbb"}
            )
        )
        # a DIFFERENT feature's event in the same workspace — MUST be ignored
        s.add(
            WorkspaceEvent(
                workspace_id=ws.id,
                kind="user_claim_dismissed",
                payload={"annotation_id": "n1", "reason": "x"},
            )
        )
        s.commit()
        assert jobs.dismissed_claim_hashes(s, workspace_id=ws.id) == {"aaa", "bbb"}
