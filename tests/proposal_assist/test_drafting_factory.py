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


def test_lock_sessions_never_come_from_the_shared_request_engine(
    _settings_env, make_app, pg_session_factory, monkeypatch
):
    """The drafting advisory-lock pool must be a DIFFERENT engine from the
    request pool — and nothing pinned that (review 2026-10-26).

    Each live drafting subprocess keeps a session-level Postgres advisory lock on
    a checked-out connection for its whole life (up to
    WORKSPACE_IDLE_TIMEOUT_SECONDS). On the shared request engine that pins one
    of its connections per live workspace, so a handful of active workspaces
    starves every web request and every scheduler tick. `build_workspace_controller`
    builds `make_drafting_engine(...)` for exactly this reason, but
    `WorkspaceController.__init__` falls back to `session_factory` when none is
    passed — so dropping the dedicated engine is a silent one-line regression.

    The PRECONDITION assertion matters: it proves `app.state.session_factory` is
    bound to `app.state.engine` in the first place, so "is not" below is a real
    comparison and not two unrelated objects.
    """
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    monkeypatch.setenv("WORKSPACES_DIR", "/tmp/test_workspaces")
    app = make_app()

    # Precondition: the request pool IS app.state.engine.
    assert app.state.session_factory.kw["bind"] is app.state.engine

    controller = build_workspace_controller(
        settings=app.state.settings,
        session_factory=app.state.session_factory,
    )
    lock_factory = controller._lock_session_factory
    lock_engine = lock_factory.kw["bind"]
    try:
        assert lock_factory is not app.state.session_factory
        assert lock_engine is not app.state.engine, (
            "drafting lock sessions must not be drawn from the shared request pool"
        )
        assert lock_engine.pool.size() == app.state.settings.drafting_db_pool_size, (
            f"the dedicated pool must be sized to the live-subprocess ceiling; "
            f"got {lock_engine.pool.size()} vs "
            f"{app.state.settings.drafting_db_pool_size}"
        )
    finally:
        lock_engine.dispose()
