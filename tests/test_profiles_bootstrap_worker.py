from datetime import UTC, datetime, timedelta

from call_manager.db.models import Profile, ProfileBootstrapJob, ProfileKind, User
from call_manager.profiles import bootstrap_worker as bw
from call_manager.profiles.bootstrap_worker import (
    requeue_stuck_bootstrap_jobs,
    run_profile_bootstrap_tick,
)
from call_manager.profiles.seed import DraftProfile


class _Embed:
    model = "fake-embed"

    def embed(self, text):
        return [0.0] * 8


def _enqueue(sf, *, name="Grp"):
    with sf() as s:
        u = User(email=f"{name.lower()}@ki.se")
        s.add(u)
        s.flush()
        p = Profile(
            name=name,
            kind=ProfileKind.ORGANIZATIONAL_UNIT.value,
            level="group",
            description="",
            structured={"areas": []},
            created_by=u.id,
        )
        s.add(p)
        s.flush()
        s.add(
            ProfileBootstrapJob(
                profile_id=p.id,
                status="queued",
                seed={"name": name, "kind": "organizational_unit", "members": []},
            )
        )
        s.commit()
        return p.id


def _patch_draft(monkeypatch, *, llm=True, pubmed="not_queried"):
    monkeypatch.setattr(
        bw,
        "bootstrap_profile",
        lambda **_: DraftProfile(
            "Drafted." if llm else "", {"areas": []}, 0, 0, llm_succeeded=llm, pubmed_status=pubmed
        ),
    )


def test_success_embeds_before_backfill_and_clears_seed(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    order = []
    _patch_draft(monkeypatch, llm=True, pubmed="ok")
    monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: order.append("embed"))
    monkeypatch.setattr(bw, "backfill_profile", lambda **k: order.append("backfill") or 0)
    run_profile_bootstrap_tick(
        session_factory=pg_session_factory, pubmed=object(), ollama=object(), embed_client=_Embed()
    )
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert s.get(Profile, pid).description == "Drafted."
        assert job.status == "done" and job.pubmed_status == "ok" and job.seed is None
    assert order == ["embed", "backfill"]


def test_degraded_pubmed_still_succeeds(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    _patch_draft(monkeypatch, llm=True, pubmed="unavailable")
    monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: None)
    calls = {"n": 0}
    monkeypatch.setattr(bw, "backfill_profile", lambda **k: calls.__setitem__("n", 1) or 0)
    run_profile_bootstrap_tick(
        session_factory=pg_session_factory, pubmed=object(), ollama=object(), embed_client=_Embed()
    )
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert job.status == "done" and job.pubmed_status == "unavailable"
    assert calls["n"] == 1


def test_llm_failure_exhausts_to_failed_no_backfill(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    _patch_draft(monkeypatch, llm=False)
    monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: None)
    called = {"n": 0}
    monkeypatch.setattr(bw, "backfill_profile", lambda **k: called.__setitem__("n", 1) or 0)
    for _ in range(bw.MAX_BOOTSTRAP_ATTEMPTS):
        run_profile_bootstrap_tick(
            session_factory=pg_session_factory,
            pubmed=object(),
            ollama=object(),
            embed_client=_Embed(),
        )
    with pg_session_factory() as s:
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one().status == "failed"
    assert called["n"] == 0


def test_backfill_exception_transitions_job_not_stuck_running(monkeypatch, pg_session_factory):
    pid = _enqueue(pg_session_factory)
    _patch_draft(monkeypatch, llm=True, pubmed="ok")
    monkeypatch.setattr(bw, "refresh_profile_embedding", lambda *a, **k: None)

    def _raise(**k):
        raise RuntimeError("backfill boom")

    monkeypatch.setattr(bw, "backfill_profile", _raise)
    run_profile_bootstrap_tick(
        session_factory=pg_session_factory, pubmed=object(), ollama=object(), embed_client=_Embed()
    )
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert job.status == "queued"  # not stuck 'running'
        assert "backfill boom" in (job.last_error or "")


def test_reaper_requeues_then_dead_letters(pg_session_factory):
    pid = _enqueue(pg_session_factory)
    stale = datetime.now(UTC) - bw.STUCK_BOOTSTRAP_AFTER - timedelta(minutes=1)
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        job.status, job.started_at, job.attempts = "running", stale, 2
        s.commit()
    with pg_session_factory() as s:
        requeue_stuck_bootstrap_jobs(s)
        s.commit()
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert job.status == "queued" and job.started_at is None and job.attempts == 2
    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        job.status, job.started_at, job.attempts = "running", stale, bw.MAX_BOOTSTRAP_ATTEMPTS
        s.commit()
    with pg_session_factory() as s:
        requeue_stuck_bootstrap_jobs(s)
        s.commit()
    with pg_session_factory() as s:
        assert s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one().status == "failed"


def test_scheduler_registers_bootstrap_tick(monkeypatch):
    from unittest.mock import MagicMock

    from call_manager.config import Settings
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller
    from call_manager.scheduler import build_scheduler

    for k, v in {
        "DATABASE_URL": "postgresql+psycopg://x:y@localhost/z",
        "SESSION_SECRET": "test-secret-at-least-16-chars",
        "ALLOWED_EMAIL_DOMAINS": "ki.se",
        "APP_BASE_URL": "http://testserver",
        "DRAFTING_ADAPTER": "fake",
    }.items():
        monkeypatch.setenv(k, v)
    settings = Settings()

    class _NullAlerter:
        def record_failure(self, *a, **k):
            pass

        def record_success(self, *a, **k):
            pass

    def session_factory():
        return None

    wc = build_workspace_controller(settings=settings, session_factory=session_factory)
    sched = build_scheduler(
        session_factory=session_factory,
        settings=settings,
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="g",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        embedding_pregate_threshold=0.30,
        embed_client=MagicMock(model="bge-m3"),
        workspace_controller=wc,
        start=False,
    )
    assert sched.get_job("profile_bootstrap_tick") is not None


# ---------------------------------------------------------------------------
# Defect 6: crash-recovery idempotency — skip bootstrap if profile has desc
# ---------------------------------------------------------------------------

def test_bootstrap_skipped_when_profile_already_has_description(monkeypatch, pg_session_factory):
    """If a profile already has a non-empty description (a prior run populated it
    but crashed before marking 'done'), bootstrap_profile must NOT be called again.
    backfill_profile and the 'done' marker must still run."""
    pid = _enqueue(pg_session_factory)

    # Pre-populate the profile with a description to simulate a prior crash
    # after merge but before the 'done' commit.
    with pg_session_factory() as s:
        profile = s.get(Profile, pid)
        profile.description = "Previously bootstrapped description."
        s.commit()

    bootstrap_calls = {"n": 0}
    backfill_calls = {"n": 0}

    monkeypatch.setattr(
        bw,
        "bootstrap_profile",
        lambda **_: bootstrap_calls.__setitem__("n", bootstrap_calls["n"] + 1) or None,
    )
    monkeypatch.setattr(
        bw,
        "refresh_profile_embedding",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        bw,
        "backfill_profile",
        lambda **k: backfill_calls.__setitem__("n", backfill_calls["n"] + 1) or 0,
    )

    run_profile_bootstrap_tick(
        session_factory=pg_session_factory, pubmed=object(), ollama=object(), embed_client=_Embed()
    )

    assert bootstrap_calls["n"] == 0, (
        "bootstrap_profile must NOT be called when profile already has a description"
    )
    assert backfill_calls["n"] == 1, "backfill_profile must still be called"

    with pg_session_factory() as s:
        job = s.query(ProfileBootstrapJob).filter_by(profile_id=pid).one()
        assert job.status == "done", f"Job should be 'done', got '{job.status}'"
