import json
import subprocess
from unittest.mock import Mock

from sqlalchemy import select

from call_manager.db.models import Workspace, WorkspaceEvent, WorkspaceSetupJob
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.phases.setup import (
    MAX_SETUP_ATTEMPTS,
    Phase0Worker,
    claim_next_setup_job,
    requeue_stuck_setup_jobs,
)
from tests.proposal_assist.conftest import make_call, make_profile, make_workspace


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
        assert claimed.queued_at is not None


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


def test_phase0_steps_1_3_init_repo_and_snapshot(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        call = make_call(s)
        profile = make_profile(s)
        s.commit()
        ws = make_workspace(s, call_id=call.id, profile_id=profile.id)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()

        worker = Phase0Worker(
            session=s,
            client=Mock(model="claude-opus-4-7"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspaces_dir=str(tmp_path / "repos"),
        )
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws.id))
        worker._snapshot_and_init_repo(job)
        s.commit()

        # repo exists with three empty artifact files
        repo_dir = tmp_path / "repos" / str(ws.id) / "repo"
        assert (repo_dir / ".git").is_dir()
        for fname in ("proposal-spec.md", "proposal-plan.md", "proposal.md"):
            assert (repo_dir / fname).is_file()
            assert (repo_dir / fname).read_text() == ""

        # one initial commit exists
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert int(out.stdout.strip()) == 1

        # snapshots populated
        ws_refetch = s.get(Workspace, ws.id)
        assert (call.full_text or "") in ws_refetch.call_body_snapshot
        assert ws_refetch.profile_desc_snapshot == profile.description


def test_phase0_step_4_copies_section_extracts_into_workspace(
    pg_session_factory,
    tmp_path,
):
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, call_id=call.id, current_phase="setup")
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

        client = Mock()
        client.model = "claude-opus-4-7"
        client.chat.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "eligibility": "E",
                                "scope": "S",
                                "budget": "B",
                                "expected_outcomes": "O",
                                "restrictions": "R",
                                "mandatory": "M",
                            }
                        ),
                    }
                }
            ],
            "model": "claude-opus-4-7",
        }

        worker = Phase0Worker(
            session=s,
            client=client,
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspaces_dir=str(tmp_path / "repos"),
        )
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        worker._ensure_section_extracts_snapshot(job)
        s.commit()
        ws_refetch = s.get(Workspace, ws_id)
        assert set(ws_refetch.section_extracts.keys()) == set(SECTION_NAMES)
        assert ws_refetch.section_extracts["eligibility"] == "E"


def test_phase0_step_5_copies_rubric_into_workspace_and_writes_init_event(
    pg_session_factory,
    tmp_path,
):
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, call_id=call.id, current_phase="setup")
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

        client = Mock()
        client.model = "claude-opus-4-7"
        client.chat.return_value = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "criteria": [
                                    {"name": "X", "description": "x", "weight": 1.0, "scale_max": 5}
                                ],
                                "funder_notes": "see FAQ",
                            }
                        ),
                    }
                }
            ],
            "model": "claude-opus-4-7",
        }

        worker = Phase0Worker(
            session=s,
            client=client,
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspaces_dir=str(tmp_path / "repos"),
        )
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        worker._ensure_rubric_snapshot(job)
        s.commit()
        ws_refetch = s.get(Workspace, ws_id)
        assert len(ws_refetch.rubric_criteria) == 1
        assert ws_refetch.rubric_criteria[0]["name"] == "X"
        assert ws_refetch.rubric_funder_notes == "see FAQ"

        events = s.scalars(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == ws_id,
                WorkspaceEvent.kind == "rubric_initialized",
            )
        ).all()
        assert len(events) == 1
        assert events[0].payload["criteria"][0]["name"] == "X"
        assert events[0].payload["funder_notes"] == "see FAQ"


def test_phase0_yields_to_waiting_for_rubric_review(
    pg_session_factory,
    tmp_path,
):
    """End-to-end of the worker scaffold + steps 1-5: run_one() should
    yield at waiting_for_rubric_review, not auto-promote to done. The
    job stays in the queue until the owner confirms via the rubric
    endpoint (Task 18)."""
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, call_id=call.id, current_phase="setup")
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id

        client = Mock()
        client.model = "claude-opus-4-7"
        # Two LLM calls expected: section extractor + rubric extractor.
        client.chat.side_effect = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    sec: f"x_{sec}"
                                    for sec in (
                                        "eligibility",
                                        "scope",
                                        "budget",
                                        "expected_outcomes",
                                        "restrictions",
                                        "mandatory",
                                    )
                                }
                            ),
                        }
                    }
                ],
                "model": "claude-opus-4-7",
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "criteria": [
                                        {
                                            "name": "C",
                                            "description": "c",
                                            "weight": 1.0,
                                            "scale_max": 5,
                                        }
                                    ],
                                    "funder_notes": "",
                                }
                            ),
                        }
                    }
                ],
                "model": "claude-opus-4-7",
            },
        ]

        worker = Phase0Worker(
            session=s,
            client=client,
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspaces_dir=str(tmp_path / "repos"),
        )
        worker.run_one()
        s.expire_all()
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        ws_refetch = s.get(Workspace, ws_id)
        assert job.status == "waiting_for_rubric_review"
        assert job.completed_at is None
        # Phase doesn't advance yet — that's Task 18's job.
        assert ws_refetch.current_phase == "setup"


def test_phase0_steps_1_3_recovers_from_aborted_git_init(
    pg_session_factory,
    tmp_path,
):
    """Simulate a prior attempt that did `git init` but crashed before
    committing. Second attempt should detect the missing initial commit
    and recreate the artifact files + commit."""
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, call_id=call.id, current_phase="setup")
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="running"))
        s.commit()
        ws_id = ws.id

        # Manually `git init` the workspace repo dir but DON'T create
        # files or commit — simulating a crash mid-init.
        repo_dir = tmp_path / "repos" / str(ws_id) / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=repo_dir,
            check=True,
        )
        assert (repo_dir / ".git").is_dir()
        # No initial commit, no artifact files.
        assert not (repo_dir / "proposal-spec.md").exists()

        worker = Phase0Worker(
            session=s,
            client=Mock(model="claude-opus-4-7"),
            extractor_version="sections-v1",
            rubric_extractor_version="rubric-v1",
            workspaces_dir=str(tmp_path / "repos"),
        )
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        worker._snapshot_and_init_repo(job)
        s.commit()

        # Recovery: artifact files + initial commit now exist.
        for fname in ("proposal-spec.md", "proposal-plan.md", "proposal.md"):
            assert (repo_dir / fname).is_file()
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert int(out.stdout.strip()) == 1


def test_claim_next_returns_oldest_by_queued_at(pg_session_factory):
    """Claim order is FIFO on queued_at, not arbitrary by UUID."""
    import time

    from call_manager.db.models import User

    with pg_session_factory() as s:
        owner_a = User(email="fifo-owner-a@ki.se")
        owner_b = User(email="fifo-owner-b@ki.se")
        s.add_all([owner_a, owner_b])
        s.flush()
        ws_a = make_workspace(s, owner=owner_a)
        ws_b = make_workspace(s, owner=owner_b)
        s.commit()
        # Insert in B-then-A order so the UUID lexical order differs from
        # the queued_at order.
        s.add(WorkspaceSetupJob(workspace_id=ws_b.id, status="queued"))
        s.commit()
        time.sleep(0.01)  # ensure distinct now() values
        s.add(WorkspaceSetupJob(workspace_id=ws_a.id, status="queued"))
        s.commit()

        # First claim should be ws_b (queued first).
        claimed = claim_next_setup_job(session=s)
        s.commit()
        assert claimed is not None
        assert claimed.workspace_id == ws_b.id


def test_requeue_stuck_setup_jobs_requeues_crashed_running(pg_session_factory):
    """A setup job abandoned in 'running' (worker crashed before a terminal
    transition) is reset to 'queued' with started_at cleared so the next tick
    re-claims it — otherwise it's invisible to the queued-only peek forever."""
    from datetime import UTC, datetime, timedelta

    from call_manager.proposal_assist.phases.setup import STUCK_SETUP_AFTER

    with pg_session_factory() as s:
        ws = make_workspace(s)
        job = WorkspaceSetupJob(
            workspace_id=ws.id,
            status="running",
            started_at=datetime.now(UTC) - (STUCK_SETUP_AFTER + timedelta(minutes=1)),
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        n = requeue_stuck_setup_jobs(s)
        s.commit()
    assert n == 1

    with pg_session_factory() as s:
        row = s.get(WorkspaceSetupJob, job_id)
        assert row.status == "queued"
        assert row.started_at is None
        assert row.attempts == 1


def test_requeue_stuck_setup_jobs_dead_letters_after_max_attempts(pg_session_factory):
    """A stuck running job that has exhausted its attempts is dead-lettered to
    'failed' rather than looping forever."""
    from datetime import UTC, datetime, timedelta

    from call_manager.proposal_assist.phases.setup import STUCK_SETUP_AFTER

    with pg_session_factory() as s:
        ws = make_workspace(s)
        job = WorkspaceSetupJob(
            workspace_id=ws.id,
            status="running",
            attempts=MAX_SETUP_ATTEMPTS - 1,
            started_at=datetime.now(UTC) - (STUCK_SETUP_AFTER + timedelta(minutes=1)),
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        n = requeue_stuck_setup_jobs(s)
        s.commit()
    assert n == 1

    with pg_session_factory() as s:
        row = s.get(WorkspaceSetupJob, job_id)
        assert row.status == "failed"
        assert row.attempts == MAX_SETUP_ATTEMPTS
        assert row.completed_at is not None


def test_requeue_stuck_setup_jobs_ignores_waiting_for_rubric_review(pg_session_factory):
    """'waiting_for_rubric_review' is a legitimate non-terminal state awaiting
    human action — the reaper must never touch it, however old it is."""
    from datetime import UTC, datetime, timedelta

    with pg_session_factory() as s:
        ws = make_workspace(s)
        job = WorkspaceSetupJob(
            workspace_id=ws.id,
            status="waiting_for_rubric_review",
            started_at=datetime.now(UTC) - timedelta(days=7),
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        assert requeue_stuck_setup_jobs(s) == 0
        s.commit()

    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
