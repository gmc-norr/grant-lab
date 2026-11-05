# Task 5: build_scheduler call sites, value by value

Measured by the prototype at `3c1c0f31` + `06-scheduler-settings-once.patch`. The reviewer checks that every
new call carries the same values as the old one. The guard test added in Task 4
(`test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job`) is changed the same way.

OLD: ollama_url="http://localhost:11434", gemma_model="gemma4:e4b-q8", ranking_tick_seconds=60, ranking_queue_batch=10, embedding_pregate_threshold=0.30 (hard_fail omitted => None, api_key omitted => None, rest default)
NEW: same five as Settings fields + embedding_hard_fail_threshold=None + anthropic_api_key=None (explicit, to preserve old effective values)

| file::test | old kwargs | new settings fields |
|---|---|---|
| test_scheduler.py::test_build_scheduler_returns_one_job_per_active_adapter | common OLD | common NEW |
| ::test_build_scheduler_includes_rate_limit_sweep_when_limiters_given | common OLD | common NEW |
| ::test_build_scheduler_omits_rate_limit_sweep_when_limiters_empty | common OLD | common NEW |
| ::test_build_scheduler_includes_rank_drain_job | common OLD | common NEW |
| ::test_auto_publish_tick_logs_its_summary_and_swallows_a_failure | common OLD | common NEW |
| ::test_daily_cron_jobs_set_misfire_grace_time | common OLD | common NEW |
| ::(board-alerts-from-digest test, ~line 375) | common OLD + digest_settings=settings | common NEW; digest_settings dropped (was the same object) |
| test_scheduler_embedding_cursor.py: fixtures embeddings_tick, ..._calls_fail, ..._profiles_fail (3 calls) | common OLD | common NEW |
| test_scheduler_ranking_logprobs.py::_run_drain_job (used by its tests) | common OLD | common NEW applied on the settings the test passes (logprobs flag preserved) |
| test_scheduler_single_instance.py::_build | common OLD | common NEW |
| test_profiles_bootstrap_worker.py::test_scheduler_registers_bootstrap_tick | gemma_model="g", rest common | gemma_model="g", rest common NEW |
| test_ranking_freshness_wiring.py::test_the_scheduler_hands_enqueue_the_drain_values[None / key] | url localhost, gemma4:e4b-q8, 60, 10, pregate 0.27, hard_fail 0.18, anthropic_api_key=api_key, escalation_min_score=50 | same 8 as fields on Settings().model_copy |
| test_ranking_remote_effort.py::_build | ollama_url=settings.ollama_url, gemma_model=settings.gemma_model (self-referential, dropped), 60, 10, 0.3, anthropic_api_key="sk-test-not-real", remote_rank_model="claude-sonnet-5-5" | 60, 10, 0.3, hard_fail None, api_key "sk-test-not-real", remote_rank_model same |
| test_scheduler_digest.py::_build (3 tests) | ollama_url="http://ollama", gemma4:e4b-q8, 60, 10, 0.30; with_digest: digest_settings=MagicMock(digest_dry_run=False, admin_dashboard_url="http://app/") DIFFERENT from settings | same 5 fields + hard_fail None + api_key None; digest_dry_run=False and admin_dashboard_url="http://app/" set on settings for BOTH digest and non-digest cases (the non-digest case never read them). The digest job is only registered, never run, so no assertion depends on them |

Notes: test_ranking_scoring_config.py does not call build_scheduler (docstring only); its docstring "main.py maps these Settings fields one-to-one into build_scheduler" is now stale prose (not edited). 17 `build_scheduler(` calls found by grep (the task said ~18); 14 are direct kwargs calls, 1 via dict (digest).
