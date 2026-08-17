# Workspace Restore (Retention Slice C1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin can un-delete a soft-deleted workspace, and it comes back usable — including one that was deleted mid-setup.

**Architecture:** One admin route. It clears `soft_deleted_at`, allocates a lifecycle generation, repairs the setup job and writes its audit event in a **single transaction under the commit mutex**, then applies `active` to the in-process authority C0 built. Every ordering rule and failure code comes from C0's mechanism: the generation travels with the operation, the apply happens after the commit, and monotonicity is what makes retries and late arrivals safe.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, asyncio, React 19 + Mantine + TanStack Query, pytest (+ testcontainers Postgres), vitest.

**Spec:** `docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md` **section 5 only**. Slices D and E are not built here.

## Global Constraints

- **Nothing is removed.** No `rmtree`, no purge, no data deletion. The only schema change is Task 2's column and its `CHECK`.
- **The generation is allocated INSIDE the restoring transaction and travels with the operation.** Never sampled after the commit. This is spec 4.4 and it is not negotiable — C0 exists for it.
- **Ordering is commit-then-apply.** The database says live first; the in-memory authority follows.
- **A guarded mutation must be ONE statement.** A predicate on a candidate `SELECT` with the write in a separate `UPDATE` does not guard the write: both run in one READ COMMITTED transaction and each takes a fresh snapshot, so a transition committing in the gap is invisible to the completed read and unchecked by the write. This is not theory — it shipped in C0 and was fixed in `28abbc4`. Task 1 closes the last instance.
- **The live path takes no mutex.** Acquiring a live workspace's commit mutex contends with its real writers (turn commits, collab checkpoints, `editing/save.py`) for a request that writes nothing.
- **Plain `acquire_commit_mutex`, never `acquire_commit_mutex_live`.** The `_live` variant re-reads liveness and raises `WorkspaceDeleted`, which would refuse every workspace restore exists to serve. It returns `None` and is **not** a context manager (`drafting/locking.py:205`).
- **`WorkspaceLifecycle.apply` is monotonic.** That is what makes a timed-out apply safe to leave running, a retry safe to issue, and a late arrival safe to ignore. Do not add joining, de-duplication or an in-flight registry.
- Work on a branch. Never commit to `master`. All Python through `uv run`.

## Environment notes

- **Fast test runs:** `uv run pytest -n 16 --dist loadfile` (~65s). Single files run fine without `-n`. Never `-n auto` (resolves to **72** here), never `--dist loadgroup`.
- **Never pipe a test command into `tail`/`head`** — the pipeline's exit status becomes the pager's, so a failing suite reports success. Use `cmd > /tmp/out.log 2>&1; echo "EXIT=$?"; tail -40 /tmp/out.log`.
- **`pytest` exits 4 on "no tests ran"** — confirm a nonzero collected count.
- **`uv run ruff check .` is the gate; `ruff format --check` is NOT.** Do not reformat anything. Lint selects `E,F,I,B,UP,SIM` — an unused import fails.
- **Baseline on master: `3450 passed, 1 skipped`.** Re-derive it before comparing.
- **Alembic head is `0062`.** The new migration sets `down_revision = "0062"`.
- **This slice changes a route's response model and adds a route**, so `frontend/src/api/schema.gen.ts` MUST be regenerated: `EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh`. The env var is required — the default model emits a WARNING that corrupts the captured schema.
- Frontend gates: `pnpm exec tsc --noEmit`, `pnpm vitest run`, `pnpm run build`, all from `frontend/`.

## File Structure

| File | Responsibility |
|---|---|
| `src/call_manager/proposal_assist/jobs_retirement.py` | Task 1 single-statement fix; Task 2 writes `cancelled_from` |
| `alembic/versions/0063_setup_job_cancelled_from.py` | **create** — the column + CHECK |
| `src/call_manager/db/models.py` | `WorkspaceSetupJob.cancelled_from` + the CheckConstraint |
| `src/call_manager/proposal_assist/phases/setup.py` | the worker's self-cancel records the source state |
| `src/call_manager/proposal_assist/lifecycle.py` | **`signal_workspace_restored`** — the sync→loop bridge that REPORTS failure |
| `src/call_manager/workspaces.py` | `restore_workspace_db` — the whole DB half, one transaction |
| `src/call_manager/web/api/admin_retention.py` | the POST route, ordering and status codes |
| `frontend/src/api/admin.ts` | `useRestoreWorkspace` mutation + invalidation |
| `frontend/src/routes/AdminDeletedWorkspaceDetail.tsx` | Restore button + confirmation |
| `frontend/src/cockpit/WorkspaceSettingsModal.tsx` | the delete copy stops claiming it cannot be undone |

---

### Task 1: Close the last two-statement guard (prerequisite)

`reconcile_cancelled_jobs` carries the identical defect `28abbc4` fixed in the two cancel helpers: it selects ids where the workspace is soft-deleted, then updates filtering only on those ids. A restore clearing `soft_deleted_at` in the gap and it still cancels the restored workspace's jobs.

**It is inert today only because nothing un-deletes a workspace. This slice is what makes it live.** That is why it is Task 1 and not a follow-up.

**Files:**
- Modify: `src/call_manager/proposal_assist/jobs_retirement.py` (`reconcile_cancelled_jobs`, :191)
- Test: `tests/proposal_assist/test_jobs_retirement_generation.py` (extend)

**Interfaces:**
- Consumes: `_still_at_generation` exists as the sibling pattern (:60). Do not reuse it — reconcile guards on `soft_deleted_at`, not on a generation.
- Produces: no new symbols. Both statements in `reconcile_cancelled_jobs` become one each.

- [ ] **Step 1: Write the failing test**

Reuse the existing `_commit_generation_just_before_the_write` machinery's *shape*, but this test flips liveness rather than the generation. Add to `tests/proposal_assist/test_jobs_retirement_generation.py`:

```python
def _restore_from_another_connection(pg_session_factory, ws_id):
    """Clear soft_deleted_at on a SEPARATE connection, exactly as the restore
    route's transaction will. reconcile locks only JOB rows, never the
    workspaces row, so this cannot block behind it — which is why the window is
    real."""
    with pg_session_factory() as other:
        other.get(Workspace, ws_id).soft_deleted_at = None
        other.commit()


def test_reconcile_does_not_cancel_a_workspace_restored_mid_statement(
    pg_session_factory, migrated_engine
):
    """The same defect 28abbc4 fixed in the cancel helpers, in the last place it
    survived. Inert until C1 ships a restore; this test is what stops it being
    live the moment it does.
    """
    ws_id = _seed(pg_session_factory, generation=1)
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    state = {"fired": False}

    def _before(conn, cursor, statement, parameters, context, executemany):
        # `UPDATE <table>`, not the bare name: the locking select contains
        # "FOR UPDATE", so a bare match fires on the READ and the bump lands
        # where the predicate already catches it — passing against the defect.
        if state["fired"] or "UPDATE workspace_setup_jobs" not in statement:
            return
        state["fired"] = True
        _restore_from_another_connection(pg_session_factory, ws_id)

    event.listen(migrated_engine, "before_cursor_execute", _before)
    try:
        with pg_session_factory() as s:
            jr.reconcile_cancelled_jobs(s)
            s.commit()
    finally:
        # NOT from inside the callback: SQLAlchemy is iterating its listener
        # collection there and mutating it raises "deque mutated during
        # iteration". The engine is shared across this module's tests.
        event.remove(migrated_engine, "before_cursor_execute", _before)

    assert state["fired"], "the trigger never matched — this test proved nothing"
    assert _status(pg_session_factory, ws_id) == "queued", (
        "reconcile cancelled a setup job belonging to a workspace that had "
        "already been restored"
    )
```

`datetime`/`UTC` and `event` are already imported in this module (the `event` import arrived with `28abbc4`). Confirm rather than assume.

- [ ] **Step 2: Run it and watch it FAIL**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_jobs_retirement_generation.py -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t1.log
```

Expected: FAIL — the job is `cancelled`. **If it passes, stop and diagnose before changing any production code**: either the trigger did not match (the assertion above catches that) or the statement shape is not what this task assumes.

- [ ] **Step 3: Make both reconcile statements single**

Same shape as `cancel_setup_jobs_for_workspace` (:129) after `28abbc4`. For the setup half:

```python
    dead_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_not(None))

    setup_candidates = (
        select(WorkspaceSetupJob.id)
        .where(
            WorkspaceSetupJob.status.in_(_CANCELLABLE),
            WorkspaceSetupJob.workspace_id.in_(dead_ws),
        )
        .with_for_update(skip_locked=True)
        .cte("reconcilable_setup_jobs")
    )
    total += session.execute(
        update(WorkspaceSetupJob)
        .where(
            WorkspaceSetupJob.id.in_(select(setup_candidates.c.id)),
            # The predicate must govern the WRITE. Selecting ids under one
            # snapshot and updating under the next is what let a restore land in
            # the gap; one statement means one snapshot. The CTE keeps FOR
            # UPDATE SKIP LOCKED, so a row a live worker holds is still skipped
            # rather than waited on.
            WorkspaceSetupJob.workspace_id.in_(dead_ws),
        )
        .values(
            status="cancelled",
            completed_at=datetime.now(UTC),
            last_error="cancelled: workspace deleted (reconciled)",
        )
        .execution_options(synchronize_session=False)
    ).rowcount
```

Apply the identical transformation to the claim-analysis half. Keep the existing "NOT an early return" comment between them — a workspace with no setup jobs must still have its claim-analysis rows reconciled.

`return total` still returns rows touched; it is now `rowcount` rather than `len(ids)`, which is the honest number.

- [ ] **Step 4: Verify green, then prove the fix is load-bearing**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_jobs_retirement_generation.py tests/proposal_assist/test_phase0_retirement.py -q > /tmp/t1.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t1.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Then the mutation. **Split the setup statement back into SELECT-then-UPDATE** (the pre-fix shape) and confirm `test_reconcile_does_not_cancel_a_workspace_restored_mid_statement` goes red. Restore, re-run, confirm green, confirm `git status` is clean.

**Do NOT mutate by moving the predicate into the CTE.** That stays green and is not a defect: it is still one statement, one snapshot. The load-bearing property is the single statement, not the predicate's position inside it. Report this explicitly so the next reader does not repeat the confusion.

- [ ] **Step 5: Commit** (write the message to a file; backticks in an inline `-m` are shell-substituted).

---

### Task 2: `cancelled_from` — record why a setup job was cancelled

**Files:**
- Create: `alembic/versions/0063_setup_job_cancelled_from.py`
- Modify: `src/call_manager/db/models.py` (`WorkspaceSetupJob`)
- Modify: `src/call_manager/proposal_assist/jobs_retirement.py` (two write sites)
- Modify: `src/call_manager/proposal_assist/phases/setup.py` (`_fail`, :616)
- Test: `tests/proposal_assist/test_setup_job_cancelled_from.py` (create)

**Interfaces:**
- Consumes: Task 1's single-statement reconcile.
- Produces: `WorkspaceSetupJob.cancelled_from: Mapped[str | None]`, NULL by default, constrained to `('queued','running','waiting_for_rubric_review')`.

**Why a column and not an inference.** Restore has to map the job back to a state that progresses (Task 4), and `cancelled` does not say where it came from. Inferring from `started_at`/`completed_at` would be guessing at exactly the moment a wrong guess produces a permanently stuck workspace.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_setup_job_cancelled_from.py`:

```python
"""Every deletion-cancellation path records the state it cancelled FROM.

Retention Slice C1 (spec 5.2.2). Restore maps the job back to a state that
progresses, and `cancelled` alone does not say which. There are THREE paths
that cancel a setup job on deletion; a restore that misses one silently
produces the permanently stuck workspace this column exists to prevent — the
setup page branches only on 'failed' and 'done' (WorkspaceSetup.tsx), so every
other status renders "Setting up…" forever.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

import call_manager.proposal_assist.jobs_retirement as jr
from call_manager.db.models import Workspace, WorkspaceSetupJob
from tests.proposal_assist.conftest import make_workspace


def _ws_with_job(pg_session_factory, *, status: str, generation: int = 1):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status=status))
        s.commit()
        ws_id = ws.id
        row = s.get(Workspace, ws_id)
        row.lifecycle_generation = generation
        row.soft_deleted_at = datetime.now(UTC)
        s.commit()
    return ws_id


def _job(pg_session_factory, ws_id) -> WorkspaceSetupJob:
    with pg_session_factory() as s:
        return s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one()


@pytest.mark.parametrize("source", ["queued", "running", "waiting_for_rubric_review"])
def test_the_retirement_hook_records_the_source_state(pg_session_factory, source):
    ws_id = _ws_with_job(pg_session_factory, status=source)
    with pg_session_factory() as s:
        jr.cancel_setup_jobs_for_workspace(s, ws_id, 1)
        s.commit()
    job = _job(pg_session_factory, ws_id)
    assert job.status == "cancelled"
    assert job.cancelled_from == source


@pytest.mark.parametrize("source", ["queued", "running", "waiting_for_rubric_review"])
def test_reconciliation_records_the_source_state(pg_session_factory, source):
    """The convergence path for rows the hook's SKIP LOCKED passed over. It
    cancels the same rows and must leave the same evidence, or a restore
    silently skips exactly the jobs that needed reconciling."""
    ws_id = _ws_with_job(pg_session_factory, status=source)
    with pg_session_factory() as s:
        jr.reconcile_cancelled_jobs(s)
        s.commit()
    job = _job(pg_session_factory, ws_id)
    assert job.status == "cancelled"
    assert job.cancelled_from == source


def test_a_cancelled_from_outside_the_three_states_is_rejected(pg_session_factory):
    """The CHECK, so the mapping in Task 4 cannot be fed a state it has no row
    for. 'done' is deliberately chosen: a real status, and one the mapping must
    never see."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(
            WorkspaceSetupJob(
                workspace_id=ws.id, status="cancelled", cancelled_from="done"
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_a_job_cancelled_by_nothing_carries_null(pg_session_factory):
    """NULL means "not cancelled by a deletion". Restore leaves those alone —
    a job cancelled some other way is not this feature's business."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
    assert _job(pg_session_factory, ws_id).cancelled_from is None
```

The worker path (`phases/setup.py`) gets its own test in Step 5 — it needs a worker harness, and the existing `tests/proposal_assist/test_phase0_*.py` files own that setup.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_setup_job_cancelled_from.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t2.log
```

Expected: FAIL — `WorkspaceSetupJob` has no attribute `cancelled_from`.

- [ ] **Step 3: Model + migration**

In `models.py`, inside `WorkspaceSetupJob`, beside the existing `CheckConstraint` in `__table_args__`:

```python
        CheckConstraint(
            "cancelled_from IS NULL OR cancelled_from IN "
            "('queued','running','waiting_for_rubric_review')",
            name="ck_workspace_setup_jobs_cancelled_from",
        ),
```

and the column:

```python
    # Retention Slice C1 (spec 5.2.2). The non-terminal state this job was
    # cancelled FROM when its workspace was deleted, so restore can map it back
    # to a state that progresses. NULL means "not cancelled by a deletion" —
    # restore leaves those alone. Consumed (set back to NULL) by restore.
    cancelled_from: Mapped[str | None] = mapped_column(Text, nullable=True)
```

Create `alembic/versions/0063_setup_job_cancelled_from.py`:

```python
"""workspace_setup_jobs: cancelled_from, the state a deletion cancelled from

Revision ID: 0063
Revises: 0062
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspace_setup_jobs",
        sa.Column("cancelled_from", sa.Text(), nullable=True),
    )
    # Rows cancelled before this migration keep NULL: their source state was
    # never recorded and cannot be recovered. Restore treats them as "not
    # cancelled by a deletion" and leaves them alone, which is the same
    # not-restorable outcome spec 0.3 accepts for pre-Slice-A deletions.
    op.create_check_constraint(
        "ck_workspace_setup_jobs_cancelled_from",
        "workspace_setup_jobs",
        "cancelled_from IS NULL OR cancelled_from IN "
        "('queued','running','waiting_for_rubric_review')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_workspace_setup_jobs_cancelled_from", "workspace_setup_jobs"
    )
    op.drop_column("workspace_setup_jobs", "cancelled_from")
```

Check `alembic/versions/0062_workspace_lifecycle_generation.py` for this repo's exact migration idiom before writing, and follow it. If `op.create_check_constraint` is not how existing migrations add CHECKs here, use whatever they do.

- [ ] **Step 4: The two SQL write sites**

Both are `UPDATE ... SET status='cancelled'` statements. Each gains one value. The source state is the row's CURRENT status, which the statement is about to overwrite — so it must be read from the column, not from Python:

```python
        .values(
            status="cancelled",
            cancelled_from=WorkspaceSetupJob.status,   # the column's value NOW
            completed_at=datetime.now(UTC),
            last_error="cancelled: workspace deleted",
        )
```

In PostgreSQL an `UPDATE` reads every column's OLD value for the whole `SET`, so `cancelled_from` receives the pre-update status rather than `'cancelled'`. **Verify this rather than trusting it** — the parametrized tests in Step 1 fail loudly if it is wrong, and one of them (`running`) would be indistinguishable from a bug if you only tested `queued`.

Apply to `cancel_setup_jobs_for_workspace` (:129) and the setup half of `reconcile_cancelled_jobs` (:191). **Not** to the claim-analysis statements: `ClaimAnalysisJob` has no such column and restore does not resurrect claim-analysis work.

- [ ] **Step 5: The worker's self-cancel**

`phases/setup.py:616`, inside `_fail`, in the branch that sets `job.status = "cancelled"`. The job object is loaded `with_for_update`, so its current status is readable:

```python
            if deleted is not None or isinstance(exc, (WorkspaceGoneDuringSetup, WorkspaceDeleted)):
                # Record the state we are cancelling FROM before overwriting it
                # (Slice C1, spec 5.2.2). The guard above already returned for
                # every terminal status, so this is one of the three the CHECK
                # allows.
                job.cancelled_from = job.status
                job.status = "cancelled"
                job.completed_at = now
```

Add a test for this path in `tests/proposal_assist/test_phase0_retirement.py` (it owns the worker-cancellation coverage). Read that file for its harness; drive a real `_fail` with a `WorkspaceGoneDuringSetup` on a `running` job and assert `cancelled_from == "running"`.

**Verify the guard's claim before relying on it.** Line 601 returns early for `("cancelled","failed","done","waiting_for_rubric_review")` — which means `waiting_for_rubric_review` can NEVER reach this branch, so this path can only ever record `queued` or `running`. Say so in your report. That is not a defect (the hook and reconcile cover the parked-for-review case), but a reader who assumes all three states flow through here will write a test that cannot fail.

- [ ] **Step 6: Verify and commit**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_setup_job_cancelled_from.py tests/proposal_assist/test_phase0_retirement.py -q > /tmp/t2.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t2.log
uv run pytest -n 16 --dist loadfile -q > /tmp/t2-full.log 2>&1; echo "FULL_EXIT=$?"; tail -3 /tmp/t2-full.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Then prove the column is load-bearing: change `cancelled_from=WorkspaceSetupJob.status` to `cancelled_from="queued"` in the hook and confirm the `running` and `waiting_for_rubric_review` parametrizations go red. Restore, re-run, confirm green and a clean `git status`. Commit.

---

### Task 3: The restore bridge — a sync→loop apply that REPORTS failure

**Files:**
- Modify: `src/call_manager/proposal_assist/lifecycle.py`
- Test: `tests/proposal_assist/test_restore_bridge.py` (create)

**Interfaces:**
- Consumes: `WorkspaceLifecycle.apply` (C0).
- Produces: `signal_workspace_restored(request, *, workspace_id: uuid.UUID, generation: int) -> bool` — True when the apply landed within the bound, False when it could not be scheduled or timed out.

**Why not reuse `signal_workspace_retirement`.** That function swallows everything and returns `None`, deliberately: the delete has already committed and must not fail because retirement could not be scheduled. Restore needs the opposite contract — spec 5.4 requires **503** when the reactivation is unconfirmed, because a 204 would promise something unverified. Same bridge mechanics, opposite failure policy, so it is a separate function rather than a flag on the existing one.

**The barrier must exceed the retirement fan-out.** C0's coordinator holds the per-workspace lifecycle lock across its whole hook fan-out, bounded by the same per-hook budgets `_RETIREMENT_BARRIER_SECONDS` is derived from (max 5.5s, `passes`). A restore racing a retirement therefore waits for that lock. A barrier below it would turn an ordinary race into a 503. Use the same 6.0s bound and **assert the inequality in a test against the live constants**, exactly as `test_retirement_coordinator.py` does for the retirement budget — so raising a sub-bound fails loudly instead of silently eroding this one.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_restore_bridge.py`:

```python
"""signal_workspace_restored: the bridge that must REPORT failure.

Retention Slice C1 (spec 5.4). Retirement's bridge swallows everything because
its delete has already committed. Restore's caller has to answer 503 when the
reactivation is unconfirmed, so this one returns a verdict.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

from call_manager.proposal_assist.lifecycle import (
    _RESTORE_BARRIER_SECONDS,
    WorkspaceLifecycle,
    signal_workspace_restored,
)
from call_manager.proposal_assist.retirement import _RETIREMENT_BARRIER_SECONDS


class _FakeState:
    pass


class _FakeApp:
    def __init__(self, loop, lifecycle):
        self.state = _FakeState()
        self.state.app_loop = loop
        self.state.workspace_lifecycle = lifecycle


class _FakeRequest:
    def __init__(self, app):
        self.app = app


def test_the_barrier_exceeds_the_retirement_fan_out():
    """A restore racing a retirement waits for the lifecycle lock the retirement
    coordinator holds across its hooks. If this bound were the smaller one, an
    ordinary race would surface to the admin as a 503."""
    assert _RESTORE_BARRIER_SECONDS >= _RETIREMENT_BARRIER_SECONDS


def test_a_landed_apply_reports_true_and_takes_effect():
    lifecycle = WorkspaceLifecycle()
    ws = uuid.uuid4()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        req = _FakeRequest(_FakeApp(loop, lifecycle))
        assert signal_workspace_restored(req, workspace_id=ws, generation=5) is True
        assert lifecycle.is_retired(ws) is False
        assert lifecycle.applied_generation(ws) == 5
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)


def test_a_missing_loop_or_lifecycle_reports_False():
    """Not an exception: the route turns this into 503, and a bridge that raised
    would make every caller wrap it."""
    app = _FakeApp(None, WorkspaceLifecycle())
    assert signal_workspace_restored(
        _FakeRequest(app), workspace_id=uuid.uuid4(), generation=1
    ) is False


def test_a_timed_out_apply_reports_False_and_is_NOT_cancelled(monkeypatch):
    """Spec 5.4: the timed-out apply keeps running and may land later. Safe only
    because apply is monotonic — asserted here by letting it land AFTER the
    bridge already reported False."""
    lifecycle = WorkspaceLifecycle()
    ws = uuid.uuid4()
    release = threading.Event()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    real_apply = lifecycle.apply

    async def _slow_apply(workspace_id, *, generation, retired):
        await asyncio.to_thread(release.wait, 30)
        return await real_apply(workspace_id, generation=generation, retired=retired)

    monkeypatch.setattr(lifecycle, "apply", _slow_apply)
    monkeypatch.setattr(
        "call_manager.proposal_assist.lifecycle._RESTORE_BARRIER_SECONDS", 0.2
    )
    try:
        req = _FakeRequest(_FakeApp(loop, lifecycle))
        assert signal_workspace_restored(req, workspace_id=ws, generation=5) is False
        assert lifecycle.applied_generation(ws) == 0, "it should not have landed yet"

        release.set()
        for _ in range(500):        # wait for the CONDITION, not a duration
            if lifecycle.applied_generation(ws) == 5:
                break
            threading.Event().wait(0.01)
        assert lifecycle.applied_generation(ws) == 5, (
            "the timed-out apply was cancelled; spec 5.4 requires it to keep "
            "running so a late arrival and a retry converge"
        )
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
```

**Note on the monkeypatched constant:** patching a module-level float only works if the bridge reads it at call time (`_RESTORE_BARRIER_SECONDS` referenced inside the function body), not if it is captured as a default argument. Write the implementation accordingly, and if you change that, change the test to match rather than leaving a patch that silently does nothing.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_restore_bridge.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -20 /tmp/t3.log
```

Expected: FAIL — `signal_workspace_restored` does not exist.

- [ ] **Step 3: Implement it**

Append to `src/call_manager/proposal_assist/lifecycle.py`. The module currently imports only stdlib; this adds `concurrent.futures` and `logging`, and duck-types `request` exactly as `retirement.signal_workspace_retirement` does (neither module imports FastAPI):

```python
# Upper bound on how long the sync restore route blocks waiting for the apply.
#
# MUST NOT be smaller than retirement._RETIREMENT_BARRIER_SECONDS. A restore
# racing a retirement waits for the per-workspace lifecycle lock, which the
# retirement coordinator holds across its ENTIRE hook fan-out (C0). A tighter
# bound here would turn that ordinary race into a 503 for the admin.
# test_restore_bridge.py asserts the inequality against the live constants.
_RESTORE_BARRIER_SECONDS = 6.0


def signal_workspace_restored(request, *, workspace_id: uuid.UUID, generation: int) -> bool:
    """Route-side (SYNC): apply `active` on the app loop and BLOCK (bounded).

    Returns True if the apply landed within the bound, False if it could not be
    scheduled or timed out. The MIRROR of retirement.signal_workspace_retirement,
    with the opposite failure policy on purpose: retirement swallows everything
    because its delete has already committed and must not fail late, while
    restore's caller must answer 503 rather than promise a reactivation it could
    not confirm (spec 5.4).

    A timed-out apply is NOT cancelled. It keeps running on the loop and may land
    later, which is safe precisely because apply is monotonic: the late one, the
    retry, and any duplicate converge on the same state and whichever arrives
    second is ignored. That is also why there is no in-flight registry here.

    *generation* is the one the restoring transaction ALLOCATED. Never re-read
    one here (spec 4.4).
    """
    loop = getattr(request.app.state, "app_loop", None)
    lifecycle = getattr(request.app.state, "workspace_lifecycle", None)
    if loop is None or lifecycle is None:
        logger.warning(
            "restore.bridge_unavailable",
            extra={
                "event": "restore_bridge_unavailable",
                "workspace_id": str(workspace_id),
            },
        )
        return False

    coro = lifecycle.apply(workspace_id, generation=generation, retired=False)
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception:
        coro.close()   # never-awaited coroutine -> close it before reporting
        logger.warning(
            "restore.schedule_failed",
            extra={
                "event": "restore_schedule_failed",
                "workspace_id": str(workspace_id),
            },
            exc_info=True,
        )
        return False
    try:
        fut.result(timeout=_RESTORE_BARRIER_SECONDS)
    except concurrent.futures.TimeoutError:
        logger.warning(
            "restore.barrier_timeout",
            extra={
                "event": "restore_barrier_timeout",
                "workspace_id": str(workspace_id),
                "generation": generation,
            },
        )   # deliberately NOT cancelled — see the docstring
        return False
    except Exception:
        logger.warning(
            "restore.apply_failed",
            extra={"event": "restore_apply_failed", "workspace_id": str(workspace_id)},
            exc_info=True,
        )
        return False
    return True
```

Add `logger = logging.getLogger(__name__)` at module scope if it is not already there.

- [ ] **Step 4: Verify and commit**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest tests/proposal_assist/test_restore_bridge.py -q > /tmp/t3.log 2>&1; echo "EXIT=$?"; tail -10 /tmp/t3.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Then the mutation: make the timeout branch `fut.cancel()` before returning False, and confirm `test_a_timed_out_apply_reports_False_and_is_NOT_cancelled` goes red. Restore, re-run, confirm green and clean `git status`. Commit.

---

### Task 4: `restore_workspace_db` — the whole DB half, one transaction

**Files:**
- Modify: `src/call_manager/workspaces.py`
- Test: `tests/test_workspace_restore_service.py` (create)

**Interfaces:**
- Consumes: `bump_lifecycle_generation` (C0), `acquire_commit_mutex`, `WorkspaceSetupJob.cancelled_from` (Task 2).
- Produces:

```python
class RestoreOutcome(NamedTuple):
    generation: int
    setup_job_restored_to: str | None
    transitioned: bool        # False when the workspace was already live
```

`restore_workspace_db(session, *, workspace_id, admin_id, timeout_seconds=10) -> RestoreOutcome`

**The mapping (spec 5.2.1), which is the heart of this task:**

| `cancelled_from` | restored to | why |
|---|---|---|
| `queued` | `queued` | never started |
| `running` | **`queued`** | the worker that owned it died with the deletion. The claim path takes only the oldest **queued** job, and `requeue_stuck_setup_jobs` will not touch a `running` row for `STUCK_SETUP_AFTER` = 30 minutes (`phases/setup.py:103`). Restoring to `running` reproduces the stuck state for half an hour. |
| `waiting_for_rubric_review` | `waiting_for_rubric_review` | sections and rubric are already extracted and a human is the next actor; re-queueing re-runs extraction over completed work |

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workspace_restore_service.py`. The mapping test is the heart of the task, so it is written out here rather than described:

```python
@pytest.mark.parametrize(
    "cancelled_from,expected_status,started_at_cleared",
    [
        ("queued", "queued", True),
        # The decision spec 5.2.1 exists for. NOT 'running'.
        ("running", "queued", True),
        # Preserved exactly, and its started_at kept: its run genuinely started.
        ("waiting_for_rubric_review", "waiting_for_rubric_review", False),
    ],
)
def test_the_setup_job_is_restored_to_a_state_that_progresses(
    pg_session_factory, cancelled_from, expected_status, started_at_cleared
):
    started = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    with pg_session_factory() as s:
        admin = make_user(s, email="admin@ki.se")
        ws = make_workspace(s)
        s.flush()
        s.add(
            WorkspaceSetupJob(
                workspace_id=ws.id,
                status="cancelled",
                cancelled_from=cancelled_from,
                completed_at=datetime.now(UTC),
                last_error="cancelled: workspace deleted",
                started_at=started,
                attempts=2,
            )
        )
        s.commit()
        ws_id, admin_id = ws.id, admin.id

    with pg_session_factory() as s:
        soft_delete_workspace_db(s, workspace_id=ws_id)

    with pg_session_factory() as s:
        outcome = restore_workspace_db(s, workspace_id=ws_id, admin_id=admin_id)

    assert outcome.setup_job_restored_to == expected_status
    with pg_session_factory() as s:
        job = s.query(WorkspaceSetupJob).filter_by(workspace_id=ws_id).one()
    assert job.status == expected_status
    # No contradiction: a restored row must not carry the evidence of its own
    # cancellation, or it reads as "cancelled" to everything but its status.
    assert job.cancelled_from is None
    assert job.completed_at is None
    assert job.last_error is None
    assert (job.started_at is None) is started_at_cleared
    # NOT reset: a job that burned retries before the deletion does not get a
    # fresh budget for having been deleted and restored.
    assert job.attempts == 2
```

Cover, in addition:

1. a deleted workspace comes back: `soft_deleted_at IS NULL`, generation incremented by exactly 1, `transitioned is True`;
4. a job with `cancelled_from IS NULL` is left completely alone, and `setup_job_restored_to` is `None`;
5. exactly one `admin_deleted_workspace_restored` event, naming the admin, in the same transaction — assert the event and the cleared flag are both present after commit;
6. **a failed commit leaves NEITHER** (spec 5.9.3). Force the commit to raise (patch `session.commit` to raise once, or violate a constraint inside the transaction) and assert afterwards that `soft_deleted_at` is still set **and** no audit row exists. "Same transaction" is a claim about atomicity; a test that only checks both are present on success does not test it;
7. `transitioned=False` when the workspace is already live under the mutex: nothing written, no audit row, and the returned generation is the row's current one;
8. **cancelled turns stay cancelled** (spec 5.9.10) — seed a cancelled `TurnJob`, restore, assert it is untouched;
9. the generation is allocated inside the transaction: change the row's generation from another session between the call and the assertion and confirm the returned value is the one the call produced, not the one the row now holds.

Assert on the returned `RestoreOutcome` **and** on the database, not one or the other: the caller's audit payload comes from the return value, and a correct DB write with a wrong return value produces a lying audit row.

- [ ] **Step 2: Run to verify they fail**

Expected: FAIL — `restore_workspace_db` does not exist.

- [ ] **Step 3: Implement it**

In `src/call_manager/workspaces.py`, mirroring `soft_delete_workspace_db`'s structure (:81):

```python
_SETUP_RESTORE_MAP = {
    "queued": "queued",
    # NOT 'running'. See the task table / spec 5.2.1: the worker that owned it
    # died with the deletion, nothing recreates it, and requeue_stuck_setup_jobs
    # will not touch a 'running' row for STUCK_SETUP_AFTER (30 minutes).
    "running": "queued",
    # Preserved exactly: extraction is DONE and a human is the next actor.
    "waiting_for_rubric_review": "waiting_for_rubric_review",
}


def restore_workspace_db(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    admin_id: int,
    timeout_seconds: int = 10,
) -> RestoreOutcome:
    """Clear soft_deleted_at, allocate the lifecycle generation, repair the setup
    job and write the audit event — all in ONE transaction, then COMMIT.

    Takes the plain commit mutex for the whole transaction, NEVER
    acquire_commit_mutex_live: the _live variant re-reads liveness and raises
    WorkspaceDeleted, which would refuse every workspace this function exists to
    serve. Restore is the one path that makes the mutex load-bearing rather than
    defensive — it FLIPS the liveness flag every other writer re-reads under that
    same mutex, which is what makes the flip a linearization point.

    Raises CommitMutexTimeout (route -> 429) having committed nothing.

    The returned generation is the one THIS transaction allocated. The caller
    passes it to signal_workspace_restored untouched and must never re-read one
    (spec 4.4).
    """
```

Body, in order:

1. `session.get(Workspace, workspace_id)`; `None` → raise `ValueError` (the route resolves existence first, so a miss means the contract was bypassed — same shape as `soft_delete_workspace_db`).
2. `acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)`.
3. **Fresh re-read under the lock** of `soft_deleted_at` via a scalar `SELECT` — never the identity-mapped instance from step 1, which was loaded before we could block on the mutex. If it is already NULL: `session.rollback()` and return `RestoreOutcome(generation=<current>, setup_job_restored_to=None, transitioned=False)`. The route turns that into a 204 with no audit row.
4. `ws.soft_deleted_at = None`.
5. `generation = bump_lifecycle_generation(session, workspace_id=workspace_id)`.
6. Setup job: select this workspace's jobs `WHERE cancelled_from IS NOT NULL`; for each, set `status = _SETUP_RESTORE_MAP[job.cancelled_from]`, `cancelled_from = None`, `completed_at = None`, `last_error = None`, and `started_at = None` **only when the target is `queued`**. Do **not** touch `attempts` — a job that burned retries before the deletion does not get a fresh budget for having been deleted.
7. `WorkspaceEvent(kind="admin_deleted_workspace_restored", ...)` with payload `{"workspace_id": str(...), "admin_user_id": admin_id, "setup_job_restored_to": <target or None>}`, `actor_id=admin_id`. Follow `_record_inspection` in `admin_retention.py` for the payload idiom.
8. `session.commit()` and return.

**On multiplicity:** `create_workspace` inserts exactly one `WorkspaceSetupJob` (`workspaces.py:72`) and the rubric retry re-queues that same row rather than inserting another, so at most one row should ever carry `cancelled_from`. **Verify that claim** with `grep -rn "WorkspaceSetupJob(" --include="*.py" src/` and report what you find. Handle the set as a loop anyway (it costs nothing), and let `setup_job_restored_to` carry the target of the single restored row — or `None` when there were none. If the grep shows a second insertion site, stop and report it: the audit field is singular and the spec assumed one.

- [ ] **Step 4: Verify, mutate, commit**

Run the new tests plus `tests/test_workspaces_service.py`. Then two mutations:

- change `"running": "queued"` to `"running": "running"` → the `running` mapping test must go red. This is the stuck-workspace defect the whole of spec 5.2 exists to prevent, so a test that survives it is worthless.
- swap `acquire_commit_mutex` for `acquire_commit_mutex_live` → the happy-path test must go red (spec 5.9.11).

Report both, restore, confirm green and clean `git status`. Commit.

---

### Task 5: The route

**Files:**
- Modify: `src/call_manager/web/api/admin_retention.py`
- Test: `tests/test_api_admin_retention_restore.py` (create)

**Interfaces:**
- Consumes: `restore_workspace_db` (Task 4), `signal_workspace_restored` (Task 3).
- Produces: `POST /api/admin/workspaces/deleted/{workspace_id}/restore` → 204.

**Ordering (spec 5.3), exactly:**

```
pre-lock read  → missing            → 404
               → LIVE               → apply(current generation, active); 204   [no mutex, no DB write, no audit]
restore_workspace_db(...)           → mutex, flag, generation, setup job, audit, COMMIT
    transitioned=False              → it went live while we waited for the mutex:
                                      nothing was written and no audit row exists.
                                      Still apply the generation it returned, then 204.
signal_workspace_restored(...)      → after the commit
    True  → 204
    False → 503 + Retry-After: 2
```

**Two different "already live" paths, one answer.** The pre-lock read catches the common case cheaply; the under-mutex re-read catches a workspace that went live in the window between them. Both end at 204 with no audit row, and both still apply — an apply is idempotent and cheap, and skipping it would leave the in-memory authority behind the database on exactly the interleaving that is hardest to reason about. Give `transitioned=False` its own test; a route that only handled the pre-lock case would pass every test that never contends.

**Status codes (spec 5.4.1):** missing → 404; deleted → 204 (or 503); live → 204. **A live workspace is 204, not 409** — a retry that races the original apply finds the workspace live and would otherwise be told the failure code for the attempt that succeeded. `CommitMutexTimeout` → 429 + `Retry-After: 2` + `BUSY_ERROR_MESSAGE`, matching every other caller.

**The live path applies the CURRENT generation.** This is the one place a generation is read rather than allocated, and it looks exactly like the pattern C0 forbids — so justify it in a comment: nothing is transitioning, so there is no transition to bind to, and monotonicity makes the read safe in both directions (a delete that already applied a higher generation ignores this; a delete that applies later wins).

**Not gated by `RETENTION_INSPECTION_ENABLED`.** That switch governs READING a deleted workspace's content. Restore reads nothing back to the caller — it returns 204 with no body. An admin who may not read a draft may still put a workspace back. Say this in the route docstring, because the neighbouring endpoint IS gated and the asymmetry will look like an oversight otherwise.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_api_admin_retention_restore.py`, using `tests/admin_retention_helpers.py` (`setup_app`, `client_for`, `soft_delete`) — do not re-create those. Cover:

1. an admin restores a deleted workspace → 204, `soft_deleted_at IS NULL`, one audit row;
2. missing → 404; non-admin → 403; live → 204 with **no** audit row written;
3. a second restore of a now-live workspace → 204 and still exactly one audit row total;
4. `CommitMutexTimeout` → 429 with `Retry-After` (follow the inspect module's contention test for how it forces this);
5. **the route passes the generation its own transaction allocated** — monkeypatch `signal_workspace_restored`, capture the generation, and mutate the row's generation from another session inside the capture, exactly as `test_workspaces_delete_route.py` does for delete;
6. **fault injection (spec 5.9.8):** force `signal_workspace_restored` to return False → 503 with `Retry-After`. Then a retry with the real bridge → 204, the workspace is usable, and **exactly one audit row exists across both calls**;
7. the route works with `RETENTION_INSPECTION_ENABLED` unset (the default) — restore is not gated by the read switch.

- [ ] **Step 2–4: Run red, implement, run green**

Add the route to `admin_retention.py` beside the others, on the **central router with a RELATIVE path** — `web/api/__init__.py` supplies the `/api` prefix and the shared error-envelope responses; an absolute path mounted on the app skips those and advertises the wrong 422 shape to the TS codegen. `require_admin` as the dependency.

Return `Response(status_code=204)`. For 503 raise `HTTPException(status_code=503, detail=..., headers={"Retry-After": "2"})`; remember the `/api` envelope reshapes the body to `{"error": {"code", "message", ...}}` (`web/api/errors.py:27`), so assert on `r.json()["error"]["message"]`, never `["detail"]`.

- [ ] **Step 5: Regenerate the OpenAPI types**

```bash
cd /home/parlar_ai/dev/call-monitor
EMBEDDING_MODEL=bge-m3 bash scripts/gen_openapi_types.sh
git diff --stat -- frontend/src/api/schema.gen.ts
```

The new route MUST appear. If the diff is empty, the route did not register — do not proceed.

- [ ] **Step 6: Full gates and commit**

Full suite, `ruff`, and report the numbers.

---

### Task 6: The UI, and the sentence that is now false

**Files:**
- Modify: `frontend/src/api/admin.ts`
- Modify: `frontend/src/routes/AdminDeletedWorkspaceDetail.tsx`
- Modify: `frontend/src/cockpit/WorkspaceSettingsModal.tsx`
- Test: `frontend/src/routes/AdminDeletedWorkspaces.test.tsx` (extend)

**Interfaces:**
- Consumes: the route from Task 5, via the regenerated `schema.gen.ts`.
- Produces: `useRestoreWorkspace(workspaceId)`.

- [ ] **Step 1: The mutation hook**

In `frontend/src/api/admin.ts`, following `useApproveUser` (:68):

```ts
export function useRestoreWorkspace(workspaceId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/admin/workspaces/deleted/{workspace_id}/restore", {
          params: { path: { workspace_id: workspaceId } },
        }),
      ),
    // The list inherits the app-wide 20 s freshness window (App.tsx:16), so
    // without invalidation the restored row is still on screen when the admin
    // arrives back at the list. Await it in the caller BEFORE navigating.
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-deleted-workspaces"] }),
  });
}
```

- [ ] **Step 2: The button**

In `AdminDeletedWorkspaceDetail.tsx`: a **Restore** button with a `window.confirm` (matching `WorkspaceSettingsModal.tsx`'s delete confirmation idiom), which on success navigates back to `/admin/deleted-workspaces`. Render the mutation's error state — a 503 is a real outcome here and the admin must be told to retry, not left looking at an unchanged page.

- [ ] **Step 3: Fix the sentence Slice A wrote**

`WorkspaceSettingsModal.tsx` currently tells the owner, in the body copy and again in the confirm dialog, that deletion cannot be undone. Slice A wrote that because it was true and refused to promise a restore that did not exist. It exists now, so the copy must say the **owner** cannot undo it and an administrator can restore it. Change both strings.

- [ ] **Step 4: Tests**

Extend `AdminDeletedWorkspaces.test.tsx` (its `mockDetail` already exists; note `mockList` is path-aware since the capability work and answers `/api/me`):

1. clicking Restore and confirming POSTs to the restore path — assert the **request**, not just that the page changed;
2. declining the confirmation POSTs nothing;
3. a failed mutation shows an error and does **not** navigate;
4. on success `["admin-deleted-workspaces"]` is invalidated **before** navigating — spy on `invalidateQueries`, and assert order rather than mere occurrence, because the whole point is the stale row.

Add a cockpit test asserting the settings copy no longer claims deletion cannot be undone at all. Find the existing `WorkspaceSettingsModal` test file first; if none exists, say so in your report and put the assertion in the nearest cockpit test module rather than creating a file for one string.

- [ ] **Step 5: Frontend gates**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend
pnpm exec tsc --noEmit; echo "TSC_EXIT=$?"
pnpm vitest run > /tmp/t6-vitest.log 2>&1; echo "VITEST_EXIT=$?"; grep -E "Test Files|Tests " /tmp/t6-vitest.log | tail -2
pnpm run build > /tmp/t6-build.log 2>&1; echo "BUILD_EXIT=$?"
```

Then mutate: remove the `invalidateQueries` call and confirm the ordering test goes red. Restore, re-run, commit.

---

### Task 7: Acceptance — the setup states and the late apply, end to end

**Files:**
- Test: `tests/test_workspace_restore_acceptance.py` (create)

This task adds no production code. It is the slice's proof, and every test here must be shown capable of failing.

- [ ] **Step 1: Write the tests**

1. **Deleted while `running` → claimed without waiting out `STUCK_SETUP_AFTER`** (spec 5.9.4). Delete a workspace whose setup job is `running`, restore it, then drive the real claim path and assert a worker picks the job up **now**. This is the acceptance test the whole `running → queued` decision exists for, and asserting only `status == "queued"` does not prove claimability — call the claim function.
2. **Deleted while `waiting_for_rubric_review` → extraction does NOT re-run.** Restore, run a scheduler tick, and assert the extraction side effect did not happen (spy the extraction entry point, or assert the sections/rubric rows are unchanged). Asserting only the status would pass even if a tick re-extracted.
3. **The late apply (spec 5.9.9).** Force the first apply to exceed the barrier, let the retry succeed, then let the original land — the workspace stays live. Monotonicity, end to end. Drive the real bridge; do not simulate it with two `apply` calls.
4. **All three cancellation paths → restore works** (spec 5.9.5 composed with 5.9.4): one test per path, cancelling via the hook, via reconciliation, and via the worker, then restoring and asserting the mapped status.
5. Cancelled turns stay cancelled after a restore.
6. **The owner can open it again** (spec 5.9.1). Restore, then have the OWNER — not the admin — fetch the workspace through its ordinary API and get 200 rather than the 404 a deleted workspace returns. Every other test in this slice asserts on rows and statuses; this is the one that checks the thing the feature is actually for. Use the cockpit/workspace endpoint the owner's UI uses, not an admin route.

- [ ] **Step 2: Prove each can fail**

| Mutation | Must go red |
|---|---|
| `_SETUP_RESTORE_MAP["running"] = "running"` | 1, and 4's worker/hook cases |
| restore also re-queues `waiting_for_rubric_review` | 2 |
| the bridge cancels its future on timeout | 3 |
| the worker's `cancelled_from` write removed | 4's worker case |

Report which tests reddened against which mutation, compared with this table. **A test that cannot fail is worse than no test**, because it certifies the thing it did not check. If one does not redden, that is a failed task — diagnose whether the mutation was equivalent or the test is decorative, and say which.

- [ ] **Step 3: Full gates**

```bash
cd /home/parlar_ai/dev/call-monitor
uv run pytest -n 16 --dist loadfile -q > /tmp/t7-full.log 2>&1; echo "FULL_EXIT=$?"; tail -3 /tmp/t7-full.log
uv run ruff check . ; echo "RUFF_EXIT=$?"
git diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
```

`DRIFT_EXIT` must be 0 — Task 5 already regenerated the schema, so a diff here means something changed after it.

- [ ] **Step 4: Commit**

---

## Done when

- An admin restores a deleted workspace; it leaves the deleted list and its owner can open it.
- Missing → 404, non-admin → 403, live → 204 **with no audit row**, mutex contention → 429. A second restore of a live workspace is 204 and writes no second audit row.
- Exactly one `admin_deleted_workspace_restored` event per real transition, naming the admin, committed in the **same transaction** as the flag clear.
- **All three setup states restore correctly**, `running → queued` is claimable immediately, and `waiting_for_rubric_review` does not re-run extraction. All three cancellation paths record `cancelled_from`, and the `CHECK` rejects anything else.
- The restored row carries no contradiction: `cancelled_from`, `completed_at`, `last_error` cleared; `started_at` cleared only for a `queued` target; `attempts` unchanged.
- A timed-out apply returns **503 + Retry-After**, the retry returns 204, exactly one audit row exists across both, and the late apply landing afterwards leaves the workspace live.
- Swapping in `acquire_commit_mutex_live` turns the happy path red; `_SETUP_RESTORE_MAP["running"] = "running"` turns the claimability test red; removing `invalidateQueries` turns the ordering test red.
- `reconcile_cancelled_jobs` is two single statements, and a restore committing mid-write cancels nothing.
- Full suite green at the re-derived baseline plus this slice; `ruff` clean; `tsc`, `vitest` and `build` clean; `schema.gen.ts` regenerated with the new route and no residual drift.
- One migration (`0063`), no `rmtree`, nothing removed. **No purge, no `purged_at`, no filesystem barrier** — those are D and E.
