from call_manager.config import Settings


def test_spa_mode_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("SPA_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "disabled"


def test_spa_mode_from_env(monkeypatch):
    monkeypatch.setenv("SPA_MODE", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    assert Settings().spa_mode == "prod"
