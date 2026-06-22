from decimal import Decimal

from call_manager.config import Settings


def test_review_defaults(_settings_env):
    s = Settings()
    assert s.review_persona_concurrency == 3
    assert s.review_runs_per_day_default == 5
    assert s.review_dollar_cap_default == Decimal("5.000")
    assert s.review_price_input_per_mtok == Decimal("5.00")
    assert s.review_price_output_per_mtok == Decimal("25.00")
    assert s.review_job_timeout_seconds == 900
