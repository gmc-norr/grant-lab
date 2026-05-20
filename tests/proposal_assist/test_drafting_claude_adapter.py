"""Real-SDK adapter tests. Skipped unless ANTHROPIC_API_KEY is set in
env — CI without a key shouldn't hit the API. The protocol-conformance
and unit tests run always (no SDK calls)."""

import os
import uuid

import pytest

from call_monitor.proposal_assist.drafting.claude_adapter import (
    ClaudeAgentSDKAdapter,
)
from call_monitor.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    DraftingAgent,
    LaunchContext,
    TurnDoneEvent,
)

# ---------------------------------------------------------------------------
# M1 — _flatten_content unit tests (no SDK calls)
# ---------------------------------------------------------------------------


def test_flatten_content_handles_single_dict():
    """resume.py produces SDK-native shapes — a single text block is a
    dict, not a list. _flatten_content must extract .text, not iterate
    the dict's keys."""
    from call_monitor.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    assert _flatten_content({"type": "text", "text": "hello"}) == "hello"


def test_flatten_content_handles_list_of_dicts():
    from call_monitor.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    blocks = [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ]
    assert _flatten_content(blocks) == "hello world"


def test_flatten_content_handles_string_passthrough():
    from call_monitor.proposal_assist.drafting.claude_adapter import (
        _flatten_content,
    )

    assert _flatten_content("hi") == "hi"


def test_flatten_content_returns_empty_for_non_text_block():
    """Image / tool_use blocks can't be replayed as text — they're
    dropped (returns empty string)."""
    from call_monitor.proposal_assist.drafting.claude_adapter import (
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

    from call_monitor.proposal_assist.drafting.claude_adapter import (
        ClaudeAgentSDKAdapter,
        SDKTurnError,
    )
    from call_monitor.proposal_assist.drafting.contract import AgentHandle

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
