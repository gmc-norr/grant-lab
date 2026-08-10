# Workspace Retirement — Slice B-retire (coordinator, turns tombstone, collab retirement, service split) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a workspace is deleted, one in-process coordinator fans out — concurrently and in isolation — to a **turns** hook (a prompt in-process tombstone that stops any *new* subprocess launch from installing, **and** best-effort terminates an already-live one instead of waiting for the 60s idle sweep) and a **collab** hook (tear down both artifact rooms, close every editor with a policy-violation 1008, drain in-flight ops, never checkpoint) — bounded to ~5s and bridged from the sync delete route, which now runs **DB commit → retire → scrub → 204** in that exact order.

**Architecture:** A neutral module `proposal_assist/retirement.py` holds the coordinator (`WorkspaceRetirement`, required hooks validated at construction = a startup invariant) and the sync→loop bridge (`signal_workspace_retirement`, modelled byte-for-byte on `signal_user_revocation`). The turns hook is `SubprocessRegistry.retire_workspace`, which adds the workspace to an in-process `_retired` set that `claim_launch_or_wait` now rejects **first** and `fulfill_pending_launch` now honours by refusing to install (returning `bool`), then pops any live entry and tears it down through the existing `_teardown_entry`. The collab hook is `RoomManager.retire_workspace`, which tombstones the workspace in `_retired_ws` (rejected by `acquire` and `operate`, re-checked after every out-of-lock `await load()`), pops both rooms, flips `room.evicted` **under `room.lock`** so it linearizes against in-flight client persists, closes clients 1008, and drains `room.ops`. Cold hydration is guarded separately, at the source, by `acquire_commit_mutex_live` inside `load_or_hydrate` — it commits update #1 during `acquire`'s out-of-lock `await load()`, where neither the room tombstone nor the `ops` drain can see it. The delete route is split into `soft_delete_workspace_db` (through the commit) and `scrub_workspace_bytes` (the FS blocks), with a thin `soft_delete_workspace` wrapper retained for existing callers.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, asyncio (`run_coroutine_threadsafe` bridge; `pycrdt` rooms), pytest (`@pytest.mark.asyncio` for drafting/turns/coordinator, **anyio** for `tests/proposal_assist/collab/`), Starlette `TestClient.websocket_connect` for the WS close-code test. No new dependencies.

## Global Constraints

- **Backend-only. No Alembic migration. No frontend change. No `schema.gen.ts` regeneration.** The final gate asserts the diff touches none of them.
- This plan is **B-retire**, the second half of spec Slice B. It implements spec sections **B1, B2, B3, B5**. **B4 (the live-workspace commit mutex) is already merged (194d8ce) — do NOT re-plan it.** Slice A is already merged (7b16547).
- **Reuse the existing shared exceptions — do NOT define new ones for the same concept.** `WorkspaceRetired` (`drafting/contract.py:122`, `.workspace_id`) is the in-process retirement tombstone, raised by both the turns registry and the collab manager. `WorkspaceDeleted` (`drafting/locking.py`) is B4's *mutex* refusal (HTTP 404) — a different thing; keep them apart.
- **Explicitly NOT claimed by this slice** (do not write code comments, docstrings, or test names asserting otherwise): that any subprocess is *proven stopped* — B2 terminates a live entry **best-effort** and is a linearization point only for *install*, never for an in-flight streaming `client.query`; that **all** writers are covered — `settings.py:69` (`update_settings`) and `settings.py:97` (`add_member`) never take the mutex and remain an accepted, documented Slice-C residual.
- **Collab write coverage, stated precisely** (this slice closes two gaps the first review found, and leaves one bounded residual): server ops via `RoomManager.operate` are covered durably by B4; client writes via `Room.handle`/`_persist` are covered by flipping `room.evicted` under `room.lock`; cold-hydration seeds are covered race-free by `acquire_commit_mutex_live` inside `load_or_hydrate`. **Residual:** if the `room.lock` acquisition exceeds `_RETIRE_LOCK_WAIT_SECONDS`, retirement sets the flag unsynchronized and proceeds — a client persist already inside the lock at that moment can still commit its `YjsUpdate` row after the 204. That row is orphaned, not corrupting: no git write, no cloud spend, on a workspace that is already 404 to every reader. Log it; do not claim it cannot happen.
- Backend tests: `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider` — grouped directories, **never** the whole suite (testcontainers Postgres drops connections under whole-suite load; those failures are infrastructural). Final sweep includes `tests/test_api_spec.py`.
- `tests/test_scheduler_single_instance.py` MUST be run **isolated** — a known nested-lifespan bug in `tests/proposal_assist/activity/test_web.py:190` poisons it when they share a process.
- Test-framework marker per directory: `tests/proposal_assist/collab/` uses **anyio** (`@pytest.mark.anyio` + the file-local `anyio_backend` fixture returning `"asyncio"`, e.g. `tests/proposal_assist/collab/test_collab_room.py:12`); everything else under `tests/proposal_assist/` for drafting/turns/coordinator uses **`@pytest.mark.asyncio`**. Getting this wrong makes tests silently not run.
- Lint: `uv run ruff check .` (line-length 100). **CI runs `ruff check .` only — never `ruff format`.**
- Frontend gate: `cd frontend && pnpm exec tsc --noEmit` (there is NO TS linter; tsc is the gate). Expected to be a no-op for this plan.
- **Explicit-path `git add` only — never `git add -A`.** Merges/pushes are a separate human step; this plan does neither.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- All git commands use `git -C /home/parlar_ai/dev/call-monitor`.

## Codebase facts (verified against HEAD `194d8ce`)

Use these exact names/forms; do not guess.

| Fact | Reality |
|---|---|
| Bridge to mirror | `signal_user_revocation` (`collab/permissions.py:226`): `loop = getattr(request.app.state, "app_loop", None)`; return early if `loop is None`; build `coro`; `fut = asyncio.run_coroutine_threadsafe(coro, loop)` inside a try that `coro.close()`es before re-raising; `fut.result(timeout=...)` catching `concurrent.futures.TimeoutError` → logged warning; **outer `except Exception` degrades the whole thing to a logged warning** so a mutation route never fails because the signal could not be scheduled. `_REVOCATION_BARRIER_SECONDS = 5.0` (`permissions.py:35`) is the ~5s precedent |
| Delete route | `web/api/workspaces.py:1488` `soft_delete_workspace_api` — a **sync def** (threadpool). Already has a `CommitMutexTimeout → 429 + Retry-After: 2` handler (`:1508`, added by B4). Calls `signal_from_request(request, ..., artifacts=["draft","spec"])` at `:1518`, then `Response(204)` |
| `signal_from_request` | imported at `web/api/workspaces.py:58`; **also used at `:715` and `:785`** — so the B-retire change removes only the CALL at `:1518`, NOT the import |
| `soft_delete_workspace` | `workspaces.py:83`, `(session, *, workspace_id, workspaces_dir, timeout_seconds=10) -> None`. Body order: `session.get(Workspace)` → `ValueError` if None → `acquire_commit_mutex_live` → stamp `soft_deleted_at` → 4 `cancel_*_jobs_for_workspace` → cancel QUEUED turns → terminalize RUNNING turns (`usage_unknown=True`) → **`session.commit()` at `:179`** → 2 best-effort FS scrub blocks (`AttachmentStorage(...).delete_workspace`, `ReferenceStorage(...).delete_workspace`, each `except OSError` + warning) at `:180-199`. Existing test callers: `tests/test_workspaces_service.py`, `tests/proposal_assist/test_turns_processor.py:749/:797` |
| `RoomManager` | `collab/room.py:443`. `self._rooms: dict[RoomKey, Room]`, `self._lock: asyncio.Lock`, `self._keylocks: dict[RoomKey, asyncio.Lock]`. `RoomKey = (workspace_id, artifact)` (`:29`). `MANAGER` module-global at `:613` |
| `acquire` | `room.py:456`, `(key, *, load, client, state) -> Room`. Fast path under `self._lock`; cold path under per-key lock; **`await load()` at `:484` runs OUTSIDE the manager lock** — the straddle window; publish `self._rooms[key] = room` under `self._lock` at `:486-488` |
| `operate` | `room.py:491`, asynccontextmanager. Same shape; bumps `room.ops`; **`await load()` at `:517`** is the straddle `kernel_accept.py:71` rides. `finally` (`:524-533`) does `room.ops -= 1` and pops the room only `if not room.clients and room.ops == 0 and self._rooms.get(key) is room` — a room already popped by retirement is **NOT** double-popped |
| `Room` | `room.py:73`. `self.ops = 0` (`:90`, in-flight server-op refcount), `self.evicted = False` (`:92`, tombstone), `self.clients: dict` (`:89`, client→ClientState), `self._lock` (`:93`), `self._recon_lock` (`:111`). Properties: `lock` (`:389`), `recon_lock` (`:393`) |
| `ClientState` | `room.py:61`; `can_write: bool` is per-client and **mutable** (`:69`). Retirement flips `state.can_write = False` per client, exactly as `revoke_user_sockets` does |
| `drop_client` | `room.py:577`, deregisters a client WITHOUT checkpointing (retirement's semantic). `release` (`:545`) is the checkpointing counterpart — NOT used by retirement |
| `evict` | `room.py:597`: pops the room, sets `room.evicted = True`, closes clients with `_WS_INTERNAL_ERROR` (**1011**). Retirement must close with **1008**, not 1011 |
| Close-code constants | `room.py:37` has only `_WS_INTERNAL_ERROR = 1011`; **there is no `_WS_POLICY_VIOLATION` in room.py yet** — this plan adds it (`= 1008`). `permissions.py:27` already has `_WS_POLICY_VIOLATION = 1008` |
| `collab_ws.py` | `web/collab_ws.py`. `_WS_POLICY_VIOLATION = 1008` (`:38`), `_WS_INTERNAL_ERROR = 1011` (`:40`). `await websocket.accept()` at `:87`; `room = await MANAGER.acquire(...)` at `:117`; the route's `try:` opens at `:118` (AFTER acquire) — a raise from `acquire` currently escapes to Starlette as 1011/1006. `contextlib` is already imported (used `:141`, `:172`) |
| `SubprocessRegistry` | `drafting/registry.py:35`. `__init__` builds `self._entries` (`:70`), `self._pending_launches` (`:74`), `self._lock` (`:75`). `claim_launch_or_wait` (`:81`): under `self._lock`, checks cached `_entries` FIRST (`:120`), then `_pending_launches` (`:128`), then capacity. `fulfill_pending_launch` (`:248`) currently `-> None`, always installs `_entries[ws] = _Entry(...)` and `set_result(handle)`. `fail_pending_launch` (`:272`) pops the pending future and `set_exception`. `_teardown_entry` (`:217`) terminates handle + releases lock + closes session, NEVER raises |
| `fulfill_pending_launch` callers | `controller.launch` (`:180`) is the only production caller. Test callers (`tests/proposal_assist/test_drafting_registry*.py`, `test_drafting_controller.py`) ignore the return value → changing `-> None` to `-> bool` is backward-compatible |
| `WorkspaceController` | `drafting/controller.py`. Registry held as `self._registry` (`:56`) — **private, no public accessor today**; this plan adds a `registry` property. `launch` (`:105`): Slice A's `if self._workspace_is_deleted(...): raise WorkspaceRetired` is at `:111` (above `claim_launch_or_wait` at `:114`). The launcher tracks `handed_off`; on any `except BaseException` (`:187`) with `handed_off is False` it terminates the handle, releases the lock, closes the session, and calls `fail_pending_launch`. Today: `fulfill_pending_launch(...)` then `handed_off = True` (`:180-185`) |
| `WorkspaceRetired` | already exists at `drafting/contract.py:122`, `(workspace_id)`, `.workspace_id`. `contract.py` imports only `uuid`, `collections.abc`, `dataclasses`, `typing` — no `call_manager` imports — so `collab/room.py` importing it creates no cycle |
| `main.py` wiring | `workspace_controller = build_workspace_controller(...)` then `app.state.workspace_controller = workspace_controller` at `:173` (inside the lifespan). `app.state.app_loop = _asyncio.get_running_loop()` at `:239`. main.py already uses function-local imports inside the lifespan (e.g. `build_workspace_controller` at `:165`) — a local import there is the module's convention |
| `build_workspace_controller` | `drafting/factory.py:22` — builds `SubprocessRegistry` (`:39`) and returns only the `WorkspaceController` (`:50`). The registry is reachable only via the controller |
| Collab test harness | `tests/proposal_assist/collab/test_collab_room.py`: `RoomManager()`, `mgr.acquire(key, load=..., client=FakeClient(), state=ClientState(user_id=.., can_write=..))`; `FakeClient.closed` records the close code (`:21/:27`). `test_collab_ws_route.py`: `client.websocket_connect(url)`, asserts `WebSocketDisconnect` `.code == 1008`/`1011` |
| Honest-scope gate | `scripts/honest_scope_gate.sh` already exists (added by B4): reads stdin, `exit 1` on `all writers|every writer|cannot write after|no writes after|proves.*stopped|fully prevents`. Covers both B-retire overclaims ("subprocess proven stopped" → `proves.*stopped`; "all writers" → `all writers`). **Reuse unchanged** |

## Design decisions already made (encode; do not re-open)

1. **Neutral module** `proposal_assist/retirement.py` holds coordinator + bridge, so `turns` and `collab` never import each other; hooks are injected at wiring time.
2. **`REQUIRED_HOOKS = ("turns", "collab")`** validated in `WorkspaceRetirement.__init__` → `MissingRetirementHook` at construction (the spec's startup invariant: the app fails to boot rather than silently not retiring).
3. `retire(workspace_id)` fans out over **all** registered hooks (not just required — Slice C may add a `passes` hook) **concurrently** via `asyncio.gather`, each in its own try/except → logged warning. `retire` never raises.
4. **Hook return values are ignored** (deliberate deviation from the spec's "hooks return `None`"): `RoomManager.retire_workspace` returns an `int` count (testability, mirroring `revoke_user_sockets -> int`); the coordinator discards it.
5. **`registry._retired` is an unbounded in-process `set[UUID]`.** Intentional: the DURABLE floor is `soft_deleted_at` (checked by `controller.launch`), so `_retired` is only a promptness optimisation; pruning it would be safe. Growth is one UUID per workspace deleted per process lifetime — a Slice-C eviction candidate, not built now (YAGNI).
6. **Collab retirement does NOT checkpoint** — `drop_client`-style deregistration, not `release`'s last-leave checkpoint: the workspace is soft-deleted, so a checkpoint would write to a dead workspace, and B4 would refuse it under the mutex anyway.
7. **Three different collab write paths need three different guards** — `room.ops` covers only one of them (review finding). (a) **Server ops** (`RoomManager.operate`: kernel accepts, checkpoints) — drain `room.ops` to zero under a bounded deadline, then **log + proceed**, because B4's under-mutex re-read of `soft_deleted_at` is the durable backstop for these. (b) **Client writes** (`Room.handle` → `collab_ws._persist` → `store.append_update(commit=True)`) — take **no** mutex and are invisible to `ops`, so retirement flips `room.evicted` **under `room.lock`**, the same lock `handle` holds across its persist; that is a real linearization point, not a poll. (c) **Cold hydration** (`load_or_hydrate` → seed + commit, run outside the manager lock during `acquire`'s `await load()`) — guarded at the source with `acquire_commit_mutex_live`, which is race-free against the delete transaction. Do NOT describe B4 as the backstop for (b) or (c); it is not.
8. **`can_write = False` before closing**, set under `room.recon_lock` (parity with `revoke_user_sockets`), NOT under `room.lock` (which `Room.handle` holds — risks a lock-order inversion).
9. **Route ordering is the whole contract:** DB commit (durable) → `signal_workspace_retirement` (bounded, in-process) → `scrub_workspace_bytes` → **204**. `CommitMutexTimeout` → 429 having committed nothing (unchanged from B4). Replace the `signal_from_request` CALL at `:1518` (rooms are gone, so permission reconciliation is subsumed) but KEEP the import.

## File Structure

| File | Role |
|---|---|
| `src/call_manager/proposal_assist/retirement.py` | **Create** — `WorkspaceRetirement`, `MissingRetirementHook`, `signal_workspace_retirement`, `REQUIRED_HOOKS`, `_RETIREMENT_BARRIER_SECONDS` |
| `src/call_manager/proposal_assist/drafting/registry.py` | Modify — `self._retired`; `retire_workspace`; `claim_launch_or_wait` rejects retired FIRST; `fulfill_pending_launch -> bool` |
| `src/call_manager/proposal_assist/drafting/controller.py` | Modify — honour `fulfill_pending_launch`'s bool; add public `registry` property |
| `src/call_manager/proposal_assist/collab/room.py` | Modify — `_WS_POLICY_VIOLATION = 1008`; `_RETIRE_OPS_DRAIN_SECONDS`; `_RETIRE_LOCK_WAIT_SECONDS`; `self._retired_ws`; `acquire`/`operate` guards; `_set_evicted_under_lock`; `retire_workspace` |
| `src/call_manager/proposal_assist/collab/hydrate.py` | Modify — `load_or_hydrate` takes `acquire_commit_mutex_live` before persisting the seed (raises; never returns `[]` on refusal) |
| `src/call_manager/proposal_assist/web/collab_ws.py` | Modify — wrap `MANAGER.acquire` → map `WorkspaceRetired` to `close(1008)` + return |
| `src/call_manager/workspaces.py` | Modify — split into `soft_delete_workspace_db` + `scrub_workspace_bytes`; thin `soft_delete_workspace` wrapper retained |
| `src/call_manager/web/api/workspaces.py` | Modify — route runs DB → signal → scrub → 204; `signal_from_request` CALL at `:1518` replaced (import kept) |
| `src/call_manager/main.py` | Modify — build + wire `WorkspaceRetirement` right after `:173` |
| `tests/proposal_assist/test_retirement_coordinator.py` | **Create** — coordinator concurrency/isolation/startup-invariant + separate-thread bridge |
| `tests/proposal_assist/test_drafting_retire_tombstone.py` | **Create** — turns tombstone: claim rejects, fulfill refuses+tears down, retire_workspace marks |
| `tests/proposal_assist/collab/test_collab_retire.py` | **Create** — rooms evicted 1008, no checkpoint, ops drained, acquire+operate reject incl. straddle |
| `tests/proposal_assist/collab/test_collab_ws_retire_route.py` | **Create** — retired `acquire` surfaces as WS close **1008** (not 1011/1006) |
| `tests/test_workspaces_service.py` | Extend — split composes to the old behaviour (behaviour-preserving) |
| `tests/proposal_assist/collab/test_collab_retire_mutex.py` | Extend — hydration refuses to seed a deleted workspace (raises, persists nothing) |
| `tests/proposal_assist/test_workspaces_delete_route.py` | Extend — 204 success; **DB→retire→scrub order asserted directly**; 429 contended nothing committed; no NEW dispatch after delete + row terminal. **Also repairs** the pre-existing `test_delete_returns_429_when_the_commit_mutex_is_held`, whose monkeypatch target this slice deletes |

---

### Task 0: Cut the build branch

**Files:** none (git only).

- [ ] **Step 1: Verify master + expected tree**

```bash
git -C /home/parlar_ai/dev/call-monitor branch --show-current
git -C /home/parlar_ai/dev/call-monitor status --porcelain
```
Expected: `master`; the only untracked entries are pre-existing `docs/superpowers/...` files (`notes/2026-06-24-...`, `specs/2026-06-28-...`, `specs/2026-07-07-...`) plus this plan. Do NOT add the pre-existing docs files.

- [ ] **Step 2: Cut the branch**

```bash
git -C /home/parlar_ai/dev/call-monitor checkout -b workspace-retirement-retire
```

---

### Task 1: coordinator + sync→loop bridge (spec B1)

**Files:**
- Create: `src/call_manager/proposal_assist/retirement.py`
- Test: `tests/proposal_assist/test_retirement_coordinator.py` (create)

**Interfaces:**
- Produces:
  - `REQUIRED_HOOKS = ("turns", "collab")`
  - `class MissingRetirementHook(RuntimeError)`
  - `class WorkspaceRetirement`; `__init__(self, hooks: dict[str, Callable[[uuid.UUID], Awaitable[object]]])` raises `MissingRetirementHook` if any `REQUIRED_HOOKS` name is absent; `async def retire(self, workspace_id: uuid.UUID) -> None` — fans out over ALL hooks concurrently+isolated, never raises, ignores return values.
  - `def signal_workspace_retirement(request, *, workspace_id: uuid.UUID) -> None` — sync bridge; schedules `retire` on `request.app.state.app_loop`, blocks up to `_RETIREMENT_BARRIER_SECONDS`, never raises.
  - `_RETIREMENT_BARRIER_SECONDS = 5.0`

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_retirement_coordinator.py`:

```python
"""WorkspaceRetirement coordinator + signal_workspace_retirement bridge (B1).

The coordinator fans out to injected async hooks concurrently and in isolation;
a missing REQUIRED hook is a startup invariant (raise at construction). The
bridge reaches the coordinator from the SYNC delete route via the app loop and
is bounded so a hanging hook can never wedge the route.
"""

import asyncio
import threading
import uuid

import pytest

from call_manager.proposal_assist import retirement
from call_manager.proposal_assist.retirement import (
    MissingRetirementHook,
    WorkspaceRetirement,
    signal_workspace_retirement,
)


def _noop_hook():
    async def _h(_ws):
        return None
    return _h


def test_missing_required_hook_raises_at_construction():
    with pytest.raises(MissingRetirementHook):
        WorkspaceRetirement(hooks={"turns": _noop_hook()})  # 'collab' missing
    with pytest.raises(MissingRetirementHook):
        WorkspaceRetirement(hooks={})                        # both missing


@pytest.mark.asyncio
async def test_hooks_run_concurrently_and_in_isolation():
    order: list[str] = []
    a_can_finish = asyncio.Event()

    async def hook_a(_ws):
        order.append("a-start")
        # If the hooks ran sequentially (a before b) this would deadlock, since
        # only hook_b sets the event — reaching "a-done" proves they overlap.
        await asyncio.wait_for(a_can_finish.wait(), timeout=5)
        order.append("a-done")

    async def hook_b(_ws):
        order.append("b-start")
        a_can_finish.set()          # release hook_a → concurrency
        raise ValueError("boom")    # isolation: must NOT stop hook_a

    coord = WorkspaceRetirement(hooks={"turns": hook_a, "collab": hook_b})
    await coord.retire(uuid.uuid4())   # must NOT raise despite hook_b

    assert "a-done" in order            # hook_a completed even though hook_b raised
    assert "b-start" in order


@pytest.mark.asyncio
async def test_return_values_are_ignored():
    async def turns_hook(_ws):
        return None
    async def collab_hook(_ws):
        return 3                       # int count (mirrors revoke_user_sockets)
    coord = WorkspaceRetirement(hooks={"turns": turns_hook, "collab": collab_hook})
    assert await coord.retire(uuid.uuid4()) is None


class _FakeState:
    pass


class _FakeApp:
    def __init__(self, *, loop, coordinator):
        self.state = _FakeState()
        self.state.app_loop = loop
        self.state.workspace_retirement = coordinator


class _FakeRequest:
    def __init__(self, app):
        self.app = app


def _loop_on_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


def _stop_loop(loop, t):
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def test_bridge_runs_hooks_on_a_separate_thread_loop():
    """The sync route reaches the coordinator via run_coroutine_threadsafe on the
    app loop, which lives on a DIFFERENT thread (the event loop) than the route
    worker (a threadpool thread)."""
    seen: dict = {}
    ran = threading.Event()

    async def turns_hook(ws):
        seen["turns"] = ws
    async def collab_hook(ws):
        seen["collab"] = ws
        ran.set()

    coord = WorkspaceRetirement(hooks={"turns": turns_hook, "collab": collab_hook})
    loop, t = _loop_on_thread()
    try:
        ws = uuid.uuid4()
        req = _FakeRequest(_FakeApp(loop=loop, coordinator=coord))
        signal_workspace_retirement(req, workspace_id=ws)   # blocks until retire returns
        assert ran.wait(timeout=5)
        assert seen == {"turns": ws, "collab": ws}
    finally:
        _stop_loop(loop, t)


def test_bridge_returns_at_the_bound_when_a_hook_hangs(monkeypatch):
    """A hook that never returns must not wedge the route: the barrier times out
    and the bridge degrades to a logged warning and RETURNS."""
    monkeypatch.setattr(retirement, "_RETIREMENT_BARRIER_SECONDS", 0.2)
    started = threading.Event()

    async def turns_hook(_ws):
        started.set()
        await asyncio.sleep(3600)      # hang well past the bound
    async def collab_hook(_ws):
        return None

    coord = WorkspaceRetirement(hooks={"turns": turns_hook, "collab": collab_hook})
    loop, t = _loop_on_thread()
    try:
        req = _FakeRequest(_FakeApp(loop=loop, coordinator=coord))
        signal_workspace_retirement(req, workspace_id=uuid.uuid4())  # must return ~0.2s
        assert started.wait(timeout=5)           # the hook really started
    finally:
        _stop_loop(loop, t)


def test_bridge_never_raises_without_a_loop():
    """No app_loop / no coordinator → logged no-op, never an exception into the route."""
    class _Empty:
        pass
    app = _Empty()
    app.state = _FakeState()
    app.state.app_loop = None
    app.state.workspace_retirement = None
    signal_workspace_retirement(_FakeRequest(app), workspace_id=uuid.uuid4())  # no raise
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_retirement_coordinator.py -p no:cacheprovider -q
```
Expected: FAIL — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.retirement'`.

- [ ] **Step 3: Implement**

Create `src/call_manager/proposal_assist/retirement.py`:

```python
"""Workspace-retirement coordinator + sync→loop bridge (Slice B-retire).

A neutral home so the turns registry and the collab RoomManager never import
each other: the app wires each subsystem's teardown as a hook at startup
(main.py), and this coordinator fans out to all of them when a workspace is
deleted. The bridge mirrors collab.permissions.signal_user_revocation — the
SYNC delete route reaches the async coordinator via the app loop, bounded so a
hanging hook can never wedge the route.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# A retirement hook: given a workspace_id, tear that subsystem's live state down.
# Return value is IGNORED (RoomManager.retire_workspace returns an int count for
# testability; the coordinator discards it).
RetirementHook = Callable[[uuid.UUID], Awaitable[object]]

# Hooks that MUST be registered or the app must not boot: a missing one would
# mean a delete silently fails to stop that subsystem. Enforced at construction.
REQUIRED_HOOKS = ("turns", "collab")

# Upper bound on how long the sync delete route blocks on the retirement barrier
# (mirrors permissions._REVOCATION_BARRIER_SECONDS). On overrun the route returns;
# retirement continues on the loop and B4's under-mutex guard is the durable
# backstop for any writer still in flight.
_RETIREMENT_BARRIER_SECONDS = 5.0


class MissingRetirementHook(RuntimeError):
    """A required retirement hook was not registered at construction — a STARTUP
    invariant violation, raised so the app fails to boot rather than silently
    skipping a subsystem's teardown on every delete."""


class WorkspaceRetirement:
    """Fans a workspace deletion out to every registered subsystem hook.

    Construction validates REQUIRED_HOOKS; retire() runs all hooks concurrently
    and in isolation and never raises."""

    def __init__(self, hooks: dict[str, RetirementHook]) -> None:
        missing = [name for name in REQUIRED_HOOKS if name not in hooks]
        if missing:
            raise MissingRetirementHook(
                f"missing required retirement hook(s): {', '.join(missing)}"
            )
        self._hooks = dict(hooks)

    async def retire(self, workspace_id: uuid.UUID) -> None:
        """Run EVERY registered hook concurrently, each isolated in its own
        try/except so one hook's failure never suppresses another's teardown.
        Never raises; return values are discarded."""

        async def _run(name: str, hook: RetirementHook) -> None:
            try:
                await hook(workspace_id)
            except Exception:
                logger.warning(
                    "retirement.hook_failed",
                    extra={
                        "event": "retirement_hook_failed",
                        "hook": name,
                        "workspace_id": str(workspace_id),
                    },
                    exc_info=True,
                )

        await asyncio.gather(*(_run(name, hook) for name, hook in self._hooks.items()))


def signal_workspace_retirement(request, *, workspace_id: uuid.UUID) -> None:
    """Route-side (SYNC): schedule WorkspaceRetirement.retire on the app loop and
    BLOCK (bounded) until it returns. Mirrors signal_user_revocation. NEVER raises
    into the route: a missing loop/coordinator, a schedule failure, a timeout, or
    a coordinator error all degrade to a logged warning — the DB soft-delete has
    already committed, so a delete must not fail because retirement could not be
    scheduled."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        coordinator = getattr(request.app.state, "workspace_retirement", None)
        if loop is None or coordinator is None:
            return
        coro = coordinator.retire(workspace_id)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()   # never-awaited coroutine → close it before re-raising
            raise
        try:
            fut.result(timeout=_RETIREMENT_BARRIER_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "retirement.barrier_timeout",
                extra={
                    "event": "retirement_barrier_timeout",
                    "workspace_id": str(workspace_id),
                },
            )   # retirement continues on the loop; degrade to eventual
    except Exception:
        logger.warning(
            "retirement.signal_failed",
            extra={
                "event": "retirement_signal_failed",
                "workspace_id": str(workspace_id),
            },
            exc_info=True,
        )
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_retirement_coordinator.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/retirement.py \
       tests/proposal_assist/test_retirement_coordinator.py
```
Expected: 6 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/retirement.py \
  tests/proposal_assist/test_retirement_coordinator.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(retirement): coordinator + sync->loop bridge

WorkspaceRetirement validates REQUIRED_HOOKS at construction (a startup
invariant), then fans a delete out to every hook concurrently and in isolation —
one hook's failure never suppresses another's. signal_workspace_retirement is
the bounded sync->loop bridge that reaches it from the sync delete route,
modelled on signal_user_revocation; it never raises into the route.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: turns hook — prompt in-process tombstone (spec B2)

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/registry.py` (`__init__` `:47-75`, `claim_launch_or_wait` `:81`, `fulfill_pending_launch` `:248`)
- Modify: `src/call_manager/proposal_assist/drafting/controller.py` (`launch` `:180-185`; add `registry` property near `:56`)
- Test: `tests/proposal_assist/test_drafting_retire_tombstone.py` (create)

**Interfaces:**
- Consumes: `WorkspaceRetired` (`drafting/contract.py:122`).
- Produces:
  - `SubprocessRegistry._retired: set[uuid.UUID]` (in-process tombstone).
  - `async def SubprocessRegistry.retire_workspace(self, workspace_id: uuid.UUID) -> None` — the turns hook. TWO effects: (a) adds to `_retired` under `self._lock` so no NEW launch installs; (b) pops an already-live `_entries` row and tears it down best-effort via the existing `_teardown_entry`, so a running subprocess is interrupted inside the ~5s barrier instead of at the next idle sweep. Idempotent; never raises.
  - `claim_launch_or_wait` raises `WorkspaceRetired(workspace_id)` as its **first** action under `self._lock`, before the cached/pending/capacity checks.
  - `async def SubprocessRegistry.fulfill_pending_launch(..., *, lock_session) -> bool` — `True` installed; `False` if retired (does NOT install; resolves any waiter with `WorkspaceRetired`).
  - `WorkspaceController.registry` property → `self._registry`.
  - `controller.launch`: raises `WorkspaceRetired` when `fulfill_pending_launch` returns `False`, **before** `handed_off = True`, so the existing `except BaseException` tears the fresh handle down.

**Honest scope (copy into a comment on `retire_workspace`):** `_retired` is a linearization point for *install*, not for an in-flight streaming `client.query` — deletion can commit between any precheck and the outbound query, so nothing here proves a subprocess died. The teardown of a live entry is **best-effort**, exactly the strength the design grants (design line 19: `prompt interrupt→terminate (B2), hard-bounded (A4) | Best-effort`). Slice A's reconciliation sweep remains the retry for anything this misses; what changes is worst-case subprocess lifetime, from one sweep cadence (`WORKSPACE_IDLE_SWEEP_SECONDS`, default **60**) down to the retirement barrier. Do NOT write "proven stopped" or "all writers" anywhere — `scripts/honest_scope_gate.sh` fails the build on both.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_drafting_retire_tombstone.py` (drafting convention: `@pytest.mark.asyncio`; reuse this suite's `FakeAgentAdapter` / `SubprocessRegistry` / `WorkspaceController` construction — grep `tests/proposal_assist/test_drafting_controller.py` and `test_drafting_registry.py` for the exact imports and mirror them):

```python
"""Turns tombstone (B2): a retired workspace cannot install a subprocess entry.

_retired is the in-process linearization point for INSTALL. It does NOT prove a
running subprocess stopped — that is Slice A's reconciliation sweep.
"""

import uuid

import pytest

from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.drafting.registry import SubprocessRegistry


@pytest.mark.asyncio
async def test_retire_workspace_makes_claim_reject_before_the_cached_entry():
    """Two effects of retire_workspace on a LIVE entry: (a) claim_launch_or_wait
    rejects the workspace as its FIRST check, so a cached _entries handle is never
    handed back; (b) that entry is torn down rather than left running."""
    adapter = FakeAgentAdapter()
    reg = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    ws = uuid.uuid4()

    # Install a cached entry the normal way (launcher path). The registry stores
    # the handle opaquely in _Entry and never dereferences it here, so a sentinel
    # object() is a valid stand-in (mirrors test_drafting_registry.py, which passes
    # a throwaway handle to fulfill_pending_launch).
    am_launcher, pending = await reg.claim_launch_or_wait(ws)
    assert am_launcher is True and pending is None
    handle = object()
    installed = await reg.fulfill_pending_launch(ws, handle, lock_session=None)
    assert installed is True

    await reg.retire_workspace(ws)

    # (b) the live entry is POPPED AND TORN DOWN, not merely shadowed by the
    # tombstone. FakeAgentAdapter records terminations in `adapter.terminations`
    # (NOT `terminated` — verified against test_drafting_registry.py:102 and
    # test_drafting_registry_capacity.py:69).
    assert adapter.terminations == [handle]
    with pytest.raises(WorkspaceRetired):
        await reg.claim_launch_or_wait(ws)     # tombstone still refuses


@pytest.mark.asyncio
async def test_retire_workspace_tears_down_a_live_entry_mid_turn():
    """Teardown must not spare a BUSY entry: a running turn on a deleted workspace
    is exactly the target (mirrors reconcile_deleted_entries, unlike sweep_idle
    which deliberately skips in_flight > 0)."""
    adapter = FakeAgentAdapter()
    reg = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    ws = uuid.uuid4()

    await reg.claim_launch_or_wait(ws)
    handle = object()
    assert await reg.fulfill_pending_launch(ws, handle, lock_session=None) is True
    # Mark the entry mid-turn the way the suite already does — grep
    # test_drafting_registry.py for how in_flight is incremented (a checkout/turn
    # helper, not direct attribute poking, if one exists) and mirror it.
    reg._entries[ws].in_flight = 1

    await reg.retire_workspace(ws)

    assert adapter.terminations == [handle]                # torn down anyway
    assert ws not in reg._entries


@pytest.mark.asyncio
async def test_fulfill_pending_launch_refuses_to_install_and_resolves_a_real_waiter():
    """A launch in flight when retirement lands must NOT install, and must resolve
    the COALESCED WAITER with WorkspaceRetired (never hang)."""
    reg = SubprocessRegistry(adapter=FakeAgentAdapter(), idle_seconds=600)
    ws = uuid.uuid4()

    am_launcher, _ = await reg.claim_launch_or_wait(ws)   # launcher installs a pending future
    assert am_launcher is True

    # A SECOND caller coalesces onto that pending future — this is the waiter the
    # refusal has to resolve. Without it the test proves nothing about waiters.
    am_launcher2, waiter = await reg.claim_launch_or_wait(ws)
    assert am_launcher2 is False and waiter is not None

    await reg.retire_workspace(ws)                        # retire lands mid-launch

    installed = await reg.fulfill_pending_launch(ws, object(), lock_session=None)
    assert installed is False

    # The waiter raises rather than hanging or receiving a handle we discarded.
    with pytest.raises(WorkspaceRetired):
        await waiter

    # No live entry was installed.
    with pytest.raises(WorkspaceRetired):
        await reg.claim_launch_or_wait(ws)


@pytest.mark.asyncio
async def test_retire_workspace_is_idempotent():
    reg = SubprocessRegistry(adapter=FakeAgentAdapter(), idle_seconds=600)
    ws = uuid.uuid4()
    await reg.retire_workspace(ws)
    await reg.retire_workspace(ws)                        # no raise
    with pytest.raises(WorkspaceRetired):
        await reg.claim_launch_or_wait(ws)


class _BlockingLaunchAdapter(FakeAgentAdapter):
    """Pauses INSIDE launch() so retirement can land while the launcher still holds
    the pending future. That is the only window in which fulfill_pending_launch's
    refusal is reachable: retiring BEFORE controller.launch would make
    claim_launch_or_wait raise first (it is now the very first check), the adapter
    would never be called, and the launch/terminate assertions could not hold."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def launch(self, context):
        self.entered.set()
        await self.release.wait()
        return await super().launch(context)


@pytest.mark.asyncio
async def test_controller_launch_tears_down_the_fresh_handle_when_install_refused(
    pg_session_factory, tmp_path
):
    """The hand-off: fulfill returns False, so controller.launch raises
    WorkspaceRetired BEFORE handed_off=True and its existing except terminates the
    just-launched handle (leak-free)."""
    from call_manager.proposal_assist.drafting.controller import WorkspaceController
    from tests.proposal_assist.conftest import make_workspace

    adapter = _BlockingLaunchAdapter()
    registry = SubprocessRegistry(adapter=adapter, idle_seconds=600)
    controller = WorkspaceController(
        registry=registry, session_factory=pg_session_factory, workspaces_dir=tmp_path
    )
    with pg_session_factory() as s:
        ws = make_workspace(s)                            # NOT soft-deleted (so the
        s.commit()                                        # :111 precheck passes)
        ws_id = ws.id

    task = asyncio.create_task(controller.launch(ws_id))
    # The claim succeeded (not yet retired) and we are now parked inside
    # adapter.launch, holding the pending future.
    await asyncio.wait_for(adapter.entered.wait(), timeout=5)

    await registry.retire_workspace(ws_id)                # retire lands MID-LAUNCH
    adapter.release.set()                                 # resume into fulfill_...

    with pytest.raises(WorkspaceRetired):
        await asyncio.wait_for(task, timeout=5)

    # The fresh handle was launched then torn down; none is live, none leaked.
    # NOTE: `launches` records LaunchContext objects and `terminations` records
    # AgentHandles, so they are NOT comparable to each other — assert counts and
    # the handle's workspace_id instead.
    assert len(adapter.launches) == 1                     # launch was attempted
    assert len(adapter.terminations) == 1                 # ...and terminated
    assert adapter.terminations[0].workspace_id == ws_id
    with pytest.raises(WorkspaceRetired):
        await registry.claim_launch_or_wait(ws_id)        # nothing installed
```

Add `import asyncio` to this test file's imports (the blocking adapter and `create_task`/`wait_for` need it).

**Implementer note on the handle sentinels:** `object()` is used as the handle wherever the registry only stores it (`fulfill_pending_launch`) and never dereferences it — confirm against `test_drafting_registry.py:58` (`test_fulfill_pending_launch_resolves_future_and_installs_entry`) that this is how the suite already exercises that method, and match its form. `FakeAgentAdapter`'s recorders are **`launches` (LaunchContext) / `terminations` (AgentHandle) / `turns`** — verified against `fake_adapter.py:33-35`; there is no `terminated` attribute, so do not write one.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_retire_tombstone.py -p no:cacheprovider -q
```
Expected: FAIL — `SubprocessRegistry` has no `retire_workspace`; `claim_launch_or_wait` does not reject; `fulfill_pending_launch` returns `None` not `bool`.

- [ ] **Step 3a: Add the tombstone set + `retire_workspace`**

In `registry.py` `__init__`, next to `self._pending_launches` (`:74`):

```python
        # In-process retirement tombstone (Slice B-retire). Unbounded set of
        # workspace_ids: the DURABLE no-relaunch floor is soft_deleted_at (checked
        # by controller.launch above claim_launch_or_wait), so this is only a
        # PROMPTNESS optimisation and pruning it would be safe. Growth is one UUID
        # per workspace deleted per process lifetime (a Slice-C eviction candidate).
        # A linearization point for INSTALL, not for an in-flight streaming query.
        self._retired: set[uuid.UUID] = set()
```

Add the hook method (place it near `reconcile_deleted_entries`, `:181`):

```python
    async def retire_workspace(self, workspace_id: uuid.UUID) -> None:
        """Turns retirement hook (coordinator-invoked). Two effects, in order:

        1. Tombstone the workspace in _retired so no NEW launch can install:
           claim_launch_or_wait rejects it first, and an in-flight
           fulfill_pending_launch refuses to install.
        2. Pop an already-live entry and tear it down best-effort, so a running
           subprocess is interrupted now rather than at the next idle sweep
           (WORKSPACE_IDLE_SWEEP_SECONDS, default 60). This is the design's
           "prompt interrupt->terminate (B2) ... Best-effort" guarantee.

        Idempotent (a second call finds no entry). Never raises: _teardown_entry
        swallows and logs both the adapter.terminate and the lock-release paths.

        Honest scope: a linearization point for INSTALL. Deletion can commit
        between any precheck and an in-flight streaming client.query, so this
        does NOT prove the subprocess died — Slice A's reconciliation sweep
        remains the retry.
        """
        async with self._lock:
            self._retired.add(workspace_id)
            entry = self._entries.pop(workspace_id, None)
        if entry is not None:
            # Popped REGARDLESS of in_flight — a running turn on a deleted
            # workspace is exactly the target (mirrors reconcile_deleted_entries
            # :181, which the idle sweep deliberately does not do). Teardown runs
            # OUTSIDE self._lock: it awaits adapter.terminate (hard-bounded by
            # Slice A) and closes the lock_session, and holding the registry lock
            # across it would stall every other claim for that duration.
            await self._teardown_entry(workspace_id, entry, context="retire-hook")
```

**Why pop-then-teardown, and why outside the lock:** this mirrors `reconcile_deleted_entries` (`:181-199`) exactly — pop under `self._lock`, `_teardown_entry` outside it — so the ordering (adapter terminate FIRST, then advisory-lock release + session close) is identical everywhere. Adapter-level termination alone cannot release the advisory lock; `_teardown_entry` is the only path that does both. Popping also frees the capacity slot immediately, and Slice A's `_unconfirmed_generations()` accounting keeps counting the dying generation until the adapter confirms it, so capacity cannot be over-subscribed by this pop.

- [ ] **Step 3b: Reject a retired workspace FIRST in `claim_launch_or_wait`**

In `claim_launch_or_wait`, as the very first statement inside `async with self._lock:` (before the cached-entry check at `:120`):

```python
        async with self._lock:
            # Retirement tombstone (B-retire): refuse a retired workspace BEFORE the
            # cached-entry check, or a cached handle would be handed straight back
            # for a workspace that is being deleted. Nothing has been mutated yet,
            # so raising here is clean.
            if workspace_id in self._retired:
                raise WorkspaceRetired(workspace_id)
            # Already cached — wrap the handle in a resolved future.
            existing = self._entries.get(workspace_id)
            ...
```

Add `WorkspaceRetired` to the existing `from call_manager.proposal_assist.drafting.contract import (...)` block at `:14`.

- [ ] **Step 3c: Make `fulfill_pending_launch` refuse to install when retired**

Rewrite `fulfill_pending_launch` (`:248-270`) to return `bool`:

```python
    async def fulfill_pending_launch(
        self,
        workspace_id: uuid.UUID,
        handle: AgentHandle,
        *,
        lock_session: Session | None,
    ) -> bool:
        """Called by the launcher on success.  Installs the entry and resolves any
        waiting futures — UNLESS the workspace was retired while this launch was in
        flight, in which case it installs nothing and resolves the waiter with
        WorkspaceRetired.

        Returns True if the entry was installed, False if retirement refused it.
        On False the launcher (controller.launch) raises WorkspaceRetired so its
        own except tears down the fresh handle + releases the lock + closes the
        session (adapter-level teardown alone cannot release the advisory lock).

        If a waiter's future was cancelled before this is called, set_result /
        set_exception is skipped (we check done() first)."""
        async with self._lock:
            future = self._pending_launches.pop(workspace_id, None)
            if workspace_id in self._retired:
                # Retired mid-launch: do NOT install. Resolve the waiter (if any)
                # with WorkspaceRetired so a caller awaiting the pending future
                # raises rather than hanging on a handle we are about to discard.
                if future is not None and not future.done():
                    future.set_exception(WorkspaceRetired(workspace_id))
                return False
            self._entries[workspace_id] = _Entry(
                handle=handle,
                last_touched_monotonic=time.monotonic(),
                lock_session=lock_session,
            )
        if future is not None and not future.done():
            future.set_result(handle)
        return True
```

- [ ] **Step 3d: Honour the bool in `controller.launch` + expose the registry**

In `controller.py`, add a public accessor near `self._registry = registry` (`:56`):

```python
    @property
    def registry(self):
        """The subprocess registry (read-only accessor). Lets startup wiring reach
        registry.retire_workspace for the retirement coordinator without importing
        the registry's concrete class."""
        return self._registry
```

Replace the hand-off at `:180-185`:

```python
            handle = await self._registry._adapter.launch(context)
            installed = await self._registry.fulfill_pending_launch(
                workspace_id,
                handle,
                lock_session=lock_session,
            )
            if not installed:
                # Retired while this launch was in flight: the registry installed
                # nothing (and resolved any waiter with WorkspaceRetired). Raise so
                # the except below terminates the fresh handle, releases the lock,
                # and closes the session — handed_off stays False. fail_pending_launch
                # there is a harmless no-op (fulfill already popped the future).
                raise WorkspaceRetired(workspace_id)
            handed_off = True
```

`WorkspaceRetired` is already imported in `controller.py` (Slice A, used at `:112`).

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_drafting_retire_tombstone.py \
  tests/proposal_assist/test_drafting_registry.py \
  tests/proposal_assist/test_drafting_registry_capacity.py \
  tests/proposal_assist/test_drafting_controller.py \
  -p no:cacheprovider -q && uv run ruff check \
  src/call_manager/proposal_assist/drafting/registry.py \
  src/call_manager/proposal_assist/drafting/controller.py
```
Expected: all pass — existing registry/controller tests ignore the new `fulfill_pending_launch` bool, so they stay green.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/registry.py \
  src/call_manager/proposal_assist/drafting/controller.py \
  tests/proposal_assist/test_drafting_retire_tombstone.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(drafting): turns retirement tombstone (registry._retired)

retire_workspace marks a workspace retired; claim_launch_or_wait rejects it
before the cached-entry check, and fulfill_pending_launch returns bool and
refuses to install a fresh handle for it (resolving any waiter with
WorkspaceRetired). controller.launch honours the bool and raises so its existing
cleanup tears the fresh handle down. A linearization point for install only —
NOT proof a running subprocess stopped (that is Slice A's sweep).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: collab retirement — evict rooms, close 1008, drain ops (spec B3)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (module constants near `:37`; `RoomManager.__init__` `:444`; `acquire` `:456`; `operate` `:491`; add `retire_workspace`)
- Modify: `src/call_manager/proposal_assist/web/collab_ws.py` (wrap `MANAGER.acquire` at `:117`)
- Modify: `src/call_manager/proposal_assist/collab/hydrate.py` (`load_or_hydrate` `:21` — refuse to persist a seed for a deleted workspace)
- Test: `tests/proposal_assist/collab/test_collab_retire.py` (create) — anyio
- Test: `tests/proposal_assist/collab/test_collab_ws_retire_route.py` (create) — the WS 1008 mapping

**Interfaces:**
- Consumes: `WorkspaceRetired` (`drafting/contract.py:122`) — imported into `room.py` (no cycle: `contract.py` imports only stdlib).
- Produces:
  - `RoomManager._retired_ws: set[uuid.UUID]`.
  - `acquire` and `operate` raise `WorkspaceRetired(workspace_id)` for a retired workspace — a cheap unlocked fast-reject at entry AND a re-check under `self._lock` immediately before publishing a freshly-hydrated room (closing the `await load()` straddle).
  - `async def RoomManager.retire_workspace(self, workspace_id: uuid.UUID) -> int` — the collab hook: tombstone in `_retired_ws`; for both `(workspace_id, "draft")` and `(workspace_id, "spec")`: pop the room under `self._lock`, set `room.evicted = True` **under `room.lock`** (the client-write linearization point — see below), flip each client's `can_write = False` and `close(1008)` under `room.recon_lock`, drain `room.ops` under a bounded deadline. Returns the number of sockets closed (discarded by the coordinator). Never checkpoints. Idempotent.
  - `load_or_hydrate` (`hydrate.py:21`) takes `acquire_commit_mutex_live` before persisting a seed, so cold hydration cannot commit update #1 for a deleted workspace.

**Why `room.ops` alone is NOT sufficient (review finding):** `room.ops` is bumped only by `RoomManager.operate`. The two heaviest collab write paths never go through it —

| path | what it commits | covered by `ops`? | covered by B4's mutex? |
|---|---|---|---|
| `collab_ws._persist` (`:103`) → `store.append_update(commit=True)` | a `YjsUpdate` row per client frame | **no** | **no** (takes no mutex) |
| `collab_ws._load` (`:92`) → `load_or_hydrate` → `append_update` + `s.commit()` (`:98`) | the hydration seed, update #1 | **no** (runs outside the manager lock, during `acquire`'s cold-path `await load()`) | **no** |
| `RoomManager.operate` (kernel accepts, checkpoints) | git + DB under the mutex | yes | yes |

So a drain of `ops` to zero can return, the route can answer 204, and either write can still commit afterwards. This slice closes both: the first with `room.lock` (below), the second with the mutex in `load_or_hydrate`. Neither is a new mutex on the hot path.
  - `room.py` constants `_WS_POLICY_VIOLATION = 1008`, `_RETIRE_OPS_DRAIN_SECONDS = 2.0`.
  - `collab_ws.py`: a `WorkspaceRetired` from `MANAGER.acquire` → `await websocket.close(code=_WS_POLICY_VIOLATION)` + `return`, mapped **before** the route's `try:`.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_retire.py` (anyio convention — copy the `anyio_backend` fixture and `FakeClient` from `test_collab_room.py`):

```python
"""Collab retirement (B3): both rooms evicted, clients closed 1008, no checkpoint,
room.ops drained; acquire AND operate reject a retired workspace, including the
straddle where retirement lands during operate's out-of-lock await load()."""

import asyncio
import uuid

import pytest

from call_manager.proposal_assist.collab.room import ClientState, RoomManager
from call_manager.proposal_assist.drafting.contract import WorkspaceRetired


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    def __init__(self):
        self.sent: list[bytes] = []
        self.closed: int | None = None

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def _empty_load():
    return []


@pytest.mark.anyio
async def test_retire_evicts_both_rooms_and_closes_clients_1008():
    mgr = RoomManager()
    ws = uuid.uuid4()
    draft, spec = (ws, "draft"), (ws, "spec")
    cd, cs = FakeClient(), FakeClient()
    await mgr.acquire(draft, load=_empty_load, client=cd,
                      state=ClientState(user_id=1, can_write=True))
    await mgr.acquire(spec, load=_empty_load, client=cs,
                      state=ClientState(user_id=1, can_write=True))

    n = await mgr.retire_workspace(ws)

    assert n == 2
    assert cd.closed == 1008 and cs.closed == 1008   # policy violation, NOT 1011
    assert mgr.get(draft) is None and mgr.get(spec) is None


@pytest.mark.anyio
async def test_retire_does_not_checkpoint(monkeypatch):
    """drop-style teardown: retirement must NEVER call the checkpoint path (the
    workspace is soft-deleted; a checkpoint would write to a dead workspace)."""
    import call_manager.proposal_assist.collab.checkpoint as cp

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("retirement must not checkpoint")

    monkeypatch.setattr(cp, "checkpoint_artifact", _boom)
    mgr = RoomManager()
    ws = uuid.uuid4()
    await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(),
                      state=ClientState(user_id=1, can_write=True))
    await mgr.retire_workspace(ws)
    assert called["n"] == 0


@pytest.mark.anyio
async def test_retire_flips_can_write_false_before_closing():
    mgr = RoomManager()
    ws = uuid.uuid4()
    st = ClientState(user_id=1, can_write=True)
    await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(), state=st)
    await mgr.retire_workspace(ws)
    assert st.can_write is False


@pytest.mark.anyio
async def test_acquire_rejects_a_retired_workspace():
    mgr = RoomManager()
    ws = uuid.uuid4()
    await mgr.retire_workspace(ws)
    with pytest.raises(WorkspaceRetired):
        await mgr.acquire((ws, "draft"), load=_empty_load, client=FakeClient(),
                          state=ClientState(user_id=1, can_write=True))


@pytest.mark.anyio
async def test_operate_rejects_a_retired_workspace():
    mgr = RoomManager()
    ws = uuid.uuid4()
    await mgr.retire_workspace(ws)
    with pytest.raises(WorkspaceRetired):
        async with mgr.operate((ws, "draft"), load=_empty_load):
            pass


@pytest.mark.anyio
async def test_operate_rejects_when_retirement_lands_during_load():
    """The straddle kernel_accept.py:71 rides: retirement commits while operate is
    inside its OUT-OF-LOCK await load(); the room must not be published."""
    mgr = RoomManager()
    ws = uuid.uuid4()
    key = (ws, "draft")
    load_entered = asyncio.Event()
    allow_load_finish = asyncio.Event()

    async def _slow_load():
        load_entered.set()
        await allow_load_finish.wait()       # park inside operate's cold-start load()
        return []

    async def _op():
        async with mgr.operate(key, load=_slow_load) as room:
            return room

    op_task = asyncio.ensure_future(_op())
    await asyncio.wait_for(load_entered.wait(), timeout=5)   # operate is inside load()
    await mgr.retire_workspace(ws)                           # retirement lands mid-load
    allow_load_finish.set()                                  # let load() return

    with pytest.raises(WorkspaceRetired):
        await op_task
    assert mgr.get(key) is None                              # never published


@pytest.mark.anyio
async def test_retire_drains_in_flight_ops():
    """retire_workspace waits for an in-flight operate() to leave before returning
    (room.ops back to 0). The op itself is refused durably by B4's mutex; the drain
    only bounds 'retirement is done'."""
    mgr = RoomManager()
    ws = uuid.uuid4()
    key = (ws, "draft")
    inside = asyncio.Event()
    release = asyncio.Event()

    async def _op():
        async with mgr.operate(key, load=_empty_load) as room:
            inside.set()
            await release.wait()             # hold room.ops == 1

    op_task = asyncio.ensure_future(_op())
    await asyncio.wait_for(inside.wait(), timeout=5)

    async def _retire_then_flag():
        await mgr.retire_workspace(ws)
        return "retired"

    retire_task = asyncio.ensure_future(_retire_then_flag())
    await asyncio.sleep(0.05)
    assert not retire_task.done()            # still draining (ops == 1)
    release.set()                            # op leaves -> ops == 0
    assert await asyncio.wait_for(retire_task, timeout=5) == "retired"
    await op_task


@pytest.mark.anyio
async def test_retire_waits_for_an_in_flight_client_persist_and_stops_later_ones():
    """The `ops` drain does NOT cover client writes: Room.handle persists inside
    room.lock, and room.ops is only bumped by operate(). Retirement flips `evicted`
    under room.lock, so (a) it cannot return while a persist is in flight, and
    (b) every LATER frame drops before apply/persist."""
    mgr = RoomManager()
    ws = uuid.uuid4()
    key = (ws, "draft")

    persisted: list[bytes] = []
    in_persist = asyncio.Event()
    release_persist = asyncio.Event()

    async def _persist(actor_id, update):
        in_persist.set()
        await release_persist.wait()
        persisted.append(update)

    room = await mgr.acquire(
        key, load=_empty_load, client=FakeClient(), state=ClientState(user_id=1, can_write=True)
    )
    # Park a write inside room.lock, mid-persist.
    write_task = asyncio.ensure_future(
        room.handle(sender=FakeClient(), state=ClientState(user_id=1, can_write=True),
                    message=_write_frame(), persist=_persist)
    )
    await asyncio.wait_for(in_persist.wait(), timeout=5)

    retire_task = asyncio.ensure_future(mgr.retire_workspace(ws))
    await asyncio.sleep(0.05)
    assert not retire_task.done()            # blocked on room.lock — the whole point

    release_persist.set()
    await asyncio.wait_for(write_task, timeout=5)
    await asyncio.wait_for(retire_task, timeout=5)
    assert len(persisted) == 1               # the in-flight one completed

    # A LATER frame on the same room object persists nothing: evicted is set.
    await room.handle(
        sender=FakeClient(), state=ClientState(user_id=1, can_write=True),
        message=_write_frame(), persist=_persist,
    )
    assert len(persisted) == 1               # unchanged — dropped before persist
```

`_write_frame()` is a helper this file needs: a valid sync-write frame carrying a
non-empty update. Build it the way `test_collab_ws_route.py:15-22` does
(`Doc()` + `Text()` + `observe` to capture the update, then
`P.create_update_message(...)`) rather than hand-rolling bytes — `Room.handle`
drops anything `P.extract_update` cannot parse, which would make this test pass
vacuously.

**Hydration straddle (DB-backed, different file).** Add to the existing
`tests/proposal_assist/collab/test_collab_retire_mutex.py`, which already imports
`load_or_hydrate` (`:20`) and owns the `_init_repo` / `_delete` helpers:

```python
def test_hydration_refuses_to_seed_a_deleted_workspace(pg_session_factory, tmp_path):
    """Cold hydration commits update #1 outside the manager lock and outside
    room.ops, so retirement's drain cannot see it. The live mutex is what stops
    it — and it must RAISE, never return [] (an empty list is indistinguishable
    from 'no content yet' and would let a kernel accept write to an empty doc)."""
    md0 = "## Background\noriginal.\n"
    with pg_session_factory() as s:
        owner = make_user(s, email="hydrate-del@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid = ws.id
    repo = Path(tmp_path) / str(wid) / "repo"
    _init_repo(repo, md0)
    _delete(pg_session_factory, wid)          # soft-delete BEFORE first hydration

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            load_or_hydrate(
                s, workspace_id=wid, artifact="draft", workspaces_dir=str(tmp_path)
            )
        s.rollback()

    # ...and nothing was persisted.
    with pg_session_factory() as s:
        rows = s.execute(
            select(YjsUpdate).where(YjsUpdate.workspace_id == wid)
        ).scalars().all()
        assert rows == []
```

Create `tests/proposal_assist/collab/test_collab_ws_retire_route.py`.

**There are no `client` / `seeded_collab_workspace` fixtures in this repo** — the
collab route tests build everything inline: `_app` + `pg_session_factory` fixtures,
`TestClient(_app)` entered manually, a session cookie from the file-local `_cookie`
helper, and the workspace seeded with `collab_state.enable_collab`. Mirror
`test_collab_ws_route.py:48-60` (`test_owner_can_connect_and_persist_an_update`)
exactly:

```python
"""A retired acquire surfaces as WS close 1008 (policy), not 1011/1006 (B3)."""

import pytest
from starlette.websockets import WebSocketDisconnect

from call_manager.auth.session_cookie import SessionSigner
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.room import MANAGER
from tests.proposal_assist.conftest import make_user, make_workspace


def _cookie(app, user_id: int) -> str:
    signer: SessionSigner = app.state.session_signer
    return signer.make(user_id=user_id)


def test_retired_acquire_closes_with_1008(_app, pg_session_factory):
    """accept() happens at collab_ws.py:87 and the route's try opens at :118, so a
    raise from MANAGER.acquire would surface as 1011/1006. The route maps
    WorkspaceRetired to close(1008) + return before entering the try."""
    from fastapi.testclient import TestClient

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ws-retire@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    client = TestClient(_app)
    client.__enter__()
    client.cookies.set("session", _cookie(_app, oid))
    # Force the in-process tombstone directly (deterministic; avoids threading the
    # async retire through the sync test client). MANAGER is the same module-global
    # the route uses. Discard in finally so it cannot leak into other tests sharing
    # the process-global MANAGER.
    MANAGER._retired_ws.add(wid)
    try:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
            f"/api/ws/workspaces/{wid}/draft"
        ) as wsconn:
            wsconn.receive_bytes()
        assert exc.value.code == 1008      # NOT 1011, NOT 1006
    finally:
        MANAGER._retired_ws.discard(wid)
        client.__exit__(None, None, None)
```

**Implementer check:** confirm the WS URL prefix against `test_collab_ws_route.py`
(`/api/ws/workspaces/{wid}/draft` at `:40`) rather than trusting this snippet, and
confirm `_app` is the fixture name that file uses.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_retire.py \
  tests/proposal_assist/collab/test_collab_ws_retire_route.py \
  -p no:cacheprovider -q
```
Expected: FAIL — `RoomManager` has no `retire_workspace` / `_retired_ws`; `acquire`/`operate` don't reject; the WS route surfaces 1011.

- [ ] **Step 3a: Constants + tombstone set + import in `room.py`**

Under the existing close-code constant at `room.py:37` (`_WS_INTERNAL_ERROR = 1011`), add:

```python
# RFC 6455 close code: policy violation. Retirement closes editors with THIS, not
# 1011 (server error): a soft-deleted workspace is a policy decision, not a crash.
_WS_POLICY_VIOLATION = 1008

# Bound on how long retire_workspace waits for an in-flight server op (a kernel
# accept mid-write) to leave before it declares a room's retirement done. On
# overrun it logs and proceeds: B4's under-mutex re-read of soft_deleted_at is the
# durable backstop (a straggler op refuses to commit anyway). Applied PER room,
# so both artifacts drain within ~2*this; well inside the 5s retirement barrier.
_RETIRE_OPS_DRAIN_SECONDS = 2.0

# Bound on how long retire_workspace waits for room.lock in order to flip the
# `evicted` tombstone. Room.handle holds that lock across its persist await, so
# in practice this is one DB round-trip. On overrun we set the flag
# unsynchronized (which still stops every LATER frame) and log.
_RETIRE_LOCK_WAIT_SECONDS = 1.0
```

Add the import (module top, after `from . import protocol as P`):

```python
from call_manager.proposal_assist.drafting.contract import WorkspaceRetired
```

In `RoomManager.__init__` (`:444`), after `self._keylocks` (`:451`):

```python
        # Retirement tombstone (Slice B-retire): a soft-deleted workspace's rooms
        # are torn down and no new room may be created for it. acquire/operate
        # reject membership; retire_workspace populates it. Unbounded (one UUID per
        # deleted workspace per process) — a Slice-C eviction candidate.
        self._retired_ws: set[uuid.UUID] = set()
```

- [ ] **Step 3b: Reject in `acquire`**

At the top of `acquire` (before `async with self._lock:` at `:465`) add the cheap fast-reject, and inside the cold-start branch add the post-`load()` re-check under the manager lock. The full method becomes:

```python
    async def acquire(self, key: RoomKey, *, load: LoadFn, client, state: ClientState) -> Room:
        workspace_id = key[0]
        # Fast reject (unlocked read is atomic under the GIL): a retired workspace
        # never gets a room. The authoritative re-check is under self._lock below,
        # before we publish a freshly-hydrated room, so the await load() straddle
        # is covered too.
        if workspace_id in self._retired_ws:
            raise WorkspaceRetired(workspace_id)
        async with self._lock:
            room = self._rooms.get(key)
            if room is not None:
                room.clients[client] = state
                return room
            keylock = self._keylocks.setdefault(key, asyncio.Lock())

        async with keylock:
            async with self._lock:
                room = self._rooms.get(key)
                if room is not None:
                    room.clients[client] = state
            if room is None:
                room = Room(key)
                for update in await load():  # runs OUTSIDE the manager lock
                    room.doc.apply_update(update)
                async with self._lock:
                    # Re-check UNDER the lock, atomically with publishing: retirement
                    # may have committed while we hydrated (the straddle). Refuse
                    # rather than publish a room for a dead workspace. The room was
                    # never in self._rooms, so there is nothing to tear down.
                    if workspace_id in self._retired_ws:
                        raise WorkspaceRetired(workspace_id)
                    self._rooms[key] = room
                    room.clients[client] = state
            return room
```

- [ ] **Step 3c: Reject in `operate`**

Add the same fast-reject at the top of `operate` and the post-`load()` re-check before publishing. The method becomes:

```python
    @asynccontextmanager
    async def operate(self, key: RoomKey, *, load: LoadFn):
        workspace_id = key[0]
        if workspace_id in self._retired_ws:
            raise WorkspaceRetired(workspace_id)
        async with self._lock:
            room = self._rooms.get(key)
            if room is not None:
                room.ops += 1
            else:
                keylock = self._keylocks.setdefault(key, asyncio.Lock())
        if room is None:
            async with keylock:
                async with self._lock:
                    room = self._rooms.get(key)
                    if room is not None:
                        room.ops += 1
                if room is None:
                    room = Room(key)
                    for update in await load():   # OUTSIDE the manager lock
                        room.doc.apply_update(update)
                    async with self._lock:
                        # Straddle re-check (kernel_accept.py:71 rides this load()):
                        # refuse a workspace retired mid-hydrate before publishing.
                        if workspace_id in self._retired_ws:
                            raise WorkspaceRetired(workspace_id)
                        self._rooms[key] = room
                        room.ops += 1
        try:
            yield room
        finally:
            pending = None
            async with self._lock:
                room.ops -= 1
                if not room.clients and room.ops == 0 and self._rooms.get(key) is room:
                    self._rooms.pop(key, None)
                    pending = room._pending_on_evict
                    room._pending_on_evict = None
            if pending is not None:
                await pending(room)   # OUTSIDE the manager lock (mirrors release)
```

(The `try/finally` body is unchanged from the current `:522-533`; it already tolerates a room retirement popped — `self._rooms.get(key) is room` is False, so it never double-pops.)

- [ ] **Step 3d: Add `retire_workspace`**

Add after `evict` (`:597-609`), before the `MANAGER = RoomManager()` global.

First, a module-level helper (place it beside `RoomManager`, not inside it — it
exists only so `asyncio.wait_for` has a coroutine to bound; `async with` cannot be
given a timeout directly):

```python
async def _set_evicted_under_lock(room: Room) -> None:
    """Flip the room tombstone while holding room.lock, so it linearizes against
    Room.handle's write path (which checks `evicted` and awaits persist under that
    same lock)."""
    async with room.lock:
        room.evicted = True
```

Then the hook itself:

```python
    async def retire_workspace(self, workspace_id: uuid.UUID) -> int:
        """Collab retirement hook (coordinator-invoked). Tombstone the workspace so
        acquire/operate refuse it, then tear down BOTH artifact rooms: pop each,
        tombstone it (room.evicted, flipped UNDER room.lock so it linearizes
        against in-flight client persists), close every editor with 1008, and drain
        any in-flight server op. NEVER checkpoints — the workspace is soft-deleted, so a
        last-leave checkpoint would write to a dead workspace (and B4 would refuse
        it under the mutex anyway); this is drop_client semantics for the whole
        room. Idempotent. Returns the number of sockets closed (tests/logging; the
        coordinator discards it)."""
        self._retired_ws.add(workspace_id)
        loop = asyncio.get_running_loop()
        closed = 0
        for artifact in ("draft", "spec"):
            key = (workspace_id, artifact)
            # Pop under the manager lock so no new op/client can attach after this.
            async with self._lock:
                room = self._rooms.pop(key, None)
            if room is None:
                continue
            # Tombstone UNDER room.lock — this is the client-write linearization
            # point, not a cosmetic flag set. Room.handle's write path checks
            # `self.evicted` and then awaits persist(...) INSIDE this same lock
            # (room.py:168-193), and that persist commits a YjsUpdate row with NO
            # commit mutex and NO soft_deleted_at read, so it is invisible to both
            # room.ops and B4. Taking the lock therefore buys two things at once:
            #   (a) when we get it, no client persist is in flight;
            #   (b) every later frame sees evicted and drops before apply/persist.
            # No inversion risk: we released self._lock at the pop above and hold
            # NOTHING while waiting, and Room.handle never waits on a manager lock.
            # Bounded like the ops drain — a wedged DB write must not eat the whole
            # retirement barrier; on timeout we set the flag anyway and log, and
            # the straggler's own commit remains the documented residual.
            try:
                await asyncio.wait_for(
                    _set_evicted_under_lock(room), timeout=_RETIRE_LOCK_WAIT_SECONDS
                )
            except TimeoutError:
                room.evicted = True   # unsynchronized fallback; still stops LATER frames
                logger.warning(
                    "collab.retire_lock_wait_timeout",
                    extra={"event": "collab_retire_lock_wait_timeout", "room": str(key)},
                )
            # Flip can_write + close under recon_lock (parity with
            # revoke_user_sockets), NOT room.lock — Room.handle holds room.lock, so
            # taking it here risks a lock-order inversion. We popped the whole room,
            # so there are no surviving peers and no presence removal is needed.
            async with room.recon_lock:
                for client, state in list(room.clients.items()):
                    state.can_write = False
                    with suppress(Exception):
                        await client.close(code=_WS_POLICY_VIOLATION)
                    closed += 1
            # Drain any in-flight server op: the room is popped so no NEW op can bump
            # room.ops, but one already inside operate() may still be mid-write. Poll
            # ops to zero under a bounded deadline, then proceed regardless (B4's
            # mutex is the durable backstop; overrun is logged, not fatal).
            deadline = loop.time() + _RETIRE_OPS_DRAIN_SECONDS
            while room.ops > 0 and loop.time() < deadline:
                await asyncio.sleep(0.01)
            if room.ops > 0:
                logger.warning(
                    "collab.retire_ops_drain_timeout",
                    extra={"event": "collab_retire_ops_drain_timeout",
                           "room": str(key), "ops": room.ops},
                )
        return closed
```

(`suppress` is already imported in `room.py` — `from contextlib import asynccontextmanager, suppress`, `:22`.)

- [ ] **Step 3e: Map a retired `acquire` to 1008 in `collab_ws.py`**

In `web/collab_ws.py`, wrap the `MANAGER.acquire` call (`:116-117`). Replace:

```python
    state = ClientState(user_id=user.id, can_write=can_write)
    room = await MANAGER.acquire(key, load=_load, client=websocket, state=state)
    try:
```

with:

```python
    state = ClientState(user_id=user.id, can_write=can_write)
    try:
        room = await MANAGER.acquire(key, load=_load, client=websocket, state=state)
    except WorkspaceRetired:
        # The workspace was retired (soft-deleted) between the membership check and
        # room acquisition — or during cold-start hydration. accept() already
        # happened (:87), so a raise would surface as 1011/1006; close with 1008
        # (policy) and return WITHOUT entering the try below (no client registered,
        # so there is nothing to release).
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_POLICY_VIOLATION)
        return
    try:
```

Add `from call_manager.proposal_assist.drafting.contract import WorkspaceRetired` to `collab_ws.py`'s imports. `contextlib` and `_WS_POLICY_VIOLATION` are already in scope (`:38`).

- [ ] **Step 3f: Refuse to persist a hydration seed for a deleted workspace**

The room tombstone cannot cover this one: `acquire`'s cold path runs `await load()`
OUTSIDE the manager lock, and `_load` (`collab_ws.py:92`) commits the seed inside
that window (`s.commit()` at `:98`). By the time the post-`load()` re-check rejects,
update #1 is already durable for a deleted workspace. Guard it at the source, in
`hydrate.py`'s seed branch — the ONLY place hydration persists:

```python
    seed = pm_json_to_ydoc(markdown_to_pm_or_empty(md)).get_update()
    # B-retire: the seed is a DB write on a cold path, so take the SAME lock the
    # delete transaction takes (B4) before committing it. This is race-free, not
    # merely narrowing: acquire_commit_mutex_live re-reads soft_deleted_at under
    # the mutex, and deletion holds that mutex across its own commit. Cost is one
    # mutex acquisition on the FIRST open of a room ever (afterwards load_updates
    # returns rows and we never reach here) — the same shape maybe_auto_enable
    # already pays on the artifact read path.
    acquire_commit_mutex_live(
        session, workspace_id, timeout_seconds=_HYDRATE_MUTEX_TIMEOUT_SECONDS
    )
    store.append_update(
        session, workspace_id=workspace_id, artifact=artifact, actor_id=None, update=seed
    )
    return [seed]
```

Imports for `hydrate.py`:

```python
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex_live

_HYDRATE_MUTEX_TIMEOUT_SECONDS = 5
```

**Do NOT catch either exception here — let both propagate.** The tempting version
swallows `WorkspaceDeleted`/`CommitMutexTimeout` and returns `[]`, and that is a
data-loss bug, not a graceful degrade: `[]` is indistinguishable from "this
artifact has no content yet", so `accept_via_kernel` would build an EMPTY room and
apply a section write to it. Raising is both safer and already the house contract —
`kernel_accept.py` imports `WorkspaceDeleted` at `:50` and handles it, and
`CommitMutexTimeout` is the standard busy path that maps to 429.

**Caller survey (already done — do not re-derive):** `load_or_hydrate` has three
production callers plus the WS route, and every one of them wraps the call in its
OWN short-lived `with session_factory() as s:` that it commits immediately:

| caller | holds the mutex at hydration time? |
|---|---|
| `web/collab_ws.py:95` (`_load`) | no |
| `proposals/kernel_accept.py:65` (`_load` inside `MANAGER.operate`) | no — its mutex is taken later, in `_body`, on a DIFFERENT session |
| `citations/collab_cite.py:64/:117/:169` | no — same shape as kernel_accept |

So the acquisition can neither self-deadlock (no caller holds it yet) nor discard a
caller's staged work (the session is dedicated to the load). **The implementer must
re-verify this table against HEAD before implementing** — a future caller that
hydrates while already holding the mutex on another session would block until
timeout, which is why the guard lives here and not deeper in `store.append_update`.

Because hydration can now raise `WorkspaceDeleted`, extend Step 3e's mapping in
`collab_ws.py` to close 1008 on it too — it arrives by the same route (out of
`MANAGER.acquire`, from inside `await load()`):

```python
    except (WorkspaceRetired, WorkspaceDeleted):
```

with `WorkspaceDeleted` imported from `drafting.locking`. The two mean the same
thing to a socket — the workspace is gone — and differ only in which layer noticed.

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab -p no:cacheprovider -q && uv run ruff check \
  src/call_manager/proposal_assist/collab/room.py \
  src/call_manager/proposal_assist/web/collab_ws.py
```
Expected: all collab tests pass (the new files plus the existing suite — `acquire`/`operate` guards are additive and existing tests never retire).

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/room.py \
  src/call_manager/proposal_assist/web/collab_ws.py \
  tests/proposal_assist/collab/test_collab_retire.py \
  tests/proposal_assist/collab/test_collab_ws_retire_route.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): retire_workspace evicts both rooms, closes editors 1008, drains ops

RoomManager._retired_ws tombstones a deleted workspace: acquire and operate
reject it, re-checked after the out-of-lock await load() (the kernel-accept
straddle). retire_workspace pops both artifact rooms, closes every editor with
1008 (policy, not 1011), flips can_write under recon_lock, and drains room.ops
under a bounded deadline — WITHOUT checkpointing (drop_client semantics). The WS
route maps a retired acquire to close(1008) before its try, so it never leaks as
1011/1006.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: service split — `soft_delete_workspace_db` + `scrub_workspace_bytes` (spec B5, part 1)

**Files:**
- Modify: `src/call_manager/workspaces.py:83-199`
- Test: `tests/test_workspaces_service.py`

**Interfaces:**
- Produces:
  - `def soft_delete_workspace_db(session, *, workspace_id: uuid.UUID, timeout_seconds: int = 10) -> None` — everything from `session.get(Workspace)` through `session.commit()` (the DB half, including B4's mutex acquire and Slice A's running-turn terminalization). Raises `CommitMutexTimeout` / `WorkspaceDeleted` exactly as today.
  - `def scrub_workspace_bytes(workspace_id: uuid.UUID, *, workspaces_dir: Path) -> None` — the two best-effort FS blocks (`AttachmentStorage` + `ReferenceStorage`), each `except OSError` + warning.
  - `def soft_delete_workspace(session, *, workspace_id, workspaces_dir, timeout_seconds=10) -> None` — thin wrapper: `soft_delete_workspace_db(...)` then `scrub_workspace_bytes(...)`. Signature and behaviour byte-for-byte as before, so existing callers (`tests/test_workspaces_service.py`, `tests/proposal_assist/test_turns_processor.py:749/:797`) are untouched.

**Why keep the wrapper:** removing it would churn several test call sites for no behavioural gain. The wrapper *is* the behaviour-preservation contract.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_workspaces_service.py`:

```python
def test_split_halves_compose_to_the_wrapper(pg_session_factory, tmp_path):
    """soft_delete_workspace_db + scrub_workspace_bytes must equal the retained
    soft_delete_workspace wrapper: same soft_deleted_at stamp, same running-turn
    terminalization, same idempotent WorkspaceDeleted-on-redelete. This pins the
    split as behaviour-preserving."""
    from call_manager.db.models import ChatTurn, TurnJob, Workspace
    from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted
    from call_manager.workspaces import (
        scrub_workspace_bytes,
        soft_delete_workspace,
        soft_delete_workspace_db,
    )
    from tests.proposal_assist.conftest import make_chat_turn, make_user, make_workspace

    # (a) the split halves, run explicitly.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=make_user(s, email="split-a@ki.se"))
        ut = make_chat_turn(s, ws, role="user", content={"type": "text", "text": "q"},
                            tokens_in=1234)
        job = TurnJob(workspace_id=ws.id, status="running",
                      user_turn_id=ut.id, submitted_by_user_id=ws.owner_id)
        s.add(job)
        s.commit()
        split_id, job_id, ut_id = ws.id, job.id, ut.id

    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=split_id)     # DB half only
    scrub_workspace_bytes(split_id, workspaces_dir=tmp_path)   # FS half only

    with pg_session_factory() as s:
        assert s.get(Workspace, split_id).soft_deleted_at is not None
        assert s.get(TurnJob, job_id).status != "running"      # terminalized
        assert s.get(ChatTurn, ut_id).tokens_in == 1234        # estimate retained (usage_unknown)
    # DB half is idempotent-refusing exactly like the wrapper.
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            soft_delete_workspace_db(s, workspace_id=split_id)
        s.rollback()

    # (b) the retained wrapper, run whole — same observable end state.
    with pg_session_factory() as s:
        ws2 = make_workspace(s, owner=make_user(s, email="split-b@ki.se"))
        job2 = TurnJob(workspace_id=ws2.id, status="running",
                       user_turn_id=make_chat_turn(
                           s, ws2, role="user", content={"type": "text", "text": "q"}).id,
                       submitted_by_user_id=ws2.owner_id)
        s.add(job2)
        s.commit()
        wrap_id, job2_id = ws2.id, job2.id

    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=wrap_id, workspaces_dir=tmp_path)

    with pg_session_factory() as s:
        assert s.get(Workspace, wrap_id).soft_deleted_at is not None
        assert s.get(TurnJob, job2_id).status != "running"
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_workspaces_service.py -p no:cacheprovider -q -k "split_halves"
```
Expected: FAIL — `cannot import name 'soft_delete_workspace_db'`.

- [ ] **Step 3: Implement the split**

In `workspaces.py`, replace `soft_delete_workspace` (`:83-199`) with three functions. `soft_delete_workspace_db` is the current body through the commit; `scrub_workspace_bytes` is the two FS blocks; `soft_delete_workspace` calls both.

```python
def soft_delete_workspace_db(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    timeout_seconds: int = 10,
) -> None:
    """DB half of soft-delete (Slice B-retire split): mark the workspace deleted,
    cancel every active background job + queued turn, terminalize any RUNNING turn
    row, and COMMIT — all under the per-workspace commit mutex.

    Takes the mutex for the whole transaction, BEFORE any mutation: raises
    CommitMutexTimeout (route -> 429) if a writer is wedged, having committed
    nothing, and WorkspaceDeleted (route -> 404) if already deleted — the
    documented non-idempotent re-delete contract. The caller resolves ownership
    (require_owner) first. FS bytes are scrubbed separately by scrub_workspace_bytes
    AFTER this commits."""
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise ValueError(f"workspace not found: {workspace_id}")
    # _live, not the plain acquire: `ws` was loaded before we could block on the
    # mutex, so a concurrent deletion that committed while we waited would be
    # invisible to it. The fresh under-mutex read raises WorkspaceDeleted instead
    # of re-stamping off a stale instance.
    acquire_commit_mutex_live(session, workspace_id, timeout_seconds=timeout_seconds)
    ws.soft_deleted_at = datetime.now(UTC)
    cancel_coherence_jobs_for_workspace(session, workspace_id=workspace_id)
    cancel_evaluator_jobs_for_workspace(session, workspace_id=workspace_id)
    cancel_review_runs_for_workspace(session, workspace_id=workspace_id)
    cancel_uncited_claims_jobs_for_workspace(session, workspace_id=workspace_id)
    queued = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "queued",
        )
    ).all()
    for row in queued:
        record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="cancelled",
            expected_status="queued",
            raw_error="workspace soft-deleted",
            emit_chat=False,
        )
    # Terminalize any RUNNING turn in THIS transaction; usage_unknown=True keeps the
    # submit-time estimate (see Slice A). The processor's success-finalize then
    # contends on the same row and becomes a no-op.
    running = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "running",
        )
    ).all()
    for row in running:
        record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="cancelled",
            expected_status="running",
            raw_error="workspace soft-deleted",
            emit_chat=False,
            usage_unknown=True,
        )
    session.commit()


def scrub_workspace_bytes(workspace_id: uuid.UUID, *, workspaces_dir: Path) -> None:
    """FS half of soft-delete (Slice B-retire split): best-effort scrub of
    attachment + reference bytes. DB rows are intentionally preserved (audit
    trail); only on-disk bytes go. Run AFTER soft_delete_workspace_db commits — a
    scrub failure leaves the workspace soft-deleted with harmless orphan files a
    future sweep collects."""
    try:
        AttachmentStorage(root=workspaces_dir).delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete_workspace: attachment cleanup failed for workspace %s; "
            "files will be collected by a future sweep",
            workspace_id,
            exc_info=True,
        )
    try:
        ReferenceStorage(root=workspaces_dir).delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete_workspace: reference cleanup failed for workspace %s; "
            "files will be collected by a future sweep",
            workspace_id,
            exc_info=True,
        )


def soft_delete_workspace(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    workspaces_dir: Path,
    timeout_seconds: int = 10,
) -> None:
    """Thin wrapper retained for existing callers: DB half then FS half, exactly
    the pre-split behaviour. The delete route now calls the two halves directly so
    it can interleave in-process retirement between them (Slice B-retire)."""
    soft_delete_workspace_db(session, workspace_id=workspace_id, timeout_seconds=timeout_seconds)
    scrub_workspace_bytes(workspace_id, workspaces_dir=workspaces_dir)
```

Keep the module's existing docstring and imports; no import changes are needed (all names already imported at `:24-37`).

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_workspaces_service.py \
  tests/proposal_assist/test_turns_processor.py \
  -p no:cacheprovider -q && uv run ruff check src/call_manager/workspaces.py
```
Expected: all pass — the wrapper preserves every existing caller's behaviour.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/workspaces.py tests/test_workspaces_service.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
refactor(workspaces): split soft_delete into DB + FS halves

soft_delete_workspace_db is everything through the commit (mutex acquire,
soft_deleted_at, job/turn cancellation, running-turn terminalization);
scrub_workspace_bytes is the two best-effort FS blocks. A thin soft_delete_workspace
wrapper is retained byte-for-byte for existing callers; a composition test pins
the split as behaviour-preserving. The delete route (next task) calls the halves
directly to interleave in-process retirement between them.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: wire the coordinator + reorder the delete route (spec B5, part 2)

**Files:**
- Modify: `src/call_manager/main.py` (right after `:173`)
- Modify: `src/call_manager/web/api/workspaces.py:1500-1519` (`soft_delete_workspace_api`)
- Test: `tests/proposal_assist/test_workspaces_delete_route.py` (extend)

**Interfaces:**
- Consumes: `WorkspaceRetirement` + `signal_workspace_retirement` (Task 1); `registry.retire_workspace` (Task 2); `MANAGER.retire_workspace` (Task 3); `soft_delete_workspace_db` + `scrub_workspace_bytes` (Task 4).
- Produces:
  - `main.py`: `app.state.workspace_retirement = WorkspaceRetirement(hooks={"turns": workspace_controller.registry.retire_workspace, "collab": MANAGER.retire_workspace})`, built inside the lifespan right after the controller is stored — so a missing hook fails startup (the invariant).
  - The delete route runs **DB commit → `signal_workspace_retirement` → `scrub_workspace_bytes` → 204**, with the `CommitMutexTimeout → 429` handler unchanged and the `signal_from_request` CALL at `:1518` removed (import kept for `:715`/`:785`).

- [ ] **Step 1: Write the failing tests**

**First, repair the test this task BREAKS.** `tests/proposal_assist/test_workspaces_delete_route.py:16-26` currently monkeypatches `ws_api.soft_delete_workspace` to pin a 1s mutex timeout — and Step 3b deletes exactly that name from the route module's imports, so `monkeypatch.setattr` will fail with `AttributeError` before the test body runs. Update the existing `test_delete_returns_429_when_the_commit_mutex_is_held` to patch the DB half instead:

```python
    monkeypatch.setattr(
        ws_api,
        "soft_delete_workspace_db",
        functools.partial(ws_api.soft_delete_workspace_db, timeout_seconds=1),
    )
```

Everything else in that test is unchanged, and its assertions must all survive — the new `test_delete_returns_429_when_mutex_held_and_commits_nothing` below is a superset (it adds "no retirement ran"), so folding the two into one is acceptable ONLY if every assertion from the original is carried over.

Then extend `tests/proposal_assist/test_workspaces_delete_route.py` (this file exists from B-mutex; reuse its `client_authed_owner` / `current_user` / `pg_session_factory` fixtures):

```python
def test_delete_returns_204_and_retires_then_scrubs(
    client_authed_owner, current_user, pg_session_factory
):
    """End state of a successful delete: 204, the row is soft-deleted, and the
    retirement bridge actually ran (the workspace is tombstoned in the
    process-global room manager). ORDERING is asserted separately, by
    test_delete_runs_db_then_retire_then_scrub_in_that_order below."""
    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.collab.room import MANAGER

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None      # DB committed
    # signal_workspace_retirement ran the collab hook, which tombstones the ws.
    assert ws_id in MANAGER._retired_ws
    MANAGER._retired_ws.discard(ws_id)                                  # don't leak the global


def test_delete_runs_db_then_retire_then_scrub_in_that_order(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """Route ORDER is the whole B5 contract (design: "Route ordering is the whole
    contract"), so assert it directly instead of inferring it from end state. Wrap
    all three route-module calls; each probe asserts the invariant that must
    ALREADY hold by the time it runs."""
    import call_manager.web.api.workspaces as ws_api
    from call_manager.db.models import Workspace

    calls: list[str] = []
    real_db = ws_api.soft_delete_workspace_db
    real_signal = ws_api.signal_workspace_retirement
    real_scrub = ws_api.scrub_workspace_bytes

    def _db(session, **kw):
        calls.append("db")
        return real_db(session, **kw)

    def _signal(request, *, workspace_id):
        calls.append("retire")
        # Strongest available assertion: the DB half is not merely "called first",
        # it is COMMITTED and visible to an INDEPENDENT session by now. A route that
        # retired inside the still-open delete transaction would fail here.
        with pg_session_factory() as s2:
            assert s2.get(Workspace, workspace_id).soft_deleted_at is not None
        return real_signal(request, workspace_id=workspace_id)

    def _scrub(workspace_id, **kw):
        calls.append("scrub")
        assert "retire" in calls          # bytes are never scrubbed before retirement
        return real_scrub(workspace_id, **kw)

    monkeypatch.setattr(ws_api, "soft_delete_workspace_db", _db)
    monkeypatch.setattr(ws_api, "signal_workspace_retirement", _signal)
    monkeypatch.setattr(ws_api, "scrub_workspace_bytes", _scrub)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    assert calls == ["db", "retire", "scrub"]
    MANAGER._retired_ws.discard(ws_id)

# NOTE: all three names are patched ON THE ROUTE MODULE (ws_api.<name>), not on
# their defining modules — the route imported them by value at module load, so
# patching call_manager.workspaces.scrub_workspace_bytes would not affect it.


def test_delete_returns_429_when_mutex_held_and_commits_nothing(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """Unchanged B4 contract, re-asserted for the reordered route: a wedged writer
    makes DELETE 429 having committed nothing and having run NO retirement/scrub."""
    import functools
    import threading

    import call_manager.web.api.workspaces as ws_api
    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.collab.room import MANAGER
    from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex

    # Pin a SHORT mutex timeout for this test only. Without it the route waits the
    # production default (10s, locking.py:144) on every run. The patch target is
    # the DB half now — the route no longer calls the soft_delete_workspace wrapper.
    monkeypatch.setattr(
        ws_api,
        "soft_delete_workspace_db",
        functools.partial(ws_api.soft_delete_workspace_db, timeout_seconds=1),
    )

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    try:
        holding.wait(timeout=5)
        resp = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
    finally:
        release.set()
        t.join(timeout=30)

    assert resp.status_code == 429 and resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None          # nothing committed
    assert ws_id not in MANAGER._retired_ws                             # no retirement ran


def test_no_new_turn_dispatches_after_delete_and_row_is_terminal(
    client_authed_owner, current_user, pg_session_factory, tmp_path
):
    """Spec test 14: after deletion no NEW turn dispatches and the turn row is
    terminal. We do NOT assert 'no send after the response' — that contradicts the
    conceded in-flight query race the design documents."""
    from call_manager.db.models import TurnJob
    from tests.proposal_assist.conftest import make_chat_turn   # conftest.py:247

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        job = TurnJob(
            workspace_id=ws.id, status="running",
            user_turn_id=make_chat_turn(
                s, ws, role="user", content={"type": "text", "text": "q"}).id,
            submitted_by_user_id=ws.owner_id,
        )
        s.add(job)
        s.commit()
        ws_id, job_id = ws.id, job.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204

    from call_manager.db.models import Workspace

    with pg_session_factory() as s:
        assert s.get(TurnJob, job_id).status != "running"     # terminalized in the delete txn
        assert s.get(Workspace, ws_id).soft_deleted_at is not None

    # No NEW turn dispatches: assert the exact SYNC gate controller.launch consults
    # before it would dispatch (Slice A's soft_deleted_at floor). This is the
    # observable "no new dispatch" without driving async launch from a sync test,
    # and it deliberately says NOTHING about an in-flight client.query (conceded race).
    controller = client_authed_owner.app.state.workspace_controller
    assert controller._workspace_is_deleted(ws_id) is True
```

**Implementer note:** confirm `client_authed_owner.app` exposes `.state.workspace_controller` in this suite's fixture; if the fixture wraps the app differently, reach the controller the same way the neighbouring route tests do. Do NOT assert anything about an in-flight `client.query` — that is the conceded race the design documents.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_workspaces_delete_route.py -p no:cacheprovider -q \
  -k "retires_then_scrubs or no_new_turn"
```
Expected: FAIL — `MANAGER._retired_ws` is not populated after DELETE (the route still calls `signal_from_request`, not `signal_workspace_retirement`; the coordinator is not wired).

- [ ] **Step 3a: Wire the coordinator in `main.py`**

In `main.py`, immediately after `app.state.workspace_controller = workspace_controller` (`:173`):

```python
    # Workspace-retirement coordinator (Slice B-retire). Built here, inside the
    # lifespan, so a missing REQUIRED hook fails STARTUP (the invariant) rather than
    # silently not retiring on delete. Local imports match this block's convention.
    from call_manager.proposal_assist.collab.room import MANAGER as _collab_manager
    from call_manager.proposal_assist.retirement import WorkspaceRetirement

    app.state.workspace_retirement = WorkspaceRetirement(
        hooks={
            "turns": workspace_controller.registry.retire_workspace,
            "collab": _collab_manager.retire_workspace,
        }
    )
```

- [ ] **Step 3b: Reorder the delete route**

In `web/api/workspaces.py`, rewrite `soft_delete_workspace_api`'s body (`:1500-1519`):

```python
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # DB half commits internally (mutex acquire + soft_deleted_at + job/turn
        # cancellation). CommitMutexTimeout -> 429 having committed NOTHING;
        # WorkspaceDeleted (re-delete) -> the app-level 404 handler (B4).
        try:
            soft_delete_workspace_db(session, workspace_id=ws.id)
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429,
                detail=BUSY_ERROR_MESSAGE,
                headers={"Retry-After": "2"},
            ) from e
    # DB soft-delete is durable. Retire in-process (bounded ~5s, never raises):
    # tombstone the turns registry so no new subprocess installs, and tear down both
    # collab rooms (close editors 1008, no checkpoint). This SUBSUMES the old
    # signal_from_request permission reconciliation — the rooms are gone. THEN scrub
    # bytes, THEN 204. Order is the contract (spec B5).
    signal_workspace_retirement(request, workspace_id=workspace_id)
    scrub_workspace_bytes(workspace_id, workspaces_dir=Path(settings.workspaces_dir))
    return Response(status_code=204)
```

Update imports in `web/api/workspaces.py`:
- Replace `soft_delete_workspace` in the `from call_manager.workspaces import (...)` block with `scrub_workspace_bytes, soft_delete_workspace_db` (the route no longer calls the wrapper).
- Add `from call_manager.proposal_assist.retirement import signal_workspace_retirement`.
- **KEEP** `from call_manager.proposal_assist.collab.permissions import signal_from_request` (`:58`) — still used at `:715` and `:785`. Only the CALL at `:1518` is removed.

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_workspaces_delete_route.py \
  tests/test_workspaces_service.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/main.py src/call_manager/web/api/workspaces.py
```
Expected: all pass; ruff clean.

Confirm `signal_from_request` is still referenced (import must not have gone unused):

```bash
cd /home/parlar_ai/dev/call-monitor && grep -n "signal_from_request" src/call_manager/web/api/workspaces.py
```
Expected: the import at `:58` plus the two remaining calls (spec-artifact routes) — and NO call in `soft_delete_workspace_api`.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/main.py src/call_manager/web/api/workspaces.py \
  tests/proposal_assist/test_workspaces_delete_route.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(workspaces): delete route retires in-process then scrubs (DB->retire->scrub->204)

Wire WorkspaceRetirement at startup (missing hook fails boot = the invariant),
with the turns registry and collab manager hooks. The delete route now runs the
DB half, then the bounded in-process retirement bridge (subsumes the old
signal_from_request reconciliation — the rooms are gone), then the FS scrub, then
204. CommitMutexTimeout still -> 429 having committed nothing.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Acceptance gates

**Files:** none (verification only). Any failure → fix on-branch, re-run.

- [ ] **Step 1: Grouped backend sweep**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist tests/test_workspaces_service.py tests/test_api_spec.py \
  -p no:cacheprovider -q
```
Expected: all pass. `connection refused` from testcontainers PG is an infra flake → re-run the flagged file isolated.

- [ ] **Step 2: Files outside the sweep that exercise the changed wiring**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_scheduler.py tests/test_main.py -p no:cacheprovider -q
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_scheduler_single_instance.py -p no:cacheprovider -q
```
`tests/test_main.py` exercises `create_app()` lifespan wiring (where `WorkspaceRetirement` is now constructed). The single-instance test runs **isolated** — a known nested-lifespan bug (`tests/proposal_assist/activity/test_web.py:190`) poisons it when run together.

- [ ] **Step 3: Lint (whole repo, as CI runs it)**

```bash
cd /home/parlar_ai/dev/call-monitor && uv run ruff check .
```
Expected: `All checks passed!`

- [ ] **Step 4: Frontend regression (untouched — must stay green)**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
```
Expected: silent; zero frontend changes, no `schema.gen.ts` diff. **Re-anchor to the repo root** (the second `cd`) — a lingering `cd frontend` makes later relative `git`/`pytest` paths resolve wrongly.

- [ ] **Step 5: Diff-scope check — no migration, no frontend, no schema regen**

```bash
git -C /home/parlar_ai/dev/call-monitor diff --stat master...workspace-retirement-retire
git -C /home/parlar_ai/dev/call-monitor diff --name-only master...workspace-retirement-retire \
  | grep -E 'alembic|migrations|schema\.gen\.ts|frontend/|\.tsx?$' && \
  echo "SCOPE VIOLATION: touched a forbidden path" || echo "OK: backend-only, no migration/frontend/schema"
```
Expected: only the files in the File Structure table; the grep prints `OK: backend-only, ...`.

- [ ] **Step 6: Re-verify the `load_or_hydrate` caller survey against HEAD**

Task 3f's mutex acquisition is safe only because no caller already holds the commit
mutex on another session when it hydrates. That was verified at plan time; re-check
it on the branch, because a caller added since would block until timeout:

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "load_or_hydrate" src/ | grep -v "def load_or_hydrate"
```
Expected: exactly the four production call sites in the plan's table
(`web/collab_ws.py`, `proposals/kernel_accept.py`, `citations/collab_cite.py` ×3).
For any NEW site, confirm it does not already hold `acquire_commit_mutex*` on a
different session before hydrating.

- [ ] **Step 7: Honest-scope gate on the branch's commit messages + diff**

The existing `scripts/honest_scope_gate.sh` already rejects "subprocess proven stopped" (`proves.*stopped`) and "all writers" (`all writers`) — the two B-retire overclaims. Reuse it unchanged.

```bash
cd /home/parlar_ai/dev/call-monitor
git log --format='%B' master..workspace-retirement-retire | ./scripts/honest_scope_gate.sh \
  && echo "OK: commit messages carry no overclaiming language"
git diff master...workspace-retirement-retire -- '*.py' | ./scripts/honest_scope_gate.sh \
  && echo "OK: diff carries no overclaiming language"
```
Expected: both `OK: ...` lines.

- [ ] **Step 8: Prove the gate can still fail (same predicate, known-bad input)**

```bash
cd /home/parlar_ai/dev/call-monitor
if echo '# this proves the subprocess stopped and covers all writers' \
   | ./scripts/honest_scope_gate.sh; then
  echo "BROKEN GATE: accepted known-overclaiming input" >&2; exit 1
fi
if ! echo '# tombstones the workspace so no new launch installs' \
   | ./scripts/honest_scope_gate.sh; then
  echo "BROKEN GATE: rejected known-good input" >&2; exit 1
fi
echo "gate verified: rejects overclaiming input, accepts ordinary input"
```
Expected: `gate verified: rejects overclaiming input, accepts ordinary input`.

- [ ] **Step 9: Every mutating mutex site is still accounted for (B4 regression guard)**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "acquire_commit_mutex(" --include=*.py src/ | grep -v "def acquire_commit_mutex" \
  | grep -v "acquire_commit_mutex_live"
```
Expected: exactly **two** lines — `turns/submit.py` (B4's recorded per-site decision) and the internal call inside `acquire_commit_mutex_live` in `locking.py`. B-retire adds no new mutex sites; anything else means a writer regressed.

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:**
  - **B1** (coordinator + bridge) → **Task 1**: `WorkspaceRetirement` with `REQUIRED_HOOKS` validated at construction (startup invariant, spec test 9), concurrent+isolated `retire`; `signal_workspace_retirement` bounded sync→loop bridge (spec test 10, separate-thread loop + hanging-hook-returns-at-bound).
  - **B2** (turns tombstone) → **Task 2**: `registry._retired`, `retire_workspace`, `claim_launch_or_wait` rejection FIRST, `fulfill_pending_launch -> bool`, `controller.launch` hand-off tearing the fresh handle down, `controller.registry` property.
  - **B3** (collab retirement) → **Task 3**: `_retired_ws`, `acquire`+`operate` guards incl. the `await load()` straddle, `retire_workspace` (both rooms, 1008, no checkpoint, ops drained), `collab_ws` 1008 mapping (spec test 11 — all clauses: evicted, 1008-not-1011/1006, no checkpoint, ops drained, both entry points, operate straddle).
  - **B5** (service split + route) → **Task 4** (`soft_delete_workspace_db` / `scrub_workspace_bytes` / retained wrapper + composition test) and **Task 5** (route order DB→retire→scrub→204, `signal_from_request` call removed/import kept, coordinator wiring; spec test 14 — 204 success, 429 nothing committed, no new dispatch + terminal row, no "no send after response" assertion).
  - **Behaviour-preservation of the split** → Task 4 `test_split_halves_compose_to_the_wrapper`.
  - **Not in scope (correctly absent):** B4 (merged 194d8ce); Slice A (merged 7b16547); Slice C passes + non-mutex `settings.py` writers.
- **Placeholder scan:** two test bodies carry an explicit implementer decision inline rather than a silent assumption — Task 2's throwaway-handle line (reuse `test_drafting_registry.py:58`'s real handle build; `object()` only where the handle is never dereferenced) and Task 5's last assertion (prefer the sync `_workspace_is_deleted` gate over driving async `launch` from a sync test). Both are flagged, not hidden, and every exception type / expected value around them is exact. The collab and WS-route tests reuse the neighbouring suites' fixtures verbatim (named explicitly). No "TBD"/"add error handling"/"similar to Task N".
- **Type consistency:**
  - `WorkspaceRetired(workspace_id)` / `.workspace_id` — one shared exception (`contract.py:122`) raised by the turns registry (T2), the collab manager (T3), and caught in `collab_ws` (T3). Never redefined.
  - `WorkspaceDeleted` (B4) stays the *mutex* 404 refusal — distinct, unchanged, only referenced in Task 4's `soft_delete_workspace_db` docstring/behaviour.
  - `fulfill_pending_launch(..., *, lock_session) -> bool` (T2 def) — the single production caller `controller.launch` consumes the bool (T2); test callers ignore it.
  - `retire_workspace` — turns variant `-> None` (T2), collab variant `-> int` (T3); both are hooks the coordinator awaits and whose return it discards (matches decision 4). The `dict[str, ...]` hook keys `"turns"`/`"collab"` match `REQUIRED_HOOKS` exactly (T1 ↔ T5 wiring).
  - `soft_delete_workspace_db(session, *, workspace_id, timeout_seconds=10)` and `scrub_workspace_bytes(workspace_id, *, workspaces_dir)` (T4 def) are called with those exact keywords by the wrapper (T4) and the route (T5).
  - `_WS_POLICY_VIOLATION = 1008` — added to `room.py` (T3) and already present in `collab_ws.py:38`; both used for the 1008 close.
- **Ordering:** T1 (coordinator) has no dependencies; T2/T3 build the two hooks; T4 splits the service; T5 wires the coordinator (needs T2+T3 hooks) and reorders the route (needs T4 halves + T1 bridge). Each task is independently testable and committable. The wiring's startup-invariant (missing hook → boot failure) is only reachable once both real hooks exist, which is why wiring lands in T5, not T1.
- **Honest scope:** no task claims a subprocess is proven stopped (T2 terminates a live entry *best-effort*; its tombstone is install-linearization only, stated in code + commit) or that every writer is covered (`settings.py:69/:97` named as residual in Global Constraints, alongside the bounded `room.lock`-wait residual from T3). The gate (T6 Steps 7–8) enforces this against **the commit messages and the `*.py` diff only** — never against this plan or the design doc, both of which quote the forbidden phrases while explaining the gate and would false-positive.

## Execution Handoff

Two execution options:

**1. Subagent-Driven (recommended)** — a fresh subagent per task with two-stage review between tasks.

**2. Inline Execution** — execute in this session via `superpowers:executing-plans`, batching with checkpoints.
