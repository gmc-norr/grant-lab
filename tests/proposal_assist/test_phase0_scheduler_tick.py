"""The scheduler-tick driver: on each tick, claims one job and runs it.
The driver is sync (matches BackgroundScheduler's worker pool) and opens
its own session per tick so APScheduler isn't holding a long-lived one."""

from unittest.mock import Mock

from sqlalchemy import select

from call_monitor.db.models import WorkspaceSetupJob
from call_monitor.proposal_assist.phases.setup import Phase0Worker, run_phase0_tick
from tests.proposal_assist.conftest import make_call, make_workspace


def test_run_phase0_tick_drains_one_queued_job(
    pg_session_factory,
    tmp_path,
    monkeypatch,
):
    with pg_session_factory() as s:
        call = make_call(s)
        s.commit()
        ws = make_workspace(s, call_id=call.id)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id

    client = Mock(model="claude-opus-4-7")
    captured: dict = {}

    def fake_run_steps(self, job):
        captured["worker_seen"] = True

    monkeypatch.setattr(Phase0Worker, "_run_steps", fake_run_steps)

    run_phase0_tick(
        session_factory=pg_session_factory,
        client_factory=lambda: client,
        extractor_version="sections-v1",
        rubric_extractor_version="rubric-v1",
        workspaces_dir=str(tmp_path / "repos"),
    )

    with pg_session_factory() as s:
        job = s.scalar(select(WorkspaceSetupJob).where(WorkspaceSetupJob.workspace_id == ws_id))
        assert job.status == "done"
        assert captured.get("worker_seen") is True


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

    from call_monitor.config import Settings
    from call_monitor.db.session import make_engine, make_session_factory
    from call_monitor.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    settings = Settings()

    engine = make_engine(settings.database_url)
    try:
        session_factory = make_session_factory(engine)

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
