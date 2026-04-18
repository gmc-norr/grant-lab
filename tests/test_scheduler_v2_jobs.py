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
        embed_client=MagicMock(model="bge-m3"),
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


def test_setwise_tick_continues_after_one_profile_fails(
    monkeypatch, pg_session_factory, profile_factory, caplog,
):
    """If run_setwise_for_profile raises for profile A, the daily tick
    must still call it for profile B and log the failure for A.
    Codex: this is the resilience contract — one bad profile shouldn't
    kill the whole daily run."""
    import logging

    from call_monitor.db.models import Profile

    pid_a, _ = profile_factory(name="ProfileA")
    pid_b, _ = profile_factory(name="ProfileB")
    with pg_session_factory() as s:
        s.get(Profile, pid_a).ranking_scheme = "v2"
        s.get(Profile, pid_b).ranking_scheme = "v2"
        s.commit()

    called_with = []
    def _fake_setwise(*, session_factory, client, profile_id, now):
        called_with.append(profile_id)
        if profile_id == pid_a:
            raise RuntimeError("simulated profile A failure")
        return 1

    monkeypatch.setattr(
        "call_monitor.scheduler.run_setwise_for_profile", _fake_setwise,
    )

    sched = _build(pg_session_factory)
    try:
        with caplog.at_level(logging.ERROR, logger="call_monitor.scheduler"):
            # Manually invoke the registered job's func.
            job = sched.get_job("setwise:daily")
            job.func()
    finally:
        sched.shutdown(wait=False)

    # Both profiles attempted, A failed first then B succeeded
    assert pid_a in called_with
    assert pid_b in called_with
    # Failure was logged
    assert any(
        "setwise_profile_failed" in (r.getMessage() or "")
        or "ranking.setwise.profile_failed" in (r.message or "")
        for r in caplog.records
    )
