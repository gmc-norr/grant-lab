# Admin retention surface for deleted workspaces — design

**Date:** 2026-08-16
**Status:** revision 2 (post-review), approved-in-principle, not yet implemented
**Goal:** Give admins a way to inspect a deleted workspace's surviving content, and to purge it
deliberately, without weakening the owner-facing delete contract.

Revision 2 folds in a review that found the fail-closed decision correct but the mechanism
insufficient: the existing retirement coordinator **cannot confirm success**, and the inspection
route this design adds is itself a repo reader that could be rmtree'd out from under. Sections
6.1, 6.2, 5 and 3 are substantially rewritten.

## 1. Why this exists

Deleting a workspace today is a soft delete: the row is marked `soft_deleted_at`, jobs and turns
are cancelled, collab rooms are evicted, and `scrub_workspace_bytes`
(`src/call_manager/workspaces.py:181`) removes attachment and reference **bytes**. Their DB rows
are kept on purpose, for the audit trail.

**`repo/` is never removed by anything.** The drafted proposal markdown and its full git history
survive indefinitely. There is no retention job, purge, or reaper anywhere in the codebase.

Policy settled by the user on 2026-08-12: deleted workspaces **stay on disk but are accessible
to an admin**, and an admin can **delete them completely**. Two follow-ups settled 2026-08-16:

- **Admin access is inspection only.** No restore/undelete.
- **Purge removes bytes and leaves a tombstone.** DB rows survive.

### 1.1 Why not restore

Rejected on a factual ground, not taste: a soft delete has already destroyed reference and
attachment **bytes** while keeping their **rows**. A restored workspace would list references
whose files no longer exist — broken in a way that reads as a bug rather than as a consequence
of deletion. Restoring faithfully would mean resurrecting bytes that are already gone.

## 2. Scope

**In scope:** an admin-only list of deleted workspaces; read-only inspection of surviving
content; a purge that removes the workspace's on-disk tree and records a tombstone; audit events
for both.

**Out of scope, deliberately:** restore/undelete (1.1); any retention *timer* or automatic
reaper — purge stays a deliberate human act; deleting any DB row (rows are the audit trail);
any change to the owner-facing delete route.

## 3. Data model

Migration **0062** adds two nullable columns to `workspaces`:

- `purged_at TIMESTAMPTZ NULL`
- `purged_by_user_id BIGINT NULL REFERENCES users(id) ON DELETE SET NULL`

`ON DELETE SET NULL` matches `WorkspaceEvent.actor_id` (`db/models.py:807`) — deleting a user
must not delete the record that a purge happened.

**The live/purged invariant is enforced in the schema, not merely in application code:**

```sql
CHECK (purged_at IS NULL OR soft_deleted_at IS NOT NULL)
```

A route guard can be bypassed by the next caller; a CHECK constraint cannot. The model's
`__table_args__` must carry it in the same task as the migration.

## 4. API surface

Three admin-only routes, all requiring `require_admin` (`auth/dependencies.py:50`), all
returning 404 unless `soft_deleted_at IS NOT NULL`, so the admin surface cannot become a back
door to live workspaces:

| Route | Purpose |
|---|---|
| `GET /api/admin/workspaces/deleted` | List (paginated, see 4.1) |
| `GET /api/admin/workspaces/deleted/{id}` | Inspect (section 5) |
| `POST /api/admin/workspaces/deleted/{id}/purge` | Purge (section 6) |

These are **new routes, not relaxations of the owner routes.** The owner routes gate on a live
row; widening them would put the deleted case on the same code path as the live case, where a
future edit could leak one into the other.

### 4.1 Bounding

Both list endpoints are bounded, because both are unbounded by nature — a long-lived instance
accumulates deleted workspaces, and a workspace's git history grows with every save.

- The deleted-workspace list is paginated (`limit`, default 50, max 200; `offset`), newest
  `soft_deleted_at` first, and returns a total count.
- The inspected commit history is capped at the most recent 200 commits, and the response
  states explicitly when it was truncated. An admin needing the full history has the purge
  audit trail and, until purge, the repository itself.

### 4.2 Purge request validation

The purge endpoint requires the workspace title in its request body and rejects a mismatch with
422 **server-side**. The UI's typed confirmation (6.4) is a usability affordance; it is not a
guard. A guard that lives only in the client is not a guard.

## 5. Inspection

Reads the surviving repository: the proposal markdown at `HEAD` plus the commit list. No new
storage, no indexing.

**Inspection takes the per-workspace commit mutex for the whole git read.** Revision 1 forbade
this on the reasoning that inspection is read-only. That was wrong: inspection is *itself a repo
reader*, the retirement coordinator has no visibility into admin HTTP reads, and so purge could
receive a confirmed barrier and still rmtree the directory out from under an in-flight
inspection. Holding the mutex makes inspection and purge mutually exclusive at the same
linearization point everything else in this workspace uses.

Under the mutex, inspection **re-reads `soft_deleted_at` and `purged_at` fresh** before serving
content — the values it was routed on may be stale.

Inspection remains read-only with respect to git and collab: it must not create a commit and
must not start a collab room. A deleted workspace has no live room and must not acquire one.

### 5.1 Missing or invalid repository

A soft-deleted, un-purged workspace may legitimately have no usable repository: it can be
deleted before setup ever created one, and a partially-completed purge (6.3) leaves one absent.
Inspection returns a deliberate **`repository_unavailable`** state in both cases, rather than
surfacing a raw git error. The admin sees "this workspace has no readable repository," which is
true and actionable, instead of a stack trace that looks like a defect.

## 6. Purge

### 6.1 Quiescence must be PROVEN, not signalled

This is the load-bearing section, and revision 1 got it wrong.

Purge is the first operation in the codebase that removes `repo/`. Revision 1 proposed reusing
the retirement coordinator through a "fail-closed bridge" that raises on timeout. **That is
insufficient**, because the coordinator and its hooks are structurally best-effort:

- `WorkspaceRetirement.retire` (`proposal_assist/retirement.py:74`) wraps every hook in its own
  `try/except`, logs failures, and is documented "**Never raises**; return values are
  discarded." A normal return therefore carries no information about whether the hooks worked.
- Drafting says so explicitly (`drafting/registry.py:212`): retirement is "a linearization point
  for INSTALL," and "does **NOT** prove the subprocess died."
- Pass retirement (`proposal_assist/passes.py`) bounds `_retire_entry` with `asyncio.wait_for`
  and, on timeout, logs and returns.
- Collab (`collab/room.py:734`) bounds its lock/ops drain and, on timeout, sets `evicted = True`
  anyway, leaving "the straggler's own commit" as a documented residual.

Every one of those is the right behaviour for soft delete, where the DB commit has already
landed and the user's delete must not fail. None of them can be reused to answer the only
question purge cares about: **is anything still reading this directory?**

Purge therefore requires a **separate, strict, end-to-end path**, visibly named
`require_workspace_quiescence_for_purge`, which either proves no reader remains or raises
`WorkspaceNotQuiescent(subsystem, reason)`. It is **not** a flag on the existing coordinator and
not a wrapper around it — a boolean parameter would let a future reader mistake the best-effort
path for the strict one.

Each subsystem supplies a strict contract that **proves emptiness or raises**:

| Subsystem | Strict contract |
|---|---|
| drafting | no live entry for the workspace **and** no live subprocess; an unconfirmable subprocess raises rather than logging |
| passes | no live pass entries remain after cancellation; a cancel that does not confirm within its bound raises |
| collab | the room is evicted **and** its ops drained under the lock; a drain timeout raises instead of setting the flag and continuing |
| turns / jobs | no rows in a running state for this workspace |

**Unconfirmed state must stay discoverable on retry.** The failure names the subsystem that
could not be confirmed, and the purge response carries it, so an admin retrying knows what was
outstanding rather than seeing an opaque failure. A coroutine returning is not confirmation.

On any `WorkspaceNotQuiescent`, purge **aborts with 503 and removes nothing**. Retrying later is
free — purge has no urgency and no deadline. Losing a directory out from under a running process
is not recoverable.

### 6.2 Ordering — every guard under the mutex

Revision 1 checked `purged_at` before acquiring the mutex, so two concurrent purges could both
pass the check. Corrected: **no guard is evaluated, and no filesystem action taken, before the
mutex is held.**

1. **Acquire the per-workspace commit mutex** — the same linearization point
   `soft_delete_workspace_db` and inspection (5) use. Contention → 429, nothing removed.
2. **Fresh re-read under the mutex.** Not soft-deleted → 404. Already purged → **409, naming the
   existing `purged_by`/`purged_at`, and never overwriting them.**
3. **`require_workspace_quiescence_for_purge`** (6.1). Not quiescent → 503, nothing removed.
4. **Remove the on-disk tree** (6.3).
5. **Stamp the tombstone and write the audit event in ONE transaction** (6.3).

### 6.3 What is removed, and the crash window

**Purge removes the whole validated `<workspaces_dir>/<workspace_uuid>/` tree**, not only
`repo/`. Revision 1 removed `repo/` alone, which contradicted its own section 11: a failed
soft-delete scrub leaves attachment and reference bytes that nothing ever collects, so a
repo-only purge would neither "delete completely" nor collect those orphans. **DB rows are still
preserved** — this is a filesystem purge, not a record deletion.

The path is resolved and validated to be exactly the workspace's directory under
`workspaces_dir` before any removal.

**The crash window is defined rather than assumed.** `rmtree` and the tombstone cannot be made
atomic with each other: the process can die after the tree is gone and before the row is
stamped, leaving an absent repository with `purged_at IS NULL`.

- **Retry treats an already-absent target as physical success** and proceeds to stamp the
  tombstone and write the audit event. It does not fail because there was nothing to remove.
- **The tombstone and the audit event commit in one transaction**, so a purge can never be
  recorded without its audit event or vice versa.
- In that window the workspace is un-purged with a missing repository, which inspection reports
  as `repository_unavailable` (5.1).

Revision 1 justified tolerating a partial removal by saying "nothing reads it." **That is no
longer true once the inspection route exists.** The accurate statement: normal workspace
processing cannot read it (the workspace is soft-deleted, and every live path gates on a live
row), and inspection — serialized against purge by the mutex — reports the repository as
unavailable until a retry completes.

### 6.4 Purge is irreversible

The UI requires the workspace title typed before the button arms, and the API independently
enforces the same match (4.2). The response states plainly that git history is gone, not merely
the current text.

## 7. Audit

Two event kinds on `WorkspaceEvent`, both with the acting admin as `actor_id`:

| Kind | Payload |
|---|---|
| `admin_deleted_workspace_inspected` | `workspace_id`, `admin_user_id`, whether content was served, commit count returned, truncation flag |
| `admin_deleted_workspace_purged` | `workspace_id`, `admin_user_id`, `purged_at`, bytes-removed indicator, and the outcome of the quiescence check |

**No inspection response containing proposal content may be returned until its audit event is
committed.** An audit written after the response, or best-effort alongside it, fails exactly
when it matters — a crash mid-response leaves content served and no record of it. Ordering the
commit first costs one round-trip and makes the trace unconditional.

Inspection is audited deliberately: an admin reading another user's deleted proposal draft is a
privacy-adjacent act in a hospital setting, and it should leave a trace even though it changes
nothing.

## 8. UI

One new page under the existing admin shell (`AdminLayout` + `AdminNav`, matching
`AdminUsers.tsx` and siblings): a paginated table of deleted workspaces, a detail view rendering
the proposal markdown and its (possibly truncated) commit history, and the purge action behind
typed confirmation.

Purged rows stay in the list, showing "purged by X on Y" in place of an inspect link. A purged
workspace that vanished would leave exactly the hole this design exists to avoid.

## 9. Risks

| Risk | Handling |
|---|---|
| `rmtree` races live work | 6.1's strict, proving quiescence check, under the mutex |
| `rmtree` races an admin inspection | Inspection holds the same mutex for its whole read (5) |
| Two concurrent purges | Every guard re-read under the mutex; second gets 409 (6.2) |
| Crash between `rmtree` and tombstone | Defined and retryable (6.3) |
| Admin surface as a back door to live workspaces | All three routes 404 unless soft-deleted; plus the schema CHECK (3) |
| Unbounded list or history response | Paginated and capped, truncation stated (4.1) |
| Client-only confirmation | Title match enforced server-side (4.2) |

## 10. Acceptance

1. An admin can list deleted workspaces; live ones are never visible through these routes.
2. A non-admin gets 403 on all three routes.
3. Inspection renders proposal text and history of a soft-deleted workspace, creating no commit
   and no collab room.
4. Purge removes the workspace tree, sets both tombstone columns, and writes its audit event —
   tombstone and event in one transaction.
5. **Purge aborts with 503 and removes nothing when quiescence cannot be proven.** Covered for
   each failure mode independently, not only an outer coordinator timeout:
   - an inner hook raising,
   - unconfirmed drafting subprocess state,
   - an unconfirmed pass cancellation,
   - an unconfirmed collab drain.
   Each asserts the directory still exists afterwards and that the response names the
   unconfirmed subsystem.
6. **Both race orders are tested:** inspection-first delays a concurrent purge; purge-first
   makes a concurrent inspection re-read the tombstone under the mutex and return 404.
7. Purge on a live workspace is 404; on an already-purged workspace, 409 with the original
   `purged_by` unchanged.
8. A retry after a simulated crash (tree already absent, `purged_at` still null) succeeds,
   stamping the tombstone rather than failing on the missing directory.
9. Inspection of a soft-deleted workspace with no repository returns `repository_unavailable`,
   not a git error.
10. The schema CHECK rejects a `purged_at` on a live workspace at the database level.
11. No inspection response carrying content is observable before its audit event is committed.
12. Full suite green: `uv run pytest -n 16 --dist loadfile` → `3357 passed, 1 skipped` plus this
    work's new tests.

## 11. Incidental fix

`scrub_workspace_bytes`'s docstring claims a failed cleanup's files "will be collected by a
future sweep". No sweep exists. The sentence is removed. With 6.3's whole-tree purge, the honest
replacement is that orphaned bytes persist until an admin purges the workspace — which is now a
real mechanism rather than an aspiration.
