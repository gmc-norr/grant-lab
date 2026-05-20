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
