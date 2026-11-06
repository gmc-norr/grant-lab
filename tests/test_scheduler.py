from unittest.mock import MagicMock, patch

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

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


def test_build_scheduler_returns_one_job_per_active_adapter(_settings_env):
    """Smoke: the scheduler is constructed with a job per source. We don't
    actually start it — that would fire HTTP requests."""

    settings = Settings()

    def session_factory():  # not invoked because we don't start
        return None

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "ollama_url": "http://localhost:11434",
                "gemma_model": "gemma4:e4b-q8",
                "ranking_tick_seconds": 60,
                "ranking_queue_batch": 10,
                "embedding_pregate_threshold": 0.30,
                "embedding_hard_fail_threshold": None,
                "anthropic_api_key": None,
            }
        ),
        alerter=_NullAlerter(),
        embed_client=MagicMock(model="bge-m3"),
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

    email = SlidingWindowLimiter(max_requests=5, window_seconds=60, name="email")
    ip = SlidingWindowLimiter(max_requests=30, window_seconds=60, name="ip")
    settings = Settings()

    def session_factory():
        return None

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "ollama_url": "http://localhost:11434",
                "gemma_model": "gemma4:e4b-q8",
                "ranking_tick_seconds": 60,
                "ranking_queue_batch": 10,
                "embedding_pregate_threshold": 0.30,
                "embedding_hard_fail_threshold": None,
                "anthropic_api_key": None,
            }
        ),
        alerter=_NullAlerter(),
        embed_client=MagicMock(model="bge-m3"),
        limiters=[email, ip],
        start=False,
    )
    job_ids = {j.id for j in sched.get_jobs()}
    assert "rate_limit:sweep" in job_ids
    sched.shutdown(wait=False)


def test_build_scheduler_omits_rate_limit_sweep_when_limiters_empty(_settings_env):
    """No limiters ⇒ no sweep job. Mirrors digest's opt-in pattern so
    unit tests that don't need the sweep don't accumulate it."""

    settings = Settings()

    def session_factory():
        return None

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "ollama_url": "http://localhost:11434",
                "gemma_model": "gemma4:e4b-q8",
                "ranking_tick_seconds": 60,
                "ranking_queue_batch": 10,
                "embedding_pregate_threshold": 0.30,
                "embedding_hard_fail_threshold": None,
                "anthropic_api_key": None,
            }
        ),
        alerter=_NullAlerter(),
        embed_client=MagicMock(model="bge-m3"),
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
        # Expired RELATIVE TO THE LIMITER'S OWN CLOCK. A literal 0.0 is not "long
        # ago" on time.monotonic: that clock counts from boot, so on a CI runner up
        # for less than the 15-minute window, 0.0 was still inside it and the key
        # rightly survived (CI run 40789842634).
        expired = board_limiter._clock() - board_limiter._window - 1
        board_limiter._events["203.0.113.9"] = deque([expired])

        job = app.state.scheduler.get_job("rate_limit:sweep")
        assert job is not None, "no rate_limit:sweep job was registered"
        job.func()  # invokes sweep() on every limiter the closure was given

        assert "203.0.113.9" not in board_limiter._events, (
            "board_limiter's expired key survived the sweep -- it is "
            "missing from main.py's limiters=[...] list"
        )
    app.state.engine.dispose()


def test_build_scheduler_includes_rank_drain_job(_settings_env):

    settings = Settings()

    def session_factory():
        return None

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "ollama_url": "http://localhost:11434",
                "gemma_model": "gemma4:e4b-q8",
                "ranking_tick_seconds": 60,
                "ranking_queue_batch": 10,
                "embedding_pregate_threshold": 0.30,
                "embedding_hard_fail_threshold": None,
                "anthropic_api_key": None,
            }
        ),
        alerter=_NullAlerter(),
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    job_ids = sorted(j.id for j in sched.get_jobs())
    assert "rank:drain" in job_ids
    sched.shutdown(wait=False)


def test_auto_publish_tick_logs_its_summary_and_swallows_a_failure(_settings_env, caplog):
    """The 05:00 board rebuild registered as `boards:auto-publish` had ZERO
    committed coverage of its closure BODY.

    That gap is not cosmetic. The tick's log line names `_logger`, a module
    global; a slip to `log` is a NameError raised INSIDE the closure --
    invisible at import, invisible to a job-id assertion, caught either by the
    tick's own `except` or by APScheduler's handler, and first seen at 05:00 in
    production. So this calls `job.func()` on BOTH branches and asserts what
    each one LOGS, because "did not raise" is exactly what the happy-branch
    slip still looks like.

    The callable is a closure over `session_factory`, so calling the closure
    with the work patched out is the only way to prove the wiring.
    """
    import logging

    from grant_lab.boards.auto_publish import AutoPublishSummary

    settings = Settings()

    def session_factory():  # never invoked: reconcile_auto_boards is patched
        return None

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "ollama_url": "http://localhost:11434",
                "gemma_model": "gemma4:e4b-q8",
                "ranking_tick_seconds": 60,
                "ranking_queue_batch": 10,
                "embedding_pregate_threshold": 0.30,
                "embedding_hard_fail_threshold": None,
                "anthropic_api_key": None,
            }
        ),
        alerter=_NullAlerter(),
        embed_client=MagicMock(model="bge-m3"),
        start=False,
    )
    job = sched.get_job("boards:auto-publish")
    assert job is not None, "no boards:auto-publish job was registered"

    summary = AutoPublishSummary(boards_seen=2, rows_added=3, rows_removed=1)
    captured: list[object] = []

    # `reconcile_auto_boards` is bound at scheduler import time, so the patch
    # target is the name in `grant_lab.scheduler`, not its defining module.
    with (
        caplog.at_level(logging.INFO, logger="grant_lab.scheduler"),
        patch(
            "grant_lab.scheduler.reconcile_auto_boards",
            side_effect=lambda **kw: captured.append(kw["session_factory"]) or summary,
        ),
    ):
        job.func()
    assert captured == [session_factory], (
        "the job under id 'boards:auto-publish' does not reconcile auto boards "
        "with the factory the scheduler was built on"
    )
    assert "boards.auto_publish.tick.done" in caplog.text, (
        "the tick's success log never fired. A NameError inside the closure -- "
        f"the `log` vs `_logger` slip -- is swallowed by its own except branch: {caplog.text!r}"
    )

    # And a raising reconcile must not kill the scheduler thread, NOR reach
    # APScheduler silently: the except branch has to log under its own name.
    caplog.clear()
    with (
        caplog.at_level(logging.INFO, logger="grant_lab.scheduler"),
        patch(
            "grant_lab.scheduler.reconcile_auto_boards",
            side_effect=RuntimeError("db down"),
        ),
    ):
        job.func()  # must not raise
    assert "boards.auto_publish.failed" in caplog.text, (
        f"a failing tick logged nothing under its own name: {caplog.text!r}"
    )

    sched.shutdown(wait=False)


def test_daily_cron_jobs_set_misfire_grace_time(_settings_env):
    """Daily crons must set an explicit misfire_grace_time well above the
    1-second APScheduler default so a restart at the fire instant doesn't
    drop the run."""

    settings = Settings()

    def session_factory():
        return None

    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings.model_copy(
            update={
                "ollama_url": "http://localhost:11434",
                "gemma_model": "gemma4:e4b-q8",
                "ranking_tick_seconds": 60,
                "ranking_queue_batch": 10,
                "embedding_pregate_threshold": 0.30,
                "embedding_hard_fail_threshold": None,
                "anthropic_api_key": None,
            }
        ),
        alerter=_NullAlerter(),
        embed_client=MagicMock(model="bge-m3"),
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


def test_digest_tick_also_sends_board_alerts(
    pg_url, pg_session_factory, profile_factory, monkeypatch
):
    """Task 8 review, Important 1: `boards_needing_attention` had no caller
    anywhere in `src/`, so no board alert was ever delivered, and Tasks 9/10
    are pure frontend so nothing later in Plan C closed the gap either. The
    fix wires `send_board_alerts` into the EXISTING `digest:weekly` job's
    closure -- Plan C's own decision text says the trigger is "evaluated on
    the existing digest schedule" -- rather than adding a new job, lock or
    route. This test invokes the ACTUAL closure the scheduler registers
    under that job id, not `send_board_alerts` in isolation, so it proves
    the wiring, not just the function."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from grant_lab.db.models import Board, BoardItem, Call, Source, Subscription, User
    from grant_lab.email.client import SendResult

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    settings = Settings()

    pid, owner_id = profile_factory(name="Scheduler Wired Board Owner")
    with pg_session_factory() as s:
        owner_email = s.get(User, owner_id).email
        s.add(Subscription(user_id=owner_id, profile_id=pid, board_alerts_enabled=True))
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="scheduler-wired-call",
            url="https://example.org/scheduler-wired-call",
            title="Scheduler-wired call",
            deadline=datetime.now(UTC) + timedelta(days=30),
        )
        s.add(c)
        s.flush()
        b = Board(
            slug="w" * 16,
            title="Scheduler Wired Board",
            profile_id=pid,
            created_by=owner_id,
            items_per_page=5,
            is_published=True,
        )
        s.add(b)
        s.flush()
        s.add(BoardItem(board_id=b.id, call_id=c.id, position=0, approved_by=owner_id))
        s.commit()

    class _FakeEmail:
        def __init__(self):
            self.calls = []

        def send(self, *, to, subject, html, text):
            self.calls.append({"to": to, "subject": subject})
            return SendResult(ok=True, message_id="fake")

    fake_email = _FakeEmail()

    sched = build_scheduler(
        session_factory=pg_session_factory,
        settings=settings.model_copy(
            update={
                "ollama_url": "http://localhost:11434",
                "gemma_model": "gemma4:e4b-q8",
                "ranking_tick_seconds": 60,
                "ranking_queue_batch": 10,
                "embedding_pregate_threshold": 0.30,
                "embedding_hard_fail_threshold": None,
                "anthropic_api_key": None,
            }
        ),
        alerter=_NullAlerter(),
        embed_client=MagicMock(model="bge-m3"),
        digest_email_client=fake_email,
        start=False,
    )
    job = sched.get_job("digest:weekly")
    assert job is not None, "no digest:weekly job registered"

    job.func()

    board_alert_calls = [c for c in fake_email.calls if c["to"] == owner_email]
    assert board_alert_calls, (
        f"expected a board-alert email to the board's profile owner ({owner_email}); "
        f"got {fake_email.calls!r}"
    )
    assert "Scheduler Wired Board" in board_alert_calls[0]["subject"]
    sched.shutdown(wait=False)
