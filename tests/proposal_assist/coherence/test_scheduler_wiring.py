import pytest

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob
from call_manager.proposal_assist.coherence.pass_runner import drain_coherence_queue_tick
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from tests.proposal_assist.conftest import make_workspace


def test_drain_tick_claims_and_runs_a_queued_job(tmp_path, pg_session_factory, _settings_env):
    import asyncio

    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True)
    (repo / "proposal.md").write_text("## Aims\n\nD.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nS.\n")

    fake = FakeAgentAdapter()
    fake.script_next_turn([TurnDoneEvent(tokens_in=10, tokens_out=5, model="m", session_id="s")])

    asyncio.run(
        drain_coherence_queue_tick(
            session_factory=pg_session_factory,
            settings=settings,
            adapter_builder=lambda **kw: fake,
        )
    )

    with pg_session_factory() as s:
        job = s.query(CoherenceJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "completed"


@pytest.mark.asyncio
async def test_build_scheduler_registers_coherence_drain_and_reaper_ticks(
    monkeypatch,
    pg_url,
):
    from unittest.mock import MagicMock

    from call_manager.db.session import make_engine, make_session_factory
    from call_manager.proposal_assist.drafting.factory import (
        build_workspace_controller,
    )
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
        controller = build_workspace_controller(
            settings=settings,
            session_factory=session_factory,
        )

        class _Alerter:
            def record_failure(self, source_slug, *, error=None):
                pass

            def record_success(self, source_slug):
                pass

        embed_client = MagicMock(model="bge-m3")
        sched = build_scheduler(
            session_factory=session_factory,
            settings=settings,
            alerter=_Alerter(),
            ollama_url="http://localhost",
            gemma_model="gemma:test",
            ranking_tick_seconds=60,
            ranking_queue_batch=10,
            embedding_pregate_threshold=0.3,
            embed_client=embed_client,
            workspace_controller=controller,
            start=False,
        )
        try:
            drain_job = sched.get_job("coherence_drain")
            assert drain_job is not None
            assert drain_job.trigger.interval.total_seconds() == settings.coherence_tick_seconds
            reaper_job = sched.get_job("coherence_reaper")
            assert reaper_job is not None
            # Sweep cadence is decoupled from the TTL age cutoff (coherence_job_timeout_seconds).
            assert (
                reaper_job.trigger.interval.total_seconds()
                == settings.coherence_reaper_tick_seconds
            )
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
