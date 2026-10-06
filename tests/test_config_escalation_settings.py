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


def test_remote_rank_model_defaults_to_sonnet_5_5():
    """Sonnet 5.5 (2026-09-28) costs the same as Sonnet 5 ($2/$10 per MTok) and
    returned a schema-valid ranking on a live probe. Reads the declared default,
    so a developer's .env cannot mask it."""
    assert Settings.model_fields["remote_rank_model"].default == "claude-sonnet-5-5"


def test_every_remote_rank_model_default_is_the_settings_default():
    """The model is named in three places, like the daily cap. Pin them together."""
    import inspect

    from grant_lab.ranking.anthropic_client import AnthropicClient
    from grant_lab.scheduler import build_scheduler

    want = Settings.model_fields["remote_rank_model"].default
    assert inspect.signature(AnthropicClient).parameters["model"].default == want
    assert inspect.signature(build_scheduler).parameters["remote_rank_model"].default == want
