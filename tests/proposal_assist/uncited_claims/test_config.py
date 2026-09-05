from grant_lab.config import Settings


def test_uncited_claims_config_defaults(_settings_env):
    s = Settings()
    assert s.uncited_claims_tick_seconds == 20
    assert s.uncited_claims_pass_timeout_seconds == 180
    assert s.uncited_claims_job_timeout_seconds == 300
    assert s.uncited_claims_reaper_tick_seconds == 60
    assert s.uncited_claims_terminate_timeout_seconds == 30
    assert s.uncited_claims_daily_token_cap == 200_000
