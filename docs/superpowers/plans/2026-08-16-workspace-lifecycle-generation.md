# Workspace Lifecycle Generation (Retention Slice C0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a workspace's lifecycle transitions totally ordered and their effects order-independent, so a delete's teardown can never undo a restore (and vice versa).

**Architecture:** Every lifecycle transition allocates a `lifecycle_generation` inside its own transaction, under the commit mutex, so the mutex totally orders generations. The generation travels with the operation and is never re-read. One app-wide object, `WorkspaceLifecycle`, holds the authoritative `(applied_generation, retired)` per workspace behind a per-workspace lock, with a **monotonic** mutator. The retirement coordinator applies the transition **once**, and holds that lock across the whole hook fan-out — so a superseded retirement runs **no teardown at all**, rather than tearing down a workspace a later transition has already brought back. The collab and turns tombstone sets are replaced by that one authority. The jobs hook, whose DB work provably escapes every lock this process holds, is made generation-conditional in SQL instead.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, asyncio, pytest (+ testcontainers Postgres).

**Spec:** `docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md` revision 7, **section 4 only**. Section 5 (C1, restore) is a separate slice and must NOT be built here.

## Global Constraints

- **C0 adds no route, no UI, and no restore.** It builds the mechanism C1 will use. The only user-visible behaviour change is that deleting a workspace increments a column.
- **Nothing is removed.** No `rmtree`, no purge, no data deletion. The only schema change is Task 1's column.
- **The generation is allocated INSIDE the transaction that performs the transition, under the commit mutex, and travels with the operation.** It is never sampled after the commit. This is spec 4.4, and half the point of the slice.
- **A superseded transition has NO effect — not a partial one.** The gate lives in `WorkspaceRetirement.retire`, upstream of every hook. It is not a per-hook check: all four hooks carry the same generation, so a per-hook gate would let the first hook win and the other three no-op, which is neither of the two states we are allowed to be in. This is spec 4.5's requirement that stale teardown do nothing, and it is the other half of the slice.
- **`WorkspaceLifecycle` is monotonic:** `if generation <= applied_generation`, the transition yields "no effect". Correctness rests on this, not on the lock (spec 4.5, 4.7).
- **No retirement hook, and no guard, may call `apply` or `transition`.** The coordinator holds the per-workspace lifecycle lock across the fan-out, so a hook that took it would deadlock against its own caller. `is_retired()` — synchronous and lock-free — is the ONLY lifecycle call legal inside a hook or a guard. Say so in a comment at every guard you write.
- **Ordering is commit-then-apply.** Applying `active` before the database says live opens a real hole: for collab client-writes and cold hydration the tombstone IS the guard, not a belt over the DB flag (`retirement.py:32-35`).
- **The jobs hook's DB mutation must be generation-conditional in SQL** — `wait_for(to_thread(...))` cancels the await, not the thread (`jobs_retirement.py:137`; the codebase documents this at `tests/proposal_assist/test_phase0_retirement.py:317`). Its thread escapes the coordinator's lock as easily as it escapes the timeout, so the coordinator's gate does not cover it. A stale check before the thread starts proves nothing.
- Work on a branch. Never commit to `master`. All Python through `uv run`.

## Environment notes

- **Fast test runs:** `uv run pytest -n 16 --dist loadfile` (~65s full suite). Single files run fine without `-n`. Never `-n auto` (resolves to **72** here — psutil is not installed), never `--dist loadgroup`.
- **Never pipe a test command into `tail`/`head`** — the pipeline's exit status becomes the pager's, so a failing suite reports success. Use `cmd > /tmp/out.log 2>&1; echo "EXIT=$?"; tail -40 /tmp/out.log`.
- **`pytest` exits 4 on "no tests ran"** — a path typo reads as success. Always confirm a nonzero collected count.
- **`uv run ruff check .` is the gate. `ruff format --check` is NOT** — 185 files are already unformatted on master. Do not reformat anything.
- **ruff's lint select is `["E", "F", "I", "B", "UP", "SIM"]` (`pyproject.toml:236`).** `F` means an unused import fails the gate: import `pytest` only in the test modules that actually call it.
- **Baseline on master: `3405 passed, 1 skipped`.** Re-derive it from a real run before comparing; it moves every slice.
- **Alembic head is `0061`.** A new migration sets `down_revision = "0061"`.
- Route docstrings become OpenAPI descriptions, so `frontend/src/api/schema.gen.ts` is a CI gate. **C0 changes no route signature or docstring**, so no regeneration should be needed — but verify with `git diff --exit-code` rather than assuming.

## File Structure

| File | Responsibility |
|---|---|
| `alembic/versions/0062_workspace_lifecycle_generation.py` | **create** — the column |
| `src/call_manager/db/models.py` | add `Workspace.lifecycle_generation` |
| `src/call_manager/workspaces.py` | `bump_lifecycle_generation()`; `soft_delete_workspace_db` uses it and returns the new generation |
| `src/call_manager/proposal_assist/lifecycle.py` | **create** — `WorkspaceLifecycle` + the process-wide `LIFECYCLE` singleton |
| `src/call_manager/proposal_assist/retirement.py` | the coordinator applies the transition once and gates the fan-out on it |
| `src/call_manager/proposal_assist/jobs_retirement.py` | generation-conditional SQL |
| `src/call_manager/proposal_assist/collab/room.py` | `_retired_ws` → `lifecycle.is_retired`; stops setting a tombstone |
| `src/call_manager/proposal_assist/drafting/registry.py` | `_retired` → `lifecycle.is_retired`; stops setting a tombstone |
| `src/call_manager/proposal_assist/drafting/factory.py` | production construction injects `LIFECYCLE` explicitly |
| `src/call_manager/proposal_assist/passes.py` | hook signature only — keeps no per-workspace state |
| `src/call_manager/web/api/workspaces.py` | delete route threads the generation through |
| `src/call_manager/main.py` | `app.state.workspace_lifecycle`; coordinator takes the lifecycle |

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

from call_manager.db.models import Workspace
from call_manager.workspaces import bump_lifecycle_generation, soft_delete_workspace_db
from tests.proposal_assist.conftest import make_workspace


def test_a_new_workspace_starts_at_generation_zero(pg_session_factory):
    """0 is reserved for "no transition has ever happened": every transition
    allocates >= 1, so a generation of 0 can never collide with a real one."""
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
- Produces:
  - `WorkspaceLifecycle` with
    - `is_retired(workspace_id: uuid.UUID) -> bool` — **synchronous**, lock-free, called from hot guard paths.
    - `applied_generation(workspace_id: uuid.UUID) -> int` — 0 for a workspace no transition has touched.
    - `transition(workspace_id, *, generation: int, retired: bool)` — an **async context manager** that holds the per-workspace lock for the whole body and yields `True` if the transition took effect, `False` if it was superseded. THE mutator.
    - `async apply(workspace_id, *, generation: int, retired: bool) -> bool` — convenience for callers with no contained work, implemented in terms of `transition`.
  - `LIFECYCLE: WorkspaceLifecycle` — the process-wide singleton, mirroring `MANAGER` (`collab/room.py:776`) and `PASS_REGISTRY` (`passes.py:200`).

`transition` is a context manager rather than a plain setter because the coordinator must hold the lock **across its hook fan-out** (Task 3). A setter that released immediately would let a restore land between the retirement's decision and its teardown, and the teardown would then destroy a live workspace — the exact defect this slice exists to close.

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

from call_manager.proposal_assist.lifecycle import WorkspaceLifecycle


def test_an_unknown_workspace_is_not_retired():
    lc = WorkspaceLifecycle()
    assert lc.is_retired(uuid.uuid4()) is False
    assert lc.applied_generation(uuid.uuid4()) == 0


async def test_a_transition_that_takes_effect_yields_true_and_sets_the_state():
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    async with lc.transition(ws, generation=1, retired=True) as took_effect:
        assert took_effect is True
    assert lc.is_retired(ws) is True
    assert lc.applied_generation(ws) == 1


async def test_a_superseded_transition_yields_false_and_changes_nothing():
    """The gate the coordinator branches on. Yielding False rather than raising
    is deliberate: a superseded retirement is a NORMAL outcome of two racing
    transitions, not an error, and the caller's job is to skip its work."""
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    await lc.apply(ws, generation=6, retired=False)
    async with lc.transition(ws, generation=5, retired=True) as took_effect:
        assert took_effect is False
    assert lc.is_retired(ws) is False
    assert lc.applied_generation(ws) == 6


async def test_a_lower_generation_is_ignored_in_both_directions():
    """The two races of spec 4.4, reduced to their essence. Neither side knows
    the other exists; monotonicity alone decides."""
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()

    # delete@5 arriving AFTER restore@6
    await lc.apply(ws, generation=6, retired=False)
    assert await lc.apply(ws, generation=5, retired=True) is False
    assert lc.is_retired(ws) is False

    # restore@6 arriving AFTER delete@7
    await lc.apply(ws, generation=7, retired=True)
    assert await lc.apply(ws, generation=6, retired=False) is False
    assert lc.is_retired(ws) is True
    assert lc.applied_generation(ws) == 7


async def test_the_same_generation_applied_twice_is_a_no_op():
    """Retries coalesce by construction (spec 5.4): N applies == 1 apply. `<=`,
    not `<`: if a repeat took effect, a duplicate delivery of one transition
    could flip state back after a later one had landed."""
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
    """Order-independence, exercised rather than asserted.

    The HIGHEST generation is delivered FIRST and the rest scramble behind it.
    Ascending delivery would be worthless here: last-write-wins would also end
    on 20, so the test would pass with monotonicity deleted.
    """
    lc = WorkspaceLifecycle()
    ws = uuid.uuid4()
    await lc.apply(ws, generation=20, retired=False)
    await asyncio.gather(
        *(lc.apply(ws, generation=g, retired=(g % 2 == 1)) for g in range(1, 20))
    )
    assert lc.applied_generation(ws) == 20, (
        "a superseded transition overwrote a newer one"
    )
    assert lc.is_retired(ws) is False   # generation 20 was retired=False


async def test_the_lock_is_held_for_the_whole_body_even_when_superseded():
    """Two properties in one exercise:

    1. Two transitions on the SAME workspace never overlap — this is what lets
       the coordinator run its teardown inside the body without a restore
       landing underneath it.
    2. The lock is held even for a SUPERSEDED transition. If it were released
       early on the stale path, a stale retirement could observe "superseded"
       and then interleave anyway.
    """
    lc = WorkspaceLifecycle()
    a, b = uuid.uuid4(), uuid.uuid4()
    order: list[str] = []

    async def hold(ws, tag, generation, delay):
        async with lc.transition(ws, generation=generation, retired=True):
            order.append(f"{tag}-in")
            await asyncio.sleep(delay)
            order.append(f"{tag}-out")

    # a2 carries the LOWER generation, so whichever order they arrive in, one of
    # the two bodies runs superseded.
    await asyncio.gather(
        hold(a, "a1", 9, 0.05), hold(a, "a2", 1, 0.0), hold(b, "b", 1, 0.0)
    )

    depth = 0
    for ev in [e for e in order if e.startswith("a")]:
        depth += 1 if ev.endswith("-in") else -1
        assert depth <= 1, f"two holders of the same workspace overlapped: {order}"
    assert order.count("b-in") == 1, "a sibling workspace was serialised behind it"
```

`pyproject.toml:241` sets `asyncio_mode = "auto"`, so a bare `async def test_...` runs without any decorator. Some older files still carry `@pytest.mark.asyncio`; do not copy that — it is redundant here. This module never calls `pytest.*`, so it must not import `pytest` (ruff `F401`).

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

One authority removes that class. `transition` is MONOTONIC: an effect from a
generation at or below the one already applied is refused. That is what makes
arrival order irrelevant, and arrival order genuinely is out of our control —
the jobs hook's work escapes every lock this process holds (spec 4.6).

`transition` is a context manager because its caller's WORK has to happen inside
the decision. WorkspaceRetirement holds it across the whole hook fan-out, so a
restore cannot land between "this retirement is current" and the teardown that
conclusion authorises. The consequence, which every hook and guard depends on:
NOTHING called from inside that body may take this lock again — `is_retired` is
lock-free precisely so guards can be called from there.

`is_retired` is also synchronous because it is called on every collab write;
making those await a lock would put a scheduling point in the hot path for a
dictionary lookup.

Growth: one entry per workspace transitioned per process, the same footprint as
the two tombstone sets it replaces. Eviction stays a later candidate (it was one
before this slice too), not built here.
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
        reference under the GIL. A caller can observe the state either side of a
        concurrent transition, which is exactly as true of any lock-free guard —
        the durable backstop is the under-mutex re-read of soft_deleted_at.

        Lock-free is also a REQUIREMENT, not just an optimisation: guards run
        inside hooks that the coordinator invokes while holding this lock."""
        s = self._state.get(workspace_id)
        return s.retired if s is not None else False

    def applied_generation(self, workspace_id: uuid.UUID) -> int:
        """0 means no transition has ever been applied. Every transition
        allocates >= 1, so 0 can never collide with a real generation."""
        s = self._state.get(workspace_id)
        return s.generation if s is not None else 0

    @contextlib.asynccontextmanager
    async def transition(
        self, workspace_id: uuid.UUID, *, generation: int, retired: bool
    ):
        """Decide whether the transition that allocated *generation* is current,
        and hold this workspace's lock for the caller's whole body either way.

        Yields True if it took effect, False if it was superseded. `<=`, not
        `<`: re-delivering the SAME generation must not flip state, or a
        duplicate delivery could undo its own transition.

        The lock is held on BOTH paths. Releasing early on the superseded path
        would let a stale caller interleave its work with a live transition's,
        which is the whole thing this serialises.
        """
        lock = self._locks.get(workspace_id)
        if lock is None:
            lock = self._locks[workspace_id] = asyncio.Lock()
        async with lock:
            current = self._state.get(workspace_id)
            if current is not None and generation <= current.generation:
                yield False
                return
            self._state[workspace_id] = _State(generation=generation, retired=retired)
            yield True

    async def apply(
        self, workspace_id: uuid.UUID, *, generation: int, retired: bool
    ) -> bool:
        """`transition` with an empty body, for callers with no contained work
        (C1's restore, and tests). Deliberately delegates rather than repeating
        the check: one mutator, one place monotonicity lives."""
        async with self.transition(
            workspace_id, generation=generation, retired=retired
        ) as took_effect:
            return took_effect


# The process-wide authority. A module singleton for the same reason MANAGER and
# PASS_REGISTRY are: the subsystems that consult it are themselves module
# singletons constructed at import. Production construction sites inject this
# object EXPLICITLY (main.py, collab/room.py, drafting/factory.py) so the wiring
# is readable; it is also the constructor DEFAULT, so that a construction site
# someone forgets to update binds to the one authority rather than quietly
# minting a second one. Tests that want isolation pass their own instance.
LIFECYCLE = WorkspaceLifecycle()
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_workspace_lifecycle.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t2.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 8 passed, `RUFF_EXIT=0`.

- [ ] **Step 5: Prove monotonicity is not vacuous**

Change `if current is not None and generation <= current.generation:` to `if False:` and re-run. These four must fail:

- `test_a_superseded_transition_yields_false_and_changes_nothing`
- `test_a_lower_generation_is_ignored_in_both_directions`
- `test_the_same_generation_applied_twice_is_a_no_op`
- `test_concurrent_applies_converge_on_the_highest_generation`

Restore, re-run, confirm green. **Report the exact list that went red and confirm `git status` is clean.** If `test_concurrent_applies_converge_on_the_highest_generation` survives the mutation, the delivery order is wrong — the highest generation must land first — and the test is worthless until it fails here.

- [ ] **Step 6: Commit** (message in a file, as Task 1).

---

### Task 3: The coordinator applies the transition, once, and gates on it

**Files:**
- Modify: `src/call_manager/proposal_assist/retirement.py`
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`retire_workspace` signature only)
- Modify: `src/call_manager/proposal_assist/drafting/registry.py` (signature only)
- Modify: `src/call_manager/proposal_assist/passes.py` (signature only)
- Modify: `src/call_manager/proposal_assist/jobs_retirement.py` (signature only — Task 4 owns the SQL)
- Modify: `src/call_manager/main.py` (the coordinator gains `lifecycle=`)
- Modify: `src/call_manager/web/api/workspaces.py` (the delete route, :1536)
- Test: `tests/proposal_assist/test_retirement_generation.py` (create)

**Interfaces:**
- Consumes: `soft_delete_workspace_db(...) -> int` (Task 1); `WorkspaceLifecycle`, `LIFECYCLE` (Task 2).
- Produces:
  - `RetirementHook = Callable[[uuid.UUID, int], Awaitable[object]]` — every hook now takes `(workspace_id, generation)`.
  - `WorkspaceRetirement(hooks=..., lifecycle=...)` — `lifecycle` is a **required** keyword argument. There are 9 construction sites in the repo; a required argument is what makes "which authority is this coordinator gating on" unanswerable-by-accident.
  - `WorkspaceRetirement.retire(workspace_id: uuid.UUID, generation: int)`.
  - `signal_workspace_retirement(request, *, workspace_id: uuid.UUID, generation: int)`.

**Why the gate is here and not in the hooks.** All four hooks receive the same generation, so a per-hook `apply` gate would return True for whichever hook won and False for the other three: the workspace would be half torn down. Worse, a hook that called `apply` would deadlock — the coordinator holds that workspace's lifecycle lock across the fan-out. One decision, upstream, holding the lock across the work it authorises.

**Why the hooks still take a generation they mostly ignore.** `jobs` needs it: its DB work runs on a thread that outlives both the timeout and the lock, so its guard has to be in the SQL (Task 4). The parameter is uniform because the coordinator's fan-out is uniform. `collab`, `turns` and `passes` ignore it and must say so in a comment naming the coordinator's gate as their reason.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_retirement_generation.py`:

```python
"""Retirement carries — and is gated on — the generation its transition allocated.

Retention Slice C0 (spec 4.4, 4.5). Two defects are closed here:

  * revision 6 had signal_workspace_retirement READ the current generation,
    which happens after the delete commits — so a restore that committed in
    between made the delete's own teardown look current;
  * forwarding a generation that nothing branches on leaves a stale retirement
    just as destructive as before. The coordinator must run NO hook when its
    transition has been superseded.
"""

from __future__ import annotations

import asyncio
import uuid

from call_manager.proposal_assist.lifecycle import WorkspaceLifecycle
from call_manager.proposal_assist.retirement import WorkspaceRetirement


def _coordinator(lifecycle, **hooks):
    """Build a coordinator with the four REQUIRED hook names, defaulting any the
    caller did not supply to a recording no-op."""
    seen: dict[str, tuple[uuid.UUID, int]] = {}

    def _recorder(name):
        async def _h(workspace_id, generation):
            seen[name] = (workspace_id, generation)
        return _h

    built = {n: hooks.get(n) or _recorder(n) for n in ("turns", "collab", "jobs", "passes")}
    return WorkspaceRetirement(hooks=built, lifecycle=lifecycle), seen


async def test_every_hook_receives_the_generation():
    lc = WorkspaceLifecycle()
    coordinator, seen = _coordinator(lc)
    ws = uuid.uuid4()
    await coordinator.retire(ws, generation=7)

    assert set(seen) == {"turns", "collab", "jobs", "passes"}
    assert all(v == (ws, 7) for v in seen.values())


async def test_the_coordinator_applies_the_transition_itself():
    """The hooks do not tombstone any more — the coordinator does, once, before
    any of them runs. Anything that consults is_retired must therefore see the
    workspace retired from inside the very first hook."""
    lc = WorkspaceLifecycle()
    observed: list[bool] = []

    async def _observe(workspace_id, generation):
        observed.append(lc.is_retired(workspace_id))

    coordinator, _ = _coordinator(lc, collab=_observe)
    ws = uuid.uuid4()
    await coordinator.retire(ws, generation=1)

    assert observed == [True], "a hook ran before the workspace was marked retired"
    assert lc.is_retired(ws) is True
    assert lc.applied_generation(ws) == 1


async def test_a_superseded_retirement_runs_NO_hook():
    """Spec 4.5. This is the finding that revision 1 of this plan missed: a
    coordinator that forwards the generation but fans out anyway still evicts
    rooms, closes sockets, pops subprocesses and cancels passes on a workspace a
    later transition brought back."""
    lc = WorkspaceLifecycle()
    coordinator, seen = _coordinator(lc)
    ws = uuid.uuid4()

    await lc.apply(ws, generation=2, retired=False)   # a restore got there first
    await coordinator.retire(ws, generation=1)

    assert seen == {}, f"a superseded retirement tore down a live workspace: {seen}"
    assert lc.is_retired(ws) is False
    assert lc.applied_generation(ws) == 2


async def test_the_lifecycle_lock_is_held_across_the_fan_out():
    """A transition landing DURING the teardown must wait for it, or it would
    un-retire a workspace whose rooms are still being evicted."""
    lc = WorkspaceLifecycle()
    started = asyncio.Event()
    order: list[str] = []

    async def _slow(workspace_id, generation):
        started.set()
        await asyncio.sleep(0.05)
        order.append("hook-done")

    coordinator, _ = _coordinator(lc, collab=_slow)
    ws = uuid.uuid4()

    task = asyncio.ensure_future(coordinator.retire(ws, generation=1))
    await started.wait()

    await lc.apply(ws, generation=2, retired=False)
    order.append("restore-applied")
    await task

    assert order == ["hook-done", "restore-applied"], (
        "a later transition landed while the teardown it supersedes was still "
        f"running: {order}"
    )


async def test_one_hook_failing_does_not_stop_the_others_receiving_it():
    """The existing isolation guarantee must survive both changes."""
    lc = WorkspaceLifecycle()

    async def _boom(workspace_id, generation):
        raise RuntimeError("hook exploded")

    coordinator, seen = _coordinator(lc, turns=_boom)
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

`signal_workspace_retirement` is imported module-level at `web/api/workspaces.py:77`, and that test file already monkeypatches it the same way at :152 — follow the surrounding fixtures and imports rather than inventing names.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_retirement_generation.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t3.log
```

Expected: FAIL — `WorkspaceRetirement()` takes no `lifecycle`.

- [ ] **Step 3: Change the coordinator**

In `src/call_manager/proposal_assist/retirement.py`:

```python
RetirementHook = Callable[[uuid.UUID, int], Awaitable[object]]
```

Update the module comment above it: hooks now receive the generation their retirement allocated; `jobs` is the only one that uses it, because its work escapes the coordinator's gate.

Constructor gains a required keyword:

```python
    def __init__(
        self, hooks: dict[str, RetirementHook], *, lifecycle: WorkspaceLifecycle
    ) -> None:
        ...
        self._lifecycle = lifecycle
```

and `retire`:

```python
    async def retire(self, workspace_id: uuid.UUID, generation: int) -> None:
        """Apply the retirement to the lifecycle authority, and — only if it is
        still current — run EVERY registered hook concurrently, each isolated in
        its own try/except so one hook's failure never suppresses another's.

        *generation* is the one the deleting transaction ALLOCATED, passed down
        untouched. Nothing here may re-read it: a generation read at this point
        would be the one current when teardown ran, not the one this teardown
        belongs to, so a transition that landed in between would look like this
        one (spec 4.4).

        The gate is HERE rather than in the hooks for two reasons. All four
        hooks carry the same generation, so a per-hook check would let the first
        win and leave the workspace half torn down. And the lifecycle lock is
        held across the whole fan-out — so a hook that took it would deadlock
        against its own caller. Inside this block, `is_retired` (lock-free) is
        the only lifecycle call that is legal.

        Holding the lock for the fan-out bounds nothing new: the hooks are
        individually bounded by the same budgets _RETIREMENT_BARRIER_SECONDS is
        derived from above (max 5.5s, passes), and test_retirement_coordinator.py
        asserts that inequality against the live constants.

        Never raises; return values are discarded.
        """
        async with self._lifecycle.transition(
            workspace_id, generation=generation, retired=True
        ) as took_effect:
            if not took_effect:
                logger.info(
                    "retirement.superseded",
                    extra={
                        "event": "retirement_superseded",
                        "workspace_id": str(workspace_id),
                        "generation": generation,
                    },
                )
                return

            async def _run(name: str, hook: RetirementHook) -> None:
                try:
                    await hook(workspace_id, generation)
                except Exception:
                    ...   # keep the existing logging block verbatim

            await asyncio.gather(
                *(_run(name, hook) for name, hook in self._hooks.items())
            )
```

Keep the existing logging and `asyncio.gather` structure exactly; only the gate and the extra argument are new.

Then `signal_workspace_retirement`:

```python
def signal_workspace_retirement(
    request, *, workspace_id: uuid.UUID, generation: int
) -> None:
```

with `coro = coordinator.retire(workspace_id, generation)`. Extend its docstring: the caller supplies the generation, and this function must never read one.

- [ ] **Step 4: Update the four hook signatures**

Each hook gains `generation: int`.

- `collab/room.py:664` → `async def retire_workspace(self, workspace_id: uuid.UUID, generation: int) -> int:`
- `drafting/registry.py:197` → same shape, `-> None`
- `passes.py:117` → same shape, `-> int`
- `jobs_retirement.py` → the built `_hook(workspace_id, generation)`

In `collab`, `turns` and `passes`, add a comment on the unused parameter: the coordinator gates the whole fan-out on this generation while holding the lifecycle lock, so this hook is never invoked for a superseded transition and must not re-check (and could not: taking the lock here deadlocks).

**Find every caller and every test double** before you finish:

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "retire_workspace\|signal_workspace_retirement\|WorkspaceRetirement(\|\.retire(" --include="*.py" src/ tests/
```

Classify each hit: production caller, test double, or assertion. A test double with the old arity fails loudly, which is what you want — fix each rather than making the parameter optional. **An optional `generation` would let a caller silently omit it and get a wrong-but-plausible default, which is the defect this slice exists to remove.** Known sites that must change: `tests/proposal_assist/test_retirement_coordinator.py` (8 constructions plus 3 bridge calls at :185, :216, :231) and `main.py:197`.

- [ ] **Step 5: Wire the lifecycle into the coordinator**

In `main.py`, import the singleton and pass it, next to the existing local imports in the lifespan:

```python
    from call_manager.proposal_assist.lifecycle import LIFECYCLE

    app.state.workspace_lifecycle = LIFECYCLE
    app.state.workspace_retirement = WorkspaceRetirement(
        hooks={...},          # unchanged
        lifecycle=LIFECYCLE,
    )
```

Task 5 wires the same object into collab and turns and adds the identity test.

- [ ] **Step 6: Thread it through the delete route**

In `src/call_manager/web/api/workspaces.py` (:1536), capture the return value and pass it on:

```python
    generation = soft_delete_workspace_db(session, workspace_id=workspace_id)
    ...
    signal_workspace_retirement(request, workspace_id=workspace_id, generation=generation)
```

Read the surrounding code first — the call sits inside a `try/except CommitMutexTimeout` block, and the variable must be in scope at the signal site.

- [ ] **Step 7: Run the tests and the gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_retirement_generation.py tests/proposal_assist/test_retirement_coordinator.py tests/proposal_assist/test_workspaces_delete_route.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t3.log
uv run pytest -n 16 --dist loadfile -q > /tmp/t3-full.log 2>&1; echo "FULL_EXIT=$?"; tail -3 /tmp/t3-full.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
git diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
```

Expected: all green, `DRIFT_EXIT=0` (no route signature or docstring changed in a way OpenAPI sees — if it did, regenerate with `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh` and say so in your report).

- [ ] **Step 8: Prove the gate is load-bearing**

Temporarily change the coordinator to ignore `took_effect` (fan out unconditionally). `test_a_superseded_retirement_runs_NO_hook` must fail. Restore, re-run, confirm green. Report both outcomes and that `git status` is clean.

- [ ] **Step 9: Commit** (message in a file).

---

### Task 4: The jobs hook, generation-conditional in SQL

**Files:**
- Modify: `src/call_manager/proposal_assist/jobs_retirement.py`
- Test: `tests/proposal_assist/test_jobs_retirement_generation.py` (create)

**Interfaces:**
- Consumes: the `generation` parameter from Task 3; `Workspace.lifecycle_generation` from Task 1.
- Produces: `cancel_setup_jobs_for_workspace(session, workspace_id, generation)` and `cancel_claim_analysis_jobs_for_workspace(session, workspace_id, generation)` — **both gain a third positional parameter**, and both statements gain a generation predicate.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_jobs_retirement_generation.py`:

```python
"""The jobs hook's work escapes every lock, so the SQL is the linearization point.

Retention Slice C0 (spec 4.6). build_jobs_retirement_hook runs its DB work as
wait_for(to_thread(...)), and the timeout cancels the AWAIT, not the thread —
this repo documents that at tests/proposal_assist/test_phase0_retirement.py:317.
The thread therefore escapes the coordinator's lifecycle lock exactly as it
escapes the timeout, so the coordinator's gate does NOT cover this hook: an
orphaned thread can wake up long after a later transition committed. A stale
check before the thread starts proves nothing about when it runs; the predicate
has to be in the statement itself.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import call_manager.proposal_assist.jobs_retirement as jr
from call_manager.db.models import Workspace, WorkspaceSetupJob
from call_manager.proposal_assist.jobs_retirement import build_jobs_retirement_hook
from tests.proposal_assist.conftest import make_workspace


def _seed(pg_session_factory, *, generation: int):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
        s.get(Workspace, ws_id).lifecycle_generation = generation
        s.commit()
    return ws_id


def _status(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one().status


async def test_the_hook_cancels_at_its_own_generation(pg_session_factory):
    ws_id = _seed(pg_session_factory, generation=1)
    await build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)
    assert _status(pg_session_factory, ws_id) == "cancelled"


async def test_a_stale_generation_cancels_nothing(pg_session_factory):
    """The orphaned-thread case, reduced: the hook runs with generation 1 while
    the row has already moved to 2. It must update ZERO rows — asserted on the
    row, not on whether the hook was called."""
    ws_id = _seed(pg_session_factory, generation=2)
    await build_jobs_retirement_hook(pg_session_factory)(ws_id, 1)
    assert _status(pg_session_factory, ws_id) == "queued", (
        "a superseded retirement cancelled a setup job that a later transition "
        "had brought back"
    )


async def test_a_thread_orphaned_by_the_timeout_changes_nothing(
    pg_session_factory, monkeypatch
):
    """The real shape of spec 4.6, in the real order:

      1. park the hook's DB thread and WAIT until it is genuinely parked;
      2. let the hook's own wait_for bound (_JOBS_CANCEL_SECONDS = 1.0) expire,
         so the thread is orphaned rather than merely slow;
      3. commit a later generation;
      4. release the orphan and wait for it to finish its transaction.

    Step 1 is what makes this different from the previous test. Scheduling the
    hook and then doing synchronous work never yields to the loop, so the hook
    would not have started at all — the thread would find the release already
    set and the test would silently degrade into a duplicate of the one above.
    """
    ws_id = _seed(pg_session_factory, generation=1)

    entered = threading.Event()
    release = threading.Event()
    real = jr.cancel_setup_jobs_for_workspace

    def _blocking(session, workspace_id, generation):
        entered.set()
        release.wait(timeout=30)   # bounded, so a failing test cannot hang the suite
        return real(session, workspace_id, generation)

    monkeypatch.setattr(jr, "cancel_setup_jobs_for_workspace", _blocking)

    task = asyncio.ensure_future(build_jobs_retirement_hook(pg_session_factory)(ws_id, 1))

    for _ in range(1000):          # wait for the CONDITION, not for a duration
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the hook's DB thread never parked; this test proves nothing")

    with pytest.raises(TimeoutError):   # asyncio.TimeoutError is this on 3.11+
        await task

    with pg_session_factory() as s:
        s.get(Workspace, ws_id).lifecycle_generation = 2
        s.commit()

    release.set()
    # to_thread gives no handle on its thread. This is the only boundary that
    # includes _work()'s COMMIT: an Event set by the wrapper fires before the
    # commit, and the assertion below would race it.
    await asyncio.get_running_loop().shutdown_default_executor()

    assert _status(pg_session_factory, ws_id) == "queued", (
        "an orphaned retirement thread cancelled a setup job after a later "
        "transition had moved the generation on"
    )
```

Notes the implementer needs, all verified against the module rather than guessed:

- `jobs_retirement.py` defines `cancel_claim_analysis_jobs_for_workspace` (:62), `cancel_setup_jobs_for_workspace` (:94), `build_jobs_retirement_hook` (:123), `_CANCELLABLE` (:55) and `_JOBS_CANCEL_SECONDS = 1.0` (:37). `_work()` calls both helpers by module-global name and then commits, which is why `monkeypatch.setattr` on the module attribute reaches it.
- `shutdown_default_executor()` must be the LAST use of a worker thread in that test — the loop's default executor does not come back.
- The parked thread holds one pooled connection while the test opens another. If the suite reports pool exhaustion here, raise it in your report rather than reducing the test to a single connection: the two-connection shape is the scenario.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_jobs_retirement_generation.py -q > /tmp/t4.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t4.log
```

Expected: FAIL — the helpers take no `generation`.

- [ ] **Step 3: Add the predicate**

Both cancellation statements gain the same guard, and both helpers gain the parameter. Shape, on the setup-job select:

```python
    .where(
        WorkspaceSetupJob.workspace_id == workspace_id,
        WorkspaceSetupJob.status.in_(_CANCELLABLE),
        # The linearization point. This runs on an ORPHANED thread whenever the
        # hook's wait_for timed out (that bound cancels the await, not the
        # thread), and the thread escapes the coordinator's lifecycle lock the
        # same way — so "was the workspace still at my generation when I
        # started" is not a question anyone can answer outside the statement.
        # Ask it here, atomically with the write.
        exists().where(
            Workspace.id == workspace_id,
            Workspace.lifecycle_generation == generation,
        ),
    )
```

`exists` comes from `sqlalchemy`; `Workspace` must be imported at module level (the module currently imports it lazily inside `reconcile_cancelled_jobs` — move it up rather than adding a second local import). Thread `generation` from `_hook` into both helpers.

- [ ] **Step 4: Fix the three test files that call the helpers directly**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "cancel_setup_jobs_for_workspace\|cancel_claim_analysis_jobs_for_workspace" --include="*.py" tests/
```

`test_phase0_retirement.py` (:335), `test_api_writers_mutex_straddle.py` (:644) and `test_claim_analysis_retirement.py` (:69, :111) call them with two arguments. Each must pass the generation **that workspace's row actually holds** — read it, or use the value its own delete allocated. **Do not pass 0 blindly:** a workspace that has been deleted in the test sits at 1, and 0 would silently make the call a no-op, turning a real assertion into a vacuous one.

- [ ] **Step 5: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_jobs_retirement_generation.py tests/proposal_assist/test_phase0_retirement.py tests/proposal_assist/test_claim_analysis_retirement.py tests/proposal_assist/test_api_writers_mutex_straddle.py -q > /tmp/t4.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t4.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

- [ ] **Step 6: Prove the predicate is load-bearing**

Delete the `exists()` clause from the setup-job statement only, re-run, and confirm both `test_a_stale_generation_cancels_nothing` **and** `test_a_thread_orphaned_by_the_timeout_changes_nothing` fail. Restore, re-run, confirm green. Report both outcomes and confirm `git status` is clean.

- [ ] **Step 7: Commit** (message in a file).

---

### Task 5: One authority — collab and turns stop keeping their own sets

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (6 guard checks, the tombstone add at :681, the ctor at :475, the singleton at :776)
- Modify: `src/call_manager/proposal_assist/drafting/registry.py` (2 guard checks at :133 and :318, the tombstone add at :217, the ctor at :85)
- Modify: `src/call_manager/proposal_assist/drafting/factory.py` (:39 — inject `LIFECYCLE`)
- Modify: `tests/proposal_assist/collab/test_collab_retire.py`, `tests/proposal_assist/test_drafting_retire_tombstone.py` (they call the hooks directly)
- Test: `tests/proposal_assist/test_lifecycle_authority.py` (create)

**Interfaces:**
- Consumes: `WorkspaceLifecycle`, `LIFECYCLE` (Task 2); the coordinator's gate (Task 3).
- Produces: `RoomManager(lifecycle=...)` and `SubprocessRegistry(..., lifecycle=...)` — keyword, defaulting to `LIFECYCLE`; `app.state.workspace_lifecycle`. `MANAGER._retired_ws` and `registry._retired` **cease to exist**.

**On the constructor default.** Production sites inject explicitly (`room.py:776`, `factory.py:39`, `main.py`). The default exists for the 25 `RoomManager(...)` and 67 `SubprocessRegistry(...)` construction sites in tests, and it is `LIFECYCLE` — the same object — precisely so that a site nobody updates cannot mint a second authority. A private per-instance default would do exactly that, which is why this plan does not use one. Step 4's identity test is what proves the production wiring, and it is the reason a default is safe rather than a hiding place.

- [ ] **Step 1: Survey every site**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "_retired_ws\|_retired\b" --include="*.py" src/ tests/
```

Classify every hit: guard check, tombstone add, test assertion, comment, or unrelated. **Report the full list.** In production there are 6 guard checks in `room.py` (:495, :510, :523, :542, :557, :571) and 2 in `registry.py` (:133, :318); a missed guard is a workspace that stays refused after a restore.

- [ ] **Step 2: Write the failing test**

Create `tests/proposal_assist/test_lifecycle_authority.py`. It must prove the guards read the **authority** — by driving the authority and observing the subsystems:

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

1. after `apply(ws, generation=1, retired=True)`, a collab `acquire` raises `WorkspaceRetired` and a turn launch is refused — **without anyone having called `retire_workspace`**, which is what proves the guard reads the authority rather than a set the hook populates;
2. after `apply(ws, generation=2, retired=False)`, **the same two operations succeed** — this is the un-poisoning revision 6 could not do reliably;
3. `apply(ws, generation=1, retired=True)` arriving late (after generation 2) leaves both subsystems accepting;
4. a `WorkspaceLifecycle` passed to `RoomManager(lifecycle=...)` is the one its guards consult — construct two managers on two different lifecycles and show they do not see each other's state.

Read `tests/proposal_assist/collab/test_collab_retire.py` and `tests/proposal_assist/test_drafting_retire_tombstone.py` for how to reach a manager and the drafting registry, and follow those patterns.

- [ ] **Step 3: Replace the sets**

In `room.py`: the constructor takes `lifecycle: WorkspaceLifecycle | None = None` and stores `lifecycle if lifecycle is not None else LIFECYCLE`; every `if workspace_id in self._retired_ws:` becomes `if self._lifecycle.is_retired(workspace_id):`; `self._retired_ws` is deleted; `MANAGER = RoomManager(lifecycle=LIFECYCLE)`. Same shape in `registry.py`, and `factory.py:39` passes `lifecycle=LIFECYCLE`.

At one guard in each file, add the comment the Global Constraints require: `is_retired` is lock-free and that is load-bearing — this can run inside a hook the coordinator invokes while holding the lifecycle lock.

**`retire_workspace` stops tombstoning.** Delete `self._retired_ws.add(workspace_id)` (`room.py:681`) and `self._retired.add(workspace_id)` (`registry.py:217`). The coordinator applies the transition before any hook runs (Task 3), so the tombstone is already set; a hook that applied it again would deadlock on the lock its caller holds. Update both docstrings: they currently promise "tombstone, then tear down" and now promise only the teardown, with the tombstone named as the coordinator's.

- [ ] **Step 4: Wire it, and prove the wiring**

`main.py` already sets `app.state.workspace_lifecycle = LIFECYCLE` and passes it to the coordinator (Task 3, Step 5). Add a lifespan-level test asserting all four are the **same object**:

```python
assert app.state.workspace_lifecycle is LIFECYCLE
assert MANAGER._lifecycle is app.state.workspace_lifecycle
assert workspace_controller.registry._lifecycle is app.state.workspace_lifecycle
assert app.state.workspace_retirement._lifecycle is app.state.workspace_lifecycle
```

Follow `tests/proposal_assist/test_pass_registry_wiring.py` for how this repo builds an app through its lifespan. Reading `_lifecycle` is deliberate and confined to this test: it asserts wiring identity, which has no public surface, and a public accessor added only to be asserted on would be worse.

- [ ] **Step 5: Fix the tests that call the hooks directly**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "retire_workspace" --include="*.py" tests/
```

Roughly 19 sites — `test_collab_retire.py` (13) and `test_drafting_retire_tombstone.py` (6) — call `retire_workspace` directly and some of them then assert the workspace is refused. That assertion now belongs to the authority. For each site, decide which of the two it is testing:

- **teardown** (rooms popped, sockets closed, entry torn down, in-flight op drained): the call gains its second argument and nothing else changes.
- **the tombstone/guard** ("after retiring, `acquire` raises"): drive the authority the way the coordinator does, then call the hook:

```python
    lifecycle = WorkspaceLifecycle()
    mgr = RoomManager(lifecycle=lifecycle)
    ...
    await lifecycle.apply(ws, generation=1, retired=True)   # what the coordinator does first
    await mgr.retire_workspace(ws, 1)
```

**Do not delete a test because the refactor made it inconvenient.** If a test no longer has a home, say so in your report and leave it failing for the review to adjudicate.

- [ ] **Step 6: Run the tests and the gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_lifecycle_authority.py -q > /tmp/t5.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t5.log
uv run pytest -n 16 --dist loadfile -q > /tmp/t5-full.log 2>&1; echo "FULL_EXIT=$?"; tail -3 /tmp/t5-full.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

- [ ] **Step 7: Prove the sets are gone**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "_retired_ws" --include="*.py" src/ ; echo "COLLAB_SET_HITS=$?"
grep -rn "self\._retired\b" --include="*.py" src/ ; echo "TURNS_SET_HITS=$?"
```

Expected: no hits for either (grep exits 1 both times). `_retired_ws` alone does not cover the turns set — `\b` after `_retired` is what separates them.

- [ ] **Step 8: Commit** (message in a file).

---

### Task 6: The races, end to end

**Files:**
- Test: `tests/proposal_assist/test_lifecycle_races.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: nothing. This task is the slice's proof.

There is no restore route yet (that is C1). Drive the `active` side with the real primitives — `bump_lifecycle_generation` plus clearing `soft_deleted_at` in one transaction, then `apply(..., retired=False)`. **Do not write a test-only helper that re-implements the transition**; call the same functions C1 will call, so these tests keep their meaning once the route exists.

**Assert on resources that already exist, not on replacements.** "A new collab room can be opened afterwards" is far too weak: an unguarded teardown evicts the room, closes the sockets and pops the subprocess entry, and you can still open a fresh one afterwards. Each race must create its live resources **after** the reactivation and then assert **those** survived — `mgr.has_room(key)` is still True, the socket was not closed, the registry entry is still present.

- [ ] **Step 1: Write the tests**

Create `tests/proposal_assist/test_lifecycle_races.py` covering spec 4.8:

1. **Forward race.** Pause a delete between its DB commit and `signal_workspace_retirement`; reactivate fully (bump + clear + apply); open a collab room and install a turn-registry entry; release the delete. Assert the room and the entry are **still there** and no socket was closed.
2. **Reverse race.** Pause a reactivation between its commit and its apply; commit a new delete (bump + flag + retire); release the reactivation. Assert the workspace stays **retired** — the late `active` was ignored.
3. **Post-timeout.** Force `signal_workspace_retirement` to exceed its barrier while the coroutine keeps running; reactivate; create the resources; then let retirement finish. Same assertion as 1.
4. **Orphaned jobs thread**, end to end: Task 4's third test driven through the real delete route.
5. **Per-workspace isolation**: a transition on A leaves B's room and B's registry entry untouched.

Each must assert on **observable subsystem behaviour**, not on `applied_generation`. Reading the counter would pass even if a guard still consulted a stale set.

- [ ] **Step 2: Run them**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_lifecycle_races.py -q > /tmp/t6.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t6.log
```

- [ ] **Step 3: Prove races 1-4 fail against the pre-slice behaviour**

Three mutations, each restored before the next:

| Mutation | Must go red |
|---|---|
| `WorkspaceRetirement.retire` ignores `took_effect` and fans out unconditionally | races 1 and 3 |
| `WorkspaceLifecycle.transition` always yields True (drop the `<=` check) | race 2 |
| drop the `exists()` predicate from the setup-job cancel | race 4 |

**Race 5 is deliberately exempt.** Per-workspace isolation held before this slice — both tombstone sets were already keyed by workspace — so no mutation of C0's mechanism can make it fail. It is a scope regression check: it catches a future change that makes a transition process-wide. Say this in your report rather than inventing a mutation for it.

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
- A superseded retirement runs **no hook at all**, and the lifecycle lock is held across the fan-out so a transition cannot land mid-teardown. Both proven by mutation.
- `WorkspaceLifecycle` is monotonic in both directions, duplicates are no-ops, and the concurrent-convergence test delivers the highest generation FIRST so that deleting the check makes it fail.
- `MANAGER._retired_ws` and `registry._retired` no longer exist: `grep -rn "_retired_ws" src/` and `grep -rn "self\._retired\b" src/` are both empty. Collab, turns, the coordinator and `app.state.workspace_lifecycle` are proven to be the **same** object.
- The jobs hook's cancellations carry a generation predicate in SQL, and a thread that the hook's own timeout orphaned updates **zero rows** — with the test proving the thread was actually parked before the generation moved.
- All five race scenarios in spec 4.8 pass. Races 1-4 were each shown to fail against the pre-slice behaviour; race 5 is recorded as a scope regression check that no C0 mutation can falsify.
- `uv run pytest -n 16 --dist loadfile` green at the re-derived baseline plus this slice's tests; `uv run ruff check .` clean; `schema.gen.ts` shows no drift.
- One migration (`0062`), no `rmtree`, nothing removed, **no restore route and no UI** — those are C1.

## Handed to C1

- Restore's live path calls `lifecycle.apply(ws, generation=..., retired=False)` after its own commit. That call may block for as long as a concurrent retirement's fan-out (bounded by the per-hook budgets `_RETIREMENT_BARRIER_SECONDS` is derived from — 5.5s worst case). That wait IS the serialization: it is what makes a restore land after the teardown it supersedes rather than underneath it. C1's route must be able to afford it.
- Nothing un-retires a workspace in C0. `apply(..., retired=False)` exists and is tested, but no production caller reaches it until C1.
