# Workspace Retirement on Delete — Design (v6)

**Status:** approved direction. v6 folds a fourth Codex review (6 findings, all
verified). v5's durable-first reframe survives, with one correction: **v5's claim
that reconciliation "dissolves" the leaked-subprocess findings was wrong** —
reconciliation scanned `_entries`, but entries are popped *before* teardown
(`registry.py:141/:367`) and the client is popped *before* disconnect
(`claude_adapter.py:532`), so in exactly the failure case there is nothing left to
find. v6 restores **one** durable ownership record (an adapter-owned live-client
inventory) — one mechanism, not the six per-path trackers v4 carried.
**Type:** backend-only, no migration, no frontend change, no OpenAPI change.
**Delivery:** **Slice A** = durable floor (standalone). **Slice B** = prompt
retirement + coordinator + collab. **Slice C** = passes.

## Guarantee model

| Guarantee | Mechanism | Strength |
|---|---|---|
| No new work | DB `soft_deleted_at`, already honored by every claim layer | Durable |
| **No relaunch** | `controller.launch` consults `soft_deleted_at` (A2) | **Durable, restart-safe** |
| Running turn row terminal | terminalized in the delete transaction (A1) | **Durable, race-free** |
| Tokens counted | usage-unknown terminalization + post-terminal stamp (A1) | Durable |
| Subprocess stops | prompt interrupt→terminate; hard-bounded (A4, B) | Best-effort |
| Anything leaked | **live-client inventory** + reconciliation sweep (A3) | Eventual, self-healing |

**Contract: `DELETE` always returns 204.** No 202/503 → no OpenAPI/`schema.gen.ts`
change, no frontend change. Missing retirement wiring is a **startup invariant**.

---

# Slice A — durable floor (independently mergeable and standalone)

### A1 — terminalize running turn rows **without breaking cap accounting**

Extend the existing **`soft_delete_workspace`** (not `soft_delete_workspace_db` —
that extraction is B3; A must stand alone) to terminalize `running` turn rows in
the same transaction as the tombstone, alongside today's queued-turn cancellation.

`_finalize_success` is already a conditional `UPDATE ... WHERE status='running'`
(`processor.py:446-452`) that rolls back on `rowcount == 0`, so once deletion
terminalizes the row both racing paths contend on the **same database row** — the
TOCTOU closes durably, and the defensive-`TurnDoneEvent` success path
(`claude_adapter.py:497` → `:338`) can no longer resurrect a deleted turn.

**Cap-accounting defect this introduces (review P1-3), and its fix.**
`record_terminal_outcome` computes `consumed = tokens_in > 0 or tokens_out > 0`
and, when nothing was consumed, **clears the submit-time estimate**
(`user_turn.tokens_in = None`, `outcomes.py:~84`). Terminalizing a *running* turn
with `0/0` would therefore (a) erase the pre-count and (b) leave the real usage
unrecorded, because the later `_finalize_success` rolls back on the terminal row.
Repeated start-and-delete cycles would then spend real tokens invisibly against
the daily cap. Fix, both halves required:
- **Usage-unknown terminalization:** a new flag (e.g.
  `record_terminal_outcome(..., usage_unknown=True)`) that **retains** the
  submit-time estimate rather than clearing it — the turn *did* consume input.
- **Post-terminal usage stamp:** a narrow helper the processor calls when the
  stream later reports real usage, which **only** updates the `ChatTurn` token
  columns — it must not change `TurnJob.status` and must not emit a second
  outcome event. The processor calls it on the retired path instead of relying on
  `_finalize_success` (whose whole update is rolled back).

### A2 — durable no-relaunch: `launch` consults the DB tombstone

`process_turn_once` launches three times (`processor.py:158`, `:276`, and
`send_turn`→`launch` `controller.py:213`), all get-or-create. In Slice A no route
signal exists yet, so the **durable** stop is a soft-delete check inside
`controller.launch`:

- The check must sit **at the top of `launch`, before `claim_launch_or_wait`** —
  the cached/pending path returns early at `controller.py:109` and never reaches
  the existing `work_session` block (which loads the `Workspace` row at `:138`).
- On `soft_deleted_at is not None` → raise `WorkspaceRetired(ws)`.
- Cost: one indexed PK lookup per launch (turns are user-paced; `send_turn` calls
  `launch` once per turn). Acceptable.

This is restart-safe and needs no wiring, which is what makes **Slice A
standalone**: an actively-deleted workspace cannot reach launch #2 or #3, so the
normal active-delete path stops promptly rather than waiting for a sweep.
(v5 wrongly claimed standalone safety while the tombstone only existed in
`registry.retire_workspace`, which Slice B wires.)

### A3 — adapter-owned live-client inventory + reconciliation (the restored anchor)

The unavoidable durable ownership, because every existing path discards its
handle before teardown succeeds:
- `ClaudeAdapter` keeps `self._live_clients: dict[uuid.UUID, ClaudeSDKClient]`,
  populated at launch and **removed only on confirmed death** (disconnect
  completed, or force-kill with the process reaped). `terminate` may still pop
  `handle.state["client"]`, but the inventory retains the reference, so a failed
  attempt leaves something to retry.
- **Reconciliation sweep** (new pass on the existing idle-sweep tick,
  `WORKSPACE_IDLE_SWEEP_SECONDS`): for each inventory entry whose workspace is
  soft-deleted, retry disconnect/force-kill; drop from the inventory on confirmed
  death. Scans the **inventory**, not `_entries` — so it finds clients orphaned by
  capacity eviction (`registry.py:141`), idle sweep (`:367`), retirement teardown,
  or launcher cleanup (`controller.py:181`, which suppresses exceptions).
- Registry entries whose workspace is soft-deleted are also terminated by this
  pass **even when `in_flight > 0`** (unlike the idle pass — a running turn on a
  deleted workspace is precisely the target).

This single inventory is what P1-1/P1-2/P1-3 actually require; no per-path acks,
strict flags, or in-progress-teardown maps.

### A4 — hard-bounded disconnect (restore the v4 mechanism)

`asyncio.wait_for` awaits cancellation completion, so a cancellation-resistant
`disconnect()` can overrun indefinitely and force-kill never runs (v5 regressed
this by reverting to plain `wait_for`). Run `client.disconnect()` as a **tracked
task** and fire `_force_kill_subprocess` from an **independent timer** at the
deadline, then reap (`process.wait()`, short second deadline) before clearing the
inventory entry. `_force_kill_subprocess` returns `bool` for logging/metrics and
inventory bookkeeping — not for a response contract.

### A5 — processor guarded region

Extend the finalization `try` (`:295`) to cover the post-claim
`controller.launch` (`:276`) + `set_turn_provenance`; catch `WorkspaceRetired` →
terminal/failed path (usually a no-op after A1) and route real usage through A1's
post-terminal stamp.

### Slice A acceptance

Standalone: deletion terminalizes the row (durable), `launch` refuses to relaunch
(durable), and any leaked client is reaped by the inventory sweep. No coordinator,
no route change.

---

# Slice B — prompt retirement, coordinator, collab

### B1 — coordinator + bridge (`proposal_assist/lifecycle/retirement.py`)

`WorkspaceRetirement` with `REQUIRED = ("turns", "collab")` validated **at
construction (startup invariant)**; `retire(ws)` fans out concurrently and
isolated (`gather`; `_run_one` logs+swallows). Hooks return `None` — no
confirmation plumbing. `signal_workspace_retirement(request, ws) -> None`: the
sync→loop bridge (as `signal_user_revocation`), **bounded ~5s** so streaming
normally stops before the 204, then returns regardless; never raises. A4/A3
remain the backstop.

### B2 — turns hook: prompt in-process tombstone

`registry._retired` + rejection in `claim_launch_or_wait`, and
`fulfill_pending_launch -> bool installed` (retired ⇒ don't install; resolve the
waiter's future with `WorkspaceRetired`; `controller.launch` raises so its
existing `except` tears the fresh handle down). This is the *promptness* layer
over A2's durable floor: it stops an in-flight launcher from installing a handle
between the delete commit and the next DB check.

### B3 — collab retirement

- `RoomManager._retired_ws`; **`acquire` and `operate`** both reject a retired
  workspace, re-checked **after every out-of-lock await** (`operate`'s
  `await load()` at `room.py:491` is the straddle `kernel_accept.py:71` rides).
- **Already-yielded operations (review P2-5):** gating entry is not enough — an
  operation that already holds its room mutates Yjs and calls
  `checkpoint_artifact` (`kernel.py:281`), and **git commits are not rolled back
  with the SQL transaction**, so "no checkpoint after delete" can be violated.
  Serialize deletion against the existing **per-workspace commit mutex** and
  require a **post-mutex live-workspace re-check** before committing; collab
  retirement additionally **drains active `room.ops`** before considering itself
  done.
- `retire_workspace(ws)`: tombstone, then for both artifact keys — `can_write =
  False`, `evicted = True`, close each client **1008** (terminal for the frontend;
  `evict()`'s 1011 invites reconnect storms), remove the room; **no checkpoint**.
- **Retired `acquire` must map to 1008 (review P2-6):** the socket is accepted at
  `collab_ws.py:87` and the route's `try` opens at `:118`, so a raise from
  `acquire` bypasses cleanup and surfaces as 1011/1006. Catch the shared
  retirement exception **around `acquire`** and `close(1008)` + return.
- Route's `signal_from_request` removed (subsumed).

### B4 — service split + route

Extract `soft_delete_workspace_db` (through `commit()`, including A1) and
`scrub_workspace_bytes` (`:134-148`); keep a thin combined wrapper. Route: DB
commit → `signal_workspace_retirement` → scrub → **204**.

## Testing

**Slice A** — (1) **cap accounting:** delete during a running turn → estimate
retained (not `None`), later real usage stamped onto the ChatTurn, status
unchanged, exactly one outcome event; repeated start-and-delete accumulates
against the daily cap. (2) delete → `_finalize_success` is a no-op; row stays
terminal. (3) `launch` for a soft-deleted workspace raises `WorkspaceRetired` —
including via the **cached-entry path** (proves the check precedes
`claim_launch_or_wait`) and via `send_turn`'s internal launch. (4) inventory:
a client orphaned by capacity-evict / idle-sweep / failed teardown is found and
killed by the reconciliation pass; entry with `in_flight > 0` on a soft-deleted
workspace is terminated. (5) **hard bound:** a `disconnect()` that ignores
cancellation → force-kill still fires at the deadline; process reaped before the
inventory entry clears. (6) processor: retire before launch #2 → no send, row
terminal, not left `running`.

**Slice B** — (7) coordinator: concurrent+isolated; missing REQUIRED hook raises
at startup. (8) bridge on a **separate-thread loop** (the M1 pattern); hanging
hook → returns at the bound. (9) collab: both rooms evicted, clients closed
**1008**, no checkpoint; `acquire` **and** `operate` reject, including retirement
landing during `operate`'s `await load()`; **a mutation already inside `operate`
cannot checkpoint after deletion** (commit-mutex + post-mutex re-check); retired
`acquire` surfaces as **1008, not 1011/1006**. (10) route: `DELETE` → **204**, no
send after the response, existing soft-delete assertions hold; `schema.gen.ts`
untouched.

## Acceptance gates

`tests/proposal_assist/{drafting,turns,collab,lifecycle}`, workspaces + route
suites, `tests/test_api_spec.py`; `ruff check .`; frontend regression only. No
migration, no frontend, no schema regen.

## References (grounded)

`web/api/workspaces.py:1488` · `workspaces.py:82` (commit `:133`, scrubs
`:134-148`) · `outcomes.py:62-90` (`consumed`, estimate clear) ·
`processor.py:158/:276/:295/:446-452` · `controller.py:105/:109/:138/:178-190/:213`
· `registry.py:81/:141/:164/:195/:286/:367` · `claude_adapter.py:497/:525/:532/:536/:551`
· `room.py:456/:491` · `kernel.py:281` · `kernel_accept.py:71/:92` ·
`collab_ws.py:87/:117` · `config.py:216` (`WORKSPACE_IDLE_SWEEP_SECONDS`) ·
`main.py:169/:235`.

## Slice C (follow-up)

Passes (coherence/evaluator/review/uncited) register a hook into the coordinator;
needs workspace-scoped cancellation of the inline-awaited pass coroutines. The A3
inventory does **not** cover them (their streams are coroutines, not SDK client
handles). Separate spec → plan.
