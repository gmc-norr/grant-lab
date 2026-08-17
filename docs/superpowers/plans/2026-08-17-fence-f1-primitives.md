# Fence F1 — Primitives and Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the two fence primitives, the `WorkspaceGenerationStale` exception,
and the `claimed_at_generation` column on all 7 live job tables — with **no call
sites converted**, so the branch is provably inert.

**Architecture:** A new module `proposal_assist/fencing.py` holds both primitives
and the exception, so slices F3–F9 have one import and the F2 coverage gate has one
name to grep. Primitive A is a SQLAlchemy predicate that takes no lock. Primitive B
wraps the commit mutex with an under-mutex generation re-read. The migration adds a
nullable column; NULL means "legacy, unfenced".

**Tech Stack:** Python 3.12, SQLAlchemy 2.x, Alembic, pytest + testcontainers Postgres.

## Global Constraints

These bind every task. Copied from
`docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md` (revision 3).

1. **F1 CONVERTS NO CALL SITES.** Not one. No existing function changes behaviour.
   The whole branch must be inert: the only reason the test count moves is the new
   tests. If a task's diff modifies a caller, that task is wrong.
2. **The fence predicate is NOT `_still_at_generation`, and the two must never be
   merged.** `jobs_retirement.py:62`'s `_still_at_generation` matches a workspace
   that is **deleted** (the delete stamped `soft_deleted_at` before the fan-out
   runs). The new `live_at_generation` requires `soft_deleted_at IS NULL`. Adding
   the liveness clause to the retirement predicate would make every cancel a no-op
   and silently disarm the entire retirement fan-out. They look like duplicates
   and are opposites. Do not "DRY" them. Task 1 Step 7 pins this with a test.
3. **A guarded mutation must be ONE statement.** A predicate on a candidate SELECT
   with the write in a separate UPDATE does not guard the write: both run in one
   READ COMMITTED transaction and each takes a fresh snapshot. Shipped as a defect
   once (fixed `28abbc4`), and again (fixed `701fbb7`).
4. **The generation is compared for EQUALITY, never `>` or `>=`.** A generation
   lower than expected is impossible under correct operation; accepting it waves
   through exactly the corruption the fence exists to detect.
5. **`claimed_at_generation` is nullable with NO server_default.** Generation `0`
   is a real value — `workspaces.lifecycle_generation` has `server_default=text("0")`,
   so a fresh workspace *is* at generation 0. A default of 0 would assert "claimed
   at generation 0" about every legacy row, which is a lie. NULL means "legacy,
   unfenced".
6. **Alembic head is `0063`.** The new migration is `0064`, `down_revision = "0063"`.
7. `uv run ruff check .` is the gate. `ruff format --check` is NOT — 185 files are
   already unformatted on master. Do not reformat anything.
8. Fast test runs: `uv run pytest -n 16 --dist loadfile`. Never `-n auto` (resolves
   to 72 here) and never `--dist loadgroup`.
9. Never pipe a test command into `tail`/`head` — the pipeline exit status becomes
   the pager's and a failing suite reports success. Use
   `cmd > /tmp/out.log 2>&1; echo "EXIT=$?"; tail -40 /tmp/out.log`.
10. pytest exits **4** on "no tests ran", so a path typo reads as success. Always
    confirm a nonzero collected count.

## Baseline

Master is at `bc1bdec`. Expected baseline: **`3501 passed, 1 skipped`**. The
`1 skipped` is the signal (an `ANTHROPIC_API_KEY`-gated real-SDK test). Re-derive
before Task 1; if the tree is not green, STOP.

## File Structure

| File | Responsibility |
|---|---|
| `src/call_manager/proposal_assist/fencing.py` | **New.** Both primitives + `WorkspaceGenerationStale`. The doctrinal home; F2's gate greps these names. |
| `src/call_manager/db/models.py` | **Modify.** `claimed_at_generation` on 7 job models. |
| `alembic/versions/0064_claimed_at_generation.py` | **New.** The column on 7 tables. |
| `tests/proposal_assist/test_fencing.py` | **New.** Both primitives, the exception, and the anti-merge pin. |
| `tests/test_migration_0064_claimed_at_generation.py` | **New.** Column presence at head + round trip. |

`locking.py` is deliberately **not** where the primitives live: it owns advisory-lock
mechanics, Primitive A takes no lock, and a separate module gives F2 one import to
key on. `WorkspaceGenerationStale` sits beside the primitives rather than beside
`WorkspaceDeleted` in `locking.py` for the same reason; Task 1 Step 5 documents the
asymmetry in the code.

---

### Task 1: Primitive A and the exception

**Files:**
- Create: `src/call_manager/proposal_assist/fencing.py`
- Test: `tests/proposal_assist/test_fencing.py`

**Interfaces:**
- Consumes: `call_manager.db.models.Workspace` (has `id`, `soft_deleted_at`,
  `lifecycle_generation`).
- Produces:
  - `live_at_generation(workspace_id: uuid.UUID, generation: int)` → a SQLAlchemy
    `Exists` clause, usable directly in a `.where(...)`.
  - `WorkspaceGenerationStale(workspace_id, *, expected: int, actual: int)` with
    attributes `.workspace_id`, `.expected`, `.actual`.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_fencing.py`:

```python
"""Fence F1 — Primitive A (`live_at_generation`) and `WorkspaceGenerationStale`.

Primitive A is a PREDICATE, not a function that runs a query. It is meant to be
dropped into the WHERE of the statement that performs the effect, so the fence
and the write are one statement (spec 3.1).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_manager.db.models import Workspace
from call_manager.proposal_assist.fencing import (
    WorkspaceGenerationStale,
    live_at_generation,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed(pg_session_factory) -> uuid.UUID:
    with pg_session_factory() as s:
        owner = make_user(s, email=f"fence-{uuid.uuid4().hex[:8]}@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        return ws.id


def _matches(pg_session_factory, ws_id: uuid.UUID, generation: int) -> bool:
    """Evaluate the predicate exactly as a caller would: inside a statement."""
    with pg_session_factory() as s:
        return bool(s.execute(select(live_at_generation(ws_id, generation))).scalar())


def test_matches_a_live_workspace_at_its_current_generation(pg_session_factory):
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        gen = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()
    assert _matches(pg_session_factory, ws_id, gen) is True


def test_does_not_match_a_different_generation(pg_session_factory):
    """The whole point: an actor holding a superseded generation is refused."""
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        gen = s.execute(
            select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
        ).scalar_one()
    assert _matches(pg_session_factory, ws_id, gen + 1) is False


def test_does_not_match_a_generation_below_the_current_one(pg_session_factory):
    """Equality, not `>=`. A LOWER generation is impossible under correct
    operation, so accepting it would wave through the corruption the fence
    exists to detect (Global Constraint 4)."""
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.lifecycle_generation = 5
        s.commit()
    assert _matches(pg_session_factory, ws_id, 4) is False
    assert _matches(pg_session_factory, ws_id, 5) is True


def test_does_not_match_a_deleted_workspace_even_at_the_same_generation(
    pg_session_factory,
):
    """THE ANTI-MERGE PIN (Global Constraint 2).

    `jobs_retirement._still_at_generation` deliberately OMITS the liveness
    clause, because it runs during the delete fan-out when `soft_deleted_at` is
    ALREADY stamped — it must match a DELETED workspace. This predicate must
    NOT. The two look like duplicates and are opposites; adding liveness to the
    retirement one would make every cancel a no-op and silently disarm the
    retirement fan-out.

    Generation is held EQUAL here on purpose, so the only thing this test can be
    measuring is the liveness clause.
    """
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        gen = ws.lifecycle_generation
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()
    assert _matches(pg_session_factory, ws_id, gen) is False


def test_does_not_match_a_missing_workspace(pg_session_factory):
    """A workspace that is not there is never fenced-through."""
    assert _matches(pg_session_factory, uuid.uuid4(), 0) is False


def test_generation_zero_is_a_real_generation(pg_session_factory):
    """`lifecycle_generation` has server_default 0, so a fresh workspace IS at
    generation 0. A fence that treated 0 as 'unset' would pass everything
    (Global Constraint 5)."""
    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        assert (
            s.execute(
                select(Workspace.lifecycle_generation).where(Workspace.id == ws_id)
            ).scalar_one()
            == 0
        ), "seed assumption broken: a fresh workspace is no longer at generation 0"
    assert _matches(pg_session_factory, ws_id, 0) is True


def test_generation_stale_carries_both_generations():
    """The exception must say what was expected AND what was found — a bare
    'stale' gives an operator nothing to reason about."""
    ws_id = uuid.uuid4()
    exc = WorkspaceGenerationStale(ws_id, expected=3, actual=7)
    assert exc.workspace_id == ws_id
    assert exc.expected == 3
    assert exc.actual == 7
    assert "3" in str(exc) and "7" in str(exc)


def test_generation_stale_is_not_a_workspace_deleted():
    """Distinct types: a caller must be able to tell 'your epoch ended' from
    'the workspace is gone' — they converge differently (spec 7)."""
    from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted

    exc = WorkspaceGenerationStale(uuid.uuid4(), expected=1, actual=2)
    assert not isinstance(exc, WorkspaceDeleted)
    with pytest.raises(WorkspaceGenerationStale):
        raise exc


def test_the_retirement_predicate_and_the_fence_predicate_are_opposites(
    pg_session_factory,
):
    """THE ANTI-MERGE PIN, behavioural (Global Constraint 2).

    On ONE deleted workspace at generation G, held at the SAME generation so
    liveness is the only variable:

        live_at_generation(ws, G)     -> False   (fence: refuse deleted)
        _still_at_generation(ws, G)   -> True    (retirement: act on deleted)

    They are not duplicates. They are opposites. `_still_at_generation` runs
    during the delete fan-out, when soft_deleted_at is ALREADY stamped, so
    giving it a liveness clause — or rewriting its body as
    `return live_at_generation(...)` — makes every cancel a no-op and disarms
    the retirement fan-out.

    This test is BEHAVIOURAL on purpose. A structural check that greps the
    function body for "soft_deleted_at" is defeated by exactly the refactor it
    is meant to stop, because `return live_at_generation(...)` contains no such
    text. It also lives HERE rather than in test_fencing_inertness.py, because
    F2 deletes that file and this invariant is permanent.

    Importing a private helper is deliberate: this test exists to pin that
    private's contract against the public one.
    """
    from call_manager.proposal_assist.jobs_retirement import _still_at_generation

    ws_id = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        gen = ws.lifecycle_generation
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        fence = bool(s.execute(select(live_at_generation(ws_id, gen))).scalar())
        retire = bool(s.execute(select(_still_at_generation(ws_id, gen))).scalar())

    assert fence is False, "the fence predicate must REFUSE a deleted workspace"
    assert retire is True, (
        "the retirement predicate must MATCH a deleted workspace — it runs "
        "during the delete fan-out. If this is False, someone gave it a "
        "liveness clause and every cancel is now a no-op."
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_fencing.py -q > /tmp/f1t1.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t1.log
```
Expected: collection error — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.fencing'`.

- [ ] **Step 3: Create the module**

Create `src/call_manager/proposal_assist/fencing.py`:

```python
"""Epoch fencing — the two primitives every belief-carrying actor uses.

Spec: docs/superpowers/specs/2026-08-17-lifecycle-fence-design.md (revision 3).

THE INVARIANT: a semantic effect produced from a belief formed earlier must
carry the lifecycle generation of that belief and validate it, durably in
Postgres, at the moment the effect takes place.

Two primitives, chosen by what the effect touches:

  A. `live_at_generation` — a PREDICATE, no lock. For effects that are purely DB
     rows. Drop it into the WHERE of the statement that performs the effect, so
     the fence and the write are ONE statement. This is the default: it cannot
     create a lock-order problem because it takes no lock.

  B. `acquire_commit_mutex_at_generation` — mutex + under-mutex re-read. For
     effects spanning git, the filesystem, or multiple rows that must move
     together.

Where a transaction ALREADY holds the workspace row under FOR UPDATE, use
neither: assert the generation on that already-locked row. Acquiring the mutex
afterwards inverts the normative M -> J -> W order and opens a real Postgres
40P01 against the delete path (spec 5).

`WorkspaceGenerationStale` lives here rather than beside `WorkspaceDeleted` in
drafting/locking.py because it is raised only by fencing code, and F2's coverage
gate keys on this module. The asymmetry is deliberate.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists

from call_manager.db.models import Workspace


class WorkspaceGenerationStale(RuntimeError):
    """The workspace has moved past the generation the caller carried.

    NOT an error condition. It means "your epoch is over; produce no further
    semantic effect." It never reaches a user as a 5xx.

    It does NOT mean "write nothing". An actor that catches this must still
    record cleanup and accounting — tokens, dollars, terminal status, releasing
    an active-unique slot — via conditional writes only (spec 7). Abandoning
    silently strands the job holding its slot and destroys deliberate late token
    accounting (see review/pass_runner.py, which keeps the tokens a cancelled
    run's sub-call spent on purpose).
    """

    def __init__(self, workspace_id: uuid.UUID, *, expected: int, actual: int) -> None:
        super().__init__(
            f"workspace {workspace_id} moved from generation {expected} to {actual}"
        )
        self.workspace_id = workspace_id
        self.expected = expected
        self.actual = actual


def live_at_generation(workspace_id: uuid.UUID, generation: int):
    """Primitive A. "Is this workspace LIVE and STILL at *generation*?", as a
    predicate that governs the WRITE.

    Use it INSIDE the statement that performs the effect:

        session.execute(
            update(SomeJob)
            .where(SomeJob.id == job_id, live_at_generation(ws_id, gen))
            .values(status="done")
        )

    NOT as a separate check before the write. A predicate evaluated in its own
    statement does not guard a later one: both run in a single READ COMMITTED
    transaction and each takes a FRESH snapshot, so a transition committing
    between them is invisible to the check and fully effective on the write.
    That defect shipped twice (fixed in 28abbc4 and again in 701fbb7).

    NOT INTERCHANGEABLE with `jobs_retirement._still_at_generation`, which
    deliberately omits the liveness clause: it runs during the delete fan-out,
    when `soft_deleted_at` is already stamped, so it must match a DELETED
    workspace. Adding liveness there would make every cancel a no-op and
    silently disarm the retirement fan-out. The two predicates look like
    duplicates and are opposites. Do not merge them.

    Generation is compared for EQUALITY. A generation lower than the current one
    is impossible under correct operation, so `>=` would wave through exactly the
    corruption this exists to detect. Note that 0 is a real generation:
    `Workspace.lifecycle_generation` has server_default 0, so a fresh workspace
    IS at generation 0 and must match.
    """
    return exists().where(
        Workspace.id == workspace_id,
        Workspace.soft_deleted_at.is_(None),
        Workspace.lifecycle_generation == generation,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/proposal_assist/test_fencing.py -q > /tmp/f1t1.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t1.log
```
Expected: `9 passed`. Confirm the collected count is 9 — pytest exits 4, not 1, when a path typo means nothing ran.

- [ ] **Step 5: Mutation — remove the liveness clause**

Edit `live_at_generation` to drop `Workspace.soft_deleted_at.is_(None)`:

```python
    return exists().where(
        Workspace.id == workspace_id,
        Workspace.lifecycle_generation == generation,
    )
```

Run the file again. **Required outcome:** exactly TWO tests go RED —
`test_does_not_match_a_deleted_workspace_even_at_the_same_generation` and
`test_the_retirement_predicate_and_the_fence_predicate_are_opposites`. Both hold
the generation equal, so the liveness clause is the only thing either can be
measuring. If either stays green it is decorative and the task is FAILED — fix
the test, not the report.

Revert the mutation. Re-run and confirm `9 passed`. Confirm `git status` is clean
of the mutation.

- [ ] **Step 6: Mutation — relax equality to `>=`**

Edit `live_at_generation` to use `Workspace.lifecycle_generation >= generation`.

Run the file again. **Required outcome:** exactly
`test_does_not_match_a_generation_below_the_current_one` goes RED.

Revert. Re-run and confirm `9 passed`. Confirm `git status` is clean.

- [ ] **Step 6b: Mutation — rewrite the retirement predicate in terms of the fence**

This is the refactor a structural grep cannot see, and the one most likely to be
proposed in good faith. In `jobs_retirement.py`, replace `_still_at_generation`'s
body with:

```python
    from call_manager.proposal_assist.fencing import live_at_generation

    return live_at_generation(workspace_id, generation)
```

Run:
```bash
uv run pytest tests/proposal_assist/test_fencing.py -q > /tmp/f1t1b.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t1b.log
```
**Required outcome:** exactly
`test_the_retirement_predicate_and_the_fence_predicate_are_opposites` goes RED,
on its second assertion.

Then, still with the mutation applied, confirm the second line of defence:
```bash
uv run pytest tests/proposal_assist/test_phase0_retirement.py -q > /tmp/f1t1c.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t1c.log
```
**Required outcome:** RED. `test_the_hook_cancels_queued_and_running_setup_jobs`
and `test_the_hook_cancels_a_job_waiting_for_rubric_review` both seed a workspace
with `soft_deleted_at` set and a NON-terminal job, and assert
`job.status == "cancelled"` — a no-op cancel leaves the job in its seeded state
and fails them. Report the exact list. If that suite stays GREEN, say so
prominently: it would mean the retirement suite has a hole and the new pin is the
only protection.

Revert the mutation. Re-run both commands and confirm green. Confirm
`git diff --stat` no longer lists `jobs_retirement.py`.

- [ ] **Step 7: Verify the retirement predicate was not touched**

Run:
```bash
git diff --stat
```
Expected: exactly two files — `src/call_manager/proposal_assist/fencing.py` and
`tests/proposal_assist/test_fencing.py`. **`jobs_retirement.py` must NOT appear.**
If it does, Global Constraint 2 was violated; revert that file entirely.

- [ ] **Step 8: Run lint and commit**

```bash
uv run ruff check . > /tmp/f1ruff.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/f1ruff.log
git add src/call_manager/proposal_assist/fencing.py tests/proposal_assist/test_fencing.py
git commit -m "feat(fencing): Primitive A + WorkspaceGenerationStale (F1 Task 1)"
```
Expected: `RUFF_EXIT=0`. Read the exit code, not the output — piping into `tail`
reports the pager's status, the same trap Global Constraint 9 names.

---

### Task 2: Primitive B — mutex + generation

**Files:**
- Modify: `src/call_manager/proposal_assist/fencing.py` (append)
- Test: `tests/proposal_assist/test_fencing_mutex.py`

**Interfaces:**
- Consumes: from Task 1, `WorkspaceGenerationStale(workspace_id, *, expected, actual)`.
  From `proposal_assist/drafting/locking.py`: `acquire_commit_mutex(session,
  workspace_id, *, timeout_seconds=10)`, `CommitMutexTimeout`, `WorkspaceDeleted`.
- Produces: `acquire_commit_mutex_at_generation(session, workspace_id, *,
  generation: int, timeout_seconds: int = 10) -> None`.

**Context you need:** `acquire_commit_mutex_live` (`locking.py:267`) is the model to
follow. It acquires the mutex, then re-reads `soft_deleted_at` with a **scalar
SELECT** — never `session.get()`, which can return the identity-map instance the
caller loaded *before* blocking on the mutex, which is precisely the stale state the
guard exists to defeat. Primitive B re-reads **both** columns in **one** statement,
for the reason `restore_workspace_db` was fixed in `701fbb7`: reading the generation
off a pre-mutex instance returns the pre-race value.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_fencing_mutex.py`:

```python
"""Fence F1 — Primitive B (`acquire_commit_mutex_at_generation`).

The interleaving test is the substance. The three sequential tests all pass
against an implementation that reads the generation off the pre-mutex instance;
only the contended one exposes it. That exact defect shipped in Slice C1 and was
fixed in 701fbb7.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, update

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import (
    WorkspaceDeleted,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.fencing import (
    WorkspaceGenerationStale,
    acquire_commit_mutex_at_generation,
)
from tests.proposal_assist.conftest import make_user, make_workspace


def _seed(pg_session_factory) -> tuple[uuid.UUID, int]:
    with pg_session_factory() as s:
        owner = make_user(s, email=f"fenceb-{uuid.uuid4().hex[:8]}@ki.se")
        ws = make_workspace(s, owner=owner)
        s.commit()
        return ws.id, ws.lifecycle_generation


def test_acquires_and_returns_when_the_generation_matches(pg_session_factory):
    ws_id, gen = _seed(pg_session_factory)
    with pg_session_factory() as s:
        acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
        s.rollback()


def test_raises_stale_when_the_generation_moved_on(pg_session_factory):
    ws_id, gen = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.lifecycle_generation = gen + 3
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceGenerationStale) as ei:
            acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
        s.rollback()
    assert ei.value.expected == gen
    assert ei.value.actual == gen + 3


def test_raises_stale_when_the_carried_generation_is_higher(pg_session_factory):
    """Equality, not `>`. A carried generation ABOVE the current one cannot
    happen under correct operation, so it is corruption, not a pass
    (Global Constraint 4)."""
    ws_id, gen = _seed(pg_session_factory)
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceGenerationStale):
            acquire_commit_mutex_at_generation(s, ws_id, generation=gen + 1)
        s.rollback()


def test_raises_deleted_not_stale_for_a_deleted_workspace(pg_session_factory):
    """Deleted takes PRECEDENCE over stale, and the test must model reality to
    prove it.

    A real deletion (`soft_delete_workspace_db`) both stamps `soft_deleted_at`
    AND bumps the generation, so after one, BOTH conditions hold at once. This
    test therefore sets both. Setting only `soft_deleted_at` — leaving the
    generation equal — would make the test unfalsifiable: an implementation that
    checked generation first would still pass it, then raise
    WorkspaceGenerationStale after every real deletion and send callers down the
    quiet-abandon path instead of the 404 path.
    """
    ws_id, gen = _seed(pg_session_factory)
    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        ws.soft_deleted_at = datetime.now(UTC)
        ws.lifecycle_generation = gen + 1  # what a real delete does
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
        s.rollback()


def test_raises_deleted_for_a_missing_workspace(pg_session_factory):
    """A missing workspace is never writable — same treatment as
    acquire_commit_mutex_live gives it."""
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            acquire_commit_mutex_at_generation(s, uuid.uuid4(), generation=0)
        s.rollback()


def test_the_generation_is_read_UNDER_the_mutex_not_before_it(
    pg_session_factory, migrated_engine
):
    """THE INTERLEAVING TEST — the only one that bites.

    The caller loads the workspace BEFORE blocking on the mutex (the normal
    shape: you look something up, then you go to write it). While it is blocked,
    another transaction bumps the generation and commits. The re-read must
    therefore happen UNDER the mutex and see the new value.

    The seam fires on `pg_advisory_xact_lock` — after the caller's pre-mutex
    load, before it holds the mutex. That is the only window in which the
    concurrent transition can land. The caller holds no locks at that instant,
    so the competing session takes the same mutex, commits and releases it
    inside the callback without deadlocking.

    The competing session takes the mutex FOR REAL, because that is what a real
    lifecycle transition does — `bump_lifecycle_generation` requires its caller
    to hold it, which is what makes the mutex the total order over generations.
    A plain UPDATE here would still exercise fresh-read ordering, but it would
    NOT exercise the serialization the docstring claims, and the two are
    different properties.

    Same seam and same shape as
    tests/test_workspace_restore_acceptance.py's test 11, which caught the
    identical pre-mutex-read defect in restore_workspace_db (701fbb7).
    """
    ws_id, gen = _seed(pg_session_factory)
    state = {"fired": False}

    def _bump_from_another_connection():
        # Mutex-held, exactly as a real transition is. The latch below keeps
        # this session's own pg_advisory_xact_lock from re-entering the seam.
        with pg_session_factory() as other:
            acquire_commit_mutex(other, ws_id)
            other.execute(
                update(Workspace)
                .where(Workspace.id == ws_id)
                .values(lifecycle_generation=Workspace.lifecycle_generation + 1)
            )
            other.commit()

    def _before(conn, cursor, statement, parameters, context, executemany):
        # The latch keeps the bump's own statements from recursing.
        if state["fired"] or "pg_advisory_xact_lock" not in statement:
            return
        state["fired"] = True
        _bump_from_another_connection()

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            # Pre-mutex load: this instance caches lifecycle_generation == gen.
            ws = s.get(Workspace, ws_id)
            assert ws.lifecycle_generation == gen
            with pytest.raises(WorkspaceGenerationStale) as ei:
                acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
            s.rollback()
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"
    assert ei.value.actual == gen + 1, (
        f"the fence read generation {ei.value.actual} — the value cached before "
        f"the mutex — instead of the committed {gen + 1}"
    )


def test_a_deleted_workspace_observed_only_under_the_mutex_still_raises(
    pg_session_factory, migrated_engine
):
    """Same seam, deletion instead of a bump: the liveness half of the re-read
    must also happen under the mutex, not off the pre-mutex instance."""
    ws_id, gen = _seed(pg_session_factory)
    state = {"fired": False}

    def _delete_from_another_connection():
        with pg_session_factory() as other:
            ws = other.get(Workspace, ws_id)
            ws.soft_deleted_at = datetime.now(UTC)
            other.commit()

    def _before(conn, cursor, statement, parameters, context, executemany):
        if state["fired"] or "pg_advisory_xact_lock" not in statement:
            return
        state["fired"] = True
        _delete_from_another_connection()

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            ws = s.get(Workspace, ws_id)
            assert ws.soft_deleted_at is None
            with pytest.raises(WorkspaceDeleted):
                acquire_commit_mutex_at_generation(s, ws_id, generation=gen)
            s.rollback()
    finally:
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
uv run pytest tests/proposal_assist/test_fencing_mutex.py -q > /tmp/f1t2.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t2.log
```
Expected: collection error — `ImportError: cannot import name 'acquire_commit_mutex_at_generation'`.

- [ ] **Step 3: Append Primitive B to the module**

Add these imports to the top of `src/call_manager/proposal_assist/fencing.py`
(beside the existing ones):

```python
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from call_manager.proposal_assist.drafting.locking import (
    WorkspaceDeleted,
    acquire_commit_mutex,
)
```

Append to the end of the file:

```python
def acquire_commit_mutex_at_generation(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    generation: int,
    timeout_seconds: int = 10,
) -> None:
    """Primitive B. Acquire the commit mutex, then refuse a workspace that is
    deleted or has moved past *generation*.

    For effects spanning git, the filesystem, or multiple rows that must move
    together. For DB-only effects prefer `live_at_generation`, which takes no
    lock and therefore cannot create a lock-order problem.

    LOCK ORDER: this acquires the mutex (M). It must be called BEFORE any setup
    job (J) or workspace (W) row lock, preserving the normative M -> J -> W
    (phases/setup.py:500, web/api/rubric.py:341). Calling it after a job or
    workspace row lock inverts the order against the delete path and opens a
    real Postgres 40P01. Where the caller ALREADY holds the workspace row under
    FOR UPDATE, do not call this at all — assert the generation on that
    already-locked row.

    Never hold this across context assembly or an LLM call.

    Both columns come from ONE statement, and it is a scalar SELECT rather than
    `session.get()`. `session.get()` can return the identity-map instance the
    caller loaded BEFORE blocking on the mutex, which carries the pre-race
    generation — precisely the staleness this guard exists to defeat. That
    defect shipped in Slice C1 and was fixed in 701fbb7.

    Raises CommitMutexTimeout exactly as acquire_commit_mutex does,
    WorkspaceDeleted if the workspace is gone or deleted, or
    WorkspaceGenerationStale if it is live at a different generation. Mutates
    nothing in every case; the caller rolls back.

    Deleted takes precedence over stale: a deletion also bumps the generation, so
    both conditions hold at once, and callers map them to different outcomes
    (404 versus a quiet abandon-with-accounting).
    """
    acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)
    row = session.execute(
        select(Workspace.soft_deleted_at, Workspace.lifecycle_generation).where(
            Workspace.id == workspace_id
        )
    ).first()
    if row is None or row[0] is not None:
        raise WorkspaceDeleted(workspace_id)
    if row[1] != generation:
        raise WorkspaceGenerationStale(
            workspace_id, expected=generation, actual=row[1]
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
uv run pytest tests/proposal_assist/test_fencing_mutex.py -q > /tmp/f1t2.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t2.log
```
Expected: `7 passed`. Confirm the collected count is 7.

- [ ] **Step 5: Mutation — read the generation off the pre-mutex instance**

Replace the body's re-read with the stale shape:

```python
    acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.soft_deleted_at is not None:
        raise WorkspaceDeleted(workspace_id)
    if ws.lifecycle_generation != generation:
        raise WorkspaceGenerationStale(
            workspace_id, expected=generation, actual=ws.lifecycle_generation
        )
```

Run the file again. **Required outcome:** exactly
`test_the_generation_is_read_UNDER_the_mutex_not_before_it` goes RED, and the six
others stay green — that asymmetry is the proof that only the interleaving test
bites.

> Note for the implementer: `session.get()` returns the cached instance only
> because the test loaded it pre-mutex. If BOTH interleaving tests go red, that is
> also acceptable and means the mutation is stronger than expected — report which.
> If the interleaving test stays GREEN, the seam is wrong: the trigger is firing at
> the wrong instant. Fix the seam, do not accept "equivalent".

Revert. Re-run and confirm `7 passed`. Confirm `git status` is clean.

- [ ] **Step 6: Mutation — relax the comparison to `>`**

Change `if row[1] != generation:` to `if row[1] > generation:`.

Run the file again. **Required outcome:** exactly
`test_raises_stale_when_the_carried_generation_is_higher` goes RED.

Revert. Re-run and confirm `7 passed`. Confirm `git status` is clean.

- [ ] **Step 6b: Mutation — check the generation BEFORE liveness**

Swap the two guards so the generation check runs first:

```python
    if row is None:
        raise WorkspaceDeleted(workspace_id)
    if row[1] != generation:
        raise WorkspaceGenerationStale(
            workspace_id, expected=generation, actual=row[1]
        )
    if row[0] is not None:
        raise WorkspaceDeleted(workspace_id)
```

Run the file again. **Required outcome:**
`test_raises_deleted_not_stale_for_a_deleted_workspace` goes RED with
`DID NOT RAISE`/`WorkspaceGenerationStale` — because a real deletion bumps the
generation too, so the reordered check reaches the stale branch first.

If it stays GREEN, that test is still setting only `soft_deleted_at` and the
precedence contract is unproven. Fix the test.

Revert. Re-run and confirm `7 passed`. Confirm `git status` is clean.

- [ ] **Step 7: Run lint and commit**

```bash
uv run ruff check . > /tmp/f1ruff.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/f1ruff.log
```
Expected: `RUFF_EXIT=0` and `All checks passed!`. Read the exit code, not the
output — `ruff check . | tail` reports the pager's status, exactly the trap
Global Constraint 9 names for test commands.

```bash
git add src/call_manager/proposal_assist/fencing.py tests/proposal_assist/test_fencing_mutex.py
git commit -m "feat(fencing): Primitive B, mutex + under-mutex generation re-read (F1 Task 2)"
```

---

### Task 3: `claimed_at_generation` on the 7 live job tables

**Files:**
- Create: `alembic/versions/0064_claimed_at_generation.py`
- Modify: `src/call_manager/db/models.py` — `TurnJob` (:879), `CoherenceJob` (:936),
  `EvaluatorJob` (:976), `UncitedClaimsJob` (:1025), `WorkspaceSetupJob` (:1404),
  `ReviewRun` (:1485), `ClaimAnalysisJob` (:1802)
- Test: `tests/test_migration_0064_claimed_at_generation.py`

**Interfaces:**
- Consumes: nothing from Tasks 1–2. This task is independent of them.
- Produces: `<Model>.claimed_at_generation: Mapped[int | None]` on all 7 models,
  which F4–F7 set at claim time.

**Context you need:**
- Alembic head is `0063` (`alembic/versions/0063_setup_job_cancelled_from.py`).
  Use it as the file template.
- **`AttachmentJob` is deliberately EXCLUDED.** It is dead schema — no production
  reference outside `db/models.py`; its only consumers are model tests. It has no
  claim path and no belief-carrying commit. Seven tables, not eight. Adding it is a
  spec violation, not thoroughness.
- **No `server_default`.** Generation 0 is real (`Workspace.lifecycle_generation`
  has `server_default=text("0")`), so defaulting to 0 would assert "claimed at
  generation 0" about every legacy row. NULL means "legacy, unfenced".

- [ ] **Step 1: Write the failing test**

Create `tests/test_migration_0064_claimed_at_generation.py`:

```python
"""Migration 0064: claimed_at_generation on the 7 live job tables.

Asserts the column exists at head on every table with the right properties, that
the dead AttachmentJob table is NOT touched, and that the migration round-trips.
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import Integer, create_engine, inspect, text

from alembic import command

# The 7 LIVE job tables. attachment_jobs is deliberately absent: dead schema, no
# production reference outside db/models.py, no claim path.
FENCED_TABLES = [
    "turn_jobs",
    "coherence_jobs",
    "evaluator_jobs",
    "uncited_claims_jobs",
    "workspace_setup_jobs",
    "review_runs",
    "claim_analysis_jobs",
]


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_claimed_at_generation_present_on_every_fenced_table(migrated_engine):
    insp = inspect(migrated_engine)
    for table in FENCED_TABLES:
        cols = {c["name"]: c for c in insp.get_columns(table)}
        assert "claimed_at_generation" in cols, (
            f"{table} is missing claimed_at_generation"
        )
        col = cols["claimed_at_generation"]
        assert col["nullable"], (
            f"{table}.claimed_at_generation must be NULLABLE — rows in flight when "
            f"the migration lands have no generation to record"
        )
        assert col["default"] is None, (
            f"{table}.claimed_at_generation must have NO server_default, got "
            f"{col['default']!r}. Generation 0 is a REAL generation "
            f"(workspaces.lifecycle_generation defaults to 0), so a default of 0 "
            f"would assert 'claimed at generation 0' about every legacy row."
        )


def test_the_dead_attachment_jobs_table_is_not_touched(migrated_engine):
    """attachment_jobs is dead schema (no production reference outside
    db/models.py). Adding the column there is scope creep the spec excludes."""
    insp = inspect(migrated_engine)
    cols = {c["name"] for c in insp.get_columns("attachment_jobs")}
    assert "claimed_at_generation" not in cols, (
        "attachment_jobs must NOT get claimed_at_generation — it is dead schema "
        "with no claim path (spec 4.5)"
    )


def test_every_model_declares_the_column_matching_the_migration():
    """ORM/migration agreement. NOTHING ELSE IN THIS REPO CHECKS THIS.

    There is no metadata-vs-schema comparison anywhere in the suite —
    test_alembic_v2_round_trip.py round-trips 0005-0008 and asserts specific
    named columns; it does not diff Base.metadata against head. So an
    implementation that adds all seven DATABASE columns and zero MODEL
    properties passes every other test in this plan, and the drift only surfaces
    in F4-F7 when a claim path tries to set an attribute that does not exist.

    Reads the mapped columns directly, so it needs no database.
    """
    from call_manager.db.models import (
        AttachmentJob,
        ClaimAnalysisJob,
        CoherenceJob,
        EvaluatorJob,
        ReviewRun,
        TurnJob,
        UncitedClaimsJob,
        WorkspaceSetupJob,
    )

    fenced_models = [
        TurnJob,
        CoherenceJob,
        EvaluatorJob,
        UncitedClaimsJob,
        WorkspaceSetupJob,
        ReviewRun,
        ClaimAnalysisJob,
    ]
    assert {m.__tablename__ for m in fenced_models} == set(FENCED_TABLES), (
        "the models checked here and the tables in the migration have drifted"
    )

    for model in fenced_models:
        col = model.__table__.c.get("claimed_at_generation")
        assert col is not None, (
            f"{model.__name__} has no claimed_at_generation property — the "
            f"migration adds the column but the ORM cannot see it"
        )
        assert col.nullable, f"{model.__name__}.claimed_at_generation must be nullable"
        assert col.server_default is None, (
            f"{model.__name__}.claimed_at_generation must have NO server_default "
            f"— 0 is a real generation"
        )
        assert isinstance(col.type, Integer), (
            f"{model.__name__}.claimed_at_generation must be Integer, got "
            f"{col.type!r}"
        )

    assert AttachmentJob.__table__.c.get("claimed_at_generation") is None, (
        "AttachmentJob must NOT declare claimed_at_generation — dead schema"
    )


def _cols(conn, table: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"
            ),
            {"t": table},
        )
    }


def test_migration_0064_round_trips(pg_url, pg_session_factory):
    """0064 applies over a row that predates it, rolls back, and re-applies.

    `pg_session_factory` is requested SOLELY for its teardown. The testcontainer
    database is SESSION-scoped and shared by the whole suite; that fixture's
    teardown is the only thing that TRUNCATEs. Without it this test leaks a live
    `queued` workspace_setup_jobs row that a later test's claim_next_setup_job
    can pick up.

    The seeding order matters and is the point: downgrade FIRST, insert at 0063,
    then upgrade — so the row genuinely predates the column. Seeding at head and
    calling the row "pre-existing" would prove nothing about existing data.

    Every step after the first downgrade is wrapped so the shared database is
    restored to head even on failure. Stranding it at 0063 would leave the next
    test's TRUNCATE and every ORM query facing a schema the models do not match,
    surfacing as opaque unrelated failures. Same precedent and same reasoning as
    test_alembic_v2_round_trip.py's try/finally.
    """
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0063")
        try:
            # Seed AT 0063 — before the column exists.
            with eng.begin() as c:
                c.execute(text("INSERT INTO users (email) VALUES ('mig64@ki.se')"))
                uid = c.execute(
                    text("SELECT id FROM users WHERE email='mig64@ki.se'")
                ).scalar_one()
                c.execute(
                    text(
                        "INSERT INTO workspaces (id, title, owner_id, status, "
                        "current_phase) VALUES (gen_random_uuid(), 'W64', :u, "
                        "'drafting', 'setup')"
                    ),
                    {"u": uid},
                )
                ws_id = c.execute(
                    text("SELECT id FROM workspaces WHERE title='W64'")
                ).scalar_one()
                c.execute(
                    text(
                        "INSERT INTO workspace_setup_jobs (id, workspace_id, status) "
                        "VALUES (gen_random_uuid(), :w, 'queued')"
                    ),
                    {"w": ws_id},
                )

            with eng.connect() as c:
                for table in FENCED_TABLES:
                    assert "claimed_at_generation" not in _cols(c, table), (
                        f"downgrade left claimed_at_generation on {table}"
                    )
        finally:
            command.upgrade(cfg, "head")

        with eng.connect() as c:
            for table in FENCED_TABLES:
                assert "claimed_at_generation" in _cols(c, table), (
                    f"re-upgrade did not restore claimed_at_generation on {table}"
                )
            # The row that predates the column survived, identified by ITS OWN id
            # — a global count would be hostage to any other row in the shared DB.
            got = c.execute(
                text(
                    "SELECT claimed_at_generation FROM workspace_setup_jobs "
                    "WHERE workspace_id = :w"
                ),
                {"w": ws_id},
            ).scalar_one()
        assert got is None, (
            f"a row that predates the column must read NULL (legacy, unfenced), "
            f"got {got!r} — a server_default would make this 0, which is a real "
            f"generation and would fence the row against a real epoch"
        )
    finally:
        eng.dispose()
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
uv run pytest tests/test_migration_0064_claimed_at_generation.py -q > /tmp/f1t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t3.log
```
Expected: FAIL — `turn_jobs is missing claimed_at_generation`.

- [ ] **Step 3: Write the migration**

Create `alembic/versions/0064_claimed_at_generation.py`:

```python
"""job tables: claimed_at_generation, the epoch an actor claimed in

Revision ID: 0064
Revises: 0063
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

# The 7 LIVE workspace-scoped job tables. attachment_jobs is deliberately
# EXCLUDED: dead schema with no production reference outside db/models.py and no
# claim path, so it has no belief to carry (spec 4.5).
FENCED_TABLES = (
    "turn_jobs",
    "coherence_jobs",
    "evaluator_jobs",
    "uncited_claims_jobs",
    "workspace_setup_jobs",
    "review_runs",
    "claim_analysis_jobs",
)


def upgrade() -> None:
    for table in FENCED_TABLES:
        op.add_column(
            table,
            # NULLABLE with NO server_default, deliberately. Rows already in
            # flight when this lands have no generation to record, and NULL means
            # "legacy, unfenced" — those rows fall back to the liveness fence and
            # the case disappears as they terminalize.
            #
            # A server_default of 0 would be WRONG, not merely redundant:
            # workspaces.lifecycle_generation itself defaults to 0, so 0 is a
            # REAL generation that a fresh workspace genuinely sits at. Defaulting
            # to 0 would assert "claimed at generation 0" about every legacy row,
            # which fences them against a real epoch instead of exempting them.
            sa.Column("claimed_at_generation", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    for table in reversed(FENCED_TABLES):
        op.drop_column(table, "claimed_at_generation")
```

- [ ] **Step 4: Add the column to the 7 models**

In `src/call_manager/db/models.py`, add this identical block to each of
`TurnJob` (:879), `CoherenceJob` (:936), `EvaluatorJob` (:976),
`UncitedClaimsJob` (:1025), `WorkspaceSetupJob` (:1404), `ReviewRun` (:1485),
`ClaimAnalysisJob` (:1802). Place it immediately after each class's `status`
column so the position is uniform and greppable:

```python
    # Fence F1 (spec 2026-08-17 §3.3). The workspaces.lifecycle_generation this
    # actor observed AT CLAIM TIME. Every semantic effect it later produces is
    # fenced against this value, so work belonging to an epoch the workspace has
    # left cannot commit. NULL = legacy row, claimed before the fence existed;
    # those fall back to the liveness check. NOT defaulted to 0 — 0 is a real
    # generation. Set by the claim statement in F4-F7; unused in F1.
    claimed_at_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

`Integer` and `Mapped`/`mapped_column` are already imported in this module — verify
with `grep -n "^from sqlalchemy import\|^from sqlalchemy.orm import" src/call_manager/db/models.py`
before assuming, and do not add a duplicate import.

- [ ] **Step 5: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/test_migration_0064_claimed_at_generation.py -q > /tmp/f1t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t3.log
```
Expected: `4 passed`.

- [ ] **Step 6: Run the full suite**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f1full.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/f1full.log
```
Expected: `3521 passed, 1 skipped` (baseline 3501 + 9 from Task 1 + 7 from Task 2
+ 4 from Task 3). The `1 skipped` must still be exactly 1. Any other total means
something beyond this plan changed — investigate before continuing.

Note what this run does NOT prove: nothing in the existing suite compares
`Base.metadata` against the migrated schema. `test_alembic_v2_round_trip.py`
round-trips 0005–0008 and asserts specific named columns; it does not diff
metadata. `test_every_model_declares_the_column_matching_the_migration` (Step 1)
is the only thing standing between this slice and a schema/ORM split, which is
why Step 9 mutates against it specifically.

- [ ] **Step 7: Mutation — give the column a server_default of 0**

In the migration, change the column to
`sa.Column("claimed_at_generation", sa.Integer(), nullable=True, server_default=sa.text("0"))`.

Run:
```bash
uv run pytest tests/test_migration_0064_claimed_at_generation.py -q > /tmp/f1t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t3.log
```
**Required outcome:** both
`test_claimed_at_generation_present_on_every_fenced_table` (the `default is None`
assertion) and `test_migration_0064_round_trips` (the legacy row reads NULL
assertion) go RED. Two tests, because this is the one mistake with two distinct
consequences — a wrong schema and a wrong value for existing rows.

Revert. Re-run and confirm `4 passed`. Confirm `git status` is clean.

- [ ] **Step 8: Mutation — add attachment_jobs to the table list**

Add `"attachment_jobs"` to `FENCED_TABLES` in the **migration only** (not the test).

Run the test file. **Required outcome:** exactly
`test_the_dead_attachment_jobs_table_is_not_touched` goes RED.

Revert. Re-run and confirm `4 passed`. Confirm `git status` is clean.

- [ ] **Step 9: Mutation — drop ONE model's column, keeping all 7 in the migration**

Remove the `claimed_at_generation` block from `ReviewRun` (:1485) only. The
migration keeps all seven tables, so the database is correct and only the ORM is
blind — the exact split no other test in this repo can see.

Run:
```bash
uv run pytest tests/test_migration_0064_claimed_at_generation.py -q > /tmp/f1t3b.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t3b.log
```
**Required outcome:** exactly
`test_every_model_declares_the_column_matching_the_migration` goes RED, naming
`ReviewRun`. The two `inspect()`-based tests must stay GREEN — they read the
database, which is still correct, and that contrast is the whole point of adding
this test.

Then confirm the gap is real by running the full suite with the mutation still
applied:
```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f1t3c.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/f1t3c.log
```
**Expected:** exactly one failure — the test above. Report the number. If
anything else fails, say so: it means the repo had drift protection after all,
somewhere this plan did not find.

Revert. Re-run and confirm `4 passed`. Confirm `git status` is clean.

- [ ] **Step 10: Run lint and commit**

```bash
uv run ruff check . > /tmp/f1ruff.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/f1ruff.log
```
Expected: `RUFF_EXIT=0` and `All checks passed!`.

```bash
git add alembic/versions/0064_claimed_at_generation.py src/call_manager/db/models.py tests/test_migration_0064_claimed_at_generation.py
git commit -m "feat(fencing): claimed_at_generation on the 7 live job tables (F1 Task 3)"
```

---

### Task 4: Prove the branch is inert

**Files:**
- Test: `tests/proposal_assist/test_fencing_inertness.py`

**Interfaces:**
- Consumes: `call_manager.proposal_assist.fencing` from Tasks 1–2, the models from
  Task 3.
- Produces: nothing. This task adds a test and no production code.

**Why this is its own task:** Global Constraint 1 says F1 converts no call sites.
That is the property that makes this branch safe to merge into a system with collab
live in production, and it is exactly the kind of claim that quietly stops being
true. It gets its own reviewer gate. It is also the seed of F2's coverage gate,
which inverts it once conversion begins.

This task is **transient by design** — F2 deletes this file. Nothing permanent may
depend on it. That is why the anti-merge invariant lives in Task 1's
`test_fencing.py` instead.

- [ ] **Step 1: Write the test**

Create `tests/proposal_assist/test_fencing_inertness.py`:

```python
"""Fence F1 is INERT: it adds primitives and a column, and wires neither.

Global Constraint 1. F1 is purely additive so it can ship against a production
system with collab live. This file fails the moment someone wires a primitive
into a caller during F1 — which is F3-F7's job, not this slice's.

F2 replaces this file with the real coverage gate, which asserts the OPPOSITE:
that every belief-carrying site DOES fence. Deleting this file is correct then,
and only then.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "call_manager"
FENCING_NAMES = {"live_at_generation", "acquire_commit_mutex_at_generation"}


def _python_files():
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_production_module_imports_the_fencing_primitives_yet():
    """The primitives exist and nothing uses them. Any import outside
    fencing.py itself means a call site was converted during F1."""
    offenders = []
    for path in _python_files():
        if path.name == "fencing.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("proposal_assist.fencing"):
                    offenders.append(f"{path}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith("proposal_assist.fencing"):
                        offenders.append(f"{path}: import {alias.name}")
    assert not offenders, (
        "F1 converts NO call sites (Global Constraint 1), but these modules "
        "import the fencing primitives:\n  " + "\n  ".join(offenders)
    )


def test_no_production_module_calls_the_fencing_primitives_yet():
    """Belt to the import test's braces: catches a call reached via a module
    alias or a re-export, which the import check alone would miss."""
    offenders = []
    for path in _python_files():
        if path.name == "fencing.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name in FENCING_NAMES:
                offenders.append(f"{path}:{node.lineno}: {name}(...)")
    assert not offenders, (
        "F1 converts NO call sites (Global Constraint 1), but these are calls "
        "to the fencing primitives:\n  " + "\n  ".join(offenders)
    )


def test_nothing_reads_or_writes_claimed_at_generation_yet():
    """The column exists and no claim path sets it. F4-F7 wire it."""
    offenders = []
    for path in _python_files():
        if path.name == "models.py":
            continue  # the declaration itself
        text = path.read_text(encoding="utf-8")
        if "claimed_at_generation" in text:
            offenders.append(str(path))
    assert not offenders, (
        "F1 adds the column but wires nothing (Global Constraint 1); these "
        "modules reference claimed_at_generation:\n  " + "\n  ".join(offenders)
    )
```

**Not in this file:** the anti-merge pin. It lives in `test_fencing.py` (Task 1,
`test_the_retirement_predicate_and_the_fence_predicate_are_opposites`) and is
**behavioural**, not structural. A structural version that greps
`_still_at_generation` for the text `soft_deleted_at` is defeated by the very
refactor it exists to stop, because `return live_at_generation(...)` contains no
such text — and it would die with this file when F2 replaces it, while the
invariant is permanent.

- [ ] **Step 2: Run the test to verify it passes**

Run:
```bash
uv run pytest tests/proposal_assist/test_fencing_inertness.py -q > /tmp/f1t4.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/f1t4.log
```
Expected: `3 passed`. If any fails now, a previous task converted a call site —
find it and revert that change before continuing.

- [ ] **Step 3: Mutation — wire a primitive into a caller by import**

In `src/call_manager/proposal_assist/jobs_retirement.py`, add at the top of the
imports:

```python
from call_manager.proposal_assist.fencing import live_at_generation  # noqa: F401
```

Run the test file. **Required outcome:** exactly
`test_no_production_module_imports_the_fencing_primitives_yet` goes RED.

Revert. Re-run and confirm `3 passed`.

- [ ] **Step 4: Mutation — wire a primitive in via a module alias**

The import test alone would miss a call reached through a module alias. In
`jobs_retirement.py`:

```python
from call_manager.proposal_assist import fencing as _f  # noqa: F401
```

and inside `cancel_setup_jobs_for_workspace`, before the `return`, add:

```python
    _ = _f.live_at_generation(workspace_id, generation)
```

Run the test file. **Required outcome:** BOTH
`test_no_production_module_imports_the_fencing_primitives_yet` and
`test_no_production_module_calls_the_fencing_primitives_yet` go RED — the second
is the one that matters, since it is what catches an aliased call the first would
also have caught here only by luck of the module path.

Revert. Re-run and confirm `3 passed`. Confirm `git diff --stat` no longer lists
`jobs_retirement.py`.

- [ ] **Step 5: Mutation — reference the column from a claim path**

In `src/call_manager/proposal_assist/phases/setup.py`, inside
`claim_next_setup_job` (:279), add after the job is flipped to running:

```python
    job.claimed_at_generation = 0
```

Run the test file. **Required outcome:** exactly
`test_nothing_reads_or_writes_claimed_at_generation_yet` goes RED, naming
`setup.py`.

Revert. Re-run and confirm `3 passed`. Confirm `git diff --stat` no longer lists
`setup.py`.

- [ ] **Step 6: Run the full suite, lint, and commit**

```bash
uv run pytest -n 16 --dist loadfile -q > /tmp/f1full.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/f1full.log
uv run ruff check . > /tmp/f1ruff.log 2>&1; echo "RUFF_EXIT=$?"; tail -5 /tmp/f1ruff.log
```
Expected: `3524 passed, 1 skipped` (3501 baseline + 9 + 7 + 4 + 3), `RUFF_EXIT=0`.

```bash
git add tests/proposal_assist/test_fencing_inertness.py
git commit -m "test(fencing): pin F1 inertness structurally (F1 Task 4)"
```

---

## Verification checklist

Before calling F1 done:

- [ ] `uv run pytest -n 16 --dist loadfile` → `3524 passed, 1 skipped`
- [ ] `uv run ruff check .` (redirected, exit code read) → `All checks passed!`
- [ ] `uv run alembic heads` → `0064`
- [ ] The suite leaves the shared testcontainer database at head — run the
      migration test file twice in a row and confirm both runs pass. A stranded
      0063 shows up as unrelated failures in whatever runs next.
- [ ] `git diff --stat bc1bdec..HEAD` touches only: `fencing.py`, `models.py`,
      `alembic/versions/0064_claimed_at_generation.py`, and the four new test files
- [ ] No frontend change, no OpenAPI change, no route change — F1 adds no HTTP surface

## What F1 deliberately leaves undone

Named so a reviewer does not read them as omissions:

- **No call site is converted.** F3–F7 do that, in the order the spec sets:
  `ToolContext` and tool writes FIRST (they are broken today), then the job
  families, then turns, then collab.
- **No compensation is removed.** Not cancel-on-delete, `reconcile_cancelled_jobs`,
  `cancelled_from`, the barriers, the 503-after-commit contract, or any
  `is_retired()` check. That is step 3, and only after the redundancy proof.
- **No coverage gate.** F2 builds it. Task 4's inertness test is its seed and its
  inverse: F2 replaces it.
- **`ToolContext` gains no generation field.** F3 adds it, together with the
  per-turn re-stamp that `registry.py:80` shows is mandatory.
