import pytest

from call_manager.config import Settings


def _base(monkeypatch, **extra):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    return Settings()


def test_default_none(monkeypatch):
    assert _base(monkeypatch).initial_admin_password is None


def test_empty_is_none(monkeypatch):
    assert _base(monkeypatch, INITIAL_ADMIN_PASSWORD="   ").initial_admin_password is None


def test_short_nonempty_rejected(monkeypatch):
    with pytest.raises(ValueError, match="INITIAL_ADMIN_PASSWORD"):
        _base(monkeypatch, INITIAL_ADMIN_PASSWORD="short")


def test_valid_kept_unmodified(monkeypatch):
    # surrounding spaces are part of the secret — must NOT be stripped
    s = _base(monkeypatch, INITIAL_ADMIN_PASSWORD="  spaced-strong-pw  ")
    assert s.initial_admin_password == "  spaced-strong-pw  "
