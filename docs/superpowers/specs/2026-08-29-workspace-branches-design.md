# Workspace Branches — Design (create, switch, delete)

**Date:** 2026-08-29
**Status:** Design agreed with the user; not implemented.
**Revision 4.** Three outside review rounds, 25 findings, none refuted.

**Merge was split out** into `2026-08-29-workspace-branch-merge-design.md` after
round 3. The findings did not land evenly — switch 8, merge 13 — and the switch
findings narrowed each round while merge's did not: round 3's merge CRITICAL was
that the design never mentioned the live document at all. Merge silently depends
on three subsystems of its own (the editor handover, the citation ledger, section
identity through conflict resolution). It is parked at design; this document is
not, and branches are useful without it.

The failure mode this document has been most prone to, across all three rounds:
**a correct code citation carrying an incorrect guarantee** — a real function at
a real line, claimed to protect something it does not protect on every path in.
**Supersedes, in scope:** the branch-lifecycle half of
`2026-08-15-branches-merging-history-graph-design.md`.
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

Merging two branches back together is designed separately, in
`2026-08-29-workspace-branch-merge-design.md`, and is **not** part of this work.
A branch that is never merged is still the feature: hold two framings, compare
them, and continue on the one you keep. Switching away from a branch loses
nothing — its commits stay on it.

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
3. **Tear both artifact rooms down FIRST** — before anything else touches the
   log — with a **switch-specific teardown**: `_retire_room`'s structure, but its
   proceed-on-lock-timeout fallback replaced by an abort. Not `RoomManager.evict`,
   and not `_retire_room` unmodified. §4.3 explains why each of those fails.
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

**But `_retire_room` cannot be reused as-is, and an earlier revision said it
could.** It waits only `_RETIRE_LOCK_WAIT_SECONDS` for `room.lock`, and on
timeout it sets `room.evicted = True` **unsynchronized and proceeds**
(`room.py:744-753`). Its own comment names the consequence: *"the straggler's own
commit remains the documented residual."*

That residual is acceptable for **retirement** — the workspace is dead, the
straggler's commit lands in a log nobody will read, and B4 refuses it under the
mutex anyway. It is **not** acceptable for a switch: that same straggler commit
lands in a live log which step 4 then checkpoints and step 5 truncates, so it
rides into the target branch. This is the exact corruption the teardown exists to
prevent, surviving on the timeout path.

**The switch needs its own teardown: the same shape, but the lock wait is not
optional.** On timeout it must **abort the switch** — raise, roll the transaction
back, leave the workspace on its current branch — not proceed. A switch that
cannot prove the room is quiet has not earned the right to move the branch, and
refusing is free: the user retries.

So: `_retire_room`'s **structure** is right (pop under the manager lock, tombstone
under `room.lock` via `_set_evicted_under_lock`, close under `recon_lock` to avoid
the inversion its comment describes, drain `room.ops`). Its **timeout policy** is
wrong here. Using `evict` is wrong outright; `evict` remains correct for what it
is documented for — poison recovery, where a lost keystroke is the point.

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

**The blocked connect does not then hydrate — it is closed, and that is
correct.** Step 6 bumps `lifecycle_generation`. A connect stamped before the
switch therefore wakes from the mutex holding the OLD generation, and
`acquire_commit_mutex_at_generation` raises `WorkspaceGenerationStale`
(`fencing.py:168-171`) rather than returning. It never reaches `load_updates`.

This is the fence working, not a defect — that attempt was authorized against a
workspace state that no longer exists. What matters is the **next** step: the
client is closed (`collab_ws` maps the stale exception to a close frame, the same
path as a timeout), it reconnects on its own, and that fresh connect is stamped at
the new generation, blocks on nothing, finds an empty log, and hydrates from the
target branch's HEAD. **The user sees a reconnect, not an error.**

The design requirement is therefore about the client, not the server: the
reconnect must be automatic and must not surface as a failure. It already is —
§5's `synced` gate shows "Loading the draft…" rather than a blank page during it.
An earlier revision claimed the blocked connect itself "re-reads an empty log and
seeds from the new HEAD"; it does not, and criteria 10 and 19 assert the real
two-step behaviour.

**The implementation must confirm no lock inversion:** nothing may hold
`room.lock` while calling `load_or_hydrate`, or a hydrating connect would wait on
the commit mutex while the switch waits on `room.lock`.

### 4.5 Failure at each step

| Fails at | Result | Recovery |
| --- | --- | --- |
| 1-2 | Nothing changed | None needed |
| 3 (teardown) | **Editors are already evicted.** The rooms are popped, tombstoned and every socket closed before any failure below can occur, and none of that is in the transaction | None needed for data. Clients reconnect on their own — to the **unchanged source branch**. See below |
| 4 (checkpoint) | Transaction rolls back; log intact. Editors stay evicted | None needed for data. The switch did not happen; clients reconnect to the source branch |
| 5 (truncate) | The **database** rolls back with its checkpoint — the `handover.py:60-62` rule: a truncate is never visible without its checkpoint. The checkpoint's **git commit survives**, and that is correct — see below | None needed |
| 8 (checkout) | Rolls back; `current_branch` reverts | None needed |
| 9 (commit) | HEAD may be moved but the DB reverted | Compensating checkout; and §4.1 reconciliation repairs it even after a crash |
| Crash between 8 and 9 | HEAD on the target, DB on the source | §4.1: the DB wins, HEAD is checked out again on next access |

**Step 3 is not transactional, and a rolled-back switch still evicts everyone.**
Popping a room, flipping its tombstone and closing sockets are in-memory and
network effects; no database rollback reverses them. So a switch that fails at
step 4 or later leaves every editor disconnected even though the branch did not
change. This is a **visible interruption with no data cost** — clients reconnect
automatically, hydrate from the unchanged source HEAD, and see the same document
they had. It must be stated because it is what a user will report ("it kicked me
out and nothing happened"), and because a design that assumed steps 1-3 were
reversible would be wrong about it.

A **partial** teardown — one artifact room torn down, the other failing — leaves
the two rooms inconsistent. The switch must abort in that case rather than
proceed on one artifact, and the surviving room is evicted on the retry.

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

## 7. Permissions

**Owner only** for create, switch and delete. (Merge too, when it is built — see
the merge design.) Members see the branch name and work on whatever branch is
current.

This follows the existing pattern in `resolve_access` (`ws_auth.py:93`), where
the setup phase and a locked spec are already owner-only. A branch switch changes
what every member sees, which is at least as consequential.

## 8. Out of scope

- **Merging two branches.** Its own document,
  `2026-08-29-workspace-branch-merge-design.md`, parked at design with six
  unresolved blockers. Nothing here depends on it.
- **Graph lanes.** `CommitInfo` carries `sha`, `authored_at`, `subject` only, and
  `list_commits` formats `%H%x00%aI%x00%s` (`gitio.py:532`) — no parents.
  Teaching it parents and drawing lanes is separate work, and the earlier
  direction on it stands: run the density query before building the lane layer.

  **`list_commits` needs no change for this work.** It runs `git log` with no
  revision argument (`gitio.py:525-533`), so it follows HEAD: the version list is
  already branch-scoped, and switching branches changes it correctly and for
  free. It needs `--first-parent` only once something writes a two-parent commit,
  which is the merge design's problem and is recorded there.
- **Per-person branch views.** §2.
- **Rebase, cherry-pick, force-push, history rewriting.** Recovery is by forward
  commit, per the 2026-08-15 document.

## 9. Acceptance

Ordinary behaviour:

1. Creating a branch from the current HEAD, switching to it, editing, and
   switching back leaves both branches' content intact in git.
2. A switch with unsaved live edits checkpoints them to the branch being left —
   verified by reading that branch's HEAD markdown after the switch.
3. A switch is refused while a drafting turn is running.
4. A non-owner cannot create, switch or delete a branch.
5. Branch names are validated by git, not by hand (§3.1): a name git rejects
   (`a..b`, a trailing `.lock`, a leading `-`) is refused before anything runs,
   and leaves no partially created ref.
6. The current branch and the last remaining branch cannot be deleted (§3.2),
   each refused with its own message, and the repo left with its branch intact.
7. Creating a branch does not move HEAD or disturb the room (§3.1): assert HEAD,
   the Yjs log and every connected socket are unchanged afterwards.

Races and failures. **These are the criteria that decide whether the feature is
safe; the ones above can all pass while it is not.** Each names the state to
assert, not merely "it works":

8. **Write between truncate and teardown.** With the rooms NOT torn down, a
   client update arriving after `truncate_log` must not survive into the target
   branch. Assert by forcing the teardown to be skipped: the test must FAIL,
   proving it is the teardown that prevents this and not luck. Then with the
   specified order, assert the log is empty at commit and the room hydrates from
   the new HEAD.
9. **Reconnect during the switch, in two steps** (§4.4). A connect stamped
   before the switch must (a) BLOCK on the commit mutex rather than reading the
   log, and (b) on waking, be refused with `WorkspaceGenerationStale` and closed
   — never seed from the pre-switch HEAD. Then assert the client's NEXT connect,
   stamped fresh, hydrates the target branch. Asserting only that "the client
   ends up on the target branch" would pass even if step (a) never happened.
10. **Failure after checkpoint, before truncate.** Assert: log intact, HEAD
    unmoved, generation unchanged, `current_branch` unchanged.
11. **Failure after truncate, before checkout.** Assert the DATABASE rolled
    back — the log is intact, `current_branch` unchanged — and that step 4's
    checkpoint commit **is still on the branch being left**, because git is not
    in the transaction (§4.5). Then assert it is benign: retry the switch and
    confirm the second checkpoint commits nothing, because the tree is unchanged
    (`gitio.py:177-195`). Asserting the commit's ABSENCE is the wrong test and an
    earlier revision of this document specified it.
12. **Failure after checkout, before DB commit.** Assert the compensating
    checkout restored HEAD, and that `current_branch` still names the source.
13. **Crash between checkout and DB commit**, simulated by committing the git
    step and abandoning the transaction. Assert reconciliation (§4.1) returns
    HEAD to `current_branch` on the next access, and that no content moved.
14. **An edit form opened before a switch is refused after it, with a section
    whose bytes are IDENTICAL on both branches** (§6.1). A form whose section
    differs would be refused by the pre-existing sha check, so it proves
    nothing; the identical-bytes case is the only one that tests the fence.
15. **`build_bibliography` and the review pass runner are refused at a
    superseded generation** (§6.2). Assert by bumping the generation and calling
    each directly.
16. **A reconnect between teardown and truncate does not republish the old
    branch's room** (§4.4). Connect while the switch transaction is open, between
    steps 3 and 5, and assert the connect BLOCKS rather than returning rows.
    **Assert by reverting `load_or_hydrate` to acquire the mutex after the
    `load_updates` read: the test must FAIL**, proving it is the moved
    acquisition that prevents this and not timing. Then, with the fix, assert the
    room that finally opens holds the TARGET branch's text.
17. **A client persist in flight at teardown does not survive it** (§4.3). Hold
    `room.lock` in a persist, start the switch, and assert the teardown waits.
    **Assert by substituting `RoomManager.evict` for the switch teardown: the
    test must FAIL**, proving the room-lock tombstone is the mechanism.
    Neither this nor criterion 16 is met by a green suite: each is only evidence
    if its named substitution reddens it.
18. **A teardown that cannot get `room.lock` ABORTS the switch** (§4.3). Hold
    `room.lock` past the wait budget and assert the switch raises, the
    transaction rolls back, `current_branch` is unchanged and HEAD has not moved.
    **Assert by restoring `_retire_room`'s proceed-on-timeout fallback: the test
    must FAIL.** A switch that proceeds here is the one that carries a straggler
    commit into the target branch.
19. **`load_or_hydrate` does not invert the lock order** (§4.4). Assert no call
    path reaches it while `room.lock` is held.
20. **A switch that fails after teardown still leaves clients evicted, and they
    recover** (§4.5). Fail step 4, then assert every socket was closed, the
    branch did not change, and a reconnect serves the SOURCE branch's text.
21. **A partial teardown aborts rather than switching one artifact** (§4.5).
    Fail the `spec` room's teardown, and assert the `draft` room's branch did not
    move.
