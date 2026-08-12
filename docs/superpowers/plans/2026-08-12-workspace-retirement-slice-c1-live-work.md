# Workspace retirement Slice C1 — live-work cancellation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision 2.** Revision 1 (`ed341c7`) was reviewed and rejected with seven blocking findings, all verified against the code and all fixed here. What changed, and why it matters when you read the tasks:

1. **Tracking now wraps the WHOLE runner, prefix included.** R1 registered the entry only after the prefix, leaving a race: an already-claimed pass validates the live workspace, releases its row lock, DELETE commits and sees an empty registry, and only then does the pass register and launch. The claim filter cannot help — the job was already claimed.
2. **The registry no longer tracks handles.** It asks the adapter for its unconfirmed generations. R1 bound a handle after `launch()` returned and treated a handle-less entry as confirmed — but `launch` registers the generation *before* `client.connect()` (`claude_adapter.py:212-216`), so a cancellation during connect leaves an unconfirmed generation with no handle, which R1 would have discarded where nothing could ever reclaim it.
3. **`ReviewRun < ReviewRunItem` is now a normative rule, and finalization is reordered.** R1's new child-prefix lock took run-then-item while existing finalization takes item-then-run (`review/pass_runner.py:136,144`) — a genuine AB-BA deadlock that R1 introduced.
4. **The audit abort raises instead of `break`ing.** A `break` returns partial issues to `analyze_job`, which carries on and calls `mark_done` (`analysis/pipeline.py:535`) — turning a deletion into a job marked *done* on partial data.
5. **`Entry` is `@dataclass(eq=False)`.** R1 put a default (hashable-by-`__eq__`) dataclass into a `set` — `__hash__` is `None`, so `.add()` raises `TypeError` on the first pass ever tracked.
6. **`reconcile_cancelled_jobs` accumulates instead of returning early.** Its `if not ids: return 0` (`jobs_retirement.py:111`) meant R1's claim-analysis block was unreachable for a workspace with no setup jobs — and R1's own test for it would have failed.
7. **The new tick is a sync closure dispatching onto `app_loop`.** R1 registered an `async def` directly with `BackgroundScheduler`, which never awaits it. The unit test would have passed while production reconciliation silently never ran.
8. **Three mutation gates that could not discriminate were replaced** with tests that drive the real production paths. Details in Tasks 5, 7 and 11.

**Goal:** Make a workspace DELETE cancel the analysis work that is actually in flight — the four LLM passes and claim-analysis — instead of letting it run on against a deleted workspace.

**Architecture:** A process-global `PASS_REGISTRY` (mirroring how collab exposes `MANAGER`) holds one entry per live pass task, registered from the moment the runner starts. A new `"passes"` retirement hook cancels every entry for a workspace concurrently, bounded at 5.5s, and asks each entry's adapter to retire any generation whose death is unconfirmed. Each runner's prefix additionally takes a workspace row lock so it cannot commit progress for a workspace being deleted. Claim-analysis, which is sync code in a thread pool and cannot be cancelled, gets the durable half only.

**Tech Stack:** Python 3.12, asyncio, FastAPI, SQLAlchemy 2.x, PostgreSQL 16, APScheduler, pytest + testcontainers.

**Source spec:** `docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md`. Spec §8 (six routes under the commit mutex) and §9 (deleting the `soft_delete_workspace` foot-gun) are **Slice C2**, a separate plan sharing no files with this one.

**Prerequisite, already merged:** the Phase-0 slice (`d459ce1`) — the `"jobs"` hook, `jobs_retirement.py`, the reconciliation tick this plan extends, and the global lock order below.

---

## Global Constraints

- **No schema migration.** Claim-analysis terminalizes to the existing `failed`, allowed by `ck_claim_analysis_jobs_status`.
- **`_RETIREMENT_BARRIER_SECONDS` stays 6.0.** `_PASS_RETIRE_SECONDS = 5.5` — clears the adapter's internal worst case (`disconnect_deadline_seconds` 3.0 + `reap_deadline_seconds` 2.0 = 5.0) and fits under the barrier. `_JOBS_CANCEL_SECONDS` stays 1.0.
- **No change to any pass's own timeout or terminate constants.**
- **No change to `reconcile_live_clients`** (`claude_adapter.py:658`). Task 6 adds a sibling for a population it does not cover.
- **No new in-memory tombstone for passes.** "Refuse to claim a deleted workspace" already exists durably in SQL.
- **No change to any runner's `except Exception` body.** `asyncio.CancelledError` inherits from `BaseException` and already passes through to the `finally`.
- **Lock order — workspace chain (from Phase-0, in force):** `commit mutex < workspace_setup_jobs < workspaces < calls`.
- **Lock order — review chain (NEW, this plan):** `review_runs < review_run_items`. Any transaction touching both must take the run first. Task 5 establishes it and reorders the one existing violator.
- **Acceptance is by mutation.** Every gate must be shown to FAIL when its fix is reverted — **against the real production path**, not a hand-rolled stand-in. This project has shipped five tests that could not fail. Revision 1 of this plan added three more. Do not add a ninth.
- **Test helpers are imports, not fixtures.** `make_workspace`, `make_call` and friends are plain functions: `from tests.proposal_assist.conftest import make_workspace`. There is no settings fixture — construct `Settings()` inline, as every existing pass test does (`tests/proposal_assist/coherence/test_pass_runner.py:34`).
- **`ClaimAnalysisJob` requires `section_index` and `commit_sha`** (both `nullable=False`, no server default). Every construction in this plan passes them; do not drop them.
- **Gates before every commit:** `.venv/bin/python -m pytest tests/proposal_assist/ -q` ending `PYTEST_EXIT=0`, and `.venv/bin/ruff check .` clean. Run pytest as a **directory**, never file-by-file.
- **Commit with `git commit -F <file>`.** Heredoc `EOF` has broken commits here. Trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File Structure

**Created**

| file | responsibility |
|---|---|
| `src/call_manager/proposal_assist/passes.py` | The pass registry: `Entry`, `PASS_REGISTRY`, `track()`, `retire_workspace()`, the unconfirmed list and its reconciliation. |
| `tests/proposal_assist/test_passes_registry.py` | Registry unit gates (no DB). |
| `tests/proposal_assist/test_pass_retirement.py` | Integration gates driving the real runners. |
| `tests/proposal_assist/test_claim_analysis_retirement.py` | Claim-analysis gates including route latency. |

**Modified**

| file | change |
|---|---|
| `drafting/claude_adapter.py` | `unconfirmed_launch_ids()` + `retire_unconfirmed()`. |
| `drafting/fake_adapter.py` | The same two methods. |
| `coherence/`, `evaluator/`, `uncited_claims/pass_runner.py` | `track()` around the whole runner; prefix row-lock guard. |
| `review/pass_runner.py` | Parent + per-child tracking; child prefix locks the run; **finalization reordered to run-then-item**. |
| `retirement.py` | `REQUIRED_HOOKS` gains `"passes"`; budget comment corrected. |
| `main.py` | Register the `"passes"` hook. |
| `scheduler.py` | `_drain_pass_future` helper + explicit `CancelledError` handling; new `_passes_reconcile_tick` sync closure. |
| `config.py` | `passes_reconcile_tick_seconds`. |
| `analysis/queue.py` | `live_ws` filter on `claim_queued_jobs`; `mark_failed` cannot resurrect a cancelled job. |
| `analysis/pipeline.py` | `should_abort` threaded into `analyze_job`; `ClaimAnalysisAborted` handled at the drain loop. |
| `citations/audit.py` | `ClaimAnalysisAborted` raised inside the per-evidence-row loop. |
| `jobs_retirement.py` | Claim-analysis cancel in the hook; reconcile accumulates both populations. |

---

### Task 1: The pass registry

**Files:**
- Create: `src/call_manager/proposal_assist/passes.py`, `tests/proposal_assist/test_passes_registry.py`
- Modify: `drafting/claude_adapter.py` (after `retiring_count`, line 656), `drafting/fake_adapter.py` (near line 113)

**Interfaces produced:**
- `PASS_REGISTRY.track(workspace_id) -> async context manager yielding Entry`
- `Entry.bind_adapter(adapter) -> None`
- `await PASS_REGISTRY.retire_workspace(workspace_id) -> int`
- `await PASS_REGISTRY.reconcile_unconfirmed() -> int`
- `PASS_REGISTRY.live_count(workspace_id) -> int`, `.unconfirmed_count() -> int`
- `_PASS_RETIRE_SECONDS = 5.5`
- `ClaudeAgentSDKAdapter.unconfirmed_launch_ids() -> list[uuid.UUID]`
- `ClaudeAgentSDKAdapter.retire_unconfirmed() -> int` (async)

**Why no handles.** `launch` registers the generation *before* `await client.connect()` and, on any `BaseException`, retires it and re-raises without returning a handle (`claude_adapter.py:212-216`). A handle-keyed registry is therefore blind to exactly the case that matters most — cancellation during connect. Asking the adapter what it still has open covers both cases with less machinery. These are one-shot per-pass adapters, so "what this adapter still has open" is precisely this pass's generations.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_passes_registry.py`:

```python
"""Registry gates in isolation — no DB, no SDK."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from call_manager.proposal_assist.passes import PASS_REGISTRY


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
async def test_the_entry_exists_from_the_very_start_not_after_the_prefix():
    """The race revision 1 had: an entry that only appears after the prefix is
    invisible to a DELETE that commits during it."""
    ws = uuid.uuid4()
    seen_before_adapter = None

    async with PASS_REGISTRY.track(ws) as entry:
        seen_before_adapter = PASS_REGISTRY.live_count(ws)   # no adapter bound yet
        entry.bind_adapter(_FakeAdapter())

    assert seen_before_adapter == 1
    assert PASS_REGISTRY.live_count(ws) == 0


@pytest.mark.asyncio
async def test_track_deregisters_when_the_body_raises():
    ws = uuid.uuid4()
    with pytest.raises(ValueError):
        async with PASS_REGISTRY.track(ws) as entry:
            entry.bind_adapter(_FakeAdapter())
            raise ValueError("boom")
    assert PASS_REGISTRY.live_count(ws) == 0


@pytest.mark.asyncio
async def test_retire_cancels_the_task_and_retires_the_adapter():
    ws = uuid.uuid4()
    adapter = _FakeAdapter(open_generations=1)
    started = asyncio.Event()

    async def _pass():
        async with PASS_REGISTRY.track(ws) as entry:
            entry.bind_adapter(adapter)
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(_pass())
    await started.wait()
    retired = await PASS_REGISTRY.retire_workspace(ws)

    assert retired == 1
    assert task.done()
    assert adapter.retire_calls == 1
    assert PASS_REGISTRY.live_count(ws) == 0


@pytest.mark.asyncio
async def test_entries_retire_concurrently_not_serially():
    """Gate 7. Mutation: replace the gather in retire_workspace with a for loop."""
    ws = uuid.uuid4()
    started = asyncio.Event()
    n = 6

    async def _pass(idx: int):
        async with PASS_REGISTRY.track(ws) as entry:
            entry.bind_adapter(_FakeAdapter(open_generations=1, retire_delay=0.3))
            if idx == n - 1:
                started.set()
            await asyncio.sleep(3600)

    tasks = [asyncio.create_task(_pass(i)) for i in range(n)]
    await started.wait()
    await asyncio.sleep(0.05)

    loop = asyncio.get_running_loop()
    began = loop.time()
    await PASS_REGISTRY.retire_workspace(ws)
    elapsed = loop.time() - began

    assert elapsed < 1.0, f"retirement took {elapsed:.2f}s — serialized, not concurrent"
    for t in tasks:
        assert t.done()


@pytest.mark.asyncio
async def test_a_generation_left_open_transfers_on_ORDINARY_exit():
    """Gate 3(b), the case that matters most: no deletion involved at all.

    Also the launch-cancel case, because unconfirmed_launch_ids() reports a
    generation registered before connect() even though no handle ever existed.
    """
    ws = uuid.uuid4()
    adapter = _FakeAdapter(open_generations=1)
    adapter.confirm_on_retire = False
    before = PASS_REGISTRY.unconfirmed_count()

    async with PASS_REGISTRY.track(ws) as entry:
        entry.bind_adapter(adapter)

    assert PASS_REGISTRY.live_count(ws) == 0
    assert PASS_REGISTRY.unconfirmed_count() == before + 1


@pytest.mark.asyncio
async def test_reconcile_retries_and_drops_once_confirmed():
    ws = uuid.uuid4()
    adapter = _FakeAdapter(open_generations=1)
    adapter.confirm_on_retire = False
    async with PASS_REGISTRY.track(ws) as entry:
        entry.bind_adapter(adapter)
    assert PASS_REGISTRY.unconfirmed_count() >= 1

    adapter.confirm_on_retire = True
    acted = await PASS_REGISTRY.reconcile_unconfirmed()

    assert acted >= 1
    assert PASS_REGISTRY.unconfirmed_count() == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_passes_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.passes'`

- [ ] **Step 3: Write the registry**

Create `src/call_manager/proposal_assist/passes.py`:

```python
"""Process-global registry of in-flight analysis passes, so a workspace DELETE can
cancel them.

Mirrors how collab exposes MANAGER: one module-level singleton, wired into the
retirement coordinator at startup (main.py). Tracking happens INSIDE each runner
rather than at the scheduler tick, so every caller is covered — not only the
scheduled path.

There is deliberately no in-memory tombstone here: "refuse to CLAIM a deleted
workspace" already exists durably in SQL. This registry answers the other
question — what is ALREADY running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Per entry: cancel the task, then await BOTH its exit AND the adapter's retirement
# of any generation it still has open — concurrently, under this one bound.
#
# Why one bound and not two: adapter teardown routes through _retire_generation,
# which does `await asyncio.shield(record.teardown)` (claude_adapter.py:571). Under
# cancellation the shield delivers CancelledError to the awaiter while teardown
# keeps running, so awaiting the task's exit proves nothing about teardown. The
# adapter's own internal worst case is disconnect_deadline_seconds (3.0) +
# reap_deadline_seconds (2.0) = 5.0. A 5.0 outer bound around a 5.0 inner one is a
# coin flip; 5.5 clears it and stays under _RETIREMENT_BARRIER_SECONDS (6.0).
_PASS_RETIRE_SECONDS = 5.5


@dataclass(eq=False)   # identity semantics — entries live in a set
class Entry:
    """One live pass task.

    The adapter is bound LATER, not at construction, because the entry must exist
    before the runner's prefix runs. Revision 1 registered after the prefix and
    left a race: an already-claimed pass validates the live workspace, releases its
    row lock, DELETE commits and snapshots an empty registry, and only then does
    the pass register and launch.

    There is no handle here on purpose. `launch` registers its generation BEFORE
    `client.connect()` and, on cancellation, retires it and re-raises without ever
    returning a handle (claude_adapter.py:212-216) — so a handle-keyed registry is
    blind to precisely that case. Confirmation is asked of the adapter instead.
    """

    workspace_id: uuid.UUID
    task: asyncio.Task
    adapter: object | None = field(default=None, repr=False)

    def bind_adapter(self, adapter: object) -> None:
        self.adapter = adapter

    def open_generations(self) -> int:
        """How many generations this entry's adapter still has un-confirmed-dead.
        Zero when there is no adapter yet — nothing has been launched."""
        if self.adapter is None:
            return 0
        ask = getattr(self.adapter, "unconfirmed_launch_ids", None)
        return len(ask()) if ask is not None else 0

    async def retire_adapter(self) -> None:
        if self.adapter is None:
            return
        retire = getattr(self.adapter, "retire_unconfirmed", None)
        if retire is not None:
            await retire()


class PassRegistry:
    """What is running, and how to stop it."""

    def __init__(self) -> None:
        self._live: dict[uuid.UUID, set[Entry]] = {}
        # Entries whose adapter still had an open generation at exit. NOT
        # leak-and-forget: reconcile_unconfirmed retries them.
        self._unconfirmed: list[Entry] = []

    @contextlib.asynccontextmanager
    async def track(self, workspace_id: uuid.UUID) -> AsyncIterator[Entry]:
        entry = Entry(workspace_id=workspace_id, task=asyncio.current_task())
        self._live.setdefault(workspace_id, set()).add(entry)
        try:
            yield entry
        finally:
            # Removal is NOT discard. An entry whose adapter still has an open
            # generation is TRANSFERRED — on EVERY exit path, not only
            # retirement-driven ones. An ordinary completion (or a pass timeout)
            # whose teardown could not confirm death would otherwise leave the
            # subprocess ownerless, and a delete a moment later would find nothing.
            self._forget(entry)
            if entry.open_generations():
                self._unconfirmed.append(entry)
                logger.warning(
                    "passes.generation_unconfirmed",
                    extra={
                        "event": "pass_generation_unconfirmed",
                        "workspace_id": str(workspace_id),
                    },
                )

    def _forget(self, entry: Entry) -> None:
        bucket = self._live.get(entry.workspace_id)
        if bucket is None:
            return
        bucket.discard(entry)
        if not bucket:
            self._live.pop(entry.workspace_id, None)

    def live_count(self, workspace_id: uuid.UUID) -> int:
        return len(self._live.get(workspace_id, ()))

    def unconfirmed_count(self) -> int:
        return len(self._unconfirmed)

    async def retire_workspace(self, workspace_id: uuid.UUID) -> int:
        """Cancel every live pass for this workspace, CONCURRENTLY.

        Concurrency keeps the hook's cost independent of entry count: five review
        personas plus a coherence pass is six entries, and serial retirement would
        blow the budget six times over.
        """
        entries = list(self._live.get(workspace_id, ()))
        if not entries:
            return 0
        await asyncio.gather(*(self._retire_entry(e) for e in entries))
        return len(entries)

    async def _retire_entry(self, entry: Entry) -> None:
        entry.task.cancel()
        waits = [asyncio.shield(_swallow(entry.task)), entry.retire_adapter()]
        try:
            await asyncio.wait_for(
                asyncio.gather(*waits, return_exceptions=True),
                timeout=_PASS_RETIRE_SECONDS,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "passes.retire_timeout",
                extra={
                    "event": "pass_retire_timeout",
                    "workspace_id": str(entry.workspace_id),
                },
            )

    async def reconcile_unconfirmed(self) -> int:
        """Retry adapter retirement for generations never confirmed dead, dropping
        each entry that comes back clean.

        A remediation ATTEMPT each tick, not a guarantee — the same wording
        reconcile_live_clients uses. This is a SIBLING of that sweep, not an edit:
        it covers the interactive registry's adapter, and these one-shot pass
        adapters are built per-pass by build_keeper_adapter and friends, so nothing
        else owns them.
        """
        if not self._unconfirmed:
            return 0
        still: list[Entry] = []
        acted = 0
        for entry in self._unconfirmed:
            acted += 1
            try:
                await entry.retire_adapter()
            except Exception:
                logger.exception(
                    "passes.reconcile_retire_failed",
                    extra={"workspace_id": str(entry.workspace_id)},
                )
            if entry.open_generations():
                still.append(entry)
        self._unconfirmed = still
        return acted


async def _swallow(task: asyncio.Task) -> None:
    """Await a task, absorbing its outcome. A cancelled pass raises CancelledError
    and a failed one raises its own error; retirement cares about neither, only
    that the task has finished."""
    with contextlib.suppress(BaseException):
        await task


PASS_REGISTRY = PassRegistry()
```

- [ ] **Step 4: Run — all six must pass**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_passes_registry.py -q`
Expected: `6 passed`

- [ ] **Step 5: Add the two adapter methods**

In `claude_adapter.py`, after `retiring_count` (line 656):

```python
    def unconfirmed_launch_ids(self) -> list[uuid.UUID]:
        """Every generation still in the inventory — i.e. death NOT confirmed.

        confirm_dead POPS the record (live_clients.py:64), so presence is the
        question and absence is the answer. Covers the generation registered
        before connect() for which no handle was ever returned.
        """
        return [r.launch_id for r in self._live.records()]

    async def retire_unconfirmed(self) -> int:
        """Retry teardown for every generation still in the inventory.

        Routes through _retire_generation, which is documented as shared by
        retirement, capacity eviction, idle sweep and reconciliation — so a
        concurrent caller JOINS the single teardown task rather than spawning a
        second kill.
        """
        ids = self.unconfirmed_launch_ids()
        for launch_id in ids:
            await self._retire_generation(launch_id)
        return len(ids)
```

In `fake_adapter.py`, near its `retiring_count` (line 113):

```python
    def unconfirmed_launch_ids(self) -> list:
        """The fake confirms everything by default; tests wanting the unconfirmed
        path set this explicitly, so the default never hides it."""
        return []

    async def retire_unconfirmed(self) -> int:
        return 0
```

- [ ] **Step 6: Prove the concurrency gate discriminates**

Replace the `asyncio.gather` in `retire_workspace` with `for e in entries: await self._retire_entry(e)`.
Run: `.venv/bin/python -m pytest tests/proposal_assist/test_passes_registry.py -q -k concurrently`
Expected: **FAIL** (~1.8s). Restore, confirm PASS, confirm `git diff --stat src/` is clean.

- [ ] **Step 7: Full gates and commit**

```bash
.venv/bin/python -m pytest tests/proposal_assist/ -q
.venv/bin/ruff check .
git add src/call_manager/proposal_assist/passes.py \
        src/call_manager/proposal_assist/drafting/claude_adapter.py \
        src/call_manager/proposal_assist/drafting/fake_adapter.py \
        tests/proposal_assist/test_passes_registry.py
git commit -F <msgfile>   # "feat(passes): registry of in-flight passes, retired concurrently"
```

---

### Task 2: Wire the hook and pin the budget

**Files:** `main.py:186-191`, `retirement.py:28` + comment block, `tests/proposal_assist/test_retirement_coordinator.py`

**Interfaces:** consumes `PASS_REGISTRY.retire_workspace`; produces `REQUIRED_HOOKS == ("turns", "collab", "jobs", "passes")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_passes_is_a_required_hook():
    """Dropping the wiring must fail at BOOT. This structural guard is the one
    that previously caught an unwired turns hook."""
    from call_manager.proposal_assist.retirement import (
        MissingRetirementHook,
        WorkspaceRetirement,
    )

    async def _noop(workspace_id):
        return 0

    with pytest.raises(MissingRetirementHook, match="passes"):
        WorkspaceRetirement(hooks={"turns": _noop, "collab": _noop, "jobs": _noop})


def test_the_barrier_exceeds_every_hooks_own_worst_case_including_passes():
    """Gate 12, read from the LIVE constants."""
    import inspect

    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.jobs_retirement import _JOBS_CANCEL_SECONDS
    from call_manager.proposal_assist.passes import _PASS_RETIRE_SECONDS
    from call_manager.proposal_assist.retirement import _RETIREMENT_BARRIER_SECONDS

    sig = inspect.signature(ClaudeAgentSDKAdapter.__init__)
    disconnect = sig.parameters["disconnect_deadline_seconds"].default
    reap = sig.parameters["reap_deadline_seconds"].default

    assert _PASS_RETIRE_SECONDS > disconnect + reap
    worst = max(3.0, disconnect + reap, _PASS_RETIRE_SECONDS, _JOBS_CANCEL_SECONDS)
    assert worst < _RETIREMENT_BARRIER_SECONDS
```

- [ ] **Step 2: Run to verify both fail.**

- [ ] **Step 3: Add the hook to `REQUIRED_HOOKS`**

`retirement.py` line 28: `REQUIRED_HOOKS = ("turns", "collab", "jobs", "passes")`

Add to the budget comment block, after the `jobs:` bullet:

```python
#   passes: _PASS_RETIRE_SECONDS (5.5) — INDEPENDENT of entry count, because
#           PassRegistry.retire_workspace gathers its entries concurrently. If it
#           ever retired serially this budget would silently become N x 5.5.
```

and change the closing line to `# => 5.5 worst case. 6.0 keeps headroom.`

- [ ] **Step 4: Register the hook** — in `main.py`, alongside the existing `"jobs"` entry:

```python
    from call_manager.proposal_assist.passes import PASS_REGISTRY
```
```python
            "passes": PASS_REGISTRY.retire_workspace,
```

- [ ] **Step 5: Run; then prove the budget gate discriminates** by setting `_PASS_RETIRE_SECONDS = 6.5` (expect FAIL) and restoring.

- [ ] **Step 6: Full gates and commit** — `"feat(retirement): require and wire the passes hook"`

---

### Task 3: Coherence — track the whole runner, guard the prefix

**Files:** `coherence/pass_runner.py:44-116`, `tests/proposal_assist/test_pass_retirement.py` (create)

**Interfaces:** consumes `PASS_REGISTRY.track`, `Entry.bind_adapter`. Produces the shape Tasks 4-5 repeat — read this task before those.

**Two changes.**

**(a) Prefix guard (spec §5).** Load the workspace `with_for_update=True` and abort if `soft_deleted_at` is set, holding the lock through the commit. The delete transaction takes the same row to stamp `soft_deleted_at`, so only two orders exist: the prefix commits and the delete waits briefly, or the delete wins and the prefix aborts having written nothing. Safe **only because this prefix is short and commits promptly** — Step 4 pins the lock duration rather than assuming it.

**(b) Track the WHOLE runner**, starting before the prefix — not from the adapter build. See the revision note: registering after the prefix leaves a window in which a DELETE snapshots an empty registry and the pass launches anyway.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_pass_retirement.py`:

```python
"""Integration gates driving the REAL runners."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, Workspace
from call_manager.proposal_assist.coherence.pass_runner import run_coherence_pass
from call_manager.proposal_assist.passes import PASS_REGISTRY
from tests.proposal_assist.conftest import make_workspace


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _never_called_adapter_builder(**kwargs):
    """This IS the assertion for the prefix-guard tests: reached only if the guard
    failed to abort."""
    raise AssertionError("the prefix guard should have aborted before the adapter build")


def _stub_adapter_builder(**kwargs):
    """Proceeds past the prefix without doing any LLM work."""

    class _Stub:
        async def launch(self, context):
            handle = type("H", (), {})()
            handle.state = {"launch_id": uuid.uuid4()}
            handle.workspace_id = kwargs.get("workspace_id")
            return handle

        async def send_turn(self, handle, instruction):
            return
            yield  # pragma: no cover — makes this an async generator

        async def terminate(self, handle) -> None:
            return None

        def unconfirmed_launch_ids(self):
            return []

        async def retire_unconfirmed(self) -> int:
            return 0

    return _Stub()


def test_the_coherence_prefix_aborts_for_a_workspace_deleted_under_it(
    pg_session_factory, current_user
):
    """Gate 9: zero prefix writes, and the adapter is never built."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = CoherenceJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = _now_utc()
        s.commit()

    asyncio.run(
        run_coherence_pass(
            job_id=job_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        assert s.get(CoherenceJob, job_id).status == "running"


def test_the_coherence_prefix_does_not_hold_its_row_lock_for_long(
    pg_session_factory, current_user
):
    """Gate 9, second half. The guard is safe ONLY while this prefix stays short."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = CoherenceJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    blocked_for: list[float] = []

    def _competing_update():
        time.sleep(0.2)
        began = time.monotonic()
        with pg_session_factory() as s:
            s.get(Workspace, ws_id, with_for_update=True)
            s.commit()
        blocked_for.append(time.monotonic() - began)

    t = threading.Thread(target=_competing_update)
    t.start()
    asyncio.run(
        run_coherence_pass(
            job_id=job_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_stub_adapter_builder,
        )
    )
    t.join(timeout=30)

    assert blocked_for, "the competing update never ran"
    assert blocked_for[0] < 5.0, (
        f"the prefix held the workspace row for {blocked_for[0]:.1f}s"
    )
```

- [ ] **Step 2: Run to verify the first fails**

Expected: `AssertionError: the prefix guard should have aborted before the adapter build`

- [ ] **Step 3: Add the guard**

Replace the prefix session opening in `coherence/pass_runner.py`:

```python
    # 1. Read job + workspace state in a short session, under a workspace ROW LOCK.
    #    The lock linearizes this prefix against a concurrent delete: the delete
    #    takes the same row to stamp soft_deleted_at, so either we commit and it
    #    waits briefly, or it wins and we abort having written nothing. Safe ONLY
    #    because this session commits promptly.
    with session_factory() as s:
        job = s.get(CoherenceJob, job_id)
        if job is None or job.status != "running":
            return
        ws = s.get(Workspace, job.workspace_id, with_for_update=True)
        if ws is None or ws.soft_deleted_at is not None:
            return
        workspace_id = job.workspace_id
        requested_by = job.requested_by
        locked = ws.spec_locked_at is not None
        ...  # the rest of the existing prefix, unchanged
```

- [ ] **Step 4: Wrap the whole runner**

The tracked region starts **before** the prefix. `run_coherence_pass`'s body becomes:

```python
async def run_coherence_pass(...) -> None:
    """..."""
    # Tracked from the FIRST line, before the prefix — an entry that appears only
    # after the prefix is invisible to a DELETE that commits during it.
    # workspace_id is not known until the prefix reads it, so the registry is keyed
    # on the job's workspace, read in its own tiny session first.
    with session_factory() as s0:
        job0 = s0.get(CoherenceJob, job_id)
        if job0 is None or job0.status != "running":
            return
        tracked_ws = job0.workspace_id

    async with PASS_REGISTRY.track(tracked_ws) as _entry:
        ...  # the entire existing body, indented one level
        # and, immediately after `adapter = adapter_builder(...)`:
        _entry.bind_adapter(adapter)
```

Add the import: `from call_manager.proposal_assist.passes import PASS_REGISTRY`

**Do not** touch the `except Exception` body or the `finally` terminate block.

- [ ] **Step 5: Run both; both must pass.**

- [ ] **Step 6: Prove the guard gate discriminates** — revert `with_for_update=True` to a bare `get` and drop the `soft_deleted_at` check; expect FAIL; restore; expect PASS.

- [ ] **Step 7: Full gates and commit** — `"feat(coherence): track the whole pass and guard its prefix"`

---

### Task 4: Evaluator and uncited-claims

**Files:** `evaluator/pass_runner.py`, `uncited_claims/pass_runner.py`, `tests/proposal_assist/test_pass_retirement.py`

**Why these matter more than coherence:** both commit **durable progress** in their prefix. Evaluator commits `evaluated_draft_sha`, `evaluated_spec_sha`, `evaluated_rubric_updated_at`; uncited-claims commits `evaluated_draft_sha`. Without the guard those are durable writes against a deleted workspace.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_evaluator_prefix_writes_nothing_for_a_deleted_workspace(
    pg_session_factory, current_user
):
    from call_manager.db.models import EvaluatorJob
    from call_manager.proposal_assist.evaluator.pass_runner import run_evaluator_pass

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = EvaluatorJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = _now_utc()
        s.commit()

    asyncio.run(
        run_evaluator_pass(
            job_id=job_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        job = s.get(EvaluatorJob, job_id)
        assert job.evaluated_draft_sha is None
        assert job.evaluated_spec_sha is None
        assert job.evaluated_rubric_updated_at is None


def test_the_uncited_prefix_writes_nothing_for_a_deleted_workspace(
    pg_session_factory, current_user
):
    from call_manager.db.models import UncitedClaimsJob
    from call_manager.proposal_assist.uncited_claims.pass_runner import (
        run_uncited_claims_pass,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = UncitedClaimsJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = _now_utc()
        s.commit()

    asyncio.run(
        run_uncited_claims_pass(
            job_id=job_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).evaluated_draft_sha is None
```

- [ ] **Step 2: Run to verify both fail.**

- [ ] **Step 3: Add the guard to each prefix**, immediately after the `job.status != "running"` early return:

```python
        ws = s.get(Workspace, job.workspace_id, with_for_update=True)
        if ws is None or ws.soft_deleted_at is not None:
            return
```

In the evaluator this replaces the existing unlocked `ws = s.get(Workspace, job.workspace_id)`. Leave the later `ws.rubric_updated_at if ws else None` expression alone — narrowing it is not this task's business.

- [ ] **Step 4: Track the whole runner in each**, using the same pre-prefix pattern as Task 3 (tiny session to read `workspace_id`, then `async with PASS_REGISTRY.track(tracked_ws) as _entry:` around the entire body, and `_entry.bind_adapter(adapter)` after the adapter build). Import `PASS_REGISTRY` in each file.

- [ ] **Step 5: Run; prove BOTH guards discriminate** (remove each guard in turn, expect that runner's test to FAIL, restore). Record all four runs.

- [ ] **Step 6: Full gates and commit** — `"feat(passes): track and guard the evaluator and uncited runners"`

---

### Task 5: Review — fan-out, the second prefix, and the lock-order fix

**Files:** `review/pass_runner.py` (child prefix ~60-93, child body 95-133, **finalization 135-144**, parent 193-260), `tests/proposal_assist/test_pass_retirement.py`

**Three changes, and the third is a correctness fix to existing code.**

**(a) Track parent and children separately.** `run_review_pass` **launches nothing** — every `_run_one_call` builds its own adapter, so each child has its own generation. The parent is tracked with no adapter ever bound; each child is tracked and binds its own.

**(b) The child prefix locks the parent run.** Each `_run_one_call` commits `item.status = "running"` before its adapter exists. It re-reads `run.status` first, but that read takes **no lock** — a TOCTOU window in which a DELETE can cancel the run between the read and the commit, stranding a `ReviewRunItem` in `running` on a deleted workspace. Lock the `ReviewRun` row: the delete transaction already updates review runs through `cancel_review_runs_for_workspace`, so it linearizes against exactly the write that matters, without adding N persona-shaped contenders to the workspace row.

**(c) Finalization must be reordered — this plan introduces a deadlock otherwise.** The new prefix takes **run → item**. Existing finalization takes **item** (line 136) **→ run** (line 144). That is AB-BA: two review sub-calls on the same run can deadlock (Postgres 40P01). This plan therefore establishes `review_runs < review_run_items` as a normative rule and reorders finalization to match. The rule goes in a comment at both sites so the next reader cannot re-invert it.

- [ ] **Step 1: Write the failing tests — all three drive the REAL runner**

```python
@pytest.mark.asyncio
async def test_every_review_child_is_tracked_as_its_own_entry(
    pg_session_factory, current_user, monkeypatch
):
    """Gate 6, against the REAL run_review_pass.

    Revision 1 tested hand-rolled coroutines, so deleting the tracking from
    production review could not have failed it. The peak live count is sampled
    from inside a real _run_one_call.
    """
    from call_manager.db.models import ReviewRun
    from call_manager.proposal_assist.review import pass_runner as rp

    peak: list[int] = []

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        run = ReviewRun(workspace_id=ws.id, status="running")
        s.add(run)
        s.commit()
        run_id, ws_id = run.id, ws.id

    real_builder = rp.build_review_adapter          # confirm the real name first

    def _sampling_builder(**kwargs):
        peak.append(PASS_REGISTRY.live_count(ws_id))
        return _stub_adapter_builder(**kwargs)

    await rp.run_review_pass(
        run_id=run_id,
        session_factory=pg_session_factory,
        settings=Settings(),
        adapter_builder=_sampling_builder,
    )

    # parent (task-only) + at least one child alive at the same moment
    assert peak and max(peak) >= 2, (
        f"peak live entries was {max(peak) if peak else 0} — children are sharing "
        "the parent entry instead of registering their own"
    )
    assert PASS_REGISTRY.live_count(ws_id) == 0


def test_a_delete_racing_the_child_prefix_strands_no_item_in_running(
    pg_session_factory, current_user
):
    """Gate 6, second case — a SYNCHRONIZED race, not a pre-set state.

    Revision 1 cancelled the run BEFORE the child started, which the existing
    unlocked status check already handles — so it could not fail. Here the child
    is made to block on the ReviewRun row lock while the cancel commits, which is
    the actual TOCTOU window.
    """
    from call_manager.db.models import ReviewRun, ReviewRunItem
    from call_manager.proposal_assist.review.pass_runner import _run_one_call

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        run = ReviewRun(workspace_id=ws.id, status="running")
        s.add(run)
        s.flush()
        s.add(ReviewRunItem(review_run_id=run.id, kind="fit_check", status="queued"))
        s.commit()
        run_id, ws_id = run.id, ws.id

    holder = pg_session_factory()
    child_done = threading.Event()

    def _child():
        asyncio.run(
            _run_one_call(
                kind="fit_check",
                instruction="x",
                run_id=run_id,
                workspace_id=ws_id,
                session_factory=pg_session_factory,
                settings=Settings(),
                adapter_builder=_stub_adapter_builder,
            )
        )
        child_done.set()

    try:
        # Hold the run row so the child's prefix BLOCKS on it (with the fix) or
        # sails past it (without).
        holder.get(ReviewRun, run_id, with_for_update=True)
        t = threading.Thread(target=_child)
        t.start()
        time.sleep(0.5)

        # Cancel the run the way the delete transaction does, then release.
        holder.get(ReviewRun, run_id).status = "cancelled"
        holder.commit()
    finally:
        holder.close()

    assert child_done.wait(timeout=30), "the child never finished"

    with pg_session_factory() as s:
        item = s.scalar(
            select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
        )
        assert item.status != "running", (
            "the child committed 'running' on a run the delete had cancelled"
        )


def test_review_takes_the_run_row_before_the_item_row_everywhere(
    pg_session_factory, current_user
):
    """The new normative rule: review_runs < review_run_items.

    Two concurrent sub-calls on one run finalizing while another takes the prefix
    must not deadlock. Mutation: restore finalization's original item-then-run
    order and this must fail with a 40P01 deadlock (or hang to the timeout).
    """
    ...  # body in Step 5 — it needs the reordered finalization to exist first
```

- [ ] **Step 2: Run to verify the first two fail.** Read each failure and confirm it is the assertion, not an import error. Fix the `build_review_adapter` name if my guess is wrong — the file is the authority.

- [ ] **Step 3: Lock the parent run in the child prefix**

```python
    with session_factory() as s:
        # LOCK ORDER: review_runs < review_run_items (normative — see the matching
        # note in the finalization session below). The bare read this replaces was
        # a TOCTOU window: a DELETE cancels review runs through
        # cancel_review_runs_for_workspace and could land between the status check
        # and the `item.status = "running"` commit, stranding the item in 'running'
        # on a deleted workspace.
        run = s.get(ReviewRun, run_id, with_for_update=True)
        item = s.scalar(
            select(ReviewRunItem).where(
                ReviewRunItem.review_run_id == run_id, ReviewRunItem.kind == kind
            )
        )
```

The rest of the prefix is unchanged; its existing `if run is None or run.status != "running":` branch now decides under the lock.

- [ ] **Step 4: Reorder finalization to run-then-item**

Replace the opening of the finalization session (currently lines 135-144):

```python
    with session_factory() as s:
        # LOCK ORDER: review_runs < review_run_items (normative). The run is taken
        # FIRST here even though the item is what we came to write, because the
        # child prefix above takes run-then-item; the original item-then-run order
        # was an AB-BA inversion against it and could deadlock two sub-calls of the
        # same run (Postgres 40P01).
        run = s.get(ReviewRun, run_id, with_for_update=True)
        item = s.get(ReviewRunItem, item_id, with_for_update=True)
        if item is None:
            return
        item.completed_at = _now()
        ...  # the rest of the existing finalization body, unchanged
```

Note the `if item is None: return` moves below both acquisitions — taking the run lock for a moment on a vanished item is harmless, and reordering the guard would reintroduce the inversion.

- [ ] **Step 5: Write the deadlock test body**

Replace the `...` in `test_review_takes_the_run_row_before_the_item_row_everywhere`: seed one run with two items; run two threads, each calling `_run_one_call` for a different `kind`, both with `_stub_adapter_builder`; join both with `timeout=30`; assert both threads finished and no exception was recorded. Capture each thread's exception into a list and assert the list is empty, so a 40P01 fails the test rather than escaping as a warning.

- [ ] **Step 6: Track the parent (task-only) and each child**

Parent — wrap from the wave-1 semaphore through the end of `run_review_pass`:

```python
    async with PASS_REGISTRY.track(workspace_id):
        # Task-only: run_review_pass owns NO adapter — every _run_one_call builds
        # its own. Retirement cancels this task; there is nothing here to retire.
        sem = asyncio.Semaphore(settings.review_persona_concurrency)
        ...  # existing body, indented one level
```

Child — wrap `_run_one_call`'s body from the prefix onward (same pre-prefix rule as Task 3: the entry must exist before the prefix), binding the adapter after it is built:

```python
    async with PASS_REGISTRY.track(workspace_id) as _entry:
        ...  # prefix, then:
        adapter = adapter_builder(...)
        _entry.bind_adapter(adapter)
        ...  # rest, INCLUDING the finalization session
```

- [ ] **Step 7: Run; then prove all three gates discriminate**

1. Delete the child's `async with PASS_REGISTRY.track(...)` → fan-out test **FAILS** (peak < 2).
2. Revert the child prefix to a bare `s.get(ReviewRun, run_id)` → race test **FAILS**.
3. Restore finalization to item-then-run → deadlock test **FAILS** (40P01 or timeout).

Restore each, confirm PASS, and confirm `git diff` shows only the intended changes.

- [ ] **Step 8: Full gates and commit** — `"fix(review): track parent and children, lock the child prefix, fix the run/item order"`

---

### Task 6: The unconfirmed-generation reconciliation tick

**Files:** `config.py`, `scheduler.py`, `tests/proposal_assist/test_passes_reconcile_tick.py`

**The trap revision 1 fell into:** this scheduler is a `BackgroundScheduler` running **sync** callables. Registering an `async def` gets you an un-awaited coroutine — the unit test passes while production never reconciles. Every async tick here is a **sync closure that dispatches onto `app_loop`**; `_coherence_tick` (`scheduler.py:728`) is the pattern to copy, and `_jobs_reconcile_tick` (`scheduler.py:580`) is sync because its work is sync.

**Placement rule (from Phase-0):** register at module scope, **outside** any `drafting_enabled` / `ANTHROPIC_API_KEY` guard. Hiding reconciliation behind those would strand every unconfirmed generation on a deployment with drafting off.

- [ ] **Step 1: Add the setting** — in `config.py`, next to `jobs_reconcile_tick_seconds`:

```python
    passes_reconcile_tick_seconds: int = 60
```

- [ ] **Step 2: Write the failing test — it must assert the tick is DISPATCHED, not just awaitable**

```python
def test_the_passes_reconcile_tick_is_dispatched_onto_the_app_loop(...):
    """The tick registered with APScheduler must be a SYNC callable that schedules
    its coroutine on app_loop. Registering an `async def` directly would return an
    un-awaited coroutine: green unit test, dead production path.

    Assert by calling the registered closure and observing that reconciliation
    actually ran — not by awaiting a coroutine ourselves.
    """
```

Build a real scheduler via the module's existing construction helper with a stub `app_loop`, locate the registered job by id `"passes_reconcile"`, assert `not asyncio.iscoroutinefunction(job.func)`, then invoke `job.func()` and assert the coroutine reached the loop.

- [ ] **Step 3: Run to verify it fails.**

- [ ] **Step 4: Add the tick as a sync closure**

Alongside `_jobs_reconcile_tick`, following `_coherence_tick`'s dispatch form exactly (read both before writing):

```python
    # Unconfirmed pass generations. A SIBLING of reconcile_live_clients, not an
    # edit to it: that sweep covers the interactive registry's adapter, and these
    # one-shot pass adapters are built per-pass, so nothing else owns them.
    # NOT gated on drafting_enabled or the Anthropic key — hiding it there would
    # strand every unconfirmed generation on a deployment with drafting off.
    def _passes_reconcile_tick() -> None:
        from call_manager.proposal_assist.passes import PASS_REGISTRY

        coro = PASS_REGISTRY.reconcile_unconfirmed()
        ...  # dispatch onto app_loop EXACTLY as _coherence_tick does, including
             # its app_loop-is-None fallback and its future handling
```

Register it with `seconds=settings.passes_reconcile_tick_seconds` and `id="passes_reconcile"`, copying the `add_job` kwargs from the `_jobs_reconcile_tick` registration in this file. **The file is the authority for those kwargs, not this plan.**

- [ ] **Step 5: Run; prove it discriminates** by re-registering the raw coroutine function instead of the closure — the test must FAIL. Restore.

- [ ] **Step 6: Full gates and commit** — `"feat(passes): reconcile unconfirmed generations on an independent tick"`

---

### Task 7: A retired pass is not a tick failure

**Files:** `scheduler.py` (`_coherence_tick` 728, `_evaluator_tick` 791, `_review_tick` 847, `_uncited_claims_tick` 894), tests

**The bug (spec §4.7).** `concurrent.futures.CancelledError` is a **different class** from `asyncio.CancelledError` and **is** an `Exception` subclass, so each tick's `except Exception` around `fut.result()` reports a deliberate retirement as a crash with a traceback.

**Revision 1's test invented `_drain_future_for_test`, which does not exist.** Rather than test a fiction, extract the shared drain into one named helper the four ticks call, and test that. The extraction is small and makes the gate real.

- [ ] **Step 1: Read all four ticks** and confirm their future-draining tails are identical apart from the pass name. If they are not, note the differences before extracting.

- [ ] **Step 2: Write the failing test**

```python
def test_a_retired_pass_logs_as_retirement_not_as_a_tick_failure(caplog):
    """Gate for spec 4.7."""
    import concurrent.futures
    import logging

    from call_manager.scheduler import _drain_pass_future

    class _CancelledFuture:
        def result(self, timeout=None):
            raise concurrent.futures.CancelledError()

    with caplog.at_level(logging.INFO):
        _drain_pass_future(_CancelledFuture(), pass_name="coherence", timeout=1)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("retire" in r.getMessage().lower() for r in caplog.records)
```

- [ ] **Step 3: Run to verify it fails** (`ImportError`).

- [ ] **Step 4: Extract the helper and use it in all four ticks**

```python
def _drain_pass_future(fut, *, pass_name: str, timeout: float) -> None:
    """Drain a pass tick's future, distinguishing a DELIBERATE retirement from a
    failure.

    concurrent.futures.CancelledError must be caught BEFORE `except Exception`,
    because — unlike asyncio.CancelledError — it IS an Exception subclass, so a
    workspace delete would otherwise be logged as a crashed tick with a full
    traceback, training operators to ignore the line that matters.
    """
    import concurrent.futures

    try:
        fut.result(timeout=timeout)
    except concurrent.futures.CancelledError:
        _logger.info(
            "%s tick pass retired (workspace deleted)",
            pass_name,
            extra={"event": "pass_tick_retired", "pass": pass_name},
        )
    except Exception:
        _logger.exception("%s tick failed", pass_name)
```

Replace each tick's inline drain with a call to it, preserving each tick's existing timeout value.

- [ ] **Step 5: Run; prove it discriminates** by removing the `except concurrent.futures.CancelledError` branch (expect FAIL); restore.

- [ ] **Step 6: Full gates and commit** — `"fix(scheduler): a retired pass is not a tick failure"`

---

### Task 8: Claim-analysis refuses to claim a deleted workspace

**Files:** `analysis/queue.py:162-187`, `tests/proposal_assist/test_claim_analysis_retirement.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""Claim-analysis retirement gates."""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime

from call_manager.db.models import ClaimAnalysisJob, Workspace
from tests.proposal_assist.conftest import make_workspace

_SHA = "0" * 40


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _job(ws_id, *, section_sha: str, status: str = "queued", attempts: int = 0):
    """section_index and commit_sha are NOT NULL with no server default."""
    return ClaimAnalysisJob(
        workspace_id=ws_id,
        section_index=0,
        section_sha=section_sha,
        commit_sha=_SHA,
        status=status,
        attempts=attempts,
    )


def test_claim_skips_jobs_for_a_soft_deleted_workspace(pg_session_factory, current_user):
    from call_manager.proposal_assist.analysis.queue import claim_queued_jobs

    with pg_session_factory() as s:
        live = make_workspace(s, owner=current_user, with_call=True)
        dead = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        dead.soft_deleted_at = _now_utc()
        s.add(_job(live.id, section_sha="a"))
        s.add(_job(dead.id, section_sha="b"))
        s.commit()
        live_id, dead_id = live.id, dead.id

    with pg_session_factory() as s:
        claimed = claim_queued_jobs(s, limit=10)
        s.commit()
        ids = {j.workspace_id for j in claimed}

    assert live_id in ids
    assert dead_id not in ids
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Add the filter**

```python
    # Refuse to claim work for a deleted workspace — durable in SQL, so it survives
    # restart, exactly like the four pass claim queries.
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    rows = (
        session.query(ClaimAnalysisJob)
        .filter(
            ClaimAnalysisJob.status == "queued",
            ClaimAnalysisJob.workspace_id.in_(live_ws),
        )
        .order_by(ClaimAnalysisJob.queued_at, ClaimAnalysisJob.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
```

`WHERE ... IN (subquery)` is not a join, so `FOR UPDATE` still locks only `claim_analysis_jobs`. No `of=` clause is needed — settled empirically during Phase-0, where the opposite was assumed and was wrong.

- [ ] **Step 4: Run, prove it discriminates, gates, commit** — `"feat(analysis): refuse to claim jobs for a deleted workspace"`

---

### Task 9: Cancel claim-analysis on the hook and its reconcile pass

**Files:** `jobs_retirement.py`, `tests/proposal_assist/test_claim_analysis_retirement.py`

**Why post-commit and why SKIP LOCKED.** `touch_running_job`'s UPDATE takes a row lock **held for the worker's whole per-job transaction** (`analysis/queue.py:190`, its own docstring says so). A cancel inside the delete transaction would block on that row while holding the uncommitted deletion — stalling DELETE and hiding the deletion from the worker's own liveness re-read. So the cancel lives on the post-commit hook, and every query uses `FOR UPDATE SKIP LOCKED`.

**`reconcile_cancelled_jobs` must be restructured, not appended to.** It currently does `if not ids: return 0` after the setup-job query, so a workspace with only claim-analysis rows returns before reaching any added block.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_hook_cancels_claim_analysis_jobs(pg_session_factory, current_user):
    from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        queued = _job(ws.id, section_sha="a", status="queued")
        running = _job(ws.id, section_sha="b", status="running")
        s.add_all([queued, running])
        s.commit()
        queued_id, running_id, ws_id = queued.id, running.id, ws.id

    asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id))

    with pg_session_factory() as s:
        for jid in (queued_id, running_id):
            job = s.get(ClaimAnalysisJob, jid)
            assert job.status == "failed", f"job {jid} left in {job.status!r}"
            assert job.last_error == "cancelled: workspace deleted"


def test_reconcile_terminalizes_a_claim_row_on_a_workspace_with_no_setup_jobs(
    pg_session_factory, current_user
):
    """Gate 10b, and the early-return bug.

    This workspace has NO setup jobs at all, so a reconcile that returns early
    when the setup query finds nothing never reaches claim-analysis. Full path: a
    running job holds its row → the hook's SKIP LOCKED cancel skips it → the
    worker's session ends WITHOUT terminalizing it (a crash, so the cooperative
    checkpoint cannot be what saves us) → a later reconcile marks it failed.
    """
    from call_manager.proposal_assist.jobs_retirement import (
        build_jobs_retirement_hook,
        reconcile_cancelled_jobs,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        job = _job(ws.id, section_sha="a", status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    holder = pg_session_factory()
    try:
        holder.query(ClaimAnalysisJob).filter_by(id=job_id).with_for_update().one()
        asyncio.run(build_jobs_retirement_hook(pg_session_factory)(ws_id))
        with pg_session_factory() as s:
            assert s.get(ClaimAnalysisJob, job_id).status == "running", (
                "the hook should have SKIPPED the locked row, not waited on it"
            )
    finally:
        holder.rollback()          # the 'crash' — no terminal write from the worker
        holder.close()

    with pg_session_factory() as s:
        reconcile_cancelled_jobs(s)
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ClaimAnalysisJob, job_id).status == "failed"


def test_the_claim_cancel_does_not_wait_on_a_row_a_worker_holds(
    pg_session_factory, current_user
):
    """The SKIP LOCKED property, asserted DIRECTLY.

    The route-level test cannot prove this: the hook's own asyncio.wait_for
    (_JOBS_CANCEL_SECONDS = 1.0) plus the coordinator's per-hook exception
    isolation would keep the route fast even with a blocking query. So assert on
    the function. Mutation: drop skip_locked and this must fail.
    """
    from call_manager.proposal_assist.jobs_retirement import (
        cancel_claim_analysis_jobs_for_workspace,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        s.add(_job(ws.id, section_sha="a", status="running"))
        s.commit()
        ws_id = ws.id

    holder = pg_session_factory()
    try:
        holder.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).with_for_update().one()
        began = time.monotonic()
        with pg_session_factory() as s:
            n = cancel_claim_analysis_jobs_for_workspace(s, ws_id)
            s.commit()
        elapsed = time.monotonic() - began
    finally:
        holder.rollback()
        holder.close()

    assert n == 0, "the held row should have been SKIPPED, not cancelled"
    assert elapsed < 2.0, f"the cancel waited {elapsed:.1f}s on a held row"
```

- [ ] **Step 2: Run to verify all three fail.**

- [ ] **Step 3: Add the claim-analysis cancel**

```python
# Non-terminal claim-analysis states. Terminalizes to 'failed', which
# ck_claim_analysis_jobs_status already allows — this slice ships NO migration.
_CLAIM_ANALYSIS_CANCELLABLE = ("queued", "running")


def cancel_claim_analysis_jobs_for_workspace(
    session: Session, workspace_id: uuid.UUID
) -> int:
    """Terminalize this workspace's active claim-analysis jobs. Caller commits.

    SKIP LOCKED for the same reason as the setup-job cancel: touch_running_job
    holds the job row for the worker's WHOLE per-job transaction, so waiting would
    make this hook inherit that lifetime. Skipped rows converge via
    reconcile_cancelled_jobs.
    """
    ids = session.scalars(
        select(ClaimAnalysisJob.id)
        .where(
            ClaimAnalysisJob.workspace_id == workspace_id,
            ClaimAnalysisJob.status.in_(_CLAIM_ANALYSIS_CANCELLABLE),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if not ids:
        return 0
    session.execute(
        update(ClaimAnalysisJob)
        .where(ClaimAnalysisJob.id.in_(ids))
        .values(
            status="failed",
            last_error="cancelled: workspace deleted",
            updated_at=datetime.now(UTC),
        )
    )
    return len(ids)
```

Call it from the hook's `_work`, summing both counts:

```python
        def _work() -> int:
            with session_factory() as session:
                n = cancel_setup_jobs_for_workspace(session, workspace_id)
                n += cancel_claim_analysis_jobs_for_workspace(session, workspace_id)
                session.commit()
                return n
```

- [ ] **Step 4: Restructure `reconcile_cancelled_jobs` to accumulate**

Remove the `if not ids: return 0` early return and make both populations independent:

```python
    total = 0

    setup_ids = session.scalars(
        select(WorkspaceSetupJob.id)
        .where(
            WorkspaceSetupJob.status.in_(_CANCELLABLE),
            WorkspaceSetupJob.workspace_id.in_(dead_ws),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if setup_ids:
        session.execute(
            update(WorkspaceSetupJob)
            .where(WorkspaceSetupJob.id.in_(setup_ids))
            .values(
                status="cancelled",
                completed_at=datetime.now(UTC),
                last_error="cancelled: workspace deleted (reconciled)",
            )
        )
        total += len(setup_ids)

    claim_ids = session.scalars(
        select(ClaimAnalysisJob.id)
        .where(
            ClaimAnalysisJob.status.in_(_CLAIM_ANALYSIS_CANCELLABLE),
            ClaimAnalysisJob.workspace_id.in_(dead_ws),
        )
        .with_for_update(skip_locked=True)
    ).all()
    if claim_ids:
        session.execute(
            update(ClaimAnalysisJob)
            .where(ClaimAnalysisJob.id.in_(claim_ids))
            .values(
                status="failed",
                last_error="cancelled: workspace deleted (reconciled)",
                updated_at=datetime.now(UTC),
            )
        )
        total += len(claim_ids)

    if total:
        logger.info(
            "jobs_retirement.reconciled",
            extra={"event": "jobs_retirement_reconciled", "rows": total},
        )
    return total
```

**Keep the existing setup-job behaviour identical** — same statuses, same `last_error`, same `completed_at`. Only the control flow changes. Run the existing Phase-0 reconcile tests to confirm.

- [ ] **Step 5: Run; prove two gates discriminate** — (a) restore the early return, expect the no-setup-jobs test to FAIL; (b) drop `skip_locked` from the claim cancel, expect the direct SKIP LOCKED test to FAIL. Restore both.

- [ ] **Step 6: Full gates and commit** — `"feat(retirement): cancel claim-analysis on the jobs hook and its reconcile"`

---

### Task 10: Abort the semantic-audit loop — as a failure, not a partial success

**Files:** `citations/audit.py` (per-row loop from line 584), `analysis/pipeline.py` (`analyze_job` 327-335, the drain loop's handler ~578), tests

**Why a per-row callback.** One check per job is **not** "bounded by one LLM call": `validate_citations_semantic` makes a per-evidence-row judgement, so a single pre-flight check leaves a 40-row job spending 40 LLM calls after the workspace is gone.

**Why it must raise, not `break`.** `analyze_job` ends with an unconditional `mark_done(session, job)` (`analysis/pipeline.py:535`). A `break` returns partial issues, execution continues through uncited analysis, and the job is marked **done** on partial data — a worse outcome than not aborting at all. The abort must prevent every subsequent write.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_audit_loop_stops_judging_rows_once_aborted(pg_session_factory, current_user):
    """Gate 10. Five judgeable rows, abort flips after the first: exactly one judge
    call, and the abort surfaces as ClaimAnalysisAborted."""
```

Before writing it, run the existing `validate_citations_semantic` tests and copy their ledger/`proposal_text` shape exactly. **Verify you get 5 judge calls with `should_abort=None` first** — a row filtered by `_SKIP_TYPES` or lacking a live marker is skipped before the judge, which would make the count 0 and the test green for the wrong reason.

```python
def test_an_aborted_job_is_never_marked_done(pg_session_factory, current_user):
    """The bug a `break` would have shipped: partial results, job marked done."""
```

Drive `analyze_job` with a `should_abort` that fires mid-audit and assert the job's final status is `failed`, never `done`, and that no annotations were committed.

- [ ] **Step 2: Run to verify both fail.**

- [ ] **Step 3: Define the exception and raise it in the loop**

In `citations/audit.py`:

```python
class ClaimAnalysisAborted(Exception):
    """The workspace was deleted mid-audit. Raised rather than returning partial
    results: analyze_job ends with an unconditional mark_done, so a partial return
    would mark the job DONE on partial data."""
```

At the top of the per-evidence-row loop:

```python
    for row in evidence:
        # Cooperative abort: claim-analysis is sync code in a thread pool with no
        # cancellation primitive, so this callback is the ONLY thing that stops the
        # remaining per-row LLM calls. Checked per ROW, not per job.
        if should_abort is not None and should_abort():
            raise ClaimAnalysisAborted(
                f"workspace {workspace_id} deleted during the semantic citation audit"
            )
        if not isinstance(row, dict) or not row.get("id"):
            continue
```

- [ ] **Step 4: Thread `should_abort` through `analyze_job` and handle the abort at the drain loop**

Add `should_abort: Callable[[], bool] | None = None` to `analyze_job`, pass it to `validate_citations_semantic`, and do **not** catch `ClaimAnalysisAborted` inside `analyze_job` — it must escape past `mark_done`.

In `drain_analysis_queue`'s per-job handler, **before** the generic `except Exception`:

```python
            except ClaimAnalysisAborted:
                s.rollback()          # discard every partial write from this job
                with session_factory() as s2:
                    job2 = s2.get(ClaimAnalysisJob, jid)
                    if job2 is not None:
                        job2.status = "failed"
                        job2.last_error = "cancelled: workspace deleted"
                        job2.updated_at = datetime.now(UTC)
                        s2.commit()
                counts["failed"] += 1
```

Construct the callback at the call site so it reads liveness in a **short independent session**, never the worker's per-job session (whose row lock is held for the whole transaction):

```python
            def _should_abort(wid=job.workspace_id) -> bool:
                with session_factory() as probe:
                    return probe.scalar(
                        select(Workspace.soft_deleted_at).where(Workspace.id == wid)
                    ) is not None
```

- [ ] **Step 5: Run; prove both gates discriminate** — (a) change the `raise` back to a `break`: the never-marked-done test must FAIL with status `done`; (b) move the check outside the loop: the judge-count test must FAIL with 5 calls. Restore both.

- [ ] **Step 6: Full gates and commit** — `"feat(analysis): abort the semantic audit as a failure, not a partial success"`

---

### Task 11: Never resurrect a cancelled job; keep the route fast

**Files:** `analysis/queue.py:216-230` (`mark_failed`), tests

**The resurrection bug.** `mark_failed` returns a job to `queued` whenever `attempts < MAX_ATTEMPTS`, so the generic handler would requeue a job the deletion just cancelled — and with Task 8's filter that row then sits `queued` forever, un-claimable and never terminal.

**On gate 14.** The end-to-end route-latency test stays as a **regression guard**, but it is *not* where SKIP LOCKED is proven — Task 9's direct test is. Revision 1 claimed the route test would fail without SKIP LOCKED; it would not, because the hook's own `asyncio.wait_for(_JOBS_CANCEL_SECONDS = 1.0)` bounds it and the coordinator isolates the resulting exception. Keep the test, drop the false claim.

- [ ] **Step 1: Write the failing tests**

```python
def test_mark_failed_does_not_requeue_a_job_whose_workspace_is_deleted(
    pg_session_factory, current_user
):
    """attempts=1 is deliberately well under MAX_ATTEMPTS so the requeue branch is
    the one that WOULD run. At the cap this would pass without the fix."""
    from call_manager.proposal_assist.analysis.queue import mark_failed

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        job = _job(ws.id, section_sha="a", status="running", attempts=1)
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        mark_failed(s, s.get(ClaimAnalysisJob, job_id), error="workspace deleted mid-job")
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ClaimAnalysisJob, job_id).status == "failed"


def test_the_delete_route_returns_within_the_barrier_with_a_worker_mid_job(
    client_authed_owner, pg_session_factory, current_user
):
    """Gate 14 — a REGRESSION GUARD on end-to-end route latency, measured as
    wall-clock on the route rather than 'the delete transaction committed'.

    NOTE: this does NOT prove SKIP LOCKED. The hook's own 1.0s wait_for and the
    coordinator's per-hook exception isolation keep the route fast even with a
    blocking cancel. The SKIP LOCKED property is proven directly in
    test_the_claim_cancel_does_not_wait_on_a_row_a_worker_holds (Task 9).
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        s.add(_job(ws.id, section_sha="a", status="running"))
        s.commit()
        ws_id = ws.id

    holder = pg_session_factory()
    try:
        holder.query(ClaimAnalysisJob).filter_by(workspace_id=ws_id).with_for_update().one()
        began = time.monotonic()
        resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
        elapsed = time.monotonic() - began
        assert resp.status_code == 204
        assert elapsed < 6.0, f"DELETE took {elapsed:.1f}s with a worker holding its row"
    finally:
        holder.rollback()
        holder.close()
```

- [ ] **Step 2: Run to verify the first fails.**

- [ ] **Step 3: Make the failure path terminal for a deleted workspace**

In `mark_failed`, before the `attempts < MAX_ATTEMPTS` requeue branch:

```python
    # A job whose workspace was deleted must never return to 'queued'. Task 8's
    # claim filter would never claim it again, so a requeue strands it non-terminal
    # forever — and the delete already decided this job's fate.
    deleted = session.scalar(
        select(Workspace.soft_deleted_at).where(Workspace.id == job.workspace_id)
    )
    if deleted is not None:
        job.status = "failed"
        return
```

- [ ] **Step 4: Run both; prove the resurrection gate discriminates** by removing the new block (expect FAIL). Restore.

- [ ] **Step 5: Final full gates and commit** — `"fix(analysis): a cancelled job goes terminal, and DELETE stays bounded"`

---

## Spec coverage

| spec section | task |
|---|---|
| §4.1 registry (adapter bound late, no handle) | 1 |
| §4.2 one entry per live task, concurrent retirement | 1, 5 |
| §4.3 the 5.5s bound, concurrent await | 1 |
| §4.4 unconfirmed transfer on EVERY exit | 1, 6 |
| §4.5 wrap points — extended to cover the prefix | 3, 4, 5 |
| §4.6 cancellation reaches the finally (verified, not changed) | 3 |
| §4.7 tick-boundary CancelledError | 7 |
| §4.8 coordinator wiring and budget | 2 |
| §5 pre-launch prefix guards | 3, 4 |
| §5.1 review's child prefix | 5 |
| §6 / §6.1 claim-analysis | 8, 9, 10, 11 |
| §12 gates 1-10b, 12, 14 | annotated per task |
| **NEW, not in the spec:** `review_runs < review_run_items` | 5 |
| §8, §9 (routes, foot-gun) | **Slice C2 — not this plan** |

**Deliberately not closed** (spec §10): teardown completion is not process death; claim-analysis cannot be cancelled mid-call; a row a live worker holds is skipped until the tick retries. The tests encode these as residuals rather than pretending otherwise.
