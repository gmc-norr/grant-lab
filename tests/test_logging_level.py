"""Review 2026-09-02 §2.6h: app.log grew 32 MB/day of APScheduler "Running job"
INFO lines; there was no LOG_LEVEL setting and logging.py set only the root level.
"""

from __future__ import annotations

import logging

import pytest

from grant_lab.config import Settings
from grant_lab.logging import QUIET_LOGGERS, configure_logging


@pytest.fixture
def _restore_levels():
    root = logging.getLogger()
    saved = {"": root.level, **{n: logging.getLogger(n).level for n in QUIET_LOGGERS}}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


def test_apscheduler_is_held_at_warning_under_an_info_root(_restore_levels):
    configure_logging(logging.INFO)
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("apscheduler").getEffectiveLevel() == logging.WARNING
    assert not logging.getLogger("apscheduler.executors.default").isEnabledFor(logging.INFO)


def test_a_higher_root_level_wins_over_the_quiet_floor(_restore_levels):
    configure_logging("ERROR")
    assert logging.getLogger().level == logging.ERROR
    assert logging.getLogger("apscheduler").getEffectiveLevel() == logging.ERROR


def test_level_names_are_accepted_and_bad_ones_refused(_restore_levels):
    configure_logging("debug")
    assert logging.getLogger().level == logging.DEBUG
    with pytest.raises(ValueError):
        configure_logging("LOUD")


def _settings(monkeypatch, **overrides):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/db")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "umu.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_settings_validate_log_level(monkeypatch):
    assert _settings(monkeypatch).log_level == "INFO"
    assert _settings(monkeypatch, LOG_LEVEL="warning").log_level == "WARNING"
    with pytest.raises(ValueError):
        _settings(monkeypatch, LOG_LEVEL="verbose")


def test_create_app_applies_the_configured_level(monkeypatch, _restore_levels):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    _settings(monkeypatch)  # sets the rest of the env
    from grant_lab.main import create_app

    create_app()  # lifespan not entered: no DB needed
    assert logging.getLogger().level == logging.WARNING
