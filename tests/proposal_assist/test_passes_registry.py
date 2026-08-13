"""Registry gates in isolation — no DB, no SDK.

Every test builds its OWN PassRegistry. The PASS_REGISTRY singleton is
process-global and its unconfirmed list is deliberately persistent, so sharing it
across tests makes one test's leftover entry the next one's failed assertion.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from call_manager.proposal_assist.passes import PassRegistry


@pytest.fixture
def registry() -> PassRegistry:
    """A fresh registry per test. Never the singleton."""
    return PassRegistry()


class _FakeAdapter:
    """The adapter surface the registry uses: an inventory it can be asked about,
    and a way to retire what is left."""

    def __init__(self, *, open_generations: int = 0, retire_delay: float = 0.0):
        self._open = [uuid.uuid4() for _ in range(open_generations)]
        self._retire_delay = retire_delay
        self.retire_calls = 0
        self.confirm_on_retire = True

    def unconfirmed_launch_ids(self):
        return list(self._open)

    async def retire_unconfirmed(self) -> int:
        self.retire_calls += 1
        if self._retire_delay:
            await asyncio.sleep(self._retire_delay)
        n = len(self._open)
        if self.confirm_on_retire:
            self._open.clear()
        return n


@pytest.mark.asyncio
async def test_the_entry_exists_from_the_very_start_not_after_the_prefix(registry):
    """The race an after-the-prefix registration leaves open: an entry that only
    appears once the prefix has committed is invisible to a DELETE that commits
    during it."""
    ws = uuid.uuid4()

    async with registry.track(ws) as entry:
        seen_before_adapter = registry.live_count(ws)  # no adapter bound yet
        entry.bind_adapter(_FakeAdapter())

    assert seen_before_adapter == 1
    assert registry.live_count(ws) == 0


@pytest.mark.asyncio
async def test_track_deregisters_when_the_body_raises(registry):
    ws = uuid.uuid4()
    with pytest.raises(ValueError):
        async with registry.track(ws) as entry:
            entry.bind_adapter(_FakeAdapter())
            raise ValueError("boom")
    assert registry.live_count(ws) == 0


@pytest.mark.asyncio
async def test_retire_cancels_the_task_and_retires_the_adapter(registry):
    ws = uuid.uuid4()
    adapter = _FakeAdapter(open_generations=1)
    started = asyncio.Event()

    async def _pass():
        async with registry.track(ws) as entry:
            entry.bind_adapter(adapter)
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(_pass())
    await started.wait()
    retired = await registry.retire_workspace(ws)

    assert retired == 1
    assert task.done()
    assert adapter.retire_calls == 1
    assert registry.live_count(ws) == 0


@pytest.mark.asyncio
async def test_entries_retire_concurrently_not_serially(registry):
    """Mutation: replace the gather in retire_workspace with a for loop."""
    ws = uuid.uuid4()
    started = asyncio.Event()
    n = 6

    async def _pass(idx: int):
        async with registry.track(ws) as entry:
            entry.bind_adapter(_FakeAdapter(open_generations=1, retire_delay=0.3))
            if idx == n - 1:
                started.set()
            await asyncio.sleep(3600)

    tasks = [asyncio.create_task(_pass(i)) for i in range(n)]
    await started.wait()
    await asyncio.sleep(0.05)

    loop = asyncio.get_running_loop()
    began = loop.time()
    await registry.retire_workspace(ws)
    elapsed = loop.time() - began

    assert elapsed < 1.0, f"retirement took {elapsed:.2f}s — serialized, not concurrent"
    for t in tasks:
        assert t.done()


@pytest.mark.asyncio
async def test_a_generation_left_open_transfers_on_ORDINARY_exit(registry):
    """The case that matters most: no deletion involved at all.

    Also the launch-cancel case, because unconfirmed_launch_ids() reports a
    generation registered before connect() even though no handle ever existed.
    """
    ws = uuid.uuid4()
    adapter = _FakeAdapter(open_generations=1)
    adapter.confirm_on_retire = False

    async with registry.track(ws) as entry:
        entry.bind_adapter(adapter)

    assert registry.live_count(ws) == 0
    assert registry.unconfirmed_count() == 1


@pytest.mark.asyncio
async def test_reconcile_retries_and_drops_once_confirmed(registry):
    ws = uuid.uuid4()
    adapter = _FakeAdapter(open_generations=1)
    adapter.confirm_on_retire = False
    async with registry.track(ws) as entry:
        entry.bind_adapter(adapter)
    assert registry.unconfirmed_count() == 1

    adapter.confirm_on_retire = True
    acted = await registry.reconcile_unconfirmed()

    assert acted == 1
    assert registry.unconfirmed_count() == 0
