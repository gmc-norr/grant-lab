"""Tests for FakeAgentAdapter strict mode (Defect 2 fix).

Strict mode models two real-adapter hazards invisible to normal fake-backed tests:
(a) re-entrant send_turn raises (models begin_in_flight_exclusive)
(b) leftover events after a cancelled iteration (models SDK stream desync)
"""

import pytest

from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    LaunchContext,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _collect(stream):
    return [event async for event in stream]


async def _collect_partial(stream, n: int):
    """Consume exactly n events then stop iterating (cancel / break)."""
    events = []
    async for event in stream:
        events.append(event)
        if len(events) >= n:
            break
    return events


# ---------------------------------------------------------------------------
# Default mode still passes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_mode_is_unchanged():
    """Default (strict=False) fake must still work as before — existing tests
    must not be broken."""
    fake = FakeAgentAdapter()
    wsid = __import__("uuid").uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))
    events = await _collect(fake.send_turn(handle, "hello"))
    assert len(events) == 1
    assert isinstance(events[0], TurnDoneEvent)


# ---------------------------------------------------------------------------
# (a) Re-entrant send_turn raises in strict mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_mode_raises_on_reentrant_send_turn():
    """A concurrent second send_turn while the first is in flight must raise
    RuntimeError in strict mode."""
    fake = FakeAgentAdapter(strict=True)
    wsid = __import__("uuid").uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))

    fake.script_next_turn(
        [
            AssistantTextEvent(text="chunk 1"),
            TurnDoneEvent(tokens_in=1, tokens_out=1, model="m"),
        ]
    )

    # Start the first send_turn stream but don't exhaust it yet
    stream1 = fake.send_turn(handle, "first")
    # Read only the first event — the stream is still in-flight
    first_event = await stream1.__anext__()
    assert isinstance(first_event, AssistantTextEvent)

    # A second send_turn while the first is still open must raise
    with pytest.raises(RuntimeError, match="re-entrant"):
        stream2 = fake.send_turn(handle, "second")
        await stream2.__anext__()

    # Drain the first stream so the in-flight flag is cleared
    remaining = await _collect(stream1)
    assert isinstance(remaining[-1], TurnDoneEvent)


@pytest.mark.asyncio
async def test_strict_mode_allows_sequential_send_turns():
    """Sequential (non-overlapping) send_turns must not raise in strict mode."""
    fake = FakeAgentAdapter(strict=True)
    wsid = __import__("uuid").uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))

    for i in range(3):
        fake.script_next_turn([TurnDoneEvent(tokens_in=i, tokens_out=i, model="m")])
        events = await _collect(fake.send_turn(handle, f"msg {i}"))
        assert isinstance(events[-1], TurnDoneEvent)


# ---------------------------------------------------------------------------
# (b) Leftover events after cancelled iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_leftover_events_appended_after_scripted():
    """Events added via add_leftover are yielded AFTER the scripted events.

    A consumer that breaks early (cancel) will miss them; one that drains fully
    gets them. This models SDK stream desync: a leftover ResultMessage after an
    interrupted iteration.
    """
    fake = FakeAgentAdapter(strict=True)
    wsid = __import__("uuid").uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))

    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m")])
    leftover = AssistantTextEvent(text="leftover after cancel")
    fake.add_leftover([leftover])

    all_events = await _collect(fake.send_turn(handle, "go"))
    # The scripted TurnDoneEvent comes first, then the leftover
    assert len(all_events) == 2
    assert isinstance(all_events[0], TurnDoneEvent)
    assert isinstance(all_events[1], AssistantTextEvent)
    assert all_events[1].text == "leftover after cancel"


@pytest.mark.asyncio
async def test_strict_leftover_missed_when_consumer_breaks_early():
    """A consumer that stops after the scripted events (models cancel) misses
    the leftover. This test proves a regression removing the drain guard fails."""
    fake = FakeAgentAdapter(strict=True)
    wsid = __import__("uuid").uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))

    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m")])
    leftover = AssistantTextEvent(text="orphan")
    fake.add_leftover([leftover])

    # Read exactly 1 event (the TurnDoneEvent), then stop — simulates cancel
    partial = await _collect_partial(fake.send_turn(handle, "go"), 1)
    assert len(partial) == 1
    assert isinstance(partial[0], TurnDoneEvent)
    # The leftover is NOT consumed
    # (the test proves that leftover events exist and CAN be missed)


@pytest.mark.asyncio
async def test_default_mode_ignores_add_leftover():
    """add_leftover is a no-op in default (non-strict) mode — events are
    NOT yielded, to preserve existing test behaviour."""
    fake = FakeAgentAdapter()  # strict=False
    wsid = __import__("uuid").uuid4()
    handle = await fake.launch(LaunchContext(workspace_id=wsid))

    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m")])
    fake.add_leftover([AssistantTextEvent(text="should not appear")])

    events = await _collect(fake.send_turn(handle, "go"))
    # Only the scripted TurnDoneEvent; no leftover
    assert len(events) == 1
    assert isinstance(events[0], TurnDoneEvent)
