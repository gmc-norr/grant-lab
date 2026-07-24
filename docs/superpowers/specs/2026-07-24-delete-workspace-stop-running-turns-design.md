# Workspace Retirement on Delete — Design (v4)

**Status:** approved direction; v4 folds a second Codex review (7 findings, all
verified against code) whose theme is: **confirmation must propagate truthfully
through every layer** — v3 could return `confirmed=True` while a subprocess was
still alive.
**Type:** backend-only, no migration, **no frontend change** (see Contract).
**Delivery:** two slices, each its own plan/branch/merge —
**Slice A** = turns durable retirement (the confidentiality fix, standalone).
**Slice B** = the coordinator + collab retirement primitive + route contract.
The four background passes remain **Slice C** (documented non-goal here).

## Problem

Deleting a workspace must stop everything **alive** for it. The DB tombstone
`soft_deleted_at` already stops *new* work uniformly (every claim layer excludes
soft-deleted workspaces). The gap is **in-flight** work, and it has **no single
owner** — each subsystem is a bespoke call in the delete route:

- **Turns:** a running `process_turn_once` keeps its SDK subprocess streaming the
  confidential transcript to Anthropic. Not stopped at all today.
- **Collab:** a live room *is* torn down, but only as an implicit side effect of
  the route's `signal_from_request` (soft-delete → not-a-member → reconcile
  evicts; `test_collab_permissions.py:195`).
- **Passes:** a running pass coroutine keeps streaming; no cancellation surface.

v4 gives in-flight teardown one owner (Slice B) on top of subsystem-specific
**durable, confirmable** retirement primitives (Slice A for turns).

## Goal

- **`retire_workspace` fan-out:** one entry point invoking uniform async
  `retire(ws)` hooks under **one bounded deadline**, yielding one truthful
  `confirmed`.
- **Turns hook** durably stops a running turn: no relaunch, bounded terminate,
  **confirmed death** (or an explicit unconfirmed signal).
- **Collab hook** is a real primitive with a confirmation boundary (not a
  best-effort reconcile).
- **Contract:** `DELETE` → **204** when all hooks confirm; **202 Accepted** when
  deletion committed but retirement is unconfirmed (cleanup continues
  server-side). Ordering: DB commit → retirement barrier → FS scrub → response.
- `TurnJob` reaches a **terminal `failed`** state for a retired turn — including
  the paths v3 missed (post-claim launch, normal-looking stream end).

## Non-goals

- **Passes = Slice C** (register a hook into the same coordinator later; needs
  new per-workspace task cancellation for the inline-awaited
  `drain_<type>_queue_tick` coroutines). This design does **not** claim a running
  background pass stops.
- No per-workspace structured-concurrency supervisor (rejected: over-engineered
  for a single-instance in-process app).
- No new `TurnJob` status; retired turns use `_finalize_failed`.
- No migration/schema. No frontend change (202 is 2xx → the existing
  `unwrap`/`onSuccess` path already closes the modal, invalidates, navigates).

---

# Slice A — turns: durable, confirmable retirement

### A1 — registry tombstone + pending-launch acknowledgement (`drafting/registry.py`)

A one-shot `terminate()` is insufficient: `process_turn_once` launches three
times — before claim (`processor.py:158`), after claim (`:276`), and
`send_turn`→`launch` (`controller.py:213`), all get-or-create — and
`registry.terminate` pops only `_entries`, so an in-flight `_pending_launches`
(`registry.py:81`→`:195`) installs a handle *after* terminate.

- `self._retired: set[uuid.UUID]` (under `self._lock`); `WorkspaceRetired`
  exception (drafting errors module).
- `claim_launch_or_wait`: first under-lock statement —
  `if ws in self._retired: raise WorkspaceRetired(ws)`. Rejects new launchers
  **and** waiters.
- **Launch-completion acknowledgement (review #1).** `_launch_done: dict[ws,
  asyncio.Event]` — created alongside the pending future when a caller becomes
  launcher; **set** when the launcher has fully finished, i.e. from
  `fulfill_pending_launch` (installed) , `fail_pending_launch`, and the new
  `ack_launch_cleanup(ws)` the controller calls **after** tearing down a
  rejected handle. `retire_workspace` **awaits** it, so the barrier cannot
  confirm while a launcher is still mid-`adapter.launch` or mid-cleanup.
- `fulfill_pending_launch` → returns `bool installed`: under the lock, if
  `ws in self._retired`, do **not** install the entry (`installed=False`) and
  resolve the future with `WorkspaceRetired` (waiters raise, never hang); else
  install + resolve with the handle.
- `retire_workspace(ws) -> bool` (**confirmed**):
  ```python
  async with self._lock:
      self._retired.add(ws)                     # synchronous no-relaunch guarantee
      entry = self._entries.pop(ws, None)
      done = self._launch_done.get(ws)
  ok = True
  if entry is not None:
      ok = await self._teardown_entry(ws, entry, context="retire", strict=True)
  if done is not None:
      try:
          await asyncio.wait_for(done.wait(), timeout=deadline.remaining())
      except TimeoutError:
          ok = False                            # launcher cleanup unconfirmed
  return ok
  ```
  Idempotent.

### A2 — teardown reports failure (review #2) (`registry.py`, `claude_adapter.py`)

`_teardown_entry` (`registry.py:164`) deliberately logs+swallows
`adapter.terminate` failures, and `_force_kill_subprocess` returns normally when
no process is reachable (`claude_adapter.py:559`) or when `kill()` raises
(`:570`) — so a hook could "confirm" over a live subprocess.

- `_teardown_entry(..., strict: bool = False) -> bool` — returns `False` when
  `adapter.terminate` raised (or reported unconfirmed). **Existing callers
  (capacity-evict, `sweep_idle`) keep today's non-raising behavior and ignore the
  result**; only retirement passes `strict=True` and propagates it. Lock release
  stays in the `finally` regardless (no DB-connection leak).
- `adapter.terminate` gains a **bounded, confirming** contract:
  ```python
  try:
      await asyncio.wait_for(client.disconnect(), timeout=deadline.slice(_DISCONNECT_S))
  except Exception:                       # incl. TimeoutError (hang, not just raise)
      logger.warning("... disconnect failed/timed out; force-killing ...", exc_info=True)
      if not self._force_kill_subprocess(client, handle.workspace_id):
          raise TerminationUnconfirmed(handle.workspace_id)
  ```
- `_force_kill_subprocess` → `bool`: `False` when no process is reachable or
  `kill()` raised. On success it **awaits process exit** with a short second
  deadline (`process.wait()`), returning `False` if the process is not reaped —
  "confirmed death" means reaped, not merely signalled.

### A3 — processor: guarantee the terminal `failed` state (review #3) (`turns/processor.py`)

Two verified holes:
- The post-claim `await controller.launch(...)` (`:276`) and
  `set_turn_provenance` sit **outside** the finalization `try` (`:295`), so
  `WorkspaceRetired` escapes and leaves the row `running` until the reaper.
  **Fix:** extend the guarded region to cover the post-claim launch + provenance.
- When retirement ends the SDK iterator *normally*, the adapter defensively emits
  `TurnDoneEvent` (`claude_adapter.py:497`) → `_finalize_success` (`:338`) for a
  deleted workspace. **Fix:** after the stream ends, before finalizing success,
  re-check retirement (registry tombstone / `soft_deleted_at`); if retired,
  route to `_finalize_failed` ("workspace retired"). `WorkspaceRetired` is also
  caught explicitly → `_finalize_failed`.

### A4 — deadline plumbing (review #6)

A single monotonic **`Deadline`** (small helper: `remaining()`, `slice(n)`)
threaded through `controller.retire_workspace` → `registry.retire_workspace` →
`_teardown_entry` → `adapter.terminate`, instead of independent nested timeouts.
Budget with margin: barrier **10s** total; interrupt ≤2s, disconnect ≤3s,
kill-reap ≤1s, launcher-ack the remainder — so a normal deadline-edge run does
not become a spurious 202. Because `asyncio.wait_for` can overrun while awaiting
cancellation, the disconnect is run as a **task** and force-kill fires at the
deadline independently of the cancellation completing.

### A5 — controller (`drafting/controller.py`)

- `launch`: `installed = await fulfill_pending_launch(...)`; `handed_off =
  installed`; `if not installed: raise WorkspaceRetired(ws)` → the existing
  `except` (`:178-190`) tears down the fresh handle + releases the lock, then
  calls `registry.ack_launch_cleanup(ws)` (new, in that path) so the retirement
  barrier's `await done.wait()` completes.
- `retire_workspace(ws, deadline) -> bool`: best-effort bounded `interrupt`
  (suppressed), then `return await registry.retire_workspace(ws, deadline)`.

### A6 — unconfirmed-teardown retry (server-side self-heal)

When `strict` teardown fails, the popped entry's handle would otherwise be
orphaned with no registry reference (nothing left to retry against). Retain it in
`self._retire_failures: dict[ws, _Entry]` and retry teardown on the existing
**idle-sweep tick** (cheap, reuses infrastructure), clearing on success. This is
what makes the 202 path self-healing rather than merely reported.

### Slice A acceptance

`controller.retire_workspace(ws, deadline) -> bool` is independently testable and
mergeable **without** the coordinator (call it directly in tests). Gates:
`tests/proposal_assist/drafting`, `tests/proposal_assist/turns`,
`tests/test_api_spec.py`, `ruff check .`.

---

# Slice B — coordinator, collab primitive, route contract

### B1 — coordinator (`proposal_assist/lifecycle/retirement.py`, new)

```python
RetireHook = Callable[[uuid.UUID, Deadline], Awaitable[bool]]

class WorkspaceRetirement:
    """Owns teardown of in-flight workspace-scoped async work. Hooks are wired in
    lifespan; the DB tombstone remains the 'no new work' gate. Fans out
    CONCURRENTLY and ISOLATED — one hook's failure never skips another — and
    reports whether ALL confirmed."""
    REQUIRED = ("turns", "collab")

    def __init__(self, hooks: dict[str, RetireHook]):
        missing = set(self.REQUIRED) - hooks.keys()
        if missing:                       # review #4: config error, not "nothing alive"
            raise ValueError(f"retirement hooks missing: {sorted(missing)}")
        self._hooks = hooks

    async def retire(self, workspace_id, deadline) -> bool:
        results = await asyncio.gather(*(
            self._run_one(n, h, workspace_id, deadline) for n, h in self._hooks.items()
        ))
        return bool(results) and all(results)      # empty ⇒ False, never vacuous True
```
`_run_one` awaits the hook, returns its bool, and converts an exception to
`False` with a logged warning.

### B2 — bridge fails **closed** (review #4) (same module)

```python
def signal_workspace_retirement(request, workspace_id) -> bool:
    """SYNC route bridge: schedule the fan-out on the app loop and BLOCK (bounded).
    Returns confirmed. NEVER raises into the route. A missing loop/coordinator is a
    CONFIGURATION failure, not proof nothing is alive → False."""
```
Missing loop/coordinator → **`False`** (was `True` in v3). Headless/test apps that
legitimately have nothing live install an **explicit no-op coordinator**
(`WorkspaceRetirement({"turns": _noop, "collab": _noop})`) rather than relying on
absence.

### B3 — collab retirement primitive (review #5) (`collab/room.py`, `collab/…`)

`refresh_permissions` is **not** a confirmation boundary: on a resolve error
`reconcile_client` flips `can_write=False`, logs, and returns — the socket stays
registered and the room stays live, yet the hook would return normally. Replace
with a direct primitive on `RoomManager`:

```python
async def retire_workspace(self, workspace_id) -> bool:
    """Tombstone BOTH artifact rooms for a deleted workspace and evict them.
    Synchronous in-process state ⇒ a real confirmation boundary."""
```
The primitive needs no deadline (pure in-process state, no I/O that can hang), so
`make_collab_retire_hook()` returns a thin adapter matching `RetireHook`:
`async def _hook(ws, _deadline): return await MANAGER.retire_workspace(ws)`.
- `self._retired_ws: set[uuid.UUID]`; `acquire` raises/immediately drops for a
  retired workspace (closes the connect-straddle; the existing post-acquire
  recheck at `collab_ws.py:117` already keeps such a socket out of the write
  loop, but rejecting in `acquire` makes it confirmable).
- For each key `(ws, "draft")`, `(ws, "spec")`: set every `ClientState.can_write
  = False` (synchronous write barrier), mark `room.evicted = True` (blocks
  in-flight persist/broadcast), close each client with **1008** (policy — the
  frontend treats 1008 as terminal, so no reconnect storm against a deleted
  workspace; `evict()`'s 1011 would invite reconnects), and remove the room.
- **No checkpoint** (workspace is gone — matches the existing soft-delete
  eviction semantics).
- Confirm by asserting both keys are absent and no clients remain → return `True`.

The route's `signal_from_request` call is **removed** (subsumed by this hook).

### B4 — service split (`src/call_manager/workspaces.py`)

- `soft_delete_workspace_db(session, *, workspace_id)` — current body through
  `session.commit()`.
- `scrub_workspace_bytes(workspace_id, workspaces_dir)` — the two best-effort FS
  scrubs (`:134-148`).
- Thin `soft_delete_workspace = _db + _bytes` wrapper retained for existing
  non-route callers/tests.

### B5 — wiring + route (`main.py` lifespan; `web/api/workspaces.py:1488`)

```python
app.state.workspace_retirement = WorkspaceRetirement(hooks={
    "turns":  workspace_controller.retire_workspace,
    "collab": make_collab_retire_hook(),          # MANAGER.retire_workspace
})
```
```python
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        soft_delete_workspace_db(session, workspace_id=ws.id)        # commits
    confirmed = signal_workspace_retirement(request, workspace_id)   # fan-out barrier
    scrub_workspace_bytes(workspace_id, Path(settings.workspaces_dir))
    return Response(status_code=204 if confirmed else 202)
```

## Contract (review #7)

- **204** = deletion committed **and** all in-flight work confirmed stopped.
- **202 Accepted** = deletion committed; retirement unconfirmed, cleanup
  continues server-side (A6 retry on the idle-sweep tick). 202 is 2xx, so
  openapi-fetch sets no `error`, `unwrap` succeeds, and the existing
  `useDeleteWorkspace.onSuccess` closes the modal, invalidates `["workspaces"]`
  and navigates — **correct behavior with no frontend change**. (v3's 503 was
  wrong on both counts: it implies the request failed, and
  `WorkspaceSettingsModal.tsx:83` would show "Could not delete workspace" for a
  deletion that *did* commit, with a retry then 404-ing.)
- The turns tombstone is added **synchronously under the registry lock**, so the
  no-relaunch guarantee holds even when the barrier reports unconfirmed; 202
  means only that a *current* subprocess's confirmed death (or a hook) is in
  doubt. Log loudly + emit the workspace id for ops.
- Hooks run concurrently, isolated; barrier ≈ slowest hook, bounded by the shared
  `Deadline`. No new locks. `_retired` growth is bounded by deletes per process
  lifetime (cleared on restart; accepted).

## Testing

Grouped-dir `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider`.

**Slice A**
1. Registry: retire-before-launch → `claim_launch_or_wait` raises; retire with
   installed handle → terminated, lock released, entry gone; re-retire no-op.
2. **Pending-launch ack:** retire *while* `adapter.launch` is in progress →
   `retire_workspace` does **not** return until the launcher's teardown acks;
   `fulfill_pending_launch` → `installed=False`; waiter future raises
   `WorkspaceRetired`; fresh handle terminated (fake adapter records exactly one
   terminate); lock released.
3. **Unconfirmed propagation:** `adapter.terminate` raising / force-kill
   unreachable / process not reaped → `_teardown_entry(strict=True)` → `False` →
   `controller.retire_workspace` → `False`. And a `sweep_idle`/capacity evict over
   the same failure still does **not** raise (behavior preserved).
4. Adapter: hanging `disconnect()` → returns within the deadline, force-kill
   invoked, exit awaited; `kill()` failing → `TerminationUnconfirmed`.
5. **Processor:** retire after claim, before 2nd launch → **no send**, row
   finalized `failed` (not left `running`); retire between provenance and
   `send_turn` → same; retire mid-stream where the adapter emits the defensive
   `TurnDoneEvent` → finalized **`failed`, not success**; `in_flight == 0`.
6. A6: a failed strict teardown is retained and retried on the sweep tick, then
   cleared.

**Slice B**
7. Coordinator: all hooks confirm → `True`; one hook `False`/raising → `False`
   and the *other* hook still ran; **empty hooks → `False`**; constructing without
   a `REQUIRED` hook raises.
8. Bridge: loop on a **separate thread** (blocking `fut.result`; the M1 pattern
   from the session-revocation barrier test) — confirmed→`True`; **missing
   loop/coordinator → `False`**; hanging hook → `False` at the deadline, logged,
   no raise.
9. Collab primitive: live rooms on both artifacts → both evicted, clients closed
   **1008**, `can_write` False, no checkpoint, returns `True`; `acquire` for a
   retired workspace rejected (straddle).
10. Route: real `DELETE` (owner) with fakes — **no send after the response**;
    `204` on confirmed; **`202`** on an injected unconfirmed hook; existing
    soft-delete assertions (queued turns `cancelled`, `soft_deleted_at`, bytes
    scrubbed, collab room gone) hold. Frontend regression: `tsc` + `vitest`
    unchanged (no frontend edit).

## References (grounded)

Route/service `web/api/workspaces.py:1488`, `workspaces.py:82` (commit `:133`,
scrubs `:134-148`) · launches `processor.py:158/:276`, `controller.py:213` ·
finalize try `processor.py:295`, success `:338` · registry `:81/:164/:195/:286` ·
controller launch/cleanup `:105-192`, `terminate :225`, `interrupt :228` ·
adapter `claude_adapter.py:497` (defensive TurnDone), `:525/:536/:551/:559/:570` ·
collab `permissions.py reconcile_client` (resolve-error swallow), `room.py`
`evict/acquire`, straddle `collab_ws.py:117`, eviction proof
`test_collab_permissions.py:195` · frontend `api/settings.ts:76-87`,
`cockpit/WorkspaceSettingsModal.tsx:83`, `api/client.ts:34` (`unwrap`) · app state
`main.py:169/:235`.

## Slice C (follow-up)

Register a **passes** hook (coherence/evaluator/review/uncited) into the same
coordinator; needs workspace-scoped cancellation of the inline-awaited pass
coroutines. Separate spec → plan. Only then is the delete/cloud-streaming issue
fully closed.
