"""The scheduler-tick driver: on each tick, claims one job and runs it.
The driver is sync (matches BackgroundScheduler's worker pool) and opens
its own session per tick so APScheduler isn't holding a long-lived one."""

import json
from unittest.mock import Mock

from sqlalchemy import select

from call_manager.db.models import WorkspaceSetupJob
from call_manager.proposal_assist.extractors.sections import SECTION_NAMES
from call_manager.proposal_assist.phases.setup import run_phase0_tick
from tests.proposal_assist.conftest import make_call, make_workspace


def _section_envelope() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({s: f"section text for {s}" for s in SECTION_NAMES}),
                }
            }
        ],
        "model": "claude-opus-4-7",
    }


def _rubric_envelope() -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "criteria": [
                                {
                                    "name": "Excellence",
                                    "description": "Scientific quality",
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
    }


def test_run_phase0_tick_drains_one_queued_job(
    pg_session_factory,
    tmp_path,
):
    """Post-Task-5, Phase0Worker no longer exposes a monkeypatchable
    `_run_steps` seam — the milestones (_m1/_m2/_m3) each open their own
    session. So this test drives the tick end-to-end with a real (stubbed)
    LLM client and checks for concrete evidence the worker actually ran: the
    job reaches the real interim-M3 terminal state, and the git repo the
    worker's M1 creates exists on disk."""
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, call_id=call.id)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id

    client = Mock(model="claude-opus-4-7")
    client.chat.side_effect = [_section_envelope(), _rubric_envelope()]

    run_phase0_tick(
        session_factory=pg_session_factory,
        client_factory=lambda: client,
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspaces_dir=str(tmp_path / "repos"),
    )

    with pg_session_factory() as s:
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        assert job.status == "waiting_for_rubric_review"
        # Proves the worker actually ran M1 (not just that claim_next_setup_job
        # flipped the row to 'running'): the on-disk repo M1 creates exists.
        assert (tmp_path / "repos" / str(ws_id) / "repo" / ".git").is_dir()


def test_run_phase0_tick_is_a_noop_when_queue_empty(
    pg_session_factory,
    tmp_path,
):
    run_phase0_tick(
        session_factory=pg_session_factory,
        client_factory=lambda: Mock(model="m"),
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspaces_dir=str(tmp_path / "repos"),
    )
    # No assertion needed — absence of exception is the contract.


def test_run_phase0_tick_does_not_build_client_when_queue_empty(
    pg_session_factory,
    tmp_path,
):
    """Empty queue → client_factory MUST NOT be called. Saves the
    cost of constructing the Anthropic SDK on every idle tick."""
    factory_calls = {"n": 0}

    def tracking_factory():
        factory_calls["n"] += 1
        return Mock(model="m")

    run_phase0_tick(
        session_factory=pg_session_factory,
        client_factory=tracking_factory,
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspaces_dir=str(tmp_path / "repos"),
    )
    assert factory_calls["n"] == 0


def test_build_scheduler_registers_phase0_tick(monkeypatch, pg_url):
    """The job lands in the scheduler with the right id + trigger, and
    starting it does not crash. Catches signature drift in build_scheduler
    (e.g. the closure receiving args it can't unpack)."""
    from unittest.mock import MagicMock

    from call_manager.config import Settings
    from call_manager.db.session import make_engine, make_session_factory
    from call_manager.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    settings = Settings()

    engine = make_engine(settings.database_url)
    try:
        session_factory = make_session_factory(engine)

        from call_manager.proposal_assist.drafting.factory import (
            build_workspace_controller,
        )

        workspace_controller = build_workspace_controller(
            settings=settings,
            session_factory=session_factory,
        )

        class _Alerter:
            def record_failure(self, source_slug, *, error=None):
                pass

            def record_success(self, source_slug):
                pass

        alerter = _Alerter()
        embed_client = MagicMock(model="bge-m3")
        sched = build_scheduler(
            session_factory=session_factory,
            settings=settings,
            alerter=alerter,
            ollama_url="http://localhost",
            gemma_model="gemma:test",
            ranking_tick_seconds=60,
            ranking_queue_batch=10,
            embedding_pregate_threshold=0.3,
            embed_client=embed_client,
            workspace_controller=workspace_controller,
            start=False,
        )
        try:
            job = sched.get_job("proposal_assist_phase0_tick")
            assert job is not None
            assert job.trigger.interval.total_seconds() == settings.phase0_tick_seconds
            # Invoke the closure once with the queue empty — must not raise.
            # anthropic_api_key is None by default so the closure returns early.
            job.func()
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
