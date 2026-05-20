from call_manager.config import Settings


def test_proposal_assist_settings_have_defaults(monkeypatch, _settings_env):
    # _settings_env provides DATABASE_URL etc.; clear only the new keys
    # to verify they take their declared defaults.
    monkeypatch.delenv("EXTRACTOR_VERSION", raising=False)
    monkeypatch.delenv("RUBRIC_EXTRACTOR_VERSION", raising=False)
    monkeypatch.delenv("PROPOSAL_ASSIST_LLM_MODEL", raising=False)
    s = Settings()
    assert s.extractor_version == "sections-v1"
    assert s.rubric_extractor_version == "rubric-v1"
    assert s.proposal_assist_llm_model == "claude-opus-4-7"


def test_proposal_assist_settings_override_via_env(monkeypatch, _settings_env):
    monkeypatch.setenv("EXTRACTOR_VERSION", "sections-v2")
    monkeypatch.setenv("RUBRIC_EXTRACTOR_VERSION", "rubric-v2")
    monkeypatch.setenv("PROPOSAL_ASSIST_LLM_MODEL", "claude-sonnet-4-6")
    s = Settings()
    assert s.extractor_version == "sections-v2"
    assert s.rubric_extractor_version == "rubric-v2"
    assert s.proposal_assist_llm_model == "claude-sonnet-4-6"


def test_phase0_scheduler_settings_have_defaults(monkeypatch, _settings_env):
    monkeypatch.delenv("PHASE0_TICK_SECONDS", raising=False)
    monkeypatch.delenv("WORKSPACES_DIR", raising=False)
    s = Settings()
    assert s.phase0_tick_seconds == 30
    assert s.workspaces_dir == "var/workspaces"


def test_phase0_scheduler_settings_override_via_env(monkeypatch, _settings_env):
    monkeypatch.setenv("PHASE0_TICK_SECONDS", "60")
    monkeypatch.setenv("WORKSPACES_DIR", "/data/workspaces")
    s = Settings()
    assert s.phase0_tick_seconds == 60
    assert s.workspaces_dir == "/data/workspaces"


def test_workspace_idle_timeout_seconds_default(monkeypatch, _settings_env):
    monkeypatch.delenv("WORKSPACE_IDLE_TIMEOUT_SECONDS", raising=False)
    assert Settings().workspace_idle_timeout_seconds == 600


def test_workspace_idle_timeout_seconds_override(monkeypatch, _settings_env):
    monkeypatch.setenv("WORKSPACE_IDLE_TIMEOUT_SECONDS", "120")
    assert Settings().workspace_idle_timeout_seconds == 120
