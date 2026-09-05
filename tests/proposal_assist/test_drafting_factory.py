import pytest

from grant_lab.proposal_assist.drafting.controller import (
    WorkspaceController,
)
from grant_lab.proposal_assist.drafting.factory import (
    build_workspace_controller,
)


def test_factory_with_fake_adapter_returns_controller(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    """Setting the env hook for fake adapter yields a working controller
    backed by FakeAgentAdapter (no SDK required)."""
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    monkeypatch.setenv("WORKSPACES_DIR", "/tmp/test_workspaces")

    from grant_lab.config import Settings

    settings = Settings()

    controller = build_workspace_controller(
        settings=settings,
        session_factory=pg_session_factory,
    )
    assert isinstance(controller, WorkspaceController)


def test_factory_with_claude_adapter_returns_controller(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    """Claude adapter (default) also returns a controller. No real SDK
    calls happen during construction — just object wiring."""
    monkeypatch.delenv("DRAFTING_ADAPTER", raising=False)  # default = claude
    monkeypatch.setenv("WORKSPACES_DIR", "/tmp/test_workspaces")

    from grant_lab.config import Settings

    settings = Settings()

    controller = build_workspace_controller(
        settings=settings,
        session_factory=pg_session_factory,
    )
    assert isinstance(controller, WorkspaceController)


def test_factory_raises_on_unknown_adapter_choice(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    monkeypatch.setenv("DRAFTING_ADAPTER", "totally-bogus")
    monkeypatch.setenv("WORKSPACES_DIR", "/tmp/test_workspaces")

    from grant_lab.config import Settings

    settings = Settings()

    with pytest.raises(ValueError, match="unknown DRAFTING_ADAPTER choice"):
        build_workspace_controller(
            settings=settings,
            session_factory=pg_session_factory,
        )
