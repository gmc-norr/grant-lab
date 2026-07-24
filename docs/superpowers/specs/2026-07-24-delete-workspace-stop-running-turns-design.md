# Workspace Retirement on Delete — Design (v3)

**Status:** approved direction (brainstorm 2026-07-24). v1 was a turn-only
barrier; a Codex review showed it didn't hold its guarantee (v2 fixed the turn
mechanics); a follow-up architecture discussion reframed the whole thing around a
**thin workspace-retirement coordinator** so in-flight teardown stops being an
ad-hoc per-subsystem patch. This v3 is that design; the turn-teardown mechanics
from v2 survive intact as the coordinator's first hook.
**Type:** backend-only, no migration.
**Scope:** the coordinator + two initial hooks (**turns**, **collab**). The four
background pass types are the documented **next hook** (Slice B), not built here.

## Problem

Deleting a workspace must stop everything currently **alive** for it. The DB
tombstone `soft_deleted_at` already handles *new* work uniformly — every
job-claim layer (turns drain + all four pass queues) excludes soft-deleted
workspaces (`workspaces.py` "each claim_next also excludes soft-deleted"). The
gap is **in-flight** work:

- **Turns:** a running `process_turn_once` keeps its SDK subprocess streaming the
  confidential transcript to Anthropic. It is not stopped at all today.
- **Collab:** a live editing room *is* torn down today — but only as an implicit
  side effect of the delete route's `signal_from_request` call (soft-delete →
  `resolve_access` not-a-member → reconcile closes every client → room evicted;
  `test_refresh_soft_delete_revokes_all_without_checkpoint`).
- **Passes** (coherence/evaluator/review/uncited): a running pass coroutine keeps
  streaming; no cancellation surface exists (Slice B).

So in-flight teardown is a cross-cutting concern with **no single owner** — each
subsystem is a separately-named, separately-shaped call in the delete route
(`signal_from_request` for collab; nothing for turns/passes). Adding a subsystem
means remembering a new bespoke teardown and re-deriving the response contract.
That is the "patching" smell. v3 gives it one owner.

Two turn-specific defects (verified against code) must also be fixed for the
turns hook to actually work — see C2/C3.

## Goal

- A single **`retire_workspace` fan-out**: one entry point that invokes a uniform
  set of async `retire(ws)` hooks under **one bounded barrier**, producing one
  `confirmed` result. Subsystems register a hook; the route calls one thing.
- The **turns hook** durably stops a running turn (no relaunch, bounded
  terminate) — the actual confidentiality fix.
- The **collab hook** subsumes today's implicit teardown (same `refresh_permissions`
  mechanism, now awaited under the barrier; the route drops its separate
  `signal_from_request`).
- **Strong contract:** `DELETE` returns **204 only after all hooks confirm**, else
  **503**. Ordering: DB commit → retirement barrier → best-effort FS scrub →
  response.
- Terminal `TurnJob` state (`failed`) is **eventual** (the running coroutine
  records it once its stream dies); the barrier guarantees "no streaming, no
  relaunch," not the row flip.

## Non-goals

- **Passes = Slice B.** They register a hook into this same coordinator later;
  building their cancellation surface (per-workspace task cancellation for the
  inline-awaited `drain_<type>_queue_tick` coroutines) is a separate spec. Until
  then this design does **not** claim a running background pass stops.
- No per-workspace supervisor / structured-concurrency rewrite (considered and
  rejected as over-engineered for a single-instance in-process app).
- No cooperative per-event soft-delete check inside runners.
- No new `TurnJob` status (retired running turn → existing `_finalize_failed`).
- No migration, no schema, no frontend change (see Contract on the 503 path).

## Architecture / components

### C1 — the retirement coordinator (`proposal_assist/lifecycle/retirement.py`, new)

```python
RetireHook = Callable[[uuid.UUID], Awaitable[None]]

class WorkspaceRetirement:
    """Owns teardown of in-flight, workspace-scoped async work. Subsystems
    register a retire(ws) hook (wired in lifespan). retire() fans out to every
    hook CONCURRENTLY and isolated — one hook's failure never skips another —
    and reports whether ALL confirmed. The DB tombstone remains the 'no new work'
    gate; this handles in-flight work."""
    def __init__(self, hooks: list[tuple[str, RetireHook]]):
        self._hooks = hooks

    async def retire(self, workspace_id) -> bool:
        results = await asyncio.gather(
            *(self._run_one(n, h, workspace_id) for n, h in self._hooks)
        )
        return all(results)

    async def _run_one(self, name, hook, workspace_id) -> bool:
        try:
            await hook(workspace_id)
            return True
        except Exception:
            logger.warning("retire hook %s failed for %s", name, workspace_id, exc_info=True)
            return False
```

Sync route bridge (same threadpool→loop pattern as `signal_user_revocation`):

```python
_RETIREMENT_BARRIER_SECONDS = 5.0

def signal_workspace_retirement(request, workspace_id) -> bool:
    """Schedule the retirement fan-out on the app loop and BLOCK (bounded) until
    it completes. Returns confirmed. NEVER raises into the route. Missing
    coordinator/loop (headless/test) → True (nothing live to retire)."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        retirement = getattr(request.app.state, "workspace_retirement", None)
        if loop is None or retirement is None:
            return True
        coro = retirement.retire(workspace_id)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close(); return False
        try:
            return fut.result(timeout=_RETIREMENT_BARRIER_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning("workspace.retirement_barrier_timeout", extra={...})
            return False
    except Exception:
        logger.warning("workspace.retirement_failed", extra={...}, exc_info=True)
        return False
```

### C2 — turns hook: durable retirement (`drafting/registry.py`, `drafting/controller.py`)

The turns hook is `controller.retire_workspace`. A one-shot `terminate()` is
insufficient: `process_turn_once` launches three times — before claim
(`processor.py:158`), after claim (`:276`), and `send_turn`→`launch`
(`controller.py:213`), all get-or-create — so terminating between two lets the
processor recreate the handle and send. And `registry.terminate` pops only
`_entries`; an in-flight `_pending_launches` (`registry.py:81`→`:195`) installs a
handle *after* terminate. Fix = a durable tombstone:

- Add `self._retired: set[uuid.UUID]` (under `self._lock`) + `WorkspaceRetired`
  exception (drafting errors module).
- `claim_launch_or_wait`: first under-lock statement `if ws in self._retired:
  raise WorkspaceRetired(ws)` — rejects every new launcher and waiter.
- `fulfill_pending_launch` → return `bool installed`: if `ws in self._retired`,
  do **not** install the entry (`installed=False`) and resolve the future with
  `WorkspaceRetired` (waiters raise); else install + resolve with the handle.
- `controller.launch`: `installed = await fulfill_pending_launch(...); handed_off
  = installed; if not installed: raise WorkspaceRetired(ws)` — the existing
  `except` (`controller.py:178-190`) tears the fresh handle down + releases the
  lock (one teardown, proven path).
- `registry.retire_workspace(ws)`: `async with self._lock: self._retired.add(ws);
  entry = self._entries.pop(ws, None)` then `if entry: await
  self._teardown_entry(ws, entry, context="retire")`. Idempotent.
- `controller.retire_workspace(ws)`: best-effort bounded `interrupt` then
  `registry.retire_workspace(ws)`:
  ```python
  with contextlib.suppress(Exception):
      await asyncio.wait_for(self.interrupt(ws), timeout=_INTERRUPT_TIMEOUT_SECONDS)
  await self._registry.retire_workspace(ws)
  ```

### C3 — bounded terminate + force-kill (`drafting/claude_adapter.py`)

`adapter.terminate` `await client.disconnect()` (`:536`) has no deadline;
`_force_kill_subprocess` (`:551`) is reached only if disconnect *raises* (`:537`),
not if it *hangs*. Wrap it:

```python
try:
    await asyncio.wait_for(client.disconnect(), timeout=_DISCONNECT_DEADLINE_SECONDS)
except Exception:  # incl. asyncio.TimeoutError (== TimeoutError, 3.11+)
    logger.warning("... disconnect failed/timed out; force-killing ...", exc_info=True)
    self._force_kill_subprocess(client, handle.workspace_id)
```

Makes `terminate` → `_teardown_entry` → `retire_workspace` genuinely bounded.
New `_DISCONNECT_DEADLINE_SECONDS` (≈3.0).

### C4 — service split (`src/call_manager/workspaces.py`)

So the barrier runs between DB commit and FS scrub:
- `soft_delete_workspace_db(session, *, workspace_id)` — the current body through
  `session.commit()`.
- `scrub_workspace_bytes(workspace_id, workspaces_dir)` — the two best-effort FS
  scrubs. Keep a thin `soft_delete_workspace = _db + _bytes` wrapper for existing
  non-route callers/tests.

### C5 — collab hook (`proposal_assist/collab/…`)

A thin hook that reuses the existing teardown, now awaited under the barrier:
```python
def make_collab_retire_hook(session_factory, workspaces_dir):
    async def _retire(ws):
        for artifact in ("draft", "spec"):
            await refresh_permissions(session_factory, workspaces_dir,
                                      workspace_id=ws, artifact=artifact)
    return _retire
```
`refresh_permissions` is already a no-op when no room is live and already
defensive (never raises), so this hook is safe + idempotent. It performs the
*same* eviction the route's `signal_from_request` triggers today — so the route
**drops** its `signal_from_request(...)` call (subsumed), trading fire-and-forget
for confirmed-under-barrier.

### C6 — wiring + route (`main.py` lifespan; `web/api/workspaces.py:1488`)

Lifespan builds the coordinator where the controller + `MANAGER` + factories
already exist, and stores it on app state:
```python
app.state.workspace_retirement = WorkspaceRetirement(hooks=[
    ("turns",  workspace_controller.retire_workspace),
    ("collab", make_collab_retire_hook(session_factory, settings.workspaces_dir)),
])
```
Route:
```python
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        soft_delete_workspace_db(session, workspace_id=ws.id)      # commits
    confirmed = signal_workspace_retirement(request, workspace_id)  # fan-out barrier
    scrub_workspace_bytes(workspace_id, Path(settings.workspaces_dir))
    return Response(status_code=204 if confirmed else 503)
```
(`signal_from_request` removed — the collab hook subsumes it.)

## Data flow / turn-race closure

- Retire before/between launches → `claim_launch_or_wait` raises `WorkspaceRetired`
  → `process_turn_once`'s launch raises → finalized `failed`, **no send**.
- Retire while a launcher is mid-`adapter.launch` → `fulfill_pending_launch`
  returns `installed=False` → `controller.launch` raises → its `except` tears the
  fresh handle down; waiters get `WorkspaceRetired`. **No handle survives.**
- Retire while streaming → `retire_workspace` pops `_entries` + bounded terminate
  → the live `send_turn` read raises → `_finalize_failed`; `decrement_in_flight`
  is a no-op (entry popped). No double-finalize (soft-delete never touches the
  running row) / no double-teardown (later lifespan terminate is a no-op).
- No live turn/handle → tombstone added, nothing to tear down, hook returns
  instantly. Collab hook likewise no-ops when no room is live. Fast-path 204.

## Contract / errors / threading

- `signal_workspace_retirement` never raises into the route; returns `confirmed`.
- **204 iff all hooks confirmed, else 503.** The turns tombstone is added
  synchronously under the registry lock, so "no relaunch" holds even on a barrier
  timeout; 503 means the *current* subprocess's confirmed death (or a hook) is in
  doubt. **Idempotency:** the workspace is already soft-deleted, so a client retry
  404s — the 503 is an anomaly signal, and the SPA's non-204 path refetches (404
  → the row vanishes, list self-heals). No frontend change; confirm in the plan.
- Hooks run **concurrently + isolated** (`gather`, each `_run_one` swallows→False):
  a collab failure never skips turn teardown; barrier ≈ slowest hook. `refresh_permissions`
  is already non-raising, so collab ~never forces a 503.
- Runs on the app loop off the route worker (same topology as the collab/turn
  bridges). No new locks (reuses `registry._lock`). `_retired` growth is bounded
  by deletes/process-lifetime (cleared on restart; accepted).

## Testing

Grouped-dir `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider`.

1. **Coordinator (unit):** fan-out runs all hooks concurrently; one hook raising
   → that hook `False`, others still run, `retire()` → `False`; all-ok → `True`.
2. **Bridge (unit):** loop on a **separate thread** (blocking `fut.result`; M1
   pattern from the session-revocation barrier test) — confirmed→True; no
   loop/coordinator→True; a hanging hook → False ≈ barrier seconds, logged, no
   raise.
3. **Turns registry/controller retirement (unit):** retire-before-launch raises;
   retire-during-pending-launch → `installed=False`, waiter gets `WorkspaceRetired`,
   fresh handle torn down; retire-with-installed-handle → terminated + lock
   released; re-retire no-op.
4. **Processor races (integration, drain-timeout harness):** retire after claim
   before 2nd launch → no `send_turn`, finalized `failed`; retire between
   provenance stamp and `send_turn` launch → same; retire while streaming →
   terminated, finalized `failed` **eventually** (poll), `in_flight==0`.
5. **Adapter (unit):** hanging `disconnect()` → `terminate` returns within
   ≈deadline, force-kill invoked, lock released.
6. **Collab hook (unit):** a soft-deleted workspace with a live room → hook awaits
   `refresh_permissions`, room evicted (reuses existing eviction assertions).
7. **Route (integration):** real `DELETE` (owner) with fake controller/adapter —
   **no send after the response**; `204` on confirmed, `503` on an injected
   unconfirmed hook; existing soft-delete assertions (queued turns `cancelled`,
   `soft_deleted_at` set, bytes scrubbed, collab room gone) still hold.

## Acceptance gates

Grouped sweep: `tests/proposal_assist/{drafting,turns,collab,lifecycle}`, the
route + workspaces suites, `tests/test_api_spec.py`. `ruff check .` clean.
Frontend `tsc`/`vitest` unchanged. Diff-scope: coordinator module + registry +
controller + adapter + service split + collab hook + lifespan + route + tests;
**no migration, no frontend**.

## References (grounded)

Route/service `web/api/workspaces.py:1488`, `workspaces.py:82` (commit `:133`,
scrubs `:134-148`) · launch points `processor.py:158/:276`, `controller.py:213` ·
registry `:81/:164/:195/:286` · controller launch/cleanup `:105-192`, `terminate
:225`, `interrupt :228` · adapter `claude_adapter.py:525/:536/:551` · collab
`permissions.py refresh_permissions`, `signal_from_request`; soft-delete eviction
proof `test_collab_permissions.py:195` · app state `main.py:169/:235`.

## §9 Slice B (follow-up)

Register a **passes** hook into this coordinator. Needs workspace-scoped
cancellation of the inline-awaited `coherence/evaluator/review/uncited` pass
coroutines (a per-workspace running-task registry, or cooperative aborts). Once
added, `retire_workspace` closes the whole delete/cloud-streaming issue. Separate
spec → plan.
