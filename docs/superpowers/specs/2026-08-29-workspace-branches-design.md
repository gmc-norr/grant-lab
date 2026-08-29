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

The live truth is the Yjs log; git holds checkpoints. A switch must therefore
move both. The sequence is:

1. **Checkpoint** the room to the branch being left, via `checkpoint_artifact`
   (`checkpoint.py:63`), which takes the per-workspace commit mutex at the
   caller's stamped generation.
2. **Truncate** the log (`store.truncate_log`) and **reset the watermark**
   (`state.reset_checkpoint`).
3. **Move HEAD** to the target branch.
4. **Evict** the room so connected clients reconnect.
5. `load_or_hydrate` (`hydrate.py:25`) then re-seeds an empty store from the new
   HEAD's markdown on the next connect — its existing `if updates: return
   updates` guard means it seeds only because step 2 emptied the log.

Steps 1, 2 and 5 are not new. `handover.py` already performs exactly that
drain-truncate-reset for the kill switch, in **one transaction**, on the stated
principle that *a truncate can never be visible without its checkpoint*
(`handover.py:60-62`). The branch switch reuses that ordering; only step 3 (the
git ref move) and step 4 (eviction) are new.

**The one-transaction rule is a requirement, not an implementation note.** A
truncate that commits without its checkpoint destroys unsaved work.

## 5. Connected editors

Switching **evicts** every client in the room. `RoomManager.evict`
(`room.py:656`) sets the `evicted` tombstone under the room lock, so any
in-flight operation rechecks and aborts rather than persisting into the wrong
branch (`room.py:197`).

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
database and never read-modify-writes from Python.

Every write path already reaches the commit mutex through
`acquire_commit_mutex_at_generation` (`fencing.py:115`), which refuses when the
workspace has moved past the caller's stamped generation. One bump therefore
invalidates every in-flight write at once — including paths neither the spec
author nor the implementer enumerated.

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

### 7.2 Matching

Sections are matched by **heading**, not by index. Index is positional and shifts
whenever a section is added or removed; heading survives reordering.

Where a heading appears more than once, sections are matched by
**(heading, occurrence number)**: the second `## Budget` pairs with the second
`## Budget`. Nothing in the app prevents duplicate headings, though neither real
workspace document has any (6/6 and 4/4 unique, measured 2026-08-29).

### 7.3 The three outcomes

For each matched pair, compare `Section.sha`:

| Case | Behaviour |
| --- | --- |
| Changed on one side only | Take it. No prompt. |
| Changed on both sides | The user chooses: theirs, mine, or keep both. |
| Present on one side only | The user chooses: take the addition, or accept the removal. |

### 7.4 Renames are shown as a delete plus an add

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
  `list_commits` formats `%H%x00%aI%x00%s` (`gitio.py:532`) — no parents. The
  version list stays a linear list of the current branch's history. Teaching it
  parents and drawing lanes is separate work, and the earlier direction on it
  stands: run the density query before building the lane layer.
- **Per-person branch views.** §2.
- **Rename detection.** §7.4.
- **Rebase, cherry-pick, force-push, history rewriting.** Recovery is by forward
  commit, per the 2026-08-15 document.

## 10. Acceptance

1. Creating a branch from the current HEAD, switching to it, editing, and
   switching back leaves both branches' content intact in git.
2. A switch with unsaved live edits checkpoints them to the branch being left —
   verified by reading that branch's HEAD markdown after the switch.
3. A truncate is never visible without its checkpoint, including when the git
   step fails.
4. An edit form opened before a switch is refused after it, **even when the
   section's bytes are identical on both branches** (§6.1).
5. A switch is refused while a drafting turn is running.
6. Merging a branch where one section changed on one side only takes it without
   prompting; a section changed on both sides prompts.
7. A renamed heading appears as one removal and one addition.
8. The merge commit has two parents.
9. A non-owner cannot create, switch, delete or merge.
