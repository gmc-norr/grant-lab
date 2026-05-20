from unittest.mock import Mock

from sqlalchemy import select

from call_monitor.db.models import WorkspaceSetupJob
from call_monitor.proposal_assist.phases.setup import (
    Phase0Worker,
    claim_next_setup_job,
)
from tests.proposal_assist.conftest import make_workspace


def test_claim_next_returns_oldest_queued(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        job = WorkspaceSetupJob(workspace_id=ws.id, status="queued")
        s.add(job)
        s.commit()
        claimed = claim_next_setup_job(session=s)
        s.commit()
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == "running"
        assert claimed.started_at is not None


def test_claim_next_returns_none_when_nothing_queued(pg_session_factory):
    with pg_session_factory() as s:
        assert claim_next_setup_job(session=s) is None


def test_phase0_worker_transitions_to_done_on_success(
    pg_session_factory,
    monkeypatch,
    tmp_path,
):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id

        worker = Phase0Worker(
            session=s,
            client=Mock(model="claude-opus-4-7"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspaces_dir=str(tmp_path / "repos"),
        )
        # Stub _run_steps so this test isolates the lifecycle wrapper.
        # monkeypatch.setattr restores at test teardown — no leak.
        monkeypatch.setattr(Phase0Worker, "_run_steps", lambda self, job: None)

        worker.run_one()
        s.expire_all()
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        assert job.status == "done"
        assert job.completed_at is not None


def test_phase0_worker_increments_attempts_and_marks_failed_after_5(
    pg_session_factory,
    monkeypatch,
    tmp_path,
):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            WorkspaceSetupJob(
                workspace_id=ws.id,
                status="queued",
                attempts=4,
            )
        )
        s.commit()
        ws_id = ws.id

        worker = Phase0Worker(
            session=s,
            client=Mock(model="m"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspaces_dir=str(tmp_path / "repos"),
        )

        def boom(self, job):
            raise RuntimeError("transient")

        monkeypatch.setattr(Phase0Worker, "_run_steps", boom)

        worker.run_one()
        s.expire_all()
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        assert job.status == "failed"
        assert job.attempts == 5
        assert "transient" in (job.last_error or "")


def test_phase0_worker_requeues_on_transient_failure_below_attempt_cap(
    pg_session_factory,
    monkeypatch,
    tmp_path,
):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.add(
            WorkspaceSetupJob(
                workspace_id=ws.id,
                status="queued",
                attempts=0,
            )
        )
        s.commit()
        ws_id = ws.id

        worker = Phase0Worker(
            session=s,
            client=Mock(model="m"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspaces_dir=str(tmp_path / "repos"),
        )

        def boom(self, job):
            raise RuntimeError("transient")

        monkeypatch.setattr(Phase0Worker, "_run_steps", boom)

        worker.run_one()
        s.expire_all()
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        assert job.status == "queued"  # back to queued for next tick
        assert job.attempts == 1
        assert job.started_at is None  # cleared so next claim works
        assert "transient" in (job.last_error or "")
