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
        vinnova_api_key="dummy",
        alerter=_NullAlerter(),
        start=False,
    )
    assert isinstance(sched, BackgroundScheduler)
    job_slugs = sorted(j.id for j in sched.get_jobs())
    assert job_slugs == ["ingest:eu_portal", "ingest:vinnova"]
    sched.shutdown(wait=False)


def test_build_scheduler_skips_vinnova_without_api_key():
    sched = build_scheduler(
        session_factory=lambda: None,
        eu_portal_base_url="https://ec.europa.eu",
        vinnova_base_url="https://data.vinnova.se",
        vinnova_api_key=None,
        alerter=_NullAlerter(),
        start=False,
    )
    job_slugs = sorted(j.id for j in sched.get_jobs())
    assert job_slugs == ["ingest:eu_portal"]
    sched.shutdown(wait=False)
