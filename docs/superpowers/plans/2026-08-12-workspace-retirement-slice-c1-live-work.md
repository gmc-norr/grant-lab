# Workspace retirement Slice C1 — live-work cancellation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a workspace DELETE cancel the analysis work that is actually in flight — the four LLM passes and claim-analysis — instead of letting it run on against a deleted workspace.

**Architecture:** A process-global `PASS_REGISTRY` (mirroring how collab exposes `MANAGER`) tracks one entry per live pass task. A new `"passes"` retirement hook cancels every entry for a workspace concurrently, bounded at 5.5s. Each runner's pre-launch prefix takes a workspace row lock so it cannot commit progress writes for a workspace that is being deleted. Claim-analysis, which is sync code in a thread pool and cannot be cancelled, gets the durable half only: a live-workspace claim filter, a `SKIP LOCKED` cancel on the existing `"jobs"` hook, an abort callback inside its audit loop, and a reconciliation tick for rows the hook skipped.

**Tech Stack:** Python 3.12, asyncio, FastAPI, SQLAlchemy 2.x, PostgreSQL 16, APScheduler, pytest + testcontainers.

**Source spec:** `docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md`. This plan covers the live-work half only. The six routes under the commit mutex (spec §8) and deleting the `soft_delete_workspace` foot-gun (spec §9) are **Slice C2**, a separate plan, and share no files with this one.

**Prerequisite, already merged:** the Phase-0 slice (`d459ce1`) — it introduced the `"jobs"` retirement hook, `jobs_retirement.py`, the reconciliation tick this plan extends, and the global lock order below.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **No schema migration.** Claim-analysis terminalizes to the existing `failed` status, which `ck_claim_analysis_jobs_status` already allows (`queued/running/done/failed/superseded`).
- **`_RETIREMENT_BARRIER_SECONDS` stays 6.0.** Do not change it.
- **`_PASS_RETIRE_SECONDS = 5.5`** — exceeds the adapter's internal worst case (`disconnect_deadline_seconds=3.0` + `reap_deadline_seconds=2.0` = 5.0) with margin, and fits under the 6.0 barrier.
- **`_JOBS_CANCEL_SECONDS` stays 1.0.** It already exists in `jobs_retirement.py`.
- **No change to any pass's own timeout or terminate constants** (`coherence_pass_timeout_seconds`, `coherence_terminate_timeout_seconds`, and the evaluator/review/uncited equivalents).
- **No change to `reconcile_live_clients`** (`claude_adapter.py:658`). Task 6 adds a sibling sweep for a population it does not cover; it does not edit it.
- **No new in-memory tombstone for passes.** The "refuse to claim a deleted workspace" gate already exists durably in SQL — all four `{coherence,evaluator,review,uncited_claims}/jobs.py` claim queries join `live_ws`.
- **No change to any runner's `except Exception` error handling.** `asyncio.CancelledError` inherits from `BaseException`, so it already passes through those handlers and reaches the `finally`. Relying on that is the design, not an accident.
- **Global lock order (established by the Phase-0 slice, in force now):** `commit mutex < workspace_setup_jobs < workspaces < calls`. Any new `FOR UPDATE` must respect it.
- **Acceptance is by mutation.** Every gate below must be shown to FAIL when its fix is reverted. A test you have not watched fail is not done. This project has shipped five tests that could not fail; do not add a sixth.
- **Gates before every commit:** `.venv/bin/python -m pytest tests/proposal_assist/ -q` ending `PYTEST_EXIT=0`, and `.venv/bin/ruff check .` clean. Run pytest as a **directory**, never file-by-file — a per-file pass has hidden real cross-file breakage on this repo before.
- **Commit with `git commit -F <file>`.** Heredoc `EOF` has broken commits in this repo. Trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File Structure

**Created**

| file | responsibility |
|---|---|
| `src/call_manager/proposal_assist/passes.py` | The pass registry: `Entry`, `PASS_REGISTRY`, `track()`, `retire_workspace()`, the unconfirmed-generation list and its reconciliation. One file, one responsibility: knowing what is in flight and stopping it. |
| `tests/proposal_assist/test_passes_registry.py` | Unit gates for the registry in isolation (no DB). |
| `tests/proposal_assist/test_pass_retirement.py` | Integration gates: real DELETE route against live passes. |
| `tests/proposal_assist/test_claim_analysis_retirement.py` | Claim-analysis gates, including end-to-end DELETE latency. |

**Modified**

| file | change |
|---|---|
| `drafting/claude_adapter.py` | Add `generation_confirmed_dead(launch_id)` — a public read of the same state `confirm_dead` writes. No behaviour change. |
| `drafting/fake_adapter.py` | Same method, so pass tests using the fake exercise the real code path. |
| `coherence/pass_runner.py` | `track()` wrap from adapter build onward; prefix row-lock guard. |
| `evaluator/pass_runner.py` | Same. |
| `uncited_claims/pass_runner.py` | Same. |
| `review/pass_runner.py` | Parent tracked task-only; each `_run_one_call` tracked with its own adapter; child prefix linearized on the parent `ReviewRun` row. |
| `retirement.py` | `REQUIRED_HOOKS` gains `"passes"`; the budget comment block is corrected. |
| `main.py` | Register the `"passes"` hook. |
| `scheduler.py` | New unconfirmed-generation reconcile tick; explicit `concurrent.futures.CancelledError` catch at each of the four pass tick boundaries. |
| `config.py` | One setting for the new tick's interval. |
| `analysis/queue.py` | `live_ws` filter on `claim_queued_jobs`; `mark_failed` must not resurrect a cancelled job. |
| `analysis/pipeline.py` | Thread an abort callback into `analyze_job`; stop the generic handler requeuing a cancelled job. |
| `citations/audit.py` | Call the abort callback inside the per-evidence-row loop. |
| `jobs_retirement.py` | Cancel `ClaimAnalysisJob` rows in the hook and in the reconcile pass. |

---

## Task Order and Why

Tasks 1–2 build the mechanism and wire it, so every later task has something real to hook into. Tasks 3–5 attach the four runners (coherence first as the simplest template; review last as the only fan-out). Tasks 6–7 add the two scheduler concerns. Tasks 8–11 are claim-analysis, which is independent of the registry entirely and could be done first — it is last because its end-to-end latency gate (Task 11) is the most valuable test in the plan and benefits from everything else being settled.

---

### Task 1: The pass registry

**Files:**
- Create: `src/call_manager/proposal_assist/passes.py`
- Create: `tests/proposal_assist/test_passes_registry.py`
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py` (add one method after `retiring_count`, currently line 653)
- Modify: `src/call_manager/proposal_assist/drafting/fake_adapter.py` (same method, near its `retiring_count` at line 113)

**Interfaces:**
- Consumes: `LiveClientRegistry.get(launch_id)` (`drafting/live_clients.py:66`) — returns `None` once `confirm_dead` has popped the record. That absence IS confirmation; there is no boolean flag.
- Produces, for Tasks 2–7:
  - `PASS_REGISTRY` — module-level singleton of `PassRegistry`
  - `PASS_REGISTRY.track(workspace_id: uuid.UUID, adapter=None)` — async context manager yielding an `Entry`
  - `Entry.bind_handle(handle) -> None`
  - `await PASS_REGISTRY.retire_workspace(workspace_id: uuid.UUID) -> int` (count retired; the coordinator discards it)
  - `await PASS_REGISTRY.reconcile_unconfirmed() -> int`
  - `_PASS_RETIRE_SECONDS = 5.5`
  - `ClaudeAgentSDKAdapter.generation_confirmed_dead(launch_id) -> bool`

**Why the adapter needs a new method:** the registry must ask "did that generation actually die?" without reaching into `adapter._live`. `confirm_dead` pops the record, so `_live.get(launch_id) is None` is exactly the question. Reading a private attribute from another module would work and would be wrong.

- [ ] **Step 1: Write the failing test — the registry tracks and deregisters**

Create `tests/proposal_assist/test_passes_registry.py`:

```python
"""Registry gates in isolation — no DB, no SDK. Every await here is real asyncio."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from call_manager.proposal_assist.passes import PASS_REGISTRY, _PASS_RETIRE_SECONDS


class _FakeAdapter:
    """Minimal adapter surface the registry uses: terminate + confirmation."""

    def __init__(self, *, confirmed: bool = True, terminate_delay: float = 0.0):
        self.terminated: list[object] = []
        self._confirmed = confirmed
        self._terminate_delay = terminate_delay

    async def terminate(self, handle) -> None:
        if self._terminate_delay:
            await asyncio.sleep(self._terminate_delay)
        self.terminated.append(handle)

    def generation_confirmed_dead(self, launch_id) -> bool:
        return self._confirmed


class _Handle:
    def __init__(self):
        self.state = {"launch_id": uuid.uuid4()}


@pytest.mark.asyncio
async def test_track_registers_then_deregisters_on_normal_exit():
    ws = uuid.uuid4()
    adapter = _FakeAdapter()
    async with PASS_REGISTRY.track(ws, adapter) as entry:
        entry.bind_handle(_Handle())
        assert PASS_REGISTRY.live_count(ws) == 1
    assert PASS_REGISTRY.live_count(ws) == 0
    assert PASS_REGISTRY.unconfirmed_count() == 0


@pytest.mark.asyncio
async def test_track_deregisters_when_the_body_raises():
    ws = uuid.uuid4()
    with pytest.raises(ValueError):
        async with PASS_REGISTRY.track(ws, _FakeAdapter()) as entry:
            entry.bind_handle(_Handle())
            raise ValueError("boom")
    assert PASS_REGISTRY.live_count(ws) == 0
```

- [ ] **Step 2: Run to verify it fails**

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

There is deliberately no in-memory tombstone here. "Refuse to claim a deleted
workspace" already exists durably in SQL: all four pass claim queries join
live_ws. This registry answers a different question — what is ALREADY running.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Per entry: cancel the task, then await BOTH its exit AND adapter.terminate()
# concurrently, all under this single bound.
#
# Why 5.5 and why one bound rather than two: terminate() routes to
# _retire_generation, which does `await asyncio.shield(record.teardown)`
# (claude_adapter.py:571). Under cancellation the shield delivers CancelledError
# to the awaiter while teardown keeps running — so awaiting the task's exit
# proves nothing about teardown. The adapter's own internal worst case is
# disconnect_deadline_seconds (3.0) + reap_deadline_seconds (2.0) = 5.0. An
# earlier design gave the task 1.0s and teardown 5.0s; that under-budgeted a
# cancellation landing inside launch()'s own `except BaseException:
# await self._retire_generation(...)` cleanup, which needs the FULL teardown
# budget, and a 5.0 outer bound around a 5.0 inner one is a coin flip, not a
# bound. 5.5 clears 5.0 with real margin and stays under
# _RETIREMENT_BARRIER_SECONDS (6.0).
_PASS_RETIRE_SECONDS = 5.5


@dataclass
class Entry:
    """One live pass task.

    `adapter` is OPTIONAL because review's orchestration task owns no adapter:
    run_review_pass launches nothing — each _run_one_call builds its own — so the
    parent's entry is task-only and retirement cancels it without terminating
    anything.

    The adapter is bound at ENTRY, not after launch. An entry is unbound for the
    whole of adapter.launch(), not merely between its return and a later bind:
    launch registers the client and then awaits client.connect()
    (claude_adapter.py:212), which is unbounded. Binding up front means
    retirement always has something to act on; bind_handle adds the handle once
    it exists.
    """

    workspace_id: uuid.UUID
    task: asyncio.Task
    adapter: object | None = None
    handle: object | None = field(default=None, repr=False)

    def bind_handle(self, handle: object) -> None:
        self.handle = handle

    @property
    def launch_id(self) -> uuid.UUID | None:
        state = getattr(self.handle, "state", None)
        return state.get("launch_id") if isinstance(state, dict) else None


class PassRegistry:
    """What is running, and how to stop it."""

    def __init__(self) -> None:
        self._live: dict[uuid.UUID, set[Entry]] = {}
        # Generations whose teardown COMPLETED without confirming death. Not a
        # leak-and-forget list: reconcile_unconfirmed retries them.
        self._unconfirmed: list[Entry] = []

    @contextlib.asynccontextmanager
    async def track(
        self, workspace_id: uuid.UUID, adapter: object | None = None
    ) -> AsyncIterator[Entry]:
        entry = Entry(
            workspace_id=workspace_id, task=asyncio.current_task(), adapter=adapter
        )
        self._live.setdefault(workspace_id, set()).add(entry)
        try:
            yield entry
        finally:
            # Removal is NOT discard. An entry whose generation is unconfirmed at
            # exit is TRANSFERRED to the reconciliation list — and this happens on
            # EVERY exit path, not only retirement-driven ones. An ordinary
            # completion (or a pass timeout) that calls terminate(), gets an
            # unconfirmed result and exits would otherwise leave the subprocess
            # ownerless, with nothing for the tick to inspect; deleting the
            # workspace a moment later would then find nothing to retire.
            self._forget(entry)
            if self._is_unconfirmed(entry):
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

    @staticmethod
    def _is_unconfirmed(entry: Entry) -> bool:
        """Confirmation is read from the adapter's own live-record inventory — the
        same state confirm_dead writes. No handle or no adapter means there is no
        generation to be unconfirmed about."""
        launch_id = entry.launch_id
        if launch_id is None or entry.adapter is None:
            return False
        check = getattr(entry.adapter, "generation_confirmed_dead", None)
        if check is None:
            return False
        return not check(launch_id)

    def live_count(self, workspace_id: uuid.UUID) -> int:
        return len(self._live.get(workspace_id, ()))

    def unconfirmed_count(self) -> int:
        return len(self._unconfirmed)

    async def retire_workspace(self, workspace_id: uuid.UUID) -> int:
        """Cancel every live pass for this workspace, CONCURRENTLY.

        Concurrency is what keeps the hook's cost independent of entry count:
        five review personas plus a coherence pass is six entries, and serial
        retirement would blow the budget six times over.
        """
        entries = list(self._live.get(workspace_id, ()))
        if not entries:
            return 0
        await asyncio.gather(*(self._retire_entry(e) for e in entries))
        return len(entries)

    async def _retire_entry(self, entry: Entry) -> None:
        entry.task.cancel()
        await self._await_bounded(entry)

    async def _await_bounded(self, entry: Entry) -> None:
        """Await the task's exit AND adapter.terminate() concurrently under one bound.

        Awaiting them concurrently is safe by design: _retire_generation is
        documented as shared by retirement, capacity eviction, idle sweep and
        reconciliation, so concurrent callers never spawn a second kill for the
        same generation — a second caller JOINS the single teardown task rather
        than racing it. Because this await is not itself cancelled, it observes
        the shielded teardown to completion.

        What this proves, precisely: TEARDOWN RAN TO COMPLETION. Not that the
        process died. _teardown_record returns normally when death is unconfirmed
        (claude_adapter.py:590). track()'s finally catches that case.
        """
        waits = [asyncio.shield(_swallow(entry.task))]
        if entry.adapter is not None and entry.handle is not None:
            waits.append(entry.adapter.terminate(entry.handle))
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
        """Retry terminate() for generations whose death was never confirmed, and
        drop each one that confirms.

        A remediation ATTEMPT each tick, not a guarantee — the same wording
        WorkspaceController.reconcile_live_clients uses for the population it
        covers. This is a SIBLING of that sweep, not an edit to it: these one-shot
        pass adapters are built per-pass by build_keeper_adapter and friends, so
        nothing else owns or sweeps them.
        """
        if not self._unconfirmed:
            return 0
        still: list[Entry] = []
        acted = 0
        for entry in self._unconfirmed:
            acted += 1
            try:
                if entry.adapter is not None and entry.handle is not None:
                    await entry.adapter.terminate(entry.handle)
            except Exception:
                logger.exception(
                    "passes.reconcile_terminate_failed",
                    extra={"workspace_id": str(entry.workspace_id)},
                )
            if self._is_unconfirmed(entry):
                still.append(entry)
        self._unconfirmed = still
        return acted


async def _swallow(task: asyncio.Task) -> None:
    """Await a task, absorbing its outcome. A cancelled pass raises
    CancelledError and a failed one raises its own error; retirement cares about
    neither, only that the task is finished."""
    with contextlib.suppress(BaseException):
        await task


PASS_REGISTRY = PassRegistry()
```

- [ ] **Step 4: Run the two tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_passes_registry.py -q`
Expected: `2 passed`

- [ ] **Step 5: Add the confirmation accessor to both adapters**

In `src/call_manager/proposal_assist/drafting/claude_adapter.py`, immediately after `retiring_count` (currently ending line 656):

```python
    def generation_confirmed_dead(self, launch_id: uuid.UUID) -> bool:
        """True once this generation has left the inventory.

        Only confirm_dead removes a record, and only after a reaped process, so
        absence IS confirmation. Public so PASS_REGISTRY can ask without reaching
        into _live.
        """
        return self._live.get(launch_id) is None
```

In `src/call_manager/proposal_assist/drafting/fake_adapter.py`, next to its `retiring_count` (line 113):

```python
    def generation_confirmed_dead(self, launch_id) -> bool:
        """The fake always confirms — pass tests that want the unconfirmed path
        override this attribute explicitly, so the default never hides it."""
        return True
```

- [ ] **Step 6: Write the failing tests for retirement, concurrency and the unconfirmed transfer**

Append to `tests/proposal_assist/test_passes_registry.py`:

```python
@pytest.mark.asyncio
async def test_retire_cancels_the_task_and_terminates_the_adapter():
    """Gate 8, live half."""
    ws = uuid.uuid4()
    adapter = _FakeAdapter()
    handle = _Handle()
    started = asyncio.Event()

    async def _pass():
        async with PASS_REGISTRY.track(ws, adapter) as entry:
            entry.bind_handle(handle)
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(_pass())
    await started.wait()
    retired = await PASS_REGISTRY.retire_workspace(ws)

    assert retired == 1
    assert task.cancelled() or task.done()
    assert adapter.terminated == [handle]
    assert PASS_REGISTRY.live_count(ws) == 0


@pytest.mark.asyncio
async def test_entries_retire_concurrently_not_serially():
    """Gate 7. Six entries whose terminate each takes 0.3s must finish in about
    0.3s, not 1.8s. Mutation: replace the gather in retire_workspace with a for
    loop and this must fail."""
    ws = uuid.uuid4()
    started = asyncio.Event()
    n = 6

    async def _pass(idx: int):
        async with PASS_REGISTRY.track(ws, _FakeAdapter(terminate_delay=0.3)) as entry:
            entry.bind_handle(_Handle())
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
async def test_an_unconfirmed_generation_transfers_on_ORDINARY_exit():
    """Gate 3(b) — the case that matters most.

    An ordinary completion whose terminate() returns unconfirmed must leave the
    generation on the reconciliation list, with NO deletion involved. A test
    covering only the retirement-driven path passes against the broken design.
    """
    ws = uuid.uuid4()
    adapter = _FakeAdapter(confirmed=False)
    before = PASS_REGISTRY.unconfirmed_count()

    async with PASS_REGISTRY.track(ws, adapter) as entry:
        entry.bind_handle(_Handle())

    assert PASS_REGISTRY.live_count(ws) == 0
    assert PASS_REGISTRY.unconfirmed_count() == before + 1


@pytest.mark.asyncio
async def test_reconcile_retries_and_drops_once_confirmed():
    ws = uuid.uuid4()
    adapter = _FakeAdapter(confirmed=False)
    async with PASS_REGISTRY.track(ws, adapter) as entry:
        entry.bind_handle(_Handle())
    assert PASS_REGISTRY.unconfirmed_count() >= 1

    adapter._confirmed = True          # the retry now confirms
    acted = await PASS_REGISTRY.reconcile_unconfirmed()

    assert acted >= 1
    assert PASS_REGISTRY.unconfirmed_count() == 0
```

- [ ] **Step 7: Run them; all four must pass**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_passes_registry.py -q`
Expected: `6 passed`

- [ ] **Step 8: Prove the concurrency gate discriminates**

Temporarily replace the `asyncio.gather` in `retire_workspace` with:

```python
        for e in entries:
            await self._retire_entry(e)
```

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_passes_registry.py -q -k concurrently`
Expected: **FAIL** — "retirement took ~1.8s — serialized, not concurrent"

Restore the `gather`, re-run, expect PASS. Confirm the file is clean: `git diff --stat src/`

- [ ] **Step 9: Full gates and commit**

```bash
.venv/bin/python -m pytest tests/proposal_assist/ -q
.venv/bin/ruff check .
git add src/call_manager/proposal_assist/passes.py \
        src/call_manager/proposal_assist/drafting/claude_adapter.py \
        src/call_manager/proposal_assist/drafting/fake_adapter.py \
        tests/proposal_assist/test_passes_registry.py
git commit -F .git/COMMIT_BRIEF   # message: "feat(passes): registry of in-flight passes, retired concurrently"
```

---

### Task 2: Wire the hook and pin the budget

**Files:**
- Modify: `src/call_manager/main.py:186-191`
- Modify: `src/call_manager/proposal_assist/retirement.py:28` (`REQUIRED_HOOKS`) and the comment block at lines 30-47
- Modify: `tests/proposal_assist/test_retirement_coordinator.py`

**Interfaces:**
- Consumes: `PASS_REGISTRY.retire_workspace` from Task 1.
- Produces: `REQUIRED_HOOKS == ("turns", "collab", "jobs", "passes")` — Task 5's integration test relies on the hook being live.

- [ ] **Step 1: Write the failing test**

In `tests/proposal_assist/test_retirement_coordinator.py`, add:

```python
def test_passes_is_a_required_hook():
    """Dropping the wiring must fail at BOOT, not silently skip teardown on every
    delete. This structural guard is the one that previously caught an unwired
    turns hook."""
    from call_manager.proposal_assist.retirement import (
        MissingRetirementHook,
        WorkspaceRetirement,
    )

    async def _noop(workspace_id):
        return 0

    with pytest.raises(MissingRetirementHook, match="passes"):
        WorkspaceRetirement(hooks={"turns": _noop, "collab": _noop, "jobs": _noop})


def test_the_barrier_exceeds_every_hooks_own_worst_case_including_passes():
    """Gate 12. Reads the LIVE constants, so raising any sub-bound fails loudly
    instead of silently eroding the barrier."""
    import inspect

    from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
    from call_manager.proposal_assist.jobs_retirement import _JOBS_CANCEL_SECONDS
    from call_manager.proposal_assist.passes import _PASS_RETIRE_SECONDS
    from call_manager.proposal_assist.retirement import _RETIREMENT_BARRIER_SECONDS

    sig = inspect.signature(ClaudeAgentSDKAdapter.__init__)
    disconnect = sig.parameters["disconnect_deadline_seconds"].default
    reap = sig.parameters["reap_deadline_seconds"].default

    # The passes bound must clear the adapter's own internal worst case...
    assert _PASS_RETIRE_SECONDS > disconnect + reap
    # ...and every hook must fit under the barrier. Hooks run CONCURRENTLY, so
    # the budget is the max, not the sum.
    worst = max(3.0, disconnect + reap, _PASS_RETIRE_SECONDS, _JOBS_CANCEL_SECONDS)
    assert worst < _RETIREMENT_BARRIER_SECONDS
```

- [ ] **Step 2: Run to verify both fail**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_retirement_coordinator.py -q -k "passes or worst_case"`
Expected: FAIL — the first because `WorkspaceRetirement` accepts three hooks today; the second on `ImportError` for `_PASS_RETIRE_SECONDS` only if Task 1 is absent (it is not, so it fails on the `max` assertion once `"passes"` is unwired — read the failure and confirm it is the assertion, not a typo).

- [ ] **Step 3: Add the hook to REQUIRED_HOOKS and correct the comment**

In `src/call_manager/proposal_assist/retirement.py`, line 28:

```python
REQUIRED_HOOKS = ("turns", "collab", "jobs", "passes")
```

Replace the budget lines inside the comment block above `_RETIREMENT_BARRIER_SECONDS` (currently the `collab:` / `turns:` / `jobs:` bullets) with:

```python
#   collab: max over the two rooms (they retire concurrently) of
#           _RETIRE_LOCK_WAIT_SECONDS (1.0) + _RETIRE_OPS_DRAIN_SECONDS (2.0) = 3.0
#   turns:  adapter disconnect_deadline_seconds (3.0) + reap_deadline_seconds (2.0) = 5.0
#   jobs:   _JOBS_CANCEL_SECONDS (1.0), and every query is SKIP LOCKED so it
#           never waits on a live worker's row = 1.0
#   passes: _PASS_RETIRE_SECONDS (5.5) — INDEPENDENT of entry count, because
#           PassRegistry.retire_workspace gathers its entries concurrently. If it
#           ever retired serially this budget would silently become N x 5.5.
# => 5.5 worst case. 6.0 keeps headroom.
```

- [ ] **Step 4: Register the hook**

In `src/call_manager/main.py`, inside the `hooks={...}` dict at line 187 (alongside the existing `"jobs"` entry at line 190):

```python
    from call_manager.proposal_assist.passes import PASS_REGISTRY
```

and in the dict:

```python
            "passes": PASS_REGISTRY.retire_workspace,
```

- [ ] **Step 5: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_retirement_coordinator.py -q`
Expected: all pass.

- [ ] **Step 6: Prove the budget gate discriminates**

Temporarily set `_PASS_RETIRE_SECONDS = 6.5` in `passes.py`.
Run: `.venv/bin/python -m pytest tests/proposal_assist/test_retirement_coordinator.py -q -k worst_case`
Expected: **FAIL**. Restore 5.5, re-run, expect PASS.

- [ ] **Step 7: Full gates and commit**

```bash
.venv/bin/python -m pytest tests/proposal_assist/ -q
.venv/bin/ruff check .
git add src/call_manager/main.py src/call_manager/proposal_assist/retirement.py \
        tests/proposal_assist/test_retirement_coordinator.py
git commit -F .git/COMMIT_BRIEF   # "feat(retirement): require and wire the passes hook"
```

---

### Task 3: Coherence — track the pass and guard its prefix

**Files:**
- Modify: `src/call_manager/proposal_assist/coherence/pass_runner.py:45-116`
- Create: `tests/proposal_assist/test_pass_retirement.py`

**Interfaces:**
- Consumes: `PASS_REGISTRY.track`, `Entry.bind_handle` (Task 1).
- Produces: the wrap+guard shape Tasks 4 and 5 repeat. Coherence is the template: read it before doing the others.

**Two independent changes, both in this file.**

**(a) The prefix guard (spec §5).** The prefix reads workspace files through `assemble_coherence_context(...)` and can commit a cap-failure. Load the workspace `with_for_update=True` and abort if `soft_deleted_at` is set, holding that row lock through the commit. The delete transaction's `ws.soft_deleted_at = ...` takes the same row lock, so only two orders exist: the prefix commits and the delete waits briefly behind it, or the delete wins and the prefix aborts having written nothing.

This is safe **only because the prefix is short and its session commits promptly.** The Phase-0 slice showed this same idiom being unsafe when the holding session lives for minutes. That difference is not stylistic — Step 5's test pins the lock duration rather than assuming it.

**(b) The tracking wrap (spec §4.5).** Everything from the adapter build onward goes inside `track`.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_pass_retirement.py`:

```python
"""Integration gates: the real DELETE route against live passes."""

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

# `make_workspace`, `current_user`, `client_authed_owner` and `pg_session_factory`
# are real fixtures — see tests/proposal_assist/conftest.py and tests/conftest.py.
# There is deliberately NO settings fixture in this repo: every existing pass test
# constructs `Settings()` inline (e.g. tests/proposal_assist/coherence/
# test_pass_runner.py:34). Follow that; do not add a fixture for it.


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _never_called_adapter_builder(**kwargs):
    """Fails loudly if the prefix guard did NOT abort. This is the assertion —
    the tests below rely on it being reached only when the guard is broken."""
    raise AssertionError("the prefix guard should have aborted before the adapter build")


def _stub_adapter_builder(**kwargs):
    """A do-nothing adapter for tests that need the pass to proceed past the
    prefix but do not care what the LLM does."""

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

        def generation_confirmed_dead(self, launch_id) -> bool:
            return True

    return _Stub()


def test_the_coherence_prefix_aborts_for_a_workspace_deleted_under_it(
    pg_session_factory, current_user
):
    """Gate 9. A delete committing while the prefix is mid-flight must leave ZERO
    prefix writes."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = CoherenceJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    # Delete BEFORE the pass runs: the prefix must see soft_deleted_at and stop.
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
        job = s.get(CoherenceJob, job_id)
        # The prefix aborted: it did not fail the job with a budget message, and
        # crucially it never built an adapter (that builder raises if called).
        assert job.status == "running"


def test_the_coherence_prefix_does_not_hold_its_row_lock_for_long(
    pg_session_factory, current_user
):
    """Gate 9, second half. The guard is only safe because the prefix is short.
    A concurrent workspace-row UPDATE must not be blocked for more than a moment."""
    ...  # full body in Step 4
```

Both helpers (`_never_called_adapter_builder`, `_stub_adapter_builder`) and `_now_utc` are defined in the file header above — do not redefine them per test.

- [ ] **Step 2: Run to verify the first test fails**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q -k prefix_aborts`
Expected: FAIL — `AssertionError: the prefix guard should have aborted before the adapter build`, because today's prefix reads the workspace without checking `soft_deleted_at`.

- [ ] **Step 3: Add the guard and the wrap**

In `coherence/pass_runner.py`, replace lines 45-59 (the prefix session) with:

```python
    # 1. Read job + workspace state in a short session, under a workspace ROW LOCK.
    #    The lock is what linearizes this prefix against a concurrent delete: the
    #    delete transaction takes the same row to stamp soft_deleted_at, so either
    #    we commit and it waits briefly, or it wins and we abort having written
    #    nothing. Safe ONLY because this session commits promptly — see the Phase-0
    #    slice for the same idiom being unsafe on a long-lived session.
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
        if coherence_cap_exceeded(s, workspace_id=workspace_id, settings=settings):
            _fail(s, job, "daily coherence budget reached")
            s.commit()
            return
        instruction = assemble_coherence_context(
            session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
        )
```

Then wrap from the adapter build (line 61) to the end of the existing `finally` (line 115). The body is unchanged — only its indentation and the two new lines:

```python
    adapter = adapter_builder(
        settings=settings,
        session_factory=session_factory,
        workspace_id=workspace_id,
        requested_by=requested_by,
        job_id=job_id,
        locked=locked,
    )
    handle = None
    done: TurnDoneEvent | None = None
    async with PASS_REGISTRY.track(workspace_id, adapter) as _entry:
        try:

            async def _run() -> TurnDoneEvent | None:
                nonlocal handle
                handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
                _entry.bind_handle(handle)
                last = None
                async for event in adapter.send_turn(handle, instruction):
                    if isinstance(event, TurnDoneEvent):
                        last = event
                return last

            done = await asyncio.wait_for(
                _run(), timeout=settings.coherence_pass_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001 — terminal: fail the job, never propagate
            # UNCHANGED. asyncio.CancelledError inherits from BaseException, so a
            # retirement cancellation passes straight through this handler to the
            # finally — and skipping this failure path is CORRECT, because the
            # delete transaction already terminalized the job row.
            ...  # keep the existing body verbatim
        finally:
            ...  # keep the existing terminate block verbatim
```

Add the import at the top of the file:

```python
from call_manager.proposal_assist.passes import PASS_REGISTRY
```

**Note on `bind_handle` placement:** it goes on the line immediately after `launch` returns, inside `_run`. The adapter is already bound at entry, so the window before this line is covered.

- [ ] **Step 4: Write the lock-duration test body**

Replace the `...` in `test_the_coherence_prefix_does_not_hold_its_row_lock_for_long`:

```python
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = CoherenceJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    blocked_for: list[float] = []

    def _competing_update():
        # Start after the prefix has had time to take its lock.
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
        f"the prefix held the workspace row for {blocked_for[0]:.1f}s — the guard is "
        "only safe while this prefix stays short"
    )
```

- [ ] **Step 5: Run both; both must pass**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q`
Expected: `2 passed`

- [ ] **Step 6: Prove the guard gate discriminates**

Temporarily revert the guard: change `with_for_update=True` back to a plain `s.get(Workspace, job.workspace_id)` and delete the `soft_deleted_at` check.
Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q -k prefix_aborts`
Expected: **FAIL**. Restore, re-run, expect PASS.

- [ ] **Step 7: Full gates and commit**

```bash
.venv/bin/python -m pytest tests/proposal_assist/ -q
.venv/bin/ruff check .
git add src/call_manager/proposal_assist/coherence/pass_runner.py \
        tests/proposal_assist/test_pass_retirement.py
git commit -F .git/COMMIT_BRIEF   # "feat(coherence): track the pass and guard its prefix"
```

---

### Task 4: Evaluator and uncited-claims — same treatment

**Files:**
- Modify: `src/call_manager/proposal_assist/evaluator/pass_runner.py:57-75` (prefix) and the adapter-build-onward block
- Modify: `src/call_manager/proposal_assist/uncited_claims/pass_runner.py:84-97` (prefix) and the adapter-build-onward block
- Modify: `tests/proposal_assist/test_pass_retirement.py`

**Interfaces:**
- Consumes: `PASS_REGISTRY.track`, `Entry.bind_handle` (Task 1); the shape established in Task 3.
- Produces: nothing new.

**Why these two matter more than coherence:** both commit **durable progress writes** in their prefix before any tracking point exists. Evaluator commits `evaluated_draft_sha`, `evaluated_spec_sha` and `evaluated_rubric_updated_at` (`evaluator/pass_runner.py:68-70`); uncited-claims commits `evaluated_draft_sha` (`uncited_claims/pass_runner.py:93`). Those are exactly the writes the guard must prevent for a deleted workspace.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_pass_retirement.py`:

```python
def test_the_evaluator_prefix_writes_nothing_for_a_deleted_workspace(
    pg_session_factory, current_user
):
    """Gate 9 for the evaluator. Unlike coherence, this prefix COMMITS progress
    (evaluated_draft_sha / evaluated_spec_sha / evaluated_rubric_updated_at), so a
    missing guard is a durable write against a deleted workspace, not just wasted
    work."""
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
    """Gate 9 for uncited-claims — it commits evaluated_draft_sha in its prefix."""
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

- [ ] **Step 2: Run to verify both fail**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q -k "evaluator_prefix or uncited_prefix"`
Expected: FAIL — `AssertionError: the prefix guard should have aborted before the adapter build`.

- [ ] **Step 3: Add the guard to the evaluator prefix**

In `evaluator/pass_runner.py`, the prefix currently does `ws = s.get(Workspace, job.workspace_id)` before line 57. Change that read to take the lock and add the abort, immediately after the `job.status != "running"` early return:

```python
        ws = s.get(Workspace, job.workspace_id, with_for_update=True)
        if ws is None or ws.soft_deleted_at is not None:
            return
```

Everything below (`workspace_id = job.workspace_id` onward) is unchanged. Note the existing code already uses `ws` at line 70 (`ws.rubric_updated_at if ws else None`) — with the guard above, `ws` can no longer be `None` there, but **leave that expression alone**; narrowing it is not this task's business.

- [ ] **Step 4: Add the guard to the uncited-claims prefix**

Identical change in `uncited_claims/pass_runner.py`, immediately after its `job.status != "running"` early return:

```python
        ws = s.get(Workspace, job.workspace_id, with_for_update=True)
        if ws is None or ws.soft_deleted_at is not None:
            return
```

- [ ] **Step 5: Add the tracking wrap to both**

In each file, wrap from the `adapter = adapter_builder(...)` line through the end of the existing `finally` block in `async with PASS_REGISTRY.track(workspace_id, adapter) as _entry:`, and add `_entry.bind_handle(handle)` on the line immediately after `handle = await adapter.launch(...)` inside the inner `_run`. Add to each file's imports:

```python
from call_manager.proposal_assist.passes import PASS_REGISTRY
```

Do not touch either runner's `except Exception` body or its terminate block.

- [ ] **Step 6: Run to verify they pass**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q`
Expected: `4 passed`

- [ ] **Step 7: Prove both gates discriminate**

For each runner in turn: remove the two guard lines, run that runner's test, confirm **FAIL**, restore, confirm PASS. Record all four runs.

- [ ] **Step 8: Full gates and commit**

```bash
.venv/bin/python -m pytest tests/proposal_assist/ -q
.venv/bin/ruff check .
git add src/call_manager/proposal_assist/evaluator/pass_runner.py \
        src/call_manager/proposal_assist/uncited_claims/pass_runner.py \
        tests/proposal_assist/test_pass_retirement.py
git commit -F .git/COMMIT_BRIEF   # "feat(passes): track and guard the evaluator and uncited runners"
```

---

### Task 5: Review — the fan-out, and its second prefix

**Files:**
- Modify: `src/call_manager/proposal_assist/review/pass_runner.py:60-93` (child prefix), `:95-133` (child wrap), `:193-232` (parent prefix), `:235-260` (parent wrap)
- Modify: `tests/proposal_assist/test_pass_retirement.py`

**Interfaces:**
- Consumes: `PASS_REGISTRY.track(workspace_id, adapter=None)` — the adapterless form (Task 1).
- Produces: nothing new.

**Why review is different, and the mistake to avoid.** `run_review_pass` **launches nothing.** Every `_run_one_call` constructs its own adapter and its own handle (`review/pass_runner.py:95`), so each child has a distinct adapter-local generation. There is no parent handle to bind and no shared generation to inherit. So:

- the orchestration task is tracked **task-only** — `track(workspace_id)` with no adapter;
- **each child task is tracked as its own entry**, with its own adapter and handle.

Making the children share the parent's entry is the specific error this task exists to prevent, and Step 6 mutates exactly that.

**Review's second prefix (spec §5.1).** Each `_run_one_call` commits `item.status = "running"` (line 90-93) before its adapter is constructed. It does re-read `run.status` first (line 84) — but that read takes **no lock**, so it is a TOCTOU window: a DELETE can cancel the parent run between the read and the commit, after which the child commits `running` and is then cancelled, stranding a `ReviewRunItem` in `running` on a deleted workspace.

The child prefix is linearized on the **parent `ReviewRun` row**, not the workspace row. The `ReviewRun` row is the tighter choice: the delete transaction already updates review-run rows through `cancel_review_runs_for_workspace`, so locking it linearizes against exactly the write that matters — without adding N persona-shaped contenders to the workspace row that every delete must pass through.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_pass_retirement.py`:

```python
@pytest.mark.asyncio
async def test_every_review_child_is_tracked_as_its_own_entry():
    """Gate 6. The parent is adapterless and task-only; each child carries its own
    adapter and handle. Mutation: make the children share the parent's entry and
    this must fail."""
    ws = uuid.uuid4()
    seen: list[int] = []

    async def _child(idx: int):
        adapter = _CountingAdapter()
        async with PASS_REGISTRY.track(ws, adapter) as entry:
            entry.bind_handle(_Handle())
            seen.append(PASS_REGISTRY.live_count(ws))
            await asyncio.sleep(0.2)

    async def _parent():
        async with PASS_REGISTRY.track(ws):        # task-only: NO adapter
            assert PASS_REGISTRY.live_count(ws) == 1
            await asyncio.gather(*(_child(i) for i in range(5)))

    await _parent()
    # parent + 5 children were all live simultaneously
    assert max(seen) == 6
    assert PASS_REGISTRY.live_count(ws) == 0


def test_a_delete_mid_child_prefix_strands_no_review_item_in_running(
    pg_session_factory, current_user
):
    """Gate 6, second case. A DELETE cancelling the run while a child sits between
    its run.status read and its commit must leave no ReviewRunItem in 'running'."""
    from call_manager.db.models import ReviewRun, ReviewRunItem

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        run = ReviewRun(workspace_id=ws.id, status="running")
        s.add(run)
        s.flush()
        s.add(ReviewRunItem(review_run_id=run.id, kind="fit_check", status="queued"))
        s.commit()
        run_id, ws_id = run.id, ws.id

    # Cancel the run the way the delete transaction does, BEFORE the child runs.
    with pg_session_factory() as s:
        s.get(ReviewRun, run_id).status = "cancelled"
        s.commit()

    asyncio.run(
        _run_one_call(
            kind="fit_check",
            instruction="x",
            run_id=run_id,
            workspace_id=ws_id,
            session_factory=pg_session_factory,
            settings=Settings(),
            adapter_builder=_never_called_adapter_builder,
        )
    )

    with pg_session_factory() as s:
        item = s.scalar(
            select(ReviewRunItem).where(ReviewRunItem.review_run_id == run_id)
        )
        assert item.status != "running", "child committed 'running' on a cancelled run"
```

Add the `_CountingAdapter` helper next to `_FakeAdapter` conventions already in the file:

```python
class _CountingAdapter:
    async def terminate(self, handle) -> None:
        return None

    def generation_confirmed_dead(self, launch_id) -> bool:
        return True
```

- [ ] **Step 2: Run to verify both fail**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q -k "review_child or child_prefix"`
Expected: FAIL — the first because no tracking exists in review yet (`live_count` stays 0); the second because the child commits `running` without locking the parent run.

- [ ] **Step 3: Lock the parent run in the child prefix**

In `review/pass_runner.py`, replace the `run = s.get(ReviewRun, run_id)` at line 66 with a locked read, and move it above the item lookup so the lock is taken first:

```python
    with session_factory() as s:
        # Lock the PARENT run row for the whole prefix. The bare read this
        # replaces was a TOCTOU window: a DELETE cancels review runs through
        # cancel_review_runs_for_workspace, and could land between the status
        # check below and the `item.status = "running"` commit, stranding the item
        # in 'running' on a deleted workspace. The ReviewRun row is the right lock
        # rather than the workspace row: it linearizes against exactly the write
        # that matters, without putting N persona-shaped contenders on the row
        # every delete must pass through.
        run = s.get(ReviewRun, run_id, with_for_update=True)
        item = s.scalar(
            select(ReviewRunItem).where(
                ReviewRunItem.review_run_id == run_id, ReviewRunItem.kind == kind
            )
        )
```

The rest of the prefix (lines 75-93) is unchanged — the existing `if run is None or run.status != "running":` branch now runs under the lock and its decision can no longer be overtaken.

- [ ] **Step 4: Track the parent, task-only**

In `run_review_pass`, wrap from the wave-1 semaphore (line 235) through the end of the function in:

```python
    async with PASS_REGISTRY.track(workspace_id):
        # Task-only: run_review_pass owns NO adapter — every _run_one_call builds
        # its own. Retirement cancels this task; there is nothing here to
        # terminate.
        sem = asyncio.Semaphore(settings.review_persona_concurrency)
        ...  # the existing body, indented one level
```

- [ ] **Step 5: Track each child, with its adapter**

In `_run_one_call`, wrap from `adapter = adapter_builder(...)` (line 95) through the end of the existing `finally` (line 133) in `async with PASS_REGISTRY.track(workspace_id, adapter) as _entry:`, and add `_entry.bind_handle(handle)` immediately after `handle = await adapter.launch(...)` inside `_drive`. Add the import:

```python
from call_manager.proposal_assist.passes import PASS_REGISTRY
```

The post-call finalization session (lines 135-173) stays **outside** the wrap — it is the child's own bookkeeping and must run whether or not the pass was retired.

- [ ] **Step 6: Run, then prove the fan-out gate discriminates**

Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q`
Expected: all pass.

Now mutate: make the children share the parent entry by deleting the `async with PASS_REGISTRY.track(workspace_id, adapter) as _entry:` line in `_run_one_call` (and its indentation).
Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q -k review_child`
Expected: **FAIL** — `assert max(seen) == 6` sees 1.
Restore, re-run, expect PASS.

Then mutate the prefix: change `with_for_update=True` back to a bare `s.get(ReviewRun, run_id)`.
Run: `.venv/bin/python -m pytest tests/proposal_assist/test_pass_retirement.py -q -k child_prefix`
Expected: **FAIL**. Restore, confirm PASS.

- [ ] **Step 7: Full gates and commit**

```bash
.venv/bin/python -m pytest tests/proposal_assist/ -q
.venv/bin/ruff check .
git add src/call_manager/proposal_assist/review/pass_runner.py \
        tests/proposal_assist/test_pass_retirement.py
git commit -F .git/COMMIT_BRIEF   # "feat(review): track parent and children separately, lock the child prefix"
```

---

### Task 6: The unconfirmed-generation reconciliation tick

**Files:**
- Modify: `src/call_manager/config.py` (one setting, next to the other tick intervals)
- Modify: `src/call_manager/scheduler.py` (one tick at module scope)
- Modify: `tests/proposal_assist/test_phase0_scheduler_tick.py` or a new `tests/proposal_assist/test_passes_reconcile_tick.py`

**Interfaces:**
- Consumes: `PASS_REGISTRY.reconcile_unconfirmed()` (Task 1).
- Produces: nothing later tasks depend on.

**Placement rule, inherited from the Phase-0 slice:** register this tick at **module scope**, NOT inside any block gated on `DRAFTING_ENABLED` or `ANTHROPIC_API_KEY`. Hiding reconciliation behind those guards would strand every unconfirmed generation on a deployment with drafting off. The Phase-0 slice made exactly this mistake and fixed it; do not reintroduce it.

- [ ] **Step 1: Add the setting**

In `src/call_manager/config.py`, next to `jobs_reconcile_tick_seconds`:

```python
    passes_reconcile_tick_seconds: int = 60
```

- [ ] **Step 2: Write the failing test**

```python
@pytest.mark.asyncio
async def test_the_reconcile_tick_retries_an_unconfirmed_generation():
    """Gate 3(b), scheduler half."""
    from call_manager.scheduler import passes_reconcile_tick

    ws = uuid.uuid4()
    adapter = _FakeAdapter(confirmed=False)
    async with PASS_REGISTRY.track(ws, adapter) as entry:
        entry.bind_handle(_Handle())
    assert PASS_REGISTRY.unconfirmed_count() == 1

    adapter._confirmed = True
    await passes_reconcile_tick()

    assert PASS_REGISTRY.unconfirmed_count() == 0
```

- [ ] **Step 3: Run to verify it fails**

Expected: `ImportError: cannot import name 'passes_reconcile_tick'`

- [ ] **Step 4: Add the tick**

In `src/call_manager/scheduler.py`, at module scope alongside `jobs_retirement_reconcile_tick`:

```python
async def passes_reconcile_tick() -> None:
    """Retry teardown for pass generations whose death was never confirmed.

    A SIBLING of WorkspaceController.reconcile_live_clients, not an edit to it:
    that sweep covers the interactive registry's adapter, and these one-shot pass
    adapters are built per-pass by build_keeper_adapter and friends, so nothing
    else owns them.

    Registered at MODULE SCOPE, deliberately outside the DRAFTING_ENABLED /
    ANTHROPIC_API_KEY guards — hiding it behind those would strand every
    unconfirmed generation on a deployment with drafting off.
    """
    from call_manager.proposal_assist.passes import PASS_REGISTRY

    n = await PASS_REGISTRY.reconcile_unconfirmed()
    if n:
        logger.info(
            "passes.reconciled",
            extra={"event": "passes_reconciled", "entries": n},
        )
```

Register it next to the existing `jobs_retirement_reconcile_tick` registration. Open `scheduler.py`, find that registration, and copy its form exactly — it is the correct precedent for a module-scope tick and it is already outside the drafting guards:

```python
    scheduler.add_job(
        passes_reconcile_tick,
        "interval",
        seconds=settings.passes_reconcile_tick_seconds,
        id="passes_reconcile",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
```

**Check two things before you commit this:** that the `add_job` kwargs above match what `jobs_retirement_reconcile_tick` actually uses in this file (correct my guess if it differs — the file is the authority, not this plan), and that your registration is NOT inside any `if settings.drafting_enabled:` or `ANTHROPIC_API_KEY` block.

- [ ] **Step 5: Run to verify it passes, then gates and commit**

```bash
.venv/bin/python -m pytest tests/proposal_assist/ -q
.venv/bin/ruff check .
git add src/call_manager/config.py src/call_manager/scheduler.py tests/proposal_assist/
git commit -F .git/COMMIT_BRIEF   # "feat(passes): reconcile unconfirmed generations on an independent tick"
```

---

### Task 7: Stop the tick boundary logging a deliberate cancellation as a crash

**Files:**
- Modify: `src/call_manager/scheduler.py` — `_coherence_tick` (728), `_evaluator_tick` (791), `_review_tick` (847), `_uncited_claims_tick` (894)
- Modify: `tests/proposal_assist/test_phase0_scheduler_tick.py`

**Interfaces:** none new.

**The bug (spec §4.7).** `concurrent.futures.CancelledError` is a **different class** from `asyncio.CancelledError` and **is** an `Exception` subclass. Each pass tick calls `fut.result()` inside an `except Exception` handler, so a deliberate retirement cancellation surfaces as `"coherence tick failed"` with a full traceback. Operationally that trains people to ignore the log line that matters.

- [ ] **Step 1: Write the failing test**

```python
def test_a_retired_pass_logs_as_retirement_not_as_a_tick_failure(caplog):
    """Gate for spec 4.7. concurrent.futures.CancelledError IS an Exception, so
    the tick's `except Exception` would report a deliberate cancellation as a
    crash."""
    import concurrent.futures

    class _CancelledFuture:
        def result(self, timeout=None):
            raise concurrent.futures.CancelledError()

    with caplog.at_level(logging.INFO):
        _drain_future_for_test(_CancelledFuture(), name="coherence")

    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert any("retire" in r.getMessage().lower() for r in caplog.records)
```

- [ ] **Step 2: Run to verify it fails**

Expected: FAIL — an ERROR record is present.

- [ ] **Step 3: Add the explicit catch to all four ticks**

In each of the four ticks, **before** the existing `except Exception:` handler around `fut.result(...)`:

```python
        except concurrent.futures.CancelledError:
            # A workspace delete retired this pass. Deliberate, not a failure —
            # and it must be caught BEFORE `except Exception`, because unlike
            # asyncio.CancelledError this class IS an Exception subclass.
            logger.info(
                "coherence tick pass retired (workspace deleted)",
                extra={"event": "pass_tick_retired", "pass": "coherence"},
            )
```

Use the matching pass name in each of the four. Ensure `import concurrent.futures` is present at the top of `scheduler.py`.

- [ ] **Step 4: Run, prove it discriminates by deleting the new catch, restore**

- [ ] **Step 5: Full gates and commit**

```bash
git add src/call_manager/scheduler.py tests/proposal_assist/
git commit -F .git/COMMIT_BRIEF   # "fix(scheduler): a retired pass is not a tick failure"
```

---

### Task 8: Claim-analysis refuses to claim a deleted workspace

**Files:**
- Modify: `src/call_manager/proposal_assist/analysis/queue.py:162-187` (`claim_queued_jobs`)
- Create: `tests/proposal_assist/test_claim_analysis_retirement.py`

**Interfaces:**
- Produces: nothing later tasks consume; Tasks 9-11 are independent of this one.

This is the one guarantee the other four passes already have and claim-analysis does not: its claim filters on `status == "queued"` alone.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_claim_analysis_retirement.py`:

```python
def test_claim_skips_jobs_for_a_soft_deleted_workspace(pg_session_factory, current_user):
    """Durable in SQL, survives restart — the same gate the other four passes have."""
    from call_manager.db.models import ClaimAnalysisJob, Workspace
    from call_manager.proposal_assist.analysis.queue import claim_queued_jobs

    with pg_session_factory() as s:
        live = make_workspace(s, owner=current_user, with_call=True)
        dead = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        dead.soft_deleted_at = _now_utc()
        s.add(ClaimAnalysisJob(workspace_id=live.id, section_sha="a", status="queued"))
        s.add(ClaimAnalysisJob(workspace_id=dead.id, section_sha="b", status="queued"))
        s.commit()
        live_id, dead_id = live.id, dead.id

    with pg_session_factory() as s:
        claimed = claim_queued_jobs(s, limit=10)
        s.commit()
        ids = {j.workspace_id for j in claimed}

    assert live_id in ids
    assert dead_id not in ids
```

- [ ] **Step 2: Run to verify it fails** — the deleted workspace's job IS claimed today.

- [ ] **Step 3: Add the filter**

In `claim_queued_jobs`, add the live-workspace join, mirroring the four pass claim queries:

```python
    # Refuse to claim work for a deleted workspace — durable in SQL, so it
    # survives restart, exactly like the four pass claim queries.
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

Add `Workspace` and `select` to the module's imports if absent.

**Lock-widening note:** `WHERE ... IN (subquery)` is not a join, so `FOR UPDATE` still locks only `claim_analysis_jobs` rows. No `of=` clause is needed and adding one is not required. (This was settled empirically during the Phase-0 slice, where the opposite was assumed and was wrong.)

- [ ] **Step 4: Run, prove it discriminates, gates, commit**

```bash
git add src/call_manager/proposal_assist/analysis/queue.py tests/proposal_assist/test_claim_analysis_retirement.py
git commit -F .git/COMMIT_BRIEF   # "feat(analysis): refuse to claim jobs for a deleted workspace"
```

---

### Task 9: Cancel claim-analysis rows on the jobs hook and its reconcile pass

**Files:**
- Modify: `src/call_manager/proposal_assist/jobs_retirement.py` (both `cancel_setup_jobs_for_workspace`'s caller path and `reconcile_cancelled_jobs`)
- Modify: `tests/proposal_assist/test_phase0_retirement.py` or `tests/proposal_assist/test_claim_analysis_retirement.py`

**Interfaces:**
- Consumes: the `"jobs"` hook and `reconcile_cancelled_jobs` shipped by the Phase-0 slice.
- Produces: `cancel_claim_analysis_jobs_for_workspace(session, workspace_id) -> int`.

**Why post-commit and why SKIP LOCKED.** `touch_running_job`'s UPDATE takes a row lock **held for the whole per-job transaction** (`analysis/queue.py:190`, and its own docstring says so). A cancel UPDATE inside the delete transaction would block on that row while holding the uncommitted workspace deletion — stalling DELETE and making the worker's own liveness re-read unable to observe the deletion. So the cancel lives on the post-commit hook, and every query uses `FOR UPDATE SKIP LOCKED`: a row a live worker holds is **skipped**, never waited on, so the hook can never inherit the worker's transaction lifetime.

**This extension is required, not optional.** Without the reconcile half, a skipped row is never cancelled by anything: the worker's cooperative checkpoint only aborts a **live** worker, and a crashed one leaves the row behind forever.

- [ ] **Step 1: Write the failing tests**

```python
def test_the_hook_cancels_claim_analysis_jobs(pg_session_factory, current_user):
    """Both non-terminal states go terminal when the workspace is deleted."""
    from call_manager.db.models import ClaimAnalysisJob
    from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        queued = ClaimAnalysisJob(workspace_id=ws.id, section_sha="a", status="queued")
        running = ClaimAnalysisJob(workspace_id=ws.id, section_sha="b", status="running")
        s.add_all([queued, running])
        s.commit()
        queued_id, running_id, ws_id = queued.id, running.id, ws.id

    hook = build_jobs_retirement_hook(pg_session_factory)
    asyncio.run(hook(ws_id))

    with pg_session_factory() as s:
        for jid in (queued_id, running_id):
            job = s.get(ClaimAnalysisJob, jid)
            assert job.status == "failed", f"job {jid} left in {job.status!r}"
            assert job.last_error == "cancelled: workspace deleted"


def test_reconcile_terminalizes_a_claim_analysis_row_the_hook_skipped(
    pg_session_factory, current_user
):
    """Gate 10b — the FULL path, not just the hook.

    A running job holds its row → the hook's SKIP LOCKED cancel skips it → the
    worker's session ends WITHOUT terminalizing it (simulating a crash, so the
    cooperative checkpoint cannot be what saves us) → a later reconcile marks the
    row 'failed'. Mutation: leave the tick handling setup jobs only and the row
    stays 'running' forever.
    """
    from call_manager.db.models import ClaimAnalysisJob, Workspace
    from call_manager.proposal_assist.jobs_retirement import (
        build_jobs_retirement_hook,
        reconcile_cancelled_jobs,
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        job = ClaimAnalysisJob(workspace_id=ws.id, section_sha="a", status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    holder = pg_session_factory()
    try:
        # A live worker's per-job transaction holding the row.
        holder.query(ClaimAnalysisJob).filter_by(id=job_id).with_for_update().one()

        hook = build_jobs_retirement_hook(pg_session_factory)
        asyncio.run(hook(ws_id))

        with pg_session_factory() as s:
            assert s.get(ClaimAnalysisJob, job_id).status == "running", (
                "the hook should have SKIPPED the locked row, not waited on it"
            )
    finally:
        holder.rollback()      # the 'crash' — no terminal write from the worker
        holder.close()

    with pg_session_factory() as s:
        reconcile_cancelled_jobs(s)
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ClaimAnalysisJob, job_id).status == "failed"
```

- [ ] **Step 2: Run to verify both fail.**

- [ ] **Step 3: Add the claim-analysis cancel**

In `jobs_retirement.py`, add alongside `cancel_setup_jobs_for_workspace`:

```python
# Non-terminal claim-analysis states. Terminalizes to 'failed', which
# ck_claim_analysis_jobs_status already allows (queued/running/done/failed/
# superseded) — this slice ships NO migration.
_CLAIM_ANALYSIS_CANCELLABLE = ("queued", "running")


def cancel_claim_analysis_jobs_for_workspace(
    session: Session, workspace_id: uuid.UUID
) -> int:
    """Terminalize this workspace's active claim-analysis jobs. Caller commits.

    SKIP LOCKED for the same reason as the setup-job cancel: touch_running_job
    holds the job row for the worker's WHOLE per-job transaction, so waiting on it
    would make this hook inherit that lifetime and stall DELETE. Skipped rows
    converge via reconcile_cancelled_jobs.
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

Call it from `build_jobs_retirement_hook`'s `_work`, summing both counts:

```python
        def _work() -> int:
            with session_factory() as session:
                n = cancel_setup_jobs_for_workspace(session, workspace_id)
                n += cancel_claim_analysis_jobs_for_workspace(session, workspace_id)
                session.commit()
                return n
```

- [ ] **Step 4: Extend the reconcile pass**

In `reconcile_cancelled_jobs`, after the existing setup-job block and before the `logger.info`, add:

```python
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
```

`dead_ws` is the existing subquery already defined in this function. Add `len(claim_ids)` to the returned count and to the `rows` value in the `logger.info` call, so the log reports total rows touched rather than only setup jobs.

- [ ] **Step 5: Run; prove gate 10b discriminates**

Mutate: remove the claim-analysis block from `reconcile_cancelled_jobs` only (leave the hook change in). The reconcile test must FAIL with the row still `running`. Restore, confirm PASS.

- [ ] **Step 6: Full gates and commit**

```bash
git add src/call_manager/proposal_assist/jobs_retirement.py tests/proposal_assist/
git commit -F .git/COMMIT_BRIEF   # "feat(retirement): cancel claim-analysis on the jobs hook and its reconcile"
```

---

### Task 10: An abort callback inside the semantic-audit loop

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/audit.py` (the per-evidence-row loop beginning at line 584)
- Modify: `src/call_manager/proposal_assist/analysis/pipeline.py:327-335` (`analyze_job` signature) and its call site at line 564
- Modify: `tests/proposal_assist/test_claim_analysis_retirement.py`

**Interfaces:**
- Produces: `analyze_job(..., should_abort: Callable[[], bool] | None = None)`.

**Why a callback and not one checkpoint.** One check per job is **not** "bounded by one LLM call": `validate_citations_semantic` iterates evidence rows making a per-row judgement (`citations/audit.py:584`). A single pre-flight check leaves a job with 40 evidence rows running 40 LLM calls after the workspace is gone. The callback must be threaded into that loop.

**Liveness checks run in short independent sessions**, never the worker's per-job session — the same row-lock reason as Task 9.

- [ ] **Step 1: Write the failing test**

```python
def test_the_audit_loop_aborts_partway_when_the_workspace_is_deleted(
    pg_session_factory, current_user
):
    """Gate 10. With 5 judgeable evidence rows and a should_abort that flips True
    after the first row, the judge must be called ONCE — not 5 times.

    One check per JOB would pass a weaker version of this test while still
    spending 5 LLM calls after the delete. The count is the whole assertion.
    """
    from call_manager.proposal_assist.citations.audit import validate_citations_semantic

    calls: list[str] = []

    class _CountingService:
        def judge(self, **kwargs):
            calls.append(kwargs.get("ev_id", "?"))
            return {"verdict": "supported", "confidence": 0.9}

    ledger = {
        "evidence": [
            {"id": f"ev{i}", "citekey": f"k{i}", "stance": "supports"} for i in range(5)
        ]
    }
    proposal_text = " ".join(f"[[ev:ev{i}]]" for i in range(5))

    # True from the SECOND consultation onward: row 1 is judged, then we abort.
    consulted: list[int] = []

    def _should_abort() -> bool:
        consulted.append(1)
        return len(consulted) > 1

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.commit()
        ws_id = ws.id

        validate_citations_semantic(
            proposal_text=proposal_text,
            ledger=ledger,
            session=s,
            session_factory=pg_session_factory,
            workspace_id=ws_id,
            citation_service=_CountingService(),
            deterministic_issues=[],
            should_abort=_should_abort,
        )

    assert len(calls) == 1, (
        f"judged {len(calls)} rows after the abort flipped — the callback is not "
        "inside the per-row loop"
    )
```

**Note on the ledger shape:** read `validate_citations_semantic`'s existing tests before writing this, and copy their evidence-row and `proposal_text` shape exactly. A row that `_SKIP_TYPES` filters out, or one with no live marker, is skipped before the judge — which would make the count 0 and the test green for the wrong reason. Verify you get 5 judge calls with `should_abort=None` FIRST; only then add the abort.

- [ ] **Step 2: Run to verify it fails** — the judge is called 5 times.

- [ ] **Step 3: Thread the callback through**

Add the parameter to `validate_citations_semantic` and check it at the **top of the per-row loop**:

```python
    for row in evidence:
        # Cooperative abort: claim-analysis is sync code in a thread pool with no
        # cancellation primitive, so this callback is the ONLY way a deleted
        # workspace stops the remaining per-row LLM calls. Checked per row, not
        # per job — a job with 40 evidence rows would otherwise keep spending
        # after the delete.
        if should_abort is not None and should_abort():
            logger.info(
                "claim-analysis semantic audit aborted mid-loop (workspace deleted)",
                extra={"event": "claim_analysis_aborted", "workspace_id": str(workspace_id)},
            )
            break
        if not isinstance(row, dict) or not row.get("id"):
            continue
```

Pass it down from `analyze_job`, and construct it at the `run_claim_analysis` call site so it reads liveness in a **short independent session**:

```python
            def _should_abort(wid=job.workspace_id) -> bool:
                # A SHORT INDEPENDENT session — never the worker's per-job session,
                # whose row lock is held for the whole transaction.
                with session_factory() as probe:
                    return probe.scalar(
                        select(Workspace.soft_deleted_at).where(Workspace.id == wid)
                    ) is not None
```

- [ ] **Step 4: Run, prove it discriminates (remove the `break`), restore, gates, commit**

```bash
git add src/call_manager/proposal_assist/citations/audit.py \
        src/call_manager/proposal_assist/analysis/pipeline.py tests/proposal_assist/
git commit -F .git/COMMIT_BRIEF   # "feat(analysis): abort the semantic audit loop for a deleted workspace"
```

---

### Task 11: Never resurrect a cancelled job, and prove the route stays fast

**Files:**
- Modify: `src/call_manager/proposal_assist/analysis/queue.py:216-230` (`mark_failed`)
- Modify: `src/call_manager/proposal_assist/analysis/pipeline.py:578-586` (the generic handler)
- Modify: `tests/proposal_assist/test_claim_analysis_retirement.py`

**Interfaces:** none new.

**The resurrection bug.** `mark_failed` returns a job to `queued` whenever `attempts < MAX_ATTEMPTS`, so the generic handler at `pipeline.py:578` would requeue a job the deletion just cancelled — and with Task 8's filter that row would then sit `queued` forever, un-claimable and never terminal.

**Gate 14 is the most valuable test in this plan.** A bounded delete *transaction* is not the same as a bounded *route*. It measures end-to-end DELETE latency with a worker mid-job holding its row lock, as wall-clock on the route. Its mutation — dropping `SKIP LOCKED` from the cancel — is the one that would have caught the unbounded wait a review round identified.

- [ ] **Step 1: Write both failing tests**

```python
def test_mark_failed_does_not_requeue_a_job_whose_workspace_is_deleted(
    pg_session_factory, current_user
):
    """A cancelled job must go terminal, not back to 'queued' — where Task 8's
    claim filter would leave it stranded, un-claimable, forever.

    attempts is deliberately set to 1 (well under MAX_ATTEMPTS) so the requeue
    branch is the one that WOULD run. Setting it at the cap would make this test
    pass without the fix.
    """
    from call_manager.db.models import ClaimAnalysisJob
    from call_manager.proposal_assist.analysis.queue import mark_failed

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        ws.soft_deleted_at = _now_utc()
        job = ClaimAnalysisJob(
            workspace_id=ws.id, section_sha="a", status="running", attempts=1
        )
        s.add(job)
        s.commit()
        job_id = job.id

    with pg_session_factory() as s:
        job = s.get(ClaimAnalysisJob, job_id)
        mark_failed(s, job, error="workspace deleted mid-job")
        s.commit()

    with pg_session_factory() as s:
        assert s.get(ClaimAnalysisJob, job_id).status == "failed"


def test_the_delete_route_returns_within_the_barrier_with_a_worker_mid_job(
    client_authed_owner, pg_session_factory, current_user
):
    """Gate 14, measured as ROUTE wall-clock.

    Mutation: drop skip_locked from cancel_claim_analysis_jobs_for_workspace and
    this must fail — the hook would then wait on the worker's row lock, which is
    held for the worker's WHOLE per-job transaction.
    """
    from call_manager.db.models import ClaimAnalysisJob

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, with_call=True)
        s.flush()
        job = ClaimAnalysisJob(workspace_id=ws.id, section_sha="a", status="running")
        s.add(job)
        s.commit()
        job_id, ws_id = job.id, ws.id

    holder = pg_session_factory()
    try:
        holder.query(ClaimAnalysisJob).filter_by(id=job_id).with_for_update().one()

        began = time.monotonic()
        resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
        elapsed = time.monotonic() - began

        assert resp.status_code == 204
        assert elapsed < 6.0, (
            f"DELETE took {elapsed:.1f}s with a claim-analysis worker holding its row"
        )
    finally:
        holder.rollback()
        holder.close()
```

- [ ] **Step 2: Run to verify both fail.**

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

- [ ] **Step 4: Run both; prove gate 14 discriminates**

Mutate: remove `skip_locked=True` from `cancel_claim_analysis_jobs_for_workspace`.
Expected: the latency test **FAILS** (the route blocks on the held row).
Restore, confirm PASS. Record both runs — this is the plan's headline gate.

- [ ] **Step 5: Final full gates and commit**

```bash
.venv/bin/python -m pytest tests/proposal_assist/ -q
.venv/bin/ruff check .
git add src/call_manager/proposal_assist/analysis/queue.py \
        src/call_manager/proposal_assist/analysis/pipeline.py tests/proposal_assist/
git commit -F .git/COMMIT_BRIEF   # "fix(analysis): a cancelled job goes terminal, and DELETE stays bounded"
```

---

## Spec coverage

| spec section | task |
|---|---|
| §4.1 registry, optional adapter, bind-at-entry | 1 |
| §4.2 one entry per live task, concurrent retirement | 1, 5 |
| §4.3 the 5.5s bound, concurrent await | 1 |
| §4.4 unconfirmed transfer on EVERY exit | 1, 6 |
| §4.5 wrap points in all four runners | 3, 4, 5 |
| §4.6 cancellation reaches the finally (no runner change) | 3 (verified, not changed) |
| §4.7 tick-boundary CancelledError | 7 |
| §4.8 coordinator wiring and budget | 2 |
| §5 pre-launch prefix guards | 3, 4 |
| §5.1 review's child prefix | 5 |
| §6 claim-analysis: filter, post-commit cancel, callback, failure path | 8, 9, 10, 11 |
| §6.1 what post-commit means; tick convergence | 9 |
| §12 gates 1-10b, 12, 14 | as annotated per task |
| §8, §9 (routes, foot-gun) | **Slice C2 — not this plan** |

**Deliberately not covered here**, per spec §10: teardown completion is not process death (gate 3 encodes it as a residual, not a fix); claim-analysis cannot be cancelled mid-call; a row held by a live worker is skipped until the tick retries. These are documented residuals, and the tests must encode them as such rather than pretending they are closed.
