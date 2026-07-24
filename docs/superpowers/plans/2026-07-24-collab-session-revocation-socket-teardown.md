# Collab Session-Revocation Socket Teardown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a user is logged out, admin-disabled, or has their `session_epoch` bumped, their live collab WebSockets are promptly and durably torn down, and the permission-reconcile layer additionally fails closed on a disabled account.

**Architecture:** A user-keyed teardown primitive (`revoke_user_sockets`) that flips `can_write` **and proactively deregisters** each matching socket (removal from `room.clients` is what makes it durable — a flag flip is reversible by a later reconcile). A strong bounded barrier wrapper (`signal_user_revocation`) that the five session-invalidation routes call post-commit. A post-`acquire` cookie re-validation in the WS route to close the handshake→register straddle window. Plus `_resolve` hardening so reconcile fails closed on `status != "active"`.

**Tech Stack:** Python 3.12, FastAPI, pycrdt, asyncio (event-loop RoomManager + threadpool sync routes), pytest (`pytest.mark.anyio`, FakeClient room pattern, TestClient route pattern). No new dependencies.

## Global Constraints

- Backend-only; **no migration** (reuses `User.status` / `User.session_epoch` + the in-process `RoomManager`).
- Relies on the **single-instance hard constraint** (the in-process registry is the source of truth for live sockets).
- Collab is **ON in prod** (post-D4) — this is a live fix, not flag-gated. The auth-route hooks degrade to no-ops when collab has no live sockets.
- No new locks / no lock-ordering change: new work sits inside the existing `recon_lock → manager._lock → room._lock` order.
- Backend tests: `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider` — grouped dirs, never whole-suite; final sweep includes `tests/test_api_spec.py` and the auth/admin/me route-test dirs.
- Lint: `uv run ruff check .` (line-length 100). Frontend gate: `pnpm exec tsc --noEmit` + `pnpm exec vitest run src/collab` (no frontend change — regression only).
- Explicit-path `git add` only (never `-A`). Merges **local `--no-ff` only**, not pushed without an explicit ask. Do NOT invoke `/code-review` / `/ultrareview`.
- Every commit message ends with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- All git commands use `git -C /home/parlar_ai/dev/call-monitor`.

## File Structure

| File | Role |
|---|---|
| `src/call_manager/proposal_assist/collab/permissions.py` | Modify: `_resolve` status check; new `revoke_user_sockets`, `signal_user_revocation`, `_REVOCATION_BARRIER_SECONDS`, `WS_RECONNECTABLE_CLOSE` |
| `src/call_manager/proposal_assist/collab/room.py` | Modify: new `RoomManager.snapshot_rooms()` |
| `src/call_manager/proposal_assist/web/collab_ws.py` | Modify: post-`acquire` straddle cookie re-validation |
| `src/call_manager/web/api/auth.py` | Modify: `logout` wiring |
| `src/call_manager/web/api/admin_users.py` | Modify: `admin_disable_user` / `admin_reset_password` / `admin_approve_user` wiring |
| `src/call_manager/web/api/me.py` | Modify: `change_password` wiring (close 1012) |
| `tests/proposal_assist/collab/test_collab_permissions.py` | Extend: `_resolve` + reconcile disabled |
| `tests/proposal_assist/collab/test_collab_revoke_user.py` | Create: `revoke_user_sockets` + `snapshot_rooms` + `signal_user_revocation` |
| `tests/proposal_assist/collab/test_collab_ws_route.py` | Extend: straddle re-validation |
| `tests/test_api_session_revocation.py` | Create: route-wiring (which routes signal, close codes) |

---

### Task 0: Cut the build branch

**Files:** none (git only).

- [ ] **Step 1: Verify master + clean tree**

```bash
git -C /home/parlar_ai/dev/call-monitor branch --show-current
git -C /home/parlar_ai/dev/call-monitor status --porcelain
```
Expected: `master`; only untracked docs (the pre-existing `docs/superpowers/...` files) — leave them.

- [ ] **Step 2: Cut the branch**

```bash
git -C /home/parlar_ai/dev/call-monitor checkout -b collab-session-revocation
```
Expected: `Switched to a new branch 'collab-session-revocation'`.

---

### Task 1: `_resolve` status hardening (defense-in-depth)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/permissions.py:81-83`
- Test: `tests/proposal_assist/collab/test_collab_permissions.py`

**Interfaces:**
- Produces: `_resolve` now returns `(False, False, ws_deleted)` for a user whose `status != "active"` (unchanged signature). `reconcile_client`'s existing not-member branch then closes+deregisters.

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/collab/test_collab_permissions.py` (add `from call_manager.db.models import User` if not present):

```python
def test_resolve_fails_closed_for_disabled_user(pg_session_factory, monkeypatch):
    # A disabled account that is still a workspace member must resolve as
    # not-member so reconcile_client tears its socket down (defense-in-depth).
    from call_manager.proposal_assist.collab import permissions as perms

    # A member+active baseline resolves True; flipping status to disabled flips it.
    ws_id, owner_id = _seed_workspace_with_owner(pg_session_factory)  # existing helper pattern
    is_member, can_write, ws_deleted = perms._resolve(
        pg_session_factory, owner_id, ws_id, "draft"
    )
    assert is_member is True and ws_deleted is False

    with pg_session_factory() as s:
        s.get(__import__("call_manager.db.models", fromlist=["User"]).User, owner_id).status = "disabled"
        s.commit()

    is_member, can_write, ws_deleted = perms._resolve(
        pg_session_factory, owner_id, ws_id, "draft"
    )
    assert is_member is False and can_write is False and ws_deleted is False
```

Note: reuse whatever workspace/owner seeding helper the file already uses (grep the file for its existing `_resolve` tests); the assertion is the point — disabled → `(False, False, False)`.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_permissions.py -p no:cacheprovider -q -k disabled
```
Expected: FAIL (disabled user still resolves `is_member=True`).

- [ ] **Step 3: Implement**

In `permissions.py::_resolve`, change the `User` guard (currently lines 81-83):

```python
        u = s.get(User, user_id)
        if u is None or u.status != "active":
            return (False, False, ws_deleted)
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_permissions.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/collab/permissions.py
```
Expected: all pass (existing permission tests unaffected — an active member still resolves normally); ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/permissions.py \
  tests/proposal_assist/collab/test_collab_permissions.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): _resolve fails closed on disabled account (session-revocation)

A disabled user who is still a workspace member now re-resolves as not-member,
so any permission reconcile closes+deregisters their live socket. Defense-in-
depth beneath the prompt teardown.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `revoke_user_sockets` primitive + `RoomManager.snapshot_rooms`

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (add `snapshot_rooms` to `RoomManager`)
- Modify: `src/call_manager/proposal_assist/collab/permissions.py` (add `revoke_user_sockets`)
- Test: `tests/proposal_assist/collab/test_collab_revoke_user.py` (create)

**Interfaces:**
- Consumes: `make_checkpoint_on_evict` (permissions.py:30), `MANAGER.release` (room.py), `_WS_POLICY_VIOLATION` (permissions.py:27).
- Produces: `RoomManager.snapshot_rooms() -> list[Room]` (async); `revoke_user_sockets(session_factory, workspaces_dir, user_id: int, *, close_code: int = _WS_POLICY_VIOLATION) -> int` (async).

- [ ] **Step 1: Write the failing tests**

Create `tests/proposal_assist/collab/test_collab_revoke_user.py`:

```python
import uuid

import pytest

from call_manager.proposal_assist.collab import permissions as perms
from call_manager.proposal_assist.collab.room import MANAGER, ClientState, Room


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeClient:
    def __init__(self):
        self.sent: list[bytes] = []
        self.closed: int | None = None

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


class BoomCloseClient(FakeClient):
    async def close(self, code: int = 1000) -> None:
        self.closed = code
        raise RuntimeError("close failed")


async def _register(key, client, user_id, can_write=True):
    async def _load():
        return []
    await MANAGER.acquire(key, load=_load, client=client,
                          state=ClientState(user_id=user_id, can_write=can_write))


async def _noop_persist(uid, upd):
    raise AssertionError("no persist expected")


@pytest.mark.anyio
async def test_snapshot_rooms_returns_live_rooms():
    key = (uuid.uuid4(), "draft")
    c = FakeClient()
    await _register(key, c, user_id=1)
    try:
        rooms = await MANAGER.snapshot_rooms()
        assert any(r.key == key for r in rooms)
    finally:
        await MANAGER.drop_client(key, c)


@pytest.mark.anyio
async def test_revoke_closes_and_deregisters_target_leaves_others(tmp_path):
    # Two rooms; user 7 has a socket in each; user 9 shares a room. Revoking 7
    # tears down BOTH of 7's sockets and removes them from room.clients; 9 stays.
    k1, k2 = (uuid.uuid4(), "draft"), (uuid.uuid4(), "draft")
    a1, a2, other = FakeClient(), FakeClient(), FakeClient()
    await _register(k1, a1, user_id=7)
    await _register(k1, other, user_id=9)
    await _register(k2, a2, user_id=7)
    try:
        n = await perms.revoke_user_sockets(
            _pg_factory_for(tmp_path), str(tmp_path), 7  # see note on factory below
        )
        assert n == 2
        r1, r2 = MANAGER.get(k1), MANAGER.get(k2)
        assert a1 not in r1.clients and a2 not in (r2.clients if r2 else {})
        assert other in r1.clients             # user 9 untouched
        assert a1.closed == 1008 and a2.closed == 1008
    finally:
        await MANAGER.drop_client(k1, other)
        MANAGER._rooms.pop(k1, None); MANAGER._rooms.pop(k2, None)


@pytest.mark.anyio
async def test_revoke_flips_can_write_and_drops_inflight_write(tmp_path):
    key = (uuid.uuid4(), "draft")
    victim = FakeClient()
    await _register(key, victim, user_id=7, can_write=True)
    room = MANAGER.get(key)
    state = room.clients[victim]
    try:
        await perms.revoke_user_sockets(_pg_factory_for(tmp_path), str(tmp_path), 7)
        assert state.can_write is False        # security barrier flipped
        assert victim not in room.clients       # durable: deregistered
    finally:
        MANAGER._rooms.pop(key, None)


@pytest.mark.anyio
async def test_close_failure_does_not_skip_deregistration(tmp_path):
    # Codex #2 durability: if client.close() raises, the socket must STILL be
    # deregistered (else a later reconcile re-grants write).
    key = (uuid.uuid4(), "draft")
    boom = BoomCloseClient()
    await _register(key, boom, user_id=7)
    room = MANAGER.get(key)
    try:
        n = await perms.revoke_user_sockets(_pg_factory_for(tmp_path), str(tmp_path), 7)
        assert n == 1
        assert boom not in room.clients        # deregistered despite close() raising
    finally:
        MANAGER._rooms.pop(key, None)
```

`_pg_factory_for(tmp_path)` / `_seed_workspace_with_owner` — reuse the module's existing pg fixtures (mirror `test_collab_permissions.py`'s session-factory + workspace seeding). The revocation only needs a session_factory + workspaces_dir to build the checkpoint callback; the last-leave checkpoint is exercised in the end-to-end test below (a room that empties on revoke). If the seeded rooms have real workspaces, assert the checkpoint ran; otherwise the callback's `ws is None` guard makes it a safe no-op.

Add an end-to-end teardown+scrub+checkpoint test that mirrors the existing `test_release_removes_departed_presence_for_survivors` pattern (a survivor sees the revoked user's presence scrubbed; a last-leave revoke checkpoints).

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_revoke_user.py -p no:cacheprovider -q
```
Expected: FAIL — `AttributeError: 'RoomManager' object has no attribute 'snapshot_rooms'` / `module 'permissions' has no attribute 'revoke_user_sockets'`.

- [ ] **Step 3a: Implement `snapshot_rooms`**

In `room.py`, add to `RoomManager` (near `get`):

```python
    async def snapshot_rooms(self) -> list["Room"]:
        """A point-in-time list of live rooms, taken under the manager lock and
        returned so callers can iterate WITHOUT holding it across per-room awaits
        (preserves the recon_lock -> manager._lock -> room._lock order)."""
        async with self._lock:
            return list(self._rooms.values())
```

- [ ] **Step 3b: Implement `revoke_user_sockets`**

In `permissions.py`, after `reconcile_client` / `refresh_permissions`:

```python
async def revoke_user_sockets(session_factory, workspaces_dir, user_id: int, *,
                              close_code: int = _WS_POLICY_VIOLATION) -> int:
    """Tear down every live collab socket belonging to ``user_id`` (all rooms).

    For each match, mirror reconcile_client's not-member branch: flip
    can_write=False (the synchronous write barrier), close the socket, and
    PROACTIVELY deregister via MANAGER.release with a last-leave checkpoint.
    Deregistration — not the flag — is what makes revocation durable: a flipped
    can_write is reversible by a later reconcile (permissions.py else-branch),
    but a socket removed from room.clients can never be re-granted. Returns the
    count torn down. Runs on the event loop; caller (signal_user_revocation)
    bridges from the sync route. Snapshot rooms first so we hold no manager/room
    lock across the per-room awaits."""
    rooms = await MANAGER.snapshot_rooms()
    n = 0
    for room in rooms:
        workspace_id, artifact = room.key
        async with room.recon_lock:
            for client, state in list(room.clients.items()):
                if state.user_id != user_id:
                    continue
                state.can_write = False
                with contextlib.suppress(Exception):
                    await client.close(code=close_code)
                on_evict = make_checkpoint_on_evict(
                    session_factory, workspaces_dir, workspace_id, artifact
                )
                await MANAGER.release(room.key, client=client, on_evict=on_evict)
                n += 1
    return n
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_revoke_user.py \
  tests/proposal_assist/collab/test_collab_room.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/collab/room.py \
       src/call_manager/proposal_assist/collab/permissions.py \
       tests/proposal_assist/collab/test_collab_revoke_user.py
```
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/room.py \
  src/call_manager/proposal_assist/collab/permissions.py \
  tests/proposal_assist/collab/test_collab_revoke_user.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): revoke_user_sockets — durable user-keyed teardown (session-revocation)

Flips can_write AND proactively deregisters (MANAGER.release) each of a user's
live sockets across all rooms — removal from room.clients is what makes the
revocation durable (a flag flip is reversible by a later reconcile). Close
failures never skip deregistration. Adds RoomManager.snapshot_rooms.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `signal_user_revocation` — strong bounded barrier wrapper

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/permissions.py` (add wrapper + constants)
- Test: `tests/proposal_assist/collab/test_collab_revoke_user.py` (extend)

**Interfaces:**
- Consumes: `revoke_user_sockets` (Task 2).
- Produces: `_REVOCATION_BARRIER_SECONDS = 5.0`; `WS_RECONNECTABLE_CLOSE = 1012` (PUBLIC — imported by `me.py` in Task 5; a public name avoids a cross-module private import); `signal_user_revocation(request, user_id: int, *, close_code: int = _WS_POLICY_VIOLATION) -> None` — schedules the teardown on `app_loop` and blocks (bounded) until it completes; never raises into the route.

**Liveness note (self-review M2):** the barrier awaits `revoke_user_sockets`, which `await`s `MANAGER.release(... on_evict=checkpoint)` → `run_in_threadpool(_cp)`. So a barrier-blocked route worker and the teardown's checkpoint compete for the same threadpool. For the rare, low-concurrency invalidation events this is fine; the `_REVOCATION_BARRIER_SECONDS` timeout is therefore a **liveness bound** (route worker releases after it, freeing the pool), not only a security-window bound. Do not remove it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/proposal_assist/collab/test_collab_revoke_user.py`:

```python
import asyncio
import threading


class _FakeRequest:
    def __init__(self, loop, session_factory, workspaces_dir):
        self.app = type("A", (), {})()
        self.app.state = type("S", (), {})()
        self.app.state.app_loop = loop
        self.app.state.session_factory = session_factory
        self.app.state.settings = type("Cfg", (), {"workspaces_dir": workspaces_dir})()


def test_signal_user_revocation_no_loop_is_noop_no_raise():
    req = _FakeRequest(None, None, "/tmp")
    perms.signal_user_revocation(req, 7)  # must not raise


def test_signal_user_revocation_blocks_until_teardown_completes(tmp_path, monkeypatch):
    # Strong barrier (Codex #3): the sync call must not return until the scheduled
    # coroutine has finished. A blocking fut.result() cannot run on its own loop's
    # thread, so we need the loop in a SEPARATE thread — the production topology
    # (sync route thread -> event loop thread).
    #
    # CRITICAL (self-review M1): do NOT reuse the module-global MANAGER here. Its
    # asyncio.Lock was bound to the pytest-anyio loop by earlier tests, so using
    # it from this background loop raises "got Future attached to a different
    # loop". Use a FRESH RoomManager monkeypatched into permissions.MANAGER and do
    # ALL manager ops on the background loop, so its lock binds to THIS loop.
    from call_manager.proposal_assist.collab.room import RoomManager

    fresh = RoomManager()
    monkeypatch.setattr(perms, "MANAGER", fresh)

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    key = (uuid.uuid4(), "draft")
    c = FakeClient()

    async def _register_on_loop():
        async def _load():
            return []
        await fresh.acquire(key, load=_load, client=c,
                            state=ClientState(user_id=7, can_write=True))

    asyncio.run_coroutine_threadsafe(_register_on_loop(), loop).result(5)
    try:
        req = _FakeRequest(loop, _pg_factory_for(tmp_path), str(tmp_path))
        perms.signal_user_revocation(req, 7)     # blocks on the barrier
        # By the time signal returns, the socket is already torn down.
        room = fresh.get(key)
        assert room is None or c not in room.clients
        assert c.closed == 1008
    finally:
        loop.call_soon_threadsafe(loop.stop)
```

Note the no-loop test uses the same `perms.MANAGER` isolation is unnecessary (it never touches MANAGER — it returns before scheduling). Only the blocking test needs the fresh manager.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_revoke_user.py -p no:cacheprovider -q -k signal
```
Expected: FAIL — `signal_user_revocation` not defined.

- [ ] **Step 3: Implement**

In `permissions.py`, add the constants near `_WS_POLICY_VIOLATION`:

```python
WS_RECONNECTABLE_CLOSE = 1012   # RFC 6455 service-restart — reconnectable; self password-change re-mint (public: imported by me.py)
_REVOCATION_BARRIER_SECONDS = 5.0
```

Then add the wrapper (after `signal_from_request`):

```python
def signal_user_revocation(request, user_id: int, *,
                           close_code: int = _WS_POLICY_VIOLATION) -> None:
    """Route-side (SYNC): schedule revoke_user_sockets on the app loop and BLOCK
    (bounded) until every matching socket is flipped+deregistered — the strong
    'no writes after the invalidation response' contract. NEVER raises into the
    route: a missing loop, a schedule failure, a timeout, or a coroutine error
    degrades to a logged warning (the DB epoch bump already invalidated future
    auth; the barrier is the live-socket guarantee, best-effort within its bound)."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        if loop is None:
            return
        session_factory = request.app.state.session_factory
        workspaces_dir = request.app.state.settings.workspaces_dir
        coro = revoke_user_sockets(
            session_factory, workspaces_dir, user_id, close_code=close_code
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            raise
        try:
            fut.result(timeout=_REVOCATION_BARRIER_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "collab.revocation_barrier_timeout",
                extra={"event": "collab_revocation_barrier_timeout", "user_id": user_id},
            )   # teardown continues on the loop; degrade to eventual
    except Exception:
        logger.warning(
            "collab.signal_user_revocation_failed",
            extra={"event": "collab_signal_user_revocation_failed", "user_id": user_id},
            exc_info=True,
        )
```

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_revoke_user.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/collab/permissions.py
```
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/collab/permissions.py \
  tests/proposal_assist/collab/test_collab_revoke_user.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): signal_user_revocation — strong bounded barrier (session-revocation)

Sync route bridge that schedules revoke_user_sockets on the app loop and BLOCKS
(bounded 5s) until the teardown completes — "no writes after the invalidation
response". Never raises into the route; a timeout degrades to eventual teardown.
Adds the 1012 (reconnectable) close code constant.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: WS-route connect-straddle epoch re-validation

**Files:**
- Modify: `src/call_manager/proposal_assist/web/collab_ws.py` (straddle block, currently `:118-131`)
- Test: `tests/proposal_assist/collab/test_collab_ws_route.py` (extend)

**Interfaces:**
- Consumes: `resolve_ws_user` / `WSAuthError` (already imported in collab_ws.py), `MANAGER.drop_client`.
- Produces: after `MANAGER.acquire`, a re-validation that closes+drops a socket whose cookie is now stale (epoch bumped / status disabled between handshake and registration).

- [ ] **Step 1: Write the failing test**

Append to `tests/proposal_assist/collab/test_collab_ws_route.py` (mirror the file's existing connect setup — enabled collab + member + valid cookie):

```python
def test_connect_straddle_revalidates_and_closes_stale_epoch(monkeypatch, ...):
    # Codex #1: a socket that passed handshake auth but whose epoch was bumped
    # before it registered must be closed by the post-acquire re-validation,
    # BEFORE the receive loop. Deterministic: resolve_ws_user succeeds at the
    # handshake call and raises WSAuthError at the straddle re-validation call.
    from call_manager.proposal_assist.collab import ws_auth

    calls = {"n": 0}
    real = ws_auth.resolve_ws_user

    async def _staggered(ws):
        calls["n"] += 1
        if calls["n"] == 1:
            return await real(ws)            # handshake: valid
        raise ws_auth.WSAuthError("session revoked")  # straddle: now stale

    monkeypatch.setattr(
        "call_manager.proposal_assist.web.collab_ws.resolve_ws_user", _staggered
    )
    # ... open the websocket via the test client; assert it is closed with 1008
    # and that MANAGER holds no client for this user in the room.
```
Match the assertion style the existing ws-route tests use for a policy close (`websocket.close` / `WebSocketDisconnect` with code 1008).

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_ws_route.py -p no:cacheprovider -q -k straddle
```
Expected: FAIL — the second `resolve_ws_user` raise is not yet handled; the socket registers and enters the loop.

- [ ] **Step 3: Implement**

In `collab_ws.py`, inside the existing straddle block, after the `reconcile_client(...)` call and before `if websocket not in room.clients: return` (currently `:129-130`), add a cookie re-validation still under `room.recon_lock`:

```python
        async with room.recon_lock:
            await reconcile_client(
                session_factory, workspaces_dir, room, websocket, state,
                workspace_id=workspace_id, artifact=artifact,
            )
            # Straddle epoch/status: a session invalidation (logout/disable/epoch
            # bump) may have committed AFTER handshake auth but BEFORE this client
            # registered — so the revocation sweep missed it and reconcile_client
            # (membership/write/status, not epoch) would keep it. Re-run the full
            # cookie validation now, under recon_lock, so a stale-epoch socket is
            # closed+dropped before the receive loop.
            if websocket in room.clients:
                try:
                    await resolve_ws_user(websocket)
                except WSAuthError:
                    with contextlib.suppress(Exception):
                        await websocket.close(code=_WS_POLICY_VIOLATION)
                    await MANAGER.drop_client(key, websocket)
        if websocket not in room.clients:
            return  # reconcile OR straddle re-validation revoked + deregistered this client
```

Add `import contextlib` to `collab_ws.py` if not already present.

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab/test_collab_ws_route.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/proposal_assist/web/collab_ws.py
```
Expected: all pass (existing ws-route tests unaffected — a valid cookie re-validates fine); ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/proposal_assist/web/collab_ws.py \
  tests/proposal_assist/collab/test_collab_ws_route.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): connect-straddle epoch re-validation (session-revocation #1)

Close the handshake->register window: after MANAGER.acquire, under recon_lock,
re-run user_from_cookie (via resolve_ws_user). A socket whose epoch/status was
invalidated between handshake and registration is closed+dropped before the
receive loop, so it can never send a write frame. Combined with the sweep, the
race is closed both ways.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Wire the five invalidation routes

**Files:**
- Modify: `src/call_manager/web/api/auth.py:204`, `src/call_manager/web/api/admin_users.py:159/201/261`, `src/call_manager/web/api/me.py:166`
- Test: `tests/test_api_session_revocation.py` (create)

**Interfaces:**
- Consumes: `signal_user_revocation`, `WS_RECONNECTABLE_CLOSE` (Task 3).
- Produces: each invalidation route calls `signal_user_revocation(request, <user_id>, close_code=<code>)` post-commit.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_session_revocation.py` — monkeypatch `signal_user_revocation` to record `(user_id, close_code)`, drive each route via the app's TestClient, assert the recorded calls. Use the app/client/seed fixtures the other route tests use (`from tests.test_api_workspaces import _app, _client, _seed`, or the module's conftest). Skeleton:

```python
def test_logout_signals_revocation_1008(monkeypatch, app_and_clients):
    calls = []
    monkeypatch.setattr(
        "call_manager.web.api.auth.signal_user_revocation",
        lambda request, user_id, *, close_code=1008: calls.append((user_id, close_code)),
    )
    # ... authenticated client, POST /api/auth/logout ...
    assert calls and calls[0][1] == 1008

def test_change_password_signals_revocation_1012(monkeypatch, ...):
    # patch call_manager.web.api.me.signal_user_revocation
    # POST the self change-password; assert recorded close_code == 1012

def test_admin_disable_and_reset_signal_1008(monkeypatch, ...): ...

def test_admin_enable_and_change_role_do_not_signal(monkeypatch, ...):
    # assert calls == [] after enable / role change
```

Each route imports `signal_user_revocation` into its own module namespace, so patch the name **in that route module** (`call_manager.web.api.auth`, `...admin_users`, `...me`).

- [ ] **Step 2: Run to verify it fails**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_api_session_revocation.py -p no:cacheprovider -q
```
Expected: FAIL — routes do not yet call `signal_user_revocation` (name not importable / no calls recorded).

- [ ] **Step 3: Implement the wiring**

`auth.py` — add `from call_manager.proposal_assist.collab.permissions import signal_user_revocation` and, inside the `if data is not None:` block, after `db.commit()` (`:204`), still inside `if user is not None:` so `data.user_id` is the confirmed target:

```python
                    user.session_epoch = (user.session_epoch or 0) + 1
                    db.commit()
                    signal_user_revocation(request, data.user_id)   # close 1008 (default)
```

`admin_users.py` — add the import; after each relevant `db.commit()`:
- `admin_disable_user` (`:201`): `signal_user_revocation(request, user_id)`.
- `admin_reset_password` (`:262`): `signal_user_revocation(request, user_id)`.
- `admin_approve_user` (`:164`): only when the epoch was bumped —
  ```python
        u.status = "active"
        ...
        db.commit()
    if body.initial_password:
        signal_user_revocation(request, user_id)
  ```
  (`admin_enable_user` and `admin_change_role` get NO call.)

`me.py` — add `from call_manager.proposal_assist.collab.permissions import signal_user_revocation, WS_RECONNECTABLE_CLOSE` and, after `session.commit()` (`:166`, after the `with` block), before the cookie re-mint:

```python
        session.commit()
    signal_user_revocation(request, user.id, close_code=WS_RECONNECTABLE_CLOSE)  # 1012: acting browser reconnects
    response.set_cookie(...)   # existing re-mint
```

Confirm `request` is a parameter of each handler (all five already take `request: Request`).

- [ ] **Step 4: Run to verify it passes**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/test_api_session_revocation.py -p no:cacheprovider -q \
  && uv run ruff check src/call_manager/web/api/auth.py \
       src/call_manager/web/api/admin_users.py src/call_manager/web/api/me.py \
       tests/test_api_session_revocation.py
```
Expected: all pass; ruff clean.

- [ ] **Step 5: Commit**

```bash
git -C /home/parlar_ai/dev/call-monitor add \
  src/call_manager/web/api/auth.py src/call_manager/web/api/admin_users.py \
  src/call_manager/web/api/me.py tests/test_api_session_revocation.py
git -C /home/parlar_ai/dev/call-monitor commit -m "$(cat <<'EOF'
feat(collab): wire session-invalidation routes to socket revocation

logout / admin-disable / admin-reset-password / admin-approve(with-password)
tear down the user's live collab sockets with 1008 (terminal); self
change-password uses 1012 (reconnectable) so the acting browser reconnects
under its re-minted cookie. admin-enable / change-role are NOT wired (no epoch
bump).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Acceptance gates

**Files:** none (verification only). Any failure → fix on-branch, re-run.

- [ ] **Step 1: Grouped backend sweep (collab + auth/admin/me routes + spec guard)**

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/proposal_assist/collab tests/test_api_session_revocation.py \
  tests/test_api_admin_users.py tests/test_api_spec.py -p no:cacheprovider -q
```
Plus the auth + me route suites (match the repo's actual filenames):

```bash
cd /home/parlar_ai/dev/call-monitor && EMBEDDING_MODEL=bge-m3 uv run pytest \
  tests/ -k "auth or (me and password)" -p no:cacheprovider -q
```
Expected: all pass. A `connection refused` on testcontainers PG = infra flake → re-run the flagged file isolated.

- [ ] **Step 2: Lint (whole repo, as CI runs it)**

```bash
cd /home/parlar_ai/dev/call-monitor && uv run ruff check .
```
Expected: `All checks passed!`

- [ ] **Step 3: Frontend regression (untouched — must stay green)**

```bash
cd /home/parlar_ai/dev/call-monitor/frontend && pnpm exec tsc --noEmit && pnpm exec vitest run src/collab
```
Expected: tsc silent; collab vitest green. `git status` shows ZERO frontend changes.

- [ ] **Step 4: Two-browser e2e (a–e) — collab regression gate**

```bash
cd /home/parlar_ai/dev/call-monitor && bash scripts/collab_e2e_run.sh
```
Expected: 5/5. Scenario (a) is occasionally timing-flaky under single-box contention — re-run once before investigating.

- [ ] **Step 5: Diff-scope check**

```bash
git -C /home/parlar_ai/dev/call-monitor diff --stat master...collab-session-revocation
```
Expected: only the 6 source files + 4 test files + docs; no migration, no frontend.

---

## Self-Review (completed at plan-writing time)

- **Spec coverage:** §4.1 → Task 1; §4.2 (primitive + snapshot_rooms + proactive deregister + close_code) → Task 2; §4.3 (strong barrier + 1012 constant) → Task 3; §4.2b (straddle re-validation) → Task 4; §4.4 (5 routes + close codes) → Task 5; §7 tests → Tasks 1–5 (each Codex-numbered test mapped: #1 straddle=Task 4, #2 durability=Task 2, #3 barrier=Task 3, #4 close-codes=Task 5); §7 gates → Task 6. §8 constraints → Global Constraints.
- **Type consistency:** `revoke_user_sockets(session_factory, workspaces_dir, user_id, *, close_code)` identical in Tasks 2 (def) and 3 (call); `signal_user_revocation(request, user_id, *, close_code)` identical in Tasks 3 (def) and 5 (calls); `snapshot_rooms()` used in Task 2 def and revoke body; `WS_RECONNECTABLE_CLOSE`/`_WS_POLICY_VIOLATION` codes consistent (1012 self-password, 1008 others).
- **Known nuance:** several test bodies reference the module's existing pg/app fixtures by pattern rather than exact name (the collab and route test files vary) — the executor matches them to each file's conftest during inline execution; assertions and monkeypatch targets are exact.
- **Route module patch targets:** each route imports `signal_user_revocation` into its own namespace, so Task 5's tests patch `call_manager.web.api.{auth,admin_users,me}.signal_user_revocation` (not the definition module) — noted in the test step.

## Self-review findings folded (plan review before execution)

- **M1 (would break execution):** Task 3's barrier test must run the loop in a separate thread (a blocking `fut.result()` can't run on its own loop), but the module-global `MANAGER`'s `asyncio.Lock` is bound to the pytest-anyio loop by earlier tests → cross-loop `RuntimeError`. **Fixed:** the barrier test uses a fresh `RoomManager()` monkeypatched into `permissions.MANAGER`, all manager ops on the background loop.
- **M2 (liveness):** the barrier `await`s `release`→`checkpoint` `run_in_threadpool`, so barrier-blocked route workers compete with the checkpoint pool; the `_REVOCATION_BARRIER_SECONDS` timeout is a **liveness bound**, not just a security bound. Documented in Task 3; acceptable for rare invalidation events.
- **L1:** the `1012` constant is **public** (`WS_RECONNECTABLE_CLOSE`) so `me.py` imports a public name (no cross-module private import).
- **L2 (accepted residual):** `revoke_user_sockets` iterates a room snapshot; a room evicted-and-recreated for the same key between `snapshot_rooms` and the per-room `recon_lock` could be missed — self-healed by the straddle re-validation (Task 4) and the next reconcile. Extreme race; not worth per-key re-fetch.
- **L3 (accepted):** Task 4's straddle test stubs `resolve_ws_user` to raise on the 2nd call (deterministic) rather than a real DB epoch bump — the epoch/status *reading* is already covered by the `ws_auth` unit tests; this test proves the new close+drop wiring. A real-epoch integration variant is optional.
- **Lock order (re-verified):** `revoke_user_sockets` calls `release` (which takes `manager._lock` → transitively `room._lock`) while holding `recon_lock` — identical to the existing `refresh_permissions`→`reconcile_client`→`release` path, so no new edge in the `recon_lock → manager._lock → room._lock` DAG.
