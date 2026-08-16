# Workspace Lifecycle Generation (Retention Slice C0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a workspace's lifecycle transitions totally ordered and their effects order-independent, so a delete's teardown can never undo a restore (and vice versa).

**Architecture:** Every lifecycle transition allocates a `lifecycle_generation` inside its own transaction, under the commit mutex, so the mutex totally orders generations. The generation travels with the operation and is never re-read. One app-loop object, `WorkspaceLifecycle`, holds the authoritative `(applied_generation, state)` per workspace with a **monotonic** mutator, so a late effect from a superseded generation is ignored rather than applied. The collab and turns tombstone sets are replaced by that one authority. The jobs hook, whose work escapes every lock, is made generation-conditional in SQL instead.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, asyncio, pytest (+ testcontainers Postgres).

**Spec:** `docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md` revision 7, **section 4 only**. Section 5 (C1, restore) is a separate slice and must NOT be built here.

## Global Constraints

- **C0 adds no route, no UI, and no restore.** It builds the mechanism C1 will use. The only user-visible behaviour change is that deleting a workspace increments a column.
- **Nothing is removed.** No `rmtree`, no purge, no data deletion. The only schema change is Task 1's column.
- **The generation is allocated INSIDE the transaction that performs the transition, under the commit mutex, and travels with the operation.** It is never sampled after the commit. This is the entire point of the slice — spec 4.4.
- **`WorkspaceLifecycle.apply` is monotonic:** `if generation <= applied_generation: return`. Correctness rests on this, not on the lock (spec 4.5, 4.7).
- **Ordering is commit-then-apply.** Applying `active` before the database says live opens a real hole: for collab client-writes and cold hydration the tombstone IS the guard, not a belt over the DB flag (`retirement.py:32-35`).
- **The jobs hook's DB mutation must be generation-conditional in SQL** — `wait_for(to_thread(...))` cancels the await, not the thread (`jobs_retirement.py:137`; the codebase documents this at `tests/proposal_assist/test_phase0_retirement.py:317`). A stale check before the thread starts proves nothing.
- Work on a branch. Never commit to `master`. All Python through `uv run`.

## Environment notes

- **Fast test runs:** `uv run pytest -n 16 --dist loadfile` (~65s full suite). Single files run fine without `-n`. Never `-n auto` (resolves to **72** here — psutil is not installed), never `--dist loadgroup`.
- **Never pipe a test command into `tail`/`head`** — the pipeline's exit status becomes the pager's, so a failing suite reports success. Use `cmd > /tmp/out.log 2>&1; echo "EXIT=$?"; tail -40 /tmp/out.log`.
- **`pytest` exits 4 on "no tests ran"** — a path typo reads as success. Always confirm a nonzero collected count.
- **`uv run ruff check .` is the gate. `ruff format --check` is NOT** — 185 files are already unformatted on master. Do not reformat anything.
- **Baseline on master: `3405 passed, 1 skipped`.** Re-derive it from a real run before comparing; it moves every slice.
- **Alembic head is `0061`.** A new migration sets `down_revision = "0061"`.
- Route docstrings become OpenAPI descriptions, so `frontend/src/api/schema.gen.ts` is a CI gate. **C0 changes no route signature or docstring**, so no regeneration should be needed — but verify with `git diff --exit-code` rather than assuming.

## File Structure

| File | Responsibility |
|---|---|
| `alembic/versions/0062_workspace_lifecycle_generation.py` | **create** — the column |
| `src/call_manager/db/models.py` | add `Workspace.lifecycle_generation` |
| `src/call_manager/workspaces.py` | `bump_lifecycle_generation()`; `soft_delete_workspace_db` uses it and returns the new generation |
| `src/call_manager/proposal_assist/lifecycle.py` | **create** — `WorkspaceLifecycle`, the single authority |
| `src/call_manager/proposal_assist/retirement.py` | hooks and `retire()` carry a generation; `signal_workspace_retirement` takes one |
| `src/call_manager/proposal_assist/jobs_retirement.py` | generation-conditional SQL |
| `src/call_manager/proposal_assist/collab/room.py` | `_retired_ws` → `lifecycle.is_retired` |
| `src/call_manager/proposal_assist/drafting/registry.py` | `_retired` → `lifecycle.is_retired` |
| `src/call_manager/proposal_assist/passes.py` | hook accepts a generation |
| `src/call_manager/web/api/workspaces.py` | delete route threads the generation through |
| `src/call_manager/main.py` | construct `WorkspaceLifecycle`, inject it |

---

### Task 1: The generation column, and who allocates it

**Files:**
- Create: `alembic/versions/0062_workspace_lifecycle_generation.py`
- Modify: `src/call_manager/db/models.py` (the `Workspace` class, beside `soft_deleted_at` at :689)
- Modify: `src/call_manager/workspaces.py` (`soft_delete_workspace_db` at :81)
- Test: `tests/test_lifecycle_generation.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Workspace.lifecycle_generation: Mapped[int]` — NOT NULL, server default `0`.
  - `bump_lifecycle_generation(session: Session, *, workspace_id: uuid.UUID) -> int` in `call_manager.workspaces` — increments and returns the NEW value. Does not commit.
  - `soft_delete_workspace_db(...) -> int` — **signature change**, was `-> None`. Returns the generation its transaction allocated.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lifecycle_generation.py`:

```python
"""lifecycle_generation: allocated inside the transition's own transaction.

Retention Slice C0 (spec 4.4). The generation exists so a transition's effects
can be recognised as stale. Sampling it after the commit binds it to when the
sampler ran, not to which transition it belongs to — which is the defect this
column replaces.
"""

from __future__ import annotations

import pytest

from call_manager.db.models import Workspace
from call_manager.workspaces import bump_lifecycle_generation, soft_delete_workspace_db
from tests.proposal_assist.conftest import make_workspace


def test_a_new_workspace_starts_at_generation_zero(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        assert s.get(Workspace, ws.id).lifecycle_generation == 0


def test_bump_increments_and_returns_the_new_value(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        assert bump_lifecycle_generation(s, workspace_id=ws_id) == 1
        assert bump_lifecycle_generation(s, workspace_id=ws_id) == 2
        s.commit()

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).lifecycle_generation == 2


def test_bump_is_computed_in_the_database_not_from_a_stale_read(pg_session_factory):
    """Two sessions that both read 0 must not both write 1. `column + 1` in SQL
    is what makes the commit mutex the only thing deciding order; `python_value
    + 1` would let a stale read silently reuse a generation, and two different
    transitions sharing a generation defeats staleness detection entirely."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as a, pg_session_factory() as b:
        a.get(Workspace, ws_id)   # both load the row at generation 0
        b.get(Workspace, ws_id)
        assert bump_lifecycle_generation(a, workspace_id=ws_id) == 1
        a.commit()
        assert bump_lifecycle_generation(b, workspace_id=ws_id) == 2, (
            "the second bump reused a stale in-Python value instead of reading "
            "the committed column"
        )
        b.commit()


def test_soft_delete_allocates_a_generation_and_returns_it(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        generation = soft_delete_workspace_db(s, workspace_id=ws_id)

    assert generation == 1
    with pg_session_factory() as s:
        row = s.get(Workspace, ws_id)
        assert row.soft_deleted_at is not None
        assert row.lifecycle_generation == 1, (
            "the delete must allocate its generation in the SAME transaction as "
            "the state change; allocating it later is spec 4.4's defect"
        )


def test_the_generation_is_per_workspace(pg_session_factory):
    with pg_session_factory() as s:
        a = make_workspace(s)
        b = make_workspace(s)
        s.commit()
        a_id, b_id = a.id, b.id

    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=a_id)

    with pg_session_factory() as s:
        assert s.get(Workspace, a_id).lifecycle_generation == 1
        assert s.get(Workspace, b_id).lifecycle_generation == 0
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_lifecycle_generation.py -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t1.log
```

Expected: FAIL — `ImportError: cannot import name 'bump_lifecycle_generation'`.

- [ ] **Step 3: Add the column to the model**

In `src/call_manager/db/models.py`, in the `Workspace` class, directly after `soft_deleted_at` (around line 692):

```python
    # Retention Slice C0. Incremented by EVERY lifecycle transition (delete
    # today, restore in C1) inside that transition's own transaction, under the
    # commit mutex — so the mutex totally orders generations and a transition's
    # teardown can be recognised as belonging to a superseded one. Never read
    # and re-written from Python: see bump_lifecycle_generation.
    lifecycle_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
```

`Integer` and `text` are already imported in this module — confirm before adding an import.

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0062_workspace_lifecycle_generation.py`:

```python
"""workspaces: lifecycle_generation, allocated per lifecycle transition

Revision ID: 0062
Revises: 0061
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so existing rows land at 0 without a backfill pass; NOT NULL
    # so no code path has to consider a null generation.
    op.add_column(
        "workspaces",
        sa.Column(
            "lifecycle_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "lifecycle_generation")
```

- [ ] **Step 5: Implement `bump_lifecycle_generation` and use it**

In `src/call_manager/workspaces.py`, add:

```python
def bump_lifecycle_generation(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Allocate the next lifecycle generation for *workspace_id* and return it.

    Computed as `lifecycle_generation + 1` IN THE DATABASE, never from a value
    read into Python first: two transitions that both read the old value would
    otherwise write the same new one, and two transitions sharing a generation
    makes staleness undetectable — which is the whole mechanism.

    Does NOT commit. The caller must already hold the commit mutex on this
    workspace, so that the mutex is what orders generations (spec 4.4).
    """
    return session.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(lifecycle_generation=Workspace.lifecycle_generation + 1)
        .returning(Workspace.lifecycle_generation)
    ).scalar_one()
```

`update` must be imported from `sqlalchemy` — check the module's existing imports first.

Then in `soft_delete_workspace_db`, change the return type to `int`, allocate the generation **before** `session.commit()`, and return it. The mutex is already held for the whole transaction, so no new locking is needed:

```python
    generation = bump_lifecycle_generation(session, workspace_id=workspace_id)
    session.commit()
    return generation
```

Extend its docstring with a sentence naming what the return value is and why it must not be re-read afterwards.

- [ ] **Step 6: Fix the callers of `soft_delete_workspace_db`**

Its return type changed. Find every caller and decide each explicitly:

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "soft_delete_workspace_db" --include="*.py" src/ tests/
```

Callers that ignore the return value keep working. **Do not** change the delete route yet — Task 3 owns that.

- [ ] **Step 7: Run the tests and the gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/test_lifecycle_generation.py -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t1.log
uv run pytest -n 16 --dist loadfile -q > /tmp/t1-full.log 2>&1; echo "FULL_EXIT=$?"; tail -3 /tmp/t1-full.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 5 passed; the full suite green at the re-derived baseline + 5; `RUFF_EXIT=0`.

- [ ] **Step 8: Prove the migration round-trips**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "def test_.*migration\|alembic" tests/ --include="*.py" -l | head
```

Follow whatever migration test this repo already has. If it has one that walks upgrade/downgrade, confirm 0062 passes it; if there is none, assert the column exists and is NOT NULL in a test, and say in your report that no round-trip harness existed.

- [ ] **Step 9: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor
git add alembic/versions/0062_workspace_lifecycle_generation.py src/call_manager/db/models.py src/call_manager/workspaces.py tests/test_lifecycle_generation.py
git status --short
git commit -F /tmp/msg-t1.txt
```

Write the message to `/tmp/msg-t1.txt` first — backticks in an inline `-m` are substituted by the shell.

---

### Task 2: `WorkspaceLifecycle` — the single authority

**Files:**
- Create: `src/call_manager/proposal_assist/lifecycle.py`
- Test: `tests/proposal_assist/test_workspace_lifecycle.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1 (this object never touches the database).
- Produces: `WorkspaceLifecycle` with
  - `is_retired(workspace_id: uuid.UUID) -> bool` — **synchronous**, called from hot guard paths.
  - `async apply(workspace_id: uuid.UUID, *, generation: int, retired: bool) -> bool` — returns whether it took effect.
  - `async lock(workspace_id: uuid.UUID)` — an async context manager over a per-workspace `asyncio.Lock`.
  - `applied_generation(workspace_id: uuid.UUID) -> int` — for tests and diagnostics.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_workspace_lifecycle.py`:

```python
"""WorkspaceLifecycle: one authority, monotonic, order-independent.

Retention Slice C0 (spec 4.5). Correctness rests on monotonicity, NOT on the
lock: the jobs hook's work provably escapes every lock this process holds
(spec 4.6), so the mechanism has to be right when effects arrive late and out
of order.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from call_manager.proposal_assist.lifecycle import WorkspaceLifecycle


def test_an_unknown_workspace_is_not_retired():
    lc = WorkspaceLifecycle()
    assert lc.is_retired(uuid.uuid4()) is False


async def test_apply_sets_the_state():
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    assert await lc.apply(ws, generation=1, retired=True) is True
    assert lc.is_retired(ws) is True
    assert lc.applied_generation(ws) == 1


async def test_a_lower_generation_is_ignored_in_both_directions():
    """The two races of spec 4.4, reduced to their essence. Neither side knows
    the other exists; monotonicity alone decides."""
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()

    # delete@5 arriving AFTER restore@6
    await lc.apply(ws, generation=6, retired=False)
    assert await lc.apply(ws, generation=5, retired=True) is False
    assert lc.is_retired(ws) is False
    assert lc.applied_generation(ws) == 6

    # restore@6 arriving AFTER delete@7
    await lc.apply(ws, generation=7, retired=True)
    assert await lc.apply(ws, generation=6, retired=False) is False
    assert lc.is_retired(ws) is True
    assert lc.applied_generation(ws) == 7


async def test_the_same_generation_applied_twice_is_a_no_op():
    """Retries coalesce by construction (spec 5.4): N applies == 1 apply. If a
    repeat took effect, a duplicate delivery could flip state back."""
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    assert await lc.apply(ws, generation=3, retired=True) is True
    assert await lc.apply(ws, generation=3, retired=False) is False
    assert lc.is_retired(ws) is True


async def test_state_is_per_workspace():
    lc = WorkspaceLifecycle()
    a, b = uuid.uuid4(), uuid.uuid4()
    await lc.apply(a, generation=1, retired=True)
    assert lc.is_retired(a) is True
    assert lc.is_retired(b) is False
    assert lc.applied_generation(b) == 0


async def test_concurrent_applies_converge_on_the_highest_generation():
    """Order-independence, exercised rather than asserted: the same set of
    effects delivered concurrently must land on the same state every time."""
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    effects = [(g, g % 2 == 1) for g in range(1, 21)]
    await asyncio.gather(
        *(lc.apply(ws, generation=g, retired=r) for g, r in effects)
    )
    assert lc.applied_generation(ws) == 20
    assert lc.is_retired(ws) is False   # generation 20 is even -> not retired


async def test_the_per_workspace_lock_excludes_and_does_not_block_siblings():
    lc = WorkspaceLifecycle()
    a, b = uuid.uuid4(), uuid.uuid4()
    order: list[str] = []

    async def hold(ws, tag, delay):
        async with lc.lock(ws):
            order.append(f"{tag}-in")
            await asyncio.sleep(delay)
            order.append(f"{tag}-out")

    await asyncio.gather(hold(a, "a1", 0.05), hold(a, "a2", 0), hold(b, "b", 0))

    # No `or`: assert the property directly. Two holders of the SAME workspace
    # must never be inside at once, whichever wins the race.
    depth = 0
    for ev in [e for e in order if e.startswith("a")]:
        depth += 1 if ev.endswith("-in") else -1
        assert depth <= 1, f"two holders of the same workspace overlapped: {order}"
    assert order.count("b-in") == 1, "a sibling workspace was serialised behind it"
```

`pyproject.toml:241` sets `asyncio_mode = "auto"`, so a bare `async def test_...` runs without any decorator. Some older files still carry `@pytest.mark.asyncio`; do not copy that — it is redundant here.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_workspace_lifecycle.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t2.log
```

Expected: FAIL — the module does not exist.

- [ ] **Step 3: Implement it**

Create `src/call_manager/proposal_assist/lifecycle.py`:

```python
"""The single authority on whether a workspace is retired.

Retention Slice C0 (spec 4.5). Before this, four retirement hooks each kept
their own in-memory tombstone and a reactivation had to clear every one of
them — a fan-out that can half-succeed, which is why revision 6's design needed
a "retryable clearing" contract it could not honour.

One authority removes that class. `apply` is MONOTONIC: an effect from a
generation at or below the one already applied is ignored. That is what makes
arrival order irrelevant, and arrival order genuinely is out of our control —
the jobs hook's work escapes every lock this process holds (spec 4.6).

Lives on the app loop. `is_retired` is deliberately synchronous: it is called
from guard paths on every collab write, and making those await a lock would put
a scheduling point in the hot path for a dictionary lookup.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass


@dataclass
class _State:
    generation: int
    retired: bool


class WorkspaceLifecycle:
    def __init__(self) -> None:
        self._state: dict[uuid.UUID, _State] = {}
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}

    def is_retired(self, workspace_id: uuid.UUID) -> bool:
        """Synchronous, and safe without the lock: a dict read of a single
        reference under the GIL. A caller can observe the state either side of
        a concurrent `apply`, which is exactly as true of any lock-free guard —
        the durable backstop is the under-mutex re-read of soft_deleted_at."""
        s = self._state.get(workspace_id)
        return s.retired if s is not None else False

    def applied_generation(self, workspace_id: uuid.UUID) -> int:
        s = self._state.get(workspace_id)
        return s.generation if s is not None else 0

    async def apply(
        self, workspace_id: uuid.UUID, *, generation: int, retired: bool
    ) -> bool:
        """Record the effect of the transition that produced *generation*.

        Returns True if it took effect, False if it was stale or a duplicate.
        `<=`, not `<`: re-delivering the SAME generation must not flip state,
        or a duplicate delivery could undo its own transition.
        """
        async with self.lock(workspace_id):
            current = self._state.get(workspace_id)
            if current is not None and generation <= current.generation:
                return False
            self._state[workspace_id] = _State(generation=generation, retired=retired)
            return True

    @contextlib.asynccontextmanager
    async def lock(self, workspace_id: uuid.UUID):
        """Per-workspace mutual exclusion for a transition's effects.

        A convenience, NOT the correctness argument — monotonic `apply` and the
        SQL predicate in jobs_retirement are what make the design correct, and
        both hold when work escapes this lock, which spec 4.6 proves it does.
        """
        lock = self._locks.get(workspace_id)
        if lock is None:
            lock = self._locks[workspace_id] = asyncio.Lock()
        async with lock:
            yield
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_workspace_lifecycle.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t2.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 7 passed, `RUFF_EXIT=0`.

- [ ] **Step 5: Prove monotonicity is not vacuous**

Change `if current is not None and generation <= current.generation:` to `if False:` and re-run. `test_a_lower_generation_is_ignored_in_both_directions`, `test_the_same_generation_applied_twice_is_a_no_op` and `test_concurrent_applies_converge_on_the_highest_generation` must all fail. Restore, re-run, confirm green. **Report both outcomes and confirm `git status` is clean.**

- [ ] **Step 6: Commit** (message in a file, as Task 1).

---

### Task 3: Thread the generation through retirement

**Files:**
- Modify: `src/call_manager/proposal_assist/retirement.py`
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`retire_workspace` signature only)
- Modify: `src/call_manager/proposal_assist/drafting/registry.py` (signature only)
- Modify: `src/call_manager/proposal_assist/passes.py` (signature only)
- Modify: `src/call_manager/proposal_assist/jobs_retirement.py` (signature only — Task 4 owns the SQL)
- Modify: `src/call_manager/web/api/workspaces.py` (the delete route, ~:1536)
- Test: `tests/proposal_assist/test_retirement_generation.py` (create)

**Interfaces:**
- Consumes: `soft_delete_workspace_db(...) -> int` from Task 1.
- Produces:
  - `RetirementHook = Callable[[uuid.UUID, int], Awaitable[object]]` — every hook now takes `(workspace_id, generation)`.
  - `WorkspaceRetirement.retire(workspace_id: uuid.UUID, generation: int)`.
  - `signal_workspace_retirement(request, *, workspace_id: uuid.UUID, generation: int)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_retirement_generation.py`:

```python
"""Retirement carries the generation its transition allocated.

Retention Slice C0 (spec 4.4). The defect being closed: revision 6 had
signal_workspace_retirement READ the current generation, which happens after
the delete commits — so a restore that committed in between made the delete's
own teardown look current, and it re-poisoned a live workspace.
"""

from __future__ import annotations

import uuid

import pytest

from call_manager.proposal_assist.retirement import WorkspaceRetirement


async def test_every_hook_receives_the_generation():
    seen: dict[str, tuple[uuid.UUID, int]] = {}

    def _hook(name):
        async def _h(workspace_id, generation):
            seen[name] = (workspace_id, generation)
        return _h

    coordinator = WorkspaceRetirement(
        hooks={n: _hook(n) for n in ("turns", "collab", "jobs", "passes")}
    )
    ws = uuid.uuid4()
    await coordinator.retire(ws, generation=7)

    assert set(seen) == {"turns", "collab", "jobs", "passes"}
    assert all(v == (ws, 7) for v in seen.values())


async def test_one_hook_failing_does_not_stop_the_others_receiving_it():
    """The existing isolation guarantee must survive the signature change."""
    seen: list[str] = []

    async def _boom(workspace_id, generation):
        raise RuntimeError("hook exploded")

    def _ok(name):
        async def _h(workspace_id, generation):
            seen.append(name)
        return _h

    coordinator = WorkspaceRetirement(
        hooks={
            "turns": _boom,
            "collab": _ok("collab"),
            "jobs": _ok("jobs"),
            "passes": _ok("passes"),
        }
    )
    await coordinator.retire(uuid.uuid4(), generation=1)
    assert sorted(seen) == ["collab", "jobs", "passes"]
```

Then add, to the existing `tests/proposal_assist/test_workspaces_delete_route.py`, a test that the route passes the generation it committed — **not** one read afterwards:

```python
def test_the_route_signals_retirement_with_the_generation_it_committed(
    monkeypatch, client_authed_owner, pg_session_factory, current_user
):
    """Spec 4.4. If the route sampled the generation after the commit, a restore
    landing in between would make this delete's teardown look current. Pinned by
    asserting the value passed equals the value the commit produced, not the
    value the row happens to hold when the signal runs."""
    import call_manager.web.api.workspaces as ws_api

    captured: dict = {}

    def _capture(request, *, workspace_id, generation):
        captured["workspace_id"] = workspace_id
        captured["generation"] = generation
        # Simulate a transition landing between the commit and the signal.
        with pg_session_factory() as s:
            ws = s.get(Workspace, workspace_id)
            ws.lifecycle_generation = 99
            s.commit()

    monkeypatch.setattr(ws_api, "signal_workspace_retirement", _capture)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    assert captured["workspace_id"] == ws_id
    assert captured["generation"] == 1, (
        "the route passed a generation it re-read instead of the one its own "
        "transaction allocated"
    )
```

Follow that file's existing fixtures and imports — read it before writing; do not invent fixture names.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_retirement_generation.py tests/proposal_assist/test_workspaces_delete_route.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t3.log
```

Expected: FAIL — `retire()` takes no `generation`.

- [ ] **Step 3: Change the coordinator**

In `src/call_manager/proposal_assist/retirement.py`:

```python
RetirementHook = Callable[[uuid.UUID, int], Awaitable[object]]
```

and

```python
    async def retire(self, workspace_id: uuid.UUID, generation: int) -> None:
        """Run EVERY registered hook concurrently, each isolated in its own
        try/except so one hook's failure never suppresses another's teardown.

        *generation* is the one the deleting transaction ALLOCATED, passed down
        untouched. No hook may re-read it: a generation read here would be the
        one current when teardown ran, not the one this teardown belongs to,
        and a transition that landed in between would then look like this one
        (spec 4.4).
        """

        async def _run(name: str, hook: RetirementHook) -> None:
            try:
                await hook(workspace_id, generation)
            ...
```

Keep the existing logging and `asyncio.gather` structure exactly; only the call gains an argument.

Then `signal_workspace_retirement`:

```python
def signal_workspace_retirement(
    request, *, workspace_id: uuid.UUID, generation: int
) -> None:
```

with `coro = coordinator.retire(workspace_id, generation)`. Extend its docstring: the caller supplies the generation, and this function must never read one.

- [ ] **Step 4: Update the four hook signatures**

Each hook gains `generation: int`. In this task they may ignore it — Task 4 uses it in `jobs`, Task 5 in `collab` and `turns`. `passes` ignores it permanently (it keeps no per-workspace state; say so in a comment).

- `collab/room.py:664` → `async def retire_workspace(self, workspace_id: uuid.UUID, generation: int) -> int:`
- `drafting/registry.py:197` → same shape, `-> None`
- `passes.py:117` → same shape, `-> int`
- `jobs_retirement.py` → the built `_hook(workspace_id, generation)`

**Find every caller and every test double** before you finish:

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "retire_workspace\|signal_workspace_retirement\|\.retire(" --include="*.py" src/ tests/
```

Classify each hit: production caller, test double, or assertion. A test double with the old arity will fail loudly, which is what you want — fix each one rather than making the parameter optional. **An optional `generation` would let a caller silently omit it and get a wrong-but-plausible default, which is the defect this slice exists to remove.**

- [ ] **Step 5: Thread it through the delete route**

In `src/call_manager/web/api/workspaces.py`, capture the return value of `soft_delete_workspace_db` and pass it on:

```python
    generation = soft_delete_workspace_db(session, workspace_id=workspace_id)
    ...
    signal_workspace_retirement(request, workspace_id=workspace_id, generation=generation)
```

Read the surrounding code first — the call sits inside a `try/except CommitMutexTimeout` block, and the variable must be in scope at the signal site.

- [ ] **Step 6: Run the tests and the gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_retirement_generation.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t3.log
uv run pytest -n 16 --dist loadfile -q > /tmp/t3-full.log 2>&1; echo "FULL_EXIT=$?"; tail -3 /tmp/t3-full.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
git diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
```

Expected: all green, `DRIFT_EXIT=0` (no route signature or docstring changed in a way OpenAPI sees — if it did, regenerate with `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` and say so in your report).

- [ ] **Step 7: Commit** (message in a file).

---

### Task 4: The jobs hook, generation-conditional in SQL

**Files:**
- Modify: `src/call_manager/proposal_assist/jobs_retirement.py`
- Test: `tests/proposal_assist/test_jobs_retirement_generation.py` (create)

**Interfaces:**
- Consumes: the `generation` parameter from Task 3; `Workspace.lifecycle_generation` from Task 1.
- Produces: no new symbols. The two cancellation updates gain a generation predicate.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_jobs_retirement_generation.py`:

```python
"""The jobs hook's work escapes every lock, so the SQL is the linearization point.

Retention Slice C0 (spec 4.6). build_jobs_retirement_hook runs its DB work as
wait_for(to_thread(...)), and the timeout cancels the AWAIT, not the thread —
this repo documents that in tests/proposal_assist/test_phase0_retirement.py:317.
So an orphaned thread can wake up long after a later transition committed. A
stale check before the thread starts proves nothing about when it runs; the
predicate has to be in the statement itself.
"""

from __future__ import annotations

import pytest

from call_manager.db.models import Workspace, WorkspaceSetupJob
from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook
from tests.proposal_assist.conftest import make_workspace


async def test_the_hook_cancels_at_its_own_generation(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
        s.get(Workspace, ws_id).lifecycle_generation = 1
        s.commit()

    hook = build_jobs_retirement_hook(pg_session_factory)
    await hook(ws_id, 1)

    with pg_session_factory() as s:
        job = s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "cancelled"


async def test_a_stale_generation_cancels_nothing(pg_session_factory):
    """The orphaned-thread case, reduced: the hook runs with generation 1 while
    the row has already moved to 2. It must update ZERO rows — asserted on the
    row, not on whether the hook was called."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
        s.get(Workspace, ws_id).lifecycle_generation = 2
        s.commit()

    hook = build_jobs_retirement_hook(pg_session_factory)
    await hook(ws_id, 1)

    with pg_session_factory() as s:
        job = s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "queued", (
            "a superseded retirement cancelled a setup job that a later "
            "transition had brought back"
        )


async def test_the_thread_released_after_a_later_transition_changes_nothing(
    pg_session_factory, monkeypatch
):
    """The real shape of spec 4.6: hold the hook's DB work open, move the
    generation on underneath it, then release it.

    `cancel_setup_jobs_for_workspace` is the seam because it is what the hook's
    thread actually calls (jobs_retirement.py:94). Wrapping it — rather than
    replacing it — keeps the real statement, and therefore the real predicate,
    under test.
    """
    import asyncio
    import threading

    import call_manager.proposal_assist.jobs_retirement as jr

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
        s.get(Workspace, ws_id).lifecycle_generation = 1
        s.commit()

    release = threading.Event()
    real = jr.cancel_setup_jobs_for_workspace

    def _blocking(session, workspace_id, generation):
        release.wait(timeout=10)
        return real(session, workspace_id, generation)

    monkeypatch.setattr(jr, "cancel_setup_jobs_for_workspace", _blocking)

    hook = build_jobs_retirement_hook(pg_session_factory)
    task = asyncio.ensure_future(hook(ws_id, 1))

    # A later transition commits while the hook's thread is parked.
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).lifecycle_generation = 2
        s.commit()

    release.set()
    # The hook's own wait_for may already have timed out; that IS the scenario,
    # not a failure. What matters is what the released thread then writes, so
    # this must wait for that thread deterministically — see the note below.
    with contextlib.suppress(asyncio.TimeoutError):
        await task
    await _join_the_orphaned_thread()

    with pg_session_factory() as s:
        job = s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "queued", (
            "an orphaned retirement thread cancelled a setup job after a later "
            "transition had moved the generation on"
        )
```

`_join_the_orphaned_thread()` is the one piece this plan does not hand you,
because it depends on a fact worth establishing rather than guessing: **`to_thread`
gives no handle on its thread.** Two honest options, and you must pick one and
say which in your report:

- `await asyncio.get_running_loop().shutdown_default_executor()` — waits for the
  default executor's threads to finish. Note this repo already documents that
  `asyncio.run()`'s cleanup does exactly this, at
  `tests/proposal_assist/test_phase0_retirement.py:321`.
- Have the wrapper signal completion on a second `threading.Event` and wait on
  that, which is deterministic and does not depend on executor internals.

**Do not `await asyncio.sleep(...)` and hope.** A test that merely usually wins
the race is flaky, and a flaky test on this slice's central guarantee certifies
something it did not check.

**Names above are verified, not guessed:** `jobs_retirement.py` defines
`cancel_claim_analysis_jobs_for_workspace` (:62), `cancel_setup_jobs_for_workspace` (:94),
`build_jobs_retirement_hook` (:123) and `_CANCELLABLE = ("queued","running","waiting_for_rubric_review")` (:55).
Both cancellation helpers currently take `(session, workspace_id)` and gain `generation` in Step 3.
Add `import contextlib` to the test module.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_jobs_retirement_generation.py -q > /tmp/t4.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t4.log
```

Expected: FAIL — the second test cancels the job, because nothing consults the generation yet.

- [ ] **Step 3: Add the predicate**

Both cancellation statements gain the same guard. Shape:

```python
    .where(
        WorkspaceSetupJob.workspace_id == workspace_id,
        WorkspaceSetupJob.status.in_(_CANCELLABLE),
        # The linearization point. This runs on an ORPHANED thread whenever the
        # hook's wait_for timed out (jobs_retirement.py's own bound cancels the
        # await, not the thread), so "was the workspace still at my generation
        # when I started" is not a question anyone can answer outside the
        # statement. Ask it here, atomically with the write.
        exists().where(
            Workspace.id == workspace_id,
            Workspace.lifecycle_generation == generation,
        ),
    )
```

Apply it to the claim-analysis cancellation too. Thread `generation` down from `_hook` to both.

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_jobs_retirement_generation.py tests/proposal_assist/test_phase0_retirement.py -q > /tmp/t4.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t4.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all green. `test_phase0_retirement.py` is included because it owns the existing bounds behaviour.

- [ ] **Step 5: Prove the predicate is load-bearing**

Delete the `exists()` clause from the setup-job statement only, re-run, and confirm `test_a_stale_generation_cancels_nothing` **and** `test_the_thread_released_after_a_later_transition_changes_nothing` fail. Restore, re-run, confirm green. Report both outcomes and confirm `git status` is clean.

- [ ] **Step 6: Commit** (message in a file).

---

### Task 5: One authority — collab and turns stop keeping their own sets

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (7 guard sites + the add at :681)
- Modify: `src/call_manager/proposal_assist/drafting/registry.py` (2 guard sites + the add at :217)
- Modify: `src/call_manager/main.py` (construct and inject `WorkspaceLifecycle`)
- Test: `tests/proposal_assist/test_lifecycle_authority.py` (create)

**Interfaces:**
- Consumes: `WorkspaceLifecycle` from Task 2; the `generation` parameter from Task 3.
- Produces: `app.state.workspace_lifecycle`. `MANAGER._retired_ws` and `registry._retired` **cease to exist**.

- [ ] **Step 1: Survey every site**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "_retired_ws\|_retired\b" --include="*.py" src/ tests/
```

Classify every hit: guard, add, test assertion, or unrelated. **Report the full list in your report** — the two sets have 7 and 2 guard sites respectively in production, and a missed guard is a workspace that stays refused after a restore.

- [ ] **Step 2: Write the failing test**

Create `tests/proposal_assist/test_lifecycle_authority.py`. It must prove the guards read the **authority**, not a local set — by driving the authority and observing the subsystems:

```python
"""collab and turns consult WorkspaceLifecycle, not their own sets.

Retention Slice C0 (spec 4.5). Two sets meant a reactivation had to clear both,
and a fan-out can half-succeed. One authority means a transition is one write.

These assertions deliberately exercise the SUBSYSTEMS rather than reading
lifecycle state: reading the authority would pass even if a guard still
consulted a stale local set.
"""
```

Write, at minimum:

1. after `apply(ws, generation=1, retired=True)`, a collab operation raises `WorkspaceRetired` and a turn launch is refused;
2. after `apply(ws, generation=2, retired=False)`, **the same two operations succeed** — this is the un-poisoning that revision 6 could not do reliably;
3. `apply(ws, generation=1, retired=True)` arriving late (after generation 2) leaves both subsystems accepting.

Read `tests/proposal_assist/test_workspaces_delete_route.py` and the collab tests for how to reach `MANAGER` and the drafting registry, and follow those patterns.

- [ ] **Step 3: Replace the sets**

In `room.py`, every `if workspace_id in self._retired_ws:` becomes `if self._lifecycle.is_retired(workspace_id):`, and `self._retired_ws.add(workspace_id)` at :681 becomes an `await self._lifecycle.apply(workspace_id, generation=generation, retired=True)`. Delete `self._retired_ws`. Same shape in `registry.py`.

The manager and registry need a reference. Follow the construction each already uses — if `MANAGER` is a module-level singleton constructed with no arguments, add a setter called from `main.py` at startup rather than changing every construction site, and make the default a private `WorkspaceLifecycle()` so a unit test that constructs one directly still works.

**Do not leave a fallback that silently tolerates a missing lifecycle.** If it is unset at guard time that is a startup wiring bug and must be loud.

- [ ] **Step 4: Wire it in `main.py`**

Construct one `WorkspaceLifecycle` in the lifespan, before the retirement coordinator (it is a dependency of the hooks), assign `app.state.workspace_lifecycle`, and inject it into `MANAGER` and the drafting registry.

- [ ] **Step 5: Run the tests and the gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_lifecycle_authority.py -q > /tmp/t5.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t5.log
uv run pytest -n 16 --dist loadfile -q > /tmp/t5-full.log 2>&1; echo "FULL_EXIT=$?"; tail -3 /tmp/t5-full.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

- [ ] **Step 6: Prove the sets are gone**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "_retired_ws" --include="*.py" src/ ; echo "COLLAB_SET_HITS=$?"
```

Expected: no hits in `src/` (grep exits 1). A surviving reference means a guard was missed.

- [ ] **Step 7: Commit** (message in a file).

---

### Task 6: The races, end to end

**Files:**
- Test: `tests/proposal_assist/test_lifecycle_races.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: nothing. This task is the slice's proof.

There is no restore route yet (that is C1). Drive the `active` side with the real primitives — `bump_lifecycle_generation` plus clearing `soft_deleted_at` in one transaction, then `apply(..., retired=False)`. **Do not write a test-only helper that re-implements the transition**; call the same functions C1 will call, so this test keeps its meaning once the route exists.

- [ ] **Step 1: Write the tests**

Create `tests/proposal_assist/test_lifecycle_races.py` covering spec 4.8:

1. **Forward race.** Pause a delete between its DB commit and `signal_workspace_retirement`; perform a full reactivation (bump + clear + apply); release the delete. Assert the workspace is live **and usable** — a collab operation succeeds and a turn launch is accepted.
2. **Reverse race.** Pause a reactivation between its commit and its apply; commit a new delete (bump + flag + apply retired); release the reactivation. Assert the workspace stays **retired** — the late `active` was ignored.
3. **Post-timeout.** Force `signal_workspace_retirement` to exceed its barrier while the coroutine keeps running; reactivate; then let retirement finish. Same assertion as 1.
4. **Orphaned jobs thread**, end to end: as Task 4's third test but driven through the real delete route.
5. **Per-workspace isolation**: a transition on A leaves B's subsystems untouched.

Each must assert on **observable subsystem behaviour**, not on `applied_generation`. Reading the counter would pass even if a guard still consulted a stale set.

- [ ] **Step 2: Run them**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_lifecycle_races.py -q > /tmp/t6.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t6.log
```

- [ ] **Step 3: Prove each race test fails against the pre-slice behaviour**

For race 1, temporarily make `retire()` ignore its `generation` argument (treat every retirement as current). Race 1 and 3 must fail. Restore.

For race 2, temporarily change `apply`'s guard to `<` instead of `<=`... **no** — that is a different mutation. Use: make `apply` unconditional (drop the monotonicity check). Race 2 must fail. Restore.

Report each mutation, which tests went red, and that `git status` is clean afterwards. **A race test that cannot fail is worse than no race test**, because it certifies the thing it does not check.

- [ ] **Step 4: Full gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest -n 16 --dist loadfile -q > /tmp/t6-full.log 2>&1; echo "FULL_EXIT=$?"; tail -3 /tmp/t6-full.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
git diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
```

- [ ] **Step 5: Commit** (message in a file).

---

## Done when

- Every lifecycle transition allocates its generation inside its own transaction, under the commit mutex, and the delete route passes **that** value to retirement — pinned by a test where the row's generation is changed between the commit and the signal.
- `WorkspaceLifecycle.apply` is monotonic in both directions, and duplicates are no-ops. Proven by mutation.
- `MANAGER._retired_ws` and `registry._retired` no longer exist; `grep -rn "_retired_ws" src/` is empty.
- The jobs hook's cancellations carry a generation predicate in SQL, and a thread released after a later transition updates **zero rows** — asserted on the row.
- All five race scenarios in spec 4.8 pass, and each was shown to fail against the pre-slice behaviour.
- `uv run pytest -n 16 --dist loadfile` green at the re-derived baseline plus this slice's tests; `uv run ruff check .` clean; `schema.gen.ts` shows no drift.
- One migration (`0062`), no `rmtree`, nothing removed, **no restore route and no UI** — those are C1.
