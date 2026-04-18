from unittest.mock import MagicMock

from call_monitor.scheduler import build_scheduler


class _NullEmail:
    def send(self, **kw):
        raise NotImplementedError


def _build(session_factory, *, with_digest=False):
    kwargs = dict(
        session_factory=session_factory,
        eu_portal_base_url="http://eu",
        vinnova_base_url="https://data.vinnova.se",
        alerter=MagicMock(),
        ollama_url="http://ollama",
        gemma_model="gemma4:e4b-it-q4_K_M",
        ranking_tick_seconds=60,
        ranking_queue_batch=10,
        start=False,
    )
    if with_digest:
        kwargs["digest_email_client"] = _NullEmail()
        kwargs["digest_settings"] = MagicMock(
            digest_dry_run=False, admin_dashboard_url="http://app/"
        )
    return build_scheduler(**kwargs)


def test_scheduler_registers_setwise_daily(pg_session_factory):
    sched = _build(pg_session_factory)
    assert sched.get_job("setwise:daily") is not None
    sched.shutdown(wait=False)


def test_scheduler_registers_golden_eval_weekly(pg_session_factory):
    sched = _build(pg_session_factory)
    assert sched.get_job("golden_eval:weekly") is not None
    sched.shutdown(wait=False)
