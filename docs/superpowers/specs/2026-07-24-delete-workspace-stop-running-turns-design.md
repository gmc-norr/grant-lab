# Delete-Workspace Stops Running Turns — Design

**Status:** approved (brainstorm 2026-07-24)
**Type:** backend-only behavior fix, no migration
**Related audit:** Codex whole-project audit 2026-07-07, adjacent finding #2 (see memory `project_collab_flip_d4_security_prereqs`).

## Problem

`soft_delete_workspace` (`src/call_manager/workspaces.py:82`) marks the workspace
`soft_deleted_at`, cancels active background jobs, marks **queued** turns
`cancelled`, commits, then scrubs attachment/reference bytes. The drain tick
(`turns/drain.py`) additionally excludes soft-deleted workspaces, so no *new*
turn dispatches after deletion.

But a turn already **running** at delete time is untouched: its
`process_turn_once` coroutine is mid-`controller.send_turn(...)`, and the SDK
subprocess keeps streaming the (confidential) proposal transcript to Anthropic
until the turn finishes naturally or hits `turn_max_seconds`. Deleting a
workspace should stop that promptly.

## Goal / success criteria

On `DELETE /api/workspaces/{id}`, a turn actively streaming to Anthropic is torn
down **before** the `204` is returned (blocking, bounded), reusing the existing
per-workspace `interrupt → terminate` teardown. The terminated turn finalizes
cleanly (`failed`, `in_flight` released, no double-finalize). The delete route
never fails because teardown couldn't be scheduled or timed out.

## Non-goals

- No change to the queued-turn cancellation (`cancelled`) already in place.
- No cooperative per-event soft-delete check inside `process_turn_once` (the
  bounded terminate is the guarantee; a per-event DB check is redundant — YAGNI).
- No new turn-outcome status: the interrupted running turn records `failed` via
  the existing `_finalize_failed` path (accepted — the workspace is `404`, so the
  chat/outcome rows are dead-on-arrival, same as the `cancelled` queued rows).
- No migration, no schema change, no frontend change.

## Architecture

**Keep the delete service pure.** `soft_delete_workspace` stays a pure DB
operation (no controller/app-loop access). A new bridge orchestrates the stop
from the **route**, which already holds `request.app.state.workspace_controller`
(`main.py:169`) and `request.app.state.app_loop` (`main.py:235`) and already
bridges to the loop via `signal_from_request`.

**New module** `src/call_manager/proposal_assist/turns/termination.py`:

```python
import asyncio
import concurrent.futures
import logging

logger = logging.getLogger(__name__)

# Bounded ceiling on how long DELETE blocks on the teardown. Also a liveness
# bound: the barrier awaits interrupt+terminate on the app loop; on timeout the
# route returns and the teardown continues on the loop (degrade to eventual).
_TURN_TERMINATION_BARRIER_SECONDS = 5.0
# Graceful-interrupt sub-bound, mirroring drain.py's timeout path.
_INTERRUPT_TIMEOUT_SECONDS = 2.0


async def _terminate_running_turn(controller, workspace_id) -> None:
    """Stop a workspace's in-flight turn: best-effort graceful interrupt (bounded)
    then the guaranteed terminate backstop — byte-for-byte the drain-timeout
    teardown (turns/drain.py). interrupt is advisory; terminate frees the handle
    + advisory lock and severs the SDK subprocess (which stops the Anthropic
    stream). At most one in-flight turn per workspace, so this is unambiguous."""
    try:
        await asyncio.wait_for(
            controller.interrupt(workspace_id), timeout=_INTERRUPT_TIMEOUT_SECONDS
        )
    except Exception:
        # TimeoutError or any interrupt failure — fall straight through to the
        # terminate backstop (expected best-effort path; not error-level).
        logger.debug(
            "signal_turn_termination: graceful interrupt for workspace %s "
            "failed/timed out; falling back to terminate", workspace_id,
        )
    await controller.terminate(workspace_id)


def signal_turn_termination(request, workspace_id) -> None:
    """Route-side (SYNC): schedule the running-turn teardown on the app loop and
    BLOCK (bounded) until it completes — "no more streaming to Anthropic after the
    204". NEVER raises into the route: a missing loop/controller, a schedule
    failure, a timeout, or a coroutine error degrades to a logged warning (the
    workspace is already durably soft-deleted; this is the live-subprocess
    guarantee, best-effort within its bound)."""
    try:
        loop = getattr(request.app.state, "app_loop", None)
        controller = getattr(request.app.state, "workspace_controller", None)
        if loop is None or controller is None:
            return
        coro = _terminate_running_turn(controller, workspace_id)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()   # never-awaited coroutine → close before re-raising
            raise
        try:
            fut.result(timeout=_TURN_TERMINATION_BARRIER_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "turns.termination_barrier_timeout",
                extra={"event": "turn_termination_barrier_timeout",
                       "workspace_id": str(workspace_id)},
            )   # teardown continues on the loop; degrade to eventual
    except Exception:
        logger.warning(
            "turns.signal_turn_termination_failed",
            extra={"event": "turn_termination_failed",
                   "workspace_id": str(workspace_id)},
            exc_info=True,
        )
```

This is structurally identical to `signal_user_revocation`
(`collab/permissions.py`, shipped 2026-07-24) — same sync→loop bridge, bounded
strong barrier, never-raise-into-route.

## Data flow

`DELETE /api/workspaces/{id}` (`web/api/workspaces.py:1488`,
`soft_delete_workspace_api`) becomes:

```python
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        soft_delete_workspace(                      # commits internally
            session, workspace_id=ws.id, workspaces_dir=Path(settings.workspaces_dir)
        )
    signal_turn_termination(request, workspace_id)  # NEW: stop a running turn (bounded)
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft", "spec"])
    return Response(status_code=204)
```

Ordering is deliberate: `soft_delete_workspace` commits FIRST (durable "deleted"
state, matching the existing commit-first invariant), the DB session **closes**,
THEN the barrier runs (so no pooled connection is held across the blocking
`fut.result`). The teardown runs after the queued-turn cancellation is durable.

## Interaction with the live turn (correctness)

`controller.terminate(ws)` → `registry.terminate` (`drafting/registry.py:286`)
pops the entry under `registry._lock`, `await adapter.terminate(handle)` (kills
the subprocess), then releases the advisory lock + closes the lock session.

The live `process_turn_once` is iterating `async for event in
controller.send_turn(...)` (`turns/processor.py`). When the subprocess dies the
stream read raises → caught by `except SDKTurnError` / `except BaseException` →
`_finalize_failed` (turn `running → failed`). `send_turn`'s `finally` calls
`decrement_in_flight(ws)` — a safe no-op once the entry is popped.

- **No double-finalize:** `soft_delete_workspace` does NOT touch the running row,
  so `process_turn_once` is the sole finalizer — no running-unique-index conflict.
- **No double-terminate:** the route's terminate pops the entry; any later
  terminate (lifespan teardown) is a no-op (`entry is None → return`).
- **No lock leak:** terminate releases the advisory lock in its `finally`; the
  coroutine never touches `lock_session` directly.

This is why interrupt→terminate composes cleanly even though — unlike the drain
timeout path — the turn coroutine is NOT cancelled first (the route is outside
the drain loop and cannot `wait_for`-cancel just that turn; `terminate(ws)` is
the per-workspace lever instead).

## Error handling / threading / constraints

- `signal_turn_termination` NEVER raises into the route. Missing loop/controller
  → silent no-op (e.g. headless/test without lifespan). Schedule failure /
  coroutine error / timeout → logged warning; the `204` still returns.
- Barrier timeout is a security bound (streaming stopped before `204`) AND a
  liveness bound (route worker releases after it; the loop-side teardown
  continues to completion).
- No new locks; reuses `registry._lock` transitively via `terminate`. Runs on the
  app loop, off the route's threadpool worker — same topology as
  `signal_permission_change` / `signal_user_revocation`.
- Backend-only; **no migration**; no frontend change.
- Single-instance constraint holds: the in-process registry is the source of
  truth for the live handle/subprocess.

## Testing

Backend tests run `EMBEDDING_MODEL=bge-m3 uv run pytest <paths>
-p no:cacheprovider`, grouped-dir (never whole-suite). New tests under
`tests/proposal_assist/turns/` + a route test under `tests/`.

1. **Bridge unit** (`test_turn_termination.py`, new) — a `FakeController`
   recording `interrupt`/`terminate` calls, a `_FakeRequest` exposing
   `app.state.app_loop` + `workspace_controller`. The blocking `fut.result` needs
   the loop on a **separate thread** (production topology; same M1 pattern as the
   session-revocation barrier test):
   - happy path: `signal_turn_termination` blocks until done; `interrupt` then
     `terminate` were called (order asserted), both for the target ws.
   - a `FakeController` whose `interrupt` **raises** → `terminate` is still called
     (best-effort interrupt).
   - no loop / no controller → no-op, no raise.
   - (optional) a `terminate` that hangs → `fut.result` times out ≈ barrier
     seconds, logs the warning, does not raise.
2. **Live-turn finalize** (extend the processor tests) — a fake adapter whose
   stream blocks; call `terminate(ws)` mid-stream; assert the turn finalizes
   `failed` and `registry` `in_flight == 0` (no leak, no double-finalize). Mirror
   the existing drain-timeout processor test harness.
3. **Route wiring** (`tests/test_api_workspaces*` or a new
   `test_api_workspace_delete_stops_turns.py`) — patch
   `call_manager.web.api.workspaces.signal_turn_termination`; `DELETE
   /api/workspaces/{id}` (owner) → assert it was called once with the workspace
   id, still returns `204`, and the existing soft-delete assertions (queued turns
   `cancelled`, `soft_deleted_at` set) still hold.

## Acceptance gates

- Grouped backend sweep: `tests/proposal_assist/turns`, the new route test, the
  workspaces route suite, `tests/test_api_spec.py`.
- `uv run ruff check .` clean.
- Diff-scope: 1 new module + route edit + tests; no migration, no frontend.

## References (grounded)

- Route: `web/api/workspaces.py:1488` `soft_delete_workspace_api`.
- Service: `workspaces.py:82` `soft_delete_workspace` (pure DB, commits).
- Controller: `drafting/controller.py:225` `terminate`, `:228` `interrupt`.
- Registry: `drafting/registry.py:286` `terminate`; `:273` `decrement_in_flight`.
- Processor finalize: `turns/processor.py` `send_turn` loop → `_finalize_failed`.
- Drain timeout precedent (interrupt 2s → terminate): `turns/drain.py`.
- App state: `main.py:169` `workspace_controller`, `:235` `app_loop`.
- Bridge precedent: `collab/permissions.py` `signal_user_revocation`,
  `signal_from_request`.
