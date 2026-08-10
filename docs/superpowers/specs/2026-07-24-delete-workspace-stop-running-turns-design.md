# Workspace Retirement on Delete — Design (v9)

**Status:** approved direction; v9 folds a seventh review, this one against the
B-retire *plan* (7 findings, all verified). Two were lifecycle gaps that changed
this design: `room.ops` never covered the collab client-write or cold-hydration
paths (two new guarantee rows + NOT-claimed item 4), and B2 must actually
terminate a live entry rather than defer it to the 60s idle sweep. v8 folded a
sixth Codex review (5 findings, all verified): two further **claim weakenings**,
one closing a real leak *and* a capacity-ceiling bypass introduced by v7's
inventory.
**Type:** backend-only, no migration, no frontend change, no OpenAPI change.
**Delivery:** **Slice A** = durable floor + generation lifecycle. **Slice B** =
prompt retirement + coordinator + collab + live-mutex helper. **Slice C** = passes.

## What is actually guaranteed (honest statement)

| Guarantee | Mechanism | Strength |
|---|---|---|
| No new turn dispatches | DB `soft_deleted_at`, honored by every claim layer | **Durable** |
| Running turn row reaches terminal | terminalized in the delete transaction (A1) | **Durable, race-free** (same-row contention) |
| Real token usage counted exactly once | usage-unknown terminalization + idempotent post-terminal stamp (A1) | **Durable** |
| **Mutex-participating** artifact/turn writers cannot mutate git/DB after delete | `acquire_commit_mutex_live` + deletion takes the same mutex (B4) | **Durable, race-free** (same mutex) |
| Relaunch avoided | pre-claim `soft_deleted_at` check (A2) | **Narrowing only — NOT race-free** |
| Subprocess stops | prompt interrupt→terminate (B2), hard-bounded (A4) | Best-effort |
| Collab **cold-hydration** seed cannot commit after delete | `acquire_commit_mutex_live` inside `load_or_hydrate` (B3) | **Durable, race-free** (same mutex as the delete txn) |
| Collab **client** writes cannot commit after delete | `room.evicted` flipped under `room.lock`, the lock `Room.handle` holds across its persist (B3) | **Race-free once the lock is won**; bounded wait, see NOT-claimed 4 |
| Leaked generations | inventory + reconciliation (A3) | **First retry at the next successful sweep, retried each tick; confirmed death remains best-effort** |

**Explicitly NOT claimed:**
1. That the response, or any slice, proves a subprocess died. Deletion can commit
   between any precheck and the outbound `client.query`, and no lock can be held
   across a streaming call, so check-then-act cannot be linearized.
2. That the reconciliation sweep *contains* a leak. It guarantees a **remediation
   attempt each tick**; `force_kill`/reap can fail, leaving the process alive.
3. That **non-mutex** writers are covered. `settings.py` `update_settings`
   (`:69`) and `add_member` (`:97`) never take the commit mutex, so a delete
   committing after their `require_owner` can be followed by a metadata write.
   **Accepted residual:** metadata-only, no git, no cloud spend, on a workspace
   that is already 404 to every reader. Listed as a Slice-C candidate.
4. That the collab **client** write path is closed with zero window. B3 flips
   `room.evicted` under `room.lock` — the same lock `Room.handle` holds across
   its `persist` await — so once retirement wins that lock no client write is in
   flight and every later frame drops. The wait for the lock is **bounded**
   (`_RETIRE_LOCK_WAIT_SECONDS`); on overrun retirement sets the flag
   unsynchronized and proceeds, and a persist already inside the lock can still
   commit its `YjsUpdate` row after the 204. **Accepted residual:** an orphaned
   update row, no git write, no cloud spend, on a workspace already 404 to every
   reader. It is logged (`collab.retire_lock_wait_timeout`).

**Note on `room.ops` (first-review correction).** `room.ops` is bumped only by
`RoomManager.operate`, so draining it covers server ops (kernel accepts,
checkpoints) and **nothing else**. The two paths that dominate collab writes —
`Room.handle`'s per-frame persist and cold hydration's seed — bypass both `ops`
and the commit mutex, and are covered by the two mechanisms added to the table
above. Do not describe B4 as their backstop.

**Contract:** a **successful** deletion returns **204**. Because deletion now
takes the commit mutex (B4), a contended delete can raise `CommitMutexTimeout`
(`locking.py:144`, 10s) → the route returns the house busy semantics (**429**,
`BUSY_ERROR_MESSAGE`) having committed **nothing**; the client retries. Missing
retirement wiring is a **startup invariant**.

---

# Slice A — durable floor + generation lifecycle (standalone)

### A1 — terminalize running turn rows, preserving cap accounting

Extend the existing **`soft_delete_workspace`** (the `_db` extraction is B) to
terminalize `running` turn rows in the delete transaction. `_finalize_success` is
already a conditional `UPDATE ... WHERE status='running'`
(`processor.py:446-452`), so both paths contend on the **same row** — closing the
TOCTOU durably and neutralizing the defensive-`TurnDoneEvent` success path
(`claude_adapter.py:497` → `:338`).

- **`usage_unknown=True`** terminalization **retains** the submit-time estimate
  (plain `0/0` would clear it via `outcomes.py`'s `consumed` guard, and the real
  usage would then be rolled back by the terminal-row guard → invisible spend).
- **Post-terminal usage stamp** — narrow helper, and (review P2-4) it must be an
  **atomic, idempotent transition that also CLEARS the retained user-row
  estimate** when stamping real usage, exactly as `_finalize_success` does
  (`processor.py:409+`). Otherwise estimate + real usage double-count. Guard the
  transition (e.g. conditional update keyed on the estimate still being present)
  so repeated calls are no-ops, and emit the distinct non-outcome
  `cloud_turn_usage_reconciled` `WorkspaceEvent` **only on the first** stamp. No
  `TurnJob.status` change; exactly one *outcome* event, no rewritten history.

### A2 — pre-claim soft-delete check (a **narrowing**, not a guarantee)

Check `soft_deleted_at` **at the top of `controller.launch`, above
`claim_launch_or_wait`** (the cached/pending path returns early at
`controller.py:109`, never reaching the `work_session` block at `:138`); raise
`WorkspaceRetired`. Cost: one indexed PK lookup per launch.

**Conceded race:** a deletion committing after this check can still see a cached
handle returned and `send_turn` reach `client.query`. Required test pauses after
the precheck, commits the deletion, resumes, and asserts the **documented**
behavior (the query may proceed; the sweep then attempts termination).

### A3 — per-generation inventory, `retiring` state, and capacity accounting

Keyed by **launch generation**, not workspace (an unconfirmed teardown releases
the advisory lock at `registry.py:164`, so a fresh launch would otherwise
overwrite the orphan, or a late teardown clear the new client):

```python
@dataclass
class _LiveClient:
    launch_id: uuid.UUID              # generation identity
    workspace_id: uuid.UUID
    client: ClaudeSDKClient
    state: str = "live"               # "live" | "retiring"   (review P1-1)
    teardown: asyncio.Task | None = None

self._live_clients: dict[uuid.UUID, _LiveClient]      # keyed by launch_id
```
- **Registered before `await client.connect()`** (`claude_adapter.py:185`) so
  connect-time failures are discoverable.
- Any teardown attempt (retirement, capacity eviction, idle sweep, launcher
  cleanup) first marks the record **`retiring`**; the record is **cleared by
  identity only on confirmed death** (disconnect returned, or force-kill with the
  process reaped).
- **One tracked teardown task per record**, reused if already running.

**Reconciliation sweep (corrected scope — review P1-1):** each tick, ensure a
teardown task is running for **every `retiring` record, regardless of whether its
workspace is deleted**, plus every record whose workspace *is* soft-deleted.
Capacity eviction, idle teardown and failed `connect()` all concern **live**
workspaces — v7 swept only deleted ones, so those orphans were never retried.

**Capacity ceiling (review P1-1, second half):** `claim_launch_or_wait` computes
`in_use = len(self._entries) + len(self._pending_launches)` (`registry.py:132`),
which ignores orphaned generations — yet each un-reaped process may still pin a
pooled DB connection, the very thing `DRAFTING_MAX_LIVE_SUBPROCESSES`
(`config.py:177`) exists to bound. **Count every not-confirmed-dead generation
toward `_max_live`** (registry reads a sync count from the adapter inventory under
its own lock). Consequence, documented deliberately: when an evicted victim's
death is unconfirmed, the replacement launch is refused with the existing
`DraftingAtCapacity` → retriable **429**, rather than silently exceeding the
ceiling.

### A4 — hard-bounded disconnect

Run `client.disconnect()` as a **tracked task**; fire `_force_kill_subprocess`
from an **independent timer** at the deadline (plain `asyncio.wait_for` awaits
cancellation completion and can overrun); **reap** (`process.wait()`, short second
deadline) before clearing the record. Force-kill returns `bool` for
logging/metrics and inventory bookkeeping only.

### A5 — processor guarded region

Extend the finalization `try` (`:295`) over the post-claim `controller.launch`
(`:276`) + `set_turn_provenance`; catch `WorkspaceRetired` → terminal path
(usually a no-op after A1); route real usage through A1's idempotent stamp.

---

# Slice B — prompt retirement, coordinator, collab, mutex helper

### B1 — coordinator + bridge

`WorkspaceRetirement`, `REQUIRED = ("turns", "collab")` validated at construction
(**startup invariant**); `retire(ws)` fans out concurrently and isolated; hooks
return `None`. `signal_workspace_retirement(request, ws) -> None`: sync→loop
bridge (as `signal_user_revocation`), **bounded ~5s**, never raises.

### B2 — turns hook: prompt in-process tombstone

`registry._retired` + rejection in `claim_launch_or_wait`;
`fulfill_pending_launch -> bool installed` (retired ⇒ don't install; resolve the
waiter with `WorkspaceRetired`; `controller.launch` raises so its existing
`except` tears the fresh handle down — which now also marks the generation
`retiring`). A linearization point for *install*, not for an in-flight `query`.

`retire_workspace` **also pops an already-live entry and tears it down
best-effort** via the shared `_teardown_entry` (adapter terminate, then advisory
lock release + session close), popping regardless of `in_flight` exactly as
`reconcile_deleted_entries` does. That is what makes the table's "prompt
interrupt→terminate" real: without it the subprocess survives until the next idle
sweep (`WORKSPACE_IDLE_SWEEP_SECONDS`, default 60). The strength stays
**Best-effort** — terminate can fail, and the sweep remains the retry.

### B3 — collab retirement

- `RoomManager._retired_ws`; **`acquire` and `operate`** reject a retired
  workspace, re-checked **after every out-of-lock await** (`operate`'s
  `await load()`, `room.py:491`, is the straddle `kernel_accept.py:71` rides).
- `retire_workspace(ws)`: tombstone; both artifact keys — `can_write=False`,
  `evicted=True` (**set under `room.lock`**, the lock `Room.handle` holds across
  its persist — this is the client-write linearization point, bounded by
  `_RETIRE_LOCK_WAIT_SECONDS`), close clients **1008**, remove the room; **no
  checkpoint**; **drain active `room.ops`** before considering retirement done.
- `load_or_hydrate` takes `acquire_commit_mutex_live` before persisting a seed,
  and **raises** rather than returning `[]` — an empty list is indistinguishable
  from "no content yet" and would let `accept_via_kernel` write to an empty doc.
  `room.ops` cannot cover this: hydration runs during `acquire`'s out-of-lock
  `await load()`.
- **Retired `acquire` → 1008:** accept happens at `collab_ws.py:87` and the
  route's `try` opens at `:118`, so a raise from `acquire` would surface as
  1011/1006. Catch the shared retirement exception **around `acquire`** →
  `close(1008)` + return.
- Route's `signal_from_request` removed (subsumed).

### B4 — live-workspace commit-mutex helper (**scoped honestly** — review P1-3)

Add `acquire_commit_mutex_live(session, workspace_id)`: acquire the mutex, then
**immediately re-read** the workspace and raise `WorkspaceDeleted` if
`soft_deleted_at is not None`, **before any mutation**. **Deletion takes the same
mutex**, so mutex-participating writers get a real linearization point.

- **Staleness (review P1-3):** `session.get(Workspace, id)` can return the
  pre-block identity-map instance. The helper MUST read fresh — a scalar
  `select(Workspace.soft_deleted_at).where(...)`, or `get(..., populate_existing=True)`.
- **Migrate every mutating mutex site.** Enumerated (17):
  `human_cite.py:102`, `analysis/insert.py:349`,
  `collab/kernel.py:136/:221/:316/:427`, **`collab/flip.py:117` (writes
  `CollabArtifactState` + commits — must migrate; v7 wrongly listed it as
  possibly keeping the plain acquire)**, `web/api/workspaces.py:536/:647/:735`,
  `turns/submit.py:242`, `editing/save.py:180`,
  `tools/citations.py:797/:911/:1038/:1213`, `collab/checkpoint.py:82`. The plan
  records a per-site decision; any site keeping the plain acquire states why.
- **Scope is exactly "mutex-participating artifact/turn writers"** — see
  "Explicitly NOT claimed #3" for non-mutex writers.

### B5 — service split + route

Extract `soft_delete_workspace_db` (through `commit()`, incl. A1) and
`scrub_workspace_bytes` (`:134-148`); thin combined wrapper retained. Route: DB
commit (under the mutex; `CommitMutexTimeout` → **429**, nothing committed) →
`signal_workspace_retirement` → scrub → **204**.

## Testing

**Slice A** — (1) cap accounting: delete mid-run → estimate retained; late real
usage stamped **and estimate cleared** (no double count); repeat stamp is a
**no-op** emitting no second event; exactly one outcome event + one
`cloud_turn_usage_reconciled`. (2) `_finalize_success` after delete is a no-op;
row stays terminal. (3) `launch` on a deleted workspace raises — incl. the
**cached-entry path** and `send_turn`'s internal launch. (4) the conceded race:
pause after precheck, commit deletion, resume → documented behavior + sweep
attempts termination. (5) inventory: two generations for one workspace coexist and
clear **by identity**; a record registered before a failing `connect()` is
discoverable; a second sweep spawns no duplicate teardown. (6) **`retiring`
reconciliation on a LIVE workspace:** a capacity-evicted / idle-swept record whose
teardown failed is retried on the next tick. (7) **capacity:** an unconfirmed-dead
generation counts toward `_max_live` → replacement launch raises
`DraftingAtCapacity` (429) rather than exceeding the ceiling. (8) hard bound: a
cancellation-ignoring `disconnect()` is force-killed at the deadline and reaped
before the record clears.

**Slice B** — (9) coordinator concurrent+isolated; missing REQUIRED hook raises at
startup. (10) bridge on a **separate-thread loop** (M1 pattern); hanging hook
returns at the bound. (11) collab: rooms evicted, clients **1008**, no checkpoint,
`ops` drained; `acquire` **and** `operate` reject incl. during `operate`'s
`await load()`; retired `acquire` → **1008, not 1011/1006**. (12) **mutex straddle,
two writers:** a collab op *and* a legacy writer (`save_section`) blocked on the
mutex when deletion commits → both raise `WorkspaceDeleted`, mutating **nothing**
(no git commit, no DB row); plus a `flip.py` variant. (13) helper reads **fresh**
state (identity-map staleness regression test). (14) route: successful `DELETE` →
**204**; contended delete → **429** with nothing committed; **no NEW turn
dispatches** after deletion and the turn row is terminal (the in-flight `query`
race is documented, not asserted away — v7's "no send after the response"
assertion contradicted the conceded race and is removed); `schema.gen.ts`
untouched.

## Acceptance gates

`tests/proposal_assist/{drafting,turns,collab,editing,citations,lifecycle}`,
workspaces + route suites, `tests/test_api_spec.py`; `ruff check .`; frontend
regression only. No migration, no frontend, no schema regen.

## References (grounded)

`web/api/workspaces.py:1488` · `workspaces.py:82` (`:133`, `:134-148`) ·
`outcomes.py:62-90`, `:118-130` · `processor.py:158/:276/:295/:409/:446-452` ·
`controller.py:105/:109/:138/:178-190/:213` ·
`registry.py:81/:127-134/:141/:164/:195/:286/:367` ·
`claude_adapter.py:185/:497/:525/:532/:536/:551` · `room.py:456/:491` ·
`kernel.py:281` · `kernel_accept.py:71/:92` · `collab_ws.py:87/:117` ·
`flip.py:117` · `settings.py:69/:97` (non-mutex writers) · `locking.py:144`
(10s timeout, `BUSY_ERROR_MESSAGE`) · `config.py:177/:216` · `main.py:169/:235`.

## Slice C (follow-up)

(a) Passes (coherence/evaluator/review/uncited) register a coordinator hook —
needs workspace-scoped cancellation of the inline-awaited pass coroutines (the A3
inventory does not cover them: coroutines, not SDK clients).
(b) Non-mutex writers (`settings.py:69/:97`) — decide whether to bring them under
a post-check or accept permanently.
