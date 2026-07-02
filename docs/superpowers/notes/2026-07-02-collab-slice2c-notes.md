# Collab Slice 2c — mid-session permission sync (implementation notes)

**Date:** 2026-07-02
**Branch:** `collab-slice2c` (built via subagent-driven-development; **not merged** — human merges)
**Plan:** `docs/superpowers/plans/2026-07-02-collab-slice2c-permission-sync.md`
**Flag status:** collab is **still OFF in prod.** The whole 2c mechanism is test-only until Slice 3 — in prod no room exists, so every permission signal is an instant no-op.

## What shipped

When a user's effective access to a *live* collab room changes, the affected connected WebSockets are now reconciled **immediately** — instead of waiting for a reconnect. One reconciliation function re-runs the single source of truth (`resolve_access`) and applies the delta.

Per-task commits (all on `collab-slice2c`):

| Task | Commit | What |
|------|--------|------|
| 1 | `853793d` | Per-client `ClientState(user_id, can_write)`; `Room.clients` set→dict; `handle` re-checks LIVE `can_write` **under the lock**; all call sites migrated |
| 2 | `03bb738` | `refresh_permissions` reconciliation + `RoomManager.drop_client` + shared `make_checkpoint_on_evict` |
| 3 | `7daa7c2` | Fail-closed `signal_permission_change` sync→async bridge + `app.state.app_loop` |
| 4 | `3186ef1` | 5 mutation routes signal after commit (+ `signal_from_request` helper; `add_member` documented no-op) |
| 5 | `4f1cb9d` | Bundled Slice-1 residual fix: `evict()` now closes all client sockets (1011) |
| 6 | *(no commit — verification-only)* | Regression sweep + OpenAPI drift check |
| 7 | *(this doc)* | Notes |

### Mechanism, component by component

- **Per-client `ClientState` (`collab/room.py`).** `Room.clients` is now `dict[client, ClientState]`. `can_write` is **mutable** — reconciliation flips it in place, and the WS write path re-reads it per message.
- **Under-lock permission re-check (Codex High #1).** `Room.handle`'s write path keeps a cheap early reject for a known non-writer, then re-evaluates `state.can_write` **under `self._lock`, with no `await` between the check and `apply_update`** — so a write frame that passed the early check but blocked on the lock while permission was revoked mid-flight loses the race and drops. Verified `await`-free by the Task-1 reviewer (source lines: early reject → lock → live recheck → `apply_update`, only a comment/`try:` between).
- **`refresh_permissions` recompute reconciliation (`collab/permissions.py`).** For every client in room `(workspace_id, artifact)`, it re-runs `resolve_access` (via a threadpool `_resolve` wrapper) and applies the delta:
  - **member, permission changed** → soft flip `state.can_write` in place (up **or** down — same mechanism handles lock→downgrade and unlock→upgrade).
  - **no longer a member** → set `state.can_write=False` **before** closing the socket (interlocks with the under-lock recheck), close with `1008`, then deregister.
- **The `ws_deleted` revoke split (Codex High #2).** `_resolve` returns `(is_member, can_write, ws_deleted)`. A revoke uses:
  - `drop_client` (**no checkpoint**) only when `ws_deleted` (soft-deleted workspace / workspace gone — a checkpoint would be pointless/error);
  - `release(on_evict=make_checkpoint_on_evict(...))` for a **live-workspace** revoke — preserving the normal last-leave checkpoint. This keeps the "git is a checkpoint of the room" invariant intact.
- **Shared `make_checkpoint_on_evict`.** The WS route's previously-inline `_checkpoint_on_evict` closure was extracted into `collab/permissions.py` and is now used by **both** the route's `release(..., on_evict=...)` and the live-workspace revoke path — one implementation. Swallows the benign teardown errors (`GitCommitError`/`CommitMutexTimeout` → warn+rollback; other → error+rollback); the durable log survives so the next trigger retries.
- **Fail-closed bridge (Codex Medium #1).** Mutation routes run in a threadpool (sync); `signal_permission_change` schedules `refresh_permissions` onto the captured app loop via `run_coroutine_threadsafe`. A missing loop (`None`) or a scheduling failure (closed loop → `RuntimeError`) degrades to a logged no-op returning `[]` — it **never** raises into the mutation route. `_log_signal_result` catches `concurrent.futures.CancelledError`. `signal_from_request` wraps the whole thing and never raises. The loop is captured as `app.state.app_loop = _asyncio.get_running_loop()` **inside the lifespan** (where a loop is running) — *not* in sync `create_app`, where `get_running_loop()` would raise (the plan's "co-locate with `session_factory`" note was corrected during the build; that site is in `create_app`).
- **5 route hooks (all fire-and-forget, on the success path only, after commit).**
  - `post_lock_spec` / `post_unlock_spec` → `["spec"]`
  - `soft_delete_workspace_api` → `["draft", "spec"]`
  - `rubric.activate` → `["draft"]` (snapshots `result = _rubric_out(ws)` **inside** the session block before signalling, to avoid a `DetachedInstanceError` — Codex Medium #2)
  - `settings.remove_member` → `["draft", "spec"]`
  - `settings.add_member` → **no signal** (a newly-added member has no live connection to reconcile; connect-time resolution handles them — documented in a comment).
  Because the signal sits only on the post-commit success path, an earlier `HTTPException` (404/409/429/500) skips it → **flag-off parity preserved** (the routes' status codes/timing/bodies are identical when no room exists).
- **Bundled Slice-1 residual fix.** `RoomManager.evict()` (poison recovery) now closes every client socket with `1011` (server error), popping the room under the lock and closing sockets outside it (per-client `suppress(Exception)`), so no client keeps editing an orphaned in-memory doc. The WS route's own `PersistError`-handler close is left in place as belt-and-suspenders (a double-close is a harmless no-op).

## Invariants proven by tests

- Soft **downgrade** on spec lock (member loses spec write; owner unaffected; member not disconnected).
- Soft **upgrade** on unlock (same mechanism flips `can_write` False→True).
- **Mid-flight-write race closed**: a write blocked on the room lock re-checks the LIVE `can_write` under the lock and drops after a concurrent revoke.
- **Live-workspace revoke checkpoints** (last client removed → `release`+checkpoint path) vs **soft-delete drops without a checkpoint** (`drop_client`, workspace gone).
- Removed member is closed (`1008`) + deregistered while **other clients stay untouched**.
- **No-op without a room** (the prod flag-off path).
- **Closed-loop signal never raises** (`[]` returned); **no-loop → `[]`**; running-loop → the coroutine actually reconciles.
- **`evict()` closes all client sockets** (`1011`) and removes the room.
- **Flag-off parity**: `tests/test_api_spec_lock.py` + the member / rubric-activate / soft-delete / spec suites under `tests/proposal_assist/` stay green **unchanged**.

## Codex findings folded (all 4, verified against source during the build)

- **High #1** — revocation lost the race to an in-flight write → FIXED (under-lock live re-check + set `can_write=False` before close; blocked-lock race test added).
- **High #2** — `drop_client` skipped the last-leave checkpoint for a live workspace → FIXED (`ws_deleted` split; `release`+checkpoint for live, `drop_client` for soft-deleted; spy test proving the split).
- **Medium #1** — not truly fail-closed → FIXED (per-schedule try/except; `CancelledError` handled; `signal_from_request` never raises; closed-loop test).
- **Medium #2** — `activate` risked a detached-ORM access → FIXED (snapshot `_rubric_out(ws)` inside the txn, then signal, then return snapshot).

## Verification (final)

- Grouped-dir sweep `tests/proposal_assist/collab/ tests/proposal_assist/ tests/test_api_spec_lock.py`: **1529 passed, 1 skipped, 0 failed**.
- `ruff check .`: clean.
- OpenAPI: **no drift** (`pnpm gen:api` regenerated `frontend/src/api/schema.gen.ts` byte-identical — no route signature/response changed, only a post-commit side effect was added).

**Known non-blocking nit:** the closed-loop bridge test emits one `RuntimeWarning: coroutine 'refresh_permissions' was never awaited` (the coroutine is constructed inline as the `run_coroutine_threadsafe` argument, then orphaned when scheduling raises on the deliberately-closed loop). It is the ONLY Slice-2c-attributable warning in the suite (all other warnings are pre-existing app/APScheduler/testcontainers teardown noise). Avoidable with a 1-line change (bind `coro = refresh_permissions(...)` and `coro.close()` in the except branch).

## Deferrals — Slice 3

- **The prod flag flip** (enable collab) — 2c is validated only via tests that call `enable_collab`.
- **Downgrade / eviction UX** — a downgraded client silently drops writes (existing non-writer behavior); a revoked client's socket just closes. Toast/redirect messaging is Slice 3.
- **Awareness / presence** — still deferred.
- **Carried-over Slice-3 prereqs** (from Slice 2b): staleness normalization (normalized-live-SHA vs raw-git-base-SHA → room-aware propose or hydrate-normalize) and whole-fragment→targeted-subtree replace. The flag flip is BLOCKED on the staleness-normalization prereq.
