# Collaborative Editing — Slice 2c: mid-session permission sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a user's effective access to a live collab room changes (spec lock/unlock, workspace activate, collaborator remove, workspace soft-delete), reconcile every affected connected WebSocket **immediately** — flip `can_write` in place, or disconnect a user who lost membership — instead of waiting for a reconnect.

**Architecture:** After a permission mutation commits, the (sync) route fire-and-forgets a signal onto the app event loop; the signal re-runs **`resolve_access`** (the same function connect uses) for every client in the affected room(s) and applies the delta — soft flip of a per-client `can_write`, or hard socket-close for a now-non-member. One reconciliation function; correctness follows from the single source of truth. The socket-close primitive also closes a Slice-1 residual (poison `evict()` didn't disconnect other clients).

**Tech Stack:** Python 3.12, FastAPI/Starlette, `pycrdt`, SQLAlchemy 2.x + PostgreSQL, pytest + testcontainers PG. **No new deps, no new migration.**

## Global Constraints

- **Flag stays OFF in prod; 2c is validated only via tests that call `enable_collab`.** In prod no room exists, so every signal is an instant no-op. The prod flag-flip is Slice 3.
- **`resolve_access` is the SINGLE source of truth.** Reconciliation re-runs `resolve_access` (`src/call_manager/proposal_assist/collab/ws_auth.py:93`); it must NOT re-implement the "who can write" rules.
- **The room-exists check IS the flag gate.** A room can only exist if collab was enabled at connect (`collab_ws.py:80-83` closes 1008 when disabled), so `refresh_permissions` returning early when `MANAGER.get(key) is None` is the entire gate — routes call the signal unconditionally, no per-route `is_collab_enabled` needed.
- **Mutation routes must never fail or block on the signal.** The signal is fire-and-forget (`run_coroutine_threadsafe`, future not awaited); a missing loop or any reconciliation error is a logged no-op and NEVER changes the route's status code, timing, or the committed mutation.
- **Flag-off parity is a hard gate.** These suites must stay green **unchanged**: `tests/test_api_spec_lock.py`, `tests/proposal_assist/` member/rubric/soft-delete suites, and the full `tests/proposal_assist/collab/` suite. New behavior is added as new tests.
- **`Room` stays DB-free.** `room.py` gains only a plain value object + a container-type change; the DB-touching reconciliation lives in the new `collab/permissions.py`.
- **Server-only.** No frontend. A downgraded client observes silently-dropped writes (existing non-writer behavior); a revoked client's socket closes. UI messaging is Slice 3.
- **Branch/worktree:** all work on branch `collab-slice2c` in a project-local `.worktrees/` worktree with its OWN `uv sync` venv (the editable install must point at the worktree's `src/`). Tests: `EMBEDDING_MODEL=bge-m3 uv run pytest`; lint gate `uv run ruff check .` (NOT format). Whole-suite testcontainers flakes — verify via grouped-dir runs.

---

## File Structure

- **Modify** `src/call_manager/proposal_assist/collab/room.py` — add `ClientState` dataclass; `Room.clients` becomes `dict[client, ClientState]`; `RoomManager.acquire` gains a `state` param; add `RoomManager.drop_client` (deregister-without-checkpoint); `RoomManager.evict` closes all client sockets (Slice-1 residual fix).
- **Modify** `src/call_manager/proposal_assist/web/collab_ws.py` — construct a `ClientState`, pass it to `acquire`, and read `state.can_write` per message.
- **Create** `src/call_manager/proposal_assist/collab/permissions.py` — `refresh_permissions` (async reconciliation), `_resolve` (threadpool `resolve_access` wrapper), `signal_permission_change` (sync→async bridge).
- **Modify** `src/call_manager/main.py` — store the lifespan event loop on `app.state.app_loop`.
- **Modify** the 5 mutation routes to signal after commit: `web/api/workspaces.py` (`post_lock_spec`, `post_unlock_spec`, `soft_delete_workspace_api`), `web/api/rubric.py` (`activate`), `web/api/settings.py` (`remove_member`); plus a documenting comment in `settings.py::add_member` (no signal — no live target).
- **Modify** existing tests that treat `Room.clients` as a set: `tests/proposal_assist/collab/test_collab_room.py`, `tests/proposal_assist/collab/test_collab_room_operate.py`.
- **Create** tests: `tests/proposal_assist/collab/test_collab_permissions.py` (reconciliation + bridge), `tests/proposal_assist/collab/test_collab_permission_routes.py` (route → signal e2e + flag-off parity), and evict-disconnect cases in `test_collab_room.py`.
- **Create** `docs/superpowers/notes/2026-07-02-collab-slice2c-notes.md`.

**Interface summary (locked across tasks):**

```python
# collab/room.py
@dataclass
class ClientState:
    user_id: int
    can_write: bool          # MUTABLE — reconciliation flips in place

class Room:
    clients: dict            # {client_object: ClientState}

class RoomManager:
    async def acquire(self, key, *, load, client, state: ClientState) -> Room
    async def drop_client(self, key, client) -> None      # deregister, NO checkpoint, evict-if-empty
    async def evict(self, key) -> None                     # now also closes all client sockets (1011)

# collab/permissions.py
async def refresh_permissions(session_factory, *, workspace_id: uuid.UUID, artifact: str) -> None
def signal_permission_change(loop, session_factory, *, workspace_id: uuid.UUID,
                             artifacts: list[str]) -> list  # fire-and-forget; [] if loop is None
```

---

### Task 1: Per-client `ClientState`; `Room.clients` becomes a dict; `acquire` carries state; route reads it

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py`
- Modify: `src/call_manager/proposal_assist/web/collab_ws.py:148,157-163`
- Modify: `tests/proposal_assist/collab/test_collab_room.py`, `tests/proposal_assist/collab/test_collab_room_operate.py` (container-type migration)
- Test: `tests/proposal_assist/collab/test_collab_room.py` (new state cases)

**Interfaces:**
- Produces: `ClientState(user_id: int, can_write: bool)`; `Room.clients: dict`; `RoomManager.acquire(key, *, load, client, state)`.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing test** — `acquire` stores a `ClientState`; a live flip of `state.can_write` is what the room now reads.

```python
# tests/proposal_assist/collab/test_collab_room.py  (append)
from call_manager.proposal_assist.collab.room import ClientState

@pytest.mark.anyio
async def test_acquire_stores_client_state_and_state_is_mutable():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    client = FakeClient()
    state = ClientState(user_id=7, can_write=False)

    async def _load():
        return []

    room = await mgr.acquire(key, load=_load, client=client, state=state)
    # the stored state is the SAME object the caller holds
    assert room.clients[client] is state
    # a later flip is visible via the room mapping (reconciliation relies on this)
    state.can_write = True
    assert room.clients[client].can_write is True
```

- [ ] **Step 2: Run it to verify it fails.** `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_room.py::test_acquire_stores_client_state_and_state_is_mutable -v` → FAIL (`ClientState` import error / `acquire` has no `state` param).

- [ ] **Step 3: Add `ClientState` and switch `Room.clients` to a dict.** In `room.py`:
  - Add `from dataclasses import dataclass` (top imports).
  - Add the dataclass just above `class Room`:

```python
@dataclass
class ClientState:
    """Per-connection state the room tracks for a client socket. `can_write` is
    MUTABLE: mid-session permission reconciliation (Slice 2c) flips it in place,
    and the WS route reads it per message."""
    user_id: int
    can_write: bool
```

  - Change `self.clients: set = set()` → `self.clients: dict = {}` (line 51).
  - In `_broadcast` (line ~149-150), change `self.clients.discard(client)` → `self.clients.pop(client, None)`. The `for client in list(self.clients):` line is UNCHANGED (iterating a dict yields its keys).

- [ ] **Step 4: Thread `state` through `RoomManager.acquire`.** Change the signature to `async def acquire(self, key, *, load, client, state):` and replace all three `room.clients.add(client)` with `room.clients[client] = state` (the fast-path at ~178, and the two slow-path sites at ~191 and ~198). `RoomManager.release`'s `room.clients.discard(client)` (line ~250) → `room.clients.pop(client, None)`. The `not room.clients` empty-checks (lines ~236, ~251) are UNCHANGED (an empty dict is falsy). `operate` does NOT register a client and is UNCHANGED.

- [ ] **Step 5: Update the WS route to construct + read `ClientState`.** In `collab_ws.py`:
  - Import: add `ClientState` to the `room` import (`from ...collab.room import MANAGER, PersistError, ClientState`).
  - After `is_member, can_write, collab_enabled = ...` and before `acquire`, build the state and pass it:

```python
    state = ClientState(user_id=user.id, can_write=can_write)
    room = await MANAGER.acquire(key, load=_load, client=websocket, state=state)
```

  - In the message loop, read the (now mutable) `state.can_write` instead of the frozen local:

```python
            await room.handle(
                sender=websocket,
                user_id=user.id,
                can_write=state.can_write,
                message=message,
                persist=_persist,
            )
```

- [ ] **Step 6: Migrate the existing set-style tests (mechanical container change).** The semantics are identical (a dict keyed by client is a superset of a set of clients); only the population syntax changes. Apply this pattern:
  - `room.clients.update({a, b})` → two lines: `room.clients[a] = ClientState(user_id=1, can_write=True)` and `room.clients[b] = ClientState(user_id=1, can_write=True)`.
  - `room.clients.add(c)` → `room.clients[c] = ClientState(user_id=1, can_write=True)`.
  - `{c1, c2} <= r1.clients` → `{c1, c2} <= r1.clients.keys()`.
  - `x in room.clients` / `x not in room.clients` — UNCHANGED (membership tests dict keys).
  Exact sites: `test_collab_room.py` lines ~51, ~71, ~91, ~112, ~168, ~191, ~210, ~230, ~249, ~314; `test_collab_room_operate.py` line ~79/~83 are `in`/`not in` (unchanged) — its comment at line ~66 ("a plain set") should be updated to "a dict keyed by client". Add `from call_manager.proposal_assist.collab.room import ClientState` where used. (The `user_id`/`can_write` values are irrelevant to these tests — they pass `can_write` to `handle()` directly — so use `user_id=1, can_write=True` uniformly.)

- [ ] **Step 7: Run the room suite.** `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_room.py tests/proposal_assist/collab/test_collab_room_operate.py tests/proposal_assist/collab/test_collab_ws_route.py -q` → PASS (new test + all migrated existing tests).

- [ ] **Step 8: Commit** (`feat(collab): per-client ClientState; Room.clients is a client→state map`).

---

### Task 2: `refresh_permissions` reconciliation + `RoomManager.drop_client` + `FakeClient.close`

**Files:**
- Create: `src/call_manager/proposal_assist/collab/permissions.py`
- Modify: `src/call_manager/proposal_assist/collab/room.py` (add `drop_client`)
- Modify: `tests/proposal_assist/collab/test_collab_room.py` (extend `FakeClient` with `close`)
- Test: `tests/proposal_assist/collab/test_collab_permissions.py` (new)

**Interfaces:**
- Consumes: `Room.clients` dict + `ClientState` (Task 1); `resolve_access` (`ws_auth.py:93`); `MANAGER` (`room.py`).
- Produces: `refresh_permissions(session_factory, *, workspace_id, artifact)`; `RoomManager.drop_client(key, client)`.

- [ ] **Step 1: Extend `FakeClient` with `close`.** In `test_collab_room.py`, add to `FakeClient`:

```python
    def __init__(self):
        self.sent: list[bytes] = []
        self.closed: int | None = None   # records the close code, or None

    async def close(self, code: int = 1000) -> None:
        self.closed = code
```

- [ ] **Step 2: Write the failing reconciliation tests.** A downgrade flips `can_write`; a revoke closes + deregisters; other clients untouched.

```python
# tests/proposal_assist/collab/test_collab_permissions.py  (new)
import uuid
import pytest
from call_manager.proposal_assist.collab.room import ClientState, MANAGER
from call_manager.proposal_assist.collab.permissions import refresh_permissions
from tests.proposal_assist.collab.test_collab_room import FakeClient  # reuse the fake

@pytest.fixture
def anyio_backend():
    return "asyncio"

# `pg_session_factory` is the project's testcontainers session factory fixture.
# `_seed_ws_owner_member` (helper you write here) creates a workspace + owner +
# one viewer via the existing model factories used elsewhere in tests/proposal_assist.

@pytest.mark.anyio
async def test_refresh_downgrades_member_write_on_locked_spec(pg_session_factory):
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    owner_c, member_c = FakeClient(), FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=owner_c,
                          state=ClientState(user_id=owner_id, can_write=True))
    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    try:
        # Lock the spec in the DB (member should lose spec write; owner keeps it).
        _lock_spec(pg_session_factory, ws_id, owner_id)
        await refresh_permissions(pg_session_factory, workspace_id=ws_id, artifact="spec")
        room = MANAGER.get(key)
        assert room.clients[member_c].can_write is False   # soft downgrade
        assert room.clients[owner_c].can_write is True     # owner unaffected
        assert member_c.closed is None                     # NOT disconnected
    finally:
        await MANAGER.evict(key)

@pytest.mark.anyio
async def test_refresh_revokes_removed_member(pg_session_factory):
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "draft")
    owner_c, member_c = FakeClient(), FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=owner_c,
                          state=ClientState(user_id=owner_id, can_write=True))
    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    try:
        _remove_member(pg_session_factory, ws_id, member_id)   # delete WorkspaceViewer
        await refresh_permissions(pg_session_factory, workspace_id=ws_id, artifact="draft")
        room = MANAGER.get(key)
        assert member_c.closed == 1008                 # hard revoke, policy-violation
        assert member_c not in room.clients            # deregistered
        assert owner_c in room.clients                 # owner untouched
    finally:
        await MANAGER.evict(key)

@pytest.mark.anyio
async def test_refresh_upgrades_member_write_on_unlock(pg_session_factory):
    # Symmetry: the SAME mechanism flips can_write False -> True. Seed a member
    # connected with can_write=False (as if the spec were locked at connect); with
    # the spec UNLOCKED in the DB, resolve_access returns can_write=True.
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=False))
    try:
        # spec is unlocked (default) → member may write
        await refresh_permissions(pg_session_factory, workspace_id=ws_id, artifact="spec")
        assert MANAGER.get(key).clients[member_c].can_write is True
        assert member_c.closed is None
    finally:
        await MANAGER.evict(key)

@pytest.mark.anyio
async def test_refresh_soft_delete_revokes_all_and_evicts(pg_session_factory):
    # Soft-delete makes resolve_access return is_member=False for EVERYONE (owner
    # included, ws_auth.py:106), so every client is closed+deregistered and the
    # room empties -> evicted WITHOUT a checkpoint (drop_client path).
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "draft")
    owner_c, member_c = FakeClient(), FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=owner_c,
                          state=ClientState(user_id=owner_id, can_write=True))
    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    _soft_delete_ws(pg_session_factory, ws_id)   # sets ws.soft_deleted_at + commit
    await refresh_permissions(pg_session_factory, workspace_id=ws_id, artifact="draft")
    assert owner_c.closed == 1008 and member_c.closed == 1008
    assert MANAGER.get(key) is None              # room emptied -> evicted (no checkpoint)

@pytest.mark.anyio
async def test_refresh_is_noop_when_no_room(pg_session_factory):
    # No room registered for this key → clean no-op (the prod flag-off path).
    await refresh_permissions(pg_session_factory, workspace_id=uuid.uuid4(), artifact="draft")
```

  **Implementer note (helpers):** also write `_soft_delete_ws(session_factory, ws_id)` — `ws = s.get(Workspace, ws_id); ws.soft_deleted_at = datetime.now(UTC); s.commit()`. All four helpers (`_seed_ws_owner_member`, `_lock_spec`, `_remove_member`, `_soft_delete_ws`) are local to this test module and use the existing `tests/proposal_assist/` model factories.

  **Implementer note:** write the three helpers (`_seed_ws_owner_member`, `_lock_spec`, `_remove_member`) using the existing `tests/proposal_assist/` factories — search for `make_user`, `Workspace(`, `WorkspaceViewer(` in that tree and mirror them. `_lock_spec` sets `ws.spec_locked_at = datetime.now(UTC)` + commit; `_remove_member` deletes the `WorkspaceViewer` row + commit. Keep them local to this test module.

- [ ] **Step 3: Add `RoomManager.drop_client`** in `room.py` (deregister without the checkpoint that `release` does):

```python
    async def drop_client(self, key: RoomKey, client) -> None:
        """Deregister a client WITHOUT checkpointing, evicting the room if it
        becomes empty. Used by permission revocation and teardown where a
        last-leave checkpoint is wrong (the workspace may be soft-deleted / the
        member just lost access). release() is the checkpointing counterpart."""
        async with self._lock:
            room = self._rooms.get(key)
            if room is None:
                return
            room.clients.pop(client, None)
            if not room.clients and room.ops == 0 and self._rooms.get(key) is room:
                self._rooms.pop(key, None)
```

- [ ] **Step 4: Implement `refresh_permissions`** in the new `permissions.py`:

```python
"""Mid-session permission reconciliation for collab rooms (Slice 2c).

After a permission mutation commits, re-derive every connected client's access
from resolve_access (the same function connect uses) and apply the delta: flip
the per-client can_write in place, or close+deregister a client that lost
membership. DB-touching (unlike room.py) — it reaches MANAGER + resolve_access.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from starlette.concurrency import run_in_threadpool

from call_manager.db.models import User
from call_manager.proposal_assist.collab.room import MANAGER
from call_manager.proposal_assist.collab.ws_auth import resolve_access

logger = logging.getLogger(__name__)

_WS_POLICY_VIOLATION = 1008  # RFC 6455 — mirrors collab_ws.py; a revoked client is closed with it.


def _resolve(session_factory, user_id: int, workspace_id: uuid.UUID, artifact: str) -> tuple[bool, bool]:
    """Re-run resolve_access for one user (its own session; runs in a threadpool).
    A removed viewer still has a User row → resolve_access returns is_member=False."""
    with session_factory() as s:
        u = s.get(User, user_id)
        if u is None:
            return (False, False)
        return resolve_access(s, user=u, workspace_id=workspace_id, artifact=artifact)


async def refresh_permissions(session_factory, *, workspace_id: uuid.UUID, artifact: str) -> None:
    """Reconcile every client in room (workspace_id, artifact). No-op if the room
    isn't live (flag off in prod, or nobody connected)."""
    key = (workspace_id, artifact)
    room = MANAGER.get(key)
    if room is None:
        return
    # Snapshot before any close/deregister so mutation can't corrupt iteration.
    # All room.clients mutations run on THIS event loop, so the snapshot is atomic
    # (no await between get and list).
    for client, state in list(room.clients.items()):
        try:
            is_member, can_write = await run_in_threadpool(
                _resolve, session_factory, state.user_id, workspace_id, artifact
            )
        except Exception:
            logger.warning(
                "collab.refresh_permissions_resolve_failed",
                extra={"event": "collab_refresh_resolve_failed", "room": str(key)},
                exc_info=True,
            )
            continue  # isolate per-client failure; leave this client as-is
        if not is_member:
            try:
                await client.close(code=_WS_POLICY_VIOLATION)
            except Exception:
                pass  # a dead socket is fine; drop it regardless
            await MANAGER.drop_client(key, client)
        else:
            state.can_write = can_write   # soft up/downgrade, in place
```

- [ ] **Step 5: Run** `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_permissions.py -q` → PASS.

- [ ] **Step 6: Commit** (`feat(collab): refresh_permissions reconciliation + drop_client`).

---

### Task 3: sync→async bridge — `signal_permission_change` + `app.state.app_loop`

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/permissions.py` (add `signal_permission_change`)
- Modify: `src/call_manager/main.py` (store the lifespan loop on `app.state.app_loop`)
- Test: `tests/proposal_assist/collab/test_collab_permissions.py` (bridge cases)

**Interfaces:**
- Consumes: `refresh_permissions` (Task 2).
- Produces: `signal_permission_change(loop, session_factory, *, workspace_id, artifacts) -> list`.

- [ ] **Step 1: Write the failing bridge tests.** With no loop → `[]` (safe no-op); with the running loop → the coroutine actually runs and reconciles.

```python
# tests/proposal_assist/collab/test_collab_permissions.py  (append)
import asyncio
from call_manager.proposal_assist.collab.permissions import signal_permission_change

@pytest.mark.anyio
async def test_signal_no_loop_is_safe_noop(pg_session_factory):
    futs = signal_permission_change(None, pg_session_factory,
                                    workspace_id=uuid.uuid4(), artifacts=["draft", "spec"])
    assert futs == []

@pytest.mark.anyio
async def test_signal_schedules_refresh_on_loop(pg_session_factory):
    ws_id, owner_id, member_id = _seed_ws_owner_member(pg_session_factory)
    key = (ws_id, "spec")
    member_c = FakeClient()

    async def _load():
        return []

    await MANAGER.acquire(key, load=_load, client=member_c,
                          state=ClientState(user_id=member_id, can_write=True))
    try:
        _lock_spec(pg_session_factory, ws_id, owner_id)
        loop = asyncio.get_running_loop()
        futs = signal_permission_change(loop, pg_session_factory,
                                        workspace_id=ws_id, artifacts=["spec"])
        assert len(futs) == 1
        # run_coroutine_threadsafe returns a concurrent.futures.Future; give the
        # loop a turn so the scheduled coroutine runs, then confirm the effect.
        await asyncio.wrap_future(futs[0])
        assert MANAGER.get(key).clients[member_c].can_write is False
    finally:
        await MANAGER.evict(key)
```

- [ ] **Step 2: Implement `signal_permission_change`** in `permissions.py`:

```python
def _log_signal_result(fut) -> None:
    exc = fut.exception()
    if exc is not None:
        logger.warning("collab.signal_permission_change_failed", exc_info=exc)


def signal_permission_change(loop, session_factory, *, workspace_id: uuid.UUID,
                             artifacts: list[str]) -> list:
    """Fire-and-forget: schedule refresh_permissions on the app event loop for
    each artifact, from a SYNC route (threadpool thread). Returns the futures
    (prod ignores them; tests may await via asyncio.wrap_future). A missing loop
    degrades to a logged-free no-op — reconciliation is best-effort."""
    if loop is None:
        return []
    futures = []
    for artifact in artifacts:
        fut = asyncio.run_coroutine_threadsafe(
            refresh_permissions(session_factory, workspace_id=workspace_id, artifact=artifact),
            loop,
        )
        fut.add_done_callback(_log_signal_result)
        futures.append(fut)
    return futures
```

- [ ] **Step 3: Store the loop on `app.state`.** In `main.py`'s `lifespan`, in the startup section (near the existing `app_loop=_asyncio.get_running_loop()` at ~line 210), add:

```python
    app.state.app_loop = _asyncio.get_running_loop()
```

  **Implementer note:** `_asyncio` is the module's existing alias (`import asyncio as _asyncio`) — confirm the alias name at the top of `main.py` and match it. Place the assignment where `app.state.settings` / `app.state.session_factory` are set (search `app.state.session_factory =`), so it's set for the whole served lifetime.

- [ ] **Step 4: Run** `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_permissions.py -q` → PASS.

- [ ] **Step 5: Commit** (`feat(collab): signal_permission_change bridge + app.state.app_loop`).

---

### Task 4: Wire the 5 mutation routes to signal after commit

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (`post_lock_spec`, `post_unlock_spec`, `soft_delete_workspace_api`)
- Modify: `src/call_manager/web/api/rubric.py` (`activate`)
- Modify: `src/call_manager/web/api/settings.py` (`remove_member` + documenting comment in `add_member`)
- Test: `tests/proposal_assist/collab/test_collab_permission_routes.py` (new) + flag-off parity

**Interfaces:**
- Consumes: `signal_permission_change` (Task 3).
- Produces: nothing new (behavior only).

- [ ] **Step 1: Add a shared route helper.** To avoid repeating the loop/session_factory plumbing in 5 places, add to `permissions.py`:

```python
def signal_from_request(request, *, workspace_id: uuid.UUID, artifacts: list[str]) -> None:
    """Route-side convenience: fire signal_permission_change using the app's
    captured loop + session_factory. Never raises (guards a missing loop)."""
    loop = getattr(request.app.state, "app_loop", None)
    signal_permission_change(
        loop, request.app.state.session_factory,
        workspace_id=workspace_id, artifacts=artifacts,
    )
```

- [ ] **Step 2: Write the failing route tests.** (Use the app `TestClient`; enable collab; connect a fake/real member; trigger the route; assert reconciliation applied. For determinism, the test can call the route and then `await` the reconciliation by having the route path exercised via the recompute directly OR by polling `state.can_write` briefly — prefer driving `refresh_permissions` assertions through the returned-future path already covered in Task 3, and here assert the ROUTE reaches the signal.)

```python
# tests/proposal_assist/collab/test_collab_permission_routes.py  (new)
# Spy that the route fires the signal with the right (workspace, artifacts):
def test_lock_route_signals_spec(monkeypatch, client_app_ctx):
    calls = []
    import call_manager.web.api.workspaces as wsmod
    monkeypatch.setattr(wsmod, "signal_from_request",
                        lambda request, *, workspace_id, artifacts: calls.append(artifacts))
    # ... set up a collab-enabled workspace with a committed non-empty spec ...
    resp = client.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert resp.status_code == 200
    assert calls == [["spec"]]

def test_remove_member_route_signals_both_artifacts(monkeypatch, client_app_ctx):
    calls = []
    import call_manager.web.api.settings as setmod
    monkeypatch.setattr(setmod, "signal_from_request",
                        lambda request, *, workspace_id, artifacts: calls.append(artifacts))
    # ... remove a member ...
    assert resp.status_code == 204
    assert calls == [["draft", "spec"]]
```

  **Implementer note:** import `signal_from_request` INTO each route module (`from call_manager.proposal_assist.collab.permissions import signal_from_request`) so the monkeypatch target is the module-level name in `workspaces.py` / `settings.py` / `rubric.py`. Also add ONE end-to-end test that does NOT monkeypatch: enable collab, connect a member via the real WS test client (mirror `test_collab_ws_route.py`), lock the spec via the route, then assert the member's next write frame is dropped — accepting that the test may need a short `await asyncio.sleep(0)`/poll for the fire-and-forget reconciliation to land.

- [ ] **Step 3: Wire `post_lock_spec` / `post_unlock_spec`.** In each, after the `with sf() as session:` block completes successfully (i.e. just before the `return SpecLockOut(...)`), add:

```python
    signal_from_request(request, workspace_id=workspace_id, artifacts=["spec"])
    return SpecLockOut(...)   # existing return
```

  Placement is AFTER the `with` block so it only runs on the success path (an HTTPException raised inside the block — 404/409/429/500 — propagates and skips the signal). Import `signal_from_request` at the top of `workspaces.py`.

- [ ] **Step 4: Wire `soft_delete_workspace_api`.** After the `with sf() as session:` block (the service committed internally), before `return Response(status_code=204)`:

```python
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft", "spec"])
    return Response(status_code=204)
```

- [ ] **Step 5: Wire `activate` (rubric.py).** After the `with ... session.commit()` block, before `return _rubric_out(ws)`:

```python
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft"])
    return _rubric_out(ws)
```

  Import `signal_from_request` at the top of `rubric.py`.

- [ ] **Step 6: Wire `remove_member` + document `add_member`.** In `settings.py`, after the `with ... session.commit()` block in `remove_member`, before `return Response(status_code=204)`:

```python
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft", "spec"])
    return Response(status_code=204)
```

  In `add_member`, add a comment (NO signal) just before its `return`: `# No permission signal: a newly-added member has no live connection to reconcile (connect requires is_member), so connect-time resolution handles them.` Import `signal_from_request` at the top of `settings.py`.

- [ ] **Step 7: Run** the route tests + the flag-off parity suites, all green:

```
EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_permission_routes.py \
  tests/test_api_spec_lock.py \
  tests/proposal_assist/ -q -k "member or rubric or activate or soft_delete or spec_lock"
```

  Expected: PASS; the parity suites unchanged (the signal is invisible when no collab room exists / flag off).

- [ ] **Step 8: Commit** (`feat(collab): mutation routes signal permission changes after commit`).

---

### Task 5: Bundled fix — `evict()` disconnects all clients (Slice-1 residual)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (`evict`)
- Test: `tests/proposal_assist/collab/test_collab_room.py`

**Interfaces:**
- Consumes: `FakeClient.close` (Task 2).
- Produces: `RoomManager.evict` now closes all client sockets.

- [ ] **Step 1: Write the failing test** — a poison `evict` closes every client socket.

```python
# tests/proposal_assist/collab/test_collab_room.py  (append)
@pytest.mark.anyio
async def test_evict_closes_all_client_sockets():
    mgr = RoomManager()
    key = (uuid.uuid4(), "draft")
    a, b = FakeClient(), FakeClient()

    async def _load():
        return []

    await mgr.acquire(key, load=_load, client=a, state=ClientState(user_id=1, can_write=True))
    await mgr.acquire(key, load=_load, client=b, state=ClientState(user_id=2, can_write=True))
    await mgr.evict(key)
    assert a.closed == 1011 and b.closed == 1011      # server-error close on poison evict
    assert mgr.get(key) is None                        # room removed
```

- [ ] **Step 2: Run it to verify it fails.** Expected: FAIL (`a.closed` is `None` — evict doesn't close sockets today).

- [ ] **Step 3: Implement.** In `room.py`, add the close-code constant near the top (`_WS_INTERNAL_ERROR = 1011  # RFC 6455 server error`) and rewrite `evict`:

```python
    async def evict(self, key: RoomKey) -> None:
        """Force-remove a room regardless of connected clients (poison recovery),
        and CLOSE every client socket so no client keeps editing an orphaned
        in-memory doc — the next connection rebuilds cleanly from the durable log.
        (Closes the Slice-1 residual where evict left other clients connected.)"""
        async with self._lock:
            room = self._rooms.pop(key, None)
        if room is None:
            return
        for client in list(room.clients):
            try:
                await client.close(code=_WS_INTERNAL_ERROR)
            except Exception:
                pass
```

  **Implementer note:** the WS route's `PersistError` handler already calls `await MANAGER.evict(key)` then `await websocket.close(_WS_INTERNAL_ERROR)`; with this change `evict` also closes the offending socket, so the route's own close becomes a harmless redundant close (a double-close is caught/no-op). Leave the route's close as-is (belt-and-suspenders); do NOT remove it.

- [ ] **Step 4: Run** `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/test_collab_room.py tests/proposal_assist/collab/test_collab_recovery.py -q` → PASS (new test + existing poison-recovery tests still green).

- [ ] **Step 5: Commit** (`fix(collab): evict() disconnects all clients (Slice-1 residual)`).

---

### Task 6: Regression sweep + OpenAPI + notes

- [ ] **Step 1: OpenAPI drift** — no route signature/response shape changed (all five routes keep their path + response_model; only a post-commit side effect was added). Run `cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api && git -C <worktree> diff --exit-code src/api/schema.gen.ts` → expect NO drift; investigate before committing if any.

- [ ] **Step 2: Grouped-dir sweep** — green:

```
EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/ tests/proposal_assist/ \
  tests/test_api_spec_lock.py -q
uv run ruff check .
```

- [ ] **Step 3: Commit** (`test(collab): 2c regression sweep`).

---

### Task 7: Notes

- [ ] **Step 1: Write** `docs/superpowers/notes/2026-07-02-collab-slice2c-notes.md` — what shipped (per-client `ClientState`; `Room.clients` map; `refresh_permissions` recompute reconciliation; `drop_client`; the `signal_permission_change` bridge + `app.state.app_loop`; the 5 route hooks; member-add is a no-op; the bundled `evict()`-disconnects-clients fix); the invariants proven by tests (soft downgrade on lock, revoke-close on member-remove/soft-delete, no-op without a room, bridge schedules+applies, flag-off parity, evict closes all); **flag still OFF in prod**; deferrals — the flag flip + downgrade/eviction UX (toast/redirect) + awareness/presence are Slice 3, alongside the carried-over Slice-3 prereqs (staleness normalization, whole-fragment→targeted-subtree replace).

- [ ] **Step 2: Commit** (`docs(collab): Slice 2c notes`).

---

## Final verification (before whole-branch review)

```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/collab/ tests/proposal_assist/ tests/test_api_spec_lock.py -q
uv run ruff check .
cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api && git -C <worktree> diff --exit-code src/api/schema.gen.ts
```

---

## Self-Review (author checklist — completed)

**1. Spec coverage:** per-client state (Task 1) ✓; recompute reconciliation via `resolve_access` (Task 2) ✓; sync→async bridge + captured loop (Task 3) ✓; 5 route hooks + member-add-no-op (Task 4) ✓; bundled evict-disconnect (Task 5) ✓; flag-off parity gated (Tasks 1/4/6) ✓; notes/deferrals (Task 7) ✓. All six triggers from the spec are covered (member-add is explicitly a documented no-op).

**2. Placeholder scan:** the only non-transcribed content is the Task-2 test helpers (`_seed_ws_owner_member`/`_lock_spec`/`_remove_member`) and the Task-1 mechanical test migration — both are "use the existing factories / apply this exact pattern" instructions with the pattern shown, not logic left undefined. Every new function (`ClientState`, `drop_client`, `refresh_permissions`, `_resolve`, `signal_permission_change`, `signal_from_request`, the `evict` rewrite) carries complete code.

**3. Type consistency:** `ClientState(user_id, can_write)` used identically in Tasks 1–3; `Room.clients: dict` keyed by client with `ClientState` values throughout; `refresh_permissions(session_factory, *, workspace_id, artifact)` and `signal_permission_change(loop, session_factory, *, workspace_id, artifacts)` match across Tasks 2–4; close codes `1008` (revoke) vs `1011` (poison evict) are used consistently; `MANAGER.acquire(..., state=...)` / `drop_client` / `evict` signatures match their definitions and call sites.

**4. Load-bearing invariants flagged for the implementer:** (a) the route signal is fire-and-forget and placed AFTER the `with sf()` block so it only fires on success and can never change a status code (flag-off parity); (b) `refresh_permissions` snapshots `room.clients` before mutating and isolates per-client failures; (c) `drop_client` (no checkpoint) — NOT `release` — is the revoke deregister, because a revoke may race a soft-delete where a checkpoint is wrong; (d) the room-exists check in `refresh_permissions` is the whole flag gate.
