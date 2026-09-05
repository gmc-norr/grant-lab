"""Registry gates in isolation — no DB, no SDK.

Every test builds its OWN PassRegistry. The PASS_REGISTRY singleton is
process-global and its unconfirmed list is deliberately persistent, so sharing it
across tests makes one test's leftover entry the next one's failed assertion.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

from grant_lab.proposal_assist.passes import PassRegistry


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
    retired = await registry.retire_workspace(ws, 1)

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
    await registry.retire_workspace(ws, 1)
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


@pytest.mark.asyncio
async def test_an_entry_appended_during_an_overlapping_reconcile_is_not_dropped(registry):
    """reconcile_unconfirmed must never REBIND the list.

    A run that assembles a survivor list from its own snapshot and publishes it
    with `self._unconfirmed = still` overwrites everything track()'s `finally`
    appended in the meantime — the entry is dropped for good and its leaked
    generation is never retried again.

    Scope: this pins the REBIND half only. That two runs may overlap at all is a
    separate property, pinned by
    test_a_wedged_reconcile_does_not_stop_the_next_one_from_making_progress.

    The interleaving driven here (each step deterministic, no sleeps):
      1. one unconfirmed entry, whose retire_unconfirmed() blocks on a gate;
      2. reconcile A starts and blocks in that call;
      3. reconcile B starts and blocks in a SECOND gated retire call;
      4. gate A opens, A finishes and drops the now-confirmed entry;
      5. a NEW unconfirmed entry arrives via track()'s ordinary exit path;
      6. gate B opens and B finishes.

    Against the rebinding version, B's rebind at step 6 publishes a list built
    before step 5 and the newcomer vanishes — unconfirmed_count() == 0.
    """
    ws = uuid.uuid4()

    class _GatedAdapter:
        """Open generation, confirmed dead only once its per-call gate opens."""

        def __init__(self) -> None:
            self._open = [uuid.uuid4()]
            self.calls = 0
            self.gates = [asyncio.Event(), asyncio.Event()]

        def unconfirmed_launch_ids(self):
            return list(self._open)

        async def retire_unconfirmed(self) -> int:
            idx = self.calls
            self.calls += 1
            if idx < len(self.gates):
                await self.gates[idx].wait()
            self._open.clear()
            return 1

    gated = _GatedAdapter()
    async with registry.track(ws) as first:
        first.bind_adapter(gated)
    assert registry.unconfirmed_count() == 1

    async def _settle() -> None:
        for _ in range(10):
            await asyncio.sleep(0)

    run_a = asyncio.create_task(registry.reconcile_unconfirmed())
    await _settle()
    assert gated.calls == 1, "run A never reached the adapter"

    run_b = asyncio.create_task(registry.reconcile_unconfirmed())
    await _settle()

    gated.gates[0].set()
    await run_a

    newcomer = _FakeAdapter(open_generations=1)
    newcomer.confirm_on_retire = False
    async with registry.track(ws) as second:
        second.bind_adapter(newcomer)

    gated.gates[1].set()
    await run_b

    assert registry.unconfirmed_count() == 1, (
        "an entry appended while a reconcile was in flight was discarded — "
        "the survivor list was rebound over it"
    )


@pytest.mark.asyncio
async def test_a_wedged_reconcile_does_not_stop_the_next_one_from_making_progress(registry):
    """Runs must NOT be serialized — not by a held lock, and not by a try-lock.

    A reconcile that never returns is reachable in production: scheduler.py's tick
    abandons its future at `fut.result(timeout=30)` while the coroutine keeps
    running on app_loop. A lock taken by that run is never released, so every
    later tick either queues behind it forever or (as `if lock.locked(): return
    0`) declines — and reconciliation is dead for the life of the process. The
    entry here is retried by the SECOND run and confirmed dead while the first is
    still stuck, which is exactly what a lock of either flavour would prevent.

    Safe because the adapter's retire_unconfirmed routes through
    _retire_generation, so the second call JOINS the single teardown task rather
    than starting a second kill (claude_adapter.py:667).

    Mutations (both must fail this test):
      * `async with self._reconcile_lock:` around the body — run B never reaches
        the adapter, so wait_for expires and the explicit fail fires.
      * `if self._reconcile_lock.locked(): return 0` — run B returns at once
        without calling the adapter; calls == 1 and the entry stays unconfirmed.
    """
    ws = uuid.uuid4()

    class _WedgedFirstCall:
        """First retirement attempt hangs forever; a later one succeeds."""

        def __init__(self) -> None:
            self._open = [uuid.uuid4()]
            self.calls = 0
            self.never_opens = asyncio.Event()

        def unconfirmed_launch_ids(self):
            return list(self._open)

        async def retire_unconfirmed(self) -> int:
            self.calls += 1
            if self.calls == 1:
                await self.never_opens.wait()
            self._open.clear()
            return 1

    adapter = _WedgedFirstCall()
    async with registry.track(ws) as entry:
        entry.bind_adapter(adapter)
    assert registry.unconfirmed_count() == 1

    async def _settle() -> None:
        for _ in range(10):
            await asyncio.sleep(0)

    run_a = asyncio.create_task(registry.reconcile_unconfirmed())
    await _settle()
    assert adapter.calls == 1, "run A never reached the adapter"

    run_b = asyncio.create_task(registry.reconcile_unconfirmed())
    try:
        # Bounded, because a serializing lock makes run B wait FOREVER — a
        # mutation must fail, never hang the suite.
        await asyncio.wait_for(run_b, timeout=2.0)
    except TimeoutError:
        pytest.fail("run B never finished — it is serialized behind a wedged run A")
    finally:
        run_a.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_a

    assert adapter.calls == 2, (
        "run B declined to touch the adapter while run A was in flight — a "
        "wedged run must not disable reconciliation"
    )
    assert registry.unconfirmed_count() == 0, (
        "the generation was never confirmed dead: the second attempt did not run"
    )
