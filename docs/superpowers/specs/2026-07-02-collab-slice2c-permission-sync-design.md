# Collaborative Editing — Slice 2c: mid-session permission sync — Design

**Status:** approved design (brainstorm 2026-07-02); next step = implementation plan.

**Goal:** When a user's effective access to a collab room changes *while they are connected* — spec locked/unlocked, workspace activated, collaborator removed, workspace deleted — reconcile every affected live WebSocket connection **immediately**, without waiting for a reconnect. A member who loses write keeps reading with writes silently dropped; a user who loses membership is disconnected; a member who *gains* write can write at once.

**Architecture (one idea):** after any permission mutation commits, **re-derive each connected client's access from the same `resolve_access` that connect uses**, and apply the delta. One reconciliation function driven by the single source of truth — not per-trigger bespoke logic. Lock/unlock/activate/member-remove/soft-delete all collapse into "re-run `resolve_access` for every client in the affected room(s) and reconcile (flip `can_write`, or close the socket if no longer a member)."

**Tech stack:** Python 3.12, FastAPI/Starlette, `pycrdt`, SQLAlchemy 2.x + PostgreSQL, pytest + testcontainers. **No new deps, no new migration.** Single-instance backend (the module-global `MANAGER` is the room registry — same hard constraint as the rest of collab).

## Global constraints

- **Flag stays OFF in prod; 2c is validated only via tests that call `enable_collab`.** Every permission-mutation route signals only when `state.is_collab_enabled(...)` for the artifact; with the flag off (prod) no room exists, so every signal is an instant no-op. The prod flag-flip is Slice 3.
- **Server-only.** No frontend in 2c (there is no collab UI until Slice 3). The client observes a downgrade as silently-dropped write frames (already the Slice-1 behavior for non-writers) or a socket close. User-facing messaging ("you were removed", "spec is now locked") is Slice 3.
- **`resolve_access` is the single source of truth.** 2c must not re-implement the "who can write" rules; it re-runs `resolve_access` (`ws_auth.py:93`), the exact function connect uses. If the rules change later, both connect and mid-session stay correct automatically.
- **`Room` stays DB-free.** `room.py`'s deliberate isolation (no FastAPI, no sockets-as-types, no DB) is preserved: per-client state is a plain value object; the DB-touching reconciliation lives in a new module, not on `Room`/`RoomManager`.
- **Mutation routes must never fail or block on the signal.** Reconciliation is fire-and-forget onto the event loop; a signal error is logged, never surfaced to the mutating user, and never rolls back the mutation (the DB commit is authoritative; the live reconciliation is best-effort convergence, and a reconnect always re-resolves correctly).

## Trigger inventory (what changes effective access)

| # | Trigger | Route / file:line | Effect on a connected client | Reconcile call |
|---|---------|-------------------|------------------------------|----------------|
| 1 | Spec **lock** | `POST /workspaces/{id}/spec/lock` — `workspaces.py:512` | non-owner members lose **spec** write (keep read) | `['spec']` |
| 2 | Spec **unlock** | `POST /workspaces/{id}/spec/unlock` — `workspaces.py:581` | non-owner members regain **spec** write | `['spec']` |
| 3 | Workspace **activate** (setup→active) | `POST /workspaces/{id}/rubric` — `rubric.py:225` | non-owner members gain **draft** write | `['draft']` |
| 4 | Collaborator **remove** | `DELETE /workspaces/{id}/members/{uid}` — `settings.py:143` | that user loses **all** access → disconnect | `['draft','spec']` |
| 5 | Workspace **soft-delete** | `DELETE /workspaces/{id}` — `workspaces.py:104` | **everyone** loses all access → disconnect all | `['draft','spec']` |
| 6 | Collaborator **add** | `POST /workspaces/{id}/members` — `settings.py:119` | (grant) — **no live client to update** | none (see below) |

**Trigger 6 is inherently a no-op for live reconciliation:** a just-added user was not a member, so they could never have connected (connect rejects `is_member=False` at `collab_ws.py:77`). They connect fresh with correct permissions. "Full symmetry" therefore means **5 live signals; member-add is auto-satisfied by connect-time resolution.** Member-add adds *no* signal call (documented so a reader doesn't think it was forgotten).

**Triggers 4 and 5 both fall out of recompute, uniformly:** `resolve_access` returns `is_member=False` for a removed viewer (no `WorkspaceViewer` row) *and* for a soft-deleted workspace (`ws.soft_deleted_at is not None → (False, False)`, `ws_auth.py:106`). So reconciliation closes those clients without either trigger needing special-case code — member-remove closes one user's sockets, soft-delete closes everyone's (emptying both rooms).

## The current gap (why this needs new machinery)

- `can_write` is resolved **once at connect** (`collab_ws.py:76`) and passed as a captured local into `room.handle()` for every message (`collab_ws.py:157-163`); it is never re-evaluated.
- `Room.clients` is a bare `set` of sockets (`room.py:51`) with **no per-client identity** — no `user_id`, no mutable permission — so a downgrade has nowhere to land.
- No push primitive exists beyond `Room.broadcast` / `RoomManager.evict` / `release`, and **every** permission-mutation route is a **sync** FastAPI function with no path to the async `MANAGER`.

## Mechanism

### Component 1 — per-client Room state

`Room.clients: set` → `Room.clients: dict[client, ClientState]`.

```python
@dataclass
class ClientState:
    user_id: int
    can_write: bool   # MUTABLE — reconciliation flips this in place
```

- The WS route constructs its `ClientState` before `acquire`, passes it in, and keeps its own reference. Because `room.clients[socket]` **is** that same object, a reconciliation that mutates `state.can_write` is observed by the route with no extra plumbing.
- The route loop reads `state.can_write` per message (replacing the frozen local) and passes it to `room.handle(...)` — `handle`'s signature is unchanged.
- `acquire` / `operate` / `release` / `_broadcast` already iterate `self.clients`; they switch from set-iteration to dict-key-iteration. `release`/eviction membership checks (`not room.clients`) are unchanged (an empty dict is falsy).
- `RoomManager.acquire` gains a `state: ClientState` parameter and stores `room.clients[client] = state`.

### Component 2 — `collab/permissions.py` (new, DB-touching)

```python
async def refresh_permissions(session_factory, *, workspace_id, artifact) -> None:
    """Re-derive and apply access for every client in one room. No-op if the room
    isn't live (flag off in prod, or nobody connected)."""
```

Behavior:
1. `room = MANAGER.get((workspace_id, artifact))`; if `None`, return (the prod no-op path).
2. Snapshot `list(room.clients.items())` so closing/deregistering during the loop can't corrupt iteration.
3. For each `(client, state)`: recompute `(is_member, can_write)` via `resolve_access` in a threadpool DB call.
   - `is_member is False` → **hard revoke**: close the socket (policy-violation 1008) and deregister it from `room.clients`; if the room is now empty, evict it **without checkpointing** (the workspace may be soft-deleted / mid-teardown — a checkpoint could error and is pointless; the durable log survives for any later reconnect).
   - else → **soft (re)sync**: `state.can_write = can_write`.
4. Isolate per-client failures (a dead socket is dropped, mirroring `_broadcast`).

This module reaches into `MANAGER` and calls `resolve_access`; it is the only new DB-touching collab code, keeping `room.py` pure.

### Component 3 — sync→async bridge

```python
def signal_permission_change(app, *, workspace_id, artifacts: list[str]) -> None:
    """Fire-and-forget: schedule refresh_permissions on the app event loop for each
    artifact. Safe to call from a sync route (threadpool thread)."""
```

- The FastAPI app captures its running event loop at startup into `app.state.loop`.
- **All five signalling routes are sync `def` (threadpool)** — confirmed for `post_lock_spec`/`post_unlock_spec`/`post_rubric`/`remove_member`/`soft_delete_workspace_api` — so `signal_permission_change` uses `asyncio.run_coroutine_threadsafe(refresh_permissions(...), app.state.loop)` per artifact (the correct primitive for scheduling onto the loop *from another thread*). It returns immediately (does **not** block the route on the future; failures are logged via a done-callback). The plan must re-confirm each route is sync; if any is `async def`, that one schedules with `asyncio.create_task` instead (calling `run_coroutine_threadsafe` from within the loop thread is wrong).
- If there is no captured loop (e.g. a unit test not running the app), it degrades to a logged no-op — reconciliation is best-effort. Tests exercise `refresh_permissions` directly for the reconciliation logic and drive the bridge end-to-end for at least one route.

### Component 4 — route wiring

Each mutation route, **after its commit**, gated on the collab flag for the artifact(s):

- `post_lock_spec` / `post_unlock_spec` → `signal_permission_change(app, workspace_id=..., artifacts=['spec'])`
- `post_rubric` (activate) → `['draft']`
- `remove_member` → `['draft','spec']`
- `soft_delete_workspace_api` → `['draft','spec']`
- `add_member` → **no call** (no live target)

The gate mirrors the existing spec-lock pattern (`is_collab_enabled(..., artifact=...)`); calling per-artifact keeps the check precise. Because the routes are sync and the signal is fire-and-forget, this adds a few lines each and cannot change their status codes or timing.

## Data flow

**Spec lock (soft downgrade):** `post_lock_spec` commits `spec_locked_at` → (flag on) `signal_permission_change(app, ws, ['spec'])` → bridge schedules `refresh_permissions(sf, ws, 'spec')` → each spec client re-resolved → non-owners get `state.can_write=False` → their next SYNC write frame is dropped by `Room.handle`'s `if not can_write` gate; reads continue.

**Collaborator remove (hard revoke):** `remove_member` deletes the `WorkspaceViewer` and commits → `signal_permission_change(app, ws, ['draft','spec'])` → for each room, the removed user's client(s) resolve `is_member=False` → sockets closed (1008) + deregistered; other members resolve `is_member=True`, unchanged. The removed user's route loop unblocks on the close and its `finally` release is a harmless no-op (already deregistered).

## Concurrency & error handling

- `refresh_permissions` and the WS route loops both run on the single app event loop, so `state.can_write` mutation vs. per-message read is serialized (no true parallelism, no lock needed on the flag). The DB requery is a threadpool `await` — reconciliation yields there, but the flag write itself is atomic on the loop.
- **Iteration safety:** snapshot `room.clients.items()` before closing/deregistering.
- **Double-release idempotency:** a hard revoke deregisters the client; the route's `finally` `release` then finds it gone (dict `pop`/discard no-op). Both paths are safe in either order.
- **No checkpoint on revoke-close eviction:** distinct from the normal last-leave checkpoint — a revoke may be racing a soft-delete, so the empty-room eviction here skips the checkpoint (which could error on a deleted workspace); the durable log is untouched.
- **Signal never breaks the mutation:** the route has already committed; the future is not awaited; a reconciliation exception is logged in a done-callback. Worst case, a connection stays on stale permission until it next reconnects — the same fallback that exists today.

## Bundled fix — `evict()` now disconnects clients

The socket-close primitive 2c introduces is the **same** one needed to close a residual open since Slice 1: poison recovery (`PersistError`) calls `MANAGER.evict(key)`, which removes the room from the registry but leaves **other** clients' sockets open, editing an orphaned in-memory doc until they happen to disconnect. With per-client state and a close helper in hand, `evict` (poison path) closes **all** the room's client sockets (server-error 1011) as it removes the room, so every client rebuilds cleanly from the log on reconnect. This is the identical primitive, not new scope — included in 2c. (Called out separately so the plan tests it explicitly.)

## Testing strategy

All tests enable the flag (`enable_collab`) and use Slice-1-style fake clients (an object with `async send_bytes` **and** `async close`; extend the existing fake to record `close(code)`).

- **Per-client state:** `acquire` stores `ClientState`; the route reads `state.can_write` per message (a flip is observed on the next frame).
- **Soft downgrade (lock):** connect an owner + a member to `spec`; lock; assert the member's `state.can_write` is `False` and a subsequent write frame is dropped (not persisted/broadcast), while the owner still writes; unlock restores it.
- **Activate:** member connected to `draft` during setup has `can_write=False`; after activate, `can_write=True`.
- **Hard revoke (member-remove):** two members connected; remove one; assert that user's sockets are closed + deregistered and the other is untouched.
- **Soft-delete:** everyone connected to both rooms is closed and both rooms evicted (no checkpoint attempted).
- **Bridge:** `signal_permission_change` with no live room is a clean no-op; with a live room it schedules and applies. Test the recompute logic directly (call `refresh_permissions`) and at least one route end-to-end through the bridge.
- **Bundled evict-disconnect:** poison a room; assert all client sockets are closed on `evict`.
- **Flag-off parity:** with the flag off, the mutation routes behave byte-for-byte as today (no signal side effects; existing `test_api_spec_lock.py`, member, rubric, soft-delete suites stay green unchanged).

## Scope / deferrals

- **In:** the 5 live reconciliation signals + member-add-is-a-no-op; per-client state; `refresh_permissions`; the bridge; the bundled poison-evict socket close. Flag stays OFF.
- **Out (Slice 3):** the flag flip; Tiptap/frontend UX for a downgrade or eviction (toast/redirect); awareness/presence/cursors; the staleness-normalization + whole-fragment-replace prerequisites already tracked for Slice 3.
- **Out (no producer):** ownership transfer (does not exist in the codebase).

## Decisions resolved in this design

1. **Trigger scope = all six (full symmetry)** — chosen by the user; realized as 5 live signals (member-add auto-satisfied).
2. **Mechanism = push via recompute** — over targeted-delta (uniform, reuses `resolve_access` as the single source of truth) and over poll/lazy (both add latency and neither cleanly disconnects an idle revoked user).
3. **Recompute over per-trigger deltas** — every trigger becomes one `refresh_permissions` call; correctness follows from `resolve_access`.
4. **Bundle the poison-`evict` socket-close** — same primitive, closes a known Slice-1 residual.
