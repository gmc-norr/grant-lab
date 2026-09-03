"""Review 2026-09-02 §2.6c / A9 F2: README step 4 (`uv run alembic upgrade head`)
failed exactly as written — alembic/env.py read only `os.environ["DATABASE_URL"]`
while the app reads `.env`, and nothing said to export the file. The natural
workaround put the production password on the command line.

`call_manager.db.url.resolve_database_url` is the one resolver: explicit URL,
then the environment, then `.env` through the app's own Settings.
"""

from __future__ import annotations

import pathlib

import pytest

from call_manager.db.url import resolve_database_url

ENV_PY = pathlib.Path(__file__).resolve().parents[1] / "alembic/env.py"


def _write_dotenv(tmp_path, url: str) -> None:
    (tmp_path / ".env").write_text(
        f"DATABASE_URL={url}\n"
        "SESSION_SECRET=test-secret-at-least-16-chars\n"
        "ALLOWED_EMAIL_DOMAINS=umu.se\n"
        "APP_BASE_URL=http://testserver\n"
    )


def test_explicit_url_wins(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://env@h/db")
    assert resolve_database_url("postgresql+psycopg://explicit@h/db").startswith(
        "postgresql+psycopg://explicit"
    )


def test_environment_beats_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _write_dotenv(tmp_path, "postgresql+psycopg://file@h/db")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://env@h/db")
    assert resolve_database_url(None) == "postgresql+psycopg://env@h/db"


def test_dotenv_is_read_when_nothing_is_exported(monkeypatch, tmp_path):
    """The README path: a `.env` in the working directory and a bare shell.
    The test suite disables Settings' env_file so the repo's own .env never
    leaks into tests; restore the production default for this one test."""
    from call_manager.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", ".env")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _write_dotenv(tmp_path, "postgresql+psycopg://file@h/db")
    assert resolve_database_url(None) == "postgresql+psycopg://file@h/db"


def test_nothing_configured_names_all_three_sources(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match=r"DATABASE_URL.*\.env.*set_main_option"):
        resolve_database_url(None)


def test_alembic_env_uses_the_shared_resolver():
    src = ENV_PY.read_text()
    assert "from call_manager.db.url import resolve_database_url" in src
    assert "resolve_database_url(config.get_main_option(" in src
    assert 'os.environ.get("DATABASE_URL")' not in src, "env.py must not keep its own copy"
