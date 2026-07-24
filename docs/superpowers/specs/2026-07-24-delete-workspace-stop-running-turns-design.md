# Workspace Retirement on Delete — Design (v5)

**Status:** approved direction. v5 follows a third Codex review (7 more findings,
all verified). Three rounds/14 findings all attacked the same thing — *proving*
in-process "confirmed death" across every teardown path. v5 **reframes**: make
the guarantees that can be **durable** actually durable (in the DB), keep
subprocess teardown **prompt but best-effort**, and add **reconciliation** so
every failure path self-heals. This is smaller than v4 and closes the same gaps.
**Type:** backend-only, no migration, **no frontend change, no OpenAPI change**.
**Delivery:** **Slice A** = durable core (fixes the confidentiality gap on its
own). **Slice B** = coordinator + promptness + collab. Passes = **Slice C**.

## Problem

Deleting a workspace must stop everything alive for it. `soft_deleted_at`
already stops *new* work (every claim layer excludes soft-deleted). The gaps:

- **Turns:** a running `process_turn_once` keeps streaming the confidential
  transcript to Anthropic; nothing stops it. Worse, `process_turn_once` launches
  three times (`processor.py:158`, `:276`, `controller.py:213`, all
  get-or-create), so a one-shot terminate can be immediately undone.
- **Collab:** a live room is evicted only as an implicit side effect of the
  route's `signal_from_request`.
- **Passes:** keep streaming; no cancellation surface (Slice C).

## Design principle (what changed in v5)

Three guarantee classes, each implemented where it can actually hold:

| Guarantee | Where it lives | Strength |
|---|---|---|
| No new work; no relaunch | DB tombstone + in-process tombstones | **Durable** |
| A running turn reaches a terminal row | **Same transaction as the delete** | **Durable, race-free** |
| The OS subprocess stops | prompt interrupt→terminate, bounded | **Best-effort** |
| Anything the above missed | **reconciliation sweep** | **Eventual, self-healing** |

We do **not** claim the HTTP response proves a subprocess died — that claim is
what generated 14 findings. We *do* guarantee: deletion is committed, no further
work can start or relaunch, the turn row is terminal, streaming is stopped
promptly in the common case, and any straggler is reaped by reconciliation.

## Contract

**`DELETE` always returns 204** (deletion committed). No 202/503 — which
dissolves the OpenAPI/`schema.gen.ts` change (P2-7) and the "202 that isn't
self-healing" problem (P1-6). No frontend change. Missing retirement wiring is a
**startup invariant** (fail loudly at boot), not a per-request signal.

---

# Slice A — durable core (independently fixes the gap)

### A1 — delete transaction terminalizes RUNNING turn rows (closes P1-4 durably)

`soft_delete_workspace_db` already cancels **queued** turns in the delete txn.
Extend it to also terminalize **running** rows via the same helper
(`record_terminal_outcome(..., expected_status="running",
raw_error="workspace soft-deleted", emit_chat=False)`, outcome `cancelled` —
owner-initiated; plan must confirm the helper accepts `expected_status="running"`).

Why this is the right fix: `_finalize_success` is already a **conditional**
`UPDATE ... WHERE status='running'` (`processor.py:446-452`) that documents
"must NOT resurrect it to 'completed'" and rolls back on `rowcount == 0`. So once
deletion terminalizes the row **in its own transaction**, both racing paths
contend on the same row in the database and the finalizer becomes a no-op — **no
in-process recheck, no TOCTOU window.** This *also* dissolves the earlier
"defensive `TurnDoneEvent` finalizes a deleted turn as success" hole
(`claude_adapter.py:497` → `:338`): it cannot flip an already-terminal row.
Side benefits: the partial unique index on running is freed, and the reaper never
sees the row.

### A2 — registry tombstone: no relaunch (`drafting/registry.py`, `controller.py`)

- `self._retired: set[uuid.UUID]` under `self._lock`; `WorkspaceRetired` exception.
- `claim_launch_or_wait`: first under-lock statement — `if ws in self._retired:
  raise WorkspaceRetired(ws)` → rejects new launchers **and** waiters, closing the
  three-launch-point relaunch race.
- `fulfill_pending_launch` → `bool installed`: if retired, do **not** install the
  entry and resolve the future with `WorkspaceRetired`; `controller.launch` sets
  `handed_off = installed` and raises `WorkspaceRetired` when not installed, so
  its existing `except` (`controller.py:178-190`) tears the fresh handle down.
  (That cleanup suppresses exceptions — **fine now**: reconciliation A4 is the
  backstop, so no ack/result plumbing is needed. Closes P1-1 by removing the
  claim rather than by adding bookkeeping.)
- `registry.retire_workspace(ws)`: add the tombstone under the lock, pop any
  entry, `_teardown_entry` it outside the lock (existing non-raising semantics —
  unchanged for capacity/sweep callers). No `strict` flag, no in-progress-teardown
  map, no retained-client retry (P1-2/P1-3 dissolve — reconciliation covers both
  the sweep/capacity window and any failed teardown).
- **In-process tombstones are an optimization, not the durable layer:** they are
  lost on restart, but the DB tombstone still blocks everything (drain excludes
  soft-deleted; `resolve_access` returns not-member).

### A3 — adapter: bounded disconnect + force-kill (`claude_adapter.py`)

Keep this from v4 — it is self-contained and genuinely valuable:
```python
try:
    await asyncio.wait_for(client.disconnect(), timeout=_DISCONNECT_DEADLINE_SECONDS)
except Exception:                     # incl. TimeoutError — a HANG, not only a raise
    logger.warning("... disconnect failed/timed out; force-killing ...", exc_info=True)
    self._force_kill_subprocess(client, handle.workspace_id)
```
`_force_kill_subprocess` returns `bool` **for logging/metrics only** (ops signal
when a process may have leaked) — not for a response contract. No
`TerminationUnconfirmed` propagation.

### A4 — reconciliation sweep (replaces all the confirmation bookkeeping)

Extend the existing idle-sweep scheduler tick with a second pass: **terminate any
live handle whose workspace is soft-deleted**, regardless of `in_flight` (unlike
the idle pass, a running turn on a deleted workspace is exactly the target). One
DB predicate + `_teardown_entry`.

This single mechanism self-heals **every** path the reviews found: launcher
cleanup failure (P1-1), the capacity-evict / idle-sweep pop-then-terminate window
(P1-2), a failed teardown with a discarded client (P1-3), missing loop /
scheduling failure (P1-6). Worst case, streaming stops within one tick instead of
instantly — and it needs no cross-path state tracking at all.

### A5 — processor: guarded region (`turns/processor.py`)

Extend the finalization `try` (`:295`) to cover the post-claim
`controller.launch` (`:276`) + `set_turn_provenance`, and catch `WorkspaceRetired`
→ `_finalize_failed`. Without this, `WorkspaceRetired` escapes and the row is left
`running` until the reaper. (With A1 the row is usually already terminal, making
the finalize a no-op — this keeps the code path honest and the logs clean.)

### Slice A acceptance

Fixes the confidentiality gap **without** the coordinator: the delete txn
terminalizes the row and blocks relaunch, and reconciliation reaps the subprocess
within a tick. `controller.retire_workspace(ws)` is directly testable.

---

# Slice B — coordinator, promptness, collab

### B1 — coordinator + bridge (`proposal_assist/lifecycle/retirement.py`, new)

The single owner for in-flight teardown the architecture discussion called for:
```python
RetireHook = Callable[[uuid.UUID], Awaitable[None]]

class WorkspaceRetirement:
    REQUIRED = ("turns", "collab")
    def __init__(self, hooks: dict[str, RetireHook]):
        missing = set(self.REQUIRED) - hooks.keys()
        if missing:                      # STARTUP invariant — fail at boot, loudly
            raise ValueError(f"retirement hooks missing: {sorted(missing)}")
        self._hooks = hooks

    async def retire(self, workspace_id) -> None:
        await asyncio.gather(*(self._run_one(n, h, workspace_id)
                               for n, h in self._hooks.items()))
```
`_run_one` logs and swallows a hook failure (isolated — one hook never skips
another); reconciliation is the backstop. Hooks return `None`: **no confirmation
plumbing**.

`signal_workspace_retirement(request, workspace_id) -> None`: the sync→loop
bridge (same pattern as `signal_user_revocation`), **bounded** (~5s) so streaming
has normally stopped before the 204, then returns regardless. Never raises. A
missing loop is logged at warning (the startup invariant is what prevents it in
production); reconciliation still covers it.

### B2 — collab retirement primitive, in `acquire` **and** `operate` (closes P1-5)

`refresh_permissions` is not a retirement primitive (on a resolve error
`reconcile_client` flips `can_write=False`, logs, returns — socket stays
registered, room stays live). Add to `RoomManager`:
- `self._retired_ws: set[uuid.UUID]`.
- **`acquire` AND `operate`** both reject a retired workspace — checked before
  registration **and re-checked after every out-of-lock await** (`operate`'s
  `await load()` at `room.py:491` is exactly the straddle
  `kernel_accept.py:71` can ride). `evicted=True` alone is insufficient, so the
  check must gate registration itself.
- `retire_workspace(ws)`: tombstone, then for `(ws,"draft")` and `(ws,"spec")` —
  set every `ClientState.can_write = False`, `room.evicted = True`, close each
  client with **1008** (terminal for the frontend; `evict()`'s 1011 would invite
  reconnect storms against a deleted workspace), remove the room. **No
  checkpoint** (workspace gone — matches existing soft-delete semantics).

The route's `signal_from_request` call is removed (subsumed).

### B3 — service split + route (`workspaces.py`, `web/api/workspaces.py:1488`)

- `soft_delete_workspace_db(session, *, workspace_id)` — through `session.commit()`
  (now including A1's running-row terminalization).
- `scrub_workspace_bytes(workspace_id, workspaces_dir)` — the two best-effort FS
  scrubs (`:134-148`). Thin `soft_delete_workspace = _db + _bytes` wrapper kept.
```python
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        soft_delete_workspace_db(session, workspace_id=ws.id)      # commits
    signal_workspace_retirement(request, workspace_id)             # prompt, bounded
    scrub_workspace_bytes(workspace_id, Path(settings.workspaces_dir))
    return Response(status_code=204)
```
Ordering: DB commit → prompt teardown (before any FS work) → scrub → 204.

## Testing

Grouped-dir `EMBEDDING_MODEL=bge-m3 uv run pytest <paths> -p no:cacheprovider`.

**Slice A**
1. **A1 (the key durability test):** delete a workspace with a `running` turn →
   row is terminal in the delete txn; a subsequent `_finalize_success` (simulating
   the in-flight processor, incl. the defensive-`TurnDoneEvent` path) is a
   **no-op** — the row stays terminal, no `completed`, no duplicate event.
2. Registry tombstone: retire-before-launch → `claim_launch_or_wait` raises;
   retire-during-pending-launch → `installed=False`, waiter raises
   `WorkspaceRetired`, fresh handle torn down; retire with installed handle →
   terminated + lock released; re-retire no-op.
3. Adapter: **hanging** `disconnect()` → returns within the deadline and
   force-kill is invoked; `kill()` failing → logged, returns False (no raise).
4. **A4 reconciliation:** a live handle for a soft-deleted workspace is
   terminated by the sweep tick **even with `in_flight > 0`**; a handle for a live
   workspace is untouched. Covers the capacity/sweep-window and
   failed-teardown cases without per-path state.
5. Processor: retire after claim, before the 2nd launch → **no send**, row
   finalized/terminal (never left `running`); retire mid-stream → `in_flight == 0`,
   row terminal.

**Slice B**
6. Coordinator: all hooks run concurrently; one raising → logged, the other still
   runs; constructing without a `REQUIRED` hook **raises at startup**.
7. Bridge: loop on a **separate thread** (the M1 pattern from the
   session-revocation barrier test) — returns after hooks complete; a hanging hook
   → returns at the bound, logged, no raise.
8. Collab: live rooms on both artifacts → evicted, clients closed **1008**,
   `can_write` False, no checkpoint; **`acquire` and `operate` both reject** a
   retired workspace, including when retirement lands during `operate`'s
   `await load()` (straddle).
9. Route: real `DELETE` (owner) → **204**; no send after the response; existing
   soft-delete assertions (queued `cancelled`, **running terminalized**,
   `soft_deleted_at`, bytes scrubbed, collab room gone) hold. Frontend `tsc` +
   `vitest` unchanged; **`schema.gen.ts` untouched** (204-only contract preserved).

## Acceptance gates

`tests/proposal_assist/{drafting,turns,collab,lifecycle}`, workspaces + route
suites, `tests/test_api_spec.py`; `ruff check .`; frontend regression only.
Diff-scope: no migration, no frontend, no OpenAPI/schema regen.

## References (grounded)

Route/service `web/api/workspaces.py:1488`, `workspaces.py:82` (commit `:133`,
scrubs `:134-148`) · launches `processor.py:158/:276`, `controller.py:213` ·
finalize try `:295`, conditional success UPDATE `:446-452` · registry `:81`
(`claim_launch_or_wait`), `:141` (capacity pop), `:164` (`_teardown_entry`), `:195`
(`fulfill_pending_launch`), `:286` (`terminate`), `:367` (sweep pop) · controller
cleanup `:178-190` · adapter `:497` (defensive TurnDone), `:525/:532/:536/:551` ·
collab `room.py:456` (`acquire`), `:491` (`operate`), straddle
`kernel_accept.py:71`, `collab_ws.py:117` · app state `main.py:169/:235`.

## Slice C (follow-up)

Register a **passes** hook (coherence/evaluator/review/uncited) into the
coordinator; needs workspace-scoped cancellation of the inline-awaited pass
coroutines. Reconciliation (A4) gives them no coverage today — their streams are
coroutines, not handles. Separate spec → plan.
