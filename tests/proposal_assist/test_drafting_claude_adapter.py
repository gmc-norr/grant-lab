"""Real-SDK adapter tests. Skipped unless ANTHROPIC_API_KEY is set in
env — CI without a key shouldn't hit the API. The protocol-conformance
test runs always (no SDK calls)."""

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
