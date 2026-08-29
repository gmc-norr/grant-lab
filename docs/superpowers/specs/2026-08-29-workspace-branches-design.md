# Workspace Branches — Design

**Date:** 2026-08-29
**Status:** Design agreed with the user; not implemented.
**Supersedes, in scope:** `2026-08-15-branches-merging-history-graph-design.md`.
That document specifies branches, merging AND the history graph together, with
per-branch git worktrees and isolated concurrent editing. This one keeps its
user-facing vocabulary (version, branch, compare, restore, merge) and narrows it
to what the current architecture supports without re-keying the collaborative
editor. The graph lanes remain that document's, unbuilt.

---

## 1. What this is for

Grant applications need parallel exploration before a decision: a conservative
and an ambitious framing of the aims, a short version for a page limit, a
funder-specific adaptation. Today a workspace has exactly one line of history and
no way to hold two of these at once.

## 2. The constraint that shapes everything

The live document is **not** git. It is a Yjs CRDT log in `yjs_updates`, keyed
`(workspace_id, artifact)` — two columns, no third (`models.py:2003-2028`). The
in-memory room is keyed the same way (`room.py:725`). Git is the checkpoint
store behind it.

So a branch cannot be a per-person view without adding a branch dimension to the
room key, every Yjs row, the checkpoint watermark, and the retirement paths.

**Decision: branches are workspace-level.** Switching moves the whole workspace.
One workspace remains one room. Per-person branches can be layered on later; the
reverse would be a rewrite.

## 3. What a branch is

A **real git branch** in the workspace's existing repo at
`var/workspaces/<uuid>/repo`.

This works because `write_and_commit_files` runs a bare `git commit`
(`gitio.py:198`) with no ref argument — every existing write already follows
whatever HEAD is. Nothing in `gitio.py` names a branch today, and nothing needs
to change for writes to land on the checked-out one.

The rejected alternative was a copied workspace with a parent pointer. It forks
everything keyed on `workspace_id` — `ProposedChange`, `ArtifactVersion`,
citations, members, viewers, settings, the setup job — and turns merge into a
cross-workspace problem with no shared history to merge against.

## 4. Switching a branch

### 4.1 The database, not git HEAD, says which branch a workspace is on

A new column, `workspaces.current_branch` (text, not null, default `'main'`),
is the **source of truth**. Git HEAD is *reconciled* to it before any git read or
write on that workspace.

This exists because a crash between moving HEAD and committing the database
would otherwise leave the two disagreeing with no way to tell which is right —
and the wrong answer means writing one branch's content onto another. With the
column, HEAD is a cache: if they differ, HEAD is wrong and gets checked out
again. Reconciliation is one `git symbolic-ref HEAD` comparison.

### 4.2 The sequence

All of steps 2-8 run inside **one transaction** holding the per-workspace commit
mutex, taken at the caller's stamped generation.

1. **Refuse** if a drafting turn is `running` (§6.3). Outside the transaction.
2. **Take the commit mutex** at the caller's generation
   (`acquire_commit_mutex_at_generation`).
3. **Evict the room FIRST** — before anything else touches the log.
4. **Checkpoint** the now-frozen log to the branch being left
   (`checkpoint_artifact`), for both artifacts.
5. **Truncate** the log (`store.truncate_log`) and **reset the watermark**
   (`state.reset_checkpoint`).
6. **Bump** `lifecycle_generation` (`workspaces.py:109`).
7. **Set** `workspaces.current_branch` to the target.
8. **Check out** the target branch in git.
9. **Commit** the transaction. On rollback, check the old branch back out — a
   compensating action, because step 8 is not transactional.

`load_or_hydrate` then seeds the empty store from the new HEAD on the next
connect (`hydrate.py:30-35`).

### 4.3 Why evict comes first

**This ordering is a correctness requirement, not a preference.**

`Room.handle` persists a client update inside the room lock, and — in the code's
own words at `room.py:731-738` — that persist *"commits a YjsUpdate row with NO
commit mutex and NO soft_deleted_at read, so it is invisible to both room.ops
and B4."*

A connected client therefore writes to the log **without taking the mutex**. If
the log were truncated before the room was tombstoned, one keystroke landing in
that window would make the store non-empty again. `load_or_hydrate` returns
early when it finds rows (`hydrate.py:30`), so it would never read the new
branch: **the workspace would show the old branch's text under the new branch's
name, and the next checkpoint would commit it there.**

`RoomManager.evict` (`room.py:656`) sets the tombstone *under the room lock*,
which the same comment identifies as the client-write linearization point:
holding that lock proves no persist is in flight, and every later frame drops at
the `if self.evicted` check (`room.py:197`). Evicting first is what makes steps
4 and 5 safe.

### 4.4 Why a reconnect cannot race the switch

A client that reconnects mid-switch would hydrate from whatever HEAD currently
is. It cannot: `load_or_hydrate` takes the **same commit mutex** before seeding
(`hydrate.py:55-72`), so it blocks until the switch's transaction ends, and then
sees the finished state.

### 4.5 Failure at each step

| Fails at | Result | Recovery |
| --- | --- | --- |
| 1-3 | Nothing changed | None needed |
| 4 (checkpoint) | Transaction rolls back; log intact | None needed. The switch did not happen |
| 5 (truncate) | Rolls back with its checkpoint — the `handover.py:60-62` rule: a truncate is never visible without its checkpoint | None needed |
| 8 (checkout) | Rolls back; `current_branch` reverts | None needed |
| 9 (commit) | HEAD may be moved but the DB reverted | Compensating checkout; and §4.1 reconciliation repairs it even after a crash |
| Crash between 8 and 9 | HEAD on the target, DB on the source | §4.1: the DB wins, HEAD is checked out again on next access |

## 5. Connected editors

Switching **evicts** every client in the room, as **step 3** — before the log is
touched. §4.3 explains why that position is a correctness requirement rather
than a preference. `RoomManager.evict` (`room.py:656`) sets the `evicted`
tombstone under the room lock, so any in-flight operation rechecks and aborts
rather than persisting into the wrong branch (`room.py:197`).

Clients reconnect on their own and re-sync from the new branch. Since
`a0e215d2` the editor gates content on `synced`, so a reconnecting client shows
"Loading the draft…" rather than a blank page.

**Accepted cost:** a collaborator mid-sentence loses that sentence — their
keystroke can land after the drain. Refusing to switch while anyone else is
connected was considered and rejected: it makes the feature unusable the moment
a workspace is shared.

## 6. Safety: what happens to work already in flight

### 6.1 The hazard, precisely

`editing/save.py:235` and `citations/human_cite.py:138` both guard a stale write
with:

```python
if head != base_commit_sha and live.sha != base_section_sha:
    raise StaleSectionError(...)
```

That is an **AND**. The write is allowed when *either* the commit matches **or**
the section's bytes match. Immediately after branching, most sections are
byte-identical on both branches — so an edit form opened on `main` would pass
this guard after a switch to another branch and **write to the wrong branch,
silently.** That is the normal case, not an edge case.

### 6.2 The fence

**A branch switch bumps `workspaces.lifecycle_generation`** via
`bump_lifecycle_generation` (`workspaces.py:109`), which computes `+ 1` in the
database and never read-modify-writes from Python. Step 6 of §4.2.

Most write paths reach the commit mutex through
`acquire_commit_mutex_at_generation` (`fencing.py:115`), which refuses when the
workspace has moved past the caller's stamped generation. One bump invalidates
all of those at once — including paths neither the spec author nor the
implementer enumerated.

**Two repository writers are NOT fenced, and this must be fixed as part of the
work.** Measured 2026-08-29 by grepping every `write_and_commit_files` caller
for `acquire_commit_mutex`:

| Writer | Reached from | Mutex |
| --- | --- | --- |
| `citations/bibliography.py:627` (`build_bibliography`) | `export.py:81`, `tools/audit.py:214` (agent-facing) | **none** |
| `review/pass_runner.py:349` | the review pass background job | **none** |

Both commit to whatever HEAD is, at any moment, with no generation check. After
a branch switch either could write the previous branch's output onto the new
branch. Neither is hypothetical: the audit tool is agent-callable and the review
pass is a scheduled job.

**Requirement:** both take `acquire_commit_mutex_at_generation` at a stamped
generation before writing, like every other writer. If that proves impossible
for one of them within this work, it is an ESCALATE, not a decision — a branch
feature that ships with an unfenced writer can silently cross-contaminate
branches.

This is the same fence deletion and restore already use (`workspaces.py:237`,
`:341`). Adding the branch to each form's staleness payload was rejected: it
requires finding every write path, and the one missed is a silent wrong-branch
write.

### 6.3 Running agent turns

A switch is **refused** while a drafting turn is `running` (there is a unique
partial index on it, `models.py:891`). A turn is minutes of model work that would
commit into a branch the user has left; failing it with the generation fence
wastes that work silently. The refusal message names the reason: the assistant is
drafting; wait or cancel.

## 7. Merging

### 7.1 The unit is a section, not a line

`editing/sectionizer.py` splits an artifact into `##`-headed sections. Each
`Section` carries a content `sha` (`sectionizer.py:30`), and the module's stated
invariant is that `"".join(s.text for s in split_sections(doc)) == doc`
byte-identically.

Merging therefore composes a document by concatenating chosen sections, with no
reconstruction step and no risk of losing bytes between them.

`git merge` was rejected. Git merges by line; two edits inside one paragraph of
Specific Aims produce conflict markers **inside prose the user exports to a
funder**, and git has no concept of a section.

### 7.2 A merge base is required

Comparing the source branch against the target can only say **equal or
different**. It cannot say *which side changed*, so "changed on one side only"
is undefined without a third point of reference.

The merge therefore reads the artifact at **three** commits:

- **base** — `git merge-base <target> <source>`, a new `gitio` helper
- **target** — the branch being merged into
- **source** — the branch being merged

Each is read with the existing `blob_sha(repo_dir, ref, path)` (`gitio.py:324`,
which already accepts an arbitrary ref) and `read_blob` (`:346`), then split with
`split_sections`.

### 7.3 Matching

Sections are matched by **heading**, not by index. Index is positional and shifts
whenever a section is added or removed; heading survives reordering.

Where a heading appears more than once **and the number of occurrences is the
same in base, source and target**, sections are matched by
`(heading, occurrence number)`: the second `## Budget` pairs with the second
`## Budget`.

**Where that count differs between any two of the three, every section with that
heading is surfaced as a conflict for the user to resolve, and none of them is
paired automatically.** Positional pairing is wrong exactly when it looks
plausible: if one branch deletes the first of two `## Budget` sections, the
survivor becomes occurrence 1 and would be silently paired with — and treated as
an edit of — the other branch's untouched first section.

Nothing in the app prevents duplicate headings, though neither real workspace
document has any (6/6 and 4/4 unique, measured 2026-08-29).

### 7.4 The three-way outcome

For each matched section, compare `Section.sha` at base, source and target:

| base vs target | base vs source | Behaviour |
| --- | --- | --- |
| same | same | Nothing changed. Keep it. |
| same | **differs** | Changed on the source only. **Take it.** No prompt. |
| **differs** | same | Changed on the target only. **Keep it.** No prompt. |
| **differs** | **differs**, and source ≠ target | Changed on **both**. The user chooses: theirs, mine, or keep both. |
| **differs** | **differs**, and source == target | Both made the same edit. Keep it. No prompt. |

Presence is resolved the same way against base:

| In base | In target | In source | Behaviour |
| --- | --- | --- | --- |
| no | no | yes | Added on the source. User chooses: take it or not. |
| no | yes | no | Added on the target. Keep it. |
| yes | yes | no | Deleted on the source. User chooses. |
| yes | no | yes | Deleted on the target. User chooses. |
| yes | no | no | Deleted on both. Stays deleted, no prompt. |
| no | yes | yes | Added on both. Conflict if the bodies differ. |

### 7.4.1 Renames are shown as a delete plus an add

Renaming a heading breaks the match: the tool sees one section removed and
another added. **This is accepted and displayed honestly** — the user sees both
entries and resolves them.

Content-similarity guessing ("did you rename this?") was rejected for now: it is
additive later and needs no data change. A stable per-section id was rejected
outright, because it means writing machine markup into `proposal.md`, the file
the user exports to the funder.

### 7.5 Order of the merged document

The **target** branch's order — the branch being merged into. A section arriving
from the source is placed after whatever heading preceded it on the source
branch; if that predecessor does not exist in the target, it goes last.

### 7.6 The merge commit has two parents

The merged content is composed by the app, not by git — but the commit records
both parents, written by setting `MERGE_HEAD` before committing.

**`MERGE_HEAD` must be removed in a `finally`, on success and on every failure
path.** It is a state file git leaves in place; `write_and_commit_files` restores
files and resets only the paths it touched (`gitio.py:216-292`) and knows nothing
about it. A `MERGE_HEAD` left behind after a failed merge turns the **next**
ordinary commit — a section save, a checkpoint — into an unintended merge commit
with a second parent nobody chose.

The graph cannot draw a fork today (§9), so this changes nothing visible. It is
recorded because **ancestry that is not written down cannot be recovered**, while
not drawing it yet is a UI gap fixable at any time.

## 8. Permissions

**Owner only** for create, switch, delete and merge. Members see the branch name
and work on whatever branch is current.

This follows the existing pattern in `resolve_access` (`ws_auth.py:93`), where
the setup phase and a locked spec are already owner-only. A branch switch changes
what every member sees, which is at least as consequential.

## 9. Out of scope

- **Graph lanes.** `CommitInfo` carries `sha`, `authored_at`, `subject` only, and
  `list_commits` formats `%H%x00%aI%x00%s` (`gitio.py:532`) — no parents.
  Teaching it parents and drawing lanes is separate work, and the earlier
  direction on it stands: run the density query before building the lane layer.

  **`list_commits` must pass `--first-parent` as part of this work.** It runs
  `git log` with no revision argument (`gitio.py:525-533`), so it follows HEAD
  and is branch-scoped today — that part is correct. But once §7.6 writes
  two-parent commits, `git log` walks BOTH ancestries and interleaves them by
  date: the moment you merge, the version list silently gains every commit made
  on the other branch, undated-looking and unexplained. `--first-parent` keeps
  the list to the branch's own line, which is what a linear list means. This is
  a one-flag change and it is in scope precisely because §7.6 causes it.
- **Per-person branch views.** §2.
- **Rename detection.** §7.4.
- **Rebase, cherry-pick, force-push, history rewriting.** Recovery is by forward
  commit, per the 2026-08-15 document.

## 10. Acceptance

Ordinary behaviour:

1. Creating a branch from the current HEAD, switching to it, editing, and
   switching back leaves both branches' content intact in git.
2. A switch with unsaved live edits checkpoints them to the branch being left —
   verified by reading that branch's HEAD markdown after the switch.
3. A switch is refused while a drafting turn is running.
4. Merging a branch where one section changed on the source only takes it
   without prompting; a section changed on both sides prompts.
5. A renamed heading appears as one removal and one addition.
6. The merge commit has two parents.
7. A non-owner cannot create, switch, delete or merge.
8. After a merge, the version list shows only the target branch's own line — the
   merged branch's individual commits do not appear in it (§9, `--first-parent`).

Races and failures. **These are the criteria that decide whether the feature is
safe; the ones above can all pass while it is not.** Each names the state to
assert, not merely "it works":

9. **Write between truncate and eviction.** With the room NOT evicted, a client
   update arriving after `truncate_log` must not survive into the target branch.
   Assert by forcing the eviction to be skipped: the test must FAIL, proving it
   is the eviction that prevents this and not luck. Then with the specified
   order, assert the log is empty at commit and the room hydrates from the new
   HEAD.
10. **Reconnect during the switch.** A connect attempt racing the switch must
    block on the commit mutex and then see the finished state — never seed from
    the pre-switch HEAD.
11. **Failure after checkpoint, before truncate.** Assert: log intact, HEAD
    unmoved, generation unchanged, `current_branch` unchanged.
12. **Failure after truncate, before checkout.** Assert the transaction rolled
    back: the log is intact AND no checkpoint commit was left on the branch
    being left. Neither half alone is sufficient — a checkpoint that survived a
    rolled-back truncate duplicates content on the next switch.
13. **Failure after checkout, before DB commit.** Assert the compensating
    checkout restored HEAD, and that `current_branch` still names the source.
14. **Crash between checkout and DB commit**, simulated by committing the git
    step and abandoning the transaction. Assert reconciliation (§4.1) returns
    HEAD to `current_branch` on the next access, and that no content moved.
15. **An edit form opened before a switch is refused after it, with a section
    whose bytes are IDENTICAL on both branches** (§6.1). A form whose section
    differs would be refused by the pre-existing sha check, so it proves
    nothing; the identical-bytes case is the only one that tests the fence.
16. **`build_bibliography` and the review pass runner are refused at a
    superseded generation** (§6.2). Assert by bumping the generation and calling
    each directly.
17. **A failed merge leaves no `MERGE_HEAD`** (§7.6), asserted by making the
    commit fail and then checking that the next ordinary commit has one parent.
18. **Duplicate headings with differing counts are surfaced as conflicts, not
    paired** (§7.3): base has two `## Budget`, the source deletes the first, and
    the merge must not treat the survivor as an edit of the target's first.
