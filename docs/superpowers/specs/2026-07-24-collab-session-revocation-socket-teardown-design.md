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

**Non-goals (decided with the user):** no periodic revalidation sweep; no `session_epoch` stored on `ClientState`; no per-frame/periodic DB re-reads. The single-instance in-process model makes the in-process loop bridge reliable, and the prompt teardown (with a strong bounded barrier, §4.3) covers every known invalidation event synchronously; a durable periodic backstop was explicitly deferred. (The §4.2b straddle re-validation re-reads the cookie's epoch from the socket + a fresh DB row — it does not store epoch on `ClientState`, so this non-goal holds.)

## 3. Approach

**User-keyed prompt teardown + status-hardened reconcile.** The prompt path is an **unconditional close-by-`user_id`**, not a reconcile: logout / reset / self-password-change bump only the epoch (status stays `"active"`), so a re-resolve would keep the socket — but it must still die. So the primitive closes *every* socket matching the user, uniformly across all invalidation events. The `_resolve` status check is separate defense-in-depth (it makes any workspace-permission reconcile also enforce active status).

**Rejected alternatives:** a maintained per-user socket **index** (O(1) teardown, but adds an invariant to the hot connect/disconnect path — premature for a small internal tool; room iteration is adequate); **flip-only / no close** (a disabled user could still read the live doc + presence indefinitely — a security fix must actually close the socket).

## 4. Components

### 4.1 `_resolve` hardening (defense-in-depth) — `collab/permissions.py`

`_resolve` already loads the `User` row. Add a fail-closed branch: if `user.status != "active"`, return `is_member=False`. The existing not-member branch of `reconcile_client` then closes (`client.close(1008)`) + deregisters (`release`/`drop_client`). Net: every workspace-permission reconcile now also enforces active status. **Does not** check `session_epoch` (no epoch is stored on `ClientState`; the prompt teardown of §4.2 covers epoch-only events).

### 4.2 `revoke_user_sockets(session_factory, workspaces_dir, user_id: int, *, close_code: int = 1008) -> int` — the prompt primitive — `collab/permissions.py`

Async, runs on the event loop. Mechanics:

1. Snapshot rooms under the manager lock and release it — add a minimal public accessor on `RoomManager` (e.g. `async def snapshot_rooms(self) -> list[Room]: async with self._lock: return list(self._rooms.values())`) rather than reaching into `MANAGER._rooms` from `permissions.py`. (Hold no manager/room lock across the per-room awaits — preserves the documented `recon_lock → manager._lock → room._lock` order.)
2. Per room: `async with room.recon_lock:` iterate `list(room.clients.items())`; for each `state.user_id == user_id`, **mirror `reconcile_client`'s not-member branch (`permissions.py:113-131`) exactly**:
   - `state.can_write = False` — the **synchronous security barrier**. The write path re-reads `can_write` under `room._lock` with no `await` between the check and `doc.apply_update`, so the very next frame from that socket is dropped.
   - `await client.close(code=close_code)` — wrapped in `contextlib.suppress(Exception)` so one socket's close failure does not abort the sweep **and does not skip the deregistration below**.
   - **Proactively deregister** `await MANAGER.release(key, client=client, on_evict=make_checkpoint_on_evict(session_factory, workspaces_dir, workspace_id, artifact))` — the workspace is live (we revoke the *user*, not the workspace), so `release` (not `drop_client`) preserves last-leave checkpoint semantics. All under the room's `recon_lock`, so a concurrent `refresh_permissions` cannot interleave.
3. Return the number of sockets torn down (logging + tests).

**Why proactive deregistration is required (Codex review):** `can_write=False` is *not durable* — `reconcile_client`'s else-branch (`permissions.py:133`) sets `state.can_write = can_write`, so a later ordinary `refresh_permissions` on an epoch-only-revoked user who is *still a member* would **re-grant write**. Only removing the client from `room.clients` is durable: a deregistered socket is no longer in the `list(room.clients.items())` any future reconcile iterates, so it can never regain authority. The route's own `finally` `MANAGER.release(...)` remains an **idempotent backstop** (a second `release` finds the client already popped / the room already evicted and returns). (Note: `_broadcast` scrubbing a dead client removes it from `room.clients` but does *not* evict/checkpoint a now-empty room — it is not a substitute for `release`.)

**Close-code parameter:** `close_code` is `1008` (policy — terminal, no client reconnect) for logout / disable / reset, and `1012` (reconnectable) for self-password-change so the acting browser recovers under its re-minted cookie (see §4.4).

### 4.2b Connect-straddle epoch re-validation (Codex review #1) — `web/collab_ws.py`

The sweep in §4.2 only sees *registered* clients. A socket can pass handshake auth (`collab_ws.py:58-60`), then an epoch-only revocation commits and completes its sweep, then the socket registers (`collab_ws.py:117`) — so the sweep misses it, and the existing connect-straddle reconcile (`collab_ws.py:118-131`) calls `reconcile_client` → `_resolve`, which checks membership/write/status but **not epoch**, so it sees an active member and keeps the stale-cookie socket. This defeats epoch-only revocations.

Fix: inside the existing straddle block (already under `room.recon_lock`, after `MANAGER.acquire`), **re-run the full cookie validation** — `user_from_cookie(websocket.cookies.get("session"), signer=..., session_factory=...)` against a fresh DB read — *in addition to* `reconcile_client`. On `WSAuthError` (stale epoch or disabled status now committed), `await websocket.close(code=1008)`, `await MANAGER.drop_client(key, websocket)`, and `return`. This re-reads the cookie's epoch and the committed `User` row — it does **not** store epoch on `ClientState` (consistent with §2's non-goal). Combined with the sweep, the race is closed both ways: either the sweep sees the registered socket, or this post-registration re-validation sees the committed epoch. Because it runs *before* the receive loop (`collab_ws.py:138`), a straddling socket can never send a write frame.

### 4.3 `signal_user_revocation(request, user_id, *, close_code=1008) -> None` — route-side wrapper with a **strong bounded barrier** — `collab/permissions.py`

Mirrors `signal_from_request`'s loop bridge but adopts the **strong contract — "no writes after the invalidation response"** (Codex review #3). Reads `request.app.state.app_loop`; schedules `revoke_user_sockets(...)` via `asyncio.run_coroutine_threadsafe`, which returns a `concurrent.futures.Future`; then **blocks the sync route on `future.result(timeout=_REVOCATION_BARRIER_SECONDS)`** (a small bound, e.g. 5s) so the route does not return until every matching socket is flipped **and deregistered**. **Never raises into the route:** a missing loop, a timeout, or a coroutine error degrades to a logged warning and the route still returns success — the DB-level epoch bump has already invalidated all future auth; the barrier is the live-socket guarantee, best-effort within its bound. The barrier covers the security-relevant flip+deregister; a last-leave checkpoint triggered by the teardown may finish just after (durable via the persisted Yjs log regardless). No deadlock: the route thread (threadpool worker A) blocks on the future while the loop task offloads any checkpoint git-commit to a *different* threadpool worker; the bound caps worst case.

### 4.4 Wiring — the five invalidation routes (post-commit)

Immediately after each route's `db.commit()`, call `signal_user_revocation(request, <target_user_id>, close_code=<code>)`:

| Route | file:line (commit) | target user | close_code |
|---|---|---|---|
| `logout` | `web/api/auth.py:204` | `data.user_id` | `1008` |
| `admin_disable_user` | `web/api/admin_users.py:201` | path `user_id` | `1008` |
| `admin_reset_password` | `web/api/admin_users.py:261` | path `user_id` | `1008` |
| `change_password` (self) | `web/api/me.py:166` | current user id | **`1012`** |
| `admin_approve_user` | `web/api/admin_users.py:159` | path `user_id` (epoch-bump branch only) | `1008` |

`admin_enable_user` and `admin_change_role` do **not** bump the epoch and are **not** wired. `admin_approve_user` is belt-and-suspenders (an approved-from-pending user has no live sockets yet → harmless no-op).

**No frontend change — but the close code matters (Codex review #4).** The provider (`frontend/src/collab/collabProvider.ts:191`) treats close codes **`1008`/`1009` as terminal** (`closed-policy`, no reconnect) and **any other code as reconnectable** (`offline` → `scheduleReconnect`). So:
- **logout / disable / reset → `1008` (terminal, correct):** those sessions are gone. The prior spec's claim that these "reconnect cleanly" was **wrong** — they must not, and don't. (Even were a reconnectable code used, a reconnect would hit a pre-accept handshake rejection surfaced to the browser as `1006`, which `scheduleReconnect`'s `maxAttempts` cap eventually abandons.)
- **self-password-change → `1012` (reconnectable):** the acting browser has a freshly re-minted cookie at the new epoch, so the provider reconnects, the handshake passes, and the tab re-registers — no reload needed. Other tabs of the same user (stale cookies) also receive `1012`, attempt reconnect, are rejected at the handshake (`1006`), and give up — correct. No frontend code changes; `1012` is already handled by the existing non-1008/1009 branch.

## 5. Data flow

```
invalidation route (sync, threadpool)
  bump session_epoch / set status="disabled"
  db.commit()
  signal_user_revocation(request, user_id, close_code)   ── run_coroutine_threadsafe ──▶ app_loop
    future.result(timeout=5s)  ◀── STRONG BARRIER: block until flipped+deregistered ──┐
                                                                                       │
                                       revoke_user_sockets(user_id, close_code)  ◀──────┘
                                         snapshot rooms (manager lock, released)
                                         per room (recon_lock), per matching socket:
                                           state.can_write = False        ← write barrier
                                           await client.close(close_code)
                                           await MANAGER.release(..., on_evict=checkpoint)  ← durable deregister
  route returns (barrier satisfied → no writes possible from revoked sockets)
  route finally (later, per socket): MANAGER.release(...) → idempotent backstop (no-op)

concurrent CONNECT straddling the sweep:
  handshake auth ok → MANAGER.acquire → straddle block (recon_lock):
    reconcile_client (membership/write/status)  AND  re-run user_from_cookie (epoch/status)
    → stale epoch/disabled → close(1008) + drop_client + return   (before the receive loop)

reconnect attempt → ws_auth handshake → 1008 terminal (logout/disable/reset) OR
                    1012 reconnectable → re-auth (password-change acting browser recovers;
                    stale-cookie tabs rejected at handshake as 1006 → give up)
```

## 6. Error handling, threading, locking

- **Strong barrier, never raises into the route.** `signal_user_revocation` blocks on `future.result(timeout=_REVOCATION_BARRIER_SECONDS)` so the route does not return before the sockets are flipped+deregistered; a missing loop, a timeout, or a coroutine error degrades to a logged warning and the route still returns success (the DB epoch bump already invalidated future auth). Per-socket `close` is wrapped so one failure does not abort the sweep **or skip that socket's deregistration**.
- **Durability comes from deregistration, not the flag.** `state.can_write = False` is the synchronous barrier for *in-flight* frames, but it is reversible by a later reconcile (`permissions.py:133`); the socket's removal from `room.clients` via `release` — under the same `recon_lock` — is what makes the revocation permanent. The route `finally`'s `release` is an idempotent backstop, not the primary mechanism. `_broadcast`'s dead-client scrub is **not** a substitute (it does not evict/checkpoint an empty room).
- **The straddle window is closed on both sides.** A socket registering after the sweep is caught by the post-`acquire` `user_from_cookie` re-validation (§4.2b), which runs under `recon_lock` before the receive loop — so it can never send a write frame.
- **Lock order preserved.** Rooms are snapshotted under `manager._lock` which is then released; per-room work takes `recon_lock`, then `release` takes `manager._lock` (then transitively `room._lock`) — matching the existing `refresh_permissions`→`reconcile_client`→`release` path exactly, so no new edge in the `recon_lock → manager._lock → room._lock` DAG.
- **Threading / no deadlock.** Invalidation routes are sync (`def`) on threadpool worker A, which blocks on the future; the loop task runs the teardown and offloads any checkpoint git-commit to a *different* threadpool worker; the bound caps worst case. `_resolve`'s DB read already runs in a threadpool inside `reconcile_client`.

## 7. Testing

- **`_resolve` unit:** a disabled member → `is_member=False` (fail-closed); an active member unchanged.
- **`revoke_user_sockets` integration** (FakeClient + rooms, the existing collab test pattern): tears down every socket of user X across multiple rooms; leaves user Y's sockets open; flips `can_write=False` **and removes the client from `room.clients`**; a write frame submitted after revocation is dropped; returns the correct count; a room with none of the user's sockets is untouched.
- **Durable-revocation regression (Codex #2):** revoke user X (with `client.close` forced to raise, to prove close-failure does not skip deregistration), then run `refresh_permissions` on the room; X's socket must **not** reappear in `room.clients` and must **not** regain `can_write` — i.e. deregistration, not the flag, is what holds.
- **Connect-straddle epoch race (Codex #1):** seed a live room; bump the user's `session_epoch` in the DB; drive a *new* connection whose handshake passed under the old epoch; assert the post-`acquire` re-validation closes it (`1008`) and drops it before any frame is processed. Deterministic (order the epoch-bump before the straddle re-validation, not a sleep race).
- **Strong barrier (Codex #3):** the invalidation route does not return until the matching socket is flipped+deregistered — assert that at the moment the route response is observed, the seeded socket is already gone from `room.clients` (no post-response write can land).
- **Server-initiated teardown end-to-end:** a real teardown deregisters, scrubs presence for peers, and triggers the last-leave checkpoint when it empties the room.
- **Close-code / reconnection (Codex #4):** `change_password` closes with `1012` (reconnectable) and the others with `1008` (terminal) — assert the code passed to `client.close` per route; a frontend vitest (if cheap) or a note that `collabProvider.ts:191` already routes non-1008/1009 to `scheduleReconnect`.
- **Route wiring:** `logout`, `admin_disable_user`, `admin_reset_password`, self `change_password` each invoke the revocation with the correct `close_code`; `admin_enable_user` and `admin_change_role` do **not**.
- **Reconnect-after-disable:** a disabled user's reconnect is rejected at the handshake (`ws_auth` status check).
- **Acceptance gates:** grouped backend sweep incl. `tests/test_api_spec.py` (`EMBEDDING_MODEL=bge-m3`, `-p no:cacheprovider`, never whole-suite; the auth/admin route tests live outside `tests/proposal_assist/` so include their dirs) · `ruff check .` · frontend `pnpm exec tsc --noEmit` + `pnpm exec vitest run src/collab` (`1012` handling is the existing non-1008/1009 branch — regression only) · two-browser e2e (a–e) as a collab regression gate.

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
- `src/call_manager/proposal_assist/web/collab_ws.py:116-167` — `MANAGER.acquire` + the connect-straddle `recon_lock` block (where §4.2b re-validation is added) + the receive loop + `finally` `MANAGER.release(...)` (the idempotent-backstop deregistration path).
- `frontend/src/collab/collabProvider.ts:183-208` — `onclose`: `1008`/`1009` → terminal `closed-policy`; any other code → `scheduleReconnect` (why `1012` lets the acting browser recover with no frontend change); the `maxAttempts` cap that abandons a permanent `1006` handshake rejection.
- `src/call_manager/auth/dependencies.py:17-47`, `src/call_manager/db/models.py:90-95` — `get_current_user` epoch check + the `session_epoch` field (per-user counter).
- Invalidation routes: `web/api/auth.py:182-208`, `web/api/admin_users.py:159/199-201/261`, `web/api/me.py:164-175`.
