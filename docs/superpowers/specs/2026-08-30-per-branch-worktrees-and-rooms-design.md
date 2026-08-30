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
  repo/                    # the repository. BARE of working files; nobody edits here
  worktrees/<branch_id>/   # one per branch, INCLUDING the root branch
```

Every branch gets a worktree, the root included, and `repo/` holds no checkout of its
own. Otherwise promote would have to physically move the root branch into `repo/` — and
§7.3 says promote is a pointer move. Path resolution must not depend on which branch the
root pointer names. (Review finding 6: the first draft said both "a worktree swap" and
"a one-row write", which cannot both be true.)

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
to artifact or commit state (`models.py:843-865`), There are 145
chat turns in the entire instance, so a fork-time copy costs nothing and beats a
pointer-plus-cutoff scheme on simplicity. (The copy is *not* trivial for a different
reason — see §4.1.) `position` is **not** a per-workspace sequence: it defaults from the
global `chat_turns_position_seq` and `ix_chat_turns_ws_position` is a plain, non-unique
index that merely orders within a workspace (`models.py:874-877`). Copied turns therefore
keep their source positions verbatim — the ordering survives, and nothing needs a dense
per-branch allocator. (Round-2 finding 6.) `chat_summaries`
(compaction output) is derived from the chat and forks with it.

**The one thing that is NOT a document: `workspaces.last_sdk_session_id`**
(`models.py:760`). It is a resume handle for a conversation living on the hosted side —
`turns/processor.py:389` passes `resume=<id>` when it is non-NULL. Two branches cannot
resume one remote session; doing so would interleave two branches' turns into a single
conversation upstream.

**Rule: a fork copies the chat rows and sets `last_sdk_session_id` to NULL on the new
branch.** (The copy is NOT trivial — see §4.1; three other tables reference `ChatTurn.id`
and must be remapped.) The next turn there starts a fresh hosted session and re-injects the copied
history as context.

This needs no new machinery. Compaction already does exactly this reset for its own
reasons — *"clear `workspaces.last_sdk_session_id` … clearing it forces"* a new session
with re-injection (`turns/compaction.py:138,258`). The fork reuses that proven path.
Because `last_sdk_session_id` lives on the workspace row, it must also move to
per-`(workspace, branch)` state along with branch selection (§5).

### 3.2 A branch has an identity, not just a name

Two independent review findings converge here, so this is a data-model requirement, not
a detail.

**A branch is a row with an immutable id.** The name is a mutable label on it. Every
branch-scoped row and every job carries the **id**, never the name.

Why it is forced:

- **Delete-then-recreate under the same name.** The fence validates the workspace's
  lifecycle generation only. Refusing to delete a branch a *user* is on (§7.2) does not
  stop a *running job*: delete `new_ideas`, recreate `new_ideas`, and the in-flight job
  still passes the workspace fence and writes into the replacement. Names are not
  identities. (Review finding 3.)
- **Slashes are legal in branch names.** `is_valid_branch_name` delegates to
  `git check-ref-format --branch`, which accepts `aims/ambitious` — the delete route
  already needs `{name:path}` rather than `{name}` for exactly this reason
  (`web/branches.py:210`), and there is a test for it. A branch segment in the WebSocket
  URL would hit the same wall. An opaque id sidesteps it. (Review finding 7.)

So §7.2's "refuse to delete a branch someone is on" stays, and the branch id is what
makes it safe rather than merely likely.

## 4. How a branch ends

Three endings, and only one of them is work:

| Ending | Cost |
|---|---|
| **Promote** — `new_ideas` becomes the new root | Near zero: move the root pointer (§7.3) |
| **Bin** — discard `new_ideas` | Near zero: remove the worktree, delete the ref, cascade its rows |
| **Copy sections across** | **The real feature** |

**There is no automatic merge, and none is wanted.** The request was to *"move the new
ideas and concepts that we built"* — a person deciding what to carry over, not a machine
reconciling two edits to one paragraph.

That is fortunate, because automatic merge is not available here anyway:
`apply_pm_section_to_fragment` is delete-then-reinsert, so two branches editing one
section converge on a **duplicated section**, silently, with both sides agreeing.

### 4.1 What "a full copy" actually costs

Three review findings say the fork is not a row copy. All three were verified.

**A fork barrier is required.** A connected client persists inside `room.lock`
**without** the commit mutex — which is precisely why today's switch tears the rooms down
first and aborts if it cannot quiesce them (`branches.py:184-188`). A fork that copies
rows while clients are typing races the same way and produces a log and a watermark that
disagree. **The fork must quiesce each source room, or copy every branch-scoped row from
one explicitly protected snapshot.** Holding the workspace mutex is not sufficient,
because the racing writer does not take it. (Review finding 2.)

**Chat rows need id remapping, so §3.1's "the copy is trivial" was wrong.**
`ChatTurn.id` is a primary key that **four** other columns reference:
`ChatSummary.summarized_through` (`models.py:1121`),
`ArtifactVersion.triggering_turn_id` (`models.py:1166`),
`ProposedChange.triggering_turn_id` (`models.py:1225`) and
`TurnJob.user_turn_id` (`models.py:907-912`, `ON DELETE SET NULL` — round-2 finding 3;
round 1 found only three). Copied turns need new ids and
**every dependent reference remapped in the same transaction** — keeping the old ids
would couple the two branches through the very rows that are supposed to be independent.
(Review finding 4.)

**Only terminal results fork; active jobs do not.** The AI-analysis tables are job queues
with `queued`/`running` statuses, `claimed_at_generation`, SDK session ids and partial
unique indexes on `status IN ('queued','running')` (e.g. `models.py:954-960`). Copying a
running row creates a phantom job nobody is executing, or trips the active-uniqueness
index. **The fork copies completed results only and enqueues fresh work if the new branch
needs any.** (Review finding 5.)

**v1 granularity: whole sections.** Sections are already first-class objects, and it
matches how people talk about a proposal ("our Methods is better on the new branch").

**Later, explicitly out of scope for v1:** AI-assisted transfer of a *concept* — "bring
the new framing of the aims into main" — where the assistant weaves it into the target's
existing prose rather than replacing a section.

## 5. What has to change

Counts below were measured, not estimated.

### Hard

- **`workspace_repo_dir` becomes branch-aware.** It hardcodes
  `<workspaces_dir>/<workspace_id>/repo` (`paths.py:9-12`). **58 call sites across 32
  files** must each decide which branch's tree they mean. This is the largest surface in
  the migration.
- **`RoomKey` becomes a 3-tuple** and the branch threads through every manager method
  and the WebSocket route `/api/ws/workspaces/{workspace_id}/{artifact}`, which has no
  branch segment (`web/collab_ws.py:61,114`). **Two** sites read the key **positionally**
  (`key[0]` at `room.py:529` and `room.py:579`), so they break loudly rather than
  silently — which is the good case. Everything else passes `RoomKey` whole.
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
   each other. DECIDED (§7.1): it stays per-workspace — the cost is throughput, and
   the delete path's single-lock guarantee is worth more than the throughput.

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

## 7. Decisions

All three settled. Nothing in this spec is open.

*(The chat question that stood here is decided — see §3.1. The rule turned out to be the
general one, not an exception: the chat is a document like any other.)*

**7.1 — Does the commit mutex become per-branch? DECIDED: NO. It stays per-workspace.**

The mutex is `pg_advisory_xact_lock` on `_advisory_lock_key("commit", workspace_id)`
(`drafting/locking.py:132-140`) and it *"serializes every writer for the workspace"*
(`fencing.py:129`). **25 callers outside `fencing.py`** (26 counting its own internal
delegation at `fencing.py:160`) — recounted, because a comment at `locking.py:234` says
"27" and the two disagree; that comment is prose from an earlier slice.

The original *reason* for one mutex does dissolve here — it existed because there was one
working tree and one HEAD, and §2 proves two worktrees commit concurrently without
contention. So splitting it is tempting. Three arguments say don't:

1. **Deletion must stop every branch at once.** Retirement is built on a barrier
   (`retirement.py:68`, `_RETIREMENT_BARRIER_SECONDS = 6.0`) and a single lock that halts
   all writers. Per-branch mutexes force the delete path to acquire N locks in a defined
   order — and this protocol documents that it has *exactly one* deadlock shape today
   (`fencing.py:197-201`). Adding N-lock acquisition to a fence that took roughly ten
   merged slices to get right is how that correctness gets lost.
2. **Keeping it costs throughput, never correctness.** Every guarantee the fence makes
   survives a workspace-wide mutex unchanged. The reverse is not true.
3. **The contention does not exist.** Holds are short by rule — *"never hold this across
   context assembly or an LLM call"* (`fencing.py:143`) — so the held section is a git
   commit, milliseconds. Two branches contend only if two commits land in the same
   instant.

The one real cost: hydration takes the mutex on every room open (`hydrate.py:58`), so
opening a room on branch A can queue behind a commit on branch B. Milliseconds, and only
while someone is actively committing.

**Revisit trigger, not a guess:** if `CommitMutexTimeout` starts appearing in logs, or a
room open is measurably delayed by another branch, split it then — with the delete path's
N-lock ordering designed first, as its own piece of work.

**7.2 — What happens to a user sitting on a branch someone else deletes?
DECIDED: refuse the delete.**

Mirror the guard that already exists — today `delete_workspace_branch` refuses the
*current* branch (`branches.py:344-359`); this generalises it to *"is any user on this
branch right now."* Least surprising, smallest change, and trivial to explain: you cannot
delete a room somebody is sitting in. The error should name who, or it is a dead end.

This also disarms trap 3 by construction: if a branch cannot be deleted while someone is
recorded on it, the switch can never resolve a user onto a ref that no longer exists.

**7.3 — Can the root branch be deleted, and what is "promote"?
DECIDED: `main` is not special; the ROOT POINTER is.**

Give the workspace a root-branch pointer. Rules:

- The branch the pointer names cannot be deleted. Every other branch can.
- **Promote = move the pointer.** `new_ideas` becomes the root; the old root becomes an
  ordinary branch, deletable like any other.
- The name `main` is never hardcoded — a workspace whose root is called `v2` is fine.

This keeps exactly one branch that cannot vanish (so the workspace is always resolvable),
makes promote a one-row write rather than a rename, and avoids a magic string.

## 8. Sizing

This is not a slice. It is a subsystem change touching the room registry, the durable
log, the git layer, the schema, the fence, six background pass families, and the frontend
cache keys — with 58 path call sites and a silent-failure mode on seven commit sites.

The design is settled; what remains is sequencing. Five stages, each landing on a green
suite with the product still working:

**S1 — The branch entity, branch-aware paths, behaviour unchanged.**

*There is no branch entity today.* `workspaces.current_branch` is a plain `Text` column
(`models.py:704`) and `grep '^class .*Branch' db/models.py` returns nothing. §3.2 requires
an immutable **id**, so S1 must create the table before anything can carry a foreign key
to it. In order:

1. Create `branches` — `(id, workspace_id, name, created_at)`, unique on
   `(workspace_id, name)`, `name` mutable, `id` never reused.
2. **Backfill one row per existing local ref, not just the current one.** A workspace may
   already have several branches: `create_workspace_branch` has shipped since `880ea243`
   and does nothing but `git branch`. Enumerate every local ref in every workspace repo
   and give each a row; a ref with no row is invisible to every later stage. (Round-2
   finding 4.)
3. Add the branch-id column to the branch-scoped tables — including the
   `artifact_versions` unique key (trap 2) — backfilling every existing row onto the
   workspace's current branch.
4. Add `git worktree` primitives to gitio, and make `workspace_repo_dir` branch-aware at
   all 58 call sites. Every caller passes the workspace's single current branch, so
   nothing observable changes.

This is also where trap 1 is closed: the seven commit sites that do not reconcile HEAD
must name a branch explicitly, and the gate is that passing no branch becomes a **type
error** rather than a silent wrong-branch write.

**S2 — Attach worktrees AND retire checkout-based switching, together.** These cannot
be separate stages, and the first draft of this spec had them separate — a CRITICAL
review finding, confirmed by experiment:

```
$ git worktree add ../wt new_ideas     # S2 as first drafted
$ git checkout new_ideas               # what switch_branch still does
fatal: 'new_ideas' is already used by worktree at '…/wt'
```

The same property that makes the design safe (§2 claim 4: git refuses one branch in two
worktrees) breaks the old switch the moment a branch is attached. So:

**Constraint: no branch may be attached to a worktree while
`switch_branch`'s `checkout_branch(repo, target)` still runs.** Attachment and the
removal of checkout-based switching land in ONE stage. Switching becomes "resolve a
different worktree path"; it still moves the whole workspace at this point, so nothing is
user-visible yet.

**And `repo/` is never attached.** The same git rule bites a second time, from the other
side — round 2 caught it, also by experiment:

```
$ git -C repo branch --show-current        # master
$ git worktree add ../wt-root master
fatal: 'master' is already used by worktree at '/tmp/wtexp/repo'
```

So S2 does not "attach a worktree for every branch". `repo/` **is** the worktree of
whichever branch it currently holds, and that branch is the workspace's root pointer
(§7.3). Only the *other* branches get `worktrees/<branch-id>/`. Path resolution is
therefore `repo/` when the branch is the root and `worktrees/<id>/` otherwise — which is
exactly the resolution §7.3 already requires promote to respect, so promote moves the
pointer and nothing on disk moves. (Round-2 finding 1.)

**S3 — Per-branch rooms.** `RoomKey` becomes a 3-tuple; the log and watermark carry the
branch **id** (§3.2, not the name — the route cannot carry a slash); each room
checkpoints into its own branch's worktree. Two branches can now be edited at once, but
selection is still workspace-wide, so still no user-visible change.

**S4 — Per-user branch selection, and create becomes a real fork.** Branch moves off the
workspace row into per-user state; the frontend cache keys and the picker change. **This
is the stage the user actually asked for.** Three things ride with it that a first draft
put elsewhere:

- **Fork-copy cannot wait for S5.** §1 promises that a new branch *"starts as a full
  copy"*, and today's `create_workspace_branch` is a bare `git branch` whose own docstring
  says it *"changes nothing anybody can see"*. Ship per-user selection on top of that and
  a user switches into a branch with an empty chat and no analysis — the headline promise,
  broken, in the first stage a user can see. §4.1's fork rules (quiesce or snapshot, remap
  the four chat references, terminal results only, null the SDK handle) land **here**.
  (Round-2 finding 2.)
- **Switching must stop being owner-only.** `switch_branch_route` calls `require_owner`
  (`web/branches.py:172`). The requirement is that *different users work on different
  branches at the same time*, which that check makes impossible. Selection becomes
  membership-gated; create, delete and promote may stay owner-only.
  (Round-2 finding 5.)
- **The teardown is retargeted, not deleted.** Switching no longer evicts anyone, so the
  *switch sequence* goes — but `MANAGER.prepare_branch_switch` (`room.py:758`), its
  cross-thread bridge (`web/branches.py:58-100`) and the `locked` proof it returns are the
  only quiesce-and-prove primitive in the codebase, and §4.1's fork barrier depends on it.
  It is currently workspace-wide (it takes only a `workspace_id` and retires both
  artifacts); after S3 it must take a branch. Deleting it here and rebuilding it for the
  fork would repeat exactly the mistake round 1 found in S2.

**S5 — Section transfer between branches.** Whole-section copy, to §4's rules. This is
the only part of the old S5 that can genuinely follow the user-visible stage, because
nothing about the branch model depends on it.

S1 is the largest and the least rewarding: it touches the most files and delivers nothing
a user can see. It is also where the branch entity is founded and the two traps that would
break the first fork are closed, so it cannot be skipped or deferred.

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


## 10. Outside review — round 1

Reviewed by `gpt-5.6-sol` via GitHub Copilot on 2026-08-30 against revision `e82a04c8`.
Package: spec plus `branches.py`, `fencing.py`, `paths.py`, `retirement.py`,
`drafting/locking.py`, `collab/hydrate.py`, `db/models.py` (184,721 chars).
`collab/room.py` and `citations/gitio.py` were deliberately excluded and the reviewer
was told so.

**Eight findings. All eight verified against the code. None refuted.** Every one is
folded into the text above; this section records what changed and why, so a later reader
does not have to re-derive it.

| # | Severity | Finding | Where it landed |
|---|---|---|---|
| 1 | CRITICAL | S2 attaches a worktree to a branch the old switch then tries to `git checkout` — it fails | §8 S2, restaged; **confirmed by experiment** |
| 2 | HIGH | Fork has no snapshot protocol; clients persist without the commit mutex | §4.1, fork barrier |
| 3 | HIGH | Delete-then-recreate under one name lets a stale job write to the replacement | §3.2, immutable branch id |
| 4 | HIGH | Chat copy needs id remapping — three tables reference `ChatTurn.id` | §4.1 and §3.1 |
| 5 | HIGH | Copying active job rows makes phantom jobs / trips active-uniqueness | §4.1, terminal results only |
| 6 | MEDIUM | Promote specified twice, incompatibly ("worktree swap" vs "one-row write") | §2 layout + §4 table |
| 7 | MEDIUM | Branch names contain `/`; a `{branch}` route segment cannot carry them | §3.2, opaque id |
| 8 | LOW | Mutex call-site count contradicted a comment in the source | §7.1, recounted |

Three things are worth keeping in view.

**Finding 1 was reachable from facts already in the spec.** §2 claim 4 records that git
refuses one branch in two worktrees; §8 S2 attached a worktree while the old
`checkout_branch` switch still ran. Both were written by the same author on the same day
and never joined. A measured fact in one section does not defend a plan in another.

**Findings 3 and 7 converge on the same fix from opposite directions** — one about stale
jobs after a delete, one about slashes in URLs — and the answer to both is that a branch
needs an identity separate from its name. Neither alone would obviously have justified a
`branches` table; together they force it.

**Finding 8 disputed a number and neither side was right.** The spec said 26, a source
comment said 27. Recounting gave **25 callers outside `fencing.py`**. The lesson is the
standing one: when a review disputes a count, measure it — do not swap one guess for
another.

§7.1 survived: the reviewer agreed that keeping the mutex per-workspace *"serializes
branches but does not make writes incorrect"*.

## 11. Outside review — round 2

Reviewed by `gpt-5.6-sol` via GitHub Copilot on 2026-08-30 against revision `0a5d5f3a`.
Package: the revised spec plus `branches.py`, `web/branches.py`, `fencing.py`, `paths.py`,
`retirement.py`, `drafting/locking.py`, `collab/hydrate.py`, `collab/room.py`,
`citations/gitio.py`, `db/models.py` (297,415 chars). `room.py` and `gitio.py` were added
**because** round 1 could not check the fork barrier or the worktree paths without them —
the blind spot round 1 flagged was closed rather than carried.

**Six findings from the reviewer, all six verified against the code, none refuted. Four
more found independently while the review ran.** Ten in total, all folded in above.

| # | Source | Severity | Finding | Where it landed |
|---|---|---|---|---|
| 1 | review | CRITICAL | S2 cannot attach the branch `repo/` already holds | §8 S2; **confirmed by experiment** |
| 2 | review | HIGH | S4 ships branch selection while create is still ref-only | §8 S4, fork-copy moved in |
| 3 | review | HIGH | `TurnJob.user_turn_id` is a fourth reference to `ChatTurn.id` | §4.1, three → four |
| 4 | review | HIGH | S1 backfills only the current branch; other refs get no row | §8 S1 step 2 |
| 5 | review | MEDIUM | `switch_branch_route` is `require_owner` — the core requirement is impossible | §8 S4 |
| 6 | review | LOW | `position` is a global sequence, not a per-workspace one | §3.1 |
| 7 | own | HIGH | No `branches` table exists; S1 had nothing for an id to reference | §8 S1 step 1 |
| 8 | own | HIGH | S4 deleted the only quiesce primitive S5's fork barrier needs | §8 S4, retargeted |
| 9 | own | LOW | 59 path call sites across 33 files — measured **58 across 32** | §5, §8 |
| 10 | own | LOW | "Three sites read the key positionally" — measured **two** | §5 |

Four things are worth keeping in view.

**Round 1's CRITICAL had a mirror image, and round 1 missed it.** Round 1 found that
attaching a worktree breaks the old `git checkout` switch. Round 2 found the same git rule
firing from the other side: you cannot attach a worktree for the branch `repo/` is already
sitting on. One property, two failure directions, two rounds to see both — which is the
argument for the second round, not against the first.

**Findings 9 and 10 are the same error round 1's finding 8 was.** Both counts were high by
exactly one, and in the path case the cause is identifiable: the count included
`paths.py`'s own definition as if it were a call site. A count taken by eye is a guess
wearing a number's clothes. Every count in this revision was re-run.

**Finding 7 shows what "verified" bought and what it did not.** Round 1's finding 3
correctly established that a branch needs an immutable id, and §3.2 was written to say so.
Nobody then asked what table the id lives in — the fix was verified against the *finding*
rather than against the *codebase*, and `models.py` has no branch entity at all. A
verified fix to a verified finding can still be unbuildable.

**Findings 2, 7 and 8 are all one shape:** a stage whose precondition a neighbouring stage
never establishes, or destroys. That is the shape of round 1's CRITICAL too. Four
instances across two rounds says the staging — not the design — is where this spec's
remaining risk lives, and it is where a round 3 should look first.
