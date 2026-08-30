# Per-branch worktrees and live rooms

**Status:** design, unbuilt. Supersedes the branch model shipped in `880ea243`.

## 1. What this is for

In the user's words:

> Just like for code, we should be able to make a branch. The new branch `new_ideas`
> contains all pre-existing work and information, but new work can be added that takes
> it into a new direction. At the same time the original branch continues to exist.
> Work can be added by multiple people to both branches. Both branches can be active at
> the same time. A user can only work on one branch at a time, but different users can
> work on different branches simultaneously. Different users can also work at the same
> time on one branch, collaboratively.

That last sentence is what makes this different from git. A branch here is not a folder
on one person's laptop — it is a **live shared editing session**.

### The model

```
workspace
├── branch main        → worktree + live room   ← Anna and Bo, typing together
├── branch new_ideas   → worktree + live room   ← Pär, alone
└── branch eu_version  → worktree + live room   ← nobody right now
```

- **One branch = one worktree = one live room** (per artifact).
- **Branch selection is per USER.** Switching moves only the person who switched.
- A new branch **starts as a full copy** of the branch it forked from.
- All branches are **active simultaneously**; none is paused.

### What today does instead

`workspaces.current_branch` is a single column on the workspace row
(`db/models.py:704`). Rooms are keyed `(workspace_id, artifact)` —
`RoomKey = tuple[uuid.UUID, str]` (`collab/room.py:32`), and `branches.py:44` states
the consequence outright: *"branches are a workspace property, which is why a switch has
to move the whole workspace at once."*

So a switch today tears down every room, checkpoints, truncates the live log, bumps the
lifecycle generation and checks out — **moving every user in the workspace.** The UI is
honest about it: the confirm dialog reads *"Switching to `<name>` will disconnect every
open editor. Unsaved keystrokes will be lost."*
(`frontend/src/cockpit/panes/BranchPicker.tsx:123-136`).

## 2. Why git worktrees, and proof they suffice

One checked-out folder can hold one branch. Two live branches need two folders. Git's own
answer is `git worktree` — one repository, several working directories, one object store.

**There is no worktree code in this repo today.** `grep -rn worktree --include=*.py src/`
returns exactly one hit, and it is a comment (`branches.py:351`).

The design rests on six properties of `git worktree`. All six were **measured**, not
assumed, in a scratch repo:

| # | Claim | Result |
|---|---|---|
| 1 | A worktree can be created for a second branch | created OK |
| 2 | Both worktrees edit and commit independently | different content in each, both committed |
| 3 | They share one object store | `git log --all` from either shows both branches |
| 4 | Git refuses the same branch in two worktrees | `fatal: 'main' is already used by worktree at …` |
| 5 | **Concurrent commits to two branches** | 40 commits, two parallel loops, **zero failures** |
| 6 | Removing a worktree loses nothing | branch, commits and content all intact |

(4) matters: the one-worktree-per-branch invariant is **enforced by git**, not by us.

(5) is the load-bearing one — the whole design assumes two branches can commit at the
same instant. Each worktree has its own index, so there is no contention.

(6) means the worktree is a **derived cache**. The durable things are the commits and the
database rows. This is a robustness property for backup and restore; it is **not** a
licence to reap folders — measured, a workspace's tracked files are about 4 KB, so ten
branches cost roughly 40 KB. Ten branches get ten permanent folders.

### Layout

```
<workspaces_dir>/<workspace_id>/
  repo/                    # the repository; also the worktree for the root branch
  worktrees/<branch>/      # one per additional branch
```

## 3. What is branch-scoped and what is shared

26 tables carry `workspace_id`. The rule:

**Branch-scoped** — the document and everything that describes *this* document:

| Data | Why |
|---|---|
| Draft and spec text | It is in git; it branches for free |
| Live log + checkpoint watermark (`yjs_updates`, `collab_artifact_state`) | This is the point of the design |
| Version list (`artifact_versions`) | These are the branch's own commits |
| All AI analysis — evaluator, review, coherence, uncited claims, claim analysis | A score for `main`'s text is meaningless on `new_ideas` |
| Comments / annotations | They are anchored to text that now differs |
| Pending proposed changes | Same reason |
| The AI chat (`chat_turns`, `chat_summaries`) | See §3.1 |

**Shared** — properties of the workspace, not of a document:

| Data | Why |
|---|---|
| Reference corpus (`references`, `reference_chunks`, `attachments`) | You must not re-upload a paper because you made a branch |
| Access control (`workspace_viewers`, owner) | Access is to the workspace |
| Spend budget (`llm_spend`) | It is money; one pot |
| Setup, approvals, event log | Workspace-level admin |

Citations fall out correctly for free: the markers live in the artifact text and the
ledger is `citations.json`, a file in git. **Which papers you have cited branches
automatically, while the papers themselves stay shared.**

### 3.1 The chat is a document, not a special case

Decided. In the user's words:

> The branches should have their own chats. Whatever existed before the branch was
> created follows into the branch. Just like any other document.

So the chat obeys exactly the rule the draft obeys: **everything up to the fork point
comes with you; after that the two conversations diverge and never meet.** No special
casing, no "shared history with a cutoff" — the branch simply has its own chat, whose
opening content is a copy.

`chat_turns` is `(workspace_id, role, content, position, created_at)` with no reference
to artifact or commit state (`models.py:843-865`), so the copy is trivial. There are 145
chat turns in the entire instance, so a fork-time copy costs nothing and beats a
pointer-plus-cutoff scheme on simplicity. `ix_chat_turns_ws_position` means `position` is
a per-workspace sequence and becomes per-`(workspace, branch)`. `chat_summaries`
(compaction output) is derived from the chat and forks with it.

**The one thing that is NOT a document: `workspaces.last_sdk_session_id`**
(`models.py:760`). It is a resume handle for a conversation living on the hosted side —
`turns/processor.py:389` passes `resume=<id>` when it is non-NULL. Two branches cannot
resume one remote session; doing so would interleave two branches' turns into a single
conversation upstream.

**Rule: a fork copies the chat rows and sets `last_sdk_session_id` to NULL on the new
branch.** The next turn there starts a fresh hosted session and re-injects the copied
history as context.

This needs no new machinery. Compaction already does exactly this reset for its own
reasons — *"clear `workspaces.last_sdk_session_id` … clearing it forces"* a new session
with re-injection (`turns/compaction.py:138,258`). The fork reuses that proven path.
Because `last_sdk_session_id` lives on the workspace row, it must also move to
per-`(workspace, branch)` state along with branch selection (§5).

## 4. How a branch ends

Three endings, and only one of them is work:

| Ending | Cost |
|---|---|
| **Promote** — `new_ideas` becomes the new main | Near zero: a ref move and a worktree swap |
| **Bin** — discard `new_ideas` | Near zero: remove the worktree, delete the ref, cascade its rows |
| **Copy sections across** | **The real feature** |

**There is no automatic merge, and none is wanted.** The request was to *"move the new
ideas and concepts that we built"* — a person deciding what to carry over, not a machine
reconciling two edits to one paragraph.

That is fortunate, because automatic merge is not available here anyway:
`apply_pm_section_to_fragment` is delete-then-reinsert, so two branches editing one
section converge on a **duplicated section**, silently, with both sides agreeing.

**v1 granularity: whole sections.** Sections are already first-class objects, and it
matches how people talk about a proposal ("our Methods is better on the new branch").

**Later, explicitly out of scope for v1:** AI-assisted transfer of a *concept* — "bring
the new framing of the aims into main" — where the assistant weaves it into the target's
existing prose rather than replacing a section.

## 5. What has to change

Counts below were measured, not estimated.

### Hard

- **`workspace_repo_dir` becomes branch-aware.** It hardcodes
  `<workspaces_dir>/<workspace_id>/repo` (`paths.py:9-12`). **59 call sites across 33
  files** must each decide which branch's tree they mean. This is the largest surface in
  the migration.
- **`RoomKey` becomes a 3-tuple** and the branch threads through every manager method
  and the WebSocket route `/api/ws/workspaces/{workspace_id}/{artifact}`, which has no
  branch segment (`web/collab_ws.py:61,114`). Three sites read the key **positionally**
  (`key[0]`), so they break loudly rather than silently — which is the good case.
- **The live log gains a branch dimension.** `yjs_updates` and `collab_artifact_state`
  are keyed `(workspace_id, artifact)` throughout. Two branches editing at once would
  otherwise interleave into one log. Needs a migration plus a backfill onto the current
  branch.
- **`create_workspace_branch` becomes a fork.** Today it is one command,
  `git branch <name> HEAD`, and a test explicitly pins that it copies nothing. It must
  now add a worktree and copy the branch-scoped rows — including the chat (§3.1), whose
  copy must also NULL the new branch's `last_sdk_session_id`.

### Moderate

- Branch selection moves from one column on `workspaces` to per-`(workspace, user)`
  state. It **cannot** be a column on `workspace_viewers` — the owner has no viewers row.
- Delete must `git worktree remove` before `git branch -D` (git refuses to delete a
  branch a worktree holds) and must cascade the branch-scoped rows.
- Frontend query keys `["draft", id]`, `["spec", id]`, `["versions", id]` must carry the
  branch. Without it one cache serves one branch's document under another branch's name,
  with no error and no request.
- The picker moves. `BranchPicker` renders only under `?view=versions`
  (`CockpitSidebar.tsx:24-29`) — defensible for a rare owner-only operation, wrong for an
  always-on personal mode switch. The `StatusBar` shows the branch as dead text today.

### Deleted outright

The entire switch sequence (`branches.py:128-243`) and its 9 tests. Every step of it —
the teardown, the checkpoint-to-the-branch-being-left, the log truncate, the watermark
reset, the generation bump — exists **only** because one repo and one set of rooms must
be moved for everybody. When a user's switch changes no shared state, all of it goes.

With it: `MANAGER.prepare_branch_switch`, `BranchSwitchTeardown`, the 1013 close path,
the `_teardown_via_loop` cross-thread bridge, and **both HEAD-reconciliation helpers** —
`_reconcile_head` exists because git HEAD is a cache of a DB column, and with a worktree
per branch each HEAD is pinned by git. The concept disappears rather than being ported.

The frontend switch-confirm dialog goes too: under this model a switch evicts nobody, so
the warning becomes a lie.

## 6. Traps

Verified against the code, in rough order of danger.

1. **Seven of eight `write_and_commit_files` call sites do not reconcile HEAD.** Only
   `checkpoint.py:212` does (via `checkpoint.py:169`). The other seven —
   `save.py:286`, `human_cite.py:239`, `citations/core.py:631`, `bibliography.py:627`,
   `analysis/insert.py:418`, `review/pass_runner.py:515`, `web/api/workspaces.py:572` —
   commit into whatever HEAD is. Each must be handed a branch-specific worktree. **A
   caller that keeps passing `workspace_repo_dir(...)` still compiles, still commits, and
   still returns a sha — it just writes to the wrong branch.** This is the single largest
   silent-breakage surface.

2. **`uq_artifact_versions_ws_sha` is UNIQUE on `(workspace_id, commit_sha)`**
   (`models.py:1147-1152`). "A branch starts as a full copy" + "versions are
   branch-scoped" means copying the parent's version rows, which reuses the parent's
   shas and violates this index. **The unique key must gain the branch column in the same
   migration, or the very first fork fails.**

3. **The switch returns early on `target == recorded` BEFORE checking the branch exists**
   (`branches.py:173-176`). Unreachable today. Under per-user selection — where user A can
   delete a branch user B is recorded on — it becomes a live path that silently succeeds
   against a missing ref.

4. **Background passes are not gated on the branch.** `_has_running_turn`
   (`branches.py:81-97`) checks `turn_jobs` only; five other pass families run straight
   through. Under this model each job must carry the branch (or the worktree path), and
   handing it the wrong one commits to the wrong branch **with no error at all**.

5. **The commit mutex is per-workspace** and "serializes every writer for the workspace"
   (`fencing.py:129`). Every room open takes it. With branches committing independently,
   this becomes a workspace-wide bottleneck across branches that have nothing to do with
   each other. Whether it becomes per-branch is an open decision (§7).

6. **`bump_lifecycle_generation` is a single per-workspace integer** (`models.py:696-700`)
   with only three writers. Making the fence per-branch is a **separate** schema change,
   and the fence architecture depends on the mutex totally ordering it. Do not fold it in.

7. **Migration `0066` does real work in `upgrade()`** — it opens a session and reads
   `os.environ["WORKSPACES_DIR"]`, defaulting to the relative path `var/workspaces`. Any
   migration that drops `current_branch` must not assume `0066` is inert, and
   `branch_backfill.py` must stay importable because `0066` imports it by name.

8. **`docs/ARCHITECTURE.md` mentions "branch" zero times.** The generated map does not
   describe this feature at all — so `scripts/gen_architecture.py` will need to learn it,
   or the map will confidently omit the thing being built.

## 7. Open decisions

*(The chat question that stood here is decided — see §3.1. The rule turned out to be the
general one, not an exception: the chat is a document like any other.)*

**7.1 — Does the commit mutex become per-branch?** Trap 5. Per-branch is obviously right
for throughput and obviously more dangerous: the fence's ordering guarantees are built on
a single per-workspace mutex.

**7.2 — What happens to a user sitting on a branch someone else deletes?** Today's
equivalent cannot arise. Needs a defined behaviour, not an accident. Related: trap 3,
where the switch's early return would silently accept a branch that no longer exists.

**7.3 — Can the root branch be deleted, and what is "promote" exactly?** A ref move is
easy; the question is whether `main` is a distinguished name or just the fork root.

## 8. Sizing

This is not a slice. It is a subsystem change touching the room registry, the durable
log, the git layer, the schema, the fence, six background pass families, and the frontend
cache keys — with 59 path call sites and a silent-failure mode on seven commit sites.

**Do not start it until §7.1–7.3 are decided**, and expect to stage it: schema and paths
first (with the old single-branch behaviour preserved), then per-branch rooms, then
per-user selection, then fork-copy, then section transfer.

## 9. What made this design possible

Worth recording, because it nearly went the other way.

The stated reason to leave branching alone was a survey: *"287 workspace repos, 68
multi-branch, zero with a divergent tip — nobody has ever used it."* That survey counted
`var/workspaces`, where **the test suite was writing 16 real workspace repos per run**.
488 of the 497 directories present were test litter, and the `main, v2` branches it found
were created by `test_api_branches.py` and `test_branch_create_delete.py`.

**There was never any evidence about branch usage.** The isolation fix is `7c6b5499`;
the cleanup left 8 real directories. Any future survey of that directory is now
meaningful. The previous one was not.
