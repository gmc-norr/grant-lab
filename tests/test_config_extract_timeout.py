"""Config: the untrusted-file extraction wall-clock budget."""

from __future__ import annotations

from call_manager.config import Settings


def _env(monkeypatch, **extra):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_extract_timeout_defaults_to_30s(monkeypatch):
    _env(monkeypatch)
    assert Settings().reference_extract_timeout_seconds == 30.0


def test_extract_timeout_is_env_overridable(monkeypatch):
    _env(monkeypatch, REFERENCE_EXTRACT_TIMEOUT_SECONDS="5.5")
    assert Settings().reference_extract_timeout_seconds == 5.5
