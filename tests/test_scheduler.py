from apscheduler.schedulers.background import BackgroundScheduler

from call_monitor.scheduler import build_scheduler


class _NullAlerter:
    def record_failure(self, *a, **kw): pass
    def record_success(self, *a, **kw): pass


def test_build_scheduler_returns_one_job_per_active_adapter():
    """Smoke: the scheduler is constructed with a job per source. We don't
    actually start it — that would fire HTTP requests."""
    sched = build_scheduler(
        session_factory=lambda: None,  # not invoked because we don't start
        eu_portal_base_url="https://ec.europa.eu",
        vinnova_base_url="https://data.vinnova.se",
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        start=False,
    )
    assert isinstance(sched, BackgroundScheduler)
    job_slugs = sorted(j.id for j in sched.get_jobs())
    assert job_slugs == ["ingest:eu_portal", "ingest:vinnova", "rank:drain"]
    sched.shutdown(wait=False)


def test_build_scheduler_includes_rank_drain_job():
    sched = build_scheduler(
        session_factory=lambda: None,
        eu_portal_base_url="https://ec.europa.eu",
        vinnova_base_url="https://data.vinnova.se",
        alerter=_NullAlerter(),
        ollama_url="http://localhost:11434",
        gemma_model="gemma4:e4b-q8",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        start=False,
    )
    job_ids = sorted(j.id for j in sched.get_jobs())
    assert "rank:drain" in job_ids
    sched.shutdown(wait=False)
