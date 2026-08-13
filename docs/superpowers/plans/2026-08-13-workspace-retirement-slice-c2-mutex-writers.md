# Workspace Retirement Slice C2 — Writers Under the Live Mutex Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring six owner-write routes under `acquire_commit_mutex_live` so a deletion committing mid-route is observed before any mutation, and delete the `soft_delete_workspace` wrapper that skips retirement entirely.

**Architecture:** Each of the six routes takes the per-workspace commit mutex immediately after `require_owner` and before any other lock. `soft_delete_workspace_db` holds that same mutex for its whole transaction, so a writer blocked on the mutex sees the deletion the instant it wins the lock and raises `WorkspaceDeleted` having mutated nothing. Error mapping is written per route: `CommitMutexTimeout` → 429 locally, `WorkspaceDeleted` → 404 via main.py's registered handler.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL 16 advisory locks (`pg_advisory_xact_lock`), pytest + testcontainers Postgres, `uv`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md` §8 (writers under the live mutex), §9 (removing the `soft_delete_workspace` foot-gun), §12 gates 11 and 13.

**Prerequisites:** Slice C1 merged as `53fa9fc`. Phase-0 merged as `d459ce1`. Slice B-retire merged as `245d7ba`.

---

## Global Constraints

Every task's requirements implicitly include this section.

**From the spec, verbatim:**

- **Lock-order rule (normative).** `acquire_commit_mutex_live` is taken **immediately after `require_owner`, before any other lock**.
- **Error mapping is written per route, explicitly.** There is no app-level `CommitMutexTimeout` handler. Each of the six gets `except CommitMutexTimeout` → 429 with `BUSY_ERROR_MESSAGE` and `Retry-After: 2`; `WorkspaceDeleted` propagates to main.py's handler → 404.
- **The claim is narrow.** This does *not* make every workspace writer linearized. The accurate claim is: **these six routes are linearized.** Do not write a comment, docstring, or commit message claiming more.
- **The six routes:** `update_settings`, `add_member`, `remove_member` (`web/api/settings.py`); `update_rubric`, `activate`, `retry_setup` (`web/api/rubric.py`).

**Corrected rationale — read this, it changes what Task 4 tests.**
The spec justifies the lock-order rule by claiming `activate`/`retry_setup` taking `WorkspaceSetupJob … FOR UPDATE` first "inverts against delete's mutex→job-row order and deadlocks". **That deadlock is not reachable, and a test asserting it would pass no matter which order you use.** Verified against the merged code:

- `soft_delete_workspace_db` (`src/call_manager/workspaces.py:106-177`) never touches `WorkspaceSetupJob`. Its mutex transaction locks the `Workspace` row, the four pass-job tables, and `TurnJob` — that is all.
- Setup-job cancellation happens in a **separate, post-commit transaction** (`jobs_retirement.py:132`, called from the retirement hook after the DB half commits) and uses `FOR UPDATE SKIP LOCKED`, which never blocks.
- Every other `WorkspaceSetupJob` acquisition is either `SKIP LOCKED` (`jobs_retirement.py:107` the cancel, `:169` the reconcile; `phases/setup.py:294` the claim) or blocking-but-mutexless (`phases/setup.py:529` `_m3`, `:595` `_fail`, `:224-247` `requeue_stuck_setup_jobs`). The other `SKIP LOCKED` sites in `jobs_retirement.py` (`:78`, `:191`) are `ClaimAnalysisJob`, not setup jobs — they do not bear on this question either way.

No path holds the mutex and then waits on a setup-job row **today**, so there is no AB-BA cycle in either order. Note that C2 itself creates the first mutex→setup-job-row edge; Task 4 bounds that wait rather than leaving it to an assumption about who holds the row.

**The rule is still correct, for this reason instead:** a route that takes the setup-job row *before* blocking on the mutex holds that row for as long as it waits (up to `timeout_seconds`, default 10s). During that window the delete's retirement hook runs `SKIP LOCKED` and **skips the row**, so the setup job is not cancelled by the delete and stays non-terminal until the reconciliation tick catches it (up to `jobs_reconcile_tick_seconds`, default 60s — *not* `passes_reconcile_tick_seconds`, which is the sibling tick for LLM passes). With the mutex first, the route raises `WorkspaceDeleted` before it ever touches the job row, the hook finds the row free, and cancellation is immediate. Task 4's gate asserts exactly this, **while the interference is still in force** — it does not assert a deadlock.

Task 6 corrects the spec so the next reader does not re-derive the wrong justification.

**Project constraints:**

- **Worktree:** `.worktrees/c2-mutex-writers` (project-local, gitignored).
- **Acceptance is by mutation.** Every gate must be shown to fail when its fix is reverted. A gate you have not mutated is not done.
- **The mutation protocol is mandatory and has a hard requirement:** `cp` the production file to a `.bak`, apply the mutation, **`md5sum` the file and confirm the hash CHANGED before running any test**, run the test, confirm it fails *for the right reason*, restore from the `.bak`, confirm the hash is back and the test is green. A mutation whose anchor did not match silently no-ops, the test "passes", and you conclude a false thing. This happened twice on Slice C1.
- **Mutate the seam, not just the unit.** Slice C1's headline capability passed 16 unit mutations and was still unwired — the gap was that nothing proved the *callers* used it. For every route here, the mutation that matters is "remove the mutex call from *this route*", not "break the helper".
- **A mutation that HANGS the suite is not a gate.** Bound every wait and `pytest.fail` on timeout.
- **Assert while the interference is in force.** Sampling after releasing a lock lets a later code path repair the thing under test.
- **Lint gate:** `.venv/bin/ruff check .` must pass. `ruff format --check` reports ~179 files repo-wide — that is pre-existing environment drift (it fails on master too). Do not try to fix it and do not report it as a defect.
- **Never run the whole suite during a task** — it takes ~10 minutes and is flaky under load (a testcontainers "connection refused" is infrastructure, not a code failure). Run the named files. The whole suite runs once, at the end.
- Commit messages: multi-line messages go in a file and use `git commit -F <file>`. Heredocs have failed on this project.

---

## File Structure

**Modified — production:**

| File | Responsibility after this slice |
|---|---|
| `src/call_manager/web/api/settings.py` | `update_settings`, `add_member`, `remove_member` each take the live mutex immediately after `require_owner` and map `CommitMutexTimeout` → 429 |
| `src/call_manager/web/api/rubric.py` | `update_rubric`, `activate`, `retry_setup` likewise; `activate`/`retry_setup` take the mutex **before** their `WorkspaceSetupJob … FOR UPDATE` |
| `src/call_manager/workspaces.py` | Exports only `soft_delete_workspace_db` and `scrub_workspace_bytes`; the composing wrapper is gone |

**Created — tests:**

| File | Responsibility |
|---|---|
| `tests/proposal_assist/test_api_writers_mutex_straddle.py` | The straddle/wedge harness plus all twelve route gates (six routes × {404-straddle, 429-wedged}) and Task 4's two lock-order gates |
| `tests/workspace_delete_helper.py` | Test-only composition of the two soft-delete halves, replacing the deleted production wrapper |

**Modified — tests:**

| File | Change |
|---|---|
| `tests/test_workspaces_service.py` | Six call sites migrate to the test helper; adds the severance guard |
| `tests/proposal_assist/test_turns_processor.py` | Two call sites migrate to the test helper |

**Modified — docs:**

| File | Change |
|---|---|
| `docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md` | §8 rationale and §12 gate 11 corrected |

---

## Task 0: Create the worktree

- [ ] **Step 1: Create the worktree and verify the branch**

```bash
cd /home/parlar_ai/dev/call-monitor
git worktree add .worktrees/c2-mutex-writers -b c2-mutex-writers master
git -C /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers rev-parse --abbrev-ref HEAD
```

Expected output: `c2-mutex-writers`

- [ ] **Step 2: Verify the environment**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
uv sync
.venv/bin/python -c "import call_manager; print('ok')"
```

Expected: `ok`. If `uv sync` creates a Python 3.14 venv, force 3.12 with `uv sync --python 3.12` — a worktree venv on the wrong minor has broken this project before.

---

## Task 1: The straddle/wedge harness, proven on `update_settings`

**Files:**
- Create: `tests/proposal_assist/test_api_writers_mutex_straddle.py`
- Modify: `src/call_manager/web/api/settings.py` (imports; `update_settings` at line 69)

**Interfaces:**
- Consumes: `acquire_commit_mutex_live`, `CommitMutexTimeout`, `BUSY_ERROR_MESSAGE` from `call_manager.proposal_assist.drafting.locking`; `require_owner` from `call_manager.proposal_assist.web._perms`.
- Produces: two harness functions later tasks call verbatim —
  - `straddle_delete(pg_session_factory, ws_id, send) -> httpx.Response` — runs `send` (a no-arg callable returning a Response) so it blocks on the commit mutex, commits a deletion underneath it, returns the response.
  - `wedge_mutex(pg_session_factory, ws_id, send) -> httpx.Response` — holds the mutex for the whole call and returns the response.
  - `patch_route_mutex_timeout(monkeypatch, module, seconds=1)` — repoints a route module's `acquire_commit_mutex_live` at a short-timeout partial.

- [ ] **Step 1: Write the harness and the two failing gates**

Create `tests/proposal_assist/test_api_writers_mutex_straddle.py`:

```python
"""Spec §12 gate 13 — each of the six owner-write routes, straddled by a delete.

Two shapes per route:

  404-straddle: a holder takes the commit mutex; the route queues behind it; the
    holder stamps soft_deleted_at and COMMITS, releasing the mutex; the route
    wins it, re-reads, and must return 404 having written NOTHING.

  429-wedged: a holder takes the mutex and never releases it; the route must
    return 429 with Retry-After: 2, having written NOTHING.

The holder stamps soft_deleted_at DIRECTLY rather than calling the delete route.
The route would take this same mutex on the same thread and self-deadlock. Do not
"simplify" that into a hang.

Blocking is detected by polling pg_locks, not by sleeping: a sleep races on a
loaded machine and turns a real gate into a flaky one.
"""

from __future__ import annotations

import functools
import threading
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from call_manager.db.models import Workspace
from call_manager.proposal_assist.drafting.locking import acquire_commit_mutex, commit_mutex_key
from tests.proposal_assist.conftest import make_workspace


def _advisory_waiters(session_factory, ws_id) -> int:
    """Count ungranted advisory locks ON THIS WORKSPACE'S commit mutex.

    Keyed, not a bare count. The precedent this copies
    (tests/proposal_assist/test_retire_mutex_straddle.py) counts every ungranted
    advisory lock in the database and justifies it with "the test DB is otherwise
    idle" — but our client fixture starts the real in-process APScheduler, whose
    ticks take advisory locks of their own. An unrelated waiter would satisfy an
    unkeyed probe early and let the job-first mutation pass, quietly destroying
    the gate this file exists for.

    pg_advisory_xact_lock(bigint) splits the key: classid is the high 32 bits,
    objid the low 32, both as int4.
    """
    key = commit_mutex_key(ws_id)
    with session_factory() as s:
        return s.scalar(
            text(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND NOT granted "
                "AND classid = :classid AND objid = :objid"
            ),
            {"classid": (key >> 32) & 0xFFFFFFFF, "objid": key & 0xFFFFFFFF},
        )


def _wait_until_blocked(session_factory, ws_id, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _advisory_waiters(session_factory, ws_id) >= 1:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"no route blocked on {ws_id}'s commit mutex within {timeout}s — the route "
        "is not participating in the mutex at all"
    )


def test_the_blocking_probe_actually_observes_a_lock_wait(pg_session_factory, current_user):
    """The probe is load-bearing for every gate in this file: if it returned 0
    forever, _wait_until_blocked would raise and every straddle would look like a
    routing failure; if it returned >=1 spuriously, the lock-order gates would
    sample too early and pass under the forbidden order. Prove it does neither.

    Ported from tests/proposal_assist/test_retire_mutex_straddle.py, which carries
    the same self-test for the same reason."""
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = ws.id

    # Nobody is waiting yet.
    assert _advisory_waiters(pg_session_factory, ws_id) == 0

    holding = threading.Event()
    release = threading.Event()
    waiting = threading.Event()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _waiter():
        holding.wait(timeout=10)
        with pg_session_factory() as s:
            waiting.set()
            acquire_commit_mutex(s, ws_id)  # queues behind the holder
            s.rollback()

    threads = [threading.Thread(target=_holder), threading.Thread(target=_waiter)]
    for t in threads:
        t.start()
    try:
        assert holding.wait(timeout=5)
        assert waiting.wait(timeout=5)
        _wait_until_blocked(pg_session_factory, ws_id, timeout=10)  # must not raise
    finally:
        release.set()
        for t in threads:
            t.join(timeout=30)

    # ...and it goes back to zero once everyone is done.
    assert _advisory_waiters(pg_session_factory, ws_id) == 0


def patch_route_mutex_timeout(monkeypatch, module, seconds: int = 1) -> None:
    """Pin a short mutex timeout for one route module. We are exercising the
    route's error mapping, not the production timeout value, and the 10s default
    would stall the suite."""
    monkeypatch.setattr(
        module,
        "acquire_commit_mutex_live",
        functools.partial(module.acquire_commit_mutex_live, timeout_seconds=seconds),
    )


def straddle_delete(pg_session_factory, ws_id, send):
    """Run `send` so it blocks on the commit mutex, then commit a deletion
    underneath it. Returns whatever `send` returned."""
    holding = threading.Event()
    out: dict = {}

    def _sender():
        holding.wait(timeout=10)
        out["resp"] = send()

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            _wait_until_blocked(pg_session_factory, ws_id, timeout=10)
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()  # releases the mutex; the route wakes up next

    threads = [threading.Thread(target=_holder), threading.Thread(target=_sender)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    if "resp" not in out:
        pytest.fail("the route never returned — it is wedged, not merely slow")
    return out["resp"]


def wedge_mutex(pg_session_factory, ws_id, send):
    """Hold the commit mutex for the whole of `send`. Returns what it returned."""
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
        assert holding.wait(timeout=5), "the holder never acquired the mutex"
        return send()
    finally:
        release.set()
        t.join(timeout=30)


def test_update_settings_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "original title"
        s.commit()
        ws_id = ws.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.patch(
            f"/api/workspaces/{ws_id}/settings",
            json={
                "title": "edited by a blocked writer",
                "status": "drafting",
                "deadline": None,
                "citation_style": "vancouver",
            },
        ),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).title == "original title"


def test_update_settings_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.settings as settings_api

    patch_route_mutex_timeout(monkeypatch, settings_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.title = "original title"
        s.commit()
        ws_id = ws.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.patch(
            f"/api/workspaces/{ws_id}/settings",
            json={
                "title": "edited under a wedged mutex",
                "status": "drafting",
                "deadline": None,
                "citation_style": "vancouver",
            },
        ),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).title == "original title"
```

**Before running:** confirm the fixture names and the valid enum values you need. Run these and use what they print:

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
grep -n "client_authed_owner\|current_user\|pg_session_factory" tests/conftest.py tests/proposal_assist/conftest.py | head -20
.venv/bin/python -c "
from call_manager.proposal_assist.web.workspace_meta import _WORKSPACE_STATUSES
from call_manager.proposal_assist.citations.bibliography import CITATION_STYLES
print('statuses:', _WORKSPACE_STATUSES); print('styles:', CITATION_STYLES)"
```

`drafting` and `vancouver` are confirmed real members (they are the literals `tests/test_api_settings.py:120` already uses) — the command above is a guard against drift, not an open question. If `client_authed_owner` lives in a different conftest than `current_user`, import accordingly.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -x -q
```

Expected: both FAIL. The straddle returns **200** (the route has no mutex, so it never blocks — `_wait_until_blocked` raises `AssertionError: no route blocked on the advisory lock within 10s` inside the holder thread, and the sender's response is a 200 written against a workspace that is about to be deleted). The wedge test returns **200** for the same reason. Either failure mode is correct at this step; both prove the route does not participate in the mutex.

- [ ] **Step 3: Bring `update_settings` under the mutex**

In `src/call_manager/web/api/settings.py`, extend the import block (after the existing `from call_manager.proposal_assist.collab.permissions import signal_from_request`):

```python
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
    acquire_commit_mutex_live,
)
```

Replace `update_settings` (currently lines 69-90) in its entirety with:

```python
@router.patch("/workspaces/{workspace_id}/settings", response_model=SettingsOut)
def update_settings(
    workspace_id: uuid.UUID,
    body: SettingsUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SettingsOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Body validation stays AFTER require_owner — hoisting it above would let
        # a non-owner with a malformed body get 400 instead of 403/404, leaking
        # that the request shape was wrong to someone with no access. It stays
        # BEFORE the mutex because it takes no lock: a malformed body must not
        # queue behind the commit mutex.
        if body.status not in _WORKSPACE_STATUSES:
            raise HTTPException(status_code=400, detail="invalid status")
        if body.citation_style not in _CITATION_STYLES:
            raise HTTPException(status_code=400, detail="invalid citation_style")
        title = body.title.strip()
        if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
            raise HTTPException(status_code=400, detail="invalid title")
        deadline = _parse_deadline_form(body.deadline)  # HTTPException(400) on malformed
        # Slice C2 lock-order rule: the live mutex is taken immediately after
        # require_owner and its lock-free validation, and before any other lock.
        # soft_delete_workspace_db holds
        # this same mutex for its whole transaction, so a deletion committing
        # while we blocked here is observed the instant we win the lock —
        # WorkspaceDeleted propagates to main.py's handler (404) and we have
        # mutated nothing.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        ws.title = title
        ws.status = body.status
        ws.deadline = deadline
        ws.citation_style = body.citation_style
        session.commit()
        return _settings_out(ws)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py tests/test_api_settings.py -q
```

Expected: PASS, including every pre-existing test in `tests/test_api_settings.py`.

- [ ] **Step 5: Prove both gates discriminate**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
F=src/call_manager/web/api/settings.py
cp $F /tmp/settings.bak
md5sum $F
# MUTATION: remove the mutex from this route only.
.venv/bin/python - <<'PY'
import re, pathlib
p = pathlib.Path("src/call_manager/web/api/settings.py")
s = p.read_text()
old = """        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        ws.title = title"""
new = "        ws.title = title"
assert old in s, "ANCHOR DID NOT MATCH — fix the anchor before running tests"
p.write_text(s.replace(old, new, 1))
PY
md5sum $F   # MUST differ from the hash above. If it does not, STOP.
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q
cp /tmp/settings.bak $F
md5sum $F   # back to the original
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q
```

Expected: with the mutation, **both** `update_settings` gates fail. After restoring, both pass.

- [ ] **Step 5b: Add the authorization-precedence gate**

Moving validation inside the session block changes an observable status code, so pin it. Append to `tests/test_api_settings.py` (which already has `_setup`, `build_app` and `authed_client`):

```python
def test_settings_patch_authorization_precedes_validation(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """A non-owner sending a MALFORMED body must get 403, not 400.

    Slice C2 moved this route's body validation inside the session block, after
    require_owner. Validating first would answer someone with no access at all
    that their body shape was wrong — an authorization decision leaking through
    a validation error."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory)
    r = authed_client(app, user_id=member_id).patch(
        f"/api/workspaces/{ws_id}/settings",
        # Invalid on three counts: bad status, bad citation_style, empty title.
        json={"title": "", "status": "nonsense", "deadline": None, "citation_style": "nope"},
    )
    assert r.status_code == 403, (
        f"expected 403 (authorization decides first), got {r.status_code}: {r.text}"
    )
```

Prove it discriminates: hoist the four validation statements back above the `with request.app.state.session_factory()` line (md5 must change), run this test — it must fail with 400 — then restore.

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/test_api_settings.py -q
```

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/ruff check .
git add tests/proposal_assist/test_api_writers_mutex_straddle.py tests/test_api_settings.py src/call_manager/web/api/settings.py
git commit -m "feat(api): update_settings under the live commit mutex, with the straddle harness"
```

---

## Task 2: `add_member` and `remove_member` under the mutex

**Files:**
- Modify: `src/call_manager/web/api/settings.py` (`add_member` at line 97, `remove_member` at line 135)
- Test: `tests/proposal_assist/test_api_writers_mutex_straddle.py`

**Interfaces:**
- Consumes: `straddle_delete`, `wedge_mutex`, `patch_route_mutex_timeout` from Task 1 (same file). The `locking` imports Task 1 added to `settings.py` are already present — do not re-add them.

- [ ] **Step 1: Write the four failing gates**

First extend the test file's imports — Task 1 deliberately imported only what Task 1 used, because `ruff check .` selects `F` and an unused import is F401 at the commit gate. Change the two lines to:

```python
from call_manager.db.models import Workspace, WorkspaceViewer
from tests.proposal_assist.conftest import make_user, make_workspace
```

Then append to `tests/proposal_assist/test_api_writers_mutex_straddle.py`:

```python
def test_add_member_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-add-straddle@ki.se")
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(
            f"/api/workspaces/{ws_id}/members", json={"email": "c2-add-straddle@ki.se"}
        ),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is None
        ), "a blocked add_member inserted a WorkspaceViewer for a deleted workspace"


def test_add_member_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.settings as settings_api

    patch_route_mutex_timeout(monkeypatch, settings_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-add-wedged@ki.se")
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(
            f"/api/workspaces/{ws_id}/members", json={"email": "c2-add-wedged@ki.se"}
        ),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is None
        )


def test_remove_member_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-rm-straddle@ki.se")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.delete(f"/api/workspaces/{ws_id}/members/{target_id}"),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is not None
        ), "a blocked remove_member deleted a WorkspaceViewer for a deleted workspace"


def test_remove_member_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.settings as settings_api

    patch_route_mutex_timeout(monkeypatch, settings_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        target = make_user(s, email="c2-rm-wedged@ki.se")
        s.flush()
        s.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
        s.commit()
        ws_id, target_id = ws.id, target.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.delete(f"/api/workspaces/{ws_id}/members/{target_id}"),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert (
            s.query(WorkspaceViewer).filter_by(workspace_id=ws_id, user_id=target_id).first()
            is not None
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k "add_member or remove_member"
```

Expected: all four FAIL — the routes take no mutex, so nothing blocks and the writes land.

- [ ] **Step 3: Bring both routes under the mutex**

In `src/call_manager/web/api/settings.py`, in `add_member`, insert immediately after the `ws = require_owner(...)` line and before `target = session.scalar(...)`:

```python
        # Slice C2 lock-order rule: the live mutex immediately after
        # require_owner, before any other lock. WorkspaceDeleted -> main.py -> 404.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
```

In `remove_member`, insert the identical block immediately after its `ws = require_owner(...)` line and before `target = session.get(User, user_id)`:

```python
        # Slice C2 lock-order rule: the live mutex immediately after
        # require_owner, before any other lock. WorkspaceDeleted -> main.py -> 404.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
```

Note on `remove_member`: `signal_from_request(...)` at the end of the function stays **outside** the `with` block, exactly where it is. It runs after the transaction commits and takes no lock; moving it under the mutex would hold the mutex across an in-process fan-out for no benefit.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py tests/test_api_settings.py -q
```

Expected: PASS, including every pre-existing test in `tests/test_api_settings.py`.

- [ ] **Step 5: Prove the gates discriminate — one mutation per route**

```bash
cd /home/parlar_ai/dev/call-monitor/.venv 2>/dev/null; cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
F=src/call_manager/web/api/settings.py
cp $F /tmp/settings2.bak
md5sum $F
# MUTATION 1: remove the mutex from add_member only.
.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("src/call_manager/web/api/settings.py")
s = p.read_text()
block = """        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        target = session.scalar(select(User).where(User.email == body.email))"""
repl = "        target = session.scalar(select(User).where(User.email == body.email))"
assert block in s, "ANCHOR DID NOT MATCH — fix the anchor before running tests"
p.write_text(s.replace(block, repl, 1))
PY
md5sum $F   # MUST differ. If not, STOP.
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k add_member
cp /tmp/settings2.bak $F

# MUTATION 2: remove the mutex from remove_member only.
.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("src/call_manager/web/api/settings.py")
s = p.read_text()
block = """        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        target = session.get(User, user_id)  # resolve email for the audit payload"""
repl = "        target = session.get(User, user_id)  # resolve email for the audit payload"
assert block in s, "ANCHOR DID NOT MATCH — fix the anchor before running tests"
p.write_text(s.replace(block, repl, 1))
PY
md5sum $F   # MUST differ. If not, STOP.
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k remove_member
cp /tmp/settings2.bak $F
md5sum $F
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q
```

Expected: mutation 1 fails **only** the two `add_member` gates; mutation 2 fails **only** the two `remove_member` gates; after restoring, all eight pass.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/ruff check .
git add tests/proposal_assist/test_api_writers_mutex_straddle.py src/call_manager/web/api/settings.py
git commit -m "feat(api): add_member and remove_member under the live commit mutex"
```

---

## Task 3: `update_rubric` under the mutex

**Files:**
- Modify: `src/call_manager/web/api/rubric.py` (imports; `update_rubric` at line 148)
- Test: `tests/proposal_assist/test_api_writers_mutex_straddle.py`

**Interfaces:**
- Consumes: `straddle_delete`, `wedge_mutex`, `patch_route_mutex_timeout` from Task 1.
- Produces: the `locking` import block in `rubric.py` that Task 4 reuses.

- [ ] **Step 1: Write the two failing gates**

Append to `tests/proposal_assist/test_api_writers_mutex_straddle.py`:

```python
_RUBRIC_BODY = {
    "criteria": [
        {"name": "Impact", "description": "how much it matters", "weight": 1.0, "scale_max": 5}
    ],
    "funder_notes": "edited by a blocked writer",
}


def test_update_rubric_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.rubric_funder_notes = "original notes"
        s.commit()
        ws_id = ws.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.put(f"/api/workspaces/{ws_id}/rubric", json=_RUBRIC_BODY),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).rubric_funder_notes == "original notes"


def test_update_rubric_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.rubric as rubric_api

    patch_route_mutex_timeout(monkeypatch, rubric_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.rubric_funder_notes = "original notes"
        s.commit()
        ws_id = ws.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.put(f"/api/workspaces/{ws_id}/rubric", json=_RUBRIC_BODY),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(Workspace, ws_id).rubric_funder_notes == "original notes"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k update_rubric
```

Expected: both FAIL — the route takes no mutex.

- [ ] **Step 3: Bring `update_rubric` under the mutex**

In `src/call_manager/web/api/rubric.py`, extend the import block (after `from call_manager.proposal_assist.collab.permissions import signal_from_request`):

```python
from call_manager.proposal_assist.drafting.locking import (
    BUSY_ERROR_MESSAGE,
    CommitMutexTimeout,
    acquire_commit_mutex_live,
)
```

Replace `update_rubric` (currently lines 148-160) in its entirety with:

```python
@router.put("/workspaces/{workspace_id}/rubric", response_model=RubricOut)
def update_rubric(
    workspace_id: uuid.UUID,
    body: RubricUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Semantic validation runs AFTER require_owner. On master it ran before
        # the session opened, so a non-owner posting a malformed rubric got 400 —
        # telling someone with no access that their body shape was wrong.
        # Authorization decides first. It stays BEFORE the mutex because it takes
        # no lock: a malformed body must not queue behind the commit mutex.
        criteria = _validate_criteria(body.criteria)
        # Slice C2 lock-order rule: the live mutex immediately after
        # require_owner and its lock-free validation, before any other lock.
        # WorkspaceDeleted -> main.py -> 404.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
        session.commit()
        return _rubric_out(ws)
```

Also delete the stale note at the top of the file (lines 7-9), which describes a task that shipped long ago and now misleads:

```python
# NOTE: Task 2 appends the write endpoints (activate, PUT rubric) + _apply_rubric
# and EXTENDS the imports below (HTTPException, WorkspaceEvent, datetime/UTC).
# They are intentionally absent here so Task 1's `ruff check` stays F401-clean.
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py tests/test_api_rubric.py -q
```

Expected: PASS, including every pre-existing test in `tests/test_api_rubric.py`.

- [ ] **Step 5: Prove both gates discriminate**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
F=src/call_manager/web/api/rubric.py
cp $F /tmp/rubric.bak
md5sum $F
.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("src/call_manager/web/api/rubric.py")
s = p.read_text()
block = """        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
        session.commit()
        return _rubric_out(ws)"""
repl = """        _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
        session.commit()
        return _rubric_out(ws)"""
assert block in s, "ANCHOR DID NOT MATCH — fix the anchor before running tests"
p.write_text(s.replace(block, repl, 1))
PY
md5sum $F   # MUST differ. If not, STOP.
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k update_rubric
cp /tmp/rubric.bak $F
md5sum $F
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k update_rubric
```

Expected: with the mutation, both `update_rubric` gates fail. After restoring, both pass.

- [ ] **Step 5b: Add the authorization-precedence gate**

`update_rubric` called `_validate_criteria` before the session even opened on master, so a non-owner with an invalid rubric got 400. Task 3 moved it after `require_owner`; pin that. Append to `tests/test_api_rubric.py`:

```python
def test_put_rubric_authorization_precedes_validation(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """A non-owner sending an INVALID rubric must get 403, not 400. Validating
    first tells someone with no access that their body shape was wrong."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory, phase="active")
    r = authed_client(app, user_id=member_id).put(
        f"/api/workspaces/{ws_id}/rubric",
        json={"criteria": [], "funder_notes": None},  # empty criteria -> 400 if validated first
    )
    assert r.status_code == 403, (
        f"expected 403 (authorization decides first), got {r.status_code}: {r.text}"
    )
```

Prove it discriminates: move `criteria = _validate_criteria(body.criteria)` back above the `with request.app.state.session_factory()` line (md5 must change), run it — must fail with 400 — restore.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/ruff check .
git add tests/proposal_assist/test_api_writers_mutex_straddle.py tests/test_api_rubric.py src/call_manager/web/api/rubric.py
git commit -m "feat(api): update_rubric under the live commit mutex"
```

---

## Task 4: `activate` and `retry_setup` — mutex BEFORE the setup-job lock

**Files:**
- Modify: `src/call_manager/proposal_assist/row_locks.py` (add `SETUP_JOB_LOCK_TIMEOUT_SECONDS`)
- Modify: `src/call_manager/web/api/rubric.py` (`retry_setup` at line 168, `activate` at line 206)
- Test: `tests/proposal_assist/test_api_writers_mutex_straddle.py`, `tests/test_api_rubric.py`

**Interfaces:**
- Consumes: `straddle_delete`, `wedge_mutex`, `patch_route_mutex_timeout`, `_wait_until_blocked` from Task 1; the `locking` imports Task 3 added to `rubric.py`.

**Read the "Corrected rationale" in Global Constraints before starting.** The spec claims these two routes deadlock against the delete if the setup-job lock is taken first. **That deadlock is not reachable** — the delete transaction never touches `WorkspaceSetupJob`, and every retirement path uses `SKIP LOCKED`. A test asserting "no deadlock" would pass in both orders and prove nothing. The real, testable consequence of the wrong order is that the route **holds the setup-job row while blocked on the mutex**, so the delete's retirement hook skips it and the job is left non-terminal until the reconciliation tick. Step 1's third and fourth tests assert exactly that, while the route is still blocked.

- [ ] **Step 1: Write the six failing gates**

First extend the test file's imports again (same F401 reason as Task 2). Add the stdlib import in its own block and extend the models import:

```python
import uuid as uuidlib
```

```python
from call_manager.db.models import Workspace, WorkspaceSetupJob, WorkspaceViewer
```

Then append to `tests/proposal_assist/test_api_writers_mutex_straddle.py`:

```python
def _setup_job(session, ws_id, status: str) -> uuidlib.UUID:
    """Create a WorkspaceSetupJob in `status` for `ws_id` and return its id."""
    job = WorkspaceSetupJob(workspace_id=ws_id, status=status)
    session.add(job)
    session.flush()
    return job.id


def test_retry_setup_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job_id = _setup_job(s, ws.id, "failed")
        s.commit()
        ws_id = ws.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/setup/retry"),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "failed", (
            "a blocked retry_setup re-queued a setup job for a deleted workspace"
        )


def test_retry_setup_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.rubric as rubric_api

    patch_route_mutex_timeout(monkeypatch, rubric_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job_id = _setup_job(s, ws.id, "failed")
        s.commit()
        ws_id = ws.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/setup/retry"),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "failed"


def test_activate_straddled_by_a_delete_returns_404_and_writes_nothing(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    resp = straddle_delete(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY),
    )

    assert resp.status_code == 404, f"expected 404, got {resp.status_code}: {resp.text}"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
        assert s.get(Workspace, ws_id).current_phase == "setup", (
            "a blocked activate transitioned a deleted workspace to active"
        )


def test_activate_returns_429_when_the_commit_mutex_is_wedged(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    import call_manager.web.api.rubric as rubric_api

    patch_route_mutex_timeout(monkeypatch, rubric_api, seconds=1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    resp = wedge_mutex(
        pg_session_factory,
        ws_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY),
    )

    assert resp.status_code == 429, f"expected 429, got {resp.status_code}: {resp.text}"
    assert resp.headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
        assert s.get(Workspace, ws_id).current_phase == "setup"


def _assert_hook_can_cancel_while_route_is_blocked(pg_session_factory, ws_id, job_id, send):
    """THE lock-order gate.

    A holder takes and KEEPS the commit mutex. The route queues behind it. While
    the route is still blocked, we run the retirement hook's setup-job cancel —
    which is FOR UPDATE SKIP LOCKED — in a third session.

    Mutex-first: the route holds no row lock while it waits, so the cancel takes
    the row and terminalizes it.
    Job-first:   the route holds the setup-job row FOR UPDATE for the whole wait,
                 so the cancel SKIPS it and the job stays non-terminal until the
                 reconciliation tick (up to 60s later).

    The assertion runs WHILE the route is still blocked. Sampling after the mutex
    is released would let the route's own rollback free the row and repair the
    very thing under test.
    """
    from call_manager.proposal_assist.jobs_retirement import cancel_setup_jobs_for_workspace

    holding = threading.Event()
    release = threading.Event()
    out: dict = {}
    cancelled = None  # assigned in the try; initialised so a failure there does
    # not turn into a NameError that hides the real error below.

    def _holder():
        with pg_session_factory() as s:
            acquire_commit_mutex(s, ws_id)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _sender():
        holding.wait(timeout=10)
        out["resp"] = send()

    holder = threading.Thread(target=_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never acquired the mutex"
        _wait_until_blocked(pg_session_factory, ws_id, timeout=10)
        # Stamp the deletion so the scenario is the real one — a delete that has
        # committed while the route waits. cancel_setup_jobs_for_workspace itself
        # has NO soft_deleted_at predicate (only reconcile_cancelled_jobs does),
        # so this is not what makes the cancel fire; it is what makes the test
        # describe a situation that can actually happen.
        with pg_session_factory() as s:
            s.get(Workspace, ws_id).soft_deleted_at = datetime.now(UTC)
            s.commit()
        with pg_session_factory() as s:
            cancelled = cancel_setup_jobs_for_workspace(s, ws_id)
            s.commit()
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    assert cancelled == 1, (
        "the retirement hook could not cancel the setup job while the route was "
        "blocked on the mutex — the route is holding the job row, which means it "
        "took the row lock BEFORE the mutex. SKIP LOCKED skipped it, so the delete "
        "leaves the job non-terminal until the reconciliation tick."
    )
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "cancelled"


def test_retry_setup_does_not_hold_the_setup_job_row_while_it_waits_for_the_mutex(
    client_authed_owner, current_user, pg_session_factory
):
    # SEED 'queued', NOT 'failed'. cancel_setup_jobs_for_workspace filters on
    # _CANCELLABLE = ("queued", "running", "waiting_for_rubric_review")
    # (jobs_retirement.py:55) BEFORE it ever consults a lock, and 'failed' is
    # terminal — so with a 'failed' row the cancel returns 0 no matter who holds
    # the row, and this gate would be red in BOTH lock orders. That makes the
    # Step 5 mutation vacuous: mutated and unmutated look identical.
    #
    # The interference we sample is unchanged: retry_setup takes its row lock
    # UNCONDITIONALLY at rubric.py:181-186, before it inspects status at :187.
    # This gate never asserts the route's own status code, so the 409 a 'queued'
    # job would earn is irrelevant here. The straddle and wedge gates above keep
    # 'failed' — they DO need the happy path.
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job_id = _setup_job(s, ws.id, "queued")
        s.commit()
        ws_id = ws.id

    _assert_hook_can_cancel_while_route_is_blocked(
        pg_session_factory,
        ws_id,
        job_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/setup/retry"),
    )


def test_activate_does_not_hold_the_setup_job_row_while_it_waits_for_the_mutex(
    client_authed_owner, current_user, pg_session_factory
):
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    _assert_hook_can_cancel_while_route_is_blocked(
        pg_session_factory,
        ws_id,
        job_id,
        lambda: client_authed_owner.post(f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY),
    )


def test_retry_setup_gives_up_on_a_held_setup_job_row_instead_of_pinning_the_mutex(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """The route takes the commit mutex and THEN the setup-job row. That second
    wait must be bounded: acquire_commit_mutex restores lock_timeout = DEFAULT,
    so an unbounded wait there holds the mutex indefinitely and a concurrent
    DELETE 429s behind it after burning its whole budget.

    Asserted as 'the request came back', not as elapsed time: without the bound
    the route does not return at all while we hold the row, so a thread that is
    still alive after the join IS the failure. Slice C1 learned that an elapsed-
    time assertion can pass for the opposite reason."""
    # Patch the name in the ROUTE module, not in row_locks. rubric.py does
    # `from ...row_locks import SETUP_JOB_LOCK_TIMEOUT_SECONDS`, which binds the
    # value into rubric's namespace at import time — patching row_locks would
    # silently no-op and this test would pass at the production 2s instead of 1s,
    # proving nothing about the bound.
    import call_manager.web.api.rubric as rubric_api

    monkeypatch.setattr(rubric_api, "SETUP_JOB_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        job_id = _setup_job(s, ws.id, "failed")
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()
    out: dict = {}

    def _row_holder():
        """Hold the setup-job row FOR UPDATE — and take NO mutex, so the route
        reaches its job lock and waits there rather than at the mutex."""
        with pg_session_factory() as s:
            s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _sender():
        holding.wait(timeout=10)
        out["resp"] = client_authed_owner.post(f"/api/workspaces/{ws_id}/setup/retry")

    holder = threading.Thread(target=_row_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never took the setup-job row"
        sender.join(timeout=15)
        assert not sender.is_alive(), (
            "retry_setup was still waiting on the setup-job row after 15s while "
            "holding the commit mutex — the row wait is unbounded"
        )
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    assert out["resp"].status_code == 429
    assert out["resp"].headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "failed"
```

**Sanity check on the patch target, once:** run this test with the patch pointed at `row_locks` instead of `rubric_api` and confirm it takes ~2s rather than ~1s. That is the difference between a bound you proved and a bound you assumed. Then put it back.

Then add the same gate for `activate` — the bound is on both routes, so it must be proved on both. Append to the same file:

```python
def test_activate_gives_up_on_a_held_setup_job_row_instead_of_pinning_the_mutex(
    client_authed_owner, current_user, pg_session_factory, monkeypatch
):
    """activate's half of the bounded setup-row wait. Same hazard, same shape as
    the retry_setup gate: the route holds the commit mutex across this row wait,
    so an unbounded wait pins the mutex and a concurrent DELETE 429s behind it
    after burning its whole budget."""
    import call_manager.web.api.rubric as rubric_api

    monkeypatch.setattr(rubric_api, "SETUP_JOB_LOCK_TIMEOUT_SECONDS", 1)

    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        ws.current_phase = "setup"
        s.flush()
        job_id = _setup_job(s, ws.id, "waiting_for_rubric_review")
        s.commit()
        ws_id = ws.id

    holding = threading.Event()
    release = threading.Event()
    out: dict = {}

    def _row_holder():
        with pg_session_factory() as s:
            s.get(WorkspaceSetupJob, job_id, with_for_update=True)
            holding.set()
            release.wait(timeout=30)
            s.rollback()

    def _sender():
        holding.wait(timeout=10)
        out["resp"] = client_authed_owner.post(
            f"/api/workspaces/{ws_id}/activate", json=_RUBRIC_BODY
        )

    holder = threading.Thread(target=_row_holder)
    sender = threading.Thread(target=_sender)
    holder.start()
    sender.start()
    try:
        assert holding.wait(timeout=5), "the holder never took the setup-job row"
        sender.join(timeout=15)
        assert not sender.is_alive(), (
            "activate was still waiting on the setup-job row after 15s while holding "
            "the commit mutex — the row wait is unbounded"
        )
    finally:
        release.set()
        holder.join(timeout=30)
        sender.join(timeout=30)

    assert out["resp"].status_code == 429
    assert out["resp"].headers.get("Retry-After") == "2"
    with pg_session_factory() as s:
        assert s.get(WorkspaceSetupJob, job_id).status == "waiting_for_rubric_review"
        assert s.get(Workspace, ws_id).current_phase == "setup"
```

And `activate`'s authorization-precedence gate, in `tests/test_api_rubric.py` alongside Task 3's:

```python
def test_activate_authorization_precedes_validation(
    monkeypatch, pg_url, tmp_path, pg_session_factory
):
    """A non-owner sending an INVALID rubric to activate must get 403, not 400."""
    app = build_app(monkeypatch, pg_url, tmp_path, pg_session_factory)
    ws_id, _, member_id, _ = _setup(pg_session_factory, phase="setup")
    r = authed_client(app, user_id=member_id).post(
        f"/api/workspaces/{ws_id}/activate",
        json={"criteria": [], "funder_notes": None},  # empty criteria -> 400 if validated first
    )
    assert r.status_code == 403, (
        f"expected 403 (authorization decides first), got {r.status_code}: {r.text}"
    )
```

Check `_setup`'s signature in `tests/test_api_rubric.py` before using `phase="setup"` — Task 3's gate uses `phase="active"`, and the existing `test_activate_member_403` (line ~243) shows which phase activate's tests use. Match it.

**Before running:** confirm `WorkspaceSetupJob` has no other NOT NULL columns without defaults. `ReviewRun.draft_sha` bit Slice C1 exactly this way.

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -c "
from call_manager.db.models import WorkspaceSetupJob as J
for c in J.__table__.columns:
    if not c.nullable and c.default is None and c.server_default is None and not c.primary_key:
        print('NEEDS A VALUE:', c.name, c.type)"
```

Add any column it prints to `_setup_job`'s constructor.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k "retry_setup or activate"
```

Expected: all six FAIL, and the two kinds fail for **different reasons** — check both, because a red test for the wrong reason proves nothing:

- The four straddle/wedge gates fail because neither route takes the mutex, so nothing ever blocks. `straddle_delete` raises `AssertionError: no route blocked on the advisory lock within 10s` from inside the holder thread, and the wedge gates get a 200.
- The two lock-order gates fail the same way — `_wait_until_blocked` times out. Today neither route touches the mutex at all, so it never waits with the job row held. **This is a weak red:** it proves the route does not block, not that the order is wrong. The order is proven in Step 5, where the mutation keeps the mutex and moves only its position. Do not treat Step 2's red as the gate's discrimination evidence.

- [ ] **Step 3: Bring both routes under the mutex, before their job lock**

First add the timeout constant. In `src/call_manager/proposal_assist/row_locks.py`, after `PASS_PREFIX_LOCK_TIMEOUT_SECONDS = 2`:

```python
# The two setup-job routes (activate, retry_setup) take the commit mutex and then
# the workspace's WorkspaceSetupJob row. acquire_commit_mutex restores
# lock_timeout = DEFAULT on success, so that row wait is otherwise unbounded —
# and it is taken while HOLDING the mutex, so an unbounded wait there blocks the
# owner's DELETE for exactly that long and 429s every other mutex-participating
# writer behind it.
#
# Why 2 seconds: same derivation as PASS_PREFIX_LOCK_TIMEOUT_SECONDS above — the
# DELETE route's asserted budget is < 6.0s. Losing a race is cheap here: the
# route has mutated nothing and the caller gets the house 429 with Retry-After,
# which is the same answer a wedged mutex already gives them.
SETUP_JOB_LOCK_TIMEOUT_SECONDS = 2
```

Then extend `src/call_manager/web/api/rubric.py`'s imports (the `locking` block Task 3 added):

```python
from call_manager.proposal_assist.row_locks import (
    SETUP_JOB_LOCK_TIMEOUT_SECONDS,
    RowLockTimeout,
    bounded_lock_waits,
)
```

Replace `retry_setup` (currently lines 168-203) in its entirety with:

```python
@router.post("/workspaces/{workspace_id}/setup/retry", response_model=SetupRetryOut)
def retry_setup(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SetupRetryOut:
    """Owner-only: re-queue a permanently-failed Phase-0 setup job.

    Resets status='queued', attempts=0, clears started_at/completed_at/last_error
    so the next scheduler tick re-claims it. Returns 404 if no failed job exists
    for this workspace; returns 409 if the job is already queued or running
    (not yet permanently failed)."""
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Slice C2 lock-order rule: the live mutex immediately after require_owner
        # and BEFORE the setup-job row lock below. Order matters here for a
        # concrete reason: a route that took the job row first would hold it for
        # the whole mutex wait, and the delete's retirement hook cancels setup
        # jobs with FOR UPDATE SKIP LOCKED — so it would skip this row and leave
        # the job non-terminal until the reconciliation tick. Taking the mutex
        # first means we raise WorkspaceDeleted (-> main.py -> 404) without ever
        # touching the row.
        #
        # This route also has its own resurrection shape, though a narrower one
        # than it looks: it cannot revive a job the delete cancelled, because it
        # refuses anything whose status is not exactly 'failed' (below) and the
        # delete writes 'cancelled'. What it CAN do is re-queue a job that was
        # already 'failed' when the workspace was deleted — 'failed' is terminal
        # and therefore not in the hook's _CANCELLABLE list, so the delete leaves
        # it alone. The under-mutex read is what stops that.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        # The job-row wait is BOUNDED. acquire_commit_mutex restores
        # lock_timeout = DEFAULT on success (locking.py:204), so without this the
        # route would hold the commit mutex while waiting indefinitely on a row
        # that _m3, _fail or requeue_stuck_setup_jobs may hold — and a concurrent
        # DELETE would 429 behind us after burning its whole 10s budget. That is
        # the unbounded-wait class Slice C1 closed for the four pass prefixes;
        # re-opening it here on the assumption "those holders are short" is
        # exactly the unwritten assumption that produced it the first time.
        try:
            with bounded_lock_waits(
                session,
                timeout_seconds=SETUP_JOB_LOCK_TIMEOUT_SECONDS,
                message="timed out waiting for the setup-job row",
            ):
                job = session.scalar(
                    select(WorkspaceSetupJob)
                    .where(WorkspaceSetupJob.workspace_id == ws.id)
                    .with_for_update()
                )
        except RowLockTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
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
        session.commit()
        return SetupRetryOut(status=job.status, attempts=job.attempts)
```

Replace `activate` (currently lines 206-237) in its entirety with:

```python
@router.post("/workspaces/{workspace_id}/activate", response_model=RubricOut)
def activate(
    workspace_id: uuid.UUID,
    body: RubricUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> RubricOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Semantic validation AFTER require_owner (see update_rubric's note): a
        # non-owner with a malformed rubric gets 403, not a 400 confirming the
        # body shape. Before the mutex, because it takes no lock.
        criteria = _validate_criteria(body.criteria)
        # Slice C2 lock-order rule: the live mutex immediately after require_owner
        # and BEFORE the setup-job row lock below. Taking the job row first would
        # hold it across the whole mutex wait, and the delete's retirement hook
        # cancels setup jobs with FOR UPDATE SKIP LOCKED — it would skip this row
        # and leave the job non-terminal until the reconciliation tick.
        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        # Bounded, for the same reason as retry_setup: acquire_commit_mutex
        # restores lock_timeout = DEFAULT, so an unbounded row wait here would
        # pin the commit mutex and make a concurrent DELETE 429 after its full
        # budget. See row_locks.py's module docstring.
        try:
            with bounded_lock_waits(
                session,
                timeout_seconds=SETUP_JOB_LOCK_TIMEOUT_SECONDS,
                message="timed out waiting for the setup-job row",
            ):
                job = session.scalar(
                    select(WorkspaceSetupJob)
                    .where(WorkspaceSetupJob.workspace_id == ws.id)
                    .with_for_update()
                )
        except RowLockTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        if job is None or job.status != "waiting_for_rubric_review":
            raise HTTPException(status_code=409, detail="workspace is not awaiting rubric review")
        _apply_rubric(session, ws, user, criteria, body.funder_notes or "")
        job.status = "done"
        job.completed_at = datetime.now(UTC)
        ws.current_phase = "active"
        session.add(
            WorkspaceEvent(
                workspace_id=ws.id, kind="workspace_activated", actor_id=user.id, payload={}
            )
        )
        session.commit()
        result = _rubric_out(ws)   # build INSIDE the block (session still open)
    # setup gates BOTH artifacts to owner-only (resolve_access), so activating
    # lifts the write-gate for the draft AND an unlocked spec — reconcile both.
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft", "spec"])
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py tests/test_api_rubric.py -q
```

Expected: PASS, all seventeen gates in the straddle file plus every pre-existing test in `tests/test_api_rubric.py`.

- [ ] **Step 5: Prove the gates discriminate — including the lock ORDER**

This is the important mutation of the whole slice: it swaps the order rather than removing the mutex, so it proves the *rule*, not merely the *presence*.

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
F=src/call_manager/web/api/rubric.py
cp $F /tmp/rubric4.bak
md5sum $F
# MUTATION: in retry_setup, take the setup-job row BEFORE the mutex (the order
# the rule forbids). The mutex still runs and the bound stays — only the ORDER
# changes. The two chunks are adjacent, so this swaps them by index rather than
# by reproducing the long comment block verbatim, which would be fragile.
.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("src/call_manager/web/api/rubric.py")
s = p.read_text()
MUTEX = """        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
"""
# retry_setup is the only site where the mutex is followed by this comment.
HEAD = MUTEX + "        # The job-row wait is BOUNDED."
TAIL = '        if job is None or job.status != "failed":'
assert s.count(HEAD) == 1, f"HEAD anchor not unique ({s.count(HEAD)}) — fix before running"
assert s.count(TAIL) == 1, f"TAIL anchor not unique ({s.count(TAIL)}) — fix before running"
i = s.index(HEAD)
mutex_end = i + len(MUTEX)
job_end = s.index(TAIL, mutex_end)
job_chunk = s[mutex_end:job_end]          # comments + try/bounded_lock_waits/except
assert "bounded_lock_waits" in job_chunk, "job chunk did not capture the bound"
p.write_text(s[:i] + job_chunk + MUTEX + s[job_end:])
PY
md5sum $F   # MUST differ. If not, STOP — do not run the tests.
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k "retry_setup"
cp /tmp/rubric4.bak $F
md5sum $F
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q
```

Expected: with the order swapped, `test_retry_setup_does_not_hold_the_setup_job_row_while_it_waits_for_the_mutex` FAILS with the `cancelled == 1` assertion (it will be 0). The two straddle/wedge gates for `retry_setup` may still pass — that is correct and is the point: they test the mutex's *presence*, this gate tests its *position*. After restoring, all seventeen pass.

Then repeat the same swap for `activate`, changing two lines of the script: `HEAD = MUTEX + "        # Bounded, for the same reason as retry_setup:"` and `TAIL = '        if job is None or job.status != "waiting_for_rubric_review":'`. Both `count(...) == 1` assertions must still hold. Confirm only `test_activate_does_not_hold_the_setup_job_row_while_it_waits_for_the_mutex` fails. Restore.

Finally, run the **removal** mutation on each of the two routes — and read this warning first.

**The mutex `try/except` block is now byte-identical in three places in `rubric.py`** (`update_rubric`, `retry_setup`, `activate`). `s.replace(block, "", 1)` therefore always hits `update_rubric`, whichever route you meant. The `md5sum` guard does not catch this: the hash *does* change, so the STOP check passes while you mutate the wrong function and then `-k`-select tests that were never affected. Every anchor must carry a **disambiguating tail line** unique to its route:

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
F=src/call_manager/web/api/rubric.py
cp $F /tmp/rubric5.bak
md5sum $F
.venv/bin/python - <<'PY'
import pathlib
p = pathlib.Path("src/call_manager/web/api/rubric.py")
s = p.read_text()
MUTEX = """        try:
            acquire_commit_mutex_live(session, ws.id)
        except CommitMutexTimeout as e:
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
"""
# The tail is what makes each anchor unique. retry_setup's comment block sits
# between the mutex and the bound; activate's differs. Anchor on the comment's
# first line, which names the route's own reason.
TAIL = "        # The job-row wait is BOUNDED."   # retry_setup only
assert s.count(MUTEX + TAIL) == 1, (
    f"anchor is not unique ({s.count(MUTEX + TAIL)} matches) — "
    "fix the anchor before running tests"
)
p.write_text(s.replace(MUTEX + TAIL, TAIL, 1))
PY
md5sum $F   # MUST differ. If not, STOP.
grep -c "acquire_commit_mutex_live(session, ws.id)" $F   # MUST print 2, not 3
.venv/bin/python -m pytest tests/proposal_assist/test_api_writers_mutex_straddle.py -q -k retry_setup
cp /tmp/rubric5.bak $F
md5sum $F
```

Expected: `retry_setup`'s straddle and wedge gates fail; `update_rubric`'s and `activate`'s stay green. The `grep -c` is the real guard — it proves you removed exactly one of the three.

Repeat for `activate`, anchoring on its own tail line (`        # Bounded, for the same reason as retry_setup:`), and confirm only `activate`'s two gates fail. Restore, then run the whole file and confirm all seventeen pass.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/ruff check .
git add src/call_manager/proposal_assist/row_locks.py src/call_manager/web/api/rubric.py \
        tests/proposal_assist/test_api_writers_mutex_straddle.py tests/test_api_rubric.py
git commit -m "feat(api): activate and retry_setup take the live mutex before their bounded setup-job lock"
```

---

## Task 5: Delete the `soft_delete_workspace` foot-gun

**Files:**
- Modify: `src/call_manager/workspaces.py` (delete lines 213-224; fix the module docstring at line 1)
- Create: `tests/workspace_delete_helper.py`
- Modify: `tests/test_workspaces_service.py` (six call sites; add the severance guard)
- Modify: `tests/proposal_assist/test_turns_processor.py` (two call sites)

**Interfaces:**
- Produces: `tests.workspace_delete_helper.soft_delete_workspace_for_test(session, *, workspace_id, workspaces_dir, timeout_seconds=10) -> None`.

Spec §9: the wrapper has **zero production callers** — the delete route calls the two halves directly so it can interleave retirement between them. It is a foot-gun, not merely dead code: it is the obvious-looking "delete a workspace" entry point and it skips retirement entirely.

- [ ] **Step 1: Create the test-only helper**

Create `tests/workspace_delete_helper.py`:

```python
"""Test-only composition of the two soft-delete halves.

Production deliberately offers only `soft_delete_workspace_db` and
`scrub_workspace_bytes` (Slice C2 §9). The old `soft_delete_workspace` wrapper
composed them and was the obvious-looking "delete a workspace" entry point — but
it skipped retirement entirely, so any caller silently left passes, jobs and the
collab room running. It is gone from production.

Tests that only need "DB half then FS half" — because they are asserting DB
effects, not retirement — use this instead. The name says test, so nobody
reaches for it in production by mistake.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.workspaces import scrub_workspace_bytes, soft_delete_workspace_db


def soft_delete_workspace_for_test(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    workspaces_dir: Path,
    timeout_seconds: int = 10,
) -> None:
    """DB half then FS half, exactly the pre-split behaviour — and no retirement."""
    soft_delete_workspace_db(
        session, workspace_id=workspace_id, timeout_seconds=timeout_seconds
    )
    scrub_workspace_bytes(workspace_id, workspaces_dir=workspaces_dir)
```

- [ ] **Step 2: Migrate the eight call sites**

Find them:

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
grep -rn "soft_delete_workspace\b" tests/ | grep -v soft_delete_workspace_db
```

Expected: imports and calls in `tests/test_workspaces_service.py` (lines ~21, 56, 96, 115, 153, 156, 194, 214, 256) and `tests/proposal_assist/test_turns_processor.py` (lines ~749, 769, 848, 867).

Migrate them **one at a time, by hand**. Do NOT run a blanket find-and-replace on the string `soft_delete_workspace`: it is a prefix of `soft_delete_workspace_db`, and one of the call sites is inside a parenthesised three-name import where deleting the wrong line breaks two other symbols.

The three shapes you will meet:

1. **A bare module-level import** (`tests/test_workspaces_service.py:21`, `from call_manager.workspaces import soft_delete_workspace`). Replace the whole line with:

```python
from tests.workspace_delete_helper import soft_delete_workspace_for_test
```

2. **A bare function-local import** (`tests/test_workspaces_service.py:96`; `tests/proposal_assist/test_turns_processor.py:749` and `:848`). Same replacement, same indentation as the line it replaces.

3. **A parenthesised multi-name import** — `tests/test_workspaces_service.py:212-216` reads:

```python
    from call_manager.workspaces import (
        scrub_workspace_bytes,
        soft_delete_workspace,
        soft_delete_workspace_db,
    )
```

Delete **only** the middle name. The other two are still production symbols this test needs:

```python
    from call_manager.workspaces import scrub_workspace_bytes, soft_delete_workspace_db
```

Then replace every **call** `soft_delete_workspace(...)` with `soft_delete_workspace_for_test(...)` — keyword arguments unchanged. Verify you got them all and touched nothing else:

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
grep -rn "soft_delete_workspace\b" tests/ src/ | grep -v soft_delete_workspace_db | grep -v soft_delete_workspace_for_test
```

Expected after migration: only `tests/workspace_delete_helper.py`'s own docstring, the severance guard added in Step 3, and prose comments.

**One test needs more than a rename: `test_split_halves_compose_to_the_wrapper` (`tests/test_workspaces_service.py:206`).** Its entire purpose is comparing the two halves against the wrapper, and Task 5 deletes the wrapper — a mechanical rename leaves branch (b) as a strict subset of branch (a) asserting the same absolute state twice, and its docstring names a symbol that no longer exists. Spec §9 says coverage migrates rather than being dropped, so migrate it: keep branch (a), delete branch (b) and its import, and rename it to what it now proves:

```python
def test_the_split_halves_stamp_terminalize_and_refuse_a_redelete(pg_session_factory, tmp_path):
    """soft_delete_workspace_db + scrub_workspace_bytes: the soft_deleted_at
    stamp, running-turn terminalization, and WorkspaceDeleted on a re-delete.

    This used to compare the halves against the soft_delete_workspace wrapper.
    Slice C2 §9 deleted that wrapper — it composed the halves while skipping
    retirement entirely — so the comparison is gone and the assertions it made
    about the halves stay here."""
```

Leave every assertion in branch (a) exactly as it is.

- [ ] **Step 3: Add the severance guard**

Append to `tests/test_workspaces_service.py`:

```python
def test_the_production_soft_delete_wrapper_stays_deleted():
    """Slice C2 §9. The wrapper composed the DB and FS halves WITHOUT retirement,
    while looking exactly like the entry point you would reach for. If it comes
    back, every caller silently skips pass, job and collab-room teardown."""
    import call_manager.workspaces as ws_mod

    assert not hasattr(ws_mod, "soft_delete_workspace"), (
        "soft_delete_workspace is back in production. It skips retirement. Use "
        "tests.workspace_delete_helper.soft_delete_workspace_for_test in tests, or "
        "call soft_delete_workspace_db and scrub_workspace_bytes directly with "
        "retirement between them (see web/api/workspaces.py's delete route)."
    )
```

- [ ] **Step 4: Run the tests to verify the guard fails**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/test_workspaces_service.py::test_the_production_soft_delete_wrapper_stays_deleted -q
```

Expected: FAIL — the wrapper is still there.

- [ ] **Step 5: Delete the wrapper**

In `src/call_manager/workspaces.py`, delete lines 213-224 in their entirety:

```python
def soft_delete_workspace(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    workspaces_dir: Path,
    timeout_seconds: int = 10,
) -> None:
    """Thin wrapper retained for existing callers: DB half then FS half, exactly
    the pre-split behaviour. The delete route now calls the two halves directly so
    it can interleave in-process retirement between them (Slice B-retire)."""
    soft_delete_workspace_db(session, workspace_id=workspace_id, timeout_seconds=timeout_seconds)
    scrub_workspace_bytes(workspace_id, workspaces_dir=workspaces_dir)
```

Then fix the module docstring's first line (line 1), which still advertises the deleted name:

```python
"""Workspace lifecycle service functions: `create_workspace`,
`soft_delete_workspace_db` and `scrub_workspace_bytes`.
```

The two log messages at lines ~196 and ~206 also say `soft_delete_workspace:` — update both to `scrub_workspace_bytes:`, which is the function they actually run in.

**And fix `soft_delete_workspace_db`'s own docstring (lines ~93-97), which becomes false the moment the wrapper is gone.** It currently reads:

```python
    Split out of soft_delete_workspace (Slice B-retire) so the delete route can
    interleave in-process retirement between the durable DB commit and the FS
    scrub: DB commit -> retire -> scrub -> 204. Nothing about the DB behaviour
    changed in the split; soft_delete_workspace is retained as a thin wrapper
    over this plus scrub_workspace_bytes.
```

Replace it with:

```python
    The delete route interleaves in-process retirement between this durable DB
    commit and the FS scrub: DB commit -> retire -> scrub -> 204. There is
    deliberately no wrapper composing this with scrub_workspace_bytes — one
    existed until Slice C2 and was deleted, because it looked like the entry
    point for deleting a workspace while skipping retirement entirely.
```

A docstring that describes a symbol the same commit deletes is how the next reader concludes the wrapper still exists and goes looking for it.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/python -m pytest tests/test_workspaces_service.py tests/proposal_assist/test_turns_processor.py tests/proposal_assist/test_workspaces_delete_route.py -q
.venv/bin/ruff check .
```

Expected: PASS, and ruff clean. If ruff reports F401 on `Path` or `Session` in `workspaces.py`, they are still used by the remaining functions — check before deleting an import.

- [ ] **Step 7: Prove the guard discriminates**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
F=src/call_manager/workspaces.py
cp $F /tmp/workspaces.bak
md5sum $F
cat >> $F <<'PY'


def soft_delete_workspace(session, *, workspace_id, workspaces_dir, timeout_seconds=10):
    soft_delete_workspace_db(session, workspace_id=workspace_id, timeout_seconds=timeout_seconds)
    scrub_workspace_bytes(workspace_id, workspaces_dir=workspaces_dir)
PY
md5sum $F   # MUST differ. If not, STOP.
.venv/bin/python -m pytest tests/test_workspaces_service.py::test_the_production_soft_delete_wrapper_stays_deleted -q
cp /tmp/workspaces.bak $F
md5sum $F
.venv/bin/python -m pytest tests/test_workspaces_service.py -q
```

Expected: the guard FAILS while the wrapper is back, passes after restoring.

- [ ] **Step 8: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/ruff check .
git add src/call_manager/workspaces.py tests/workspace_delete_helper.py tests/test_workspaces_service.py tests/proposal_assist/test_turns_processor.py
git commit -m "refactor: delete the soft_delete_workspace wrapper; tests compose the halves themselves"
```

---

## Task 6: Correct the spec's deadlock claim

**Files:**
- Modify: `docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md` (§8 lines 389-395, §12 gate 11 lines 525-527)

The spec's stated justification for the lock-order rule is wrong, and leaving it means the next reader re-derives a deadlock that cannot happen — and writes the test that cannot fail. The rule itself stands; only its rationale and its gate change.

- [ ] **Step 1: Replace the §8 lock-order paragraph**

Replace lines 389-395 (the paragraph beginning **Lock-order rule (normative).**) with:

```markdown
**Lock-order rule (normative).** `acquire_commit_mutex_live` is taken
**immediately after `require_owner`, before any other lock**. Lock-free work may
sit between the two — and semantic body validation specifically SHOULD, so that
authorization decides before a validation error can answer a caller who has no
access, and so a malformed body does not queue behind the mutex. What may not
sit there is another lock. `activate` and `retry_setup` both currently take
`WorkspaceSetupJob … FOR UPDATE` (`rubric.py:180`, `rubric.py:217`) as their
first lock, and that order must change.

Where the mutex is held across a subsequent row wait — as it now is in both
setup-job routes — that wait must carry its own bound, for the reason
`row_locks.py`'s module docstring gives: `acquire_commit_mutex` restores
`lock_timeout = DEFAULT` on success, so an unbounded wait under the mutex pins
it and blows the DELETE route's < 6.0s budget.

The reason is not deadlock. As of the pre-C2 tree, no AB-BA cycle is reachable
in either order: `soft_delete_workspace_db` never touches `WorkspaceSetupJob`
(its mutex transaction locks the workspace row, the four pass-job tables and
`TurnJob`), setup-job cancellation runs in a separate post-commit transaction
with `FOR UPDATE SKIP LOCKED`, and every other setup-job acquisition is either
`SKIP LOCKED` or mutexless — nothing held the mutex and then waited on a
setup-job row. C2 introduces the first such edge itself, and bounds it (see
below), so the property is maintained by construction rather than by luck.

This does **not** license inverting `phases/setup.py`'s own documented order
(`:498-503`, job → workspace → Call): that ordering answers a different,
real 40P01 against `_m2`/`_m3`, and it is what `activate` matches.

The reason is **convergence**. A route that takes the setup-job row before
blocking on the mutex holds that row for the whole wait — up to
`timeout_seconds`, default 10s. The delete's retirement hook cancels setup jobs
with `SKIP LOCKED`, so it *skips* the held row and the job stays non-terminal
until the reconciliation tick catches it (up to `jobs_reconcile_tick_seconds`,
default 60s). With the mutex first, the route raises `WorkspaceDeleted` before
touching the row, the hook finds it free, and cancellation is immediate.

Because the mutex is now held across the setup-job row wait, that wait is
bounded (`row_locks.SETUP_JOB_LOCK_TIMEOUT_SECONDS = 2`, → 429): an unbounded
wait there would pin the mutex and blow the DELETE route's < 6.0s budget, which
is the hazard §4 closed for the pass prefixes.
```

- [ ] **Step 2: Replace §12 gate 11**

Replace lines 525-527 (gate 11) with:

```markdown
11. **Lock order, and the bound on the wait it creates.** Two gates, both
    required, for **each** of `activate` and `retry_setup`.

    (a) *Order.* While the route is blocked on the commit mutex, the retirement
    hook's `cancel_setup_jobs_for_workspace` must still be able to cancel the
    setup job — proving the route holds no row lock while it waits. Mutation:
    take the setup-job lock first and the test must fail (the `SKIP LOCKED`
    cancel skips the held row and returns 0). Asserted *while* the route is
    still blocked; sampling after the mutex is released lets the route's own
    rollback repair the thing under test. Seed the job in a **cancellable**
    status — `_CANCELLABLE` excludes terminal states, so a `'failed'` row makes
    this gate red in both orders and its mutation vacuous.
    Note: a "resolves without deadlock" gate would NOT discriminate — no AB-BA
    cycle exists in either order. See §8.

    (b) *Bound.* Because the mutex is held across the setup-job row wait, that
    wait must be bounded (`row_locks.SETUP_JOB_LOCK_TIMEOUT_SECONDS`, → 429).
    With the row held by another session, the request must RETURN rather than
    block. Mutation: remove the bound from one route and that route's test must
    fail. Asserted as "the request came back", not as elapsed time.
```

- [ ] **Step 2b: Record the revision**

This edits a **(normative)** paragraph and a numbered acceptance gate in a spec whose header records an approved revision. Silently rewriting your own requirements is a process failure even when the new text is right, so leave provenance. Bump the revision in the header and append to whatever changelog/revision note the file already carries (check the top of the file for the existing convention and follow it exactly):

```
revision 4 (2026-08-13, Slice C2): §8's lock-order rationale and §12 gate 11
replaced. The rule stands; the deadlock justifying it does not exist. Verified
against the merged tree at 53fa9fc — soft_delete_workspace_db never touches
WorkspaceSetupJob, and setup-job cancellation is post-commit and SKIP LOCKED.
The rule's real basis is convergence (a held row is skipped by the hook), and
C2 bounds the mutex→row wait it newly introduces.
```

If the header carries no revision or changelog convention, add the note under the title as a short "Revisions" list rather than inventing a different mechanism.

- [ ] **Step 3: Verify the spec has no other reference to the deadlock claim**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
grep -n "deadlock" docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md
```

Expected: no remaining line claims `activate`/`retry_setup` deadlock against the delete. If §13 (Risks) repeats it, correct it the same way.

- [ ] **Step 4: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
git add docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md
git commit -m "docs(spec): correct Slice C's lock-order rationale — convergence, not deadlock"
```

**Superseded in part (2026-08-13, spec revision 5).** The §8 replacement text
above says the mutex is held across a row wait "as it now is in both setup-job
routes". That parenthetical is false: **all six** routes hold the mutex across a
row wait — `update_settings` and `update_rubric` UPDATE the `workspaces` row,
`add_member`/`remove_member` take `FOR KEY SHARE` on it as their FK parent, and
`activate` reaches it through `_apply_rubric` on top of its job-row wait. A later
commit bounded those four (`WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS`), and spec
revision 5 restates the rule scoped to the six, widens gate 11(b) to all six, and
adds §10 residual 8 for the root cause. Read the spec's §8, not this block, for
the current wording.

---

## Final verification

- [ ] **Step 1: Full suite**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
set -o pipefail
.venv/bin/python -m pytest tests/ -q 2>&1 | tail -20
echo "PYTEST_EXIT=${PIPESTATUS[0]}"
```

`set -o pipefail` plus `PIPESTATUS[0]` because a bare `pytest | tail` reports **tail's** exit status, which is always 0 — the pipe would hide a red suite behind a green-looking command. `PYTEST_EXIT=0` is the gate, not the absence of an error message.

Expected: the master baseline plus this slice's additions. Count them yourself rather than trusting a number written before execution: **17** in `test_api_writers_mutex_straddle.py` (1 probe self-test + 12 straddle/wedge + 2 lock-order + 2 lock-bound), **3** authorization-precedence gates (1 in `test_api_settings.py`, 2 in `test_api_rubric.py`) and **1** severance guard = **+21**, against a master baseline of 3339 passed / 1 skipped. `test_split_halves_compose_to_the_wrapper` is renamed, not added. Assert the DELTA; if the absolute baseline has moved, that is a master change, not a C2 failure. A testcontainers "connection refused" is infrastructure — re-run the affected file individually before treating it as a failure.

- [ ] **Step 2: Lint**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers
.venv/bin/ruff check .
```

Expected: `All checks passed!`

- [ ] **Step 3: Confirm the whole diff**

```bash
git -C /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers diff --stat master..HEAD
git -C /home/parlar_ai/dev/call-monitor/.worktrees/c2-mutex-writers status --porcelain
```

Expected: **9 files** changed — `row_locks.py`, `settings.py`, `rubric.py`, `workspaces.py`, the new straddle test file, the new test helper, `test_api_settings.py`, `test_api_rubric.py`, `test_workspaces_service.py`, `test_turns_processor.py` and the spec. (That is 11 paths; the count is whatever `--stat` prints — reconcile it against this list rather than against the number.) Worktree clean.

- [ ] **Step 4: Finish**

**REQUIRED SUB-SKILL:** Use superpowers:finishing-a-development-branch.

---

## Open residuals this slice does not close

Stated so nobody reads the six-route change as more than it is:

1. **Only six routes are linearized.** A file-level survey shows twenty modules with mutating routes that never call `acquire_commit_mutex`. Many are linearized indirectly through the collab kernel or `save_section`; others may not be linearized at all. Separating the two needs a per-route inventory, which is out of scope.
2. **`mark_failed`'s unbounded `FOR UPDATE`** on the workspace row (`analysis/queue.py`, the lock-order block) is unchanged, and it now sits on the recovery path that C1's `mark_done` timeouts route into. Carried from the C1 review.
3. **The reconciliation tick's 60s default** (`jobs_reconcile_tick_seconds`) is what bounds convergence for any row the hook skips. This slice removes one cause of skipping; it does not shorten the tick.
4. **`straddle_delete`'s wedge diagnostic can mislabel a raise.** `pytest.fail("the route never returned — it is wedged")` also fires when the route raised and TestClient re-raised into the sender thread, so an unmapped exception reads as a wedge. Cosmetic; fix if it bites during execution.
5. **Flake surface not established.** Nobody has run these gates. Three concurrent sessions per test against the testcontainers pool, TestClient's cross-thread portal, and the lifespan-started APScheduler's own 60s ticks are all unexercised together. The keyed probe (Task 1) removes the known interference, but "keyed" is a reasoned fix, not a measured one — if a gate flakes, suspect pool sizing first.

6. **The SPA has no 429 story for these six routes.** `useUpdateSettings` (`frontend/src/api/settings.ts`) and its member/rubric siblings `unwrap(...)` into a generic error state; nothing reads `Retry-After` or shows the busy wording, so a wedged mutex surfaces to the owner as an unexplained save failure. The *type* is already there — `web/api/__init__.py` declares `responses={code: ApiError ...}` for every `STATUS_CODES` entry on the `/api` router, so `schema.gen.ts` carries a 429 `ApiError` shape for all six operations; only the client handling is missing. Deferred deliberately: frontend work this plan scoped out (carried from the Task 1 review, triaged in the minors sweep).

7. **The post-mutex tail is unbounded everywhere this slice did not bound it — one line causes it.** `acquire_commit_mutex` restores `SET LOCAL lock_timeout = DEFAULT` after winning the advisory lock (`proposal_assist/drafting/locking.py:207`), deliberately: the `timeout_seconds` bound was written for the *acquisition* (so a wedged holder cannot pin FastAPI workers) and the restore keeps it scoped there, rather than silently making the caller's own later statements failable with a 55P03 nobody translates. The consequence is that `acquire_commit_mutex{,_live}`'s **27 call sites across 14 modules** each run their post-acquisition tail at `lock_timeout = 0`; this slice re-imposes a bound at **6** of them (the six routes), and the remaining 21 — including `collab/kernel.py` ×5, `tools/citations.py` ×4, `phases/setup.py`'s `_m1`, and the DELETE route's own `soft_delete` write — are unchanged. A holder blocked on a row there pins the mutex, so every other mutex-participating writer for that workspace 429s behind it and the DELETE route's < 6.0s budget is blown for as long as the block lasts. **Not fixable by a follow-up commit:** bounding centrally (leaving a bound in force instead of restoring DEFAULT) turns a wait into a raw `OperationalError` → 500 at all 27 sites unless each translates 55P03 or an app-level handler maps it — and the spec's §8 records that there is deliberately no app-level `CommitMutexTimeout` handler, so adding one is an error-mapping architecture decision with blast radius past this slice. It also changes behaviour asymmetrically: background and scheduler callers (`_m1`, `collab/checkpoint.py`'s tick) that today *wait* would start *raising*, converting contention into job failure and requeue. Full statement in the spec, §10 residual 8; it needs its own design pass.

**Not verified by the plan review, flagged for triage during execution:** the review that hardened this plan capped adversarial verification at 10 of 32 findings. The 22 uncapped items were plausible and evidenced but unchecked; the ones judged material were folded in above. If a task behaves unexpectedly, re-read the review output at `/tmp/claude-1066/-home-parlar-ai-dev-call-monitor/5a1c57fc-1cd1-4a11-8aa9-70a02386ee07/tasks/wv0as1h5d.output` before debugging from scratch.
