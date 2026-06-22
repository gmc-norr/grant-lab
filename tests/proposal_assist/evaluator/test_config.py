from call_manager.config import Settings


def test_evaluator_settings_defaults(_settings_env):
    s = Settings()
    assert s.evaluator_tick_seconds == 20
    assert s.evaluator_pass_timeout_seconds == 180
    assert s.evaluator_job_timeout_seconds == 300
    assert s.evaluator_reaper_tick_seconds == 60
    assert s.evaluator_terminate_timeout_seconds == 30
    assert s.evaluator_daily_token_cap == 200_000
