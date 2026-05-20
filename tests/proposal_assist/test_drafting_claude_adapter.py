"""Real-SDK adapter tests. Skipped unless ANTHROPIC_API_KEY is set in
env — CI without a key shouldn't hit the API. The protocol-conformance
and unit tests run always (no SDK calls)."""

import os
import uuid

import pytest

from call_manager.proposal_assist.drafting.claude_adapter import (
    ClaudeAgentSDKAdapter,
)
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    DraftingAgent,
    LaunchContext,
    TurnDoneEvent,
)

# ---------------------------------------------------------------------------
# M1 — _flatten_content unit tests (no SDK calls)
# ---------------------------------------------------------------------------


def test_flatten_content_handles_single_dict():
    """ToolResultBlock content may be a single text-block dict.
    _flatten_content must extract .text, not iterate the dict's keys."""
    from call_manager.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    assert _flatten_content({"type": "text", "text": "hello"}) == "hello"


def test_flatten_content_handles_list_of_dicts():
    from call_manager.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    blocks = [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ]
    assert _flatten_content(blocks) == "hello world"


def test_flatten_content_handles_string_passthrough():
    from call_manager.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    assert _flatten_content("hi") == "hi"


def test_flatten_content_returns_empty_for_non_text_block():
    """Non-text ToolResultBlock content (image, tool_use) is dropped
    (returns empty string)."""
    from call_manager.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    assert _flatten_content({"type": "image", "source": {}}) == ""
    assert _flatten_content({"type": "tool_use", "id": "x", "name": "y"}) == ""


# ---------------------------------------------------------------------------
# M2 — SDKTurnError on ResultMessage error result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_turn_raises_on_sdk_error_result():
    """A ResultMessage with is_error=True must raise SDKTurnError, not
    pass through as a successful TurnDoneEvent. B4 catches this and
    marks the turn failed."""
    from unittest.mock import AsyncMock, MagicMock

    from claude_agent_sdk.types import ResultMessage

    from call_manager.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
        SDKTurnError,
    )
    from call_manager.proposal_assist.drafting.contract import AgentHandle

    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )

    # Build a fake client that yields a single ResultMessage with is_error=True.
    fake_client = MagicMock()
    fake_client.query = AsyncMock()

    async def fake_receive():
        # Construct a ResultMessage with is_error=True. The SDK's actual
        # __init__ has many required fields — use object.__new__ to
        # bypass __init__ and set only what we need.
        rm = object.__new__(ResultMessage)
        rm.is_error = True
        rm.api_error_status = "529"
        rm.errors = ["overloaded"]
        rm.model_usage = None
        yield rm

    fake_client.receive_response = fake_receive

    handle = AgentHandle(
        workspace_id=uuid.uuid4(),
        state={"client": fake_client},
    )

    with pytest.raises(SDKTurnError, match="529"):
        async for _ in adapter.send_turn(handle, "hi"):
            pass


# ---------------------------------------------------------------------------
# B3 — native session-ID resume scaffolding (no SDK calls)
# ---------------------------------------------------------------------------


def test_launch_uses_resume_session_id_in_options(monkeypatch):
    """When LaunchContext.resume_session_id is set, the adapter passes
    it to ClaudeAgentOptions(resume=...) so the SDK natively resumes
    the prior conversation. Verified by spying on options construction."""
    import asyncio

    from call_manager.proposal_assist.drafting import claude_adapter as ca

    captured: dict[str, object] = {}
    real_options = ca.ClaudeAgentOptions

    def spy_options(**kw):
        captured.update(kw)
        return real_options(**kw)

    monkeypatch.setattr(ca, "ClaudeAgentOptions", spy_options)

    class _StubClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def disconnect(self):
            pass

    monkeypatch.setattr(ca, "ClaudeSDKClient", _StubClient)

    adapter = ca.ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )
    ctx = LaunchContext(
        workspace_id=uuid.uuid4(),
        resume_session_id="abc-123",
    )
    asyncio.run(adapter.launch(ctx))
    assert captured.get("resume") == "abc-123"


def test_launch_omits_resume_when_session_id_is_none(monkeypatch):
    """No resume_session_id → no resume= in options (fresh session)."""
    import asyncio

    from call_manager.proposal_assist.drafting import claude_adapter as ca

    captured: dict[str, object] = {}
    real_options = ca.ClaudeAgentOptions

    def spy_options(**kw):
        captured.update(kw)
        return real_options(**kw)

    monkeypatch.setattr(ca, "ClaudeAgentOptions", spy_options)

    class _StubClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def disconnect(self):
            pass

    monkeypatch.setattr(ca, "ClaudeSDKClient", _StubClient)

    adapter = ca.ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )
    ctx = LaunchContext(workspace_id=uuid.uuid4())
    asyncio.run(adapter.launch(ctx))
    assert "resume" not in captured


@pytest.mark.asyncio
async def test_send_turn_captures_session_id_on_turn_done():
    """ResultMessage.uuid (SDK 0.2.82 attribute name) flows through to
    TurnDoneEvent.session_id so B4 can persist it for native resume on
    the next launch."""
    from unittest.mock import AsyncMock, MagicMock

    from claude_agent_sdk.types import ResultMessage

    from call_manager.proposal_assist.drafting.contract import AgentHandle

    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/x",
    )

    fake_client = MagicMock()
    fake_client.query = AsyncMock()

    async def fake_receive():
        rm = object.__new__(ResultMessage)
        rm.is_error = False
        rm.api_error_status = None
        rm.errors = None
        rm.model_usage = None
        # SDK 0.2.82 uses `uuid`; the adapter probes both names.
        rm.uuid = "session-abc-123"
        yield rm

    fake_client.receive_response = fake_receive

    handle = AgentHandle(workspace_id=uuid.uuid4(), state={"client": fake_client})

    events = []
    async for ev in adapter.send_turn(handle, "hi"):
        events.append(ev)

    done = next(ev for ev in events if isinstance(ev, TurnDoneEvent))
    assert done.session_id == "session-abc-123"


# ---------------------------------------------------------------------------
# Protocol conformance (no SDK calls)
# ---------------------------------------------------------------------------


def test_claude_adapter_implements_protocol():
    """No SDK calls — just checks the class satisfies the Protocol.
    Runs unconditionally (no API key needed)."""
    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/test_workspaces",
    )
    assert isinstance(adapter, DraftingAgent)


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="real SDK tests require ANTHROPIC_API_KEY",
)
@pytest.mark.asyncio
async def test_claude_adapter_can_round_trip_a_simple_turn():
    """End-to-end: launch, send a one-line turn, receive at least one
    AssistantTextEvent and a TurnDoneEvent. Uses Haiku for speed/cost."""
    adapter = ClaudeAgentSDKAdapter(
        model="claude-haiku-4-5",
        workspaces_dir="/tmp/test_workspaces",
    )
    wsid = uuid.uuid4()
    ctx = LaunchContext(workspace_id=wsid)
    handle = await adapter.launch(ctx)
    try:
        events = []
        async for event in adapter.send_turn(handle, "Say 'hello'."):
            events.append(event)
        assert any(isinstance(e, AssistantTextEvent) for e in events)
        assert isinstance(events[-1], TurnDoneEvent)
    finally:
        await adapter.terminate(handle)
