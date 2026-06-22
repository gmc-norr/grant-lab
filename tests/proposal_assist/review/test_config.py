from decimal import Decimal

import pytest
from pydantic import ValidationError

from call_manager.config import Settings


def test_review_defaults(_settings_env):
    s = Settings()
    assert s.review_persona_concurrency == 3
    assert s.review_runs_per_day_default == 5
    assert s.review_dollar_cap_default == Decimal("5.000")
    assert s.review_price_input_per_mtok == Decimal("5.00")
    assert s.review_price_output_per_mtok == Decimal("25.00")
    assert s.review_job_timeout_seconds == 900


def test_review_persona_concurrency_floor(_settings_env, monkeypatch):
    """Semaphore(0) would hang the whole wave-1 fan-out until the reaper TTL, so
    the config must reject a concurrency below 1 at load time, not at runtime."""
    monkeypatch.setenv("REVIEW_PERSONA_CONCURRENCY", "0")
    with pytest.raises(ValidationError):
        Settings()
