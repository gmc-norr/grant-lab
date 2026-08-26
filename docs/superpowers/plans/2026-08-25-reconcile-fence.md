# Reconcile Fence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revision 2** (2026-08-25). Revision 1 was reviewed by Sol via GitHub Copilot and
**five of its six findings were confirmed against the code.** The largest one cut
this plan's centrepiece. Read "What revision 1 got wrong" before anything else —
it is the reason this plan is now a tidy-up rather than a bug fix, and saying so
is more useful than letting the next reader inherit the old framing.

**Goal:** Stop `reconcile.py` writing to a workspace that has been soft-deleted,
fix a dedup rule that would delete the wrong row, and pin at the git level an
existing protection that nothing currently asserts.

**Architecture:** Reconcile's two writes become fenced DB effects using the
codebase's existing mechanism-1 pair — `acquire_shared_lease` (Primitive C) plus
`live_at_generation` (Primitive A) inside the WHERE of the write. No new
primitive, no new schema concept, no change to any writer.

**Tech Stack:** Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2.x, Alembic,
pytest + testcontainers, `uv`, `ruff`.

---

## What revision 1 got wrong

Revision 1 claimed a user-visible bug: that `review/pass_runner.py:349` could
commit to git after a workspace was deleted, and that reconciliation would then
promote that "zombie" commit into product history after a restore. It made
fencing that line the centrepiece (its Task 3).

**That premise is refuted.** Verified in code, not remembered:

- `soft_delete_workspace_db` calls `cancel_review_runs_and_items_for_workspace`
  **inside the delete transaction** (`workspaces.py:190`).
- That helper runs one `UPDATE ReviewRun ... WHERE status IN ('queued','running')
  SET status='cancelled' ... RETURNING` (`review/jobs.py:111-122`).
- The finalize block that performs the git write locks the run and refuses
  unless it is still running (`pass_runner.py:328-331`) — its own comment reads
  `# 4. Write artifacts + finalize (row-locked, still-running guard).`
- Restore never resurrects a cancelled run: `workspaces.py` references
  `ReviewRun` only at line 190, in the delete path.

Both interleavings are therefore safe:

| Order | Outcome |
|---|---|
| Delete commits first | Finalize's `FOR UPDATE` read (new READ COMMITTED txn) sees `cancelled` → returns before the git write |
| Finalize locks the run first | Delete's cancel UPDATE blocks on that row until finalize commits → the commit landed while the delete was still **uncommitted**, i.e. the workspace was live |

And the timeout leg is safe too: if the delete's cancel UPDATE hits its
`lock_timeout` waiting on the finalize's row lock, the **whole delete transaction
rolls back** (the Slice C3 behaviour — the route answers 429 having deleted
nothing). There is no partial delete for a later commit to be "after".

**Consequences carried into this revision:**

- Revision 1's Task 3 is **cut**. Do not re-add it. It would also have been the
  most invasive change here, and it was defending a state that cannot occur.
- The old Task 3's test could not have worked anyway: it soft-deleted the
  workspace *before* calling `run_review_pass`, whose prefix returns at
  `pass_runner.py:242-243`, never reaching line 349. It would have passed
  before the fix and after it, proving nothing.
- The headline claim "the hole closes at pass_runner, not reconcile" is
  **withdrawn**. There is no hole there.

**What survives is genuinely smaller.** Reconcile can still write to a
soft-deleted workspace, and `reconcile_workspace_citekeys` mutates a
**user-visible** column (`references.citekey`, served by the references API)
while doing it. That is a real leak worth closing. It is not the user-visible
history corruption revision 1 advertised.

## The defect that is real

`reconcile_workspace_git_drift` and `reconcile_workspace_citekeys` run on **every
workspace open** (`drafting/controller.py:167` and `:175`), inside a best-effort
`try/except Exception`. Neither takes a stamp, a generation, a liveness check, or
the commit mutex — verified: zero `acquire_commit_mutex*`, zero `FenceStamp`,
zero `live_at_generation` in `reconcile.py`.

`controller.launch` holds `acquire_workspace_lock`, but that is a **different
advisory-lock namespace**: `_advisory_lock_key("workspace", …)` versus
`_advisory_lock_key("commit", …)` (`drafting/locking.py:47-64`, `:132-140`). The
module's own docstring says the namespaces are distinct "so holding one never
implies the other". Reconcile therefore runs concurrent with every git writer and
with delete/restore.

`controller.launch:120-122` already documents the window:
`# NARROWING ONLY — a deletion committing after this check can still see a`
`# cached handle returned; reconciliation is the backstop.`

### Why the fence never covered it

The fence programme's audit unit is the **git sink**: the inventory and the
tree-level gates scan `write_and_commit_files` call sites. Reconcile calls no git
sink — it *reads* `git log` and writes only DB rows. It was invisible to the
inventory by construction. Confirmed: `grep -rn reconcile tests/fencing/` returns
nothing.

That is the general lesson worth keeping: a sink-shaped audit cannot see a
DB-only effect, and `fencing.py`'s own invariant covers "a semantic effect
produced from a belief formed earlier", not "a git write".

## The trap that constrains every design here

`editing/save.py:292` — inside the commit mutex, immediately after the git write:

> `# 8. Provenance rows (committed by the caller, atomically with the mutex`
> `#    release; a DB failure from here on is repaired by reconciliation).`

and the module docstring (`save.py:10-11`):

> `A DB failure AFTER the git commit is recovered by`
> `reconcile_workspace_git_drift + the analysis reconcile tick (spec §6).`

**Reconcile is load-bearing for legitimate crash recovery.** A guard strict
enough to reject a properly-fenced writer's orphaned commit causes silent history
loss — strictly worse than the leak being fixed. This is why Tasks 1–2 guard on
*workspace liveness* and never on the commit's provenance, and why every task
below carries a live-workspace regression test alongside its new gate.

## The load-bearing half (do not invert this)

Reconcile forms its belief (the `git log` read) and performs its effect (the DB
write) inside one function, seconds apart. It carries **no** cross-boundary
belief. Therefore, in Tasks 1–2:

- the **shared lease** is load-bearing: it is what stops a delete committing
  underneath the write;
- the **liveness** clause of `live_at_generation` is load-bearing: the workspace
  may already be soft-deleted when reconcile starts;
- the **generation-equality** clause is, by construction, *hollow*: the
  generation is read under the lease, and a lifecycle transition needs that key
  exclusively, so it cannot move. It can never fail.

Keep the equality clause anyway — it is one term in a predicate every other
mechanism-1 site uses, and dropping it here would make this the odd one out. But
write the comment, because the tempting "simplification" is backwards: dropping
liveness and keeping generation leaves a gate that cannot fail.

**Sol's round-1 finding 2, confirmed:** revision 1 mutated only
`live_at_generation`, never `acquire_shared_lease`, and both its fixtures deleted
the workspace *before* reconcile started — so the predicate alone carried every
test and the lease was untested scaffolding. Tasks 1 and 2 below each carry a
dedicated lease test with a dedicated lease mutation. Do not drop them.

## Module map

Verified against the tree at `8ea3f68e`.

| Symbol | Defined in |
|---|---|
| `reconcile_workspace_git_drift` | `proposal_assist/reconcile.py:38` |
| `reconcile_workspace_citekeys` | `proposal_assist/reconcile.py:112` |
| both call sites | `proposal_assist/drafting/controller.py:167`, `:175` |
| `acquire_shared_lease` (Primitive C) | `proposal_assist/fencing.py:174` |
| `live_at_generation` (Primitive A) | `proposal_assist/fencing.py:72` |
| `commit_mutex_key` | `proposal_assist/drafting/locking.py:132` |
| the INSERT…SELECT…WHERE idiom to copy | `proposal_assist/port_impl/effects.py:270-315` |
| the finalize still-running guard (Task 3 pins this) | `proposal_assist/review/pass_runner.py:328-331` |
| delete-time run cancel | `workspaces.py:190` → `review/jobs.py:96-138` |
| delete: sets `soft_deleted_at`, bumps generation | `workspaces.py:184`, `:237` |
| restore: clears `soft_deleted_at`, bumps generation | `workspaces.py:338`, `:341` |
| `ArtifactVersion` model | `db/models.py:1133` |
| blocking-test recipe to copy | `tests/proposal_assist/test_pass_retirement.py:216-273` |
| pausing-adapter recipe to copy | `tests/proposal_assist/test_pass_retirement.py:278-310` |
| alembic head | `0064` |

## Global Constraints

- **Line length 100.** `ruff check` is the gate; `ruff format` is **not**.
  Run `uv run --python 3.12 ruff check src tests` before every commit.
- **Test command:** `uv run --python 3.12 pytest -n 16 --dist loadfile`.
  Never `-n auto` (resolves to 72 here). Never `--dist loadgroup`.
  **Never pipe pytest through `tail`/`head`** — the exit code becomes the pager's.
  **pytest exit 4 = "no tests ran"**, exit 5 = "no tests collected". Both are the
  false-pass signature: fix the path, never read either as a pass.
- **Baseline at `8ea3f68e`: `3910 passed, 1 skipped`.** The `1 skipped` is the
  signal (a real-SDK test gated on `ANTHROPIC_API_KEY`), not the total.
- **Every new test must be proven able to fail.** For each one: make the exact
  mutation named in the task, run *that test file*, record `FAILED` (never
  `ERROR` — an ERROR means the mutation broke import, which proves nothing),
  then `git checkout --` the mutation. **Commit the fix BEFORE mutating**, never
  after; reverting an uncommitted fix wipes it.
- **A blocking test must make its observation WHILE the interference is in
  force.** Asserting after releasing the lock proves nothing — the fixed and
  unfixed code both pass. This is stated in
  `test_pass_retirement.py:220-225` and it is the single easiest way to write a
  hollow gate here.
- **Never take Primitive C and then Primitive B for the same workspace** in one
  transaction (`fencing.py:198-201`). Every task here takes C only.
- Do not weaken, delete, or skip any existing test.

---

### Task 1: Fence `reconcile_workspace_git_drift`

**Files:**
- Modify: `src/call_manager/proposal_assist/reconcile.py:38-109`
- Test: `tests/proposal_assist/test_reconcile.py`

**Interfaces:**
- Consumes: `acquire_shared_lease`, `live_at_generation` from
  `call_manager.proposal_assist.fencing`; `commit_mutex_key` from
  `call_manager.proposal_assist.drafting.locking` (test only).
- Produces: signature **unchanged** —
  `reconcile_workspace_git_drift(session_factory, workspace_id, repo_dir) -> int`.
  No caller changes.

**Why no `generation` parameter:** reconcile carries no belief across an
authorization boundary (see "The load-bearing half"). A generation minted at
`controller.launch` would be a *different* belief from the `git log` read, and
`launch()` takes only `workspace_id` — it has no stamp to pass. Adding one would
be provenance theatre.

- [ ] **Step 1: Write the three failing tests**

Append to `tests/proposal_assist/test_reconcile.py`. Add these imports at the top
of the file:

```python
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import func, text

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import commit_mutex_key
```

Test A — the liveness gate:

```python
def test_reconcile_git_drift_writes_nothing_to_a_soft_deleted_workspace(
    pg_session_factory, tmp_path
):
    """The workspace was soft-deleted before reconcile ran.

    The commits are real and orphaned, so unfenced reconcile backfilled all of
    them. The fenced version must insert ZERO rows and ZERO audit events — the
    INSERT ... SELECT ... WHERE live_at_generation finds no live workspace.
    """
    repo_dir = tmp_path / "repo"
    _init_repo_with_n_commits(repo_dir, 2)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        wsid = ws.id
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)

    assert backfilled == 0
    with pg_session_factory() as s:
        rows = list(
            s.scalars(select(ArtifactVersion).where(ArtifactVersion.workspace_id == wsid))
        )
        assert rows == []
        events = list(
            s.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == wsid,
                    WorkspaceEvent.kind == "git_drift_reconciled",
                )
            )
        )
        assert events == []
```

Test B — **the lease gate.** This is the one revision 1 was missing. It is the
only test here that fails if `acquire_shared_lease` is removed.

```python
def test_reconcile_git_drift_waits_for_the_commit_mutex_key(pg_session_factory, tmp_path):
    """The shared lease is what stops a delete committing underneath the INSERT.

    A delete holds the workspace's commit-mutex key EXCLUSIVELY for its whole
    transaction. Simulate that with a holder session, then run reconcile in a
    thread and observe WHILE THE HOLDER IS STILL IN FORCE. Asserting after
    releasing proves nothing: reconcile would then complete either way.

    Without acquire_shared_lease, reconcile ignores the key and inserts
    immediately — which is exactly the race fencing.py:14-24 documents for an
    INSERT, whose lack of a target row means there is no READ COMMITTED re-check
    to mask a missing fence.
    """
    repo_dir = tmp_path / "repo"
    _init_repo_with_n_commits(repo_dir, 2)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        wsid = ws.id
        s.commit()

    done = threading.Event()

    def _reconcile():
        try:
            reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)
        finally:
            done.set()

    holder = pg_session_factory()
    observed: list[int] = []
    t = threading.Thread(target=_reconcile)
    try:
        # Transaction-scoped exclusive hold on the same key a delete would take.
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": commit_mutex_key(wsid)}
        )
        t.start()
        time.sleep(1.5)  # ample time for an UNLEASED reconcile to finish inserting
        with pg_session_factory() as probe:
            observed.append(
                probe.scalar(
                    select(func.count())
                    .select_from(ArtifactVersion)
                    .where(ArtifactVersion.workspace_id == wsid)
                )
            )
    finally:
        holder.rollback()   # releases the xact lock
        holder.close()
        assert done.wait(timeout=30), "reconcile never finished"
        t.join(timeout=5)

    assert observed == [0], (
        f"while the commit-mutex key was held exclusively, reconcile had already "
        f"inserted {observed!r} row(s) — it did not take the shared lease"
    )
    # And it must still complete once the key is free.
    with pg_session_factory() as s:
        assert s.scalar(
            select(func.count())
            .select_from(ArtifactVersion)
            .where(ArtifactVersion.workspace_id == wsid)
        ) == 2
```

`acquire_shared_lease` defaults to `timeout_seconds=10`, so a 1.5s hold leaves it
waiting, not timing out. If this test ever becomes flaky on a loaded box, raise
the lease timeout in the test, never shorten the sleep.

Test C — the regression guard:

```python
def test_reconcile_git_drift_still_backfills_a_live_workspace(pg_session_factory, tmp_path):
    """The legitimate crash-recovery case must keep working.

    save.py:292 documents reconciliation as the repair path for a DB failure
    after the git commit. A guard that rejects this causes silent history loss.
    """
    repo_dir = tmp_path / "repo"
    shas = _init_repo_with_n_commits(repo_dir, 2)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        wsid = ws.id
        s.commit()

    backfilled = reconcile_workspace_git_drift(pg_session_factory, wsid, repo_dir)

    assert backfilled == 2
    with pg_session_factory() as s:
        got = set(
            s.scalars(
                select(ArtifactVersion.commit_sha).where(
                    ArtifactVersion.workspace_id == wsid
                )
            )
        )
        assert got == set(shas)
```

- [ ] **Step 2: Run to verify A and B fail, C passes**

Run:
```
uv run --python 3.12 pytest tests/proposal_assist/test_reconcile.py -v
```
Expected:
- Test A **FAILS** with `assert 2 == 0`.
- Test B **FAILS** with `observed == [2]` — unfenced reconcile inserted while the
  key was held.
- Test C **PASSES** already; it is the regression guard for Step 3, not new
  behaviour.

- [ ] **Step 3: Fence the write**

In `src/call_manager/proposal_assist/reconcile.py`, change the imports:

```python
from sqlalchemy import insert, literal, select
from call_manager.db.models import ArtifactVersion, Reference, Workspace, WorkspaceEvent
from call_manager.proposal_assist.fencing import acquire_shared_lease, live_at_generation
```

Replace the `with session_factory() as s:` block (currently lines 77-108) with:

```python
    with session_factory() as s:
        # Primitive C FIRST. Shared locks do not conflict with each other, so
        # concurrent reconciles stay concurrent, but a delete or restore needs
        # this key EXCLUSIVELY — so it either waits behind us or wins first and
        # leaves the predicate below to refuse.
        acquire_shared_lease(s, workspace_id)
        # Read the generation UNDER the lease: the belief this effect carries is
        # the git log read a few lines above, not an HTTP authorization
        # boundary, so this is the right place to form it.
        generation = s.scalar(
            select(Workspace.lifecycle_generation).where(Workspace.id == workspace_id)
        )
        if generation is None:
            return 0  # workspace row is gone entirely (hard delete)
        existing_shas = set(
            s.scalars(
                select(ArtifactVersion.commit_sha).where(
                    ArtifactVersion.workspace_id == workspace_id
                )
            ).all()
        )
        backfilled = 0
        for sha, ts, message in git_commits:
            if sha in existing_shas:
                continue
            # INSERT ... SELECT ... WHERE, never INSERT ... VALUES after a check.
            # An INSERT has no target row, so there is no READ COMMITTED re-check
            # to mask a missing fence; check-then-insert is the exact race
            # reproduced on PostgreSQL 16 (see port_impl/effects.py:258-268).
            #
            # THE LOAD-BEARING TERMS ARE THE LEASE ABOVE AND THE LIVENESS CLAUSE
            # HERE — NOT the generation. Under the lease the generation cannot
            # move, so the equality term can never fail; it is kept for
            # uniformity with every other mechanism-1 site. If you are tempted
            # to simplify this, drop the generation term, NEVER the liveness
            # term and NEVER the lease.
            landed = s.execute(
                insert(ArtifactVersion)
                .from_select(
                    [
                        "id",
                        "workspace_id",
                        "commit_sha",
                        "triggering_turn_id",
                        "created_by",
                        "message",
                        "created_at",
                    ],
                    select(
                        literal(uuid.uuid4()),
                        literal(workspace_id),
                        literal(sha),
                        literal(None),
                        literal("reconciled"),
                        literal(message),
                        literal(datetime.fromtimestamp(ts, tz=UTC)),
                    ).where(live_at_generation(workspace_id, generation)),
                )
                .returning(ArtifactVersion.id)
            ).first()
            if landed is None:
                # Under the lease the generation cannot move, so if ANY insert is
                # refused the first one was — nothing has landed yet. Roll back
                # anyway rather than committing a partial batch, and leave every
                # orphan for a later run.
                logger.info(
                    "reconcile_workspace_git_drift: refused for %s — workspace "
                    "not live at generation %s; orphan commits left for a later run",
                    workspace_id,
                    generation,
                )
                s.rollback()
                return 0
            backfilled += 1
        if backfilled > 0:
            # Safe as a plain add: same transaction, still holding the lease, and
            # the predicate above already passed — so the workspace is provably
            # live at `generation` right now.
            s.add(
                WorkspaceEvent(
                    workspace_id=workspace_id,
                    kind="git_drift_reconciled",
                    payload={"backfilled_count": backfilled},
                )
            )
        s.commit()
    return backfilled
```

`id` is supplied explicitly because `FROM SELECT` does not apply Python-side
column defaults. `created_at` is explicit because the commit's real `%ct`
timestamp is the whole point (spec line 258) — the server default would record
when reconciliation ran instead.

- [ ] **Step 4: Run to verify all three pass**

Run:
```
uv run --python 3.12 pytest tests/proposal_assist/test_reconcile.py -v
```
Expected: every test in the file PASSES, including the pre-existing ones.

- [ ] **Step 5: ruff + full suite**

```
uv run --python 3.12 ruff check src tests
uv run --python 3.12 pytest -n 16 --dist loadfile
```
Expected: ruff clean. `3913 passed, 1 skipped` (baseline 3910 + 3 new).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/reconcile.py tests/proposal_assist/test_reconcile.py
git commit -m "fix(reconcile): fence the git-drift backfill against a dead workspace"
```

- [ ] **Step 7: Prove both gates can fail — TWO mutations**

Commit first (Step 6): these are reverted with `git checkout --`, which would
wipe an uncommitted fix.

**Mutation A — remove the predicate.** Change
`.where(live_at_generation(workspace_id, generation))` to `.where(literal(True))`.

Run:
```
uv run --python 3.12 pytest tests/proposal_assist/test_reconcile.py -v
```
Expected: Test A **FAILED**. Test B still passes (the lease is intact).

Restore: `git checkout -- src/call_manager/proposal_assist/reconcile.py`

**Mutation B — remove the lease.** Delete the
`acquire_shared_lease(s, workspace_id)` line.

Run the same command. Expected: Test B **FAILED**. Test A still passes (the
predicate is intact).

Restore: `git checkout -- src/call_manager/proposal_assist/reconcile.py`

Record both. The point of running them separately is to show that neither test is
redundant: each gate has exactly one test that bites for it. **If Mutation B
leaves Test B green, Test B is hollow — fix the test, do not proceed.**

---

### Task 2: Fence `reconcile_workspace_citekeys`

**Files:**
- Modify: `src/call_manager/proposal_assist/reconcile.py:112-229`
- Test: `tests/proposal_assist/test_reconcile_citekeys.py`

**Interfaces:**
- Consumes: `acquire_shared_lease`, `live_at_generation` (already imported by
  Task 1); `commit_mutex_key` (test only).
- Produces: signature unchanged.

This one matters more than the git-drift half in one specific way: it UPDATEs
`references.citekey`, a **user-visible** column served by the references API. The
git-drift half only adds history rows.

- [ ] **Step 1: Write the three failing tests**

Read the existing tests in `tests/proposal_assist/test_reconcile_citekeys.py`
first and **reuse their setup helper** — the fixture that builds a workspace, a
`Reference` with a NULL citekey, and a `citations.json` ledger. Do not write a
new one. Then add:

```python
def test_reconcile_citekeys_repairs_nothing_on_a_soft_deleted_workspace(
    pg_session_factory, tmp_path
):
    """references.citekey is user-visible. A dead workspace must not be mutated."""
    # <same fixture as the file's existing "repairs a NULL citekey" test>
    with pg_session_factory() as s:
        ws = s.get(Workspace, wsid)
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()

    repaired = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)

    assert repaired == 0
    with pg_session_factory() as s:
        assert s.get(Reference, ref_id).citekey is None, (
            "a dead workspace's user-visible column was mutated"
        )


def test_reconcile_citekeys_waits_for_the_commit_mutex_key(pg_session_factory, tmp_path):
    """The lease gate. Structure and reasoning are identical to
    test_reconcile_git_drift_waits_for_the_commit_mutex_key in
    tests/proposal_assist/test_reconcile.py — read that test's docstring; the
    observation must be made WHILE the holder is in force.
    """
    # <same fixture, workspace LIVE>
    done = threading.Event()

    def _reconcile():
        try:
            reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
        finally:
            done.set()

    holder = pg_session_factory()
    observed: list[str | None] = []
    t = threading.Thread(target=_reconcile)
    try:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": commit_mutex_key(wsid)}
        )
        t.start()
        time.sleep(1.5)
        with pg_session_factory() as probe:
            observed.append(probe.get(Reference, ref_id).citekey)
    finally:
        holder.rollback()
        holder.close()
        assert done.wait(timeout=30), "reconcile never finished"
        t.join(timeout=5)

    assert observed == [None], (
        f"while the commit-mutex key was held exclusively the citekey was already "
        f"{observed!r} — reconcile did not take the shared lease"
    )
    with pg_session_factory() as s:
        assert s.get(Reference, ref_id).citekey == "smith2020"


def test_reconcile_citekeys_still_repairs_a_live_workspace(pg_session_factory, tmp_path):
    """Regression guard: the fence must not break the case reconcile exists for."""
    # <same fixture, no soft delete>
    repaired = reconcile_workspace_citekeys(pg_session_factory, wsid, repo_dir)
    assert repaired == 1
    with pg_session_factory() as s:
        assert s.get(Reference, ref_id).citekey == "smith2020"
```

Substitute the real expected citekey from the file's existing fixture for
`"smith2020"` — do not invent one.

- [ ] **Step 2: Run to verify the first two fail**

Run:
```
uv run --python 3.12 pytest tests/proposal_assist/test_reconcile_citekeys.py -v
```
Expected: the soft-deleted test **FAILS** with `assert 1 == 0`; the lease test
**FAILS** with the citekey already repaired; the live test PASSES.

- [ ] **Step 3: Fence the UPDATE**

Add the lease and generation read immediately after opening the session
(currently line 178):

```python
    repaired = 0
    with session_factory() as s:
        # Primitive C first — see reconcile_workspace_git_drift for the reasoning.
        acquire_shared_lease(s, workspace_id)
        generation = s.scalar(
            select(Workspace.lifecycle_generation).where(Workspace.id == workspace_id)
        )
        if generation is None:
            return 0
```

Then replace the write block (currently lines 209-227) with a fenced UPDATE:

```python
            # The fence and the write are ONE statement. THE LOAD-BEARING TERMS
            # ARE THE LEASE AND THE LIVENESS CLAUSE — see the note in
            # reconcile_workspace_git_drift.
            #
            # The savepoint stays. Its documented reason still holds: a
            # concurrent write racing the holder check above can still raise
            # IntegrityError, and only this one row should roll back. The
            # rowcount == 0 branch below is the NEW fence path and is a
            # different case from that IntegrityError.
            try:
                with s.begin_nested():
                    result = s.execute(
                        update(Reference)
                        .where(
                            Reference.id == ref_uuid,
                            Reference.workspace_id == workspace_id,
                            live_at_generation(workspace_id, generation),
                        )
                        .values(citekey=ledger_citekey)
                    )
                if result.rowcount == 0:
                    logger.info(
                        "reconcile_workspace_citekeys: refused for %s — workspace "
                        "not live at generation %s; leaving citekey cache as-is",
                        workspace_id,
                        generation,
                    )
                    s.rollback()
                    return 0
                repaired += 1
            except IntegrityError:
                logger.warning(
                    "reconcile_workspace_citekeys: integrity error repairing "
                    "reference %s -> %r in workspace %s — skipping",
                    ref_uuid,
                    ledger_citekey,
                    workspace_id,
                    exc_info=True,
                )
```

Add `update` to the sqlalchemy import line.

- [ ] **Step 4: Run to verify all pass**

```
uv run --python 3.12 pytest tests/proposal_assist/test_reconcile_citekeys.py -v
```
Expected: all PASS.

- [ ] **Step 5: ruff + full suite**

```
uv run --python 3.12 ruff check src tests
uv run --python 3.12 pytest -n 16 --dist loadfile
```
Expected: ruff clean; `1 skipped`; `3916 passed` (3913 + 3 new).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/reconcile.py tests/proposal_assist/test_reconcile_citekeys.py
git commit -m "fix(reconcile): fence the citekey cache repair against a dead workspace"
```

- [ ] **Step 7: Prove both gates can fail — TWO mutations**

**Mutation A — remove the predicate.** Delete the
`live_at_generation(workspace_id, generation),` line from the UPDATE's `.where()`.
Expected: the soft-deleted test **FAILED**; the lease test still passes.

Restore: `git checkout -- src/call_manager/proposal_assist/reconcile.py`

**Mutation B — remove the lease.** Delete the `acquire_shared_lease(s, workspace_id)`
line from `reconcile_workspace_citekeys` only. Expected: the lease test
**FAILED**; the soft-deleted test still passes.

Restore: `git checkout -- src/call_manager/proposal_assist/reconcile.py`

---

### Task 3: Pin the review-pass protection at the git level

**Files:**
- Test only: `tests/proposal_assist/test_pass_retirement.py`
- Modify: nothing in `src/`.

**This task replaces revision 1's Task 3, which is cut.** Read "What revision 1
got wrong" above before starting. There is no fence to add here — the protection
already exists and works. What is missing is a test that names it.

The protection is an **implicit coupling between two modules**: the delete
transaction cancels the run (`workspaces.py:190` → `review/jobs.py:111-122`), and
the finalize block refuses a non-running run (`pass_runner.py:329-331`). Neither
module's tests state the consequence the coupling actually buys, which is that
**no git commit lands in a workspace deleted mid-pass**.

The mechanism is well covered — `test_the_child_prefix_blocks_on_the_run_row_instead_of_committing_running`,
`test_finalization_takes_the_run_row_before_the_item_row`,
`test_deleting_a_workspace_leaves_no_review_item_in_running`,
`test_subcall_skips_when_run_no_longer_running`. But the git-level consequence is
asserted nowhere: `grep -c "HEAD\|rev-parse\|write_and_commit" tests/proposal_assist/test_pass_retirement.py`
returns **0**. A future refactor of either module could reopen this silently.

**Do not weaken the assertion to make it easy.** A test that deletes the
workspace *before* calling `run_review_pass` passes for the wrong reason — the
prefix returns at `pass_runner.py:242-243` and never reaches the git write. That
was revision 1's mistake (Sol's round-1 finding 4). The delete must land
**between** the prefix and the finalize.

- [ ] **Step 1: Write the test**

Use the file's existing `_seed_review` helper and the `_pausing_adapter_builder`
at `test_pass_retirement.py:278-310`, which parks a turn until a gate is set —
that is exactly the "hold the child between its prefix and its finalization" hook
this needs. Read both before writing.

```python
def test_no_git_commit_lands_for_a_workspace_deleted_mid_review_pass(
    pg_session_factory, current_user, tmp_path, _settings_env
):
    """The git-level consequence of the delete/finalize coupling.

    Nothing else asserts this. The mechanism is covered by the run-row tests in
    this file, but a refactor of either soft_delete_workspace_db's cancel or
    pass_runner's still-running guard could reopen a post-delete git write
    without any of them failing.

    The delete must land BETWEEN the prefix and the finalize. Deleting first
    makes the prefix return at pass_runner.py:242-243, which passes whether or
    not the finalize guard exists — a hollow gate.
    """
    run_id, ws_id = _seed_review(pg_session_factory, current_user)
    repo_dir = workspace_repo_dir(Settings().workspaces_dir, ws_id)
    head_before = _head(repo_dir)

    gate = threading.Event()
    entered = threading.Event()
    done = threading.Event()

    def _pass():
        try:
            asyncio.run(
                run_review_pass(
                    run_id=run_id,
                    claimed_generation=0,
                    session_factory=pg_session_factory,
                    settings=Settings(),
                    adapter_builder=_pausing_adapter_builder(gate, entered),
                )
            )
        finally:
            done.set()

    t = threading.Thread(target=_pass)
    t.start()
    try:
        assert entered.wait(timeout=30), "the pass never reached its turn"
        # The prefix has committed; finalization has not run. Delete FOR REAL —
        # through the service, so the run-cancel in the delete transaction is
        # part of what is under test.
        with pg_session_factory() as s:
            # Signature is (session, *, workspace_id, timeout_seconds=10) —
            # verified at workspaces.py:128. There is NO actor_id parameter, and
            # the function COMMITS ITSELF (workspaces.py:238), so do not add a
            # second commit here.
            soft_delete_workspace_db(s, workspace_id=ws_id)
    finally:
        gate.set()
        assert done.wait(timeout=60), "the pass never finished"
        t.join(timeout=5)

    assert _head(repo_dir) == head_before, (
        "a review pass committed to the repo of a workspace deleted mid-pass"
    )
    with pg_session_factory() as s:
        assert s.get(ReviewRun, run_id).status == "cancelled"
```

Add a `_head` helper to the file if it has none:

```python
def _head(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
```

`_seed_review` may not create a git repo; if
`_head` fails because there is none, extend the seed helper to init one with a
single commit, in the same style as `_init_repo_with_n_commits` in
`tests/proposal_assist/test_reconcile.py`.

- [ ] **Step 2: Run to verify it passes**

Run:
```
uv run --python 3.12 pytest tests/proposal_assist/test_pass_retirement.py -v
```
Expected: **PASSES on unmodified `src/`.** That is the point — this task adds no
production change. If it FAILS, stop: either the test is wrong, or revision 1's
refuted premise was actually right and this plan needs rewriting again. Report
that, do not "fix" it by relaxing the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/proposal_assist/test_pass_retirement.py
git commit -m "test(review): pin that a mid-pass delete leaves git HEAD untouched"
```

- [ ] **Step 4: Prove the test can fail — TWO mutations**

A test that only ever passes is worthless. Both mutations below must make it fail;
each removes one half of the coupling.

**Mutation A — break the finalize guard.** In `pass_runner.py:330`, change
`if run is None or run.status != "running":` to `if run is None:`.

Run the test file. Expected: **FAILED** with HEAD moved (not ERROR).

Restore: `git checkout -- src/call_manager/proposal_assist/review/pass_runner.py`

**Mutation B — break the delete-time cancel.** In `workspaces.py`, comment out
the `cancel_review_runs_and_items_for_workspace(...)` call at line 190.

Run the test file. Expected: **FAILED** with HEAD moved.

Restore: `git checkout -- src/call_manager/workspaces.py`

Record both. If either mutation leaves the test green, the test is not pinning
what this task claims and must be fixed before moving on. Mutation B may also
break other tests in the file — that is fine and expected; what matters is that
**this** test is among the failures.

---

### Task 4: Make `build_bibliography`'s unfenced git write opt-in

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/bibliography.py:581`, `:623-628`
- Test: the existing bibliography tests (locate with
  `grep -rln build_bibliography tests/`)

**Scope, stated honestly** (Sol's round-1 finding 5, accepted): flipping the
default does **not** fence or remove the write path — `bibliography.py:623-628`
still calls `write_and_commit_files` with no mutex and no generation whenever
`commit=True`. This task makes the unsafe path opt-in. It does not make it safe.

The path is **not reachable in production today**: both callers pass
`commit=False` (`export.py:88`, `tools/audit.py:220`). The claim to make is "the
only current production callers pass `commit=False`" — **not** "there is no other
unfenced git path", which this plan has not established across the whole tree.

- [ ] **Step 1: Write the failing test**

```python
def test_build_bibliography_does_not_commit_unless_asked(tmp_path, ...):
    """The git write is unfenced, so it must never be the default.

    bibliography.py calls write_and_commit_files with no mutex and no
    generation. Both current production callers pass commit=False; the default
    must match, so a future caller who forgets the keyword does not get an
    unfenced git write.
    """
    # <reuse the fixture from the file's existing commit test>
    head_before = _head(repo_dir)
    result = build_bibliography(
        proposal_text=proposal_text,
        ledger=ledger,
        session=session,
        workspace_id=wsid,
        style="vancouver",
        repo_dir=repo_dir,
        # commit deliberately omitted
    )
    assert result["commit_sha"] is None
    assert _head(repo_dir) == head_before
```

- [ ] **Step 2: Run to verify it fails**

```
uv run --python 3.12 pytest tests/<bibliography_test_file>.py -v
```
Expected: **FAILS** — `result["commit_sha"]` is a sha and HEAD moved.

- [ ] **Step 3: Flip the default and document why**

At `bibliography.py:581`: `commit: bool = False,`

Replace the docstring sentence "The write path is retained + unit-tested for
optional reuse." with:

```
    ``commit`` defaults to False because the write path calls
    ``write_and_commit_files`` with NO commit mutex and NO lifecycle generation
    — it is an unfenced git write, and this default does not change that, only
    who opts into it. The only current production callers pass ``commit=False``
    explicitly (export.py, tools/audit.py); the default matches them so that
    omitting the keyword cannot produce an unfenced commit. Any future caller
    that genuinely needs to persist must fence this function first.
```

- [ ] **Step 4: Run to verify it passes**

```
uv run --python 3.12 pytest tests/<bibliography_test_file>.py -v
```
Expected: all PASS. Existing tests that exercise the commit path should already
pass `commit=True` explicitly; if any relied on the default, update those call
sites to pass `commit=True` and say so in the task report.

- [ ] **Step 5: ruff + full suite**

```
uv run --python 3.12 ruff check src tests
uv run --python 3.12 pytest -n 16 --dist loadfile
```

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/citations/bibliography.py tests/<bibliography_test_file>.py
git commit -m "fix(citations): make build_bibliography's unfenced git write opt-in"
```

- [ ] **Step 7: Prove the gate can fail (mutation)**

Mutation: set `commit: bool = True` again. Run the bibliography test file.
Expected: the new test reports **FAILED**.

Restore: `git checkout -- src/call_manager/proposal_assist/citations/bibliography.py`

---

### Task 5: Unique index on `(workspace_id, commit_sha)`

**Files:**
- Create: `alembic/versions/0065_artifact_versions_unique_sha.py`
- Create: `tests/test_migration_0065_artifact_versions_unique_sha.py`
- Modify: `src/call_manager/db/models.py:1133-1146` (`ArtifactVersion.__table_args__`)
- Modify: `src/call_manager/proposal_assist/reconcile.py` (add `ON CONFLICT DO NOTHING`)
- Test: `tests/proposal_assist/test_reconcile.py`

**This is a latent bug, not a live one — label it that way in the report.**
`artifact_versions` has no unique constraint on `(workspace_id, commit_sha)`:
`__table_args__` carries only a `CheckConstraint` on `created_by` and a
non-unique `Index`. Reconcile does read-then-insert with no DB-level guard.

Today the only caller is `controller.launch`, which holds
`acquire_workspace_lock` — a non-blocking exclusive advisory lock — so two
concurrent launches cannot both reconcile. **The shared lease from Task 1 does
not prevent this**: shared locks do not conflict with each other, so two
lease-holding reconciles both proceed. The protection is the workspace lock,
which the reconcile functions neither take nor can rely on. `reconcile.py:8-9`
already announces a second caller — "on-demand from /admin (B6/B8 will wire
that)" — which would not hold it.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_reconcile.py`, adding
`from sqlalchemy.exc import IntegrityError` to its imports:

```python
def test_artifact_versions_rejects_a_duplicate_sha_for_one_workspace(pg_session_factory):
    """The DB, not a caller's read-then-insert, must prevent a duplicate version
    row appearing in the user's history."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        wsid = ws.id
        s.commit()
    with pg_session_factory() as s:
        s.add(ArtifactVersion(
            workspace_id=wsid, commit_sha="deadbeef", triggering_turn_id=None,
            created_by="agent", message="first",
        ))
        s.commit()
    with pg_session_factory() as s:
        s.add(ArtifactVersion(
            workspace_id=wsid, commit_sha="deadbeef", triggering_turn_id=None,
            created_by="reconciled", message="duplicate",
        ))
        with pytest.raises(IntegrityError):
            s.commit()
```

- [ ] **Step 2: Run to verify it fails**

```
uv run --python 3.12 pytest tests/proposal_assist/test_reconcile.py::test_artifact_versions_rejects_a_duplicate_sha_for_one_workspace -v
```
Expected: **FAILS** with `DID NOT RAISE IntegrityError`.

- [ ] **Step 3: Write the migration**

**The dedup rule matters and revision 1 got it backwards** (Sol's round-1
finding 3, confirmed). Reconciled rows are stamped with the git commit's own
`%ct` (`reconcile.py:96`), while ordinary rows use the model's
`server_default=func.now()` (`models.py:1164-1169`). A reconciled row for a
commit is therefore routinely **older** than the provenance-bearing row for the
same commit. "Keep the oldest" would keep the `reconciled` row and delete the
`user`/`agent` row — destroying `triggering_turn_id` and the real `created_by`.
**Prefer non-reconciled rows explicitly; use `(created_at, id)` only to break
ties inside one provenance class.**

Create `alembic/versions/0065_artifact_versions_unique_sha.py`:

```python
"""artifact_versions: one row per (workspace, commit sha)

Revision ID: 0065
Revises: 0064
"""

from __future__ import annotations

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reconciliation's read-then-insert has no DB-level guard, so two
    # reconcilers racing (a workspace open and a future admin-triggered run)
    # can each miss the other's row and both insert. A duplicate is
    # user-visible: a second entry in the version history for one commit.
    #
    # Deduplicate before indexing — a deployment that already drifted would
    # otherwise fail the migration.
    #
    # DO NOT rank by created_at alone. A 'reconciled' row carries the GIT
    # COMMIT's timestamp (reconcile.py:96), while a normal row uses the
    # server's now() default, so the reconciled row is routinely the OLDER
    # one. Ranking by age would delete the provenance-bearing row and keep
    # the derived one. Provenance class first; age only breaks ties within it.
    op.execute(
        """
        DELETE FROM artifact_versions a
        USING artifact_versions b
        WHERE a.workspace_id = b.workspace_id
          AND a.commit_sha   = b.commit_sha
          AND (
                (a.created_by = 'reconciled') > (b.created_by = 'reconciled')
             OR (
                  (a.created_by = 'reconciled') = (b.created_by = 'reconciled')
                  AND (a.created_at, a.id) > (b.created_at, b.id)
                )
              )
        """
    )
    op.create_index(
        "uq_artifact_versions_ws_sha",
        "artifact_versions",
        ["workspace_id", "commit_sha"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_artifact_versions_ws_sha", table_name="artifact_versions")
```

**This rule was executed against PostgreSQL 16, not reasoned about.** Measured
on `postgres:16-alpine`:

- `SELECT (true > false), (false > true)` → `t`, `f`. `boolean > boolean` is
  valid and orders `false < true`, so
  `(a.created_by = 'reconciled') > (b.created_by = 'reconciled')` is true exactly
  when `a` is reconciled and `b` is not — i.e. `a` is the one to drop.
- On the realistic drift pair (a `reconciled` row at the git commit's older
  timestamp, and a `user` row at the newer server time carrying
  `triggering_turn_id=99`), **this rule keeps the `user` row with its turn id.**
  Revision 1's naive `(created_at, id)` rule, run on the same two rows, kept the
  `reconciled` row and destroyed the turn id.
- On mixed groups (2 reconciled + 1 user; 3 same-class rows; plus a same-sha row
  under a **different** `workspace_id`), exactly one row survived per
  `(workspace_id, commit_sha)` group, always the non-reconciled one, and the
  other workspace's row was untouched. The comparison is total within each pair,
  so the delete cannot leave two survivors or empty a group.

- [ ] **Step 4: Mirror it in the model**

In `db/models.py`, add to `ArtifactVersion.__table_args__` after the existing
`Index(...)`:

```python
        Index(
            "uq_artifact_versions_ws_sha",
            "workspace_id",
            "commit_sha",
            unique=True,
        ),
```

- [ ] **Step 5: Make reconcile tolerate the constraint**

Task 1's INSERT would now raise on a concurrent duplicate instead of silently
double-inserting. In `reconcile.py`, change the import to
`from sqlalchemy.dialects.postgresql import insert` and append to the INSERT
statement, before `.returning(...)`:

```python
                .on_conflict_do_nothing(index_elements=["workspace_id", "commit_sha"])
```

With `ON CONFLICT DO NOTHING`, `returning(...).first()` is `None` for **both** a
fence refusal and a conflict. Distinguish them so the fence's log stays truthful
— insert this immediately before Task 1's `if landed is None:` refusal branch:

```python
            if landed is None:
                already = s.scalar(
                    select(ArtifactVersion.id).where(
                        ArtifactVersion.workspace_id == workspace_id,
                        ArtifactVersion.commit_sha == sha,
                    )
                )
                if already is not None:
                    # A concurrent reconcile inserted it. Not a fence refusal.
                    continue
```

- [ ] **Step 6: Run to verify it passes**

```
uv run --python 3.12 alembic upgrade head
uv run --python 3.12 pytest tests/proposal_assist/test_reconcile.py -v
```
Expected: all PASS, including Task 1's three tests.

- [ ] **Step 7: Add the migration's own test (repo convention)**

Every migration here gets a dedicated test file (`tests/test_migration_0034_*.py`
through `test_migration_0064_*.py`). Follow the 0064 pattern exactly — read
`tests/test_migration_0064_claimed_at_generation.py` first; it uses
`alembic.config.Config("alembic.ini")`, the `pg_url` fixture, `command.upgrade` /
`command.downgrade`, and `sqlalchemy.inspect`.

Create `tests/test_migration_0065_artifact_versions_unique_sha.py` asserting:

1. at head, `artifact_versions` has an index `uq_artifact_versions_ws_sha` over
   `["workspace_id", "commit_sha"]` with `unique=True`;
2. the pre-existing non-unique `ix_artifact_versions_ws_created` is untouched;
3. the migration round-trips: `upgrade head` → `downgrade 0064` → `upgrade head`
   is clean, and after the downgrade the unique index is absent;
4. **the dedup keeps the right row.** At revision `0064`, seed two rows with the
   same `(workspace_id, commit_sha)`:
   - row R: `created_by='reconciled'`, `created_at` = an **older** timestamp
     (this is the realistic case — it carries the git commit's own time);
   - row P: `created_by='user'`, `created_at` = a **newer** timestamp,
     and a non-NULL `triggering_turn_id`.

   Upgrade to `0065`. Assert exactly one row survives, that it is **P**, and
   that its `triggering_turn_id` is still set.

Point 4 is the one worth the effort. Points 1–3 restate what the migration says;
point 4 is the only one that would have caught revision 1's inverted rule, and a
deployment that has already drifted is precisely when a wrong rule does damage.

Run:
```
uv run --python 3.12 pytest tests/test_migration_0065_artifact_versions_unique_sha.py tests/test_alembic_v2_round_trip.py -v
```
Expected: all PASS. Exit 5 ("no tests collected") means a wrong path — fix it,
never read it as a pass.

- [ ] **Step 8: ruff + full suite**

```
uv run --python 3.12 ruff check src tests
uv run --python 3.12 pytest -n 16 --dist loadfile
```
Expected: ruff clean; `1 skipped`; zero FAILED, zero ERROR.

- [ ] **Step 9: Commit**

```bash
git add alembic/versions/0065_artifact_versions_unique_sha.py \
        src/call_manager/db/models.py \
        src/call_manager/proposal_assist/reconcile.py \
        tests/proposal_assist/test_reconcile.py \
        tests/test_migration_0065_artifact_versions_unique_sha.py
git commit -m "fix(db): one artifact_versions row per (workspace, commit sha)"
```

- [ ] **Step 10: Prove both gates can fail — TWO mutations**

**Mutation A — drop the uniqueness.** In `db/models.py` change the new index's
`unique=True` to `unique=False`, and comment out `op.create_index(...)` in the
migration. Re-create the test database so the migration re-runs.

Run:
```
uv run --python 3.12 pytest tests/proposal_assist/test_reconcile.py -v
```
Expected: the duplicate-sha test **FAILED**.

**Mutation B — restore revision 1's inverted dedup rule.** In the migration,
replace the whole `WHERE` of the `DELETE ... USING` with the naive
`AND (a.created_at, a.id) > (b.created_at, b.id)`.

Run:
```
uv run --python 3.12 pytest tests/test_migration_0065_artifact_versions_unique_sha.py -v
```
Expected: the dedup test (point 4) **FAILED** — the surviving row is the
`reconciled` one.

Restore after each:
```bash
git checkout -- src/call_manager/db/models.py alembic/versions/0065_artifact_versions_unique_sha.py
```

Mutation B is the important one: it is the evidence that point 4 pins the rule
rather than restating it.

---

## Outside review

Revision 1 was reviewed by **Sol (`gpt-5.6-sol`) via GitHub Copilot** on
2026-08-25. Package, rubric and settled-points file:
`.superpowers/review/reconcile-fence*.md` (gitignored). Six findings; five
confirmed against the code, one accepted as a wording correction, none refuted.

| # | Finding | Disposition |
|---|---|---|
| 1 | Task 3's zombie-commit premise is refuted by the delete-time run cancel | **Confirmed** — Task 3 cut, framing rewritten |
| 4 | The Task 3 test could not have failed (prefix returns first) | **Confirmed** — test replaced with the paused-adapter version in the new Task 3 |
| 3 | The dedup rule would delete the provenance row, not the derived one | **Confirmed** — rule rewritten, and point 4 of the migration test pins it |
| 2 | No mutation removed `acquire_shared_lease`; the lease was untested | **Confirmed** — Tasks 1 and 2 each gained a lease test and a lease mutation |
| 5 | Task 4 only changes a default; the unfenced path remains | **Accepted** — scope and claim reworded |
| 6 | Task 2 contradicted itself about removing the savepoint | **Confirmed** — the contradictory sentence is gone |

## Explicitly out of scope

- **Fencing `review/pass_runner.py:349`.** Refuted; see "What revision 1 got
  wrong". Do not re-add it without first defeating the delete-time cancel
  argument with a test that demonstrates HEAD moving after a **committed**
  delete.
- **The clone-on-restore architecture** (restore mints a new workspace UUID).
  Verified to close the lifecycle race — 36/36 clean across two independent
  harnesses — but its payoff was overstated ~9x (621 lines deletable, not
  ~5,900) and one premise is wrong: `collab/room.py:670` `retire_workspace`
  never checkpoints, so nothing is being reordered. Undecided, and independent
  of this plan.
- **Gate 0 re-pricing.** Deferred by the user.
- **Admin hard delete.** `scrub_workspace_bytes` (`workspaces.py:364`) has zero
  production callers and does not remove the git repo.
- **A delete-time git watermark.** Was revision 1's alternative for closing a
  shape that turns out not to exist. Moot.
- **Adding DB-only effects to the fence inventory.** The inventory's unit is the
  git sink; extending it to cover effects like reconcile's is a real gap but a
  much larger piece of work than this plan.
