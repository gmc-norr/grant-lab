from call_manager.config import Settings


def _env(monkeypatch, **extra):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_escalation_settings_have_defaults(monkeypatch):
    _env(monkeypatch)
    s = Settings()
    assert s.ranking_escalation_min_score == 45
    assert s.ranking_remote_daily_cap == 100


def test_cosine_threshold_setting_removed(monkeypatch):
    _env(monkeypatch)
    s = Settings()
    assert not hasattr(s, "remote_rank_cosine_threshold")
