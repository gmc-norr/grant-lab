from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from grant_lab.config import Settings
from grant_lab.scheduler import build_scheduler


class _NullAlerter:
    def record_failure(self, *a, **kw):
        pass

    def record_success(self, *a, **kw):
        pass


@pytest.fixture
def _settings_env(monkeypatch):
    """Populate the env vars Settings() requires.

    Source base_urls fall back to the Settings defaults when EU_PORTAL_BASE_URL /
    VINNOVA_BASE_URL are unset, which is fine for these smoke tests — we don't
    actually start the scheduler.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")


def test_build_scheduler_returns_one_job_per_active_adapter(_settings_env):
    """Smoke: the scheduler is constructed with a job per source. We don't
    actually start it — that would fire HTTP requests."""
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():  # not invoked because we don't start
        return None

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    assert isinstance(sched, BackgroundScheduler)
    actual_ids = {j.id for j in sched.get_jobs()}
    expected_required = {
        "ingest:eu_portal",
        "ingest:vinnova",
        "rank:drain",
        "golden_eval:weekly",
        "embeddings:refresh-missing",
        "ranking:reconcile",
    }
    assert expected_required.issubset(actual_ids)
    sched.shutdown(wait=False)


def test_build_scheduler_includes_rate_limit_sweep_when_limiters_given(_settings_env):
    """The in-memory rate limiter's `_events` dict grows with every unique
    (email|ip) key. Without a periodic sweep, a long-running process with
    turnover in failed-login attempts will leak memory indefinitely. The
    scheduler wires up a sweep job when callers hand it the limiters to
    sweep."""
    from grant_lab.auth.rate_limit import SlidingWindowLimiter
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    email = SlidingWindowLimiter(max_requests=5, window_seconds=60, name="email")
    ip = SlidingWindowLimiter(max_requests=30, window_seconds=60, name="ip")
    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        limiters=[email, ip],
        workspace_controller=workspace_controller,
        start=False,
    )
    job_ids = {j.id for j in sched.get_jobs()}
    assert "rate_limit:sweep" in job_ids
    sched.shutdown(wait=False)


def test_build_scheduler_omits_rate_limit_sweep_when_limiters_empty(_settings_env):
    """No limiters ⇒ no sweep job. Mirrors digest's opt-in pattern so
    unit tests that don't need the sweep don't accumulate it."""
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    job_ids = {j.id for j in sched.get_jobs()}
    assert "rate_limit:sweep" not in job_ids
    sched.shutdown(wait=False)


def test_main_pys_rate_limit_sweep_covers_the_board_limiter(pg_url, migrated_engine, monkeypatch):
    """IMPORTANT 2 (Plan C Task 7 review): `main.py`'s
    `build_scheduler(limiters=[...])` call must include `board_limiter` --
    the public board route's own counter -- alongside `email_limiter` and
    `ip_limiter`. `SlidingWindowLimiter.allow()` never deletes a key on its
    own (it only pops expired timestamps off one key's deque); only
    `sweep()`, run by this job, removes the key entirely. A limiter left out
    of that list leaks one dict entry per distinct source IP forever, and
    the board route is the one anonymous route in the app -- the most
    exposed to scan traffic.

    This is the actual call site in `main.py`, not `build_scheduler`'s
    generic mechanism (already covered above) -- a passing test there says
    nothing about what `main.py` actually passes in.
    """
    from collections import deque

    from fastapi.testclient import TestClient

    from grant_lab.main import create_app

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")

    app = create_app()
    with TestClient(app):
        board_limiter = app.state.board_limiter
        assert app.state.email_limiter is not board_limiter
        assert app.state.ip_limiter is not board_limiter
        board_limiter._events["203.0.113.9"] = deque([0.0])  # long-expired

        job = app.state.scheduler.get_job("rate_limit:sweep")
        assert job is not None, "no rate_limit:sweep job was registered"
        job.func()  # invokes sweep() on every limiter the closure was given

        assert "203.0.113.9" not in board_limiter._events, (
            "board_limiter's expired key survived the sweep -- it is "
            "missing from main.py's limiters=[...] list"
        )
    app.state.engine.dispose()


def test_build_scheduler_includes_rank_drain_job(_settings_env):
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    job_ids = sorted(j.id for j in sched.get_jobs())
    assert "rank:drain" in job_ids
    sched.shutdown(wait=False)


def test_build_scheduler_includes_turn_reaper_job(_settings_env):
    """The stuck-turn reaper must be scheduled so a wedged workspace
    (turn_jobs row stuck status='running') gets unwedged."""
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    job = sched.get_job("turn_reaper")
    assert job is not None

    # Review 2026-09-02 A6 F4: the id-only assertion was the whole test, and an
    # id is exactly what wiring does NOT prove — a job registered under
    # "turn_reaper" with the wrong callable or a once-a-year trigger passed.
    #
    # The callable is a closure over session_factory, so there is no importable
    # symbol to compare it to. Calling it is the proof: patch the reaper and
    # check the job actually reaps, with the factory the scheduler was built on.
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(seconds=300), (
        f"turn_reaper cadence is {job.trigger.interval}, not the intended 5 minutes"
    )

    calls: list[object] = []
    with patch(
        "grant_lab.proposal_assist.turns.reaper.reap_stuck_turns",
        side_effect=lambda **kw: calls.append(kw["session_factory"]),
    ):
        job.func()
    assert calls == [session_factory], "the job under id 'turn_reaper' does not reap"

    # And a failing tick must not kill the scheduler thread: the wrapper logs
    # and swallows, which is what lets the next tick run at all.
    with patch(
        "grant_lab.proposal_assist.turns.reaper.reap_stuck_turns",
        side_effect=RuntimeError("db down"),
    ):
        job.func()  # must not raise

    sched.shutdown(wait=False)


_ANALYSIS_JOB_IDS = ("claim_analysis_drain", "claim_analysis_reconcile")


def _build_with_analysis_deps(settings, **kw):
    """Shared builder for the claim-analysis conditional-registration tests —
    same arguments as the smoke tests above, plus the analysis kwargs."""
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )
    return build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
        **kw,
    )


def test_build_scheduler_includes_analysis_ticks_when_service_and_embedder_given(_settings_env):
    """Both analysis ticks register when a citation service AND an embedder
    are provided and drafting is enabled (the default). Gated on availability,
    NOT on ANTHROPIC_API_KEY — the citation LLM defaults to local Ollama."""
    sched = _build_with_analysis_deps(
        Settings(), citation_service=object(), analysis_embedder=object()
    )
    job_ids = {j.id for j in sched.get_jobs()}
    for jid in _ANALYSIS_JOB_IDS:
        assert jid in job_ids, jid
    sched.shutdown(wait=False)


def test_build_scheduler_omits_analysis_ticks_without_citation_service(_settings_env):
    sched = _build_with_analysis_deps(Settings(), citation_service=None, analysis_embedder=object())
    job_ids = {j.id for j in sched.get_jobs()}
    for jid in _ANALYSIS_JOB_IDS:
        assert jid not in job_ids, jid
    sched.shutdown(wait=False)


def test_build_scheduler_omits_analysis_ticks_without_embedder(_settings_env):
    sched = _build_with_analysis_deps(Settings(), citation_service=object(), analysis_embedder=None)
    job_ids = {j.id for j in sched.get_jobs()}
    for jid in _ANALYSIS_JOB_IDS:
        assert jid not in job_ids, jid
    sched.shutdown(wait=False)


def test_build_scheduler_omits_analysis_ticks_when_drafting_disabled(_settings_env, monkeypatch):
    monkeypatch.setenv("DRAFTING_ENABLED", "false")
    sched = _build_with_analysis_deps(
        Settings(), citation_service=object(), analysis_embedder=object()
    )
    job_ids = {j.id for j in sched.get_jobs()}
    for jid in _ANALYSIS_JOB_IDS:
        assert jid not in job_ids, jid
    sched.shutdown(wait=False)


def test_daily_cron_jobs_set_misfire_grace_time(_settings_env):
    """Daily crons must set an explicit misfire_grace_time well above the
    1-second APScheduler default so a restart at the fire instant doesn't
    drop the run."""
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings,
        session_factory=session_factory,
    )
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    for jid in (
        "ingest:eu_portal",
        "ingest:vinnova",
        "golden_eval:weekly",
        "embeddings:refresh-missing",
        "ranking:reconcile",
    ):
        job = sched.get_job(jid)
        assert job is not None, jid
        assert job.misfire_grace_time is not None and job.misfire_grace_time >= 6 * 3600, jid
    sched.shutdown(wait=False)


def test_build_scheduler_wires_collab_checkpoint_job(_settings_env, monkeypatch):
    from unittest.mock import MagicMock

    import grant_lab.proposal_assist.collab.checkpoint as cp
    from grant_lab.config import Settings
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings, session_factory=session_factory
    )
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    job = sched.get_job("collab:checkpoint_tails")
    assert job is not None
    # wired with the CONFIGURED interval...
    assert job.trigger.interval.total_seconds() == settings.collab_checkpoint_scan_seconds
    # ...and the job fn passes the configured session_factory / workspaces_dir / min_tail.
    captured = {}
    monkeypatch.setattr(
        cp,
        "checkpoint_active_tails",
        lambda sf, *, workspaces_dir, min_tail: captured.update(
            sf=sf, workspaces_dir=workspaces_dir, min_tail=min_tail
        ),
    )
    job.func()  # the closure lazy-imports checkpoint_active_tails → picks up the patch
    assert captured == {
        "sf": session_factory,
        "workspaces_dir": settings.workspaces_dir,
        "min_tail": settings.collab_checkpoint_min_tail,
    }


def test_build_scheduler_omits_collab_checkpoint_when_disabled(_settings_env, monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setenv("COLLAB_CHECKPOINT_SCAN_SECONDS", "0")
    from grant_lab.config import Settings
    from grant_lab.proposal_assist.drafting.factory import build_workspace_controller

    settings = Settings()

    def session_factory():
        return None

    workspace_controller = build_workspace_controller(
        settings=settings, session_factory=session_factory
    )
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=workspace_controller,
        start=False,
    )
    assert sched.get_job("collab:checkpoint_tails") is None
