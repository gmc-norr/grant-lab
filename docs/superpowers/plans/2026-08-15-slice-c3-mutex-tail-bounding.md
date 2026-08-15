# Slice C3 — Mutex-Held Tail Bounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it impossible for a transaction holding the per-workspace commit mutex to stall indefinitely, by replacing `locking.py:207`'s restore-to-DEFAULT with a bounded tail cap.

**Architecture:** One line changes at the root: `SET LOCAL lock_timeout = DEFAULT` becomes `SET LOCAL lock_timeout = '<tail cap>'`, so every post-mutex lock wait in every transaction is bounded at 2s, and a nested re-acquire re-applies the cap instead of wiping it. A new app-level exception handler maps the resulting 55P03 to the same 429 envelope the routes already emit. Background callers already absorb the raise; the collab kernel already evicts; three dead call sites are deleted; `gitio.py`'s nine untimed git subprocesses get a timeout.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL 16, pytest + testcontainers, `uv`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-08-15-slice-c3-mutex-tail-bounding-design.md` (**revision 2** — read §3.1, §4, §4.1 and §5 before Task 1).

**Worktree:** create one at `.worktrees/c3-tail-bound` off `master` (`582ce9a` or later). `.worktrees/` is the project-local worktree directory, gitignored.

**Worktree venv:** `uv sync --python 3.12`. A bare `uv sync` picks the newest interpreter on the box (3.14) because `pyproject.toml` says only `requires-python = ">=3.12"` and there is no `.python-version`. Confirm `python -V` reports 3.12 before running anything.

## Global Constraints

- **The tail cap is 2 seconds.** Derived in spec §3.1 from the DELETE route's 6.0s budget × two-thirds ÷ 2 contended rows. Do not pick a different number; if the arithmetic seems wrong, stop and escalate rather than adjusting the constant.
- **Do not change any mutex ACQUISITION timeout.** The 25 × 10s and 2 × 5s acquisition budgets (`flip.py:32`, `hydrate.py:23`) stay exactly as they are. Only the post-acquisition tail changes.
- **Do not add the tail timeout to the collab kernel's clean-error lists** (`kernel_accept.py:43-53`, `collab_cite.py:34-37`). Spec §4.1: those lists mean "raised before any fragment mutation", a tail timeout can fire either side of the mutation, and listing it would skip eviction after a real mutation — diverging the live doc from the durable log.
- **Do not modify `bounded_lock_waits`, `workspace_write`, or the six routes already migrated** by Slice C2 and the consolidation slice.
- **Every mutation must be md5-verified:** hash before and after, confirm the hash CHANGED, run, restore, confirm the hash matches the original. A no-op mutation proves nothing; that has happened twice in this project.
- **Capture pytest's exit status explicitly** (`; echo "PYTEST_EXIT=$?"`). Piping through `tail` masks it.
- **Commit messages go in a file**, committed with `git commit -F <file>`. Heredocs break in this environment.
- **Full suite baseline on `master`:** 3391 passed, 1 skipped. Nothing may be removed except the three dead call sites' tests in Task 5, whose removal is counted explicitly.

---

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `src/call_manager/main.py` | **Modify.** Register the app-level 55P03 → 429 handler beside the existing `WorkspaceDeleted` / `WorkspaceRetired` ones. | 1 |
| `src/call_manager/proposal_assist/drafting/locking.py` | **Modify.** Add `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS`; replace the restore at :207. | 2 |
| `tests/proposal_assist/test_row_lock_ceilings.py` | **Modify.** The new constant joins `_MUTEX_HELD_CAPS`. | 2 |
| `tests/proposal_assist/test_mutex_tail_bound.py` | **New.** The runtime gates: bounded tail, the nested case, background absorption, kernel eviction, the 503 holdout. | 2, 3, 4 |
| `src/call_manager/proposal_assist/tools/citations.py` | **Modify.** Delete the three unreachable impls and their `_*_locked` helpers. | 5 |
| `src/call_manager/proposal_assist/citations/gitio.py` | **Modify.** Nine `subprocess.run` calls gain a timeout. | 6 |

---

### Task 1: The app-level 55P03 handler

**Files:**
- Modify: `src/call_manager/main.py` (register beside the handler at :512)
- Test: `tests/proposal_assist/test_mutex_tail_bound.py` (new)

**Interfaces:**
- Consumes: `api_error`, `api_request`, `code_for` from `call_manager.web.api.errors`; `BUSY_ERROR_MESSAGE` from `call_manager.proposal_assist.drafting.locking`.
- Produces: nothing importable. Task 2 relies on this handler existing so that its 55P03s answer 429 rather than 500.

**This task goes first on purpose.** It is inert until Task 2 makes anything raise an untranslated 55P03 — no tail is bounded yet, so nothing times out. Landing it second would open a window where a contended tail answers 500.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_mutex_tail_bound.py`:

```python
"""Slice C3 — the mutex-held tail is bounded, and its timeout is a 429.

`acquire_commit_mutex` used to restore `lock_timeout` to DEFAULT (= disabled)
after winning the mutex, so every lock wait afterwards was unbounded WHILE the
transaction held the mutex every other writer for that workspace needs. This
file holds the gates for the fix.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError

from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE


class _Orig(Exception):
    """Stands in for psycopg's error object, which carries the SQLSTATE the
    handler keys on. The REAL 55P03 path is covered by the DB-backed gates
    below; this stub is only for the mapping itself."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("canceling statement due to lock timeout")
        self.sqlstate = sqlstate


def _probe_app(app: FastAPI, sqlstate: str) -> None:
    @app.get("/api/_c3_probe")
    def _probe():  # pragma: no cover - body is the raise
        raise OperationalError("SELECT 1", {}, _Orig(sqlstate))


def test_a_lock_timeout_becomes_the_same_429_envelope_the_routes_emit(_app, client_authed_owner):
    """Byte-parity with the routes' own 429: code `too_many_requests`, the shared
    busy message, and Retry-After: 2."""
    _probe_app(_app, "55P03")
    resp = client_authed_owner.get("/api/_c3_probe")
    assert resp.status_code == 429, resp.text
    assert resp.headers.get("Retry-After") == "2"
    body = resp.json()
    assert body["error"]["code"] == "too_many_requests"
    assert body["error"]["message"] == BUSY_ERROR_MESSAGE


def test_a_non_lock_OperationalError_is_still_a_500(_app, client_authed_owner):
    """The handler must not swallow real database failures. 08006 is
    connection_failure — nothing to do with contention."""
    _probe_app(_app, "08006")
    resp = client_authed_owner.get("/api/_c3_probe")
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "internal_error"
```

- [ ] **Step 2: Run them to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: `test_a_lock_timeout_becomes_the_same_429_envelope_the_routes_emit` FAILS with 500 (the catch-all at `main.py:570` handles it today). The second test PASSES already — that is correct and it is there to stay passing.

- [ ] **Step 3: Register the handler**

In `src/call_manager/main.py`, immediately after the `_workspace_retired` handler (which ends around :533) and BEFORE `@app.exception_handler(AccountInactive)`:

```python
    @app.exception_handler(OperationalError)
    async def _lock_timeout(request: Request, exc: OperationalError):
        # SQLSTATE 55P03 = lock_not_available: a `SET LOCAL lock_timeout` fired.
        # Since Slice C3, `acquire_commit_mutex` leaves a 2s cap in force for the
        # WHOLE mutex-held transaction (drafting/locking.py), so an untranslated
        # 55P03 reaching here is by definition a mutex-held tail giving up rather
        # than pinning the mutex against every other writer. It maps to the same
        # 429 the six `workspace_write` routes already emit, byte for byte.
        #
        # Registered centrally rather than per-route because a local `except` only
        # covers what its `try` encloses, and two sites (`turns.py:197`,
        # `proposals.py:145`) already commit OUTSIDE the relevant try — a local
        # clause would miss commit-time timeouts there. See spec section 5.
        #
        # `collab_checkpoint.py:57` answers 503 for the ACQUISITION failure and
        # keeps its own catch; that is CommitMutexTimeout, a different exception,
        # so it resolves before this handler ever sees anything.
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if sqlstate != "55P03":
            # A real connection/server failure. Same shape as the catch-all below;
            # deliberately NOT re-raised (see that handler's note on empty bodies).
            if api_request(request):
                return api_error(500, "internal_error", "internal server error")
            return PlainTextResponse("Internal Server Error", status_code=500)
        if api_request(request):
            return api_error(
                429, code_for(429), BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            )
        return JSONResponse(
            status_code=429,
            content={"detail": BUSY_ERROR_MESSAGE},
            headers={"Retry-After": "2"},
        )
```

Add the imports it needs at the top of `main.py`, alongside the existing ones:

```python
from sqlalchemy.exc import OperationalError

from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE
from call_manager.web.api.errors import code_for
```

`api_error`, `api_request`, `JSONResponse` and `PlainTextResponse` are already imported in this module — check before adding duplicates; `ruff` will flag a redefinition.

- [ ] **Step 4: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 2 passed, both exits 0.

- [ ] **Step 5: Confirm the handler did not steal the existing 429 path**

```bash
uv run pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: 28 passed. Those routes raise `HTTPException(429)`, which resolves at the `StarletteHTTPException` handler — the new handler must not have changed their envelope.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/main.py tests/proposal_assist/test_mutex_tail_bound.py
git commit -F <message-file>
```

Message subject: `feat(errors): map an untranslated 55P03 to the routes' own 429`

---

### Task 2: Replace the restore — the slice's whole mechanism

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/locking.py` (the constant, and :207)
- Modify: `tests/proposal_assist/test_row_lock_ceilings.py` (`_MUTEX_HELD_CAPS`)
- Test: `tests/proposal_assist/test_mutex_tail_bound.py`

**Interfaces:**
- Consumes: Task 1's handler.
- Produces: `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS: int = 2` in `call_manager.proposal_assist.drafting.locking`. The ceilings test imports it by name.

- [ ] **Step 1: Write the failing gates**

Append to `tests/proposal_assist/test_mutex_tail_bound.py`:

```python
import threading

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex_live
from tests.proposal_assist.conftest import make_workspace


def _hold_workspace_row(pg_session_factory, ws_id):
    """Hold the `workspaces` row FOR UPDATE taking NO mutex — the shape the four
    background pass prefixes really have (row_locks.py:71-77)."""
    holding, release = threading.Event(), threading.Event()

    def _holder():
        with pg_session_factory() as s:
            s.get(Workspace, ws_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    assert holding.wait(timeout=5), "the holder never took the workspaces row"
    return release, t


def test_the_tail_after_the_mutex_is_bounded(pg_session_factory, current_user):
    """The core invariant. A transaction that has WON the mutex must not wait
    forever on a row: before this slice `lock_timeout` was restored to DEFAULT
    (disabled) at locking.py:207, so this call never returned.

    Asserted as "it raised", never as elapsed time — without the bound the
    statement does not return at all, so a hung thread IS the failure.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            acquire_commit_mutex_live(s, ws_id)
            with pytest.raises(OperationalError) as exc:
                s.get(Workspace, ws_id, with_for_update=True)
            sqlstate = getattr(exc.value.orig, "sqlstate", None)
            assert sqlstate == "55P03", f"expected a lock timeout, got {sqlstate}"
            s.rollback()
    finally:
        release.set()
        t.join(timeout=30)


def test_a_nested_acquire_does_not_wipe_the_bound(pg_session_factory, current_user):
    """THE decisive gate. `checkpoint_artifact` re-acquires the same mutex
    mid-tail (kernel.py:202-204 documents the re-entrancy), and the old code
    re-ran `SET LOCAL lock_timeout = DEFAULT` there — erasing any bound its
    caller had established. Ten call sites reach checkpoint_artifact.

    If this test passes while `test_the_tail_after_the_mutex_is_bounded` also
    passes, the fix survives nesting. Mutation: restore the DEFAULT line and this
    must fail.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            acquire_commit_mutex_live(s, ws_id)
            acquire_commit_mutex_live(s, ws_id)  # re-entrant, as checkpoint does
            with pytest.raises(OperationalError) as exc:
                s.get(Workspace, ws_id, with_for_update=True)
            assert getattr(exc.value.orig, "sqlstate", None) == "55P03"
            s.rollback()
    finally:
        release.set()
        t.join(timeout=30)
```

- [ ] **Step 2: Run them to verify they fail — carefully**

```bash
timeout 120 uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q -k "tail_after_the_mutex or nested_acquire" ; echo "PYTEST_EXIT=$?"
```

Expected: **the run HANGS until the 120s `timeout` kills it** — that is the pre-fix behaviour and it is the point of the slice. The holder releases after 30s, so you may instead see the tests fail because `s.get` eventually succeeded and `pytest.raises` saw no exception. Either outcome confirms "unbounded". Record which one you got.

- [ ] **Step 3: Add the constant**

In `src/call_manager/proposal_assist/drafting/locking.py`, immediately after `BUSY_ERROR_MESSAGE` (around :146):

```python
# The cap left in force for the REST of a mutex-held transaction, applied by
# acquire_commit_mutex once it has won the lock. PostgreSQL's own default is 0 =
# disabled, and this line used to restore exactly that — which is why every lock
# wait after the mutex was unbounded while the transaction held the mutex every
# other writer for that workspace needs.
#
# Why 2: `lock_timeout` is a PER-ACQUISITION cap, and a mutex-held transaction may
# wait on up to MAX_CONTENDED_ROWS = 2 distinct rows, against the two-thirds of
# the DELETE route's asserted 6.0s budget that lock stalls may consume (4.0s).
# 2 x 2 = 4.0. tests/proposal_assist/test_row_lock_ceilings.py enforces that
# arithmetic and parses the budget out of the DELETE test rather than restating
# it, so this number cannot drift away from its derivation silently.
#
# It matches every other mutex-held cap in the tree (row_locks.py's three,
# analysis/queue.py:41, phases/setup.py:136), so the six workspace_write routes
# keep setting the same value they already set.
MUTEX_TAIL_LOCK_TIMEOUT_SECONDS = 2
```

- [ ] **Step 4: Replace the restore**

At `locking.py:207`, replace:

```python
    # Restore the default for the rest of the transaction's statements (only
    # reachable on success — after a timeout the txn is aborted and this
    # statement would itself fail).
    session.execute(text("SET LOCAL lock_timeout = DEFAULT"))
```

with:

```python
    # Leave a BOUND in force for the rest of the transaction, rather than the
    # DEFAULT (= 0 = disabled) this used to restore. Only reachable on success —
    # after a timeout the txn is aborted and this statement would itself fail.
    #
    # This is Slice C3's entire mechanism. Restoring DEFAULT here un-bounded every
    # lock wait at all 27 mutex call sites, while the transaction held the mutex
    # every other writer for that workspace needs; one stuck row lock blocked the
    # owner's DELETE indefinitely. Replacing rather than deleting the statement is
    # what makes it nesting-proof: checkpoint_artifact re-acquires this same mutex
    # mid-tail (a re-entrant no-op for the lock), and with a restore it wiped its
    # caller's bound — with a re-applied cap there is nothing left to wipe.
    #
    # The resulting 55P03 is mapped to 429 + Retry-After: 2 by main.py's
    # OperationalError handler, centrally, because two sites already commit
    # outside their try block and a local clause would miss them.
    session.execute(
        text(f"SET LOCAL lock_timeout = '{MUTEX_TAIL_LOCK_TIMEOUT_SECONDS * 1000}ms'")
    )
```

- [ ] **Step 5: Run the gates**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: 4 passed, exit 0, and the run finishes in seconds rather than hanging.

- [ ] **Step 6: Extend the ceilings test**

In `tests/proposal_assist/test_row_lock_ceilings.py`, add to `_MUTEX_HELD_CAPS` (the dict at :150):

```python
_MUTEX_HELD_CAPS = {
    "SETUP_JOB_LOCK_TIMEOUT_SECONDS": row_locks.SETUP_JOB_LOCK_TIMEOUT_SECONDS,
    "WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS": row_locks.WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
    "MUTEX_TAIL_LOCK_TIMEOUT_SECONDS": locking.MUTEX_TAIL_LOCK_TIMEOUT_SECONDS,
}
```

and add the import at the top of that file:

```python
from call_manager.proposal_assist.drafting import locking
```

This cap belongs in `_MUTEX_HELD_CAPS` and not merely `_ALL_CAPS` because it is, by construction, only ever in force while the mutex is held.

- [ ] **Step 7: Run the ceilings test and prove the arithmetic still binds**

```bash
uv run pytest tests/proposal_assist/test_row_lock_ceilings.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: pass.

Then mutate `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS = 2` to `= 3` (md5 before/after; confirm changed) and re-run. Expected: FAIL, because `2 × 3 = 6.0 > 4.0`. Restore and confirm the md5 matches. This proves the constant is genuinely governed rather than merely listed.

- [ ] **Step 8: THE mutation — restore the DEFAULT and prove the nested gate catches it**

Change the new `SET LOCAL` back to `SET LOCAL lock_timeout = DEFAULT` (md5 before/after; confirm changed), then:

```bash
timeout 180 uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: `test_the_tail_after_the_mutex_is_bounded` AND `test_a_nested_acquire_does_not_wipe_the_bound` both fail or hang. Restore and confirm the md5 matches the original.

**This mutation is the whole slice.** If it does not turn something red, stop and escalate — the gates are not testing the mechanism.

- [ ] **Step 9: Run the full suite**

```bash
uv run pytest tests/ -q ; echo "PYTEST_EXIT=$?"
```

Expected: **3395 passed, 1 skipped** (3391 + Task 1's 2 + this task's 2), exit 0. The tail is now bounded everywhere — a regression here means some existing transaction legitimately waits more than 2s on a row while holding the mutex, which is exactly what this slice forbids. Investigate rather than raising the cap.

- [ ] **Step 10: Commit**

```bash
git add src/call_manager/proposal_assist/drafting/locking.py \
        tests/proposal_assist/test_row_lock_ceilings.py \
        tests/proposal_assist/test_mutex_tail_bound.py
git commit -F <message-file>
```

Message subject: `fix(locking): leave a 2s cap in force for the mutex-held tail, not DEFAULT`

---

### Task 3: Prove the background callers really absorb the raise

**Files:**
- Test: `tests/proposal_assist/test_mutex_tail_bound.py`

**Interfaces:**
- Consumes: Task 2's bounded tail.
- Produces: nothing importable.

Spec §4 claims `checkpoint.py:90` and `phases/setup.py:361` need no change because their existing handlers absorb the raise and a recovery path retries the work. **That claim must be tested, not assumed.** A reaper that filters on a status the failure path never sets recovers nothing — this project has shipped exactly that error twice, in prose about these two functions.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_mutex_tail_bound.py`:

```python
from call_manager.db.models import WorkspaceSetupJob


def test_a_bounded_m1_failure_requeues_the_setup_job_rather_than_losing_it(
    pg_session_factory, current_user, tmp_path
):
    """Phase-0's _m1 takes the workspace row FOR UPDATE right after the mutex
    (setup.py:361-362). With the tail bounded that wait now RAISES instead of
    pinning the mutex — which is only safe if _fail requeues.

    Asserted on the job's STATUS, not on the exception: `_fail` sets
    status='queued' and started_at=None for ordinary contention, and only
    dead-letters at MAX_SETUP_ATTEMPTS. A test that merely asserts "it raised"
    would pass just as happily if the job were lost.
    """
    from call_manager.proposal_assist.phases import setup as setup_mod

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.add(WorkspaceSetupJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        job_id = s.scalar(
            select(WorkspaceSetupJob.id).where(WorkspaceSetupJob.workspace_id == ws_id)
        )

    # `client` is never reached: _m1 raises on the row wait, long before any LLM
    # call. The five fields are Phase0Worker's real signature (setup.py:314-318).
    worker = setup_mod.Phase0Worker(
        session_factory=pg_session_factory,
        client=None,
        extractor_version="v1",
        rubric_extractor_version="v1",
        workspaces_dir=str(tmp_path),
    )

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        # run_one() takes NO arguments — it claims the queued job itself
        # (setup.py:320-324) and routes any exception through _fail at :335.
        assert worker.run_one() is True, "the worker did not claim the queued job"
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        job = s.get(WorkspaceSetupJob, job_id)
    assert job.status == "queued", (
        f"a contended _m1 left the setup job in {job.status!r}; ordinary contention "
        f"must requeue (setup.py:633-634), not dead-letter"
    )
    assert job.attempts == 1
    assert job.started_at is None
```

- [ ] **Step 2: Run it**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q -k "requeues_the_setup_job" ; echo "PYTEST_EXIT=$?"
```

Expected: PASS. If it fails because the job dead-lettered or vanished, that is a genuine finding — spec §4's "no change needed" is wrong for this site. Stop and escalate rather than editing the assertion.

- [ ] **Step 3: Write the checkpoint absorption test**

```python
from call_manager.proposal_assist.collab import store
from call_manager.proposal_assist.collab.checkpoint import checkpoint_active_tails


def test_a_bounded_checkpoint_failure_leaves_the_tail_for_the_next_scan(
    pg_session_factory, current_user, tmp_path
):
    """checkpoint_active_tails is a periodic scheduler job (checkpoint.py:192).
    With the tail bounded its work can now raise; that is only safe because a
    rolled-back attempt never advances the watermark, so find_checkpoint_candidates
    re-selects the same room on the next scan (store.py:101-111).

    Asserted as "still a candidate afterwards", not as "it logged" — a job that
    logs and drops the work would satisfy the weaker claim just as well.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    # Give the room a tail. append_update(commit=False) is the room-kernel path;
    # here we only need rows above the (absent) watermark, so min_tail=1 suffices
    # and we avoid depending on COLLAB_CHECKPOINT_MIN_TAIL's production value.
    with pg_session_factory() as s:
        store.append_update(s, workspace_id=ws_id, artifact="draft", update=b"\x00", commit=False)
        s.commit()

    with pg_session_factory() as s:
        before = store.find_checkpoint_candidates(s, min_tail=1)
    assert (ws_id, "draft") in before, "the room should be a checkpoint candidate to begin with"

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        # Must NOT raise out of the scan: errors are per-candidate and swallowed
        # (checkpoint.py:196-197). The bounded tail turns a hang into one of those.
        done = checkpoint_active_tails(
            pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1
        )
    finally:
        release.set()
        t.join(timeout=30)

    assert done == 0, "the contended candidate must not be counted as checkpointed"

    with pg_session_factory() as s:
        after = store.find_checkpoint_candidates(s, min_tail=1)
    assert (ws_id, "draft") in after, (
        "a failed checkpoint advanced the watermark anyway — the tail would now be "
        "skipped forever instead of retried on the next scan"
    )
```

> **Implementer note:** `store.append_update`'s exact keyword names must be read from
> `src/call_manager/proposal_assist/collab/store.py:34-45` and the call above matched to them.
> Everything else in this test — especially both `find_checkpoint_candidates` assertions and
> `done == 0` — is the point of the test and must not be softened.

- [ ] **Step 4: Run both, then commit**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
git add tests/proposal_assist/test_mutex_tail_bound.py
git commit -F <message-file>
```

Message subject: `test(c3): the background callers requeue after a bounded failure, not lose the work`

---

### Task 4: The kernel evicts, and the 503 holdout still wins

**Files:**
- Test: `tests/proposal_assist/test_mutex_tail_bound.py`

**Interfaces:**
- Consumes: Task 2's bounded tail, Task 1's handler.
- Produces: nothing importable.

Two dispositions from spec §4.1 and §5 that are asserted rather than changed, and so need gates.

- [ ] **Step 1: Assert the kernel evicts rather than silently diverging**

```python
def test_a_lock_timeout_is_not_on_either_kernel_clean_list():
    """Spec section 4.1, guarded structurally rather than by driving a room.

    The clean lists mean one specific thing (kernel_accept.py:36-38): these errors
    are raised BEFORE any fragment mutation, so the room doc is untouched and there
    is nothing to evict. A tail lock timeout can fire on EITHER side of the
    mutation — at _load_scoped's FOR UPDATE (clean) or at checkpoint/COMMIT (not) —
    and one exception type cannot distinguish them. Listing it as clean would skip
    eviction in the case where the doc WAS mutated and the transaction rolled back,
    diverging the live in-memory doc from the durable log.

    So the correct behaviour is to evict, and this gate exists to stop a future
    change from "tidying" the timeout onto either list. It is deliberately
    structural: a room-driving test would prove today's behaviour, but this proves
    the DECISION, and it cannot rot or go flaky.
    """
    from call_manager.proposal_assist.citations.collab_cite import _CLEAN_CITE_ERRORS
    from call_manager.proposal_assist.proposals.kernel_accept import _CLEAN_ACCEPT_ERRORS

    for name, clean in (
        ("_CLEAN_ACCEPT_ERRORS", _CLEAN_ACCEPT_ERRORS),
        ("_CLEAN_CITE_ERRORS", _CLEAN_CITE_ERRORS),
    ):
        offenders = [e for e in clean if isinstance(e, type) and issubclass(OperationalError, e)]
        assert not offenders, (
            f"{name} lists {[e.__name__ for e in offenders]}, which would classify a "
            f"mutex-tail lock timeout as raised-before-mutation. It can also fire AFTER "
            f"the fragment mutation, and skipping the evict there leaves the live room "
            f"doc diverged from the durable log. See spec section 4.1."
        )
```

- [ ] **Step 2: Assert `collab_checkpoint` still answers 503**

```python
import functools

from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex


def _wedge_mutex(pg_session_factory, ws_id):
    """Hold the commit mutex until the returned Event is set. Duplicated rather
    than imported from test_api_writers_mutex_straddle.py, which this slice does
    not edit — the same choice test_workspace_write.py made."""
    holding, release = threading.Event(), threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    assert holding.wait(timeout=5), "the holder never acquired the mutex"
    return release, t


def test_the_collab_checkpoint_route_still_answers_503_not_the_new_429(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """web/collab_checkpoint.py:57 is the documented holdout: it maps
    CommitMutexTimeout to 503, not 429. That is the mutex ACQUISITION failure and
    a different exception type, so Task 1's OperationalError handler must never
    see it — this asserts the two mappings did not collapse into one.
    """
    import call_manager.proposal_assist.web.collab_checkpoint as ckpt_mod

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    # Shorten the acquisition wait from the production 10s. Patch the module the
    # route calls, and pass NO call-time keyword there — a call-time keyword
    # overrides a partial's, which is how the consolidation slice's equivalent
    # patch was silently defeated.
    monkeypatch.setattr(
        ckpt_mod,
        "acquire_commit_mutex_live",
        functools.partial(ckpt_mod.acquire_commit_mutex_live, timeout_seconds=1),
        raising=False,
    )

    release, t = _wedge_mutex(pg_session_factory, ws_id)
    try:
        resp = client_authed_owner.post(
            f"/api/workspaces/{ws_id}/collab/draft/checkpoint"
        )
    finally:
        release.set()
        t.join(timeout=30)

    assert resp.status_code == 503, (
        f"expected the documented 503 holdout, got {resp.status_code}: {resp.text}"
    )
```

> **Implementer note:** `collab_checkpoint.py` reaches the mutex through
> `checkpoint_artifact`, so if `acquire_commit_mutex_live` is not a module-level name there,
> patch it on `call_manager.proposal_assist.collab.checkpoint` instead — whichever module
> actually resolves the call. Verify by confirming the test takes about a second rather than
> ten; ten means the patch missed.

- [ ] **Step 3: Run and commit**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
git add tests/proposal_assist/test_mutex_tail_bound.py
git commit -F <message-file>
```

Message subject: `test(c3): the kernel evicts on a bounded failure and the 503 holdout is unchanged`

---

### Task 5: Delete the three unreachable call sites

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py`
- Delete: `tests/proposal_assist/test_tools_citations_deleted.py`, `tests/proposal_assist/tools/test_citations_remove_update.py`, `tests/proposal_assist/tools/test_citations_rename.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. These functions have no importers.

- [ ] **Step 1: Re-verify the claim before deleting anything**

```bash
grep -rn "remove_citation_impl\|update_citation_impl\|rename_citekey_impl" --include=*.py . | grep -v "^./tests/" | grep -v "tools/citations.py"
grep -n "from call_manager.proposal_assist.tools.citations import" -A5 src/call_manager/proposal_assist/tools/registry.py
```

Expected: the first prints **nothing**; the second shows exactly `list_citations_impl` and `propose_citation_impl`. If either differs, STOP — the site is live and must be bounded rather than deleted.

- [ ] **Step 2: Delete the functions**

Remove from `src/call_manager/proposal_assist/tools/citations.py`: `remove_citation_impl` and `_remove_citation_locked` (containing the site at :921), `update_citation_impl` and `_update_citation_locked` (:1053), `rename_citekey_impl` and `_rename_citekey_locked` (:1233). Remove any imports left unused; `ruff` F401 will name them.

- [ ] **Step 3: Delete their tests**

```bash
git rm tests/proposal_assist/test_tools_citations_deleted.py \
       tests/proposal_assist/tools/test_citations_remove_update.py \
       tests/proposal_assist/tools/test_citations_rename.py
```

- [ ] **Step 4: Run the suite and record the new count**

```bash
uv run pytest tests/ -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Record the exact drop in test count in the commit message. A drop larger than the three deleted files' contents means something else broke — investigate before committing.

- [ ] **Step 5: Commit**

Message subject: `refactor(tools): delete three citation tool impls nothing has ever called`

The body must state the count drop and name the verification from Step 1.

---

### Task 6: Bound the git subprocesses

**Files:**
- Modify: `src/call_manager/proposal_assist/citations/gitio.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_GIT_TIMEOUT_SECONDS` in `gitio.py`.

`write_and_commit_files` runs inside `checkpoint_artifact`, i.e. inside a mutex-held tail. Nine `subprocess.run` calls there pass no `timeout=`, so a stalled filesystem stalls a mutex-held transaction with no ceiling — and no lock bound touches that. Without this the slice's headline claim is still false.

- [ ] **Step 1: Add the constant**

At the top of `src/call_manager/proposal_assist/citations/gitio.py`:

```python
# Ceiling on any single git subprocess. write_and_commit_files runs inside a
# mutex-held transaction (via checkpoint_artifact), so an untimed git call stalls
# the mutex for every other writer exactly as an unbounded lock wait did before
# Slice C3 — and no lock timeout can bound a subprocess.
#
# Far above the 2s lock cap on purpose: a git timeout is a genuine filesystem
# failure, not routine contention, so it should be rare and loud rather than a
# routine refusal. Matches phases/setup.py:108's _GIT_TIMEOUT_SECONDS.
_GIT_TIMEOUT_SECONDS = 30.0
```

- [ ] **Step 2: Pass it at all nine call sites**

Add `timeout=_GIT_TIMEOUT_SECONDS,` to the `subprocess.run(...)` calls at lines 87, 96, 103, 113, 120, 148, 178, 198 and 220. Change nothing else about those calls.

- [ ] **Step 3: Verify none was missed**

```bash
grep -c "subprocess.run" src/call_manager/proposal_assist/citations/gitio.py
grep -c "timeout=_GIT_TIMEOUT_SECONDS" src/call_manager/proposal_assist/citations/gitio.py
```

Expected: both print **9**.

- [ ] **Step 4: Run the git-touching suites**

```bash
uv run pytest tests/proposal_assist/ -q -k "git or checkpoint or collab" ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: pass. A `subprocess.TimeoutExpired` anywhere means a test drives git slower than 30s — investigate rather than raising the constant.

- [ ] **Step 5: Commit**

Message subject: `fix(gitio): bound the nine git subprocesses that run inside a mutex-held tail`

---

### Task 7: Document the retiring sites and run the acceptance set

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py`, `editing/save.py`, `citations/human_cite.py`, `analysis/insert.py`, `web/api/workspaces.py` (one comment each)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Add one comment at each of the five retiring sites**

At `tools/citations.py:802`, `editing/save.py:180`, `citations/human_cite.py:102`, `analysis/insert.py:351` and `web/api/workspaces.py:537`, add a comment above the `acquire_commit_mutex_live(...)` call, adjusted to name that site's own severance recheck:

```python
    # Flag-off-only path: with collab enabled this 409s at the severance recheck
    # below, so it is retiring rather than maintained. Slice C3 bounds its tail
    # anyway — the 2s cap now left in force by acquire_commit_mutex applies at
    # every site, including this one, with nothing needed here.
```

- [ ] **Step 2: Run the full acceptance set**

```bash
uv run pytest tests/ -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
EMBEDDING_MODEL=bge-m3 ./scripts/gen_openapi_types.sh
git diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
git status --short
```

Expected: exit 0 everywhere; drift clean; `git status --short` shows only this task's uncommitted comments, and is empty after Step 4.

- [ ] **Step 3: Verify no restore survives anywhere**

```bash
grep -rn "lock_timeout = DEFAULT" src/ || echo "none — good"
```

Expected: `none — good`. This is success criterion 1.

- [ ] **Step 4: Commit**

Message subject: `docs(c3): mark the five flag-off-only mutex sites as retiring`

---

## Success criteria (spec §10)

1. `locking.py` leaves a bounded tail cap in force; no `SET LOCAL lock_timeout = DEFAULT` remains in `src/` (Task 7 Step 3).
2. Restoring that statement turns at least one test red, and one of them exercises the **nested** case (Task 2 Step 8).
3. The ceilings test governs the new constant, with the budget still parsed from the DELETE test, and raising the cap to 3 fails it (Task 2 Step 7).
4. Each of the 13 live sites has a verified disposition: absorbed (Task 3), mapped centrally (Task 1), or asserted unchanged (Task 4).
5. The three dead sites and their tests are gone (Task 5).
6. `gitio.py`'s nine subprocess calls carry a timeout (Task 6 Step 3).
7. Full suite green with nothing removed beyond Task 5's counted deletions; `ruff` clean; OpenAPI drift clean.
