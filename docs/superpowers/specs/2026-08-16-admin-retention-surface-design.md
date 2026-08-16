# Admin retention surface for deleted workspaces — design

**Date:** 2026-08-16
**Status:** approved, not yet implemented
**Goal:** Give admins a way to inspect a deleted workspace's surviving content, and to
purge it deliberately, without weakening the owner-facing delete contract.

## 1. Why this exists

Deleting a workspace today is a soft delete: the row is marked `soft_deleted_at`, jobs and
turns are cancelled, collab rooms are evicted, and `scrub_workspace_bytes`
(`src/call_manager/workspaces.py:181`) removes attachment and reference **bytes**. Their DB
rows are kept on purpose, for the audit trail.

**`repo/` is never removed by anything.** The drafted proposal markdown and its full git
history survive indefinitely. There is no retention job, purge, or reaper anywhere in the
codebase.

The policy was settled by the user on 2026-08-12: deleted workspaces **stay on disk but are
accessible to an admin**, and an admin can **delete them completely**. Two follow-up
decisions were settled on 2026-08-16:

- **Admin access is inspection only.** No restore/undelete.
- **Purge removes bytes and leaves a tombstone.** DB rows survive.

### 1.1 Why not restore

Restore was considered and rejected on a factual ground, not a taste one: a soft delete has
already destroyed reference and attachment **bytes** while keeping their **rows**. A restored
workspace would therefore list references whose files no longer exist — broken in a way that
reads as a bug rather than as a consequence of deletion. Restoring faithfully would mean
resurrecting bytes that are already gone, which nothing can do.

## 2. Scope

**In scope:** an admin-only list of deleted workspaces; read-only inspection of the surviving
`repo/` content; a purge action that removes `repo/` and records a tombstone; audit events for
both inspection and purge.

**Out of scope, deliberately:** restore/undelete (1.1); any retention *timer* or automatic
reaper — purge stays a deliberate human act; purging attachment/reference DB rows (they are the
audit trail the current design intentionally preserves); any change to the owner-facing delete
route.

## 3. Data model

Migration **0062** adds two nullable columns to `workspaces`:

- `purged_at TIMESTAMPTZ NULL`
- `purged_by_user_id BIGINT NULL REFERENCES users(id) ON DELETE SET NULL`

`ON DELETE SET NULL` matches the convention already used for `WorkspaceEvent.actor_id`
(`db/models.py:807`) — deleting a user must not delete the record that a purge happened.

A workspace is **purged** iff `purged_at IS NOT NULL`. That state is only reachable from
`soft_deleted_at IS NOT NULL`; a live workspace can never be purged.

## 4. API surface

Three admin-only routes, all requiring `require_admin` (`auth/dependencies.py:50`), all
returning 404 for a workspace that is not soft-deleted so the admin surface cannot become a
back door to live workspaces:

| Route | Purpose |
|---|---|
| `GET /api/admin/workspaces/deleted` | List: id, title, owner, `soft_deleted_at`, `purged_at`, `purged_by` |
| `GET /api/admin/workspaces/deleted/{id}` | Inspect: proposal markdown at git HEAD + commit history |
| `POST /api/admin/workspaces/deleted/{id}/purge` | Purge (section 6) |

These are **new routes, not relaxations of the owner routes.** The owner routes gate on a live
row via the row-source-of-truth lookup; widening them to serve deleted workspaces would put the
deleted case on the same code path as the live case, where a future edit could leak one into the
other. A purged workspace returns 404 from the inspect route — its content is gone.

## 5. Inspection

Reads `repo/` directly: the proposal markdown at `HEAD` plus the commit list. No new storage, no
new indexing, nothing written.

Inspection is read-only in the strict sense — it must not create a commit, take the commit
mutex, or start a collab room. A deleted workspace has no live room and must not acquire one.

## 6. Purge

Ordered, and the order is the design:

1. **Reject unless soft-deleted.** Not soft-deleted → 404. Already purged → 409 (idempotency is
   not silently faked; the admin is told it already happened, and by whom).
2. **Take the per-workspace commit mutex**, the same linearization point
   `soft_delete_workspace_db` uses. Contention → 429, having removed nothing.
3. **Run the retirement coordinator and require it to succeed** (section 6.1).
4. **`rmtree` the `repo/` directory.**
5. **Stamp the tombstone** (`purged_at`, `purged_by_user_id`) and write the audit event.

### 6.1 The barrier must fail CLOSED — this is the non-obvious part

This feature was sequenced after Slice C because purge is the first operation in the codebase
that removes `repo/`. Today's scrub is safe partly *because* it never touches `repo/`; a purge
can pull the directory out from under a live drafting subprocess or a pass mid-read.

The existing entry point cannot be reused as-is. `signal_workspace_retirement`
(`proposal_assist/retirement.py:96`) **never raises** — a missing loop, a schedule failure, a
timeout, or a coordinator error all degrade to a logged warning. That is correct for soft
delete, where the DB commit has already landed and the user's delete must not fail because
retirement could not be scheduled. It is **wrong for purge**: an unconfirmed barrier there means
proceeding to `rmtree` without knowing whether anything is still reading the directory.

Purge therefore needs a **fail-closed variant**: if the coordinator is missing, the schedule
fails, or the barrier times out, the purge **aborts with 503 and removes nothing**. Retrying
later is free — purge has no urgency and no deadline. Losing a directory out from under a
running process is not recoverable.

The two variants must be visibly different at the call site, not one function with a boolean.
A future reader must not be able to mistake the fail-open path for the fail-closed one.

### 6.2 Purge is irreversible

The UI requires typed confirmation of the workspace title before the button arms — the same bar
the project already sets for discarding work. The response states plainly that git history is
gone, not merely the current text.

## 7. Audit

Both inspection and purge write a `WorkspaceEvent` with the acting admin as `actor_id`.

Inspection is audited deliberately: an admin reading another user's deleted proposal draft is a
privacy-adjacent act in a hospital setting, and it should leave a trace even though it changes
nothing. This is the one place the design spends effort on something no user asked for, and the
reason is that an un-audited admin read surface is the kind of thing that is uncomfortable to
explain after the fact.

## 8. UI

One new page under the existing admin shell (`AdminLayout` + `AdminNav`, matching
`AdminUsers.tsx` and siblings): a table of deleted workspaces, a detail view rendering the
proposal markdown and its commit history, and the purge action behind typed confirmation.

Purged rows stay in the list, showing "purged by X on Y" in place of an inspect link. A purged
workspace that vanished from the list would leave exactly the hole this design exists to avoid.

## 9. Risks

| Risk | Handling |
|---|---|
| `rmtree` races live work | 6.1's fail-closed barrier under the commit mutex |
| Admin surface becomes a back door to live workspaces | All three routes 404 unless `soft_deleted_at IS NOT NULL` |
| A partial `rmtree` leaves a half-deleted repo | Stamp the tombstone only after `rmtree` returns; a failed purge leaves the workspace un-purged and retryable, with the failure logged. A partially-removed `repo/` is acceptable in that window because nothing reads it — the workspace is already soft-deleted — and the retry completes it |
| Purge used as an undo for a mis-delete | It is the opposite of an undo. 6.2's typed confirmation and the explicit "git history is gone" wording |

## 10. Acceptance

1. An admin can list deleted workspaces, and cannot see live ones through these routes.
2. A non-admin gets 403 on all three routes.
3. Inspection renders the proposal text and history of a soft-deleted workspace, and creates no
   commit and no collab room.
4. Purge removes `repo/`, sets both tombstone columns, and writes an audit event.
5. **Purge aborts with 503 and removes nothing when the retirement barrier cannot be
   confirmed** — proven by a test that makes the coordinator time out and then asserts the
   directory still exists.
6. Purge on a live workspace is 404; on an already-purged workspace, 409.
7. The full suite stays green: `uv run pytest -n 16 --dist loadfile` → `3357 passed, 1 skipped`
   plus this work's new tests.

## 11. Incidental fix

`scrub_workspace_bytes`'s docstring claims a failed cleanup's files "will be collected by a
future sweep". No sweep exists anywhere in the codebase. The sentence is removed — the honest
statement is that a failed cleanup leaves orphan bytes that nothing reads and nothing collects,
which is precisely why an admin-driven purge is worth having.
