# Delete-Workspace Stops Running Turns — Design (v2, post-review)

**Status:** approved direction (brainstorm 2026-07-24); reworked after a Codex
review that found the v1 barrier did not establish its stated guarantee. This v2
adopts the reviewer's preferred shape: **commit → durably retire the workspace in
the controller so no pending/future launch survives → bounded terminate with an
adapter-level force-kill → scrub files → return 204 only after confirmed
shutdown.**
**Type:** backend-only behavior fix, no migration.
**Scope decision:** interactive turns only (this slice). The four background pass
types are an explicit non-goal here — see §9 / Slice B.
**Related audit:** Codex whole-project audit 2026-07-07, adjacent finding #2
(memory `project_collab_flip_d4_security_prereqs`).

## Problem

`soft_delete_workspace` (`src/call_manager/workspaces.py:82`) stamps
`soft_deleted_at`, cancels background jobs, marks **queued** turns `cancelled`,
commits, then scrubs bytes. The drain tick excludes soft-deleted workspaces, so
no *new* turn dispatches. But a turn already **running** keeps its SDK subprocess
streaming the (confidential) transcript to Anthropic. Three defects make a naïve
"terminate on delete" insufficient (all verified against code):

1. **Relaunch race.** `process_turn_once` launches three times — before claim
   (`processor.py:158`), after claim (`processor.py:276`), and `send_turn` calls
   `launch` internally (`controller.py:213`). `launch` is get-or-create. A
   one-shot `terminate()` between any two lets the processor recreate the handle
   and send the prompt afterward. Additionally `registry.terminate` only pops
   `_entries`; an in-flight `_pending_launches` entry (`registry.py:81` →
   `:195 fulfill_pending_launch`) installs a fresh handle *after* terminate ran.
2. **Terminate is unbounded.** `adapter.terminate` `await client.disconnect()`
   (`claude_adapter.py:536`) has no deadline; `_force_kill_subprocess` is reached
   only if disconnect *raises* (`:537`), not if it *hangs*. An outer
   `fut.result(timeout)` frees the route worker but leaves the OS process alive
   and the advisory lock pinned.
3. **Ordering.** `soft_delete_workspace` commits (`workspaces.py:133`) then does
   two synchronous FS scrubs before returning, so a barrier placed after the
   service call runs *after* the scrub — streaming continues throughout cleanup.

## Goal / success criteria

On `DELETE /api/workspaces/{id}`:
- The workspace is **durably retired** in the controller so no pending or future
  `launch()` can install a live handle (closes the relaunch race).
- Any installed SDK handle is **terminated within a bounded time**, with an
  adapter-level force-kill fallback if `disconnect()` hangs.
- The route returns **204 only after retirement + termination are confirmed**;
  on the rare unconfirmed case it returns **503** (see §7).
- The delete route never raises because teardown couldn't be scheduled.

The guarantee is "**no more streaming to Anthropic, and no relaunch possible,
before 204**." Terminal `TurnJob` state (`failed`) is **eventual** — the running
`process_turn_once` records it after its stream dies (§6).

## Non-goals

- **The four background pass types** (`coherence` / `evaluator` / `review` /
  `uncited_claims`). A running pass coroutine also streams to Anthropic and
  survives delete (its DB row is marked terminal, but the inline-awaited
  coroutine keeps going — `coherence/pass_runner.py:73`,
  `evaluator/pass_runner.py:90`, `review/pass_runner.py:108`,
  `uncited_claims/pass_runner.py:111`). They have **no controller/registry
  cancellation surface** (each runs inline inside `drain_<type>_queue_tick` with
  no per-workspace task handle), so stopping a running one needs new
  workspace-scoped task-cancellation infrastructure. **Out of scope here → Slice
  B.** This slice does **not** claim the broader delete/cloud-streaming issue is
  closed.
- No cooperative per-event soft-delete check inside `process_turn_once` — the
  retirement + bounded terminate is the guarantee.
- No new `TurnJob` outcome status; the retired running turn records `failed` via
  the existing `_finalize_failed` path (accepted — the workspace is `404`, rows
  are dead-on-arrival, same as the `cancelled` queued rows).
- No migration, no schema change, no frontend change (but see §7 on the 503 path).

## Architecture / components

### C1 — `registry.retire_workspace` + tombstone (`drafting/registry.py`)

Add `self._retired: set[uuid.UUID] = set()` (guarded by `self._lock`) and a new
`WorkspaceRetired(workspace_id)` exception (in the drafting errors module,
alongside `DraftingAtCapacity` / `LockNotAcquired`).

- **`claim_launch_or_wait`** — first statement under `self._lock`:
  `if workspace_id in self._retired: raise WorkspaceRetired(workspace_id)`.
  Rejects every new launcher AND new waiter for a retired workspace.
- **`fulfill_pending_launch`** — return `bool` (`installed`). Under the lock,
  after popping the future: if `workspace_id in self._retired`, do **not** install
  the entry (`installed = False`); else install as today (`installed = True`).
  Outside the lock: on `installed` resolve the future with the handle; otherwise
  resolve it with `WorkspaceRetired` (so waiters raise, never hang). Return
  `installed`.
- **`retire_workspace(workspace_id)`** (new, async):
  ```python
  async with self._lock:
      self._retired.add(workspace_id)
      entry = self._entries.pop(workspace_id, None)
  if entry is not None:
      await self._teardown_entry(workspace_id, entry, context="retire")
  ```
  A launcher currently mid-`adapter.launch` is handled by `fulfill_pending_launch`
  returning `installed=False` (below) — `retire_workspace` does not need to touch
  `_pending_launches`. Idempotent (re-retire is a no-op teardown).

### C2 — `controller.launch` + `controller.retire_workspace` (`drafting/controller.py`)

- `launch`: change the fulfill call to honor the return:
  ```python
  installed = await self._registry.fulfill_pending_launch(
      workspace_id, handle, lock_session=lock_session,
  )
  handed_off = installed
  if not installed:
      raise WorkspaceRetired(workspace_id)  # retired mid-launch → fall to except
  ```
  With `handed_off=False`, the existing `except BaseException` block
  (`controller.py:178-190`) tears down the fresh handle (terminate → release lock
  → close session) and calls `fail_pending_launch` (a no-op — the future was
  already popped+resolved by fulfill). One teardown, reusing the proven cleanup.
  `WorkspaceRetired` then propagates to `send_turn` → `process_turn_once`, which
  finalizes the turn `failed` without sending.
- New `async def retire_workspace(self, workspace_id)`:
  ```python
  with contextlib.suppress(Exception):
      await asyncio.wait_for(self.interrupt(workspace_id), timeout=_INTERRUPT_TIMEOUT_SECONDS)
  await self._registry.retire_workspace(workspace_id)
  ```
  Best-effort graceful interrupt (bounded), then the durable tombstone + bounded
  terminate. `interrupt` runs while the handle is still installed.

### C3 — adapter-level bounded terminate + force-kill (`drafting/claude_adapter.py`)

Wrap the disconnect in a deadline so a *hung* disconnect also force-kills:
```python
try:
    await asyncio.wait_for(client.disconnect(), timeout=_DISCONNECT_DEADLINE_SECONDS)
except Exception:  # includes asyncio.TimeoutError (== TimeoutError, 3.11+)
    logger.warning("SDK client disconnect failed/timed out for workspace %s; "
                   "force-killing subprocess", handle.workspace_id, exc_info=True)
    self._force_kill_subprocess(client, handle.workspace_id)
```
`_force_kill_subprocess` already reaches `client._transport._process.kill()`
(`claude_adapter.py:551`). This makes `adapter.terminate` — and therefore
`_teardown_entry` and `retire_workspace` — genuinely bounded. New constant
`_DISCONNECT_DEADLINE_SECONDS` (≈3.0).

### C4 — service split (`src/call_manager/workspaces.py`)

Split `soft_delete_workspace` so the barrier runs between the DB commit and the
FS scrub:
- `soft_delete_workspace_db(session, *, workspace_id)` — stamp `soft_deleted_at`,
  cancel background jobs, cancel queued turns, `commit`. (The current body up to
  and including `session.commit()`.)
- `scrub_workspace_bytes(workspace_id, workspaces_dir)` — the two best-effort FS
  scrubs (attachment + reference bytes). No session.

Keep a thin `soft_delete_workspace` wrapper (`_db` then `_bytes`) for any
non-route caller/tests that want the combined behavior, so existing call sites
don't break.

### C5 — `signal_turn_termination` bridge + route (`turns/termination.py` new; `web/api/workspaces.py:1488`)

```python
_TURN_TERMINATION_BARRIER_SECONDS = 5.0
_INTERRUPT_TIMEOUT_SECONDS = 2.0  # (lives in controller/termination as used)

def signal_turn_termination(request, workspace_id) -> bool:
    """SYNC route bridge: schedule controller.retire_workspace on the app loop and
    BLOCK (bounded) until it completes. Returns True when retirement+termination
    are CONFIRMED, False when unconfirmed (missing loop→vacuously True: nothing to
    retire; schedule failure / coroutine error / timeout → False). NEVER raises."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        controller = getattr(request.app.state, "workspace_controller", None)
        if loop is None or controller is None:
            return True  # no live controller (headless/test) → nothing streaming
        coro = controller.retire_workspace(workspace_id)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()
            return False
        try:
            fut.result(timeout=_TURN_TERMINATION_BARRIER_SECONDS)
            return True
        except concurrent.futures.TimeoutError:
            logger.warning("turns.termination_barrier_timeout", extra={...})
            return False  # tombstone likely installed (no relaunch); terminate unconfirmed
    except Exception:
        logger.warning("turns.signal_turn_termination_failed", extra={...}, exc_info=True)
        return False
```

Route (`soft_delete_workspace_api`) becomes:
```python
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        soft_delete_workspace_db(session, workspace_id=ws.id)      # commits
    confirmed = signal_turn_termination(request, workspace_id)     # retire+terminate barrier
    scrub_workspace_bytes(workspace_id, Path(settings.workspaces_dir))  # best-effort FS
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft", "spec"])
    return Response(status_code=204 if confirmed else 503)
```
Ordering matches the reviewer's preferred shape: DB commit (durable "deleted") →
retire+terminate barrier (stop streaming, before any FS work) → best-effort FS
scrub → response.

## Data flow / race closure

- **Retire before launch #1 or between launches:** `claim_launch_or_wait` raises
  `WorkspaceRetired` → `process_turn_once`'s `launch` raises → finalized `failed`,
  **no send**.
- **Retire while a launcher is mid-`adapter.launch`:** `fulfill_pending_launch`
  returns `installed=False` → `controller.launch` raises `WorkspaceRetired` →
  its `except` tears down the fresh handle + releases the lock; the pending
  future is resolved with `WorkspaceRetired` so waiters raise. **No handle
  survives.**
- **Retire while a turn is streaming (installed handle):** `retire_workspace`
  pops `_entries` + `_teardown_entry` (bounded terminate → force-kill) → the live
  `send_turn` stream read raises → `process_turn_once` `_finalize_failed`;
  `send_turn`'s `finally` `decrement_in_flight` is a no-op (entry popped). **No
  double-finalize** (soft-delete does not touch the running row; the coroutine is
  the sole finalizer) and **no double-teardown** (a later lifespan `terminate` is
  a no-op — entry gone).
- **No live turn / no handle:** `retire_workspace` pops nothing, adds the
  tombstone, returns immediately → barrier confirms instantly → 204 (fast no-op,
  like the no-live-socket path in `signal_user_revocation`).

## Error handling / contract / threading

- `signal_turn_termination` **never raises into the route**; it returns a
  `confirmed` bool. Runs the retire on the app loop, off the route's threadpool
  worker (same topology as `signal_permission_change` / `signal_user_revocation`).
- **Strong contract:** 204 iff `confirmed`. On `confirmed=False` (schedule
  failure / coroutine error / barrier timeout) → **503**. The tombstone is added
  to `_retired` *synchronously under the registry lock at the top of*
  `retire_workspace`, so even on a barrier timeout the no-relaunch guarantee is
  in place once retirement began; only the current subprocess's confirmed death
  is in doubt (adapter force-kill continues on the loop). 503 surfaces that for
  ops; the delete itself is durably committed.
- **Idempotency note:** the workspace is already soft-deleted, so a client retry
  of `DELETE` returns 404 (`require_owner` filters `soft_deleted_at`) — the 503
  is an anomaly signal, not a ret/ry-to-complete step. The SPA's delete flow
  treats any non-204 as an error + refetches; the refetch 404s the row, so the
  list self-heals. **No frontend change required; confirm this in the plan.**
- **`_retired` growth:** bounded by deletes per process lifetime (cleared on
  restart). Accepted; a cap/TTL is a possible future refinement, not needed now.
- No new locks (reuses `registry._lock`). Single-instance constraint holds.

## Testing

Backend tests: `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider`,
grouped-dir. New tests under `tests/proposal_assist/drafting/` (registry +
controller retire), `tests/proposal_assist/turns/` (bridge), `tests/` (route).

1. **Registry/controller retirement (unit):**
   - retire before launch → `claim_launch_or_wait` raises `WorkspaceRetired`;
     `controller.launch` raises it; no entry installed.
   - retire **during a pending launch** → `fulfill_pending_launch` returns
     `installed=False`, resolves the waiter's future with `WorkspaceRetired`, and
     `controller.launch` tears down the fresh handle (fake adapter records one
     `terminate`, lock released).
   - retire with an installed handle → handle terminated, lock released, entry
     gone; re-retire is a no-op.
2. **Processor race (integration, mirrors the drain-timeout processor harness):**
   - retire **after the running claim, before the second launch** → the turn
     finalizes `failed`, **no `send_turn`** occurs (fake adapter records zero
     sends).
   - retire **between provenance stamping and `send_turn`'s internal launch** →
     same: no send, finalized `failed`.
   - retire **while streaming** → subprocess terminated; the turn finalizes
     `failed` **eventually** (poll), `in_flight == 0`, no double-finalize.
3. **Adapter bounded terminate (unit):** a fake client whose `disconnect()`
   **hangs** → `terminate` returns within ≈`_DISCONNECT_DEADLINE_SECONDS` and
   `_force_kill_subprocess` is invoked; lock released afterward.
4. **Bridge (unit):** loop on a **separate thread** (blocking `fut.result`; same
   M1 pattern as the session-revocation barrier test) — happy path returns
   `True` and retirement ran; no loop/controller → `True` (vacuous); a
   `retire_workspace` that hangs → returns `False` ≈ barrier seconds, logs, no
   raise.
5. **Route (integration):** real `DELETE /api/workspaces/{id}` (owner) with a
   fake controller/adapter — assert **no send after the response**, `204` on
   confirmed, and `503` on an injected unconfirmed teardown; existing soft-delete
   assertions (queued turns `cancelled`, `soft_deleted_at` set, bytes scrubbed)
   still hold. Also a patched-bridge variant asserting it's called once with the
   workspace id (fast, DB-only).

## Acceptance gates

- Grouped backend sweep: `tests/proposal_assist/drafting`,
  `tests/proposal_assist/turns`, the route test, the workspaces route suite,
  `tests/test_api_spec.py`.
- `uv run ruff check .` clean. Frontend `tsc` + `vitest` unchanged (regression).
- Diff-scope: registry + controller + adapter + service split + new bridge module
  + route edit + tests; **no migration, no frontend**.

## References (grounded)

- Route/service: `web/api/workspaces.py:1488`; `workspaces.py:82` (commit `:133`,
  scrubs `:134-148`).
- Launch points: `turns/processor.py:158`, `:276`; `controller.py:213`
  (`send_turn` → `launch`).
- Registry: `registry.py:81` `claim_launch_or_wait`, `:164` `_teardown_entry`,
  `:195` `fulfill_pending_launch`, `:286` `terminate`.
- Controller launch/cleanup: `controller.py:105-192`; `terminate` `:225`,
  `interrupt` `:228`.
- Adapter: `claude_adapter.py:525` `terminate`, `:536` `disconnect`, `:551`
  `_force_kill_subprocess`.
- App state: `main.py:169` `workspace_controller`, `:235` `app_loop`.
- Bridge precedent: `collab/permissions.py` `signal_user_revocation`,
  `signal_from_request`.

## §9 Slice B (follow-up, not this spec)

Stop the four background pass types on delete. Requires workspace-scoped
cancellation of the inline-awaited pass coroutines (a per-workspace running-task
registry, or cooperative soft-delete checks in each `drain_<type>_queue_tick` /
`pass_runner`). Separate spec → plan → implement. Until then, the delete flow
does **not** guarantee a running background pass stops streaming.
