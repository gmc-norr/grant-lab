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
- **Do not add the tail timeout to any of the collab kernel's THREE clean-error lists** — `_CLEAN_ACCEPT_ERRORS` (`kernel_accept.py:43`), `_CLEAN_CITE_ERRORS` (`collab_cite.py:34`) and `_CLEAN_INIT_SPEC_ERRORS` (`collab_cite.py:44`). The third is defined ten lines below the second and is easy to miss; its `except` at `collab_cite.py:142` wraps a body that commits after mutating the doc, so it is exactly as exposed. Spec §4.1: those lists mean "raised before any fragment mutation", a tail timeout can fire either side of the mutation, and listing it would skip eviction after a real mutation — diverging the live doc from the durable log.
- **Do not modify `bounded_lock_waits`, `workspace_write`, or the six routes already migrated** by Slice C2 and the consolidation slice.
- **Every mutation must be md5-verified:** hash before and after, confirm the hash CHANGED, run, restore, confirm the hash matches the original. A no-op mutation proves nothing; that has happened twice in this project.
- **Capture pytest's exit status explicitly** (`; echo "PYTEST_EXIT=$?"`). Piping through `tail` masks it.
- **Commit messages go in a file**, committed with `git commit -F <file>`. Heredocs break in this environment.
- **Full suite baseline on `master`:** 3391 passed, 1 skipped. Nothing may be removed except the tests of the three dead call sites in Task 5, whose removal is counted explicitly — and that is three named tests plus two whole files, NOT three whole files: `test_tools_citations_deleted.py` also covers `insert_citation_impl`, which stays.

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
- Modify: `src/call_manager/proposal_assist/drafting/locking.py` (add `is_lock_timeout`)
- Test: `tests/proposal_assist/test_mutex_tail_bound.py` (new)

**Interfaces:**
- Consumes: `api_error`, `api_request`, `code_for` from `call_manager.web.api.errors`; `BUSY_ERROR_MESSAGE` from `call_manager.proposal_assist.drafting.locking`.
- Produces: `is_lock_timeout(exc) -> bool` in `call_manager.proposal_assist.drafting.locking`. Task 2 rewires `acquire_commit_mutex`'s own SQLSTATE check onto it; Task 4 uses it in `collab_ws.py` and `flip.py`. This handler must exist before Task 2 so that its 55P03s answer 429 rather than 500.

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

Expected: **both fail, and neither fails by returning a status code.** The catch-all at `main.py:570` is registered as `@app.exception_handler(Exception)`, which Starlette routes through `ServerErrorMiddleware` — and that middleware always re-raises after building its response. `client_authed_owner` is constructed with `raise_server_exceptions=True` (`tests/proposal_assist/conftest.py:118`), so today both probes raise `OperationalError` out of the `client.get(...)` call rather than returning 429 or 500.

After Step 4 both pass, and the reason is worth understanding before you write the handler: a handler registered for a NON-`Exception` class (here `OperationalError`) is dispatched by `ExceptionMiddleware`, which returns the response and does **not** re-raise. That is what makes the 500 arm testable at all.

- [ ] **Step 3: Add the shared SQLSTATE classifier**

Three sites already need to answer "is this a lock timeout?" — this handler, and Task 4's
`collab_ws.py` and `flip.py` arms — and `acquire_commit_mutex` has a fourth copy inline. Write
it once. In `src/call_manager/proposal_assist/drafting/locking.py`, immediately above
`acquire_commit_mutex`:

```python
def is_lock_timeout(exc: BaseException) -> bool:
    """True iff *exc* is a Postgres lock_not_available (SQLSTATE 55P03) — a
    `SET LOCAL lock_timeout` that fired.

    Since Slice C3 the mutex-held tail keeps a 2s cap in force, so 55P03 is the
    routine "someone else holds a row I need" signal rather than a server fault.
    Every site that must tell contention from a real database failure calls this,
    so the SQLSTATE check exists exactly once.

    psycopg exposes the code as `sqlstate`; older drivers use `pgcode`. Anything
    that is not a DBAPI error at all (no `.orig`) is not a lock timeout.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == "55P03"
```

- [ ] **Step 4: Register the handler**

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
        if not is_lock_timeout(exc):
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

from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, is_lock_timeout
from call_manager.web.api.errors import code_for
```

`api_error`, `api_request`, `JSONResponse` and `PlainTextResponse` are already imported in this module — check before adding duplicates; `ruff` will flag a redefinition.

- [ ] **Step 5: Run the tests**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 2 passed, both exits 0.

- [ ] **Step 6: Confirm the handler did not steal the existing 429 path**

```bash
uv run pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: 28 passed. Those routes raise `HTTPException(429)`, which resolves at the `StarletteHTTPException` handler — the new handler must not have changed their envelope.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/main.py \
        src/call_manager/proposal_assist/drafting/locking.py \
        tests/proposal_assist/test_mutex_tail_bound.py
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

Then, in the same function, fold its inline SQLSTATE check onto Task 1's helper so there is
exactly one definition of "this is a lock timeout" in the tree. Replace:

```python
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if sqlstate == "55P03":
            raise CommitMutexTimeout(str(workspace_id)) from exc
        raise
```

with:

```python
        if is_lock_timeout(exc):
            raise CommitMutexTimeout(str(workspace_id)) from exc
        raise
```

`is_lock_timeout` is defined in this module (Task 1 Step 3), so no import is needed. Behaviour
is identical — the helper reads `.orig` off the exception exactly as this did.

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
- Test: `tests/proposal_assist/collab/test_collab_checkpoint_scan.py` (the checkpoint half — see Step 3 for why it goes there)

**Interfaces:**
- Consumes: Task 2's bounded tail.
- Produces: nothing importable.

Spec §4 claims `checkpoint.py:90` and `phases/setup.py:361` need no change because their existing handlers absorb the raise and a recovery path retries the work. **That claim must be tested, not assumed.** A reaper that filters on a status the failure path never sets recovers nothing — this project has shipped exactly that error twice, in prose about these two functions.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/test_mutex_tail_bound.py`:

```python
from sqlalchemy import select

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

This half goes in `tests/proposal_assist/collab/test_collab_checkpoint_scan.py`, not in
`test_mutex_tail_bound.py`. That file already has `_mk` — the only helper in the tree that
builds a workspace with a real git repo, collab enabled, and one authored edit, which is exactly
the state a checkpoint needs before it does any real work.

**Contend the right row, or this test proves nothing.** Three separate things would defeat the
obvious version:

1. `acquire_commit_mutex_live` takes the advisory lock and then reads `soft_deleted_at` with a
   **plain** `SELECT` (`locking.py:232-234`). It takes no row lock, so holding the *workspace*
   row does not stall a checkpoint.
2. With no git repo, `checkpoint_artifact` returns a clean no-op at `checkpoint.py:105`
   (`head == ""`) before touching anything.
3. `checkpoint_active_tails` does `done += 1` after `s.commit()` regardless of whether
   `CheckpointResult.committed` is true, so that no-op would still count as success.

The row that actually gets locked is the `collab_artifact_state` row `advance_checkpoint`
updates (`state.py:91-96`), and `enable_collab` has already created it, so it exists to be held.

Add these imports at the top of the file, beside the existing ones:

```python
import threading

from call_manager.db.models import CollabArtifactState
```

Then append:

```python
def _hold_state_row(pg_session_factory, wid, artifact="draft"):
    """Park a FOR UPDATE on the collab_artifact_state row advance_checkpoint
    updates; return (release_event, thread).

    Deliberately NOT the workspace row — acquire_commit_mutex_live's liveness
    re-read is a plain SELECT and takes no row lock, so holding the workspace row
    would let the checkpoint sail straight through.
    """
    holding = threading.Event()
    release = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            s.execute(
                select(CollabArtifactState)
                .where(
                    CollabArtifactState.workspace_id == wid,
                    CollabArtifactState.artifact == artifact,
                )
                .with_for_update()
            ).first()
            holding.set()
            # 15s, not 60: with the tail bounded the waiter gives up after ~2s. If
            # the bound is ever removed this must FAIL in a sane time rather than
            # look like an infrastructure hang.
            release.wait(timeout=15)
            s.rollback()

    t = threading.Thread(target=_holder)
    t.start()
    assert holding.wait(timeout=10), "the holder never took the state row"
    return release, t


def test_a_bounded_checkpoint_failure_leaves_the_tail_for_the_next_scan(
    pg_session_factory, tmp_path
):
    """checkpoint_active_tails is a periodic scheduler job. With the tail bounded
    its work can now raise; spec section 4 claims that is safe because a rolled-back
    attempt never advances the watermark, so find_checkpoint_candidates re-selects
    the same room on the next scan.

    Asserted as "still a candidate, watermark unmoved" rather than "it logged" — a
    job that logs and drops the work would satisfy the weaker claim just as well.
    """
    wid = _mk(pg_session_factory, tmp_path, "c3-absorb@ki.se", "## Background\n\nedited.\n")

    with pg_session_factory() as s:
        assert (wid, "draft") in store.find_checkpoint_candidates(s, min_tail=1)
        before = state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft")

    release, t = _hold_state_row(pg_session_factory, wid)
    try:
        # Must NOT raise out of the scan: errors are per-candidate and swallowed
        # (checkpoint.py's `except Exception` in the scan loop). The bounded tail
        # turns what was a hang into one of those.
        done = cp.checkpoint_active_tails(
            pg_session_factory, workspaces_dir=str(tmp_path), min_tail=1
        )
    finally:
        release.set()
        t.join(timeout=30)

    assert done == 0, "a contended candidate must not be counted as checkpointed"

    with pg_session_factory() as s:
        assert (wid, "draft") in store.find_checkpoint_candidates(s, min_tail=1), (
            "a failed checkpoint advanced the watermark anyway — the tail would now be "
            "skipped forever instead of retried on the next scan"
        )
        assert (
            state.get_checkpoint_watermark(s, workspace_id=wid, artifact="draft") == before
        ), "the rolled-back attempt moved the watermark"
```

The git commit for this checkpoint may already exist on disk when the row wait fails. That is
pre-existing behaviour and is fine: the **watermark** is the retry key, and it is what the
rollback protects. The next scan re-runs, `write_and_commit_files` finds no diff, and the
watermark advances then.

- [ ] **Step 4: Run both, then commit**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
uv run pytest tests/proposal_assist/collab/test_collab_checkpoint_scan.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
git add tests/proposal_assist/test_mutex_tail_bound.py \
        tests/proposal_assist/collab/test_collab_checkpoint_scan.py
git commit -F <message-file>
```

Expected: both pass, all exits 0, and the scan file's existing tests still pass unchanged — the
new one adds a helper and a test, it does not alter `_mk` or any existing assertion.

Mutation for this task: comment out the `assert done == 0` line and confirm the test still fails
on the surviving candidate assertion. If it passes, the two assertions are not independent and
the test is weaker than it reads. md5-verify before and after; restore.

Message subject: `test(c3): the background callers requeue after a bounded failure, not lose the work`

---

### Task 4: The collab dispositions — three clean lists, two sites that need code, one holdout

**Files:**
- Modify: `src/call_manager/proposal_assist/web/collab_ws.py` (new `OperationalError` arm)
- Modify: `src/call_manager/proposal_assist/collab/flip.py` (new `OperationalError` arm)
- Modify: `tests/proposal_assist/conftest.py` (promote the repo-init helper)
- Modify: `tests/proposal_assist/collab/test_collab_checkpoint_scan.py` (use the promoted helper)
- Test: `tests/proposal_assist/test_mutex_tail_bound.py`, `tests/proposal_assist/collab/test_collab_ws_retire_route.py`

**Interfaces:**
- Consumes: `is_lock_timeout` from Task 1; `_hold_workspace_row` from Task 2's test file.
- Produces: `init_workspace_repo(repo: Path, content: str) -> None` in `tests/proposal_assist/conftest.py`.

Spec section 4 sorts the 13 live sites into four classes. Three of them need no code — but two
of the sites in the fourth ("Collab, other") do, and revision 4 of the spec is where that was
found. Both catch `CommitMutexTimeout` **by type**, and a bounded tail raises a raw
`OperationalError`, which walks straight past them. Neither is reachable by Task 1's handler in
the way that matters: a WebSocket has no HTTP status at all, and `flip` runs on read paths where
a 429 is the wrong answer by design.

- [ ] **Step 1: Gate the three clean-error lists**

Append to `tests/proposal_assist/test_mutex_tail_bound.py`:

```python
def test_a_lock_timeout_is_not_on_any_kernel_clean_list():
    """The kernel's clean-error lists mean exactly one thing (kernel_accept.py:36-38):
    these errors are "raised BEFORE any fragment mutation", so the room doc is
    untouched and does NOT need evicting. Every other exception may have left
    room.doc mutated while the transaction rolled back, so it evicts.

    A tail lock timeout can fire on either side of that line — at _load_scoped's
    FOR UPDATE (clean) or at checkpoint/COMMIT (not) — and one exception type
    cannot distinguish them. Listing it as clean would skip eviction in the case
    where the doc WAS mutated and the transaction rolled back, diverging the live
    in-memory doc from the durable log.

    So the correct behaviour is to evict, and this gate exists to stop a future
    change from "tidying" the timeout onto any of the lists. It is deliberately
    structural: a room-driving test would prove today's behaviour, but this proves
    the DECISION, and it cannot rot or go flaky.

    THREE lists, not two. _CLEAN_INIT_SPEC_ERRORS is defined ten lines below
    _CLEAN_CITE_ERRORS and is easy to miss, but its except at collab_cite.py:142
    wraps a body that commits after mutating the doc — exactly as exposed.
    """
    from call_manager.proposal_assist.citations.collab_cite import (
        _CLEAN_CITE_ERRORS,
        _CLEAN_INIT_SPEC_ERRORS,
    )
    from call_manager.proposal_assist.proposals.kernel_accept import _CLEAN_ACCEPT_ERRORS

    for name, clean in (
        ("_CLEAN_ACCEPT_ERRORS", _CLEAN_ACCEPT_ERRORS),
        ("_CLEAN_CITE_ERRORS", _CLEAN_CITE_ERRORS),
        ("_CLEAN_INIT_SPEC_ERRORS", _CLEAN_INIT_SPEC_ERRORS),
    ):
        offenders = [e for e in clean if isinstance(e, type) and issubclass(OperationalError, e)]
        assert not offenders, (
            f"{name} lists {[e.__name__ for e in offenders]}, which would classify a "
            f"mutex-tail lock timeout as raised-before-mutation. It can also fire AFTER "
            f"the fragment mutation, and skipping the evict there leaves the live room "
            f"doc diverged from the durable log. See spec section 4.1."
        )
```

- [ ] **Step 2: Give the WebSocket route its arm**

In `src/call_manager/proposal_assist/web/collab_ws.py`, immediately BEFORE the existing
`except CommitMutexTimeout:` around `MANAGER.acquire`, add:

```python
    except OperationalError as exc:
        # Slice C3: load_or_hydrate persists the seed INSIDE the mutex-held tail
        # (hydrate.py:55-60), and that INSERT takes an FK parent lock on
        # `workspaces` — so with the tail bounded a concurrent holder of that row
        # surfaces here as a raw 55P03. Same animal as CommitMutexTimeout below:
        # contention on a cold room, not a refusal. main.py's handler cannot help —
        # a WebSocket has no HTTP status — so map it to the same 1013. A non-lock
        # OperationalError is a real database failure and keeps its 1011.
        if not is_lock_timeout(exc):
            raise
        with contextlib.suppress(Exception):
            await websocket.close(code=_WS_TRY_AGAIN_LATER)
        return
```

Add the two imports it needs at the top of the module:

```python
from sqlalchemy.exc import OperationalError

from call_manager.proposal_assist.drafting.locking import is_lock_timeout
```

`CommitMutexTimeout` and `contextlib` are already imported there — check before adding
duplicates; `ruff` will flag a redefinition.

- [ ] **Step 3: Test the close code, and prove hydration can really produce it**

The close-code mapping is best proven the way its sibling already is — by stubbing the raise,
which is deterministic and needs no contention. But a stub alone would not show the exception is
reachable, so a second test drives the real code path under a real held row. Both are cheap.

First promote the repo helper. In `tests/proposal_assist/conftest.py`, add (it is a verbatim
move of `_init_repo` from `test_collab_checkpoint_scan.py`, which two files now need):

```python
def init_workspace_repo(repo: Path, content: str) -> None:
    """Create a workspace git repo with one committed artifact file. Several
    collab tests need a workspace whose git HEAD has content — hydration seeds
    from it, and checkpoint no-ops without it."""
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    (repo / PROPOSAL_FILENAME).write_text(content)
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
```

It needs `import subprocess`, `from pathlib import Path` and
`from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME` in that conftest — check
which are already present. Then in `tests/proposal_assist/collab/test_collab_checkpoint_scan.py`
delete the local `_init_repo`, import the new one
(`from tests.proposal_assist.conftest import init_workspace_repo`), and update the single call
inside `_mk`. Leave `subprocess`/`PROPOSAL_FILENAME` imports there only if still used; `ruff`
F401 will tell you.

Append to `tests/proposal_assist/collab/test_collab_ws_retire_route.py`:

```python
def test_a_bounded_hydration_lock_timeout_closes_with_1013_not_1011(
    _app, pg_session_factory, monkeypatch
):
    """The 1013 mapping above covers CommitMutexTimeout, raised while ACQUIRING the
    mutex. Slice C3 bounded the tail AFTER it, so the same cold-room hydration can
    now fail with a raw OperationalError (55P03) from the seed's INSERT. That type
    is not in the route's except list, so without this arm it would be 1011 plus a
    traceback for ordinary contention — and unlike every HTTP site, main.py's
    handler cannot reach a WebSocket.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ws-tailbound@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    class _Orig(Exception):
        sqlstate = "55P03"

    def _boom(*a, **k):
        raise OperationalError("INSERT INTO yjs_updates", {}, _Orig())

    monkeypatch.setattr(collab_ws_module, "load_or_hydrate", _boom)

    client = TestClient(_app)
    client.__enter__()
    try:
        client.cookies.set("session", _cookie(_app, oid))
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
            f"/api/ws/workspaces/{wid}/draft"
        ) as wsconn:
            wsconn.receive_bytes()
        assert exc.value.code == 1013      # NOT 1011
    finally:
        client.__exit__(None, None, None)


def test_a_non_lock_operational_error_still_closes_1011(_app, pg_session_factory, monkeypatch):
    """The arm must not swallow real database failures. 08006 is connection_failure."""
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError

    _app.state.session_factory = pg_session_factory
    with pg_session_factory() as s:
        owner = make_user(s, email="ws-tailbound-real@ki.se")
        ws = make_workspace(s, owner=owner, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        wid, oid = ws.id, owner.id

    class _Orig(Exception):
        sqlstate = "08006"

    def _boom(*a, **k):
        raise OperationalError("SELECT 1", {}, _Orig())

    monkeypatch.setattr(collab_ws_module, "load_or_hydrate", _boom)

    client = TestClient(_app)
    client.__enter__()
    try:
        client.cookies.set("session", _cookie(_app, oid))
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(
            f"/api/ws/workspaces/{wid}/draft"
        ) as wsconn:
            wsconn.receive_bytes()
        assert exc.value.code == 1011      # a real failure keeps its 1011
    finally:
        client.__exit__(None, None, None)
```

Now the reachability half. Append to `tests/proposal_assist/test_mutex_tail_bound.py`:

```python
def test_hydration_really_can_raise_a_lock_timeout_after_the_mutex(
    pg_session_factory, current_user, tmp_path
):
    """The two close-code tests stub the raise, which proves the mapping but not
    that hydration can produce it. This drives the real path: load_or_hydrate takes
    the live mutex and THEN appends the seed (hydrate.py:55-60); that INSERT takes
    an FK parent lock on `workspaces`, which conflicts with a held FOR UPDATE. With
    the tail bounded the wait gives up after 2s with 55P03 instead of hanging.
    """
    from pathlib import Path

    from call_manager.proposal_assist.collab import state as collab_state
    from call_manager.proposal_assist.collab.hydrate import load_or_hydrate
    from call_manager.proposal_assist.drafting.locking import is_lock_timeout
    from tests.proposal_assist.conftest import init_workspace_repo

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        s.commit()
        ws_id = ws.id

    init_workspace_repo(Path(tmp_path) / str(ws_id) / "repo", "## Background\n\nseed.\n")
    with pg_session_factory() as s:
        collab_state.enable_collab(s, workspace_id=ws_id, artifact="draft")
        s.commit()

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s, pytest.raises(OperationalError) as exc:
            load_or_hydrate(
                s, workspace_id=ws_id, artifact="draft", workspaces_dir=str(tmp_path)
            )
    finally:
        release.set()
        t.join(timeout=30)

    assert is_lock_timeout(exc.value), f"expected a 55P03 lock timeout, got {exc.value!r}"
```

- [ ] **Step 4: Give `flip` its arm, and prove the read path still defers**

In `src/call_manager/proposal_assist/collab/flip.py`, extend the commit's `except`:

```python
    try:
        session.commit()  # releases the mutex
    except IntegrityError:
        session.rollback()  # concurrent first read won the insert — same outcome
    except OperationalError as exc:
        # Slice C3: enable_collab's INSERT commits INSIDE the mutex-held tail and
        # takes an FK parent lock on `workspaces`, so this COMMIT can now fail on a
        # bounded row wait. Defer exactly as the acquisition timeout above does —
        # maybe_auto_enable runs on READ paths, and letting this reach main.py's
        # handler would turn a routine read into a 429, which is the outcome the
        # defer arm exists to prevent. A non-lock OperationalError is a real
        # database failure and must propagate.
        if not is_lock_timeout(exc):
            raise
        session.rollback()
        return False
```

Add `OperationalError` to the existing `sqlalchemy.exc` import and `is_lock_timeout` to the
existing `drafting.locking` import.

Append to `tests/proposal_assist/test_mutex_tail_bound.py`:

```python
def test_a_bounded_flip_commit_defers_instead_of_erroring_the_read(
    pg_session_factory, current_user
):
    """maybe_auto_enable is called on READ paths and its contract is to defer, not
    fail: on CommitMutexTimeout it returns False and stays legacy so the next read
    retries. Its enable_collab + commit run AFTER the mutex, so with the tail
    bounded that commit can raise 55P03 — and escaping to the 429 handler would
    defeat the defer arm. The row wait is real, not stubbed: the INSERT takes an FK
    parent lock on `workspaces`.
    """
    from call_manager.proposal_assist.collab import state as collab_state
    from call_manager.proposal_assist.collab.flip import maybe_auto_enable

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        s.commit()
        ws_id = ws.id

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            assert maybe_auto_enable(s, workspace_id=ws_id, artifact="draft") is False
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert (
            collab_state.is_collab_enabled(s, workspace_id=ws_id, artifact="draft") is False
        ), "the deferred flip committed anyway"
```

- [ ] **Step 5: Assert `collab_checkpoint` still answers 503**

```python
import functools
import threading

from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex


def _wedge_mutex(pg_session_factory, ws_id):
    """Hold the workspace commit mutex until released, so the next acquirer times
    out. Returns (release_event, thread)."""
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
    assert holding.wait(timeout=5), "the holder never acquired the mutex"
    return release, t


def test_the_collab_checkpoint_route_still_answers_503_not_the_new_429(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """collab_checkpoint is the documented holdout: it maps CommitMutexTimeout to
    503, not 429. That is the mutex ACQUISITION failure and a different exception
    type, so Task 1's OperationalError handler must never see it — this asserts the
    two mappings did not collapse into one.

    Two things this test must get right, both of which a first draft got wrong.
    The route screens `is_collab_enabled` BEFORE it reaches the mutex and returns
    409 if the artifact is off (collab_checkpoint.py:43), so collab must be enabled
    or the test never exercises anything. And the route does NOT import
    acquire_commit_mutex_live — it reaches the mutex through checkpoint_artifact —
    so the patch target is the `collab.checkpoint` module, with raising=True so a
    future rename fails loudly instead of installing a dead attribute.
    """
    import call_manager.proposal_assist.collab.checkpoint as checkpoint_mod
    from call_manager.proposal_assist.collab import state as collab_state

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="active")
        collab_state.enable_collab(s, workspace_id=ws.id, artifact="draft")
        s.commit()
        ws_id = ws.id

    # Shorten the acquisition wait from the production 10s. Pass NO call-time
    # keyword at the call site — a call-time keyword overrides a partial's, which is
    # how the consolidation slice's equivalent patch was silently defeated.
    monkeypatch.setattr(
        checkpoint_mod,
        "acquire_commit_mutex_live",
        functools.partial(checkpoint_mod.acquire_commit_mutex_live, timeout_seconds=1),
        raising=True,
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

Sanity check on the patch: the test should take about a second, not ten. Ten means the partial
was defeated and the production timeout is still in force.

- [ ] **Step 6: Run and commit**

```bash
uv run pytest tests/proposal_assist/test_mutex_tail_bound.py -q ; echo "PYTEST_EXIT=$?"
uv run pytest tests/proposal_assist/collab/test_collab_ws_retire_route.py -q ; echo "PYTEST_EXIT=$?"
uv run pytest tests/proposal_assist/collab/test_collab_checkpoint_scan.py -q ; echo "PYTEST_EXIT=$?"
uv run pytest tests/proposal_assist/collab/ -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all pass, all exits 0.

Then the two mutations for this task, each md5-verified before and after:

1. Add `OperationalError` to `_CLEAN_INIT_SPEC_ERRORS`. Expect Step 1's gate to fail naming
   `_CLEAN_INIT_SPEC_ERRORS`. Restore.
2. Delete the `OperationalError` arm from `collab_ws.py`. Expect
   `test_a_bounded_hydration_lock_timeout_closes_with_1013_not_1011` to fail with code 1011.
   Restore.

```bash
git add -A
git commit -F <message-file>
```

Message subject: `feat(collab): map a bounded tail timeout to 1013 on the socket and a defer in flip`

---

### Task 5: Delete the three unreachable call sites

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py`
- Modify: `tests/proposal_assist/test_tools_citations_deleted.py` (drop three of its four tests — see Step 3)
- Delete: `tests/proposal_assist/tools/test_citations_remove_update.py`, `tests/proposal_assist/tools/test_citations_rename.py`

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

- [ ] **Step 3: Delete their tests — but NOT the whole of `test_tools_citations_deleted.py`**

That file's name is about deleted **workspaces**, not deleted functions: every test in it
asserts a citation tool refuses once the workspace is soft-deleted. Three of its four tests
belong to the functions Step 2 removes, but the fourth —
`test_insert_citation_refuses` (`:69`) — covers `insert_citation_impl`, which is **not** being
deleted. Its `_insert_citation_locked` is a live production dependency of proposal acceptance
(`proposals/accepting.py:207`). Deleting the file would silently drop a regression for shipping
code.

These two files are exclusively about the three dead functions, so they go whole:

```bash
git rm tests/proposal_assist/tools/test_citations_remove_update.py \
       tests/proposal_assist/tools/test_citations_rename.py
```

In `tests/proposal_assist/test_tools_citations_deleted.py`, delete only
`test_remove_citation_refuses` (`:84`), `test_update_citation_refuses` (`:93`) and
`test_rename_citekey_refuses` (`:104`). Keep `test_insert_citation_refuses` and the module's
fixtures. Trim the `from call_manager.proposal_assist.tools.citations import (...)` block to
the names still used; `ruff` F401 will name the leftovers.

Verify the survivor still binds to live code before moving on:

```bash
uv run pytest tests/proposal_assist/test_tools_citations_deleted.py -q ; echo "PYTEST_EXIT=$?"
grep -rn "_insert_citation_locked" --include=*.py src/
```

Expected: 1 passed; the grep shows the definition in `tools/citations.py` **and** the call in
`proposals/accepting.py`.

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

- [ ] **Step 4: Translate the new exception — `timeout=` alone makes this WORSE**

`subprocess.TimeoutExpired` is a **sibling** of `CalledProcessError` under `SubprocessError`,
not a subclass. Stopping after Step 2 would therefore break three contracts, and the first is a
data-integrity regression, not a cosmetic one — see spec section 7.1.

**(a) `write_and_commit_files`' rollback would be skipped entirely.** The function writes the
working tree, then runs `git add` (`:87`) and `git commit` (`:113`). Its restore-and-reset
rollback lives in `except subprocess.CalledProcessError` at `:130`. A timeout on either call
would sail past it, leaving the repo with modified files and a dirty index while the DB
transaction rolls back. Widen that clause:

```python
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
```

and replace the stderr extraction inside it (which assumes `CalledProcessError`'s shape — on a
`TimeoutExpired`, `stderr` is whatever was captured before the kill and may be `None`) with:

```python
        raw = getattr(e, "stderr", None) or b""
        stderr = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        if isinstance(e, subprocess.TimeoutExpired):
            msg = f"atomic commit timed out after {_GIT_TIMEOUT_SECONDS}s: {stderr.strip()}"
        else:
            msg = f"atomic commit failed: {stderr.strip()}"
```

**(b) The rollback's own `git reset` can time out.** At `:148` it now carries a timeout, and if
that raises it masks the original error and still leaves the tree dirty. Wrap it:

```python
        # A timeout HERE must not mask the original failure: the per-file restore
        # above has already run, and the caller needs the real error. check=False
        # covers a non-zero exit; only the timeout needs catching.
        with contextlib.suppress(subprocess.TimeoutExpired):
            subprocess.run(
                ["git", "reset", "HEAD", "--", *paths],
                cwd=repo_dir,
                check=False,
                capture_output=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=env,
            )
```

Add `import contextlib` at the top of the module if it is not already there.

**(c) The three read helpers promise `GitCommitError`.** `current_head` (`:178`), `blob_sha`
(`:198`) and `read_blob` (`:220`) each catch only `OSError`. Widen each to
`except (OSError, subprocess.TimeoutExpired) as exc:` — the message they already build
(`f"... failed: {exc}"`) reads correctly for both.

- [ ] **Step 5: Test the translation — by injection, not by waiting**

Create `tests/proposal_assist/test_gitio_timeouts.py`:

```python
"""Slice C3 — gitio's subprocesses are bounded, and a timeout is a GitCommitError.

subprocess.TimeoutExpired is a sibling of CalledProcessError, not a subclass, so
adding `timeout=` without widening the handlers would skip write_and_commit_files'
whole restore-and-reset rollback and leak a raw exception out of the read helpers.
Every test here injects the timeout; none waits 30 seconds.
"""

from __future__ import annotations

import subprocess

import pytest

from call_manager.proposal_assist.citations import gitio
from call_manager.proposal_assist.citations.gitio import (
    GitCommitError,
    current_head,
    write_and_commit_files,
)
from tests.proposal_assist.conftest import init_workspace_repo


def test_a_timed_out_commit_is_a_GitCommitError_and_leaves_the_tree_clean(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original
")

    real_run = subprocess.run

    def _run(args, **kw):
        # Let `git add` succeed so the tree really is dirty when the commit dies —
        # this is the exact window where skipping the rollback loses data.
        if args[:2] == ["git", "commit"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)
        return real_run(args, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

    with pytest.raises(GitCommitError) as exc:
        write_and_commit_files(repo, {"PROPOSAL.md": "rewritten
"}, "msg")
    assert "timed out" in str(exc.value)

    # The rollback ran: file content restored and the index reset.
    assert (repo / "PROPOSAL.md").read_text() == "original
"
    staged = real_run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.strip() == "", f"index left dirty: {staged.stdout!r}"


def test_a_reset_timeout_does_not_mask_the_original_failure(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original
")

    real_run = subprocess.run

    def _run(args, **kw):
        if args[:2] == ["git", "commit"] or args[:2] == ["git", "reset"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)
        return real_run(args, **kw)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

    # GitCommitError, NOT a bare TimeoutExpired from inside the handler.
    with pytest.raises(GitCommitError):
        write_and_commit_files(repo, {"PROPOSAL.md": "rewritten
"}, "msg")


def test_a_read_helper_timeout_is_a_GitCommitError(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    init_workspace_repo(repo, "original
")

    def _run(args, **kw):
        raise subprocess.TimeoutExpired(cmd=args, timeout=gitio._GIT_TIMEOUT_SECONDS)

    monkeypatch.setattr(gitio.subprocess, "run", _run)

    with pytest.raises(GitCommitError):
        current_head(repo)
```

Check `write_and_commit_files`' real signature at `gitio.py:30` and match the call above to it
(argument order and the files-mapping shape) before running — do not guess it.

- [ ] **Step 6: Run the git-touching suites**

```bash
uv run pytest tests/proposal_assist/ -q -k "git or checkpoint or collab" ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Also run the new file:

```bash
uv run pytest tests/proposal_assist/test_gitio_timeouts.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: pass, and the whole file finishes in seconds — if any test takes 30s the injection
missed and a real timeout is being waited out. An unexpected `TimeoutExpired` in the *other*
suites means a test drives git slower than 30s; investigate rather than raising the constant.

Mutation: narrow the widened clause back to `except subprocess.CalledProcessError as e:` and
confirm `test_a_timed_out_commit_is_a_GitCommitError_and_leaves_the_tree_clean` fails on the
raw `TimeoutExpired`. md5-verify before and after; restore.

- [ ] **Step 7: Commit**

Message subject: `fix(gitio): bound the nine git subprocesses and translate their timeout`

---

### Task 7: Document the retiring sites and run the acceptance set

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py`, `editing/save.py`, `citations/human_cite.py`, `analysis/insert.py`, `web/api/workspaces.py` (one comment each)
- Modify: `src/call_manager/proposal_assist/row_locks.py` (three stale comments — Step 2)
- Modify: `tests/proposal_assist/test_api_writers_mutex_straddle.py`, `tests/proposal_assist/test_phase0_transaction_boundaries.py` (four stale comments — Step 2)

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

- [ ] **Step 2: Correct every comment that still describes the old restore**

Seven comments across the tree state, as present-tense fact, that the mutex restores
`lock_timeout` to `DEFAULT`. Task 2 made all seven false. They are also why the Step 3 grep
below cannot pass while they stand — it matches comments as readily as SQL.

In `src/call_manager/proposal_assist/row_locks.py` at `:8`, `:83` and `:113`, and in
`tests/proposal_assist/test_api_writers_mutex_straddle.py` at `:13` and `:719`, and in
`tests/proposal_assist/test_phase0_transaction_boundaries.py` at `:412` and `:716`: read each
comment and rewrite the clause so it describes the bound. The substance in every case is the
same — "the mutex restores `lock_timeout = DEFAULT`, so this wait is otherwise unbounded"
becomes "the mutex leaves `MUTEX_TAIL_LOCK_TIMEOUT_SECONDS` in force, so this wait is bounded at
2s even without an explicit `bounded_lock_waits`" — but each sentence sits in a different
argument, so adapt rather than paste.

These are comments only. Do not change a single assertion, timeout value, or test body; this is
the one edit permitted to the straddle file, and its 28 tests must still pass unchanged.

```bash
uv run pytest tests/proposal_assist/test_api_writers_mutex_straddle.py tests/proposal_assist/test_phase0_transaction_boundaries.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: pass, with the same counts as before this task.

- [ ] **Step 3: Run the full acceptance set**

```bash
uv run pytest tests/ -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
EMBEDDING_MODEL=bge-m3 ./scripts/gen_openapi_types.sh
git diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
git status --short
```

Expected: exit 0 everywhere; drift clean; `git status --short` shows only this task's uncommitted comments, and is empty after Step 4.

- [ ] **Step 4: Verify no restore survives anywhere**

```bash
grep -rn "SET LOCAL lock_timeout = DEFAULT" src/ tests/ || echo "none — good"
grep -rn "lock_timeout = DEFAULT" src/ tests/ || echo "none — good"
```

Expected: `none — good` from both. The first is the executable SQL; the second is the wider net
that also catches prose, and it only comes back clean once Step 2 is done. This is success
criterion 1.

- [ ] **Step 5: Commit**

Message subject: `docs(c3): mark the five flag-off-only mutex sites as retiring`

---

## Success criteria (spec §10)

1. `locking.py` leaves a bounded tail cap in force; no `SET LOCAL lock_timeout = DEFAULT` remains in `src/` **or `tests/`**, in SQL or in prose (Task 7 Step 4).
2. Restoring that statement turns at least one test red, and one of them exercises the **nested** case (Task 2 Step 8).
3. The ceilings test governs the new constant, with the budget still parsed from the DELETE test, and raising the cap to 3 fails it (Task 2 Step 7).
4. Each of the 13 live sites has a verified disposition: absorbed (Task 3), mapped centrally (Task 1), asserted unchanged (Task 4 Step 1), or **newly handled** (Task 4 Steps 2 and 4 — `collab_ws.py` and `flip.py`, the two sites spec revision 4 found needed code rather than verification).
5. The three dead sites are gone, along with the two test files exclusive to them — and `test_insert_citation_refuses` survives, because `_insert_citation_locked` is still live via `proposals/accepting.py:207` (Task 5 Step 3).
6. `gitio.py`'s nine subprocess calls carry a timeout **and** a `TimeoutExpired` translates to `GitCommitError` without skipping the rollback (Task 6 Steps 3-5).
7. There is exactly one definition of "this is a lock timeout" in the tree — `is_lock_timeout` — used by the app handler, `acquire_commit_mutex`, `collab_ws.py` and `flip.py` (Tasks 1, 2, 4).
8. Full suite green with nothing removed beyond Task 5's counted deletions; `ruff` clean; OpenAPI drift clean.
