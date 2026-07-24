# Workspace Retirement on Delete — Design (v7)

**Status:** approved direction; v7 folds a fifth Codex review (4 findings, all
verified). Two changes are **claim weakenings**, not new machinery: v6 overstated
race-freedom. v7 states only what is true.
**Type:** backend-only, no migration, no frontend change, no OpenAPI change.
**Delivery:** **Slice A** = durable floor + containment. **Slice B** = prompt
retirement + coordinator + collab + the live-workspace mutex helper.
**Slice C** = passes.

## What is actually guaranteed (honest statement)

| Guarantee | Mechanism | Strength |
|---|---|---|
| No new turn dispatches | DB `soft_deleted_at`, honored by every claim layer | **Durable** |
| Running turn row reaches terminal | terminalized in the delete transaction (A1) | **Durable, race-free** (same-row contention) |
| Real token usage counted | usage-unknown terminalization + post-terminal stamp (A1) | **Durable** |
| No writer mutates git/DB after delete | live-workspace commit-mutex helper (B4) | **Durable, race-free** (same mutex) |
| Relaunch avoided | pre-claim `soft_deleted_at` check (A2) | **Narrowing only — NOT race-free** |
| Subprocess stops | prompt interrupt→terminate (B), hard-bounded (A4) | Best-effort |
| Anything leaked | per-launch client inventory + reconciliation (A3) | **Eventual, bounded by one sweep tick** |

**Explicitly NOT claimed (review P1-2):** that the response, or Slice A, proves a
subprocess died. Deletion can commit between any precheck and the outbound
`client.query`, and no lock can be held across a streaming call, so
check-then-act cannot be linearized. Slice A promises **containment within one
reconciliation tick** (`WORKSPACE_IDLE_SWEEP_SECONDS`); Slice B narrows the
common case to ~immediate. The *durable* rows above are unaffected by that race.

**Contract:** `DELETE` always returns **204**. Missing retirement wiring is a
**startup invariant**.

---

# Slice A — durable floor + containment (standalone)

### A1 — terminalize running turn rows, preserving cap accounting

Extend the existing **`soft_delete_workspace`** (the `_db` extraction is B) to
terminalize `running` turn rows in the delete transaction, alongside today's
queued cancellation. `_finalize_success` is already a conditional `UPDATE ...
WHERE status='running'` (`processor.py:446-452`), so both racing paths contend on
the **same database row** — closing the TOCTOU durably and neutralizing the
defensive-`TurnDoneEvent` success path (`claude_adapter.py:497` → `:338`).

Cap accounting (v6 fix, retained): `record_terminal_outcome` computes `consumed =
tokens_in > 0 or tokens_out > 0` and, when false, clears the submit-time estimate
(`outcomes.py:~84`). Terminalizing a *running* turn at `0/0` would erase the
pre-count while the real usage is rolled back by the terminal-row guard →
repeated start-and-delete spends invisibly. Required:
- **`usage_unknown=True`** terminalization — **retains** the submit-time estimate.
- **Post-terminal usage stamp** — a narrow helper updating only the `ChatTurn`
  token columns; no `TurnJob.status` change, no second *outcome* event.

**Audit honesty (review P2-4):** the outcome event's payload is written at
cancellation time with zero usage (`outcomes.py:118-130`), and the late stamp
touches only `ChatTurn`. Emit a **distinct non-outcome**
`cloud_turn_usage_reconciled` `WorkspaceEvent` carrying the real tokens, leaving
exactly one outcome event and no rewritten history. (Alternative, if rejected:
narrow the guarantee to billing/cap accuracy and say audit payloads may
understate — but the extra event is cheap and keeps both true.)

### A2 — pre-claim soft-delete check (a **narrowing**, not a guarantee)

`process_turn_once` launches three times (`processor.py:158`, `:276`,
`send_turn`→`launch` `controller.py:213`), all get-or-create. Add a
`soft_deleted_at` check **at the top of `controller.launch`, above
`claim_launch_or_wait`** — the cached/pending path returns early at
`controller.py:109` and never reaches the existing `work_session` block (`:138`).
On deleted → raise `WorkspaceRetired(ws)`. Cost: one indexed PK lookup per launch.

**Race (conceded):** a deletion committing *after* this check can still see a
cached handle returned and `send_turn` reach `client.query`. This check therefore
**shrinks** the window; it does not close it. Containment for the remainder is A3.
Required test: pause after the precheck, commit the deletion, resume, and assert
the **documented** behavior (query may proceed; the reconciliation tick then
terminates it) — the test encodes the honest contract, not an aspirational one.

### A3 — per-launch live-client inventory + reconciliation

Keyed by **launch generation, not workspace** (review P1-1): after an unconfirmed
teardown the registry releases the advisory lock (`registry.py:164`), so a fresh
launch for the same workspace would otherwise overwrite the orphaned record — or a
late teardown would clear the *new* client's record.

```python
@dataclass
class _LiveClient:
    launch_id: uuid.UUID          # generation identity
    workspace_id: uuid.UUID
    client: ClaudeSDKClient
    teardown: asyncio.Task | None = None

self._live_clients: dict[uuid.UUID, _LiveClient]   # keyed by launch_id
```
- **Registered before `await client.connect()`** (`claude_adapter.py:185`) so
  connect-time failures are discoverable.
- **Cleared by identity** (`launch_id`) and only on **confirmed death**
  (disconnect returned, or force-kill with the process reaped).
- **One tracked teardown task per record**, reused if already running — repeated
  retirement/sweep passes never spawn duplicate kills.
- Orphan and replacement records coexist harmlessly; both are reaped on their own
  terms.

**Reconciliation pass** (new pass on the existing idle-sweep tick,
`config.py:216`): for every record whose workspace is soft-deleted, ensure its
teardown task is running; drop on confirmed death. Because it scans the
inventory, it finds clients orphaned by capacity eviction (`registry.py:141`),
idle sweep (`:367`), retirement teardown, or launcher cleanup
(`controller.py:181`, which suppresses exceptions). It also terminates registry
entries for soft-deleted workspaces **even with `in_flight > 0`**.

### A4 — hard-bounded disconnect

`asyncio.wait_for` awaits cancellation completion, so a cancellation-resistant
`disconnect()` can overrun and force-kill never fires. Run `client.disconnect()`
as a **tracked task**; fire `_force_kill_subprocess` from an **independent timer**
at the deadline; **reap** (`process.wait()`, short second deadline) before
clearing the inventory record. Force-kill returns `bool` for logging/metrics and
inventory bookkeeping only.

### A5 — processor guarded region

Extend the finalization `try` (`:295`) over the post-claim `controller.launch`
(`:276`) + `set_turn_provenance`; catch `WorkspaceRetired` → terminal path
(usually a no-op after A1); route real usage through A1's post-terminal stamp.

---

# Slice B — prompt retirement, coordinator, collab, mutex helper

### B1 — coordinator + bridge

`WorkspaceRetirement`, `REQUIRED = ("turns", "collab")` validated at construction
(**startup invariant**); `retire(ws)` fans out concurrently and isolated; hooks
return `None`. `signal_workspace_retirement(request, ws) -> None`: sync→loop
bridge (as `signal_user_revocation`), **bounded ~5s**, never raises. A3/A4 remain
the backstop.

### B2 — turns hook: prompt in-process tombstone

`registry._retired` + rejection in `claim_launch_or_wait`;
`fulfill_pending_launch -> bool installed` (retired ⇒ don't install; resolve the
waiter with `WorkspaceRetired`; `controller.launch` raises so its existing
`except` tears the fresh handle down). This is the promptness layer over A2's
floor — it is a linearization point for *install*, though (per the honest
statement) not for an already-cached client's in-flight `query`.

### B3 — collab retirement

- `RoomManager._retired_ws`; **`acquire` and `operate`** both reject a retired
  workspace, re-checked **after every out-of-lock await** (`operate`'s
  `await load()`, `room.py:491`, is the straddle `kernel_accept.py:71` rides).
- `retire_workspace(ws)`: tombstone; for both artifact keys — `can_write=False`,
  `evicted=True`, close each client **1008** (terminal for the frontend; 1011
  invites reconnect storms), remove the room; **no checkpoint**; **drain active
  `room.ops`** before considering retirement done.
- **Retired `acquire` → 1008:** the socket is accepted at `collab_ws.py:87` and
  the route's `try` opens at `:118`, so a raise from `acquire` bypasses cleanup
  and surfaces as 1011/1006. Catch the shared retirement exception **around
  `acquire`** and `close(1008)` + return.
- Route's `signal_from_request` removed (subsumed).

### B4 — live-workspace commit-mutex helper (review P1-3 — **all writers**)

An already-waiting writer can acquire the mutex *after* deletion and mutate git
(git commits are **not** rolled back with the SQL transaction — `kernel.py:281`
checkpoints inside `operate`). This is **not** collab-specific. Enumerated call
sites of `acquire_commit_mutex` (17): `human_cite.py:102`,
`analysis/insert.py:349`, `collab/kernel.py:136/:221/:316/:427`,
`collab/flip.py:117`, `web/api/workspaces.py:536/:647/:735`, `turns/submit.py:242`,
`editing/save.py:180`, `tools/citations.py:797/:911/:1038/:1213`,
`collab/checkpoint.py:82`. None currently rechecks soft-deletion (`save.py`'s
under-mutex recheck is *severance*; `flip.py` re-reads flip state).

**Add `acquire_commit_mutex_live(session, workspace_id)`** = acquire, then
**immediately** re-read the `Workspace` row and raise `WorkspaceDeleted` if
`soft_deleted_at is not None`, **before any mutation**. Migrate every *mutating*
call site to it (the plan enumerates each of the 17 and records the decision;
read-only/flip sites may keep the plain acquire with a stated reason). Deletion
itself takes the same mutex, giving a **real linearization point** for writers.

### B5 — service split + route

Extract `soft_delete_workspace_db` (through `commit()`, incl. A1) and
`scrub_workspace_bytes` (`:134-148`); keep a thin combined wrapper. Route: DB
commit → `signal_workspace_retirement` → scrub → **204**.

## Testing

**Slice A** — (1) cap accounting: delete mid-run → estimate retained, late real
usage stamped on the `ChatTurn`, status unchanged, **exactly one outcome event**
plus one `cloud_turn_usage_reconciled`; repeated start-and-delete accumulates
against the daily cap. (2) delete → `_finalize_success` is a no-op; row stays
terminal. (3) `launch` on a soft-deleted workspace raises — including via the
**cached-entry path** and `send_turn`'s internal launch. (4) **the conceded
race:** pause after the precheck, commit deletion, resume → assert documented
behavior + that the reconciliation tick terminates it. (5) inventory: two
generations for one workspace (orphan + fresh) are tracked and cleared **by
identity**; a record registered before a failing `connect()` is discoverable; a
second sweep does not spawn a duplicate teardown task. (6) hard bound: a
cancellation-ignoring `disconnect()` still gets force-killed at the deadline and
reaped before the record clears. (7) processor: retire before launch #2 → no
send, row terminal.

**Slice B** — (8) coordinator concurrent+isolated; missing REQUIRED hook raises at
startup. (9) bridge on a **separate-thread loop** (M1 pattern); hanging hook
returns at the bound. (10) collab: both rooms evicted, clients **1008**, no
checkpoint, `ops` drained; `acquire` **and** `operate` reject, incl. retirement
landing during `operate`'s `await load()`; retired `acquire` surfaces **1008, not
1011/1006**. (11) **mutex helper straddle, two writers:** a collab operation *and*
a legacy writer (`save_section`) each blocked on the mutex when deletion commits
→ both raise `WorkspaceDeleted` and mutate **nothing** (no git commit, no DB row).
(12) route: `DELETE` → **204**, no send after the response, existing soft-delete
assertions hold; `schema.gen.ts` untouched.

## Acceptance gates

`tests/proposal_assist/{drafting,turns,collab,editing,citations,lifecycle}`,
workspaces + route suites, `tests/test_api_spec.py`; `ruff check .`; frontend
regression only. No migration, no frontend, no schema regen.

## References (grounded)

`web/api/workspaces.py:1488` · `workspaces.py:82` (`:133`, `:134-148`) ·
`outcomes.py:62-90` (consumed/estimate), `:118-130` (event payload) ·
`processor.py:158/:276/:295/:446-452` ·
`controller.py:105/:109/:138/:178-190/:213` ·
`registry.py:81/:141/:164/:195/:286/:367` ·
`claude_adapter.py:185/:497/:525/:532/:536/:551` · `room.py:456/:491` ·
`kernel.py:281` · `kernel_accept.py:71/:92` · `collab_ws.py:87/:117` ·
17 mutex sites (listed in B4) · `config.py:216` · `main.py:169/:235`.

## Slice C (follow-up)

Passes (coherence/evaluator/review/uncited) register a coordinator hook; needs
workspace-scoped cancellation of the inline-awaited pass coroutines. A3's
inventory does **not** cover them (coroutines, not SDK client handles). Separate
spec → plan.
