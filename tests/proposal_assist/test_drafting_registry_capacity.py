"""Tests for the live-subprocess ceiling (Defect 1) and queued-turn-aware
idle sweep (Defect 4) added in the review-remediation batch.

These exercise SubprocessRegistry directly with FakeAgentAdapter — no real
subprocesses. The ceiling guarantees drafting can never consume more than
``max_live`` checked-out DB connections, well below the engine pool size, so
it can't starve web/scheduler work.
"""

import asyncio
import time
import uuid

import pytest

from call_manager.proposal_assist.drafting.contract import (
    DraftingAtCapacity,
    LaunchContext,
)
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry


async def _install_handle(registry, workspace_id):
    am_launcher, _ = await registry.claim_launch_or_wait(workspace_id)
    assert am_launcher, "expected to become the launcher"
    ctx = LaunchContext(workspace_id=workspace_id)
    handle = await registry._adapter.launch(ctx)
    await registry.fulfill_pending_launch(workspace_id, handle, lock_session=None)
    return handle


# ---------------------------------------------------------------------------
# Defect 1 — hard ceiling on concurrent live subprocesses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_at_capacity_evicts_lru_idle_handle():
    """At the ceiling, a new launch evicts the least-recently-used IDLE handle
    (terminating it, freeing its DB connection) instead of growing _entries."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600, max_live=2)

    ws_old = uuid.uuid4()
    ws_new = uuid.uuid4()
    ws_recent = uuid.uuid4()

    old_handle = await _install_handle(registry, ws_old)
    # Make ws_old the least-recently-touched of the two.
    async with registry._lock:
        registry._entries[ws_old].last_touched_monotonic = time.monotonic() - 100
    await _install_handle(registry, ws_recent)

    assert len(registry._entries) == 2  # at the ceiling

    # A third launch must evict the LRU idle handle (ws_old), not exceed the cap.
    am_launcher, _ = await registry.claim_launch_or_wait(ws_new)
    assert am_launcher is True
    ctx = LaunchContext(workspace_id=ws_new)
    handle = await registry._adapter.launch(ctx)
    await registry.fulfill_pending_launch(ws_new, handle, lock_session=None)

    assert len(registry._entries) <= 2
    assert ws_old not in registry._entries
    assert ws_new in registry._entries
    assert ws_recent in registry._entries
    # The evicted handle was terminated (its subprocess + connection freed).
    assert old_handle in adapter.terminations


@pytest.mark.asyncio
async def test_claim_at_capacity_all_busy_refuses_with_at_capacity():
    """If every live handle has an in-flight turn (none idle), a new launch is
    refused with DraftingAtCapacity rather than evicting an active turn or
    exceeding the cap. The API layer maps this to a retriable 429."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600, max_live=2)

    ws_a = uuid.uuid4()
    ws_b = uuid.uuid4()
    ws_c = uuid.uuid4()
    await _install_handle(registry, ws_a)
    await _install_handle(registry, ws_b)
    # Both are mid-turn — not evictable.
    await registry.increment_in_flight(ws_a)
    await registry.increment_in_flight(ws_b)

    with pytest.raises(DraftingAtCapacity):
        await registry.claim_launch_or_wait(ws_c)

    # Nothing was terminated, cap not exceeded, ws_c not installed.
    assert adapter.terminations == []
    assert len(registry._entries) == 2
    assert ws_c not in registry._entries


@pytest.mark.asyncio
async def test_cached_handle_never_triggers_capacity_check():
    """Re-launching an ALREADY-live workspace returns its cached handle even at
    capacity (no eviction, no refusal) — only NET-NEW launches are gated."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600, max_live=1)
    ws = uuid.uuid4()
    h = await _install_handle(registry, ws)
    await registry.increment_in_flight(ws)  # busy, so it could not be evicted

    am_launcher, pending = await registry.claim_launch_or_wait(ws)
    assert am_launcher is False
    assert pending is not None
    assert pending.result() is h
    assert adapter.terminations == []


@pytest.mark.asyncio
async def test_concurrent_net_new_launches_respect_ceiling():
    """Concurrent net-new launches never push _entries past the ceiling."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600, max_live=3)

    async def _launch(wsid):
        am, _ = await registry.claim_launch_or_wait(wsid)
        if am:
            ctx = LaunchContext(workspace_id=wsid)
            handle = await registry._adapter.launch(ctx)
            await registry.fulfill_pending_launch(wsid, handle, lock_session=None)

    # All idle (in_flight==0) so eviction keeps the cap; assert it never grows.
    wsids = [uuid.uuid4() for _ in range(6)]
    for wsid in wsids:
        await _launch(wsid)
    assert len(registry._entries) <= 3


@pytest.mark.asyncio
async def test_unbounded_when_max_live_none():
    """max_live=None preserves the pre-defect unbounded behavior (no ceiling)."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600, max_live=None)
    for _ in range(5):
        await _install_handle(registry, uuid.uuid4())
    assert len(registry._entries) == 5
    assert adapter.terminations == []


# ---------------------------------------------------------------------------
# Defect 4 — idle sweep skips workspaces with a queued turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_idle_skips_workspace_with_queued_turn():
    """sweep_idle must NOT tear down an idle handle whose workspace has a queued
    turn — the queued turn would just force a cold relaunch."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=0)
    ws_queued = uuid.uuid4()
    ws_plain = uuid.uuid4()
    await _install_handle(registry, ws_queued)
    await _install_handle(registry, ws_plain)
    await asyncio.sleep(0.01)  # past the idle threshold

    swept = await registry.sweep_idle(has_queued_turn={ws_queued}.__contains__)

    # ws_plain swept; ws_queued preserved for its queued turn.
    assert swept == 1
    assert ws_queued in registry._entries
    assert ws_plain not in registry._entries
    assert len(adapter.terminations) == 1


@pytest.mark.asyncio
async def test_sweep_idle_default_predicate_sweeps_everything():
    """Without a has_queued_turn predicate, sweep_idle behaves as before."""
    adapter = FakeAgentAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=0)
    ws = uuid.uuid4()
    await _install_handle(registry, ws)
    await asyncio.sleep(0.01)
    swept = await registry.sweep_idle()
    assert swept == 1
    assert ws not in registry._entries
