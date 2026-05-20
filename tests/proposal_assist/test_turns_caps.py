from datetime import UTC, datetime, timedelta

from call_monitor.proposal_assist.turns.caps import (
    TokenCapExceeded,
    check_daily_token_cap,
)
from tests.proposal_assist.conftest import (
    make_chat_turn,
    make_workspace,
)


def test_check_daily_token_cap_under_limit_returns_allowed(
    pg_session_factory,
    _settings_env,
):
    """Workspace with zero token usage today is allowed."""
    from call_monitor.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is True
    assert result.used_in == 0
    assert result.used_out == 0
    assert result.cap_in == settings.daily_token_cap_input
    assert result.cap_out == settings.daily_token_cap_output


def test_check_daily_token_cap_over_input_limit_returns_not_allowed(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    """A workspace whose summed input tokens today >= cap is not allowed."""
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    monkeypatch.setenv("DAILY_TOKEN_CAP_OUTPUT", "1000000")
    from call_monitor.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "x"}, tokens_in=60)
        make_chat_turn(s, ws, role="assistant", content={"type": "text", "text": "y"}, tokens_in=50)
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is False
    assert result.used_in == 110
    assert result.reason == "input_cap_exceeded"


def test_check_daily_token_cap_respects_per_workspace_override(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    """The override on workspaces.daily_token_cap_in_override takes
    precedence over the setting default."""
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    from call_monitor.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.daily_token_cap_in_override = 10_000  # generous override
        make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "x"}, tokens_in=200
        )
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is True
    assert result.cap_in == 10_000


def test_check_daily_token_cap_only_counts_today(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    """Yesterday's turns don't count toward today's cap (Stockholm day boundary)."""
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    from call_monitor.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        # Backdate a turn by 2 days.
        old_turn = make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "x"}, tokens_in=200
        )
        old_turn.created_at = datetime.now(UTC) - timedelta(days=2)
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is True
    assert result.used_in == 0


def test_token_cap_exceeded_carries_diagnostic_fields():
    exc = TokenCapExceeded(
        workspace_id=__import__("uuid").uuid4(),
        used_in=200,
        used_out=50,
        cap_in=100,
        cap_out=1000,
        reason="input_cap_exceeded",
    )
    assert "200" in str(exc)
    assert exc.reason == "input_cap_exceeded"


def test_check_daily_token_cap_rejects_at_exact_boundary(
    pg_session_factory,
    _settings_env,
    monkeypatch,
):
    """Documents the `>=` rejection rule (cap is a hard ceiling, not 'one
    more please' permissive). A workspace whose usage exactly equals the
    cap is blocked — protects against a single in-flight turn pushing the
    workspace tens of thousands of tokens past budget."""
    monkeypatch.setenv("DAILY_TOKEN_CAP_INPUT", "100")
    monkeypatch.setenv("DAILY_TOKEN_CAP_OUTPUT", "1000000")
    from call_monitor.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        make_chat_turn(
            s, ws, role="assistant", content={"type": "text", "text": "x"}, tokens_in=100
        )
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is False
    assert result.used_in == 100
    assert result.cap_in == 100
    assert result.reason == "input_cap_exceeded"


def test_check_daily_token_cap_override_zero_pauses_workspace(
    pg_session_factory,
    _settings_env,
):
    """Override=0 is the documented admin "pause this workspace" hook:
    any turn is blocked, even with zero accumulated usage."""
    from call_monitor.config import Settings

    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.daily_token_cap_in_override = 0
        s.commit()
        result = check_daily_token_cap(s, ws.id, settings=settings)
    assert result.allowed is False
    assert result.cap_in == 0
    assert result.used_in == 0
    assert result.reason == "input_cap_exceeded"
