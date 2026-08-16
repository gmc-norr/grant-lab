# Admin retention surface for deleted workspaces — design

**Date:** 2026-08-16
**Status:** revision 6 — Slice C **splits into C0 (lifecycle generation) and C1 (restore)** after
a review found three blockers in revision 5. Slices A and B have shipped (`ef8ccb3`, `991e7a7`).
Partial-restore detection is dropped, with the reason recorded in 0.3.
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

### 4.4 The mechanism: a per-workspace generation

One object on the app loop, `WorkspaceLifecycle`, owns a per-workspace **generation**: a
monotonically increasing integer, starting at 0, held in memory beside the subsystems it governs.

- **Retirement is stamped.** `signal_workspace_retirement` reads the current generation and passes
  it to `retire(workspace_id, generation)`. Each hook records "retired at generation N" instead of
  "retired".
- **A subsystem is poisoned iff its recorded generation equals the current one.** A tombstone from
  generation 3 does not poison a workspace now at generation 4.
- **Reactivation is one atomic bump.** `generation += 1` un-poisons every subsystem at once. There
  is no second subsystem to fail on, so 4.3's partial-clear class does not exist.
- **Stale retirement no-ops.** A retirement scheduled for generation 3 that runs after a bump to 4
  finds its generation stale and returns without touching anything. That closes 4.1 completely,
  including retirement still running after the route's barrier timed out.

The generation is **in-memory, not persisted**. It is a within-process serialization token, and the
process is single-instance by hard constraint (APScheduler). A restart resets every generation to 0
and every tombstone with it — which is correct, because the tombstones it governs are in-memory too.

### 4.5 Serialization

`WorkspaceLifecycle` holds a per-workspace `asyncio.Lock`. Retirement and reactivation both acquire
it, so they cannot interleave; the generation decides which one is stale. Both run on the app loop.

Ordering is still **commit-then-bump** — see 4.2 for why bumping first is unsafe. But the bump is
now a single in-memory operation behind a lock, not a fan-out, so "it half-worked" is not a
reachable state. What remains reachable is "it did not happen at all", which 5.4 makes retryable.

### 4.6 Acceptance

1. A retirement stamped with a stale generation no-ops: it does not re-add a tombstone, does not
   cancel jobs, and does not touch passes.
2. **The 4.1 race, run as a test.** Pause a delete between its DB commit and
   `signal_workspace_retirement`; run a restore to completion; release the delete. The workspace is
   live and usable — collab accepts a write and a turn launches. Restore wins last.
3. **The post-timeout case.** Force `signal_workspace_retirement` to time out while its coroutine
   keeps running, restore, then let retirement finish. Same result.
4. One bump un-poisons both subsystems. Proven by exercising collab and a turn launch, not by
   reading the sets — reading them would pass even if the bump touched the wrong workspace.
5. Generations are per workspace: bumping A does not un-poison B.
6. No migration in C0. No `rmtree`. Nothing removed.

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

Every workspace is created with a `WorkspaceSetupJob` in `queued` (`workspaces.py:72`). Retirement
cancels it from any of three non-terminal states — `queued`, `running`,
`waiting_for_rubric_review` (`jobs_retirement.py:39`).

`cancelled` is a **dead end**:

- the retry endpoint requires `failed` and returns **404** for anything else (`rubric.py:271`);
- `WorkspaceSetup.tsx` branches only on `failed` and `done`, so every other status renders
  "Setting up…" — forever;
- the repository may not exist yet, a state 3.2 already recognises.

So a workspace deleted during setup and then restored is permanently stuck. That makes "returns a
usable workspace" false.

**Decision: restore puts the setup job back to the state it was cancelled from.** A cancelled setup
job is not user work being resurrected — it is the workspace's own construction, unfinished. The
principle in 5.1 is about not resuming someone's interrupted session, and that is not this.

Restoring all three states to `queued` would be wrong: a job parked at
`waiting_for_rubric_review` has already extracted its sections and rubric and is waiting on a
human, and re-queueing it would re-run extraction over work already done.

**C1 therefore carries a migration**: a nullable `cancelled_from` column on `workspace_setup_jobs`,
written by the retirement hook when it cancels and consumed by restore. Null means "not cancelled
by retirement" — a job cancelled any other way, or never cancelled, is left alone.

### 5.3 Ordering

```
pre-lock read           → 404 if missing; see 5.4 for live
acquire_commit_mutex      (plain, NOT _live — see 5.5)
  re-read soft_deleted_at (fresh, under the lock)
  clear soft_deleted_at
  restore the setup job from cancelled_from, if set
  write the audit event
  COMMIT                  ← one transaction: no restore without its audit
bump the generation       ← after the commit; 4.2 says why not before
204
```

### 5.4 Idempotence — the retryable contract

The bump can fail to happen at all: the bridge to the app loop can fail to schedule, or time out.
The DB then says live while the generation still says poisoned.

**Restore is therefore idempotent on the generation.** A POST against a workspace that is already
live but whose generation is still stale performs the bump alone and returns 204. It does not 404.

That gives three cases:

| Workspace state | Response |
|---|---|
| Deleted | Full restore: clear, setup job, audit, commit, bump → 204 |
| Live, generation stale (a previous bump did not land) | Bump only → 204 |
| Live, generation current | 409 — nothing to do |
| Missing | 404 |

The second row is what makes the operation retryable, and it is the whole answer to "the commit
landed and the bump did not". An admin retries the same button.

The audit is written only in the first case: the second changes no database state, and an audit row
per retry would misreport how many times the workspace was restored.

### 5.5 The mutex

The plain `acquire_commit_mutex` (`drafting/locking.py:205`), never `acquire_commit_mutex_live`
(`:267`) — the `_live` variant re-reads liveness and raises `WorkspaceDeleted`, so it would refuse
every workspace restore exists to serve. Identical trap to inspection (3.2), identical answer.

It returns `None` and is not a context manager. It is taken **after** the pre-lock 404, so an
attempt against a missing workspace never contends for a mutex it will refuse anyway.

Restore is the one path that makes the mutex load-bearing rather than defensive: it **flips** the
liveness flag every other writer re-reads under that same mutex, making the flip a linearization
point. It does **not** serialize against retirement — nothing about the mutex can, per 4.1. That is
C0's job.

`CommitMutexTimeout` → 429 + `Retry-After: 2` + `BUSY_ERROR_MESSAGE`, matching every other caller.

### 5.6 API surface

| Route | Purpose |
|---|---|
| `POST /api/admin/workspaces/deleted/{workspace_id}/restore` | Clear `soft_deleted_at`, restore the setup job, bump the generation; 204 |

`require_admin` (`auth/dependencies.py:50`); a non-admin gets 403. Status codes per 5.4. No
response body; a POST needs no `Cache-Control`.

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
2. A missing workspace 404s; a live, current one 409s; a non-admin gets 403.
3. Exactly one `admin_deleted_workspace_restored` event, naming the acting admin, in the same
   transaction as the flag clear — a failed commit leaves neither.
4. **Setup, all three states.** A workspace deleted while `queued`, while `running`, and while
   `waiting_for_rubric_review` restores to that same state — and the `waiting_for_rubric_review`
   case does not re-run extraction.
5. **Fault injection: the bump does not land.** Force the bridge to fail after the commit. The
   workspace is live and poisoned; a second POST returns 204 and makes it usable; no second audit
   row is written.
6. Cancelled turns stay cancelled.
7. Swapping in `acquire_commit_mutex_live` turns the happy-path test red.
8. The list is invalidated: after a restore the admin returns to a list that no longer shows it.
9. The delete-confirmation copy no longer claims deletion cannot be undone at all.
10. No `rmtree`. Nothing removed. The only schema change is 5.2's nullable column.

## 7. Risks

| Risk | Handling |
|---|---|
| Admin surface becomes a back door to live workspaces | Both routes 404 unless `soft_deleted_at IS NOT NULL` |
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
