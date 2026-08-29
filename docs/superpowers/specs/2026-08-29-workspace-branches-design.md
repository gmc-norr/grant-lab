# Workspace Branches — Design

**Date:** 2026-08-29
**Status:** Design agreed with the user; not implemented.
**Revision 3.** Two outside review rounds, seventeen findings, none refuted.
Round 2 found two CRITICALs, both of them this document citing real code and
drawing a guarantee from it that the code does not give: `RoomManager.evict`
does not take `room.lock` (§4.3), and `load_or_hydrate` reaches its mutex only
on the cold seed path (§4.4).
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

### 3.1 Creating a branch

`git branch <name> HEAD` in the workspace repo. It does **not** move HEAD, touch
the Yjs log, or evict anyone: creating a branch changes nothing anybody can see.
It is therefore not a switch, needs no mutex for safety, and no checkpoint.

It **does** take the commit mutex at the caller's generation anyway, for one
reason: `HEAD` must not move under it. A checkpoint or section save committing
between the `rev-parse` and the `git branch` would silently found the branch on a
commit the user did not choose. One mutex acquisition on a cold, user-initiated
path.

Name validation, before anything runs:

- Rejected if `git check-ref-format --branch <name>` fails. Do not hand-roll the
  rules — git owns them, and a name that passes a hand-rolled check but fails
  git's produces a half-created branch.
- Rejected if the name already exists (`git rev-parse --verify`), with the
  existing name shown. Never silently reuse.
- Rejected if the repo has **no commits yet** — there is no HEAD to branch from.
  A workspace in setup is in this state.

Creating a branch is the only branch operation that is safe while a drafting turn
is running, because it changes nothing the turn can observe.

### 3.2 Deleting a branch

`git branch -D <name>` — force, not `-d`. `-d` refuses to delete a branch whose
commits are not merged, which is precisely the branch a user most wants to throw
away ("that framing did not work"). The refusal would be unexplainable in the UI.

Two refusals, both absolute:

- **The current branch cannot be deleted.** `workspaces.current_branch` names it;
  git refuses too, but the app must refuse first with a message that says to
  switch away.
- **The last remaining branch cannot be deleted.** A repo with no branch has a
  detached HEAD and §4.1's reconciliation has nothing to reconcile to.

Deletion is destructive and unrecoverable through the UI: `-D` drops the ref, and
nothing else in the app names those commits (`list_commits` follows HEAD;
`ArtifactVersion` rows persist but their shas become unreachable). The UI must
say so before it runs — this is the one branch operation that destroys work.

Deletion takes the commit mutex at the caller's generation for the same reason a
switch does: it must not run interleaved with a switch onto the branch it is
deleting.

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
3. **Tear the rooms down FIRST** — before anything else touches the log, and
   using the `_retire_room` teardown shape, **not** `RoomManager.evict` (§4.3).
   Both artifacts.
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

### 4.3 Why the teardown comes first — and why `evict` cannot do it

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

**`RoomManager.evict` is the wrong primitive, and an earlier revision of this
document said it was the right one.** `evict` (`room.py:656-668`) pops the room
and assigns `room.evicted = True` while holding only the *manager* lock
(`self._lock`). It never takes `room.lock`. A client persist already inside that
lock therefore commits **after** `evict` returns — after the tombstone, and
potentially after the checkpoint and the truncate.

The comment quoted above is `_retire_room`'s, not `evict`'s, and it says exactly
why the distinction matters: the tombstone is flipped *"UNDER `room.lock` — this
is the client-write linearization point, not a cosmetic flag set"*
(`room.py:731-738`). Taking that lock buys two things at once: when you get it,
no client persist is in flight; and every later frame drops at the
`if self.evicted` check (`room.py:197`).

**The switch must use the `_retire_room` teardown** (`room.py:721`): pop under
the manager lock, then tombstone under `room.lock`, then close every socket and
drain any in-flight server op. That is the shape workspace retirement already
uses, and it is the only one in the codebase that establishes the linearization
point this design depends on. It also already does the right thing about
checkpointing — it never checkpoints, and this sequence checkpoints separately at
step 4, from the frozen **log**, not from the room.

Whether that teardown is reused directly or factored out of `retire_workspace`
is an implementation choice; using `evict` is not. `evict` remains correct for
what it is documented for — poison recovery, where a lost keystroke is the point.

### 4.4 A reconnect CAN race the switch today — `load_or_hydrate` must change

An earlier revision of this document claimed `load_or_hydrate` "takes the same
commit mutex before seeding, so it blocks until the switch's transaction ends."
**That guarantee does not exist.** The mutex is acquired at `hydrate.py:62-67`,
but the function returns at `hydrate.py:30-32`:

```python
updates = store.load_updates(session, workspace_id=..., artifact=...)
if updates:
    return updates
```

The mutex is on the **cold seed path only**. A warm load — the log has rows —
never reaches it.

That is the whole race. Between step 3 (teardown) and step 5 (truncate) the log
still holds the *old* branch's updates. A client reconnecting in that window
loads those rows without touching the mutex, and `RoomManager` publishes a fresh
room from them — a room step 3 has already torn down and will not tear down
again. The switch then truncates underneath it. The workspace is left serving the
old branch's text under the new branch's name, and the next checkpoint commits it
there: the same corruption §4.3 exists to prevent, arriving through the other
door.

**Requirement: `load_or_hydrate` must acquire the commit mutex at the caller's
stamped generation BEFORE the `load_updates` read**, not only before the seed
write. Then a mid-switch connect blocks until the switch's transaction commits,
re-reads an empty log, and seeds from the new HEAD — which is what the previous
revision incorrectly assumed was already true.

Two consequences to accept deliberately:

- **Cost.** Every room open now pays one per-workspace mutex acquisition, not
  just the first open ever. Room open is a connect-time path, not a per-keystroke
  one, so this is bounded — but it is a real change to a warm path and must be
  stated, not discovered.
- **A new failure mode on a warm path.** `CommitMutexTimeout` can now fire on an
  ordinary reconnect, where previously only a cold seed could hit it. The
  handling already exists and is documented in that function's own comment
  (`hydrate.py:52-61`): HTTP maps it to 429, and `collab_ws` maps it to a close
  frame rather than a denial the client cannot see. What changes is how often it
  can happen, not what happens.

**The implementation must confirm no lock inversion:** nothing may hold
`room.lock` while calling `load_or_hydrate`, or a hydrating connect would wait on
the commit mutex while the switch waits on `room.lock`. Acceptance criterion 10
asserts the blocking behaviour; criterion 10a asserts the inversion is absent.

### 4.5 Failure at each step

| Fails at | Result | Recovery |
| --- | --- | --- |
| 1-3 | Nothing changed | None needed |
| 4 (checkpoint) | Transaction rolls back; log intact | None needed. The switch did not happen |
| 5 (truncate) | The **database** rolls back with its checkpoint — the `handover.py:60-62` rule: a truncate is never visible without its checkpoint. The checkpoint's **git commit survives**, and that is correct — see below | None needed |
| 8 (checkout) | Rolls back; `current_branch` reverts | None needed |
| 9 (commit) | HEAD may be moved but the DB reverted | Compensating checkout; and §4.1 reconciliation repairs it even after a crash |
| Crash between 8 and 9 | HEAD on the target, DB on the source | §4.1: the DB wins, HEAD is checked out again on next access |

**A rolled-back switch can leave a real commit behind, and must.** Step 4 calls
`checkpoint_artifact`, which calls `write_and_commit_files`
(`checkpoint.py:144-146`), which runs an actual `git commit` (`gitio.py:197`).
Git is not enlisted in the database transaction; **no rollback can remove that
commit.**

This is harmless, and demanding its absence would be demanding the deletion of
valid work. The commit contains the source branch's own live content, written to
the source branch — exactly where it belongs, and exactly what step 4 exists to
preserve. If the switch is retried, the log is still intact, the checkpoint
recomputes the same bytes, and `write_and_commit_files` short-circuits on an
empty diff (`gitio.py:177-195`) and commits nothing the second time.

An earlier revision asserted the opposite in acceptance criterion 12 — that a
rolled-back truncate must leave no checkpoint commit — on the reasoning that a
surviving checkpoint "duplicates content on the next switch". **That reasoning was
wrong** (the empty-diff short circuit is what prevents the duplication) and the
criterion was unachievable. Criterion 12 now asserts the survivor is present and
benign.

## 5. Connected editors

Switching **tears down** both artifact rooms and closes every client socket, as
**step 3** — before the log is touched. §4.3 explains why that position is a
correctness requirement rather than a preference, and why the teardown must be
`_retire_room`'s (`room.py:721`) rather than `RoomManager.evict`'s: only the
former flips the `evicted` tombstone under `room.lock`, so an in-flight client
persist is drained instead of landing after the switch (`room.py:197`,
`:731-738`).

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

### 7.5 The citation ledger merges with the document, in the same commit

**Merging `proposal.md` alone corrupts every citation the merge brings across.**

`citations.json` is a separate file. The whole citation subsystem is built on the
two moving together: `gitio.py`'s own module docstring says the atomic commit
exists "so `proposal.md` and `citations.json` always move together", and both
writers honour it in one `write_and_commit_files` call —
`human_cite.py:239-243` and `citations/core.py:629-634`. Export resolves the
markers in the prose against the on-disk ledger (`export.py:72-97`), and
`build_bibliography` renders only what the prose actually cites.

So a merge that takes a cited section from the source branch, while committing
the target branch's ledger, produces prose whose markers have no evidence rows:
the export either drops them or raises. The bug appears at export time, in front
of the funder-facing document, long after the merge looked successful.

**The ledger is merged as a set of rows keyed by `ev_id`**, not by section.
`ev_id` is `ev-<12 hex of uuid4>` (`ledger.py:50`) — assigned once at creation
and never recomputed — so three-way set semantics against the base are well
defined and need no heading matching at all:

| In base | In target | In source | Behaviour |
| --- | --- | --- | --- |
| no | no | yes | Added on the source. Take it. |
| no | yes | no | Added on the target. Keep it. |
| yes | yes | yes, identical | Unchanged. Keep it. |
| yes | yes | yes, **differing bodies** | Conflict. Surface with the section that owns it (`section_path`). |
| yes | no | yes | Deleted on the target. Drop it. |
| yes | yes | no | Deleted on the source. Keep it. |

Rows are keyed independently of the section decisions on purpose. A row taken in
whose section was **not** taken becomes an orphan — evidence for a claim not in
the document — and that is safe: `build_bibliography` parses the prose for
markers, so an unreferenced row renders nothing. The reverse (a section taken
without its rows) is the unsafe direction, and set-union on `ev_id` cannot
produce it.

**The invariant is checkable, not aspirational.** The marker in the prose carries
its own `ev_id` — `insert_marker` writes it (`markers.py:83`) and `find_markers`
reads it back (`markers.py:45`) — so the merge has a postcondition it can assert
directly before committing:

> every `ev_id` returned by `find_markers(merged_proposal)` has a row in the
> merged ledger.

A merge that cannot satisfy it must fail **before** the commit, not produce a
document that breaks at export. This is the assertion acceptance criterion 23
makes, and it is why the rule is set-union rather than anything cleverer: a
cleverer rule would need this check too, and could still fail it.

**The merged `proposal.md` and the merged `citations.json` must be written in one
`write_and_commit_files` call**, preserving the invariant the rest of the
subsystem depends on. A merge that commits them separately is a defect even if
both commits succeed.

`references.citekey` is a database cache of this ledger and can drift; the repair
path already exists (`reconcile.py:183`) and should run after a merge.

### 7.6 Order of the merged document

The **target** branch's order — the branch being merged into.

A section arriving from the source is placed after its **source-branch
predecessor**, identified by `(heading, occurrence number)` — the same key §7.3
matches on, not the bare heading. Identifying the predecessor by heading alone is
ambiguous exactly where §7.3 permits duplicates: "after `## Budget`" does not say
which `## Budget`.

Three rules settle the remaining cases:

- **Several sections arriving after the same predecessor keep their source order**
  relative to each other.
- **If the predecessor is not present in the merged document** — absent from the
  target, or deleted by the merge — the arriving section falls back to the next
  earlier source predecessor that *is* present, and goes last if none is.
- **If the predecessor is itself conflicted** and the user has not yet resolved
  it, placement is deferred until they do. Placement is never computed against a
  section whose presence is undecided.

### 7.7 The merge commit has two parents

The merged content is composed by the app, not by git — but the commit records
both parents, written by setting `MERGE_HEAD` before committing.

**`write_and_commit_files` cannot write this commit.** It short-circuits when the
staged tree is unchanged: `git diff --cached --quiet` returning 0 makes it return
the existing HEAD **without committing at all** (`gitio.py:177-195`). A merge
resolved entirely to target content — every section kept, which is a perfectly
ordinary outcome — stages nothing. The result is silent and bad: no merge commit,
no second parent, the branches never actually joined, and the next merge redoing
all the same work because the ancestry says nothing happened.

The merge therefore needs its **own commit path** in `gitio`, not a flag threaded
through the general one. It sets `MERGE_HEAD`, stages, and commits with
`--allow-empty` so an unchanged tree still produces a commit. The empty-diff
short circuit stays exactly as it is for every other caller, where skipping a
no-op commit is the correct behaviour.

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
  and is branch-scoped today — that part is correct. But once §7.7 writes
  two-parent commits, `git log` walks BOTH ancestries and interleaves them by
  date: the moment you merge, the version list silently gains every commit made
  on the other branch, undated-looking and unexplained. `--first-parent` keeps
  the list to the branch's own line, which is what a linear list means. This is
  a one-flag change and it is in scope precisely because §7.7 causes it.
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

9. **Write between truncate and teardown.** With the rooms NOT torn down, a
   client update arriving after `truncate_log` must not survive into the target
   branch. Assert by forcing the teardown to be skipped: the test must FAIL,
   proving it is the teardown that prevents this and not luck. Then with the
   specified order, assert the log is empty at commit and the room hydrates from
   the new HEAD.
10. **Reconnect during the switch.** A connect attempt racing the switch must
    block on the commit mutex and then see the finished state — never seed from
    the pre-switch HEAD.
11. **Failure after checkpoint, before truncate.** Assert: log intact, HEAD
    unmoved, generation unchanged, `current_branch` unchanged.
12. **Failure after truncate, before checkout.** Assert the DATABASE rolled
    back — the log is intact, `current_branch` unchanged — and that step 4's
    checkpoint commit **is still on the branch being left**, because git is not
    in the transaction (§4.5). Then assert it is benign: retry the switch and
    confirm the second checkpoint commits nothing, because the tree is unchanged
    (`gitio.py:177-195`). Asserting the commit's ABSENCE is the wrong test and an
    earlier revision of this document specified it.
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
17. **A failed merge leaves no `MERGE_HEAD`** (§7.7), asserted by making the
    commit fail and then checking that the next ordinary commit has one parent.
18. **Duplicate headings with differing counts are surfaced as conflicts, not
    paired** (§7.3): base has two `## Budget`, the source deletes the first, and
    the merge must not treat the survivor as an edit of the target's first.
19. **A reconnect between teardown and truncate does not republish the old
    branch's room** (§4.4). Connect while the switch transaction is open, between
    steps 3 and 5, and assert the connect BLOCKS rather than returning rows.
    **Assert by reverting `load_or_hydrate` to acquire the mutex after the
    `load_updates` read: the test must FAIL**, proving it is the moved
    acquisition that prevents this and not timing. Then, with the fix, assert the
    room that finally opens holds the TARGET branch's text.
20. **A client persist in flight at teardown does not survive it** (§4.3). Hold
    `room.lock` in a persist, start the switch, and assert the teardown waits.
    **Assert by substituting `RoomManager.evict` for the `_retire_room` teardown:
    the test must FAIL**, proving the room-lock tombstone is the mechanism.
    Neither of these two criteria is met by a green suite: each is only evidence
    if its named substitution reddens it.
21. **`load_or_hydrate` does not invert the lock order** (§4.4). Assert no call
    path reaches it while `room.lock` is held.
22. **A merge resolved entirely to target content still records a two-parent
    commit** (§7.7). Merge a branch whose every section the user keeps, and
    assert the new HEAD has two parents — not that the merge "succeeded".
    `write_and_commit_files` would report success and write nothing.
23. **A merged cited section carries its evidence rows** (§7.5). Cite a source in
    a section on the source branch only, merge, and assert every `ev_id` in
    `find_markers(merged_proposal)` has a row in the merged ledger, and that
    `get_composed_export` resolves the marker rather than raising. Assert also
    that `proposal.md` and `citations.json` landed in ONE commit — the same sha
    for both paths. **Assert by merging the sections while committing the
    target's ledger unchanged: the test must FAIL**, proving it is the ledger
    merge that holds this and not the fixture.
24. **Branch names are validated by git, not by hand** (§3.1): a name git rejects
    (`a..b`, a trailing `.lock`, a leading `-`) is refused before anything runs,
    and leaves no partially created ref.
25. **The current branch and the last branch cannot be deleted** (§3.2), each
    refused with its own message, and the repo left with its branch intact.
26. **Creating a branch does not move HEAD or disturb the room** (§3.1): assert
    HEAD, the Yjs log and every connected socket are unchanged afterwards.
