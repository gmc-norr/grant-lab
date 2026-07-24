# Workspace Retirement — Slice B-mutex (live-workspace commit mutex) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **v2 — revised after a Codex review of v1** that found two implementation holes and three plan/gate gaps, all verified against code and folded below. One finding generalized during verification: the pre-mutex mutation exists in **both** kernel accept entry points, not just `kernel_accept_prose`.

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
| `checkpoint_artifact` callers | 10 sites; the 4 that are NOT already inside a `_live`-guarded transaction are `collab/permissions.py:54` (**skip** — already catches `(GitCommitError, CommitMutexTimeout)`), `collab/checkpoint.py:201` (`checkpoint_active_tails` — **skip**; this also closes its pre-mutex soft-delete check at `:198`), `collab/handover.py:35` (**skip, quietly**), and `web/collab_checkpoint.py:52` (**propagate → 404**, NOT skip — see the next row) |
| `soft_delete_workspace` | `workspaces.py:82`, `(session, *, workspace_id, workspaces_dir) -> None`; commits internally at `:133` (after Slice A's running-turn terminalization); FS scrub at `:134-148` |
| DELETE route | `web/api/workspaces.py:1488` `soft_delete_workspace_api`; `require_owner` → `soft_delete_workspace` → `signal_from_request(...)` → `Response(204)`. **No `CommitMutexTimeout` handler today** |
| Test factories | `make_workspace(session, *, owner=None, ...)`; `make_user(session, *, email="owner@ki.se")` — default is **FIXED**, so two defaulted users in one test collide; always pass distinct emails |
| **Pre-mutex mutation (v2 P1)** | **BOTH** accept entry points mutate `ProposedChange` before any mutex is held, and their wrapper commits: `kernel_accept_prose` flushes `status="stale"` at `kernel.py:204-208`, above its acquire at `:221`; `kernel_accept_citation`'s `_stale()` (`:361-366`) fires when `apply_citation_to_room` raises `CiteStale/CiteInvalid/CiteRefNotFound` — and that raise is at `kernel.py:132`, **above** the acquire at `:136`. Replacing the acquire in place would leave both holes open |
| `_load_scoped` | takes `SELECT … FOR UPDATE` on the `ProposedChange` row. Hoisting the mutex above it fixes the holes **and** pins one lock order everywhere: advisory mutex → row lock |
| `web/collab_checkpoint.py:51` | `res = checkpoint_artifact(...)` is assigned **inside** the `try` and read **after** it (`:66-70`), and there is no loop. A catch-and-skip here is an `UnboundLocalError` → 500. It is also the user-triggered "Save a version" route, so the right answer is to let `WorkspaceDeleted` propagate to the app handler → **404**. **This file needs no change** |
| `collab/handover.py:51` | already has a broad `except Exception: rollback + log.error + continue`, and its `res` is used **inside** the try — so a deleted workspace is already isolated, just noisily. Needs only a quiet explicit branch |
| `soft_delete_workspace` callers | one production caller (`web/api/workspaces.py:1504`) + 4 test call sites; none calls it twice on the same workspace, so making a re-delete raise is safe |
| App test file | **`tests/test_main.py`** (there is no `tests/test_main_app.py`) |

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
| `src/call_manager/proposal_assist/collab/handover.py` | Modify — kill-switch drain skips **quietly** (ahead of its broad handler) |
| ~~`src/call_manager/proposal_assist/web/collab_checkpoint.py`~~ | **NOT modified** — the user-triggered "Save a version" route lets `WorkspaceDeleted` propagate to the app handler → 404 (v2 P2) |
| `src/call_manager/proposal_assist/collab/flip.py` | Modify — enable-on-touch refuses a deleted workspace |
| `src/call_manager/proposal_assist/editing/save.py` | Modify — site migrated |
| `src/call_manager/proposal_assist/citations/human_cite.py` | Modify — site migrated |
| `src/call_manager/proposal_assist/analysis/insert.py` | Modify — site migrated |
| `src/call_manager/proposal_assist/tools/citations.py` | Modify — 4 sites migrated, deleted → error dict |
| `tests/proposal_assist/test_commit_mutex_live.py` | **Create** |
| `tests/test_workspaces_service.py` | Extend — delete takes the mutex |
| `tests/proposal_assist/collab/test_collab_retire_mutex.py` | **Create** — collab writers refuse |
| `tests/proposal_assist/test_tools_citations_deleted.py` | **Create** |
| `tests/proposal_assist/test_workspaces_delete_route.py` | **Create** — 429 on contention; concurrent double-delete |
| `tests/proposal_assist/test_retire_mutex_straddle.py` | **Create** (Task 7) — the spec's real-writer straddle: writers **blocked on the mutex** when the deletion commits |
| `scripts/honest_scope_gate.sh` | **Create** (Task 8) — the overclaim predicate, factored out so the gate can be run against known-bad input |

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
- Consumes: `acquire_commit_mutex_live`, `WorkspaceDeleted`, `CommitMutexTimeout` (Task 1).
- Produces: `soft_delete_workspace` holds the commit mutex across its whole transaction and **re-reads liveness under it**; the DELETE route maps `CommitMutexTimeout` to **429** having committed nothing, and a lost double-delete to **404**.

**Why the mutex goes FIRST:** it must be held *before* `ws.soft_deleted_at` is set, or a writer could win the mutex, pass the live-check, and then have the deletion commit underneath it — the very ordering the helper exists to prevent.

**Why the delete uses `_live`, not the plain acquire (v2 P2):** `soft_delete_workspace` loads `ws` via `session.get` at `workspaces.py:98`, *before* it could possibly block on the mutex. Two concurrent DELETEs can both pass `require_owner` while the workspace is live; the loser then wins the mutex and re-stamps `soft_deleted_at` off that stale identity-map instance, returning a second **204**. That contradicts the route's own documented contract ("Re-delete of an already-soft-deleted workspace → 404 … not idempotent, by design", `web/api/workspaces.py:1494`). Using `acquire_commit_mutex_live` makes the loser raise `WorkspaceDeleted`, which the Task-1 handler renders as exactly that 404 — the contract, for free.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workspaces_service.py`:

```python
def test_soft_delete_refuses_a_second_deletion(pg_session_factory, tmp_path):
    """The delete's own under-mutex liveness read (v2 P2). Sequential here; the
    concurrent form is the route test below."""
    from call_manager.proposal_assist.drafting.locking import WorkspaceDeleted
    from call_manager.workspaces import soft_delete_workspace
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id

    with pg_session_factory() as s:
        soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)
    with pg_session_factory() as s:
        with pytest.raises(WorkspaceDeleted):
            soft_delete_workspace(s, workspace_id=ws_id, workspaces_dir=tmp_path)
        s.rollback()


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


def test_concurrent_double_delete_yields_exactly_one_204_and_one_404(
    client_authed_owner, current_user, pg_session_factory
):
    """v2 P2: both requests pass require_owner while the workspace is live; the
    loser wins the mutex afterwards and MUST see the committed deletion via the
    under-mutex re-read, not re-stamp it from a stale identity-map instance.
    404 is the route's own documented re-delete contract."""
    import threading

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    codes: list[int] = []
    start = threading.Barrier(2)
    lock = threading.Lock()

    def _delete():
        start.wait(timeout=10)
        r = client_authed_owner.delete(f"/api/workspaces/{ws_id}")
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=_delete) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(codes) == [204, 404]
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).soft_deleted_at is not None
```

**Implementer note:** if `require_owner` filters the loser out *before* the mutex (the common, non-racing interleaving) the loser's 404 comes from `require_owner` instead of the helper — the assertion still holds and is still the contract. The point of the test is that **no interleaving yields two 204s**. If the barrier proves too coarse to reach the under-mutex path at all, keep this test *and* add the deterministic service-level twin from Task 7's straddle helper.

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
    #
    # _live, not the plain acquire: `ws` above was loaded BEFORE we could block on
    # the mutex, so a concurrent deletion that committed while we waited would be
    # invisible to it and we would re-stamp soft_deleted_at off a stale instance
    # and return a second 204. The fresh under-mutex read raises WorkspaceDeleted
    # instead — which is exactly this route's documented re-delete 404.
    acquire_commit_mutex_live(session, workspace_id, timeout_seconds=timeout_seconds)
```

Import `acquire_commit_mutex_live` (and `WorkspaceDeleted` if you reference it) from `call_manager.proposal_assist.drafting.locking`, and extend the function's docstring with one sentence naming the mutex, the `CommitMutexTimeout` contract, and the `WorkspaceDeleted`-on-re-delete contract.

Because the helper already validates existence, the pre-existing `if ws is None: raise ValueError(...)` guard now has a second, stricter gate behind it. **Keep** the `ValueError` guard — it is the "caller bypassed `require_owner`" contract check and its message is asserted by an existing test; the helper's raise is the *concurrency* gate. Order: `session.get` → `ValueError` guard → `acquire_commit_mutex_live`.

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
- Produces: all four kernel entry points raise `WorkspaceDeleted` before mutating **anything** — room doc *or* `ProposedChange` row; the room is NOT evicted (the raise joins the clean-error tuples).

**Why the tuples matter:** `WorkspaceDeleted` is raised at the same place `CommitMutexTimeout` is — before any doc mutation. If it is not in `_CLEAN_ACCEPT_ERRORS` / `_CLEAN_CITE_ERRORS`, a refused write would needlessly evict a clean room and disconnect every editor in it.

**Why the acquire is HOISTED, not replaced in place (v2 P1 — the finding with teeth):** an in-place swap leaves two real holes, because both accept entry points mutate the `ProposedChange` on their *early-exit* paths, above any mutex:

| Entry point | Pre-mutex mutation | Reachable when |
|---|---|---|
| `kernel_accept_prose` | `pc.status="stale"` + `resolved_by/at` + `session.flush()` (`kernel.py:204-208`) | the live section SHA moved — i.e. any concurrent edit |
| `kernel_accept_citation` | `_stale()` (`:361-366`), same three columns + flush | `apply_citation_to_room` raises `CiteStale/CiteInvalid/CiteRefNotFound` — raised at `:132`, **above** that function's own acquire at `:136` |

`accept_via_kernel` commits the transaction, so on a deleted workspace either path would durably resolve a proposal that must not be touched. Hoisting the acquire to the **top of both functions**, above `_load_scoped`, closes both — and pins one lock order process-wide: **advisory mutex → `ProposedChange` row lock**, never the reverse. `apply_citation_to_room`'s own acquire then becomes a re-entrant no-op with one redundant fresh read; leave it in place (it is also called directly by `direct_cite_via_kernel`, which has no other guard).

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


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["prose_edit", "citation_insert"])
async def test_a_STALE_proposal_on_a_deleted_workspace_is_not_resolved(
    kind, pg_session_factory, tmp_path
):
    """v2 P1 REGRESSION. Both accept paths resolve a proposal to 'stale' on an
    early-exit branch that sits ABOVE any mutex, and the wrapper commits. With the
    acquire merely swapped in place, a deleted workspace still gets a durable
    ProposedChange mutation. Build a proposal that IS stale (bump the live section
    so base_section_sha no longer matches), delete the workspace, and assert the
    row is untouched.

    This is the test that distinguishes 'replaced the acquire' from 'hoisted it'.
    """
    # ... build ws + live room + a PENDING proposal of `kind`;
    #     mutate the room section so the proposal is now STALE ...
    _delete(pg_session_factory, ws_id)

    with pytest.raises(WorkspaceDeleted):
        await accept_via_kernel(
            session_factory=pg_session_factory, workspaces_dir=tmp_path,
            workspace_id=ws_id, pc_id=pc_id, approver=owner, artifact="draft",
        )

    with pg_session_factory() as s:
        pc = s.get(ProposedChange, pc_id)
        assert pc.status == "pending"        # NOT "stale"
        assert pc.resolved_by is None
        assert pc.resolved_at is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_retire_mutex.py -p no:cacheprovider -q
```
Expected: FAIL — the writes succeed on a deleted workspace.

- [ ] **Step 3a: Migrate the four sites — two in place, two HOISTED**

In `kernel.py`, change the import at `:53` to `acquire_commit_mutex_live`, then:

**In place** (nothing mutates above them) — `:136` (`apply_citation_to_room`) and `:316` (`kernel_init_spec`) and `:427` (`apply_annotation_insert_to_room`): replace the call with

```python
    acquire_commit_mutex_live(session, workspace_id)
```
leaving every surrounding comment intact.

**Hoisted** — `kernel_accept_prose` (`:183`): **delete** the acquire at `:221` and put it at the very top of the function body, above `pc = _load_scoped(...)`:

```python
def kernel_accept_prose(...) -> KernelAcceptResult:
    # Mutex FIRST — above _load_scoped, above the staleness branch. Two reasons:
    #   (1) the staleness branch below resolves the ProposedChange to "stale" and
    #       flushes, and accept_via_kernel commits — so a workspace deleted mid-turn
    #       would get a durable row mutation if we only guarded the later write path;
    #   (2) it pins ONE lock order everywhere: advisory mutex -> ProposedChange row
    #       lock (_load_scoped takes FOR UPDATE), never the reverse.
    # It also still precedes the spec_locked_at read below, which is what the
    # original placement was for (Codex H3): post_lock_spec sets that lock under
    # this same mutex, so holding it here freezes the lock state for our whole
    # transaction. checkpoint_artifact re-acquires it later — advisory xact locks
    # are re-entrant, so that is an instant no-op with one redundant fresh read.
    acquire_commit_mutex_live(session, workspace_id)

    pc = _load_scoped(session, proposed_change_id, workspace_id)  # FOR UPDATE
    ...
```
Replace the now-orphaned comment block at `:216-220` with a one-liner pointing at the top of the function, so the H3 rationale is not lost.

**Hoisted** — `kernel_accept_citation` (`:340`): add the same acquire as the first statement of the body, above `pc = _load_scoped(...)`:

```python
    # Mutex FIRST (see kernel_accept_prose): _stale() below flushes a resolved
    # ProposedChange, and it fires on CiteStale/CiteInvalid/CiteRefNotFound —
    # which apply_citation_to_room raises ABOVE its own acquire. Guarding only
    # that inner acquire would leave the stale path unguarded.
    acquire_commit_mutex_live(session, workspace_id)
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
- **NOT modified:** `src/call_manager/proposal_assist/web/collab_checkpoint.py` — see below
- Modify: `src/call_manager/proposal_assist/collab/flip.py:117`
- Test: `tests/proposal_assist/collab/test_collab_retire_mutex.py` (extend)

**Interfaces:**
- Produces: `checkpoint_artifact` raises `WorkspaceDeleted` on a deleted workspace; its **three** background/teardown callers skip; the **user-triggered route propagates** to the app handler → 404; `enable_collab_on_touch` (`flip.py`) returns `False` rather than enabling collab on a deleted workspace.

**Why skip:** checkpointing writes git + DB rows. On a deleted workspace that is exactly the mutation we are preventing, and teardown / a background tick / a kill-switch drain are not user actions that deserve an error.

**Why the explicit endpoint does NOT skip (v2 P2):** `web/collab_checkpoint.py` is the user-facing "Save a version" route. Two reasons a skip is wrong there: mechanically, `res` is assigned inside the `try` (`:51`) and read after it (`:66-70`) with no loop, so catch-and-continue is an `UnboundLocalError` → 500; and semantically, a user who asked to save a version deserves to be told the workspace is gone. Let `WorkspaceDeleted` propagate to the Task-1 handler → **404**. That file needs no edit — only a test pinning the 404.

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

`collab/handover.py:35` — it already isolates per artifact with a broad `except Exception: rollback + log.error + continue` (`:51`), so a deleted workspace is handled but logged as an **error**. Add a quiet explicit branch **above** the broad one, so a legitimate mid-drain deletion is not alarming noise:

```python
            except WorkspaceDeleted:
                # Deleted between the pre-screen above and the mutex. Expected,
                # not a failure: nothing to hand over.
                s.rollback()
                continue
```

`web/collab_checkpoint.py` — **no change.** `WorkspaceDeleted` propagates to the app-level handler and the route returns 404. Add only a test:

```python
def test_explicit_checkpoint_endpoint_404s_for_a_deleted_workspace(
    client_authed_owner, current_user, pg_session_factory
):
    """The user-triggered 'Save a version' route must SAY the workspace is gone,
    not skip (which would read an unbound `res` and 500)."""
    # ... build ws with collab enabled on 'draft', delete it ...
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/collab/draft/checkpoint")
    assert r.status_code == 404
```
Confirm the exact route path from the module's decorator before writing the call.

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
  src/call_manager/proposal_assist/collab/flip.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/checkpoint.py \
  src/call_manager/proposal_assist/collab/permissions.py \
  src/call_manager/proposal_assist/collab/handover.py \
  src/call_manager/proposal_assist/collab/flip.py \
  tests/proposal_assist/collab/test_collab_retire_mutex.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): checkpoint and enable-on-touch refuse a deleted workspace

Checkpointing writes git + DB rows and enable-on-touch commits a
CollabArtifactState row — both are exactly what deletion must stop. The three
teardown/background callers SKIP rather than fail; the user-triggered "Save a
version" route deliberately does NOT (its `res` is read after the try, so a skip
would 500, and a user who asked to save deserves the 404). The under-mutex raise
also closes checkpoint_active_tails' pre-mutex soft-delete TOCTOU.

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

### Task 7: the real-writer straddle (spec test 12)

**Files:**
- Test: `tests/proposal_assist/test_retire_mutex_straddle.py` (create). No source changes — if this task needs one, an earlier task was wrong.

**Interfaces:**
- Consumes: everything Tasks 1–6 built.

**Why this task exists (v2 P1-test):** Tasks 1–6 test the helper directly, and test concrete writers only *after* a deletion has already committed. Neither shape proves the property the spec actually asks for (`design.md:217-220`): that a real writer **already blocked on the mutex** when the deletion commits then mutates **nothing**. That is the property the whole slice claims, and a straddle test against `accept_via_kernel` is precisely what would have caught the Task-3 hoist bug — under a straddle, the un-hoisted code resolves the proposal to `stale` and commits it.

**The blocking probe.** Do not sleep-and-hope. Postgres publishes lock waits, so poll for the writer to be genuinely queued:

```python
def _advisory_waiters(session_factory) -> int:
    """Count ungranted advisory locks. The test DB is otherwise idle, so a
    non-zero count means our writer really is queued behind the mutex — not
    merely 'started' (a sleep would race on a loaded machine)."""
    from sqlalchemy import text

    with session_factory() as s:
        return s.scalar(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
        )


def _wait_until_blocked(session_factory, *, n: int = 1, timeout: float = 10.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _advisory_waiters(session_factory) >= n:
            return
        time.sleep(0.02)
    raise AssertionError(f"no writer blocked on the advisory lock within {timeout}s")
```

- [ ] **Step 1: Write the straddle tests**

Create `tests/proposal_assist/test_retire_mutex_straddle.py`:

```python
"""Spec test 12 — real writers BLOCKED on the commit mutex when a deletion
commits must wake up, refuse, and mutate nothing.

Shape, for every case:
  1. a holder thread takes the commit mutex and parks;
  2. the writer thread calls the REAL entry point -> queues behind it;
  3. we poll pg_locks until the writer is genuinely blocked;
  4. the holder marks the workspace deleted and COMMITS (releasing the mutex);
  5. the writer wins the mutex, re-reads, and must raise WorkspaceDeleted
     having written no git commit and no DB row.
"""
```

Write one test per writer, all three the spec names:

- **`test_blocked_save_section_refuses`** — the legacy git-first writer. Assert `WorkspaceDeleted`, and that git `HEAD` for the workspace repo is byte-identical to the pre-delete SHA.
- **`test_blocked_kernel_accept_refuses_and_leaves_the_proposal_pending`** — parametrize over `prose_edit` and `citation_insert`, and run each **twice**: once with a fresh proposal and once with a **stale** one (bump the live section first). The stale variant is the one that fails against an un-hoisted acquire. Assert `status == "pending"`, `resolved_by is None`, `resolved_at is None`, and that the room was not evicted.
- **`test_blocked_enable_on_touch_returns_false`** — `flip.py`'s variant: assert `False`, and that **no** `CollabArtifactState` row exists for the workspace/artifact.

Each test's step 4 must use a **separate session** that holds the mutex, sets `soft_deleted_at`, and commits — not `soft_delete_workspace`, which would take the mutex the holder already owns. State that in a comment so a later reader does not "simplify" it into a deadlock.

- [ ] **Step 2: Run — and expect the stale-proposal cases to be the interesting ones**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/test_retire_mutex_straddle.py -p no:cacheprovider -q
```
Expected: all pass. If a stale-proposal case fails with the proposal resolved to `"stale"`, Task 3's hoist was not applied to that entry point — fix Task 3, do not weaken this test.

- [ ] **Step 3: Sanity-check that the probe actually probes**

Temporarily change `_wait_until_blocked`'s poll to `return` immediately and re-run: at least one test must become flaky or fail. Restore it. This proves the tests are synchronizing on a real lock wait rather than passing by luck.

- [ ] **Step 4: Lint**

```bash
cd /home/parlar_ai/dev/call-monitor && uv run ruff check tests/proposal_assist/test_retire_mutex_straddle.py
```

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add tests/proposal_assist/test_retire_mutex_straddle.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
test(retirement): real writers blocked on the mutex refuse and mutate nothing

Spec test 12. Testing the helper in isolation proves nothing about the actual
entry points; these park a holder on the mutex, poll pg_locks until the real
writer is genuinely queued, then commit the deletion underneath it. The stale-
proposal variants are the ones that fail if the kernel acquire is swapped in
place rather than hoisted above the early-exit mutation.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Acceptance gates

**Files:** none (verification only). Any failure → fix on-branch, re-run.

- [ ] **Step 1: Every mutating site is migrated or has a recorded reason**

```bash
cd /home/parlar_ai/dev/call-monitor
grep -rn "acquire_commit_mutex(" --include=*.py src/ | grep -v "def acquire_commit_mutex" \
  | grep -v "acquire_commit_mutex_live"
```
Expected: exactly **two** lines — `turns/submit.py:242` (the recorded per-site decision, Task 5) and the internal call inside `acquire_commit_mutex_live` in `locking.py`. Anything else is an unmigrated writer. Note `workspaces.py`'s delete path uses `_live` (Task 2, v2 P2), so it must NOT appear here.

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
  tests/test_scheduler.py tests/test_scheduler_digest.py tests/test_main.py \
  -p no:cacheprovider -q
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_scheduler_single_instance.py -p no:cacheprovider -q
```
`tests/test_main.py` is the verified filename (there is no `tests/test_main_app.py`) and is what exercises `create_app()`'s handler registration. The single-instance test runs **isolated** — a known nested-lifespan bug in another file poisons it when run together.

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

The predicate is factored into a function so Step 8 can execute **the same code** against known-bad input. A gate that is never run against something it should reject is not a verified gate.

Write `scripts/honest_scope_gate.sh`:

```bash
#!/usr/bin/env bash
# Slice B-mutex honest-scope gate. Reads candidate text on stdin; exits 1 if it
# contains language claiming more than the slice delivers. This slice covers
# MUTEX-PARTICIPATING writers only — settings.py update_settings/add_member are
# an accepted, documented residual.
set -uo pipefail

OVERCLAIM='all writers|every writer|cannot write after|no writes after|proves.*stopped|fully prevents'

if grep -nEi "$OVERCLAIM"; then
  echo "FAIL: overclaiming language introduced — this slice covers MUTEX-PARTICIPATING writers only" >&2
  exit 1
fi
exit 0
```

Run it:

```bash
cd /home/parlar_ai/dev/call-monitor && chmod +x scripts/honest_scope_gate.sh
git diff master...workspace-retirement-mutex -- '*.py' | ./scripts/honest_scope_gate.sh \
  && echo "OK: no overclaiming language"
```
Expected: `OK: no overclaiming language`.

- [ ] **Step 8: Prove the gate can fail — by running it on known-bad input**

```bash
cd /home/parlar_ai/dev/call-monitor
# Feed the SAME predicate a line it must reject, and assert a non-zero exit.
if echo '# this fully prevents any write after deletion' | ./scripts/honest_scope_gate.sh; then
  echo "BROKEN GATE: accepted known-overclaiming input" >&2
  exit 1
fi
# And a line it must accept, so the gate is not simply rejecting everything.
if ! echo '# takes the per-workspace commit mutex' | ./scripts/honest_scope_gate.sh; then
  echo "BROKEN GATE: rejected known-good input" >&2
  exit 1
fi
echo "gate verified: rejects overclaiming input, accepts ordinary input"
```
Expected: `gate verified: rejects overclaiming input, accepts ordinary input`. Note both failure branches `exit 1` — v1's echoed a message and exited 0, so a broken gate would have reported success.

Add `scripts/honest_scope_gate.sh` to the File Structure table's created files and to Task 8's commit.

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** B4 → Tasks 1, 3, 4, 5, 6 (helper + all 17 sites, each migrated or with a recorded reason); B4's "deletion takes the same mutex" → Task 2; **spec test 12 (real-writer straddle) → Task 7** — collab accept, `save_section`, and `flip.py`, each genuinely blocked on the mutex when the deletion commits; spec test 13 (identity-map staleness) → Task 1's `test_reads_fresh_state_not_the_identity_map`; spec test 14's "contended delete → 429 with nothing committed" → Task 2. **Deferred to B-retire by design:** B1 coordinator, B2 turns tombstone, B3 collab retirement, B5 service split + `signal_workspace_retirement` + 204 ordering.
- **v2 review fixes folded:** P1-1 → Task 3 (acquire **hoisted** above `_load_scoped` in **both** `kernel_accept_prose` *and* `kernel_accept_citation` — the second hole found while verifying the first — plus the stale-proposal regression test). P2-2 → Task 4 (`web/collab_checkpoint.py` is now explicitly **not modified**; `WorkspaceDeleted` propagates to the 404 handler, since its `res` is read after the `try` and a skip would 500). P2-3 → Task 2 (deletion uses `acquire_commit_mutex_live`, so the losing concurrent DELETE gets the route's own documented re-delete 404 instead of a second 204). P1-test-4 → **new Task 7**, with a `pg_locks` probe instead of a sleep, and the weak either-outcome service test deleted. P2-5 → Task 8 (predicate factored into `scripts/honest_scope_gate.sh`, executed against known-bad *and* known-good input, both failure branches `exit 1`; verified empirically at plan-writing time: bad input → exit 1, good input → exit 0). `tests/test_main_app.py` → `tests/test_main.py`.
- **Type consistency:** `acquire_commit_mutex_live(session, workspace_id, *, timeout_seconds=10) -> None` (T1 def; T2/T3/T4/T5/T6 calls — `timeout_seconds` passed at `flip.py`, matching its existing `_FLIP_MUTEX_TIMEOUT_SECONDS`, and at `soft_delete_workspace`, which now takes the same keyword). `WorkspaceDeleted(workspace_id)` with `.workspace_id` (T1 def; T1–T5, T7 assertions). `DELETED_ERROR_MESSAGE` (T1 def; T6 alias `_DELETED_ERROR`, mirroring the existing `_BUSY_ERROR` alias).
- **Ordering:** the helper (T1) precedes every consumer; delete-takes-the-mutex (T2) precedes the migrations so each can be tested against real contention; the clean-error tuples (T3b) land in the same commit as the kernel sites that raise into them; the straddle suite (T7) runs last because it exercises all of them at once.
- **Lock-order note:** hoisting the kernel acquire above `_load_scoped` makes the order **advisory mutex → `ProposedChange` row lock** on every path. Keep it that way; the reverse order on any future path is a deadlock.
- **Placeholder scan:** three test bodies stay deliberately prose-sketched — Task 3's kernel/room fixtures, Task 5's route-404 test, Task 6's `ToolContext` build — because each must reuse an existing suite's construction helper verbatim rather than a divergent invention. Every assertion, exception type and expected value in them is exact. Task 2's delete-route test and Task 7's straddle probe each carry an explicit implementer decision flagged inline rather than silently assumed.
- **Known sharp edge:** Task 1 Step 3a may hit a circular import (`locking` → `db.models`). The step says to fall back to a function-local import and document it, rather than restructuring `db.models`.

## Execution Handoff

Two execution options:

**1. Subagent-Driven** — a fresh subagent per task with review between tasks.

**2. Inline Execution** — execute in this session via `superpowers:executing-plans`, batching with checkpoints.
