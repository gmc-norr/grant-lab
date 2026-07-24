# Workspace Retirement — Slice B-mutex (live-workspace commit mutex) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every mutex-participating artifact/turn writer re-reads the workspace's live state *under* the commit mutex and refuses to mutate a deleted workspace — and the delete transaction takes that same mutex, so the two get a real linearization point instead of a hope.

**Architecture:** One helper, `acquire_commit_mutex_live`, replaces `acquire_commit_mutex` at the mutating call sites: it acquires the mutex and then issues a **fresh** scalar read of `soft_deleted_at` (never `session.get`, whose identity map can hand back the pre-block instance), raising `WorkspaceDeleted` **before any mutation**. `soft_delete_workspace` acquires the same mutex before it mutates anything, so a writer blocked on the mutex when a deletion commits observes the deletion the instant it wins the lock. HTTP surfaces map `WorkspaceDeleted` to **404** through one app-level exception handler; agent tools return an error dict; background/read-model paths skip.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL advisory xact locks, pytest (real-PG fixtures; `pytest.mark.asyncio` for collab/kernel async paths). No new dependencies.

## Global Constraints

- **Backend-only. No migration. No frontend change. No OpenAPI/`schema.gen.ts` change.**
- This plan is **B-mutex**, the first half of spec Slice B. It is **standalone and independently mergeable** — it must not require the retirement coordinator (`WorkspaceRetirement`), the turns tombstone, collab room retirement, or the route's `signal_workspace_retirement`. Those are **B-retire**, the follow-up plan.
- **Deliberately deferred to B-retire** (do NOT do here): the `soft_delete_workspace_db` / `scrub_workspace_bytes` service split (B5), removal of the delete route's `signal_from_request`, and the 204-after-retirement ordering. The existing combined service and its existing 204 stay as they are.
- **Explicitly NOT claimed by this slice** (do not write comments, docstrings, or test names asserting otherwise): that **non-mutex** writers are covered — `settings.py:69` (`update_settings`) and `settings.py:97` (`add_member`) never take the mutex and remain an accepted residual (spec "Explicitly NOT claimed #3"); that any subprocess stopped; that an in-flight streaming `query` is prevented.
- Backend tests: `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider` — grouped dirs, **never** whole-suite; final sweep includes `tests/test_api_spec.py`.
- Collab tests in `tests/proposal_assist/collab/` use **anyio**; drafting/turns tests use `@pytest.mark.asyncio`. Follow the convention of the file you are appending to.
- Lint: `uv run ruff check .` (line-length 100). CI runs `ruff check` only, never `ruff format`.
- Explicit-path `git add` only (never `-A`). Merges are **local `--no-ff` only**, never pushed without an explicit ask. Do NOT invoke `/code-review` or `/ultrareview`.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- All git commands use `git -C /home/parlar_ai/dev/call-monitor`.

## Codebase facts (verified against HEAD `7b16547`)

Use these exact names/forms; do not guess.

| Fact | Reality |
|---|---|
| Mutex helper | `acquire_commit_mutex(session, workspace_id, *, timeout_seconds: int = 10) -> None` in `drafting/locking.py:144`; uses `SET LOCAL lock_timeout` + `pg_advisory_xact_lock`; released at COMMIT/ROLLBACK, **no explicit release**; re-entrant within one transaction |
| Busy signals | `CommitMutexTimeout(RuntimeError)` (`locking.py:133`) + `BUSY_ERROR_MESSAGE = "workspace is busy — try again in a moment"` (`:141`). Routes map to **429 + `Retry-After: 2`** (`workspaces.py:445`), NOT the 503 the stale docstring claims |
| `WorkspaceDeleted` | **Does not exist yet** — this plan creates it |
| Mutating mutex call sites | Exactly **17**, all line numbers still valid: `tools/citations.py:797/:911/:1038/:1213`, `collab/flip.py:117`, `analysis/insert.py:349`, `web/api/workspaces.py:536/:647/:735`, `collab/kernel.py:136/:221/:316/:427`, `citations/human_cite.py:102`, `turns/submit.py:242`, `editing/save.py:180`, `collab/checkpoint.py:82` |
| `turns/submit.py:242` | **Already** does mutex → `SELECT Workspace.id WHERE soft_deleted_at IS NULL ... FOR UPDATE` → raise `ValueError` (`submit.py:250-256`). Strictly stronger than the helper (it also row-locks). `web/api/turns.py` maps `ValueError → 400`. **Per-site decision: KEEP the plain acquire** — see Task 5 |
| `kernel.py:221` | Its own comment records that `checkpoint_artifact` re-acquires the same xact lock later and that this is an instant no-op (advisory xact locks are re-entrant) |
| Clean-error tuples | `_CLEAN_ACCEPT_ERRORS` (`proposals/kernel_accept.py:43`) and `_CLEAN_CITE_ERRORS` (`citations/collab_cite.py:34`) — errors raised **before** any doc mutation, so the room is NOT evicted. Both already contain `CommitMutexTimeout` |
| Tool error shape | `tools/citations.py` returns `{"error": _BUSY_ERROR}` on busy; `_BUSY_ERROR = BUSY_ERROR_MESSAGE` (`:93`) |
| App exception handlers | `main.py` registers `StarletteHTTPException` (`:445`), `AccountInactive` (`:463`), `RequestValidationError` (`:488`), `Exception` (`:498`). The `AccountInactive` handler is the pattern to copy: `api_request(request)` → `api_error(status, code, message)`, else a plain `JSONResponse` |
| `checkpoint_artifact` callers | 10 sites; the 4 that are NOT already inside a `_live`-guarded transaction are `collab/permissions.py:54` (already catches `(GitCommitError, CommitMutexTimeout)`), `collab/checkpoint.py:201` (`checkpoint_active_tails`, whose pre-mutex soft-delete check at `:198` this closes), `collab/handover.py:35`, `web/collab_checkpoint.py:52` |
| `soft_delete_workspace` | `workspaces.py:82`, `(session, *, workspace_id, workspaces_dir) -> None`; commits internally at `:133` (after Slice A's running-turn terminalization); FS scrub at `:134-148` |
| DELETE route | `web/api/workspaces.py:1488` `soft_delete_workspace_api`; `require_owner` → `soft_delete_workspace` → `signal_from_request(...)` → `Response(204)`. **No `CommitMutexTimeout` handler today** |
| Test factories | `make_workspace(session, *, owner=None, ...)`; `make_user(session, *, email="owner@ki.se")` — default is **FIXED**, so two defaulted users in one test collide; always pass distinct emails |

## File Structure

| File | Role |
|---|---|
| `src/call_manager/proposal_assist/drafting/locking.py` | Modify — `WorkspaceDeleted`; `acquire_commit_mutex_live` |
| `src/call_manager/main.py` | Modify — one `@app.exception_handler(WorkspaceDeleted)` → 404 |
| `src/call_manager/workspaces.py` | Modify — delete takes the same mutex, before any mutation |
| `src/call_manager/web/api/workspaces.py` | Modify — DELETE route `CommitMutexTimeout` → 429; 3 writer sites migrated |
| `src/call_manager/proposal_assist/collab/kernel.py` | Modify — 4 sites migrated |
| `src/call_manager/proposal_assist/proposals/kernel_accept.py` | Modify — `WorkspaceDeleted` into `_CLEAN_ACCEPT_ERRORS` |
| `src/call_manager/proposal_assist/citations/collab_cite.py` | Modify — `WorkspaceDeleted` into `_CLEAN_CITE_ERRORS` |
| `src/call_manager/proposal_assist/collab/checkpoint.py` | Modify — site migrated; `checkpoint_active_tails` skips |
| `src/call_manager/proposal_assist/collab/permissions.py` | Modify — on-evict checkpoint skips a deleted workspace |
| `src/call_manager/proposal_assist/collab/handover.py` | Modify — kill-switch drain skips |
| `src/call_manager/proposal_assist/web/collab_checkpoint.py` | Modify — route-side checkpoint skips |
| `src/call_manager/proposal_assist/collab/flip.py` | Modify — enable-on-touch refuses a deleted workspace |
| `src/call_manager/proposal_assist/editing/save.py` | Modify — site migrated |
| `src/call_manager/proposal_assist/citations/human_cite.py` | Modify — site migrated |
| `src/call_manager/proposal_assist/analysis/insert.py` | Modify — site migrated |
| `src/call_manager/proposal_assist/tools/citations.py` | Modify — 4 sites migrated, deleted → error dict |
| `tests/proposal_assist/test_commit_mutex_live.py` | **Create** |
| `tests/test_workspaces_service.py` | Extend — delete takes the mutex |
| `tests/proposal_assist/collab/test_collab_retire_mutex.py` | **Create** — two-writer straddle |
| `tests/proposal_assist/test_tools_citations_deleted.py` | **Create** |
| `tests/proposal_assist/test_workspaces_delete_route.py` | Extend or create — 429 on contention |

---

### Task 0: Cut the build branch

**Files:** none (git only).

- [ ] **Step 1: Verify master + clean tree**

```bash
git -C /home/parlar_ai/dev/call-monitor branch --show-current
git -C /home/parlar_ai/dev/call-monitor status --porcelain
```
Expected: `master`; only pre-existing untracked `docs/superpowers/...` files.

- [ ] **Step 2: Cut the branch**

```bash
git -C /home/parlar_ai/dev/call-monitor checkout -b workspace-retirement-mutex
```

---

### Task 1: `WorkspaceDeleted` + `acquire_commit_mutex_live` + the 404 handler

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/locking.py` (after `CommitMutexTimeout`, `:133-141`)
- Modify: `src/call_manager/main.py` (next to the `AccountInactive` handler, `:463`)
- Test: `tests/proposal_assist/test_commit_mutex_live.py` (create)

**Interfaces:**
- Produces: `WorkspaceDeleted(RuntimeError)` with `.workspace_id`; `acquire_commit_mutex_live(session, workspace_id, *, timeout_seconds: int = 10) -> None`. Raises `CommitMutexTimeout` exactly as `acquire_commit_mutex` does, or `WorkspaceDeleted` when the row is missing or `soft_deleted_at is not None`. Mutates nothing.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_commit_mutex_live.py`:

```python
"""acquire_commit_mutex_live: the live-workspace guard on the commit mutex.

The point of the helper is the FRESH read. session.get() can return the
identity-map instance loaded before we blocked on the mutex — which is exactly
the instance a writer holds while a deletion commits underneath it.
"""

import threading
from datetime import UTC, datetime

import pytest

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import (
    CommitMutexTimeout,
    WorkspaceDeleted,
    acquire_commit_mutex,
    acquire_commit_mutex_live,
)
from tests.proposal_assist.conftest import make_workspace


def test_live_workspace_acquires_normally(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        acquire_commit_mutex_live(s, ws_id)   # must not raise
        s.rollback()


def test_deleted_workspace_raises_before_any_mutation(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted) as e:
            acquire_commit_mutex_live(s, ws_id)
        assert e.value.workspace_id == ws_id
        s.rollback()


def test_missing_workspace_raises(pg_session_factory):
    import uuid

    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            acquire_commit_mutex_live(s, uuid.uuid4())
        s.rollback()


def test_reads_fresh_state_not_the_identity_map(pg_session_factory):
    """REGRESSION (spec review P1-3): the writer loaded the Workspace BEFORE
    blocking on the mutex. A session.get() would return that stale instance and
    the guard would pass on a workspace that is already deleted."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as writer:
        stale = writer.get(Workspace, ws_id)          # warms the identity map
        assert stale.soft_deleted_at is None

        with pg_session_factory() as deleter:          # committed by ANOTHER session
            deleter.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            deleter.commit()

        with pytest.raises(WorkspaceDeleted):
            acquire_commit_mutex_live(writer, ws_id)
        writer.rollback()


def test_writer_blocked_on_the_mutex_sees_a_deletion_that_commits_meanwhile(
    pg_session_factory,
):
    """The linearization point. The writer blocks on the mutex; the deleter holds
    it, marks the workspace deleted, and commits. The writer must wake up and
    refuse."""
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    holder_has_mutex = threading.Event()
    writer_is_blocking = threading.Event()
    result: dict = {}

    def _deleter():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holder_has_mutex.set()
            writer_is_blocking.wait(timeout=5)    # let the writer queue behind us
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()                            # releases the mutex

    def _writer():
        holder_has_mutex.wait(timeout=5)
        with pg_session_factory() as s:
            writer_is_blocking.set()
            try:
                acquire_commit_mutex_live(s, ws_id)
                result["outcome"] = "acquired"
            except WorkspaceDeleted:
                result["outcome"] = "refused"
            except CommitMutexTimeout:
                result["outcome"] = "timeout"
            s.rollback()

    threads = [threading.Thread(target=_deleter), threading.Thread(target=_writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert result["outcome"] == "refused"
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_commit_mutex_live.py -p no:cacheprovider -q
```
Expected: FAIL — `cannot import name 'WorkspaceDeleted'`.

- [ ] **Step 3a: Add the exception + helper**

In `locking.py`, immediately after `BUSY_ERROR_MESSAGE`:

```python
class WorkspaceDeleted(RuntimeError):
    """The workspace was soft-deleted, observed UNDER the commit mutex.

    Raised by :func:`acquire_commit_mutex_live` before the caller mutates
    anything, so the caller's transaction can be rolled back having written no
    git commit and no DB row. HTTP surfaces map this to 404 (the workspace is
    already 404 to every reader); agent tools return an error; background and
    read-model paths skip."""

    def __init__(self, workspace_id: uuid.UUID) -> None:
        super().__init__(f"workspace {workspace_id} was deleted")
        self.workspace_id = workspace_id


# Shared 404 detail string for the deleted-workspace refusal, so the message the
# agent sees and the message the browser sees stay byte-identical.
DELETED_ERROR_MESSAGE = "workspace no longer exists"
```

and after `acquire_commit_mutex`:

```python
def acquire_commit_mutex_live(
    session: Session, workspace_id: uuid.UUID, *, timeout_seconds: int = 10
) -> None:
    """Acquire the commit mutex, then refuse a workspace that is already deleted.

    The linearization point for mutex-participating writers: soft_delete takes
    this SAME mutex before it mutates anything, so a writer that was blocked on
    the mutex while a deletion committed observes the deletion the instant it
    wins the lock — and raises BEFORE performing any git or DB mutation.

    The re-read MUST be fresh. ``session.get(Workspace, id)`` can return the
    identity-map instance the caller loaded before blocking on the mutex, which
    is precisely the stale state this guard exists to defeat; a scalar SELECT of
    the column always round-trips. A missing row is treated as deleted (hard
    deletes are not a thing here, but a missing workspace is never writable).

    Raises CommitMutexTimeout exactly as acquire_commit_mutex does, or
    WorkspaceDeleted. Mutates nothing in either case; the caller rolls back.
    """
    acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)
    row = session.execute(
        select(Workspace.soft_deleted_at).where(Workspace.id == workspace_id)
    ).first()
    if row is None or row[0] is not None:
        raise WorkspaceDeleted(workspace_id)
```

Add the imports `locking.py` needs: `from sqlalchemy import select, text` (it already imports `text`) and `from call_manager.db.models import Workspace`. **If that import is circular**, import `Workspace` lazily inside the function body and say so in a one-line comment — do not restructure `db.models`.

- [ ] **Step 3b: Map it to 404 once, at the app level**

In `main.py`, directly after the `AccountInactive` handler block:

```python
    from call_manager.proposal_assist.drafting.locking import (
        DELETED_ERROR_MESSAGE,
        WorkspaceDeleted,
    )

    @app.exception_handler(WorkspaceDeleted)
    async def _workspace_deleted(request: Request, exc: WorkspaceDeleted):
        # A writer that won the commit mutex only to find the workspace deleted.
        # It mutated nothing. 404 matches what every reader already sees — the
        # workspace is gone — and keeps the SPA on its existing not-found path.
        # Registered explicitly so it resolves ahead of the catch-all Exception
        # handler below.
        if api_request(request):
            return api_error(404, "workspace_deleted", DELETED_ERROR_MESSAGE)
        return JSONResponse(status_code=404, content={"detail": DELETED_ERROR_MESSAGE})
```

Match the surrounding code's existing `api_request` / `api_error` / `JSONResponse` imports — do not add new ones if they are already in scope.

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_commit_mutex_live.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/drafting/locking.py \
       src/call_manager/main.py tests/proposal_assist/test_commit_mutex_live.py
```
Expected: 5 passed; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/drafting/locking.py src/call_manager/main.py \
  tests/proposal_assist/test_commit_mutex_live.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(locking): acquire_commit_mutex_live — refuse a deleted workspace under the mutex

Acquires the commit mutex then re-reads soft_deleted_at with a FRESH scalar
SELECT (session.get can hand back the identity-map instance loaded before we
blocked — the exact stale state this guard defeats) and raises WorkspaceDeleted
before any mutation. One app-level handler maps it to 404.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: the delete transaction takes the same mutex

**Files:**
- Modify: `src/call_manager/workspaces.py:98-133` (`soft_delete_workspace`)
- Modify: `src/call_manager/web/api/workspaces.py:1488-1508` (DELETE route)
- Test: `tests/test_workspaces_service.py`, `tests/proposal_assist/test_workspaces_delete_route.py`

**Interfaces:**
- Consumes: `acquire_commit_mutex` (Task 1 leaves it unchanged), `CommitMutexTimeout`.
- Produces: `soft_delete_workspace` holds the commit mutex across its whole transaction; the DELETE route maps `CommitMutexTimeout` to **429** having committed nothing.

**Why the mutex goes FIRST:** it must be held *before* `ws.soft_deleted_at` is set, or a writer could win the mutex, pass the live-check, and then have the deletion commit underneath it — the very ordering the helper exists to prevent.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workspaces_service.py`:

```python
def test_soft_delete_holds_the_commit_mutex_so_a_blocked_writer_refuses(
    pg_session_factory, tmp_path
):
    """The other half of the linearization point: a writer already blocked on the
    mutex when the deletion commits must observe the deletion, not proceed."""
    import threading

    from call_manager.proposal_assist.drafting.locking import (
        WorkspaceDeleted,
        acquire_commit_mutex_live,
    )
    from call_manager.workspaces import soft_delete_workspace
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    writer_started = threading.Event()
    outcome: dict = {}

    def _writer():
        with pg_session_factory() as s:
            writer_started.set()
            try:
                acquire_commit_mutex_live(s, ws_id)
                outcome["r"] = "acquired"
            except WorkspaceDeleted:
                outcome["r"] = "refused"
            s.rollback()

    t = threading.Thread(target=_writer)
    t.start()
    writer_started.wait(timeout=5)
    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)
    t.join(timeout=30)

    # Either the writer got in first (before the delete took the mutex) or it was
    # refused — it must NEVER acquire a mutex on an already-deleted workspace.
    if outcome["r"] == "acquired":
        # Won the race legitimately; re-running now must refuse.
        with pg_session_factory() as s:
            with pytest.raises(WorkspaceDeleted):
                acquire_commit_mutex_live(s, ws_id)
            s.rollback()
    else:
        assert outcome["r"] == "refused"


def test_soft_delete_propagates_commit_mutex_timeout_without_committing(
    pg_session_factory, tmp_path
):
    """A wedged writer holding the mutex must make the delete FAIL cleanly, not
    half-apply: nothing committed, the workspace still live."""
    import threading

    from call_manager.db.models import Workspace
    from call_manager.proposal_assist.drafting.locking import (
        CommitMutexTimeout,
        acquire_commit_mutex,
    )
    from call_manager.workspaces import soft_delete_workspace
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
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
        with pg_session_factory() as s:
            with pytest.raises(CommitMutexTimeout):
                soft_delete_workspace(
                    s, workspace_id=ws_id, workspaces_dir=tmp_path, timeout_seconds=1
                )
            s.rollback()
    finally:
        release.set()
        t.join(timeout=30)

    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None   # nothing committed
```

Create `tests/proposal_assist/test_workspaces_delete_route.py`:

```python
"""DELETE /api/workspaces/{id} under commit-mutex contention."""

import threading

import pytest

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex
from tests.proposal_assist.conftest import make_workspace


def test_delete_returns_429_when_the_commit_mutex_is_held(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """Deletion now takes the commit mutex, so a wedged writer makes it busy —
    the house 429, having committed NOTHING (not a 500, not a partial delete)."""
    monkeypatch.setenv("_", "_")  # placeholder; see note below
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

    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is None


def test_delete_still_returns_204_uncontended(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    assert client_authed_owner.delete(f"/api/workspaces/{ws_id}").status_code == 204
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None
```

**Note for the implementer:** the route's default mutex timeout is 10s, so the contended test blocks for ~10s. If that is too slow for the suite, thread a `timeout_seconds` through from the route's settings and pin it low in the test — but do NOT silently drop the assertion. Remove the placeholder `monkeypatch.setenv` line; it exists only to flag that this choice is yours to make explicit.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_workspaces_service.py tests/proposal_assist/test_workspaces_delete_route.py \
  -p no:cacheprovider -q -k "mutex or 429 or holds_the"
```
Expected: FAIL — `soft_delete_workspace` takes no `timeout_seconds`; the route returns 204 (or 500) under contention.

- [ ] **Step 3a: Take the mutex in the delete transaction**

In `workspaces.py`, change the signature and add the acquire as the FIRST mutating-path statement:

```python
def soft_delete_workspace(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    workspaces_dir: Path,
    timeout_seconds: int = 10,
) -> None:
```

and immediately after the `ws is None` guard, **before** `ws.soft_deleted_at = ...`:

```python
    # Take the per-workspace commit mutex for this whole transaction, BEFORE any
    # mutation. Mutex-participating writers call acquire_commit_mutex_live, which
    # re-reads soft_deleted_at once it wins this lock — so holding it here is what
    # turns their check into a real linearization point rather than a hope. A
    # wedged writer makes the delete raise CommitMutexTimeout having committed
    # nothing; the route maps that to the house 429 and the client retries.
    acquire_commit_mutex(session, workspace_id, timeout_seconds=timeout_seconds)
```

Import `acquire_commit_mutex` from `call_manager.proposal_assist.drafting.locking`, and extend the function's docstring with one sentence naming the mutex and the `CommitMutexTimeout` contract.

- [ ] **Step 3b: Map the timeout on the route**

In `web/api/workspaces.py`'s `soft_delete_workspace_api`, wrap the service call:

```python
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # soft_delete_workspace commits internally (unlike create_workspace_api,
        # where the caller commits) — do not add a second commit here.
        try:
            soft_delete_workspace(
                session, workspace_id=ws.id, workspaces_dir=Path(settings.workspaces_dir)
            )
        except CommitMutexTimeout as e:
            # Deletion participates in the commit mutex (so live writers can
            # linearize against it). A wedged writer means nothing was committed —
            # the workspace is untouched and the client may retry.
            session.rollback()
            raise HTTPException(
                status_code=429,
                detail=BUSY_ERROR_MESSAGE,
                headers={"Retry-After": "2"},
            ) from e
```

`CommitMutexTimeout`, `BUSY_ERROR_MESSAGE` and `HTTPException` are already imported in this module (`:62-63`).

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_workspaces_service.py tests/proposal_assist/test_workspaces_delete_route.py \
  tests/proposal_assist/test_commit_mutex_live.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/workspaces.py src/call_manager/web/api/workspaces.py
```

Also re-run every existing caller of the service so the new keyword doesn't break one:

```bash
cd /home/parlar_ai/dev/call-monitor && grep -rn "soft_delete_workspace(" --include=*.py src/ tests/ | grep -v "def soft_delete_workspace"
```
Expected: every call already uses keywords; no positional breakage.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/workspaces.py src/call_manager/web/api/workspaces.py \
  tests/test_workspaces_service.py tests/proposal_assist/test_workspaces_delete_route.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(workspaces): deletion takes the per-workspace commit mutex

Held across the whole delete transaction, before any mutation — this is what
makes acquire_commit_mutex_live's re-read a real linearization point instead of
a hope. A wedged writer now makes DELETE return the house 429 having committed
nothing, rather than a 500 or a partial delete.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: migrate the collab kernel writers (4 sites) + the clean-error tuples

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/kernel.py:136/:221/:316/:427`
- Modify: `src/call_manager/proposal_assist/proposals/kernel_accept.py:43-52`
- Modify: `src/call_manager/proposal_assist/citations/collab_cite.py:34-37`
- Test: `tests/proposal_assist/collab/test_collab_retire_mutex.py` (create)

**Interfaces:**
- Consumes: `acquire_commit_mutex_live`, `WorkspaceDeleted` (Task 1).
- Produces: all four kernel entry points raise `WorkspaceDeleted` before mutating the room doc; the room is NOT evicted (the raise joins the clean-error tuples).

**Why the tuples matter:** `WorkspaceDeleted` is raised at the same place `CommitMutexTimeout` is — before any doc mutation. If it is not in `_CLEAN_ACCEPT_ERRORS` / `_CLEAN_CITE_ERRORS`, a refused write would needlessly evict a clean room and disconnect every editor in it.

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/collab/test_collab_retire_mutex.py`. Follow this directory's **anyio** convention (`@pytest.mark.anyio`), and build the room/proposed-change fixtures exactly the way `test_collab_kernel_accept.py` does — grep that file for its setup helper and reuse it verbatim rather than inventing a divergent one.

```python
"""Mutex-participating collab writers refuse a deleted workspace (Slice B-mutex)."""

from datetime import UTC, datetime

import pytest

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted


def _delete(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()


@pytest.mark.anyio
async def test_kernel_accept_prose_refuses_a_deleted_workspace_without_evicting(
    pg_session_factory, tmp_path
):
    # ... build ws + a live room + a PENDING prose_edit ProposedChange, exactly as
    # test_collab_kernel_accept.py does ...
    _delete(pg_session_factory, ws_id)

    with pytest.raises(WorkspaceDeleted):
        await accept_via_kernel(
            session_factory=pg_session_factory, workspaces_dir=tmp_path,
            workspace_id=ws_id, pc_id=pc_id, approver=owner, artifact="draft",
        )

    # Raised BEFORE any doc mutation => clean error => the room survives.
    room = MANAGER.get((ws_id, "draft"))
    assert room is not None and room.evicted is False
    with pg_session_factory() as s:
        assert s.get(ProposedChange, pc_id).status == "pending"   # nothing applied


@pytest.mark.anyio
async def test_direct_cite_refuses_a_deleted_workspace_without_evicting(
    pg_session_factory, tmp_path
):
    # ... same shape, driving direct_cite_via_kernel ...
    _delete(pg_session_factory, ws_id)
    with pytest.raises(WorkspaceDeleted):
        await direct_cite_via_kernel(...)
    room = MANAGER.get((ws_id, "draft"))
    assert room is not None and room.evicted is False


@pytest.mark.anyio
async def test_kernel_init_spec_and_annotation_insert_refuse(pg_session_factory, tmp_path):
    """The other two kernel mutex sites (kernel.py:316 kernel_init_spec,
    kernel.py:427 apply_annotation_insert_to_room). Both are sync functions —
    call them directly with a session, no room operate() needed."""
    # ... build ws, delete it, assert both raise WorkspaceDeleted ...
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_retire_mutex.py -p no:cacheprovider -q
```
Expected: FAIL — the writes succeed on a deleted workspace.

- [ ] **Step 3a: Migrate the four sites**

In `kernel.py`, replace each of the four `acquire_commit_mutex(session, workspace_id)` calls (`:136`, `:221`, `:316`, `:427`) with:

```python
    acquire_commit_mutex_live(session, workspace_id)
```

Change the import at `kernel.py:53` to `acquire_commit_mutex_live`. Leave every surrounding comment intact except at `:221`, where the existing note about `checkpoint_artifact` re-acquiring the lock gains one sentence:

```python
    # checkpoint_artifact later re-acquires the same xact lock (re-entrant, an
    # instant no-op) and now also re-reads soft_deleted_at — a redundant but
    # harmless second fresh read inside one transaction.
```

- [ ] **Step 3b: Keep a refused write from evicting a clean room**

In `proposals/kernel_accept.py`, add to `_CLEAN_ACCEPT_ERRORS`:

```python
    WorkspaceDeleted,
```
In `citations/collab_cite.py`, add the same to `_CLEAN_CITE_ERRORS`. Import `WorkspaceDeleted` from `call_manager.proposal_assist.drafting.locking` in both. Both tuples already list `CommitMutexTimeout`, which is raised from the same place — keep the new entry adjacent to it.

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab tests/proposal_assist/test_commit_mutex_live.py \
  -p no:cacheprovider -q && uv run ruff check \
  src/call_manager/proposal_assist/collab/kernel.py \
  src/call_manager/proposal_assist/proposals/kernel_accept.py \
  src/call_manager/proposal_assist/citations/collab_cite.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/kernel.py \
  src/call_manager/proposal_assist/proposals/kernel_accept.py \
  src/call_manager/proposal_assist/citations/collab_cite.py \
  tests/proposal_assist/collab/test_collab_retire_mutex.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): kernel writers refuse a deleted workspace under the mutex

All four kernel mutex sites migrate to acquire_commit_mutex_live. The refusal is
raised where CommitMutexTimeout is — before any doc mutation — so it joins the
clean-error tuples: a refused write must not evict a healthy room and disconnect
everyone editing it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: checkpoint + flip — the paths that must SKIP, not fail

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/checkpoint.py:82` and `:184-210` (`checkpoint_active_tails`)
- Modify: `src/call_manager/proposal_assist/collab/permissions.py:54`
- Modify: `src/call_manager/proposal_assist/collab/handover.py:35`
- Modify: `src/call_manager/proposal_assist/web/collab_checkpoint.py:52`
- Modify: `src/call_manager/proposal_assist/collab/flip.py:117`
- Test: `tests/proposal_assist/collab/test_collab_retire_mutex.py` (extend)

**Interfaces:**
- Produces: `checkpoint_artifact` raises `WorkspaceDeleted` on a deleted workspace; its four unguarded callers catch and skip; `enable_collab_on_touch` (`flip.py`) returns `False` for a deleted workspace rather than enabling collab on it.

**Why skip and not propagate:** checkpointing writes git + DB rows. On a deleted workspace that is exactly the mutation we are preventing, and none of these four callers is a user action that deserves an error — they are teardown, a background tick, a kill-switch drain, and a best-effort route-side flush.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/collab/test_collab_retire_mutex.py`:

```python
def test_checkpoint_artifact_refuses_a_deleted_workspace(pg_session_factory, tmp_path):
    # ... build ws with a yjs log, delete it ...
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            checkpoint_artifact(s, workspace_id=ws_id, artifact="draft",
                                workspaces_dir=tmp_path)
        s.rollback()


def test_checkpoint_active_tails_skips_a_workspace_deleted_mid_tick(
    pg_session_factory, tmp_path
):
    """The pre-mutex soft-delete check at checkpoint.py:198 is a TOCTOU; the
    under-mutex raise closes it. The tick must SKIP, not blow up."""
    # ... seed two workspaces with checkpoint-worthy tails, delete one ...
    n = checkpoint_active_tails(pg_session_factory, workspaces_dir=tmp_path, min_tail=1)
    assert n >= 1                      # the live one was checkpointed
    # and the deleted one produced no git commit / no ArtifactVersion row


def test_enable_on_touch_refuses_a_deleted_workspace(pg_session_factory, tmp_path):
    """Enable-on-touch must never flip collab ON for a deleted workspace: that
    writes a CollabArtifactState row and commits."""
    # ... build ws (collab NOT enabled), delete it ...
    with pg_session_factory() as s:
        assert enable_collab_on_touch(
            s, workspace_id=ws_id, artifact="draft", head_md="# x\n",
            artifact_path=tmp_path / "proposal.md",
        ) is False
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_retire_mutex.py -p no:cacheprovider -q \
  -k "checkpoint or enable_on_touch"
```

- [ ] **Step 3a: Migrate `checkpoint.py:82`**

Replace `acquire_commit_mutex(session, workspace_id)` with `acquire_commit_mutex_live(session, workspace_id)` and update the import. Extend the existing comment with:

```python
    # ...and refuse outright if the workspace was deleted while we blocked: a
    # checkpoint writes git + DB rows, which is precisely the mutation deletion
    # must stop. Callers that are teardown/background skip on this.
```

- [ ] **Step 3b: Make the four unguarded callers skip**

`collab/permissions.py:54` — widen the existing except tuple:

```python
                except (GitCommitError, CommitMutexTimeout, WorkspaceDeleted):
```

`collab/checkpoint.py:201` (inside `checkpoint_active_tails`) — wrap the call:

```python
                try:
                    checkpoint_artifact(...)   # unchanged arguments
                except WorkspaceDeleted:
                    # Deleted between the pre-mutex screen at :198 and the mutex.
                    # Not an error: nothing to checkpoint, nothing to retry.
                    session.rollback()
                    continue
```

`collab/handover.py:35` and `web/collab_checkpoint.py:52` — same shape: catch `WorkspaceDeleted`, roll the session back, and skip that artifact. Preserve each site's existing surrounding error handling; only add the new branch.

- [ ] **Step 3c: `flip.py:117` — refuse rather than enable**

The flip already handles a mutex timeout by deferring. Give the deleted case the same shape:

```python
    try:
        acquire_commit_mutex_live(
            session, workspace_id, timeout_seconds=_FLIP_MUTEX_TIMEOUT_SECONDS
        )
    except CommitMutexTimeout:
        session.rollback()
        return False
    except WorkspaceDeleted:
        # Never enable collab on a deleted workspace — enable_collab writes a
        # CollabArtifactState row and COMMITS. Stay legacy; there is no next read.
        session.rollback()
        return False
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab -p no:cacheprovider -q && uv run ruff check \
  src/call_manager/proposal_assist/collab/checkpoint.py \
  src/call_manager/proposal_assist/collab/permissions.py \
  src/call_manager/proposal_assist/collab/handover.py \
  src/call_manager/proposal_assist/web/collab_checkpoint.py \
  src/call_manager/proposal_assist/collab/flip.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/checkpoint.py \
  src/call_manager/proposal_assist/collab/permissions.py \
  src/call_manager/proposal_assist/collab/handover.py \
  src/call_manager/proposal_assist/web/collab_checkpoint.py \
  src/call_manager/proposal_assist/collab/flip.py \
  tests/proposal_assist/collab/test_collab_retire_mutex.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): checkpoint and enable-on-touch refuse a deleted workspace

Checkpointing writes git + DB rows and enable-on-touch commits a
CollabArtifactState row — both are exactly what deletion must stop. The four
teardown/background callers SKIP rather than fail, and the under-mutex raise
closes checkpoint_active_tails' pre-mutex soft-delete TOCTOU.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: migrate the route/service writers (6 sites) + record the `submit.py` decision

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py:536/:647/:735`
- Modify: `src/call_manager/proposal_assist/editing/save.py:180`
- Modify: `src/call_manager/proposal_assist/citations/human_cite.py:102`
- Modify: `src/call_manager/proposal_assist/analysis/insert.py:349`
- Modify: `src/call_manager/proposal_assist/turns/submit.py:238-256` (comment only)
- Test: `tests/proposal_assist/test_commit_mutex_live.py` (extend)

**Interfaces:**
- Produces: each site raises `WorkspaceDeleted`, which the Task-1 app handler turns into 404. No per-route `except` blocks are added — that is the whole point of the single handler.

**Per-site decision — `turns/submit.py:242` KEEPS the plain acquire.** It already performs the guard the helper provides, and more: mutex → `SELECT Workspace.id WHERE soft_deleted_at IS NULL ... FOR UPDATE` → raise. The row lock is strictly stronger, and its `ValueError` is mapped to 400 by `web/api/turns.py`; converting it to `WorkspaceDeleted` would silently change that route's status code. Record the reason in a comment; change no behavior.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/test_commit_mutex_live.py`:

```python
def test_save_section_refuses_a_deleted_workspace(pg_session_factory, tmp_path):
    """Representative service-level writer: no git commit, no DB row."""
    # ... build ws + repo with proposal.md, delete the ws ...
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            save_section(s, workspace=ws, ...)      # reuse editing/save's existing test setup
        s.rollback()
    # HEAD unchanged: the refusal happened before any git write.


def test_deleted_workspace_maps_to_404_on_a_writer_route(
    client_authed_owner, current_user, pg_session_factory
):
    """The single app-level handler, exercised end-to-end through a real route."""
    # ... build ws owned by current_user, take the mutex in a holder thread ONLY long
    # enough that the route blocks, delete the ws, release ...
    # Simpler deterministic variant: delete the ws first, then call the route and
    # assert 404 — require_owner already 404s, so pick a route whose mutex acquire
    # is reachable with the workspace live at require_owner time. If no such
    # deterministic window exists without threading, drive save_section directly
    # (test above) and assert the handler separately with a synthetic raise:
    #   app.get("/__boom") style is NOT available — instead assert
    #   main.create_app() has a registered handler for WorkspaceDeleted:
    #     assert WorkspaceDeleted in app.exception_handlers


def test_submit_user_turn_keeps_its_own_stronger_guard(pg_session_factory):
    """Per-site decision: submit already re-checks under a FOR UPDATE row lock and
    raises ValueError (mapped to 400). Pin that so a later 'uniformity' refactor
    doesn't silently change the route's status code."""
    # ... build ws, delete it, assert submit_user_turn raises ValueError ...
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_commit_mutex_live.py -p no:cacheprovider -q \
  -k "save_section or 404 or submit_user_turn"
```

- [ ] **Step 3a: Migrate the six sites**

Replace the call and the import in each of:
- `web/api/workspaces.py:536` → `acquire_commit_mutex_live(session, ws.id)`
- `web/api/workspaces.py:647` → `acquire_commit_mutex_live(session, ws.id)`
- `web/api/workspaces.py:735` → `acquire_commit_mutex_live(session, ws.id)`
- `editing/save.py:180` → `acquire_commit_mutex_live(session, workspace.id)`
- `citations/human_cite.py:102` → `acquire_commit_mutex_live(session, workspace.id)`
- `analysis/insert.py:349` → `acquire_commit_mutex_live(session, workspace.id)`

Update each module's import line to `acquire_commit_mutex_live`. In `web/api/workspaces.py` **keep** the `acquire_commit_mutex` import — Task 2's delete path still uses it. In `analysis/insert.py`, fix the stale docstring line "`CommitMutexTimeout` (web maps to 503)" to say 429 while you are in there, and add "`WorkspaceDeleted` (404)".

- [ ] **Step 3b: Record the `submit.py` decision in place**

Above `turns/submit.py:242`, add:

```python
    # PER-SITE DECISION (Slice B-mutex): this site keeps the plain acquire. The
    # block below already does what acquire_commit_mutex_live does and more — it
    # re-reads soft_deleted_at under a SELECT ... FOR UPDATE row lock, so submit
    # and soft_delete serialize on the ROW as well as the mutex. Its ValueError is
    # mapped to 400 by web/api/turns.py; swapping in WorkspaceDeleted would change
    # that route's status code for no gain.
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist tests/test_workspaces_service.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/web/api/workspaces.py \
       src/call_manager/proposal_assist/editing/save.py \
       src/call_manager/proposal_assist/citations/human_cite.py \
       src/call_manager/proposal_assist/analysis/insert.py \
       src/call_manager/proposal_assist/turns/submit.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/web/api/workspaces.py \
  src/call_manager/proposal_assist/editing/save.py \
  src/call_manager/proposal_assist/citations/human_cite.py \
  src/call_manager/proposal_assist/analysis/insert.py \
  src/call_manager/proposal_assist/turns/submit.py \
  tests/proposal_assist/test_commit_mutex_live.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(writers): route and service writers refuse a deleted workspace

Six mutex sites migrate to acquire_commit_mutex_live; the single app-level
handler maps the refusal to 404, so no per-route except blocks are needed.
turns/submit.py KEEPS its plain acquire — it already re-checks under a FOR UPDATE
row lock and its ValueError is mapped to 400; the reason is recorded in place.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: migrate the agent-tool writers (4 sites)

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/citations.py:797/:911/:1038/:1213`
- Test: `tests/proposal_assist/test_tools_citations_deleted.py` (create)

**Interfaces:**
- Produces: each of `_insert_citation_locked`, `_remove_citation_locked`, `_update_citation_locked`, `_rename_citekey_locked` returns `{"error": DELETED_ERROR_MESSAGE}` for a deleted workspace.

**Why an error dict, not a raise:** these run inside the SDK subprocess as MCP tools. A raise would surface to the agent as an opaque tool crash; the module's established contract is `{"error": ...}` (that is what the `CommitMutexTimeout` branch two lines away already does).

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/test_tools_citations_deleted.py`, reusing this suite's existing `ToolContext` construction (grep `tests/proposal_assist/test_tools_citations*.py` for how neighbouring tests build `ctx`):

```python
"""Agent citation tools refuse a deleted workspace (Slice B-mutex)."""

from datetime import UTC, datetime

import pytest

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import DELETED_ERROR_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["insert", "remove", "update", "rename"])
async def test_citation_tools_return_an_error_for_a_deleted_workspace(
    tool, pg_session_factory, tmp_path
):
    # ... build ws + repo + a reference/citation as the neighbouring tests do ...
    with pg_session_factory() as s:
        s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
        s.commit()

    result = await _call(tool, ctx)          # dispatch to the impl under test
    assert result == {"error": DELETED_ERROR_MESSAGE}
    # and no git commit was created for the workspace
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_tools_citations_deleted.py -p no:cacheprovider -q
```

- [ ] **Step 3: Migrate the four sites**

At each of `:797`, `:911`, `:1038`, `:1213`, extend the existing try/except:

```python
    try:
        acquire_commit_mutex_live(session, ctx.workspace_id)
    except CommitMutexTimeout:
        return {"error": _BUSY_ERROR}
    except WorkspaceDeleted:
        # The workspace was deleted while the agent was mid-turn. Nothing was
        # mutated; tell the agent plainly rather than crashing the tool.
        session.rollback()
        return {"error": _DELETED_ERROR}
```

Near `_BUSY_ERROR` (`:93`) add:

```python
_DELETED_ERROR = DELETED_ERROR_MESSAGE  # canonical string lives in locking.py
```

and extend the module's `locking` import to bring in `DELETED_ERROR_MESSAGE`, `WorkspaceDeleted`, `acquire_commit_mutex_live`.

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/tools/citations.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/tools/citations.py \
  tests/proposal_assist/test_tools_citations_deleted.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(tools): agent citation tools refuse a deleted workspace

The last four mutex sites. These run as MCP tools inside the SDK subprocess, so
the refusal follows the module's established {"error": ...} contract rather than
raising an opaque tool crash at the agent.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Acceptance gates

**Files:** none (verification only). Any failure → fix on-branch, re-run.

- [ ] **Step 1: Every mutating site is migrated or has a recorded reason**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "acquire_commit_mutex(" --include=*.py src/ | grep -v "def acquire_commit_mutex" \
  | grep -v "acquire_commit_mutex_live"
```
Expected: exactly **two** lines — `workspaces.py` (the delete path, Task 2) and `turns/submit.py` (the recorded per-site decision, Task 5) — plus the `locking.py` internal call inside `acquire_commit_mutex_live`. Anything else is an unmigrated writer.

- [ ] **Step 2: Grouped backend sweep**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist tests/test_workspaces_service.py tests/test_api_spec.py \
  -p no:cacheprovider -q
```
Expected: all pass. `connection refused` from testcontainers PG is an infra flake → re-run the flagged file isolated.

- [ ] **Step 3: Files outside the sweep that import what changed**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_scheduler.py tests/test_scheduler_digest.py tests/test_main_app.py \
  -p no:cacheprovider -q
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_scheduler_single_instance.py -p no:cacheprovider -q
```
(The single-instance test runs isolated — a known nested-lifespan bug in another file poisons it when run together.) Substitute the real filename if `tests/test_main_app.py` does not exist; the point is to cover whatever exercises `create_app()`'s handler registration.

- [ ] **Step 4: Lint (whole repo, as CI runs it)**

```bash
cd /home/parlar_ai/dev/call-monitor && uv run ruff check .
```
Expected: `All checks passed!`

- [ ] **Step 5: Frontend regression (untouched — must stay green)**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit
cd /home/parlar_ai/dev/call-monitor
```
Expected: silent; zero frontend changes, no `schema.gen.ts` diff. **Re-anchor to the repo root** (the second `cd`) — a lingering `cd frontend` makes later relative `git`/`pytest` paths resolve wrongly.

- [ ] **Step 6: Diff-scope check**

```bash
git -C /home/parlar_ai/dev/call-monitor diff --stat master...workspace-retirement-mutex
```
Expected: only the files in the File Structure table. **No migration, no frontend, no `schema.gen.ts`.**

- [ ] **Step 7: Honest-scope gate (must FAIL the build if violated)**

```bash
cd /home/parlar_ai/dev/call-monitor
if git diff master...workspace-retirement-mutex -- '*.py' \
     | grep -nEi "all writers|every writer|cannot write after|no writes after|proves.*stopped|fully prevents"; then
  echo "FAIL: overclaiming language introduced — this slice covers MUTEX-PARTICIPATING writers only; settings.py update_settings/add_member remain uncovered"
  exit 1
fi
echo "OK: no overclaiming language"
```
Expected: `OK: no overclaiming language`.

- [ ] **Step 8: Prove the gate can fail**

```bash
cd /home/parlar_ai/dev/call-monitor
if git diff master...workspace-retirement-mutex -- '*.py' | grep -qEi "commit mutex"; then
  echo "gate control flow verified: it detects a pattern that is definitely present"
else
  echo "BROKEN GATE"
fi
```

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** B4 → Tasks 1, 3, 4, 5, 6 (helper + all 17 sites, each migrated or with a recorded reason); B4's "deletion takes the same mutex" → Task 2; spec test 12 (two-writer straddle) → Task 1 Step 1's `test_writer_blocked_on_the_mutex_sees_a_deletion_that_commits_meanwhile` + Task 2's service-level twin + Task 3's collab twin; spec test 13 (identity-map staleness) → Task 1's `test_reads_fresh_state_not_the_identity_map`; spec test 14's "contended delete → 429 with nothing committed" → Task 2. **Deferred to B-retire by design:** B1 coordinator, B2 turns tombstone, B3 collab retirement, B5 service split + `signal_workspace_retirement` + 204 ordering.
- **Type consistency:** `acquire_commit_mutex_live(session, workspace_id, *, timeout_seconds=10) -> None` (T1 def; T3/T4/T5/T6 calls — `timeout_seconds` passed only at `flip.py`, matching its existing `_FLIP_MUTEX_TIMEOUT_SECONDS` usage). `WorkspaceDeleted(workspace_id)` with `.workspace_id` (T1 def; T1/T3/T4/T5 assertions). `DELETED_ERROR_MESSAGE` (T1 def; T6 alias `_DELETED_ERROR`, mirroring the existing `_BUSY_ERROR` alias).
- **Ordering:** the helper (T1) precedes every consumer; delete-takes-the-mutex (T2) precedes the migrations so each can be tested against real contention; the clean-error tuples (T3b) land in the same commit as the kernel sites that raise into them.
- **Placeholder scan:** three test bodies are deliberately prose-sketched — Task 3's kernel/room fixtures, Task 5's route-404 test, Task 6's `ToolContext` build — because each must reuse an existing suite's construction helper verbatim rather than a divergent invention. Every assertion, exception type and expected value in them is exact. Task 2's delete-route test carries an explicit implementer decision (mutex timeout vs. suite speed) flagged inline rather than silently assumed.
- **Known sharp edge:** Task 1 Step 3a may hit a circular import (`locking` → `db.models`). The step says to fall back to a function-local import and document it, rather than restructuring `db.models`.

## Execution Handoff

Two execution options:

**1. Subagent-Driven** — a fresh subagent per task with review between tasks.

**2. Inline Execution** — execute in this session via `superpowers:executing-plans`, batching with checkpoints.
