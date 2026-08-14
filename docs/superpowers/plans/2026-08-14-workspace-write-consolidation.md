# `workspace_write` Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the six hand-written copies of the commit-mutex ceremony in the proposal-assist write routes with one `workspace_write` context manager, so the bound-through-COMMIT invariant becomes impossible to express wrongly rather than merely tested.

**Architecture:** A new context manager `workspace_write(session, workspace_id, *, timeout_seconds, what)` takes the live commit mutex, opens `bounded_lock_waits`, yields to the route body, commits inside the bound, rolls back on every exceptional exit, and translates both timeout types to the route-contracted `429 + Retry-After: 2`. Routes keep `require_owner`, their validation, their own row locks and their own freshness re-reads. Migration is one route per commit behind a transitional AST gate that accepts either the legacy or the migrated shape, tightened to migrated-only at the end.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL 16, pytest + testcontainers, `uv`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-08-14-workspace-write-consolidation-design.md` (revision 4). Read §3–§8 before Task 1.

**Worktree:** create one at `.worktrees/workspace-write` off `master` (`eef35ed` or later). Per `CLAUDE.md`, `.worktrees/` is the project-local worktree directory and is gitignored.

## Global Constraints

- **Behaviour-neutral, with exactly one documented exception.** Every status code, response body, header, timeout value and bound message stays identical. The exception is §6: `add_member`'s two no-op paths commit an empty transaction where today they do not.
- **`tests/proposal_assist/test_api_writers_mutex_straddle.py`: exactly one edit is permitted, in Task 1, to the `patch_route_mutex_timeout` helper and its import.** Nothing else in that file may change — no test body, no scenario, no assertion. All 28 tests must pass at every commit. Needing any other change means behaviour moved: stop and escalate rather than reconciling it.
- **Timeout values are unchanged:** `WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS = 2`, `SETUP_JOB_LOCK_TIMEOUT_SECONDS = 2`. Do not alter `src/call_manager/proposal_assist/row_locks.py`.
- **No route docstring may change.** FastAPI publishes route docstrings as the OpenAPI operation description; `frontend/src/api/schema.gen.ts` is a hard CI gate.
- **Each task runs the subset named in its own steps; the full suite runs at Tasks 1, 5, 8 and 9.** Those are the module-completion points (`settings.py` done, `rubric.py` done) plus the manager and the final gate — the places where a regression outside the named subsets is most likely. Baseline on `master` (`eef35ed`): **3381 passed, 1 skipped**. Nothing may be removed, and no task may leave its named subset red.
- **AST gates never match source text.** No regex, no `in`, no `str.index` over source. This repository has shipped a source-text assertion that was a tautology.
- **Every mutation you run must be md5-verified.** Hash the file before and after the edit, confirm the hash CHANGED, run the test, restore the file, confirm the hash matches the original. A mutation that silently no-ops proves nothing; that has happened twice in this project.
- **`uv run pytest` and `uv run ruff check .`** from the worktree root. Capture pytest's exit status explicitly (`; echo "PYTEST_EXIT=$?"`) — piping through `tail` masks it.
- **Commit messages** go in a file and are passed with `git commit -F <file>`. Heredocs break in this environment.

---

## File Structure

| File | Responsibility | Task |
| --- | --- | --- |
| `src/call_manager/proposal_assist/web/_writes.py` | **New.** The `workspace_write` context manager and its private `_busy()` helper. Sibling of `_perms.py`, which is where these routes already get `require_owner`. | 1 |
| `tests/proposal_assist/test_workspace_write.py` | **New.** Direct DB-backed tests of the manager, independent of any route. | 1 |
| `tests/proposal_assist/test_api_writers_mutex_straddle.py` | **Modify, once, minimally.** Only `patch_route_mutex_timeout` and its import — the mutex acquisition moves into `_writes.py`, so the helper's patch target must follow it. No test body changes. | 1 |
| `tests/proposal_assist/test_bounded_region_encloses_commit.py` | **Modify.** Gate 16 becomes the transitional two-arm gate, then the final migrated-only gate. | 2, 9 |
| `src/call_manager/web/api/settings.py` | **Modify.** `update_settings`, `add_member`, `remove_member` migrate; the now-unused lock imports go with the third. | 3, 4, 5 |
| `tests/test_api_settings.py` | **Modify.** Gains the `add_member` no-op pin (§6). | 4 |
| `src/call_manager/web/api/rubric.py` | **Modify.** `update_rubric`, `retry_setup`, `activate` migrate; the now-unused lock imports go with the third. | 6, 7, 8 |

---

### Task 1: The `workspace_write` context manager

**Files:**
- Create: `src/call_manager/proposal_assist/web/_writes.py`
- Test: `tests/proposal_assist/test_workspace_write.py`

**Interfaces:**
- Consumes: `acquire_commit_mutex_live(session, workspace_id, timeout_seconds=10)`, `CommitMutexTimeout`, `WorkspaceDeleted`, `BUSY_ERROR_MESSAGE` from `call_manager.proposal_assist.drafting.locking`; `bounded_lock_waits(session, *, timeout_seconds, error=RowLockTimeout, message=...)` and `RowLockTimeout` from `call_manager.proposal_assist.row_locks`.
- Produces: `workspace_write(session: Session, workspace_id: uuid.UUID, *, timeout_seconds: float, what: str) -> Iterator[None]` — a `@contextmanager`. Tasks 3–8 import it as `from call_manager.proposal_assist.web._writes import workspace_write`.

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/test_workspace_write.py`:

```python
"""Direct tests of `workspace_write` — the transaction scope the six
proposal-assist owner-write routes run their body inside.

These exercise the manager itself, NOT a route. The route-level behaviour is
already pinned by tests/proposal_assist/test_api_writers_mutex_straddle.py,
which this slice must leave untouched; what is NOT pinned there is the
manager's contract when the caller does something the six routes happen not to
do — most importantly, catching the exception and reusing the session.
"""

from __future__ import annotations

import functools
import threading
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

import call_manager.proposal_assist.web._writes as writes_module
from call_manager.db.models import Workspace, WorkspaceEvent
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    WorkspaceDeleted,
    acquire_commit_mutex,
)
from call_manager.proposal_assist.web._writes import workspace_write
from tests.proposal_assist.conftest import make_workspace


def _hold_mutex(pg_session_factory, ws_id):
    """Hold the commit mutex until the returned Event is set. Mirrors
    `wedge_mutex` in test_api_writers_mutex_straddle.py; duplicated rather than
    imported because that file must not be edited by this slice."""
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


def _hold_workspace_row(pg_session_factory, ws_id):
    """Hold the `workspaces` row FOR UPDATE, taking NO mutex, until the returned
    Event is set — so a manager-scoped body that takes the same row blocks and
    its bound fires."""
    holding = threading.Event()
    release = threading.Event()

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


def _short_mutex_wait(monkeypatch, seconds: int = 1) -> None:
    """Shorten the manager's mutex wait from the production 10s.

    Patches the module global `workspace_write` looks up at call time. This only
    works because the manager calls `acquire_commit_mutex_live(session, ws_id)`
    with NO timeout_seconds keyword — a call-time keyword would override this
    partial's and silently restore the 10s default (spec §3).
    """
    monkeypatch.setattr(
        writes_module,
        "acquire_commit_mutex_live",
        functools.partial(writes_module.acquire_commit_mutex_live, timeout_seconds=seconds),
    )


def test_a_clean_exit_commits(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        ws = s.get(Workspace, ws_id)
        with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
            ws.title = "written under the manager"

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).title == "written under the manager"


def _assert_busy(exc: HTTPException) -> None:
    assert exc.status_code == 429
    assert exc.detail == BUSY_ERROR_MESSAGE
    assert exc.headers == {"Retry-After": "2"}


def test_a_wedged_mutex_raises_the_contracted_429(pg_session_factory, current_user, monkeypatch):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    _short_mutex_wait(monkeypatch)
    release, t = _hold_mutex(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            with pytest.raises(HTTPException) as exc:
                with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                    pytest.fail("the body must not run when the mutex is wedged")
            _assert_busy(exc.value)
            assert not s.in_transaction(), (
                "workspace_write left the transaction open after a mutex timeout"
            )
    finally:
        release.set()
        t.join(timeout=30)


def test_a_held_row_raises_the_contracted_429(pg_session_factory, current_user):
    """The RowLockTimeout arm. No route uses the manager yet, so if this
    translation is broken nothing else in the suite catches it.

    The manager does not take rows — the body does, exactly as the migrated
    routes will. The manager's job is that the resulting 55P03 comes back as the
    contracted 429 rather than escaping as a raw OperationalError (a 500).
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    release, t = _hold_workspace_row(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            ws = s.get(Workspace, ws_id)
            with pytest.raises(HTTPException) as exc:
                with workspace_write(s, ws_id, timeout_seconds=1, what="test"):
                    # Blocks on the holder; the bound fires after 1s.
                    s.refresh(ws, with_for_update=True)
            _assert_busy(exc.value)
            assert not s.in_transaction(), (
                "workspace_write left the transaction open after a row-lock timeout — "
                "a caller that reuses this session hits 'current transaction is aborted'"
            )
    finally:
        release.set()
        t.join(timeout=30)


def test_a_body_HTTPException_propagates_unchanged_and_commits_nothing(
    pg_session_factory, current_user
):
    """The exit `retry_setup` (404/409) and `activate` (409) depend on. Tested
    with the exception type they actually raise, not a stand-in."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        with pytest.raises(HTTPException) as exc:
            with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                s.add(
                    WorkspaceEvent(
                        workspace_id=ws_id,
                        kind="should_never_land_409",
                        actor_id=current_user.id,
                        payload={},
                    )
                )
                raise HTTPException(status_code=409, detail="not in that state")
        assert exc.value.status_code == 409, "the manager must not rewrite a body HTTPException"
        assert exc.value.detail == "not in that state"
        assert not s.in_transaction()

    with pg_session_factory() as s:
        kinds = [e.kind for e in s.query(WorkspaceEvent).filter_by(workspace_id=ws_id).all()]
    assert "should_never_land_409" not in kinds


def test_a_deleted_workspace_raises_WorkspaceDeleted_not_a_429(pg_session_factory, current_user):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.soft_deleted_at = datetime.now(UTC)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                pytest.fail("the body must not run for a deleted workspace")
        assert not s.in_transaction(), (
            "workspace_write left the transaction open after WorkspaceDeleted"
        )


def test_a_body_exception_propagates_and_rolls_back_for_a_REUSED_session(
    pg_session_factory, current_user
):
    """The contract that cannot be proved by throwing the session away.

    The six routes each sit inside `with session_factory() as session:`, whose
    exit closes the session and discards the transaction — so "nothing is
    committed" holds today by accident of the caller. This test catches the
    exception OUTSIDE the manager, keeps using the same session, and commits it.
    Without the manager's rollback the body's write persists and this fails.
    """
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        with pytest.raises(RuntimeError, match="deliberate"):
            with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                s.add(
                    WorkspaceEvent(
                        workspace_id=ws_id,
                        kind="should_never_land",
                        actor_id=current_user.id,
                        payload={},
                    )
                )
                raise RuntimeError("deliberate")
        # The caller carries on with the SAME session, as a non-route caller may.
        s.commit()

    with pg_session_factory() as s:
        kinds = [
            e.kind
            for e in s.query(WorkspaceEvent).filter_by(workspace_id=ws_id).all()
        ]
    assert "should_never_land" not in kinds, (
        "the body's write survived a body exception — workspace_write did not "
        "roll back, so a caller that reuses the session persists a write the "
        "route decided against"
    )


def test_a_timeout_leaves_the_session_usable_rather_than_aborted(
    pg_session_factory, current_user, monkeypatch
):
    """After a 55P03 the Postgres transaction is aborted and refuses further
    statements. The manager's rollback is what clears it; without it a caller
    that catches the 429 and reuses the session gets a confusing failure far
    from its cause."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    _short_mutex_wait(monkeypatch)
    release, t = _hold_mutex(pg_session_factory, ws_id)
    try:
        with pg_session_factory() as s:
            with pytest.raises(HTTPException):
                with workspace_write(s, ws_id, timeout_seconds=2, what="test"):
                    pytest.fail("unreachable")
            # Must not raise "current transaction is aborted".
            assert s.get(Workspace, ws_id) is not None
    finally:
        release.set()
        t.join(timeout=30)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/proposal_assist/test_workspace_write.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: collection error — `ModuleNotFoundError: No module named 'call_manager.proposal_assist.web._writes'`.

- [ ] **Step 3: Write the implementation**

Create `src/call_manager/proposal_assist/web/_writes.py`:

```python
"""`workspace_write` — the transaction scope for a mutex-held workspace write.

Slice C2 put six owner-write routes under the per-workspace commit mutex and
required each of them to bound every lock wait in the transaction THROUGH
`session.commit()`. It expressed that as a six-step ceremony hand-written at six
call sites, and the ceremony was wrong at four of them before review caught it:
a bound that stops short of the commit leaves the write bounded but UNtranslated,
so a contended row answers 500 where the slice contracts for 429.

This manager exists so the rule cannot be written wrongly. A route that uses it
does not write a commit at all, so it cannot put the commit outside the bound.

WHAT IT DOES NOT DO, deliberately:
  - authorization. `require_owner` stays visible in the route, above the `with`.
    Slice C2 established that authorization precedes semantic validation which
    precedes any lock; folding authz in here would hide that ordering.
  - validation. Same reason. Note `add_member`/`remove_member` validate INSIDE
    the block on purpose — their validation is a DB read that must not run
    before the mutex.
  - row locks. The route writes its own `.with_for_update()`.
  - freshness re-reads. `activate`'s re-read must sit AFTER its setup-job lock
    to match `phases/setup.py`'s `_m3` (M -> J -> W); a parameter here would run
    it at the top of the block and invert that order, opening a real 40P01. The
    re-reads stay in the route bodies where their position is visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy.orm import Session

from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
    acquire_commit_mutex_live,
)
from call_manager.proposal_assist.row_locks import RowLockTimeout, bounded_lock_waits


def _busy() -> HTTPException:
    """The one spelling of the busy response. Byte-identical to the twelve
    hand-written copies this manager replaces."""
    return HTTPException(
        status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
    )


@contextmanager
def workspace_write(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    timeout_seconds: float,
    what: str,
) -> Iterator[None]:
    """Run the body as a mutex-held, fully bounded workspace write.

    Takes the live commit mutex, opens `bounded_lock_waits` for the rest of the
    transaction, yields, and COMMITS INSIDE THE BOUND. Both timeout types become
    `429 + Retry-After: 2`. `WorkspaceDeleted` propagates untouched (main.py
    answers 404 — the documented straddle contract), as does an `HTTPException`
    raised by the body, which is what preserves `retry_setup`'s 404/409 and
    `activate`'s 409.

    Rolls back on EVERY exceptional exit. That is not redundant with the
    caller's `with session_factory()`: without it, a caller that catches the
    exception and reuses the session carries the body's pending writes into its
    next commit, and after a 55P03 the transaction is aborted and refuses
    further statements. The rollback also clears the `SET LOCAL lock_timeout`
    the bound deliberately leaves in force, which is correct — that setting is
    scoped to the transaction being ended.

    DO NOT pass `timeout_seconds` to `acquire_commit_mutex_live` below, not even
    from a parameter defaulting to 10. Both the straddle harness and this
    manager's own tests shorten the mutex wait by replacing the module global
    with `functools.partial(..., timeout_seconds=1)`, and a call-time keyword
    OVERRIDES a partial's keyword — so passing it explicitly silently restores
    the production 10s and makes every wedged-mutex test wait ten seconds
    instead of one. Spec §3 states this as a requirement.
    """
    committed = False
    try:
        try:
            acquire_commit_mutex_live(session, workspace_id)
        except CommitMutexTimeout as e:
            raise _busy() from e
        with bounded_lock_waits(
            session,
            timeout_seconds=timeout_seconds,
            message=f"timed out waiting for a row lock in the {what} transaction",
        ):
            yield
            session.commit()
            committed = True
    except RowLockTimeout as e:
        raise _busy() from e
    finally:
        if not committed:
            session.rollback()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/proposal_assist/test_workspace_write.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: 7 passed, `PYTEST_EXIT=0`, `RUFF_EXIT=0`. A `ruff` F401 here means an import in the test file is unused — remove it rather than silencing it.

Check the wall-clock too: the two mutex tests must take about a second each, not ten. Ten means `_short_mutex_wait` is not taking effect — almost certainly because the manager passed `timeout_seconds` explicitly and overrode the patched partial (spec §3).

- [ ] **Step 5: Prove the rollback and both timeout arms are not vacuous**

Three mutations, each md5-verified before and after and restored:

**(a) Delete the `finally:` block's two lines.** Expected: `test_a_body_exception_propagates_unchanged_and_commits_nothing`… — specifically `test_a_body_exception_propagates_and_rolls_back_for_a_REUSED_session` FAILS on the surviving write, `test_a_timeout_leaves_the_session_usable_rather_than_aborted` FAILS, and every `not session.in_transaction()` assertion FAILS. If any of them still passes, that assertion is not testing the rollback — fix the test, not the implementation.

**(b) Change `except RowLockTimeout as e: raise _busy() from e` to `raise`.** Expected: `test_a_held_row_raises_the_contracted_429` FAILS with a raw `OperationalError`/`RowLockTimeout` instead of `HTTPException`. This is the arm no route can cover yet.

**(c) Change `except CommitMutexTimeout as e: raise _busy() from e` to `raise`.** Expected: `test_a_wedged_mutex_raises_the_contracted_429` FAILS with `CommitMutexTimeout`.

Record all three with the observed failure text.

- [ ] **Step 6: Retarget the straddle harness's mutex-timeout patch**

This is the one permitted edit to `tests/proposal_assist/test_api_writers_mutex_straddle.py` (Global Constraints). It must land **now**, before any route migrates, so the helper works throughout.

Why it is forced: that helper shortens the mutex wait by patching `acquire_commit_mutex_live` **on the route module**. Tasks 3–8 move that call into `_writes.py`, so the name stops being present or called there and `monkeypatch.setattr` fails — breaking the six `*_returns_429_when_the_commit_mutex_is_wedged` tests. The nine `*_LOCK_TIMEOUT_SECONDS` patches in the same file are NOT affected and must not be touched: a migrated route still reads those module globals to pass `timeout_seconds=`.

Add to that file's imports:

```python
from call_manager.proposal_assist.web import _writes as writes_module
```

Replace the body of `patch_route_mutex_timeout` (currently lines 148–157) with:

```python
def patch_route_mutex_timeout(monkeypatch, module, seconds: int = 1) -> None:
    """Pin a short mutex timeout for one route module. We are exercising the
    route's error mapping, not the production timeout value, and the 10s default
    would stall the suite.

    Patches BOTH the route module and `_writes`, tolerating the name's absence on
    either, because the routes migrate one commit at a time: before a route moves
    it calls `acquire_commit_mutex_live` itself; afterwards `workspace_write`
    does. `module` is retained so each caller still names the module under test.
    """
    for target in (module, writes_module):
        current = getattr(target, "acquire_commit_mutex_live", None)
        if current is not None:
            monkeypatch.setattr(
                target,
                "acquire_commit_mutex_live",
                functools.partial(current, timeout_seconds=seconds),
            )
```

Change nothing else in that file. Verify:

```bash
git diff --stat tests/proposal_assist/test_api_writers_mutex_straddle.py
uv run pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: the diff touches only the import and that helper; 28 passed, `PYTEST_EXIT=0` — the six wedged-mutex tests still complete quickly, because the route-module patch is still the live one at this point.

- [ ] **Step 7: Run the full suite**

```bash
uv run pytest tests/ -q ; echo "PYTEST_EXIT=$?"
```

Expected: **3381 + 7 = 3388 passed, 1 skipped**, `PYTEST_EXIT=0`. This is one of the four full-suite checkpoints (Global Constraints).

- [ ] **Step 8: Commit**

```bash
git add src/call_manager/proposal_assist/web/_writes.py \
        tests/proposal_assist/test_workspace_write.py \
        tests/proposal_assist/test_api_writers_mutex_straddle.py
git commit -F <message-file>
```

Message subject: `feat(writes): workspace_write — one transaction scope for the mutex-held routes`

The message body must record why the straddle harness was touched: the mutex acquisition moves into `_writes`, so `patch_route_mutex_timeout`'s target follows it; no test body, scenario or assertion changed.

---

### Task 2: The transitional gate

**Files:**
- Modify: `tests/proposal_assist/test_bounded_region_encloses_commit.py`

**Interfaces:**
- Consumes: `workspace_write` from Task 1 (by name only — the gate matches AST nodes, it does not import it).
- Produces: a gate that accepts each of the six routes in exactly one of two shapes. Tasks 3–8 each flip one route from the legacy arm to the migrated arm.

**Why this task exists and why it is second.** Today's gate asserts `len(blocks) == 1` for each route's `bounded_lock_waits` blocks, and its module-level presence check fails if a listed route stops using one. So the FIRST migrated route turns it red — not the sixth — and it stays red through the fifth. Tightening straight to the final rule instead would leave the five un-migrated routes unguarded. The gate must therefore accept both shapes for the duration, and it must land while all six routes are still legacy so it lands green.

- [ ] **Step 1: Generalise the two block-matching helpers**

Replace `_calls_bounded_lock_waits` and `_bound_blocks` (currently lines 152–171) with:

```python
def _calls(item: ast.withitem, name: str) -> bool:
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    # Matches both `name(...)` and `module.name(...)`.
    if isinstance(func, ast.Name):
        return func.id == name
    if isinstance(func, ast.Attribute):
        return func.attr == name
    return False


def _blocks_calling(fn: ast.AST, name: str) -> list[ast.With | ast.AsyncWith]:
    return [
        node
        for node in ast.walk(fn)
        if isinstance(node, (ast.With, ast.AsyncWith))
        and any(_calls(item, name) for item in node.items)
    ]


def _bound_blocks(fn: ast.AST) -> list[ast.With | ast.AsyncWith]:
    return _blocks_calling(fn, "bounded_lock_waits")


def _managed_blocks(fn: ast.AST) -> list[ast.With | ast.AsyncWith]:
    return _blocks_calling(fn, "workspace_write")
```

- [ ] **Step 2: Widen the presence check to either shape**

Replace `_discovered_bounded_writers` (currently lines 191–196) with:

```python
def _discovered_bounded_writers(module_name: str) -> set[str]:
    """Routes taking EITHER shape: a legacy `bounded_lock_waits` bound, or a
    migrated `workspace_write` scope. During the migration a module holds a mix,
    and a route that has neither must still be reported as missing."""
    return {
        name
        for name, fn in _module_level_functions(_parse(module_name)).items()
        if _bound_blocks(fn) or _managed_blocks(fn)
    }
```

The two assertion messages in `test_the_six_bounded_writers_are_all_present` refer to `bounded_lock_waits` by name. Change both to `bounded_lock_waits or workspace_write` so a failure names what it actually looked for.

- [ ] **Step 3: Replace the per-route rule with the two-arm rule**

Replace `test_session_commit_sits_inside_the_bounded_lock_waits_block` entirely with:

```python
@pytest.mark.parametrize(
    ("module_name", "route"),
    sorted(
        (module_name, route)
        for module_name, routes in EXPECTED_BOUNDED_WRITERS.items()
        for route in routes
    ),
)
def test_each_route_is_exactly_one_of_legacy_or_migrated(module_name: str, route: str):
    """TRANSITIONAL (see this file's docstring). Each route must be exactly one of:

      legacy:   one `with bounded_lock_waits(...)` block with `session.commit()`
                lexically inside it and no commit outside it;
      migrated: one `with workspace_write(...)` block and NO `session.commit()`
                anywhere in the route.

    Both arms enforce the same rule — the transaction is committed inside the
    bound — the migrated arm by making the alternative unwriteable. Anything
    else fails: a half-migrated route holding both constructs, a route with
    neither, or a migrated route that kept a stray commit.

    The legacy arm is deleted once the last route migrates.
    """
    fn = _module_level_functions(_parse(module_name)).get(route)
    assert fn is not None, (
        f"{module_name}.{route} is not a module-level function any more — neither "
        f"arm of this rule can be checked for it. See "
        f"test_the_six_bounded_writers_are_all_present."
    )

    legacy = _bound_blocks(fn)
    migrated = _managed_blocks(fn)
    commits = _session_commit_calls(fn)

    assert not (legacy and migrated), (
        f"{module_name}.{route} has BOTH a `with bounded_lock_waits(...)` block and a "
        f"`with workspace_write(...)` block. A half-migrated route is neither shape: "
        f"finish the migration (drop the legacy bound, drop the commit) or revert it."
    )
    assert legacy or migrated, (
        f"{module_name}.{route} has neither a `with bounded_lock_waits(...)` block nor "
        f"a `with workspace_write(...)` block, so nothing bounds its lock waits through "
        f"COMMIT. A contended row makes it answer 500 with a raw OperationalError "
        f"instead of the contracted 429 + Retry-After: 2."
    )

    if migrated:
        assert len(migrated) == 1, (
            f"{module_name}.{route} has {len(migrated)} `with workspace_write(...)` "
            f"blocks; this gate is written for exactly one write scope per route."
        )
        assert not commits, (
            f"{module_name}.{route} uses `workspace_write` but still calls "
            f"session.commit() at line(s) {[c.lineno for c in commits]}. The manager "
            f"owns the commit — a route-level commit either double-commits or commits "
            f"outside the managed scope. Delete it."
        )
        return

    assert len(legacy) == 1, (
        f"{module_name}.{route} has {len(legacy)} `with bounded_lock_waits(...)` blocks; "
        f"this gate is written for exactly one bounded region per route. If the route "
        f"genuinely needs two, teach this check which commit belongs to which."
    )
    bound = legacy[0]
    inside = {id(c) for c in _session_commit_calls(bound)}
    outside = [c for c in commits if id(c) not in inside]
    assert inside, (
        f"{module_name}.{route}: session.commit() is NOT inside its "
        f"`with bounded_lock_waits(...)` block (bound at line {bound.lineno}"
        + (f", commit at line {outside[0].lineno}" if outside else "")
        + "). bounded_lock_waits leaves SET LOCAL lock_timeout in force when the "
        f"`with` exits but translates 55P03 -> RowLockTimeout only INSIDE its body, so "
        f"a commit outside the bound is still bounded yet UNtranslated: a contended row "
        f"makes {route} answer 500 with a raw OperationalError instead of the "
        f"contracted 429 + Retry-After: 2. Move the commit back inside the `with`."
    )
    assert not outside, (
        f"{module_name}.{route} calls session.commit() at line(s) "
        f"{[c.lineno for c in outside]}, outside its `with bounded_lock_waits(...)` "
        f"block. Every commit in this route must sit inside the bound."
    )
```

- [ ] **Step 4: Record the transition in the file docstring**

Add to the module docstring, immediately after its existing first paragraph:

```
TRANSITIONAL, for the duration of the workspace_write consolidation
(docs/superpowers/specs/2026-08-14-workspace-write-consolidation-design.md §5).
Each route must be exactly one of two shapes — legacy `bounded_lock_waits` with
its commit inside, or migrated `workspace_write` with no route-level commit —
because the routes migrate one commit at a time and this gate has to stay green
throughout. The legacy arm is deleted in the slice's final commit, after which
re-introducing a hand-rolled bound-plus-commit in these two modules fails.
```

- [ ] **Step 5: Run the gate against six still-legacy routes**

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: 8 passed, `PYTEST_EXIT=0`. It must land green — no route has migrated yet.

- [ ] **Step 6: Prove the legacy arm still discriminates**

The gate is guarding six legacy routes right now, so the mutation that matters today is the legacy one. For **each** of the six routes, dedent that route's `session.commit()` out of its `with bounded_lock_waits(...)` block (md5 before/after; confirm changed), run:

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py -q ; echo "PYTEST_EXIT=$?"
```

Expected each time: `1 failed, 7 passed`, the failure being that route's own parametrized case and no other. Restore the file, confirm the md5 matches the original, before moving to the next route.

Then prove the new arms fail closed, in two probes. Both edit `settings.py` temporarily; run only this gate file while they are in place (they will trip `ruff` F821, which is expected and irrelevant to the check), and restore with `git checkout --` afterwards, confirming the md5 returns to its original.

Probe A — an *unlisted* migrated writer must be caught. Append a module-level `def _probe_unlisted(session, ws_id):` whose body is a `with workspace_write(session, ws_id, timeout_seconds=2, what="probe"):` block containing `ws_id` only. Expect **1 failure**: the `unlisted` presence arm naming `_probe_unlisted`.

Probe B — a migrated route with a stray commit must be caught. In `update_settings`, which is still legacy at this point, this cannot be shown; instead temporarily convert `add_member`'s bound to `workspace_write` while leaving its `session.commit()` in place. Expect **1 failure**: `test_each_route_is_exactly_one_of_legacy_or_migrated[call_manager.web.api.settings-add_member]` with the "still calls session.commit()" message. This also exercises the both-constructs assertion if you leave the legacy bound in — do that variant too and expect the "has BOTH" message.

- [ ] **Step 7: Commit**

```bash
git add tests/proposal_assist/test_bounded_region_encloses_commit.py
git commit -F <message-file>
```

Message subject: `test(gate): accept either shape while the six routes migrate one at a time`

---

### Task 3: Migrate `update_settings`

**Files:**
- Modify: `src/call_manager/web/api/settings.py:86-150`

**Interfaces:**
- Consumes: `workspace_write` (Task 1); the transitional gate (Task 2).
- Produces: nothing new. `update_settings` keeps its signature, its response model and its 400 validation.

`update_settings` goes first because it is the simplest body: four assignments, no early returns, no re-read.

- [ ] **Step 1: Add the import**

In the import block, after the `row_locks` import group, add:

```python
from call_manager.proposal_assist.web._writes import workspace_write
```

Leave every existing import in place — `add_member` and `remove_member` still use them.

- [ ] **Step 2: Replace the ceremony**

Replace everything from `# Slice C2 lock-order rule: the live mutex is taken immediately after` (line 101) through the `except RowLockTimeout` block that ends at line 145, with:

```python
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner and its lock-free validation, and before any other lock.
        # workspace_write takes the live commit mutex, bounds every lock wait in
        # the rest of the transaction, and commits INSIDE that bound —
        # soft_delete_workspace_db holds the same mutex for its whole
        # transaction, so a deletion committing while we blocked is observed the
        # instant we win it and WorkspaceDeleted propagates to main.py's 404
        # handler with nothing mutated. Both timeout types become the contracted
        # 429 + Retry-After: 2.
        with workspace_write(
            session,
            ws.id,
            timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            what="settings-update",
        ):
            ws.title = title
            ws.status = body.status
            ws.deadline = deadline
            ws.citation_style = body.citation_style
```

Leave the comment at lines 146–149 and `return _settings_out(ws)` exactly as they are.

- [ ] **Step 3: Run the gates**

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py \
  tests/proposal_assist/test_api_writers_mutex_straddle.py \
  tests/test_api_settings.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all pass, `PYTEST_EXIT=0`. In particular `test_update_settings_returns_429_when_the_commit_mutex_is_wedged` and `test_update_settings_gives_up_on_a_held_workspace_row_instead_of_pinning_the_mutex` must pass **unedited** — they are the behaviour-neutrality proof for this route.

- [ ] **Step 4: Prove the migrated arm discriminates for this route**

Delete the `with workspace_write(...)` line and dedent its body one level, so the route commits nothing and takes no scope (md5 before/after; confirm changed). Run the gate:

Expected: `test_each_route_is_exactly_one_of_legacy_or_migrated[call_manager.web.api.settings-update_settings]` FAILS with the "neither" message, plus the presence arm. Restore and confirm md5.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/settings.py
git commit -F <message-file>
```

Message subject: `refactor(api): update_settings runs its write inside workspace_write`

---

### Task 4: Migrate `add_member`, and pin its no-op paths

**Files:**
- Modify: `src/call_manager/web/api/settings.py:158-228`
- Modify: `tests/test_api_settings.py`

**Interfaces:**
- Consumes: `workspace_write` (Task 1).
- Produces: nothing new.

This route carries the slice's one deliberate behaviour change (spec §6): it returns early from inside the block on two no-op paths, and a commit-on-clean-exit scope commits an empty transaction there. Both paths write nothing today, so the observable delta is nil — but it is pinned rather than assumed.

- [ ] **Step 1: Write the failing pin test**

Add to `tests/test_api_settings.py`:

```python
def test_add_member_no_op_paths_add_no_rows(client_authed_owner, current_user, pg_session_factory):
    """spec §6. add_member returns early from inside its write scope on two
    no-op paths, so `workspace_write` commits an empty transaction there.

    Asserted as NO NEW ROWS, not "no rows": the already-a-member path is
    reachable only by pre-seeding exactly the WorkspaceViewer row it detects, so
    "leaves no viewer" is unsatisfiable. If someone later adds a write above
    those returns, these counts move and this test says so.
    """
    from call_manager.db.models import WorkspaceEvent, WorkspaceViewer
    from tests.proposal_assist.conftest import make_user, make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        member = make_user(s, email="already-a-member@example.test")
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=member.id))
        s.commit()
        ws_id, member_email = ws.id, member.email
        owner_email = current_user.email

    def snapshot():
        with pg_session_factory() as s:
            viewers = {
                v.user_id
                for v in s.query(WorkspaceViewer).filter_by(workspace_id=ws_id).all()
            }
            events = s.query(WorkspaceEvent).filter_by(workspace_id=ws_id).count()
        return viewers, events

    for label, email in (("owner", owner_email), ("already-a-member", member_email)):
        before = snapshot()
        resp = client_authed_owner.post(
            f"/api/workspaces/{ws_id}/members", json={"email": email}
        )
        assert resp.status_code == 200, f"{label}: expected 200, got {resp.status_code}"
        assert snapshot() == before, (
            f"{label} is a no-op path but it changed the database: "
            f"viewers/events went from {before} to {snapshot()}"
        )
```

- [ ] **Step 2: Run it against the un-migrated route**

```bash
uv run pytest tests/test_api_settings.py::test_add_member_no_op_paths_add_no_rows -q ; echo "PYTEST_EXIT=$?"
```

Expected: PASS. The pin is written before the migration precisely so it can be shown to hold on both sides — it is a regression guard, not a new requirement.

- [ ] **Step 3: Migrate the route**

Replace from `# Slice C2 lock-order rule: the live mutex is taken immediately after` (line 167) through the `except RowLockTimeout` block ending at line 223 with:

```python
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner, before any other lock. WorkspaceDeleted -> main.py -> 404.
        # The bound reaches COMMIT because the WorkspaceEvent insert takes
        # FOR KEY SHARE on its FK parents there — see update_settings' note.
        #
        # The two no-op returns below exit this scope cleanly, so the manager
        # COMMITS an empty transaction where the hand-written version did not
        # (spec §6, a deliberate and pinned behaviour change). Both paths write
        # nothing; test_add_member_no_op_paths_add_no_rows holds that.
        with workspace_write(
            session,
            ws.id,
            timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            what="add-member",
        ):
            # This route's only validation is a DB read on THIS session, so unlike
            # update_settings' body checks it cannot be hoisted above the mutex — a
            # lookup before the lock would read a workspace a committing delete is
            # about to remove. Consequence, deliberately accepted: with a wedged
            # holder an unknown email answers 429 after the timeout rather than 400
            # immediately (which also denies a straddling request a user-existence
            # oracle). Do not "align" this with update_settings by hoisting it.
            target = session.scalar(select(User).where(User.email == body.email))
            # Generic failure (no "no such user" oracle), mirroring the Jinja add_viewer.
            if target is None:
                raise HTTPException(status_code=400, detail="could not add collaborator")
            if target.id == ws.owner_id:
                response.status_code = 200  # owner already has full access — no-op
                return MemberOut(user_id=target.id, email=target.email, role="owner")
            existing = (
                session.query(WorkspaceViewer)
                .filter_by(workspace_id=ws.id, user_id=target.id)
                .first()
            )
            if existing is not None:
                response.status_code = 200  # idempotent
                return MemberOut(user_id=target.id, email=target.email, role="member")
            session.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
            session.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="member_added",
                    actor_id=user.id,
                    payload={"target_user_id": target.id, "target_email": target.email},
                )
            )
```

Leave lines 224–228 (the no-permission-signal comment and the final `return MemberOut(...)`) unchanged.

- [ ] **Step 4: Run the gates**

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py \
  tests/proposal_assist/test_api_writers_mutex_straddle.py \
  tests/test_api_settings.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all pass. The pin from Step 1 must still pass — now against the committing version.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/web/api/settings.py tests/test_api_settings.py
git commit -F <message-file>
```

Message subject: `refactor(api): add_member runs its write inside workspace_write`

The message body must state the §6 behaviour change explicitly: the two no-op paths now commit an empty transaction, both write nothing, and `test_add_member_no_op_paths_add_no_rows` holds that.

---

### Task 5: Migrate `remove_member` and drop `settings.py`'s dead lock imports

**Files:**
- Modify: `src/call_manager/web/api/settings.py:238-287` and its import block

**Interfaces:**
- Consumes: `workspace_write` (Task 1).
- Produces: `settings.py` no longer imports `CommitMutexTimeout`, `acquire_commit_mutex_live`, `BUSY_ERROR_MESSAGE`, `RowLockTimeout` or `bounded_lock_waits`.

This is the third and last route in `settings.py`, so the shared lock imports become unused in this same commit. `ruff` will flag them (F401) — dropping them here is what keeps this commit clean.

- [ ] **Step 1: Migrate the route**

Replace from `# Slice C2 lock-order rule: the live mutex immediately after` (line 240) through the `except RowLockTimeout` block ending at line 282 with:

```python
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner, before any other lock. WorkspaceDeleted -> main.py -> 404.
        # The bound reaches COMMIT because both the DELETE and the WorkspaceEvent
        # insert flush there — see update_settings' note.
        with workspace_write(
            session,
            ws.id,
            timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            what="remove-member",
        ):
            target = session.get(User, user_id)  # resolve email for the audit payload
            deleted = (
                session.query(WorkspaceViewer)
                .filter_by(workspace_id=ws.id, user_id=user_id)
                .delete()
            )
            if deleted:
                session.add(
                    WorkspaceEvent(
                        workspace_id=ws.id,
                        kind="member_removed",
                        actor_id=user.id,
                        payload={
                            "target_user_id": user_id,
                            "target_email": target.email if target else None,
                        },
                    )
                )
```

Leave lines 283–287 (the `signal_from_request` comment, the call, and `return Response(status_code=204)`) exactly as they are. That call is dedented past the `with session_factory()` block on purpose and must stay there.

- [ ] **Step 2: Drop the now-dead imports**

Replace the two import groups at lines 24–33 with:

```python
from call_manager.proposal_assist.row_locks import WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS
```

`WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS` is still used by all three routes; everything else in those two groups is now unreferenced in this module.

- [ ] **Step 3: Run the gates**

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py \
  tests/proposal_assist/test_api_writers_mutex_straddle.py \
  tests/test_api_settings.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all pass, `RUFF_EXIT=0`. A ruff F401 here means an import was dropped that is still used, or one was left that is not.

- [ ] **Step 3b: Run the full suite**

`settings.py` is now fully migrated, so this is a module-completion checkpoint (Global Constraints).

```bash
uv run pytest tests/ -q ; echo "PYTEST_EXIT=$?"
```

Expected: **3388 passed, 1 skipped**, `PYTEST_EXIT=0` — the same count as Task 1, since Tasks 3–5 add no tests beyond Task 4's pin. If it is 3389, Task 4's pin is counted here; reconcile the arithmetic in your report rather than assuming.

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/web/api/settings.py
git commit -F <message-file>
```

Message subject: `refactor(api): remove_member runs its write inside workspace_write`

---

### Task 6: Migrate `update_rubric`

**Files:**
- Modify: `src/call_manager/web/api/rubric.py:162-249` and its import block

**Interfaces:**
- Consumes: `workspace_write` (Task 1).
- Produces: nothing new. `_apply_rubric`'s signature is unchanged.

The re-read stays inside the body. Do not move it, and do not delete its comment — it records why `session.refresh` and not `session.get(..., with_for_update=True)`, and two gates in the straddle suite fail if that is "simplified".

- [ ] **Step 1: Add the import**

After the `row_locks` import group, add:

```python
from call_manager.proposal_assist.web._writes import workspace_write
```

Leave the existing imports — `retry_setup` and `activate` still use them.

- [ ] **Step 2: Migrate the route**

Replace from `# Slice C2 lock-order rule: the live mutex immediately after` (line 170) through the `except RowLockTimeout` block ending at line 246 with:

```python
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner and its lock-free validation, before any other lock.
        # WorkspaceDeleted -> main.py -> 404. workspace_write bounds every lock
        # wait in the rest of the transaction and commits inside that bound:
        # _apply_rubric UPDATEs the `workspaces` row and the re-read below takes
        # it explicitly, so a wait can land at either end, and an untranslated
        # 55P03 would answer 500 where this route contracts for 429. The holder
        # is real — the four pass prefixes take that row FOR UPDATE holding NO
        # mutex, so the mutex we hold does not exclude them.
        # WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS, not SETUP_JOB_LOCK_TIMEOUT_SECONDS:
        # this route never touches a WorkspaceSetupJob row.
        with workspace_write(
            session,
            ws.id,
            timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            what="rubric-update",
        ):
            # Re-read the workspace row UNDER the mutex, BEFORE _apply_rubric
            # reads it. require_owner loaded `ws` before the mutex, and this
            # request may then have waited up to 10s there while another
            # writer changed the rubric and committed. Nothing refreshed the
            # instance in the meantime: acquire_commit_mutex_live's freshness
            # guard is a scalar SELECT of soft_deleted_at ONLY (it repopulates
            # no attribute), and db/session.py builds the sessionmaker with
            # expire_on_commit=False.
            #
            # It matters here because _apply_rubric derives content_changed
            # and the rubric_edited event's old_criteria/old_funder_notes from
            # these attributes. Stale, an identical concurrent write records a
            # SECOND event for one edit, and a distinct one records old_* from
            # a rubric two versions back, breaking the audit chain.
            #
            # session.refresh, NOT session.get(..., with_for_update=True): with
            # `get`, a non-None with_for_update skips the identity lookup and
            # emits the SELECT ... FOR UPDATE, so the row lock is taken — but
            # the loader does not overwrite attributes that are ALREADY
            # populated, so the stale values survive the round trip (verified
            # against SQLAlchemy 2.0.49; `get` needs populate_existing=True to
            # refresh). Do not "simplify" this into that form; the two
            # identical-concurrent-write gates in
            # tests/proposal_assist/test_api_writers_mutex_straddle.py fail if
            # you do.
            #
            # Lock order is unchanged (M -> W): _apply_rubric's UPDATE already
            # took this row at flush; with_for_update only takes it earlier and
            # explicitly. The wait sits inside the managed bound, so its 55P03
            # is translated to this route's contracted 429 like every other wait
            # in this transaction.
            session.refresh(ws, with_for_update=True)
            _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
```

Leave lines 247–249 (the expire_on_commit comment and `return _rubric_out(ws)`) unchanged.

- [ ] **Step 3: Run the gates**

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py \
  tests/proposal_assist/test_api_writers_mutex_straddle.py \
  tests/test_api_rubric.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all pass. `test_update_rubric_maps_a_commit_time_users_fk_wait_to_429_not_500` is the one that proves the bound still reaches COMMIT for this route, and its body must not be touched to make it pass.

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/web/api/rubric.py
git commit -F <message-file>
```

Message subject: `refactor(api): update_rubric runs its write inside workspace_write`

---

### Task 7: Migrate `retry_setup`

**Files:**
- Modify: `src/call_manager/web/api/rubric.py:271-344`

**Interfaces:**
- Consumes: `workspace_write` (Task 1).
- Produces: nothing new.

This route raises `HTTPException` 404 and 409 from inside the scope. The manager must let both through without committing — that is what Task 1's design guarantees, and Step 3 checks it.

- [ ] **Step 1: Migrate the route**

Replace from `# Slice C2 lock-order rule: the live mutex immediately after require_owner` (line 273) through the `except RowLockTimeout` block ending at line 343 with:

```python
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner and BEFORE the setup-job row lock below. Order matters
        # here for a concrete reason: a route that took the job row first would
        # hold it for the whole mutex wait, and the delete's retirement hook
        # cancels setup jobs with FOR UPDATE SKIP LOCKED — so it would skip this
        # row and leave the job non-terminal until the reconciliation tick.
        # Opening the scope first means we raise WorkspaceDeleted (-> main.py ->
        # 404) without ever touching the row.
        #
        # This route also has its own resurrection shape, though a narrower one
        # than it looks: it cannot revive a job the delete cancelled, because it
        # refuses anything whose status is not exactly 'failed' (below) and the
        # delete writes 'cancelled'. What it CAN do is re-queue a job that was
        # already 'failed' when the workspace was deleted — 'failed' is terminal
        # and therefore not in the hook's _CANCELLABLE list, so the delete leaves
        # it alone. The under-mutex read is what stops that.
        #
        # The 404 and 409 below are raised INSIDE the scope. workspace_write lets
        # an HTTPException through untouched and commits nothing, so neither
        # early exit writes.
        with workspace_write(
            session,
            ws.id,
            timeout_seconds=SETUP_JOB_LOCK_TIMEOUT_SECONDS,
            what="setup-retry",
        ):
            job = session.scalar(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws.id)
                .with_for_update()
            )
            if job is None or job.status != "failed":
                if job is not None and job.status in ("queued", "running"):
                    raise HTTPException(
                        status_code=409,
                        detail="setup job is already queued or running",
                    )
                raise HTTPException(
                    status_code=404,
                    detail="no failed setup job for this workspace",
                )
            job.status = "queued"
            job.attempts = 0
            job.started_at = None
            job.completed_at = None
            job.last_error = None
```

Leave line 344 (`return SetupRetryOut(status=job.status, attempts=job.attempts)`) unchanged.

- [ ] **Step 2: Run the gates**

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py \
  tests/proposal_assist/test_api_writers_mutex_straddle.py \
  tests/proposal_assist/test_setup_retry.py \
  tests/test_api_rubric.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all pass.

- [ ] **Step 3: Confirm the two early-exit paths still behave**

Both raise `HTTPException` from inside a commit-on-clean-exit scope, so the manager must let them through and roll back rather than commit. These existing tests are the proof and must pass unmodified:

```bash
uv run pytest tests/proposal_assist/test_setup_retry.py -q \
  -k "no_job_at_all_returns_404 or queued_job_returns_409 or resets_failed_job" ; echo "PYTEST_EXIT=$?"
```

Expected: 3 passed, `PYTEST_EXIT=0`. `test_setup_retry_queued_job_returns_409` proves the `HTTPException` **passthrough** — it does not prove the rollback, because that path has nothing pending to roll back. The rollback itself is proved directly in Task 1 by `test_a_body_HTTPException_propagates_unchanged_and_commits_nothing`, which stages a write before raising. `test_setup_retry_resets_failed_job` is the one that would break if the manager failed to commit the success path.

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/web/api/rubric.py
git commit -F <message-file>
```

Message subject: `refactor(api): retry_setup runs its write inside workspace_write`

---

### Task 8: Migrate `activate` and drop `rubric.py`'s dead lock imports

**Files:**
- Modify: `src/call_manager/web/api/rubric.py:354-437` and its import block

**Interfaces:**
- Consumes: `workspace_write` (Task 1).
- Produces: `rubric.py` no longer imports `CommitMutexTimeout`, `acquire_commit_mutex_live`, `BUSY_ERROR_MESSAGE`, `RowLockTimeout` or `bounded_lock_waits`. All six routes are now migrated.

`activate` goes last because it carries both constraints at once: the M → J → W order, and a re-read whose position is load-bearing. **The re-read must stay AFTER the job SELECT.** Hoisting it inverts against `phases/setup.py`'s `_m3`, which writes the rubric holding job → workspace, and opens a real Postgres 40P01.

- [ ] **Step 1: Migrate the route**

Replace from `# Slice C2 lock-order rule: the live mutex immediately after require_owner` (line 360) through the `except RowLockTimeout` block ending at line 432 with:

```python
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner and BEFORE the setup-job row lock below. Taking the job
        # row first would hold it across the whole mutex wait, and the delete's
        # retirement hook cancels setup jobs with FOR UPDATE SKIP LOCKED — it
        # would skip this row and leave the job non-terminal until the
        # reconciliation tick.
        #
        # workspace_write bounds the WHOLE remainder including COMMIT. It matters
        # more here than on retry_setup: the re-read below takes the `workspaces`
        # row FOR UPDATE and _apply_rubric UPDATEs it, while the four pass
        # prefixes hold that row FOR UPDATE for up to
        # PASS_PREFIX_LOCK_TIMEOUT_SECONDS holding no mutex, so this transaction
        # really can block after the job lock.
        with workspace_write(
            session,
            ws.id,
            timeout_seconds=SETUP_JOB_LOCK_TIMEOUT_SECONDS,
            what="activate",
        ):
            job = session.scalar(
                select(WorkspaceSetupJob)
                .where(WorkspaceSetupJob.workspace_id == ws.id)
                .with_for_update()
            )
            if job is None or job.status != "waiting_for_rubric_review":
                raise HTTPException(
                    status_code=409, detail="workspace is not awaiting rubric review"
                )
            # Re-read the workspace row under the mutex before _apply_rubric
            # reads it — same defect, same fix, full rationale on
            # update_rubric above (including why session.refresh and not
            # session.get(..., with_for_update=True)).
            #
            # It is worse on this route: phases/setup.py's _m3 WRITES the
            # rubric while holding job -> workspace, so the rubric activate
            # loaded at require_owner can have been replaced by the setup
            # worker while activate queued on the mutex — the very transition
            # this route is confirming.
            #
            # AFTER the job lock, deliberately. The normative order is
            # M -> J -> W and it matches _m3's; hoisting this above the job
            # SELECT would invert against _m3 and open a real deadlock shape
            # (Postgres 40P01). Nothing is lost by taking it here: the 409 path
            # above returns without ever needing the row.
            session.refresh(ws, with_for_update=True)
            _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
            job.status = "done"
            job.completed_at = datetime.now(UTC)
            ws.current_phase = "active"
            session.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="workspace_activated",
                    actor_id=user.id,
                    payload={},
                )
            )
        result = _rubric_out(ws)  # build INSIDE the session block (still open)
```

Leave lines 434–437 (the signal comment, `signal_from_request(...)` and `return result`) unchanged — the signal call is dedented past the `with session_factory()` block on purpose.

- [ ] **Step 2: Drop the now-dead imports**

In the import block (lines 22–29), remove `BUSY_ERROR_MESSAGE`, `CommitMutexTimeout`, `acquire_commit_mutex_live`, `RowLockTimeout` and `bounded_lock_waits`, keeping:

```python
from call_manager.proposal_assist.row_locks import (
    SETUP_JOB_LOCK_TIMEOUT_SECONDS,
    WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
)
```

Both constants are still used. If the `drafting.locking` import group becomes empty, delete the whole group.

- [ ] **Step 3: Run the gates**

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py \
  tests/proposal_assist/test_api_writers_mutex_straddle.py \
  tests/test_api_rubric.py tests/test_api_settings.py -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
```

Expected: all pass, `RUFF_EXIT=0`. `test_activate_maps_a_post_job_lock_row_timeout_to_429_not_500` and `test_activate_maps_a_commit_time_users_fk_wait_to_429_not_500` are the lock-order and bound-reach proofs for this route.

- [ ] **Step 3b: Run the full suite**

All six routes are now migrated — the second module-completion checkpoint (Global Constraints).

```bash
uv run pytest tests/ -q ; echo "PYTEST_EXIT=$?"
```

Expected: **3389 passed, 1 skipped**, `PYTEST_EXIT=0` (3381 baseline + 7 manager tests + Task 4's pin), with nothing removed.

- [ ] **Step 4: Commit**

```bash
git add src/call_manager/web/api/rubric.py
git commit -F <message-file>
```

Message subject: `refactor(api): activate runs its write inside workspace_write`

---

### Task 9: Tighten the gate, and run the full acceptance set

**Files:**
- Modify: `tests/proposal_assist/test_bounded_region_encloses_commit.py`

**Interfaces:**
- Consumes: all six migrated routes (Tasks 3–8).
- Produces: the final gate. Re-introducing a hand-rolled `bounded_lock_waits` + commit in these two modules now fails.

- [ ] **Step 1: Delete the legacy arm**

Replace `test_each_route_is_exactly_one_of_legacy_or_migrated` with:

```python
@pytest.mark.parametrize(
    ("module_name", "route"),
    sorted(
        (module_name, route)
        for module_name, routes in EXPECTED_BOUNDED_WRITERS.items()
        for route in routes
    ),
)
def test_each_route_delegates_its_whole_write_to_workspace_write(module_name: str, route: str):
    """Each route runs its write inside exactly one `with workspace_write(...)`
    scope and calls `session.commit()` nowhere.

    This is the stronger successor to the bound-through-COMMIT rule. That rule
    said the commit must sit inside the bound; this one removes the commit from
    the route entirely, so the violation is unwriteable rather than merely
    tested. Rule 2 — no route-level commit — is what keeps it that way: it fails
    if someone re-introduces a hand-rolled transaction beside the managed one.

    `retry_setup` deserves a specific note. Its scope opens with the setup-job
    row's `.with_for_update()` and it writes no WorkspaceEvent, so its COMMIT has
    no FK check and no reachable contention: no runtime interleaving can tell an
    enclosed commit from an escaped one for that route. This static check is its
    only enforcement.
    """
    fn = _module_level_functions(_parse(module_name)).get(route)
    assert fn is not None, (
        f"{module_name}.{route} is not a module-level function any more — this rule "
        f"cannot be checked for it. See test_the_six_bounded_writers_are_all_present."
    )

    managed = _managed_blocks(fn)
    legacy = _bound_blocks(fn)
    commits = _session_commit_calls(fn)

    assert len(managed) == 1, (
        f"{module_name}.{route} has {len(managed)} `with workspace_write(...)` blocks; "
        f"every mutex-held write in these two modules runs inside exactly one. Without "
        f"it nothing bounds the route's lock waits through COMMIT, and a contended row "
        f"answers 500 with a raw OperationalError instead of the contracted "
        f"429 + Retry-After: 2."
    )
    assert not legacy, (
        f"{module_name}.{route} re-introduces a hand-rolled "
        f"`with bounded_lock_waits(...)` block at line {legacy[0].lineno}. That is the "
        f"ceremony workspace_write replaced, and it was written wrongly at four of six "
        f"sites before review caught it. Use workspace_write."
    )
    assert not commits, (
        f"{module_name}.{route} calls session.commit() at line(s) "
        f"{[c.lineno for c in commits]}. workspace_write owns the commit — a "
        f"route-level commit either double-commits or commits outside the managed scope."
    )
```

- [ ] **Step 1b: Add the module-wide no-commit arm**

The per-route check above only ever looks at the six names in `EXPECTED_BOUNDED_WRITERS`, and `_discovered_bounded_writers` only discovers functions that already contain one of the two constructs. So a **new** route carrying a raw `session.commit()` and neither construct is invisible to both — while §5's stated invariant is that *no* route in these modules commits. Close it with an arm that walks the module rather than the table.

Add these two helpers beside the others:

```python
def _is_router_decorated(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if any decorator is a `router.<method>(...)` call — i.e. this
    function is an HTTP route, whether or not it is named in the table."""
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "router"
        ):
            return True
    return False


def _router_functions(module_name: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        name: fn
        for name, fn in _module_level_functions(_parse(module_name)).items()
        if _is_router_decorated(fn)
    }
```

And this test:

```python
@pytest.mark.parametrize("module_name", sorted(EXPECTED_BOUNDED_WRITERS))
def test_no_route_in_these_modules_calls_session_commit(module_name: str):
    """Module-wide, NOT table-driven. The per-route rule above can only see
    routes already listed; this arm sees every `router.<method>`-decorated
    function in the module, so a NEW route that commits by hand fails here even
    though it is in no table and takes neither context manager.

    Read routes commit nothing, so the rule is simply: none of them.
    """
    offenders = {
        name: [c.lineno for c in _session_commit_calls(fn)]
        for name, fn in _router_functions(module_name).items()
        if _session_commit_calls(fn)
    }
    assert not offenders, (
        f"{module_name}: {offenders} call session.commit() directly. Every "
        f"mutex-held write in these two modules runs inside `workspace_write`, "
        f"which owns the commit; a hand-rolled commit is either unbounded or "
        f"outside the managed scope, and answers 500 on a contended row where "
        f"these routes contract for 429 + Retry-After: 2."
    )
```

Prove it with its own mutation: append to `settings.py` a module-level function decorated `@router.get("/workspaces/{workspace_id}/_probe")` whose body calls `session.commit()`, listed in no table and using neither construct. Expected: **1 failure**, `test_no_route_in_these_modules_calls_session_commit[call_manager.web.api.settings]`, naming `_probe`. Confirm the six per-route cases and both presence cases still pass — that is the blind spot being demonstrated. Restore and confirm md5.

- [ ] **Step 2: Update the docstring**

Delete the TRANSITIONAL paragraph added in Task 2 and replace it with a sentence recording that all six routes are migrated and the legacy arm is gone.

Also update the `EXPECTED_BOUNDED_WRITERS` comment: it currently talks about `bounded_lock_waits` blocks. It must now describe `workspace_write` scopes, and must KEEP its two earned statements — that the table is deliberately not derived from the source, and that the walk's scope is these two modules only (a `workspace_write` scope added in `web/api/workspaces.py`, say, is invisible to it).

- [ ] **Step 3: Run the gate**

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py -q ; echo "PYTEST_EXIT=$?"
```

Expected: **10 passed** (8 as before, plus the two module-wide no-commit cases from Step 1b), `PYTEST_EXIT=0`.

- [ ] **Step 4: Prove the final gate with six mutations**

For **each** of the six routes, delete that route's `with workspace_write(...)` call line and dedent its body one level (md5 before/after; confirm changed). Run:

```bash
uv run pytest tests/proposal_assist/test_bounded_region_encloses_commit.py -q ; echo "PYTEST_EXIT=$?"
```

Expected each time: failures naming **that route** — its own `test_each_route_delegates_its_whole_write_to_workspace_write` case, plus its module's presence arm. No other route's case may fail. Restore and confirm the md5 matches the original before the next.

Record all six in your report with the observed failure text. A mutation whose md5 did not change proves nothing and must be redone.

- [ ] **Step 5: Run the full acceptance set**

```bash
uv run pytest tests/ -q ; echo "PYTEST_EXIT=$?"
uv run ruff check . ; echo "RUFF_EXIT=$?"
EMBEDDING_MODEL=bge-m3 ./scripts/gen_openapi_types.sh
git diff --exit-code -- frontend/src/api/schema.gen.ts ; echo "DRIFT_EXIT=$?"
git status --short
```

Expected: **3389 passed, 1 skipped** (3381 baseline + 7 manager tests + Task 4's pin) with nothing removed and `PYTEST_EXIT=0`; `RUFF_EXIT=0`; `DRIFT_EXIT=0`. `git status --short` shows exactly one modified file at this point — `tests/proposal_assist/test_bounded_region_encloses_commit.py`, this task's own uncommitted work. It must be empty only AFTER Step 7's commit; verify that there.

- [ ] **Step 6: Verify the untouched-file constraint**

```bash
git diff $(git merge-base master HEAD)..HEAD -- tests/proposal_assist/test_api_writers_mutex_straddle.py
```

Expected: the ONLY changes are the `writes_module` import and the body of `patch_route_mutex_timeout`, both from Task 1 Step 6. No test function, no assertion, no scenario may appear in that diff. If anything else does, behaviour moved — stop and escalate rather than reconciling it.

- [ ] **Step 7: Commit**

```bash
git add tests/proposal_assist/test_bounded_region_encloses_commit.py
git commit -F <message-file>
git status --short
```

Message subject: `test(gate): every route delegates its whole write to workspace_write`

Expected: `git status --short` is now empty.

---

## Success criteria (spec §10)

1. All six routes use `workspace_write`; no route in the two modules calls `session.commit()`.
2. All 28 tests in `tests/proposal_assist/test_api_writers_mutex_straddle.py` pass, and that file's whole-branch diff is confined to the `patch_route_mutex_timeout` helper and its import — verified by Task 9 Step 6.
3. The final gate fails under each of six per-route `workspace_write`-removal mutations (each failing its own case plus its module's presence arm, and no other route's case), its module-wide no-commit arm fails under an unlisted router-decorated committer, and its legacy arm is gone.
4. The manager rolls back on all four exceptional exits — mutex timeout, row timeout, body exception, `WorkspaceDeleted` — each asserting `not session.in_transaction()`, and two of them proving it against a reused, re-committed session.
5. Full suite 3389 passed, 1 skipped, with nothing removed; `ruff` clean; OpenAPI drift clean.
6. `update_settings`'s ceremony is gone: the route reads as validation, a `with`, four assignments, and a return.
