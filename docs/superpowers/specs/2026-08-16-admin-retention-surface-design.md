# Admin retention surface for deleted workspaces — design

**Date:** 2026-08-16
**Status:** revision 7 — C0's generation is **bound to the database transition** (4.4) after a
review showed revision 6's in-memory sampling failed its own race test; the four retirement
tombstones collapse into one authority (4.5); the jobs hook is made generation-conditional in SQL
(4.6); and C1 gains an explicit failure contract (5.4) and setup-state mapping (5.2). Slices A and
B have shipped (`ef8ccb3`, `991e7a7`). Partial-restore detection is dropped, per 0.3.
**Goal:** Deleting a workspace freezes it and destroys nothing. An admin can then inspect it,
restore it intact, or purge it for real.

## 0. The model, and the slices

**Deleting a workspace freezes it. It destroys nothing.** Users are evicted and cannot get back
in; in-flight work is stopped; every byte stays. An admin then decides the outcome: restore it
intact, or purge it for real.

This replaces the model revisions 1-3 assumed, in which deletion itself destroyed reference and
attachment bytes while keeping the proposal and its history. That split was never principled —
it is simply where the line happened to land — and it is what made restore impossible.

| Slice | Content | Status |
|---|---|---|
| **A** | **Freeze-only delete** — stop destroying bytes on delete | **SHIPPED** `ef8ccb3` |
| **B** | Admin inspection surface — list, read, audit | **SHIPPED** `991e7a7` |
| **C0** | Workspace lifecycle generation — the prerequisite C1 needs | designed in 4 |
| **C1** | Restore | designed in 5 |
| **D** | Workspace filesystem-activity barrier | deferred, scoped in 9.1 |
| **E** | Purge, built on D | deferred, scoped in 9.2 |

None of A, B, C0 or C1 removes anything. Every hazard the reviews found — proving quiescence,
racing an `rmtree`, late writes recreating a purged tree — belongs to E alone.

**C splits in revision 6.** Revision 5 designed restore as one slice. A review showed that cannot
be built safely: nothing serializes a delete's retirement against a restore, and the mechanism
that fixes it is a separate piece of machinery with its own tests. C1 depends on C0 the way E
depends on D.

### 0.1 Why freeze-only ships first

**Every deletion between now and Slice A permanently destroys data that Slice C could otherwise
restore.** Reference and attachment bytes removed today cannot come back; the workspaces they
belonged to will only ever be partially restorable, no matter what is built later.

Slice B is more visible and its plan is already written, but nothing is lost by it waiting a day.
Slice A is a one-line removal at `web/api/workspaces.py:1530` plus test updates, and it stops an
ongoing, irreversible loss. That asymmetry decides the order.

**Settled: A shipped (`ef8ccb3`), then B (`991e7a7`).** This section is kept as the record of why
that order was chosen. It does NOT impose partial-restore handling on Slice C — the set of
workspaces this paragraph is about turned out to be empty; see 0.3.

### 0.2 What this changes about deletion, stated plainly

Before: deleting a workspace removed uploaded attachments and reference files from disk.
After: deleting a workspace removes nothing; the files persist until an admin purges.

This is a deliberate policy shift, consistent with the 2026-08-12 decision that deleted
workspaces stay on disk — but it is broader than that decision's letter, which concerned `repo/`.
A user deleting a workspace may reasonably expect their uploads to be gone. **The delete
confirmation must say what deletion now does**: the workspace is frozen and hidden, and its
contents remain until an administrator removes them.

### 0.3 Workspaces deleted before Slice A

Their reference and attachment bytes are already gone: those deletions really did scrub. A
restore would bring the row and the proposal history back, but not those files.

**Revision 5 drops the partial-restore machinery.** Earlier revisions required Slice C to detect
reference and attachment rows whose files are missing and report which workspaces are only
partially restorable. The system is under development and no real workspaces exist, so the
population that could be partially restorable is **empty** — and it can never grow, because
Slice A shipped and deletion now destroys nothing. Building detection for a set that is empty by
construction is work with no reachable consumer.

Recorded here so it reads as a decision rather than an omission: if this ever ships to an
instance carrying pre-Slice-A deletions, that detection becomes required again before restore is
exposed there.

## 1. Background

> **Historical.** This section describes the system as it was when this document was written,
> before Slice A. It is kept because the later sections argue against it. For what deletion does
> **today**, see 0.2 and 2.

Deleting a workspace was a soft delete: the row is marked `soft_deleted_at`, jobs and turns are
cancelled, collab rooms evicted, and `scrub_workspace_bytes` (`src/call_manager/workspaces.py:181`)
removed attachment and reference **bytes** while deliberately keeping their DB rows for the audit
trail. **Slice A removed that last step** (`ef8ccb3`): deletion now destroys nothing, and
`scrub_workspace_bytes` has no production caller.

**`repo/` is never removed by anything.** The drafted proposal markdown and its full git history
survive indefinitely. There is no retention job, purge, or reaper in the codebase — which is
exactly why an admin cannot currently see what those surviving bytes contain.

Policy settled by the user 2026-08-12: deleted workspaces stay on disk but are **accessible to an
admin**, and an admin can **delete them completely**. Follow-ups settled 2026-08-16: admin access
is **inspection only** (no restore), and purge — when it lands in Slice 3 — removes bytes while
**leaving a tombstone**, never DB rows.

### 1.1 Restore: rejected in revisions 1-3, reinstated in revision 4

Earlier revisions rejected restore on a factual ground: soft delete had already destroyed
reference and attachment **bytes** while keeping their **rows**, so a restored workspace would
list references whose files no longer exist — broken in a way that reads as a bug rather than a
consequence of deletion.

That objection was about **how deletion behaved**, not about restore. Once deletion stops
destroying bytes (Slice A), restore is lossless and the objection disappears. The one residual —
0.3's pre-existing deletions — is empty in practice and is dropped there.

## 2. Slice A — freeze-only delete

The whole change is to stop calling the scrubber. `scrub_workspace_bytes` has exactly one caller,
`web/api/workspaces.py:1530`, which runs it after the DB commit. Delete keeps everything else it
does: marking `soft_deleted_at`, cancelling jobs and queued turns, terminalizing running turns,
evicting collab rooms, and running the retirement coordinator. It simply stops removing files.

`scrub_workspace_bytes` itself is **kept, not deleted** — Slice E's purge needs exactly this
logic (extended to the whole tree), and re-deriving it later from a git history nobody will think
to search is worse than leaving a documented, unused-for-now function in place. Its docstring
must say it is no longer on the delete path and name the slice that will call it.

Slice A also **inverts its error contract**: it raised nothing and swallowed every `OSError`,
because inside the DELETE route a failed cleanup must not block the 204. With that caller gone,
its only intended caller needs the opposite — see §9's Slice E bullet requiring a partial removal
to surface as a controlled retryable failure committing no tombstone and no audit event. Doing it
now, while the function has no production caller, is free; leaving it would hand Slice E a
best-effort primitive it must notice and undo before it can honour that requirement.

The tests asserting deletion removes bytes (`tests/test_workspaces_service.py:208` and
neighbours) invert: they must now assert the bytes **survive** a delete. That inversion is the
slice's real deliverable — a test that still passes unchanged would mean the behaviour did not
actually change.

The delete confirmation copy changes per 0.2.

## 3. Slice B scope (inspection)

**In scope:** an admin-only paginated list of soft-deleted workspaces; read-only inspection of the
surviving repository (proposal markdown at `HEAD` plus bounded commit history); an audit event
for every inspection.

**Out of scope:** purge and everything it implies (slices 2-3); restore; retention timers; any
schema change; any change to the owner-facing delete route.

**Slice 1 adds no migration.** The tombstone columns belong to purge and are specified in 9.2.

### 3.1 API surface

Two admin-only routes, both requiring `require_admin` (`auth/dependencies.py:50`), both returning
404 unless the workspace has `soft_deleted_at IS NOT NULL`:

| Route | Purpose |
|---|---|
| `GET /api/admin/workspaces/deleted` | Paginated list |
| `GET /api/admin/workspaces/deleted/{id}` | Inspect one |

These are **new routes, not relaxations of the owner routes**. The owner routes gate on a live
row; widening them would put the deleted case on the same code path as the live case, where a
future edit could leak one into the other.

#### 3.1.1 Bounding

Both responses are unbounded by nature — a long-lived instance accumulates deleted workspaces, and
history grows with every save.

- The list is paginated: `limit` default 50, max 200; `offset`; newest `soft_deleted_at` first;
  returns a total count.
- Commit history is capped at the most recent 200 commits, and the response states explicitly
  when it was truncated.

### 3.2 Inspection

Reads the surviving repository: proposal markdown at `HEAD`, plus the bounded commit list. No new
storage, no indexing, nothing written.

**Inspection takes the per-workspace commit mutex for the whole git read**, and re-reads
`soft_deleted_at` fresh under it before serving content.

Two reasons, and the second is why it is here in Slice 1 rather than deferred with purge:

1. It is correct today — the mutex is this workspace's established linearization point, and a
   consistent read should hold it.
2. **It is the contract Slice 3 depends on.** Purge and inspection must be mutually exclusive.
   Building inspection without the mutex now would mean revisiting it later, under pressure, in
   the slice that is already the risky one.

**Use `acquire_commit_mutex` (`drafting/locking.py:205`), NOT `acquire_commit_mutex_live`
(`:267`).** The `_live` variant performs a liveness re-read and raises `WorkspaceDeleted`
(`:292`) — it would refuse every workspace this surface exists to serve. This is the single
easiest mistake to make in Slice 1.

Inspection stays read-only with respect to git and collab: it must not create a commit and must
not start a collab room. A deleted workspace has no live room and must not acquire one.

#### 3.2.1 Missing or invalid repository

A soft-deleted workspace may legitimately have no usable repository — it can be deleted before
setup ever creates one. Inspection returns a deliberate **`repository_unavailable`** state rather
than surfacing a raw git error, so an admin sees "this workspace has no readable repository"
instead of something that looks like a defect.

### 3.3 Audit

One event kind on `WorkspaceEvent`, with the acting admin as `actor_id`:

| Kind | Payload |
|---|---|
| `admin_deleted_workspace_inspected` | `workspace_id`, `admin_user_id`, whether content was served, commit count returned, truncation flag |

**No inspection response containing proposal content may be returned until its audit event is
committed.** An audit written after the response fails exactly when it matters — a crash
mid-response leaves content served and no record of it. Committing first costs one round-trip and
makes the trace unconditional.

Inspection is audited deliberately: an admin reading another user's deleted proposal draft is a
privacy-adjacent act in a hospital setting, and it should leave a trace even though it changes
nothing.

### 3.4 UI

One new page under the existing admin shell (`AdminLayout` + `AdminNav`, matching `AdminUsers.tsx`
and siblings): a paginated table of deleted workspaces, and a detail view rendering the proposal
markdown and its (possibly truncated) commit history.

No purge control. The page states that deleted workspaces are retained on disk, so an admin is not
left wondering whether inspection is all there will ever be.

## 4. Slice C0 — workspace lifecycle generation

C1 cannot be built without this. The reasoning is in 4.1-4.3; the mechanism is 4.4.

### 4.1 Deletion's teardown is not serialized with anything

`DELETE /api/workspaces/{id}` does two things in sequence:

1. `soft_delete_workspace_db` marks the row and **commits** (`workspaces.py:175`). The commit
   releases the commit mutex.
2. The route then calls `signal_workspace_retirement` (`web/api/workspaces.py:1536`), which
   schedules `WorkspaceRetirement.retire` on the app loop and blocks on it for a bounded time.

Step 2 runs **outside any mutex**, and none of the four retirement hooks takes one. So the commit
mutex — which serializes writers to a workspace's repository — says nothing about retirement.

Worse, the barrier is a bound on *waiting*, not on *running*: on timeout
`signal_workspace_retirement` logs and returns while **the coroutine keeps running on the loop**
(`retirement.py:123`, "retirement continues on the loop; degrade to eventual"). Retirement can
therefore still be in flight after the DELETE has returned 204.

A restore in that window is silently undone:

```
DELETE:  soft_delete_workspace_db  → COMMIT           (mutex released)
RESTORE:                             clear flag, COMMIT, un-poison
DELETE:  signal_workspace_retirement → hooks run      ← re-poisons a LIVE workspace
```

The result is a workspace that is live in the database and refused by collab and by every new
turn launch, with no error anywhere: every hook did exactly what it was told.

### 4.2 The tombstones

Deletion runs four retirement hooks (`main.py:198`). Two leave **in-memory state that nothing ever
clears**:

| Hook | State | Cleared by? |
|---|---|---|
| `collab` | `MANAGER._retired_ws` — a `set` on the process-global room manager (`collab/room.py:475`) | **No.** Only `.add`, at `:681` |
| `turns` | `registry._retired` (`drafting/registry.py:85`) | **No.** Only `.add`, at `:217` |
| `jobs` | DB rows only | n/a |
| `passes` | cancels live entries; keeps no per-workspace set | n/a |

These are not merely defence in depth. For **mutex-participating** writers the under-mutex re-read
of `soft_deleted_at` is the durable backstop — but the collab **client-write** and **cold
hydration** paths are guarded by the room tombstone itself (`retirement.py:32-35`). Un-poisoning
before the DB says "live" would therefore open a real hole, not merely remove a redundant check.
This fixes the ordering in 5.3.

### 4.3 Why clearing the sets is the wrong primitive

Revision 5 said restore would "clear both tombstones" after committing. Three problems:

- **It is not straight-line code.** Both live in async, lock-protected subsystems on the app loop.
  A synchronous HTTP worker must cross the same `run_coroutine_threadsafe` bridge retirement uses,
  with the same failure modes: schedule failure, timeout, or an exception between the two clears.
- **It has no recoverable failure contract.** If the commit succeeds and the bridge then fails or
  clears only one subsystem, the workspace is live in the database and still poisoned — and
  revision 5's route rejected live workspaces, so a retry got 404.
- **Revision 5's answer to that was wrong.** It argued a crash repairs itself, because the sets are
  in-memory and a restart clears them. But nothing crashes in this scenario. There is no restart.

**Clearing N sets across M subsystems is inherently partial-failure-prone. Do not make it
retryable — make it a single operation.**

### 4.4 The mechanism: a generation bound to the database transition

Revision 6 put the generation in memory and had `signal_workspace_retirement` **read** it. That is
broken, and it fails revision 6's own acceptance test:

```
DELETE:  commit (deleted), then pause
RESTORE: commit (live), bump 0 → 1
DELETE:  resume → signal_workspace_retirement READS generation = 1
         → its retirement is "current", not stale → re-poisons a live workspace
```

Sampling the generation after the commit binds it to *when the signal ran*, not to *which
transition it belongs to*. The reverse order fails too: a restore that commits at generation 1 and
bumps afterwards can invalidate the retirement of a **newer** delete that committed at generation
2, un-poisoning a workspace that is now deleted.

**The generation must be allocated by the transition itself, inside the transaction, while the
commit mutex still orders it.** It then travels with the operation and is never re-read.

```sql
ALTER TABLE workspaces ADD COLUMN lifecycle_generation INT NOT NULL DEFAULT 0;
```

Every lifecycle transition — today: delete and restore — runs under the commit mutex and, in the
same transaction as its state change, does `lifecycle_generation = lifecycle_generation + 1`. The
mutex therefore totally orders generations, and the generation a transition produced is the one
its teardown or reactivation carries.

C0 owns this column and its migration. (Revision 6 claimed C0 needed no migration. That followed
from the broken in-memory design and is withdrawn.)

### 4.5 One authority, not four tombstones

`WorkspaceLifecycle` lives on the app loop and holds, per workspace:

| Field | Meaning |
|---|---|
| `applied_generation` | the highest generation whose effect has been applied in this process |
| `state` | `active` or `retired`, as of that generation |

Its only mutator is monotonic:

```
apply(workspace_id, generation, state):
    if generation <= applied_generation:  return      # stale or duplicate — ignore
    applied_generation, state = generation, state
```

**Subsystems ask this object instead of keeping their own sets.** `MANAGER._retired_ws` and
`registry._retired` are replaced by `lifecycle.is_retired(workspace_id)`. That is the point: with
one authority there is no fan-out to half-apply, so revision 6's partial-clear class and its
"retryable clearing" contract both stop existing rather than needing to be specified.

Monotonicity makes arrival order irrelevant, which is what closes 4.4's two races:

- delete@5 `retired` arriving **after** restore@6 `active` → `5 <= 6` → ignored;
- restore@6 `active` arriving **after** delete@7 `retired` → `6 <= 7` → ignored.

Neither needs to know the other exists.

**Teardown is separate from the guard.** Closing sockets and terminating subprocesses is not the
same act as marking a workspace refused. Teardown stays where it is, and each hook checks the
generation it was given is still current before acting — a late teardown for a superseded
generation does nothing.

### 4.6 The jobs hook is not contained by any lock

`build_jobs_retirement_hook` runs its DB work as `wait_for(to_thread(...))`
(`jobs_retirement.py:137`). **The timeout cancels the await, not the thread** — the codebase says
so in a test written for exactly this (`test_phase0_retirement.py:317`, "the wait_for is the
bound"). So:

```
retirement times out → the lifecycle operation returns → restore transitions to generation 6
                     → the orphaned thread wakes up and cancels setup and claim-analysis jobs
```

Holding a lifecycle lock cannot help: the thread is outside it, and a stale check *before* the
thread starts proves nothing about when it runs.

**The mutation is therefore generation-conditional at its own linearization point — in SQL:**

```sql
UPDATE workspace_setup_jobs SET ...
 WHERE workspace_id = :ws
   AND status IN ('queued','running','waiting_for_rubric_review')
   AND EXISTS (SELECT 1 FROM workspaces w
                WHERE w.id = :ws AND w.lifecycle_generation = :generation)
```

An orphaned thread that resumes after any later transition updates **zero rows**. The same
predicate guards the claim-analysis cancellation. This is the pattern the codebase already uses
for mutex-participating writers — re-read liveness at the point of mutation — extended from
"is it deleted" to "is it still the same generation".

### 4.7 Serialization

`WorkspaceLifecycle` holds a per-workspace `asyncio.Lock` so a teardown and a reactivation cannot
interleave on the loop. The lock is a convenience, not the correctness argument: **monotonic
`apply` and the SQL predicate are what make the design correct**, and both hold even when work
escapes the lock — which 4.6 proves it can.

Ordering remains commit-then-apply, for 4.2's reason: for collab client-writes and cold hydration
the tombstone is the guard, so applying `active` before the database says live would open a real
hole.

### 4.8 Acceptance

1. **The forward race.** Pause a delete between its DB commit and `signal_workspace_retirement`;
   run a restore to completion; release the delete. The workspace is live and usable — collab
   accepts a write and a turn launches. The delete's retirement carried its own generation and
   no-ops.
2. **The reverse race.** Pause a restore between its commit and its apply; commit a new delete;
   release the restore. The workspace stays deleted. The late `active` is ignored.
3. **The post-timeout case.** Force `signal_workspace_retirement` to time out while its coroutine
   keeps running; restore; then let retirement finish. Same result as 1.
4. **The orphaned jobs thread.** Release the jobs thread only *after* a restore has committed.
   It updates zero rows: the setup job is untouched. Asserted on the row, not on the call.
5. `apply` is monotonic: a lower generation never overwrites a higher one, in either direction.
6. Generations are per workspace: a transition on A does not affect B.
7. The guard has one authority — `MANAGER._retired_ws` and `registry._retired` no longer exist.
   Proven by exercising collab and a turn launch after a transition, not by reading state.
8. No `rmtree`. Nothing removed. The only schema change is 4.4's column.

## 5. Slice C1 — restore

Un-deletes a workspace: clears `soft_deleted_at` so every liveness gate passes again, and bumps the
generation from 4.4 so every subsystem accepts it again. Admin-only, audited like inspection.
**Depends on C0.**

### 5.1 What restore is, and is not

**Restore returns a usable workspace, not an interrupted session.** Turns that were running or
queued at delete time stay cancelled. Nothing resurrects them; the owner picks up from the restored
content.

**Setup is the exception, and it is not an exception to the principle** — see 5.2.

**Restore is lossless for anything deleted after Slice A**, because that deletion destroyed
nothing. See 0.3 for why the pre-Slice-A case is not handled.

**The owner is not notified.** Restore is an administrative act with an administrative trace; a
restored workspace simply reappears. Verified safe rather than assumed: `activity/feed.py:172`
drops unmapped event kinds fail-safe, so the audit kind introduced here cannot reach the owner's
feed.

**Restore will be blocked on a purged workspace.** Slice E does not exist, so `purged_at` does not
exist and there is nothing to check yet.

### 5.2 The setup job — why "cancelled stays cancelled" does not apply

Every workspace is created with a `WorkspaceSetupJob` in `queued` (`workspaces.py:72`). Deletion
cancels it from any of three non-terminal states — `queued`, `running`,
`waiting_for_rubric_review`.

`cancelled` is a **dead end**: the retry endpoint requires `failed` and returns 404 otherwise
(`rubric.py:271`), and `WorkspaceSetup.tsx` branches only on `failed` and `done`, so every other
status renders "Setting up…" — forever. A workspace deleted during setup and then restored is
permanently stuck, which makes "returns a usable workspace" false.

**Decision: restore puts the setup job back into a state that actually progresses.** A cancelled
setup job is not user work being resurrected — it is the workspace's own construction, unfinished.
The principle in 5.1 is about not resuming someone's interrupted session; this is not that.

#### 5.2.1 The mapping

| Cancelled from | Restored to | Why |
|---|---|---|
| `queued` | `queued` | Never started. |
| `running` | **`queued`** | **Not `running`.** The worker that owned it died with the deletion, and nothing recreates it: the claim path takes only the oldest **queued** job, and `requeue_stuck_setup_jobs` will not touch it for `STUCK_SETUP_AFTER` = 30 minutes (`phases/setup.py:103`). Restoring it to `running` reproduces the stuck state this section exists to prevent, for half an hour. |
| `waiting_for_rubric_review` | `waiting_for_rubric_review` | Sections and rubric are already extracted and a human is the next actor. Re-queueing would re-run extraction over completed work. |

`running → queued` is safe because Phase-0 extraction is restartable from the beginning; it is
`waiting_for_rubric_review` that must not be re-queued, and it is the one state preserved exactly.

#### 5.2.2 Recording the source state

**C1 carries a migration**: a nullable `cancelled_from` on `workspace_setup_jobs`, plus

```sql
CHECK (cancelled_from IS NULL
       OR cancelled_from IN ('queued','running','waiting_for_rubric_review'))
```

so the mapping cannot be fed a state it has no row for. Null means "not cancelled by a deletion" —
a job cancelled any other way, or never cancelled, is left alone by restore.

**Every deletion-cancellation path must write it**, not only the retirement hook. There are three,
and a restore that misses one silently produces the stuck workspace anyway:

| Path | Site |
|---|---|
| the retirement hook | `jobs_retirement.py` (the `_CANCELLABLE` update) |
| deletion reconciliation | `jobs_retirement.py:173` |
| the running worker cancelling itself on `WorkspaceDeleted` / `WorkspaceGoneDuringSetup` | `phases/setup.py:615` |

#### 5.2.3 What restore writes

Restoring the status alone leaves a row that contradicts itself — a `queued` job carrying a
`completed_at` and a deletion error. Restore sets:

- `status` per 5.2.1;
- `cancelled_from` → `NULL` (consumed);
- `completed_at` → `NULL`;
- `last_error` → `NULL` (it holds "cancelled: workspace deleted");
- `started_at` → `NULL` **when the target is `queued`**, so the row is not born looking stuck.
  Left untouched for `waiting_for_rubric_review`, whose run genuinely started.

`attempts` is **not** reset: a job that had already burned retries before the deletion should not
get a fresh budget for having been deleted and restored.

### 5.3 Ordering

```
pre-lock read → 404 if the workspace does not exist
              → if LIVE: skip the mutex entirely; apply(current generation, active); 204

acquire_commit_mutex        (plain, NOT _live — see 5.5)
  re-read soft_deleted_at   (fresh, under the lock — it can have gone live since)
      if now live: release; apply(current generation, active); 204
  clear soft_deleted_at
  lifecycle_generation += 1            ← 4.4: allocated HERE, inside the transaction
  restore the setup job (5.2)
  write the audit event
  COMMIT                               ← one transaction: no restore without its audit

apply(workspace_id, generation, active)  ← after the commit; 4.2 and 4.7 say why
204
```

**The live path never takes the mutex.** Slice B's review found the mirror of this on inspection:
acquiring a live workspace's commit mutex before deciding to refuse it contends with that
workspace's real writers — turn commits, collab checkpoints, `editing/save.py` — and can block for
the full 10-second acquisition timeout to answer a request that touches no database state. The live
path here writes nothing, so it needs no mutex at all.

The generation the apply carries is the one this transaction produced. It is never re-read, which
is the whole of 4.4.

### 5.4 The failure contract

`apply` crosses the same `run_coroutine_threadsafe` bridge retirement uses, and can fail to
schedule or time out. Revision 6 said "restore is idempotent, so retry" without saying what the
first caller sees, and its retry could collide with the original attempt. Both are specified here.

**`apply` is monotonic (4.5), so it is safe to call any number of times, in any order.** That is
what makes everything below work.

| Outcome of the bridge | Response | Why |
|---|---|---|
| applied within the bound | **204** | Done. |
| **timed out** | **503**, `Retry-After: 2` | The database transition committed; reactivation is unconfirmed. Returning 204 would promise something unverified. |
| failed to schedule | **503**, `Retry-After: 2` | Same. |

**A timed-out apply is NOT cancelled.** Like retirement, it keeps running on the loop and may land
later. This is safe precisely because `apply` is monotonic: the late one and any retry converge on
the same state, and whichever arrives second is ignored.

**Retries coalesce by construction.** N applies of the same generation equal one. No joining,
no de-duplication, no in-flight registry.

#### 5.4.1 Status codes

| Workspace state | Response |
|---|---|
| Missing | 404 |
| Deleted | full restore → 204 (or 503 per the table above) |
| Live | `apply(current generation, active)` only; no DB write, no audit → **204** |

**A live workspace returns 204, not 409.** Revision 6 returned 409 for "live and current", which
breaks the recovery path it promised: a retry that races the original apply finds the workspace
live and current and would be told 409 — the failure code — for the attempt that succeeded. Since
`apply` is idempotent, answering 204 whenever the end state holds is both simpler and correct.

The cost is that restoring a workspace that was never deleted also returns 204. That is accepted
deliberately: distinguishing "never deleted" from "restored a moment ago" is exactly the race that
made 409 wrong, and the operation is a no-op in both cases. **The audit distinguishes them** — it
is written only when a transition actually occurred.

#### 5.4.2 A lost response

If the response never reaches the admin, they retry: the workspace is live, the retry re-applies
and returns 204, and no second audit row is written. The end state is the same whether the
original landed or not.

### 5.5 The mutex

The plain `acquire_commit_mutex` (`drafting/locking.py:205`), never `acquire_commit_mutex_live`
(`:267`) — the `_live` variant re-reads liveness and raises `WorkspaceDeleted`, so it would refuse
every workspace restore exists to serve. Identical trap to inspection (3.2), identical answer.

It returns `None` and is not a context manager. It is taken **only on the deleted path**, after
the pre-lock read — so neither a missing workspace nor a live one contends for a mutex the request
does not need (5.3).

Restore is the one path that makes the mutex load-bearing rather than defensive: it **flips** the
liveness flag every other writer re-reads under that same mutex, making the flip a linearization
point. It does **not** serialize against retirement — nothing about the mutex can, per 4.1. That is
C0's job.

`CommitMutexTimeout` → 429 + `Retry-After: 2` + `BUSY_ERROR_MESSAGE`, matching every other caller.

### 5.6 API surface

| Route | Purpose |
|---|---|
| `POST /api/admin/workspaces/deleted/{workspace_id}/restore` | Clear `soft_deleted_at`, restore the setup job, bump the generation; 204 |

`require_admin` (`auth/dependencies.py:50`); a non-admin gets 403. Status codes per 5.4.1;
`CommitMutexTimeout` → 429 per 5.5. No response body; a POST needs no `Cache-Control`.

### 5.7 Audit

| Kind | Payload |
|---|---|
| `admin_deleted_workspace_restored` | `workspace_id`, `admin_user_id`, `setup_job_restored_to` (null when there was nothing to restore) |

Committed in the **same transaction** as the flag clear. Unlike inspection — where the audit must
commit *before* the response because the response carries content — here the audit and the mutation
are the same fact, so atomicity is the requirement, not ordering.

### 5.8 UI

A **Restore** button with a confirmation on the admin deleted-workspace detail page. On success the
workspace is no longer deleted, so the page navigates back to the list.

**The mutation must invalidate `["admin-deleted-workspaces"]` before navigating.** The list query
inherits the app-wide 20-second freshness window (`App.tsx:13`), so without invalidation the
restored row is still on screen when the admin arrives back at the list.

**Restore makes one existing string false.** `WorkspaceSettingsModal.tsx` tells the owner deletion
cannot be undone — written that way in Slice A because it was true. It must now say the owner
cannot undo it and an administrator can restore it. Slice A refused to promise this before it
existed; the same rule applies in reverse now that it does.

### 5.9 Acceptance

1. An admin restores a deleted workspace; it leaves the deleted list and its owner can open it.
2. A missing workspace 404s; a non-admin gets 403; a live workspace 204s without writing an audit
   row.
3. Exactly one `admin_deleted_workspace_restored` event, naming the acting admin, in the same
   transaction as the flag clear — a failed commit leaves neither.
4. **Setup, all three states.** Deleted while `queued` → restored `queued`; while `running` →
   restored **`queued`**, and a worker actually claims it without waiting out `STUCK_SETUP_AFTER`;
   while `waiting_for_rubric_review` → restored to that state, **and extraction does not re-run**.
5. **All three cancellation paths set `cancelled_from`** — the retirement hook, deletion
   reconciliation, and the worker cancelling itself. A test per path, asserting the column.
6. The restored row carries no contradiction: `cancelled_from`, `completed_at` and `last_error`
   are cleared, `started_at` is cleared for a `queued` target and kept for
   `waiting_for_rubric_review`, and `attempts` is unchanged.
7. The `CHECK` rejects a `cancelled_from` outside the three source states.
8. **Fault injection: the apply times out.** The route returns 503 with `Retry-After`. A retry
   returns 204 and the workspace is usable. Exactly one audit row exists across both calls.
9. **The timed-out apply lands late.** Let the original apply complete after the retry has already
   succeeded; the workspace stays live. Monotonicity, exercised end to end.
10. Cancelled turns stay cancelled.
11. Swapping in `acquire_commit_mutex_live` turns the happy-path test red.
12. After a restore the admin returns to a list that no longer shows the workspace
    (`["admin-deleted-workspaces"]` invalidated).
13. The delete-confirmation copy no longer claims deletion cannot be undone at all.
14. No `rmtree`. Nothing removed. The only schema change is 5.2.2's column and its `CHECK`.

## 7. Risks

| Risk | Handling |
|---|---|
| Admin surface becomes a back door to live workspaces | The two **read** routes (3.1) 404 unless `soft_deleted_at IS NOT NULL`. Restore is deliberately different: it answers 204 for a live workspace because it must stay idempotent (5.4.1). It exposes nothing — no body, no state change, and no audit row — so it is not a read path into live data. |
| Using the `_live` mutex variant, which refuses deleted workspaces | Called out explicitly in 3.2 (inspection) and 5.5 (restore); acceptance 8.6 and 5.9.7 pin it |
| Unbounded list or history response | Paginated and capped, truncation stated (3.1) |
| Content served without an audit record | Audit commits before the response (3.3) |
| Inspection accidentally mutating a deleted workspace | Acceptance tests 4 and 5 assert no new commit and no collab room |
| Restore produces a zombie: live row, permanently refused by collab and turns | Generation bump un-poisons every subsystem at once (4.4); C0 acceptance 4 exercises the subsystems, not the sets |
| A delete's retirement lands after a restore and re-poisons a live workspace | Retirement is stamped with a generation and no-ops when stale (4.4); C0 acceptance 2 and 3 |
| The commit lands and the generation bump does not | Restore is idempotent on the generation (5.4); C1 acceptance 5 injects exactly this |
| A workspace deleted mid-setup restores into a permanent "Setting up…" | Setup job restored from `cancelled_from` (5.2); C1 acceptance 4 covers all three states |

## 8. Acceptance — Slice B

C0 and C1 have their own acceptance lists, at 4.6 and 5.9.

1. An admin can list soft-deleted workspaces; live workspaces are never visible through either
   route.
2. A non-admin gets 403 on both routes.
3. Inspection renders proposal text and bounded history for a soft-deleted workspace.
4. Inspection creates no git commit — the repository's `HEAD` is byte-identical before and after.
5. Inspection starts no collab room.
6. Inspection succeeds on a workspace whose `soft_deleted_at` is set — the test that would fail if
   the `_live` mutex variant were used.
7. Inspection of a soft-deleted workspace with no repository returns `repository_unavailable`, not
   a git error.
8. The list is paginated and its cap enforced; history truncation is reported when it occurs.
9. No inspection response carrying content is observable before its audit event is committed.
10. Full suite green: `uv run pytest -n 16 --dist loadfile` → `3361 passed, 1 skipped` plus this
    slice's new tests.

## 9. Deferred slices — the analysis, so it is not re-derived

Two review rounds established the following. It is recorded here in full because rediscovering it
would be expensive.

### 9.1 Slice D — workspace filesystem-activity barrier

**The problem:** nothing in this codebase can currently answer "is anything still reading or
writing this workspace's directory?"

The retirement coordinator cannot, and says so itself:

- `WorkspaceRetirement.retire` (`proposal_assist/retirement.py:74`) wraps every hook in its own
  `try/except`, logs failures, and is documented "**Never raises**; return values are discarded."
  A normal return carries no information about whether the hooks worked.
- Drafting states it outright (`drafting/registry.py:212`): retirement is "a linearization point
  for INSTALL" and "does **NOT** prove the subprocess died."
- Collab (`collab/room.py:734`) bounds its drain and, on timeout, sets `evicted = True` anyway,
  leaving "the straggler's own commit" as a documented residual.
- Pass retirement (`proposal_assist/passes.py`) bounds `_retire_entry` and, on timeout, logs and
  returns.

Each is correct for soft delete, where the DB commit already landed and the user's delete must not
fail. None can prove quiescence.

**Worse, the hook list is not the consumer list.** 41 files reach the workspace tree. Three
consumer shapes evade the hooks entirely:

- **Gate-then-write.** Reference fetch validates membership, closes its session, then performs a
  long fetch and ingestion with no re-check (`web/api/references.py:179`), after which storage
  creates directories and writes bytes. Attachment upload has the same shape
  (`web/api/files.py:87`, `attachments/storage.py:99`). **A write landing after a purge does not
  merely fail — it recreates the directory**, so a "successful" purge silently comes back.
- **Write-after-return.** Attachment and reference `FileResponse`s open and stream their paths
  after the route has returned (`web/api/files.py:169`), outside any gate.
- **Snapshot-then-read.** `reconcile_analysis_coverage` (`analysis/reconcile.py:31`) snapshots live
  workspace IDs, then reads `proposal.md` and git `HEAD` per workspace with no commit mutex, no
  running-job row, and no in-memory registry — from an APScheduler thread
  (`scheduler.py:1083`).

**Shape of the fix:** an in-process activity registry every filesystem consumer participates in,
covering a request from *before* its live-row gate through completion of its response body or
mutation. An in-process registry is sound here because the scheduler is a documented
single-instance hard constraint (`CLAUDE.md:30`), and `passes.py:79`'s
`self._live: dict[uuid.UUID, set[Entry]]` is the established precedent to follow. Writers must
additionally re-check liveness under the commit mutex immediately before creating directories.
Alternatively `reconcile_analysis_coverage` takes the commit mutex per workspace in a fresh
transaction.

### 9.2 Slice E — purge

Requires Slice D. Settled design points:

- **Removes the whole validated `<workspaces_dir>/<workspace_uuid>/` tree**, not just `repo/`.
  Repo-only would neither "delete completely" nor collect the orphaned attachment/reference bytes
  that a failed soft-delete scrub leaves behind forever. **DB rows are preserved** — filesystem
  purge, not record deletion.
- **Migration adds** `purged_at TIMESTAMPTZ NULL` and `purged_by_user_id BIGINT NULL REFERENCES
  users(id) ON DELETE SET NULL`, plus the invariant as a schema constraint, not application
  behaviour: `CHECK (purged_at IS NULL OR soft_deleted_at IS NOT NULL)`.
- **Every guard is evaluated under the mutex**, including the already-purged check — a pre-lock
  check lets two concurrent purges both pass. The second gets 409 naming the existing
  `purged_by`/`purged_at`, and never overwrites them.
- **Quiescence is proven by a separate strict path**, visibly named
  `require_workspace_quiescence_for_purge`, which raises `WorkspaceNotQuiescent(subsystem, reason)`
  naming what could not be confirmed. Not a flag on the existing coordinator — a boolean parameter
  would let a future reader mistake the best-effort path for the strict one. Unconfirmed → 503,
  nothing removed. Retrying is free; losing a directory out from under a running process is not.
- **The crash window is defined**: `rmtree` and the tombstone cannot be atomic with each other.
  Retry treats an already-absent target as physical success and proceeds to stamp; tombstone and
  audit event commit in one transaction.
- **Ordinary `rmtree` failure is distinct from process death**: an `OSError` mid-removal leaves a
  partial tree with the process alive. No tombstone and no audit event are committed, the response
  is a controlled retryable failure, and a retry removes the remainder. Fault-injected test
  required.
- **Purge validates the typed workspace title server-side** (422 on mismatch), not only in the UI —
  a guard that lives only in the client is not a guard. Its test must prove a mismatch invokes
  neither the quiescence check nor any filesystem or database mutation.
- **Acceptance must cover each quiescence failure mode independently** — an inner hook raising,
  unconfirmed drafting subprocess state, an unconfirmed pass cancellation, an unconfirmed collab
  drain — not merely an outer coordinator timeout; plus both race orders against inspection, and
  the delayed-consumer races from 9.1 (delayed reference ingestion, delayed `FileResponse`,
  delayed draft read, and the reconciliation reader).

### 9.3 Slice C — restore

Promoted out of this section in revision 5, and split in revision 6. See **4** (C0, the
lifecycle generation) and **5** (C1, restore itself).

### 9.4 The false-sweep docstring — fixed in Slice A, not deferred

`scrub_workspace_bytes`'s docstring claims a failed cleanup's files "will be collected by a future
sweep". No sweep exists anywhere in the codebase.

Revision 3 deferred this to the purge slice on the grounds that the honest replacement could only
be written once purge existed. **Revision 4 supersedes that:** Slice A takes the function off the
delete path entirely, so its docstring is being rewritten regardless — it can no longer describe
itself as "the FS half of a soft-delete". Leaving one verified-false sentence inside a docstring
being rewritten for other reasons would be deliberate, not incidental.
