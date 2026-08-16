# Admin retention surface for deleted workspaces — design

**Date:** 2026-08-16
**Status:** revision 4 — delete becomes **freeze-only**, and **restore is back in scope**.
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
| **A** | **Freeze-only delete** — stop destroying bytes on delete | scoped in 2; **ship first, see 0.1** |
| **B** | Admin inspection surface — list, read, audit | planned: `docs/superpowers/plans/2026-08-16-admin-inspection-surface.md` |
| **C** | Restore | scoped in 9.3 |
| **D** | Workspace filesystem-activity barrier | deferred, scoped in 9.1 |
| **E** | Purge, built on D | deferred, scoped in 9.2 |

A, B and C are all safe: none of them removes anything. Every hazard the reviews found — proving
quiescence, racing an `rmtree`, late writes recreating a purged tree — belongs to E alone.

### 0.1 Why freeze-only ships first

**Every deletion between now and Slice A permanently destroys data that Slice C could otherwise
restore.** Reference and attachment bytes removed today cannot come back; the workspaces they
belonged to will only ever be partially restorable, no matter what is built later.

Slice B is more visible and its plan is already written, but nothing is lost by it waiting a day.
Slice A is a one-line removal at `web/api/workspaces.py:1530` plus test updates, and it stops an
ongoing, irreversible loss. That asymmetry decides the order.

### 0.2 What this changes about deletion, stated plainly

Before: deleting a workspace removed uploaded attachments and reference files from disk.
After: deleting a workspace removes nothing; the files persist until an admin purges.

This is a deliberate policy shift, consistent with the 2026-08-12 decision that deleted
workspaces stay on disk — but it is broader than that decision's letter, which concerned `repo/`.
A user deleting a workspace may reasonably expect their uploads to be gone. **The delete
confirmation must say what deletion now does**: the workspace is frozen and hidden, and its
contents remain until an administrator removes them.

### 0.3 Workspaces deleted before Slice A

Their reference and attachment bytes are already gone. Restore (Slice C) will bring the row and
the proposal history back, but those files cannot return. Slice C must detect this — reference
and attachment rows whose files are missing — and tell the admin which workspaces are only
partially restorable, rather than restoring them silently into a broken state.

## 1. Background

Deleting a workspace is a soft delete: the row is marked `soft_deleted_at`, jobs and turns are
cancelled, collab rooms evicted, and `scrub_workspace_bytes` (`src/call_manager/workspaces.py:181`)
removes attachment and reference **bytes** while deliberately keeping their DB rows for the audit
trail.

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
destroying bytes (Slice A), restore is lossless and the objection disappears. The one residual is
0.3's pre-existing deletions, which are partially restorable and must say so.

## 2. Slice A — freeze-only delete

The whole change is to stop calling the scrubber. `scrub_workspace_bytes` has exactly one caller,
`web/api/workspaces.py:1530`, which runs it after the DB commit. Delete keeps everything else it
does: marking `soft_deleted_at`, cancelling jobs and queued turns, terminalizing running turns,
evicting collab rooms, and running the retirement coordinator. It simply stops removing files.

`scrub_workspace_bytes` itself is **kept, not deleted** — Slice E's purge needs exactly this
logic (extended to the whole tree), and re-deriving it later from a git history nobody will think
to search is worse than leaving a documented, unused-for-now function in place. Its docstring
must say it is no longer on the delete path and name the slice that will call it.

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

## 7. Risks

| Risk | Handling |
|---|---|
| Admin surface becomes a back door to live workspaces | Both routes 404 unless `soft_deleted_at IS NOT NULL` |
| Using the `_live` mutex variant, which refuses deleted workspaces | Called out explicitly in section 4; acceptance test 6 pins it |
| Unbounded list or history response | Paginated and capped, truncation stated (3.1) |
| Content served without an audit record | Audit commits before the response (5) |
| Inspection accidentally mutating a deleted workspace | Acceptance tests 4 and 5 assert no new commit and no collab room |

## 8. Acceptance

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
10. Full suite green: `uv run pytest -n 16 --dist loadfile` → `3360 passed, 1 skipped` plus this
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

Un-deletes a workspace: clears `soft_deleted_at` so every liveness gate passes again. Admin-only,
audited like inspection.

Settled points:

- **Restore does not resurrect work.** Jobs, queued turns and running turns were cancelled at
  delete time and stay cancelled. Restore returns a usable workspace, not an interrupted session.
  The owner starts again from the restored content.
- **Partial restores must be visible** (0.3). A workspace deleted before Slice A has lost its
  reference and attachment bytes. Restore must detect rows whose files are missing and report
  which ones, rather than restoring silently into a state that looks broken.
- **Restore is blocked on a purged workspace** — there is nothing to restore. Once Slice E
  exists, `purged_at IS NOT NULL` makes restore a 409.
- Restore takes the same plain `acquire_commit_mutex` as inspection, for the same reason: it must
  work on a workspace that is currently deleted, so `acquire_commit_mutex_live` would refuse it.

### 9.4 The false-sweep docstring — fixed in Slice A, not deferred

`scrub_workspace_bytes`'s docstring claims a failed cleanup's files "will be collected by a future
sweep". No sweep exists anywhere in the codebase.

Revision 3 deferred this to the purge slice on the grounds that the honest replacement could only
be written once purge existed. **Revision 4 supersedes that:** Slice A takes the function off the
delete path entirely, so its docstring is being rewritten regardless — it can no longer describe
itself as "the FS half of a soft-delete". Leaving one verified-false sentence inside a docstring
being rewritten for other reasons would be deliberate, not incidental.
