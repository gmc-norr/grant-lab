from grant_lab.config import Settings


def _env(monkeypatch, **extra):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_escalation_settings_have_defaults(monkeypatch):
    _env(monkeypatch)
    s = Settings()
    assert s.ranking_escalation_min_score == 45
    assert s.ranking_remote_daily_cap == 300


def test_cosine_threshold_setting_removed(monkeypatch):
    _env(monkeypatch)
    s = Settings()
    assert not hasattr(s, "remote_rank_cosine_threshold")


def test_every_daily_cap_default_is_the_settings_default():
    """27d487f4 raised the setting and build_scheduler's default but left
    drain_queue at 150 (fixed in 19ee7513). Pin all three together."""
    import inspect

    from grant_lab.ranking.worker import drain_queue
    from grant_lab.scheduler import build_scheduler

    want = Settings.model_fields["ranking_remote_daily_cap"].default
    assert inspect.signature(drain_queue).parameters["remote_daily_cap"].default == want
    assert inspect.signature(build_scheduler).parameters["ranking_remote_daily_cap"].default == want
