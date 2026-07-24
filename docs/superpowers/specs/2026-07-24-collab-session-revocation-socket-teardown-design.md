# Collab Session-Revocation Socket Teardown (Design)

**Date:** 2026-07-24
**Status:** Design approved; ready for implementation plan.
**Severity:** Live security gap (collab shipped to prod in D4). Not part of the D0–D4 flip slices — a follow-up hardening item surfaced by the 2026-07-07 Codex whole-project audit (finding #1).

---

## 1. Problem

A live collaborative-editing WebSocket authenticates the connecting user's **account status** and **`session_epoch`** only at the handshake (`proposal_assist/collab/ws_auth.py::user_from_cookie`, checked at connect via `resolve_ws_user`). After connect, write authority rides the cached `ClientState.can_write` re-checked under `room._lock` (`collab/room.py`), and the per-connection identity in `ClientState` is `user_id` only — no epoch, no session reference.

The session-invalidation routes bump `User.session_epoch` and/or set `User.status="disabled"` but **never signal collab**, so live sockets survive:

- **Logout** (`web/api/auth.py:203`) — bumps epoch (global; invalidates every cookie).
- **Admin disable** (`web/api/admin_users.py:199-200`) — sets `status="disabled"` + bumps epoch.
- **Admin reset password** (`web/api/admin_users.py:261`) — bumps epoch.
- **Self password change** (`web/api/me.py:164`) — bumps epoch, then re-mints the acting browser's cookie at the new epoch.
- **Admin approve** (`web/api/admin_users.py:159`) — bumps epoch when an initial password is set.

The permission-reconcile machinery (`collab/permissions.py::reconcile_client` / `_resolve`) that *can* reach a live socket re-derives only membership + write-gate via `resolve_access`; it reads neither `User.status` nor `User.session_epoch`. So a disabled account or a logged-out tab keeps editing through its open socket indefinitely.

`session_epoch` is **per-user (global)** — one `Integer` counter on `User`; any bump invalidates all of that user's cookies. Therefore a teardown keyed on **`user_id`** is exactly the right granularity.

## 2. Goal & non-goals

**Goal:** when a user is logged out, disabled, or has their `session_epoch` bumped, their live collab sockets are torn down promptly, and the permission-reconcile layer additionally fails closed on a disabled account.

**Non-goals (decided with the user):** no periodic revalidation sweep; no `session_epoch` stored on `ClientState`; no per-frame/periodic DB re-reads. The single-instance in-process model makes the fire-and-forget loop bridge reliable, and the prompt teardown covers every known invalidation event; a durable periodic backstop was explicitly deferred.

## 3. Approach

**User-keyed prompt teardown + status-hardened reconcile.** The prompt path is an **unconditional close-by-`user_id`**, not a reconcile: logout / reset / self-password-change bump only the epoch (status stays `"active"`), so a re-resolve would keep the socket — but it must still die. So the primitive closes *every* socket matching the user, uniformly across all invalidation events. The `_resolve` status check is separate defense-in-depth (it makes any workspace-permission reconcile also enforce active status).

**Rejected alternatives:** a maintained per-user socket **index** (O(1) teardown, but adds an invariant to the hot connect/disconnect path — premature for a small internal tool; room iteration is adequate); **flip-only / no close** (a disabled user could still read the live doc + presence indefinitely — a security fix must actually close the socket).

## 4. Components

### 4.1 `_resolve` hardening (defense-in-depth) — `collab/permissions.py`

`_resolve` already loads the `User` row. Add a fail-closed branch: if `user.status != "active"`, return `is_member=False`. The existing not-member branch of `reconcile_client` then closes (`client.close(1008)`) + deregisters (`release`/`drop_client`). Net: every workspace-permission reconcile now also enforces active status. **Does not** check `session_epoch` (no epoch is stored on `ClientState`; the prompt teardown of §4.2 covers epoch-only events).

### 4.2 `revoke_user_sockets(user_id: int) -> int` — the prompt primitive — `collab/permissions.py`

Async, runs on the event loop. Mechanics:

1. Snapshot rooms under the manager lock and release it — add a minimal public accessor on `RoomManager` (e.g. `async def snapshot_rooms(self) -> list[Room]: async with self._lock: return list(self._rooms.values())`) rather than reaching into `MANAGER._rooms` from `permissions.py`. (Hold no manager/room lock across the per-room awaits — preserves the documented `recon_lock → manager._lock → room._lock` order.)
2. Per room: `async with room.recon_lock:` iterate `list(room.clients.items())`; for each `state.user_id == user_id`:
   - `state.can_write = False` — the **synchronous security barrier**. The write path re-reads `can_write` under `room._lock` with no `await` between the check and `doc.apply_update`, so the very next frame from that socket is dropped.
   - `await client.close(code=1008)` — wrapped so one socket's close failure does not abort the sweep.
3. Deregistration / presence-scrub / last-leave checkpoint happen via the socket's **own route `finally`** (`MANAGER.release(..., on_evict=make_checkpoint_on_evict(...))`, `web/collab_ws.py:160-167`) — the established disconnect path. We deliberately do not duplicate the checkpoint-callback wiring in the primitive; the `can_write` flip has already removed write authority before the close completes.
4. Return the number of sockets closed (logging + tests).

**Why not proactively deregister inline (like `reconcile_client`)?** The `can_write=False` flip is the real security barrier and is synchronous under `recon_lock`; letting the route `finally` deregister reuses the checkpoint wiring and avoids duplicating it. A closed-but-not-yet-deregistered socket is harmless — a subsequent broadcast to it fails and `_broadcast` scrubs dead clients, so it self-heals even if the `finally` were skipped.

### 4.3 `signal_user_revocation(request, user_id: int) -> None` — route-side wrapper — `collab/permissions.py`

Mirrors the existing `signal_from_request`: reads `request.app.state.app_loop`, fire-and-forgets `revoke_user_sockets(user_id)` via `asyncio.run_coroutine_threadsafe`, **never raises**, and degrades to a logged no-op if the loop is missing. In the single-instance in-process model the loop is always present; when collab is kill-switched or the user has no live sockets, it is a clean no-op.

### 4.4 Wiring — the five invalidation routes (post-commit)

Immediately after each route's `db.commit()`, call `signal_user_revocation(request, <target_user_id>)`:

| Route | file:line (commit) | target user |
|---|---|---|
| `logout` | `web/api/auth.py:204` | `data.user_id` |
| `admin_disable_user` | `web/api/admin_users.py:201` | path `user_id` |
| `admin_reset_password` | `web/api/admin_users.py:261` | path `user_id` |
| `change_password` (self) | `web/api/me.py:166` | current user id |
| `admin_approve_user` | `web/api/admin_users.py:159` | path `user_id` (epoch-bump branch only) |

`admin_enable_user` and `admin_change_role` do **not** bump the epoch and are **not** wired. `admin_approve_user` is belt-and-suspenders (an approved-from-pending user has no live sockets yet → harmless no-op).

**No frontend change.** A server-closed socket that auto-reconnects hits the handshake auth (`ws_auth`): logged-out (cookie deleted → no cookie), disabled (`status`), and stale-epoch cookies are all rejected there. A self-password-change's acting browser reconnects cleanly under its re-minted cookie.

## 5. Data flow

```
invalidation route (sync, threadpool)
  bump session_epoch / set status="disabled"
  db.commit()
  signal_user_revocation(request, user_id)          ── run_coroutine_threadsafe ──▶ app_loop
                                                                                       │
                                              revoke_user_sockets(user_id)  ◀──────────┘
                                                snapshot rooms (manager lock)
                                                per room (recon_lock):
                                                  state.can_write = False   ← write barrier
                                                  await client.close(1008)
  route finally (per closed socket): MANAGER.release(..., on_evict=checkpoint) → deregister + scrub + checkpoint

reconnect attempt → ws_auth handshake → rejected (no cookie / disabled / stale epoch)
```

## 6. Error handling, threading, locking

- **Fire-and-forget, never raises.** `signal_user_revocation` degrades to a logged no-op if `app_loop` is absent. Per-socket `close` is wrapped so one failure does not abort the sweep.
- **Security barrier is synchronous.** `state.can_write = False` under `recon_lock` removes write authority before the async close resolves; correctness of "no writes after revocation" does not depend on close timing.
- **Lock order preserved.** Rooms are snapshotted under `manager._lock` which is then released; per-room work takes only `recon_lock`; the route `finally`'s `release` takes `manager._lock` (then transitively `room._lock`) in a separate task after our `recon_lock` is released — no new edge in the `recon_lock → manager._lock → room._lock` DAG.
- **Threading.** Invalidation routes are sync (`def`) on the threadpool; the primitive runs on the event loop via the established bridge. `_resolve`'s DB read already runs in a threadpool inside `reconcile_client`.

## 7. Testing

- **`_resolve` unit:** a disabled member → `is_member=False` (fail-closed); an active member unchanged.
- **`revoke_user_sockets` integration** (FakeClient + rooms, the existing collab test pattern): closes every socket of user X across multiple rooms; leaves user Y's sockets open; flips `can_write=False`; a write frame submitted after revocation is dropped; returns the correct count; a room with none of the user's sockets is untouched.
- **Route tests:** `logout`, `admin_disable_user`, `admin_reset_password`, self `change_password` each invoke the revocation (assert the signal fires / a seeded socket is torn down); `admin_enable_user` and `admin_change_role` do **not**.
- **Reconnect-after-disable:** a disabled user's reconnect is rejected at the handshake (`ws_auth` status check).
- **Acceptance gates:** grouped backend sweep incl. `tests/test_api_spec.py` (`EMBEDDING_MODEL=bge-m3`, `-p no:cacheprovider`, never whole-suite; the auth/admin route tests live outside `tests/proposal_assist/` so include their dirs) · `ruff check .` · frontend `pnpm exec tsc --noEmit` + `pnpm exec vitest run src/collab` (regression only — no frontend change) · two-browser e2e (a–e) as a collab regression gate.

## 8. Constraints

- Backend-only; **no migration** (reuses `User.status` / `User.session_epoch` + the in-process `RoomManager`).
- Relies on the **single-instance hard constraint** (the in-process registry IS the source of truth for live sockets).
- Collab is **ON in prod** (post-D4), so this is a live fix, not flag-gated. The auth-route hooks degrade to no-ops when collab has no live sockets / is kill-switched.
- Explicit-path `git add`; merges **local `--no-ff` only**, not pushed without an explicit ask; do NOT invoke `/code-review` / `/ultrareview`; Codex findings are authoritative-but-verify-against-code.
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## 9. Key references

- `src/call_manager/proposal_assist/collab/ws_auth.py:25-47` — `user_from_cookie` (connect-time status+epoch check; the only validation today).
- `src/call_manager/proposal_assist/collab/permissions.py` — `_resolve` (harden), `reconcile_client` (close+deregister branch to reuse), `refresh_permissions` / `signal_from_request` / `signal_permission_change` (the loop-bridge pattern to mirror).
- `src/call_manager/proposal_assist/collab/room.py` — `ClientState` (user_id only), `RoomManager` (`_rooms`, `_lock`, `release`/`drop_client`/`evict`), the `can_write` write-path re-check.
- `src/call_manager/proposal_assist/web/collab_ws.py:138-167` — the receive loop + `finally` `MANAGER.release(...)` (the deregistration path the teardown relies on).
- `src/call_manager/auth/dependencies.py:17-47`, `src/call_manager/db/models.py:90-95` — `get_current_user` epoch check + the `session_epoch` field (per-user counter).
- Invalidation routes: `web/api/auth.py:182-208`, `web/api/admin_users.py:159/199-201/261`, `web/api/me.py:164-175`.
