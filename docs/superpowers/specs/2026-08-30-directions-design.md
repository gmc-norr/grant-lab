# Directions — trying a new direction without git branches

**Date:** 2026-08-30
**Status:** design, decisions taken
**Replaces:** the workspace-branches feature merged at `880ea243`
**Source:** 26-agent design workflow `wf_c94098d9-0ee` (5 readers → 4 designs → 16 adversarial
critiques → synthesis), plus measurements taken after it, which flipped two of its conclusions.

## 1. What the user actually asked for

> "Branches are really convenient to have: the ability to try a new direction and see what
> happens."

That is a **capability**. Git branches are one **mechanism** for it. This design keeps the
capability and drops the mechanism.

## 2. What the measurements settled

Every number below was executed, not estimated.

**The branch feature has never been used for parallel work.**

```
repos = 287    multi-branch = 68    repos with a DIVERGENT tip = 0
```

68 workspaces have extra branch refs. **Not one branch anywhere has ever accumulated a
commit of its own.** Deleting the branch layer destroys nothing.

**The verdict panels are the payload — but only prospectively.**

| | count |
|---|---|
| workspaces | 11 |
| completed `EvaluatorJob`s, ever | 1 |
| completed `ReviewRun`s, ever | 0 |
| completed `CoherenceJob`s, ever | 0 |
| `proposed_changes` rows | 0 |
| chat turns | 145 |

The synthesis named this query as the one that would flip its recommendation from
Directions to the cheaper Waypoints. It came back near zero — but the human resolved the
ambiguity the number cannot: **this instance is pre-launch, not in real use.** Those counts
describe development, not researchers. Directions stands.

**The fork copy is not a bottleneck, and is being removed anyway.**

`reference_chunks`: 558 rows, 8256 kB total; the largest workspace's 549 rows read in
**0.04s** against `_HYDRATE_MUTEX_TIMEOUT_SECONDS = 5` (`hydrate.py:26`). 125× headroom —
but on only 10 references org-wide, so the margin rests on a number that will grow. §5
removes the copy entirely, which retires the risk rather than budgeting for it.

## 3. Auto-merge is impossible here, and that is load-bearing

Three readers independently overturned the 2026-08-27 verdict's reason #4 by measuring:
two pycrdt Docs **forked from a shared ancestor update** and edited independently **do**
merge correctly — base once, both edits interleaved, both sides converging byte-identically.
Concatenation happens only when the two Docs are built by independently re-hydrating
markdown, which is exactly what `branches.py:204-206` (truncate) plus `hydrate.py:69-77`
(re-seed from git) forces today.

So CRDT forking works in principle. **It still does not work here.**
`apply_pm_section_to_fragment` (`yjs_bridge.py:290`) replaces a section by **deleting its
block range and inserting new blocks**. Two directions editing the same section therefore
merge to a **duplicated section** — silently, and both sides converge on the duplicate.
Three critiques found this independently.

**Consequence, which shapes everything below: bringing work back is a human election, never
a machine merge.** A design that promises automatic merging is promising a corruption.

## 4. The mechanism

**A direction is a first-class Workspace row forked from a root.**

Chosen because **every read surface in the cockpit is keyed on `workspace_id`** — rooms
(`RoomKey = (workspace_id, artifact)`), the yjs log, the commit mutex
(`_advisory_lock_key('commit', workspace_id)`), the fence generation, the repo path, the
evaluator scorecard, Problems, Claims, Activity, the version graph, and the agent's read
tools. A direction inherits all of them for free. Every rejected alternative paid a
CRDT-layer dimension change and still left those panels reading the trunk.

```
root workspace  ──── references, chunks, citekeys (ONE corpus, ONE keyspace)
   │
   ├── direction A   own draft, own yjs log, own rooms, own repo, own passes
   └── direction B   own draft, own yjs log, own rooms, own repo, own passes
```

Fork copies **the draft only**. Adopt is a human picking sections, applied back-to-front.

## 5. Decisions taken

**D1 — Directions, not Waypoints.** A direction is LIVE: it can run the evaluator, the
review panel and the coherence pass on the alternative, so "see what happens" means
comparing *verdicts*, not just prose. Justified by D0 below.

**D0 — This instance is pre-launch.** The near-zero pass counts in §2 reflect development.
If that ever proves wrong — real researchers, still no completed passes — Waypoints becomes
correct and is roughly 3× cheaper. Re-run the §2 query before slice 4.

**D2 — Delete the branch layer.** ~2,070 lines across 13 files. Safe: zero divergent tips.

**D3 — Directions SHARE the root's reference corpus; they do not copy it.** References are
inputs, not the draft; two directions of one proposal read the same papers. This kills the
fork-copy cost at any scale **and dissolves the citekey-collision problem entirely** — one
corpus means one keyspace, so adopt can never rebind a citation marker to the wrong source.
(The workflow assumed copying and therefore budgeted a ~150-line content-identity slice to
dedupe on `original_sha256` and re-key. That slice is now unnecessary, not deferred.)

**D4 — A direction is a first-class workspace owned by its creator.** Not a hidden child of
the root. A postdoc can fork a direction off the PI's workspace and own it, which is the
common case in a hospital research group. Cost accepted knowingly: family membership,
family spend and family visibility become real concerns rather than a nullable column.

**D5 — Corpus reads re-check live access to the ROOT.** D3 and D4 are each fine and
together they leak. Access today is `ws.owner_id == user.id` OR a `WorkspaceViewer` row
(`ws_auth.py:108-118`), and reference access is implied by access to the reference's own
`workspace_id` — the root's. So a postdoc who forks a direction and is later removed from
the root would keep reading the PI's papers, which hold uploaded PDF text.

Rule: **the direction's draft belongs to its creator forever; every reference and chunk read
re-checks live access to the root.** Revoke someone and their direction keeps their own
writing and immediately stops serving the root's papers. The same rule covers root deletion.

**D6 — Adopt is a human election, never an automatic merge.** Forced by §3.

## 6. What gets deleted

~2,070 lines across 13 files:

`branches.py` (363), `web/branches.py` (235), `branch_backfill.py` (44), the 8 gitio branch
verbs, `room.prepare_branch_switch` + `BranchSwitchTeardown`,
`checkpoint._reconcile_head_for_checkpoint` (a fail-open path with **zero** test
references), `workspaces.current_branch`, `api/branches.ts` + `BranchPicker.tsx` + 2
frontend tests, and **989 lines of branch tests across 6 files** (not 899/5 — the sixth is
`tests/proposal_assist/collab/test_collab_branch_teardown.py`, the only test of
`prepare_branch_switch`).

**Deleting `branches.py` does not recover the whole cost.** The `880ea243` merge was
**3050 insertions / 172 deletions across 33 files**; only 407 lines are branch-only modules.
The rest is permanent modification of collab internals (`room.py` +263, `gitio.py` +134,
`checkpoint.py` +74, `hydrate.py` +67, `collab_ws.py` +48, `turns/processor.py` +33) plus
348 lines of frontend, including two permanent taxes on hot paths: a commit-mutex
acquisition on every collab room open, and a shared lease on every drafting-turn claim.

**Do NOT delete, despite the temptation:** the `1013` close code (independently load-bearing
at `collab_ws.py:54,183,200`), `hydrate`'s per-open commit mutex (`truncate_log` keeps its
kill-switch-handover caller), and `claim_turn_job`'s shared lease (removing a fence needs
its own proof).

## 7. Prerequisite — the unfenced commit sink

`review/pass_runner.py:433` commits to git with **no mutex and no fencing at all** —
measured: zero occurrences of `branch`, `acquire_commit_mutex`, `acquire_shared_lease`,
`live_at_generation`, or any fencing import. It is the only production commit site taking no
mutex. Of the 8 git-commit sinks in `src/`, exactly one (`collab/checkpoint.py:212`)
consults `workspaces.current_branch`; four of the rest are inert because they abort with
`CollabSeveranceConflict` once collab is enabled, and `bibliography.py:627` is unreachable
(both production callers pass `commit=False`).

This must be fixed **before** anything else, and it matters independently of Directions:
`git commit` carries no pathspec (`gitio.py:198`), so it commits the whole index and an
unmutexed writer can hand a checkpoint a commit it did not author.

**Hoist the mutex ABOVE `pass_runner.py:413`**, not at `:433`. The finalize already holds
`s.get(ReviewRun, with_for_update=True)` at `:413`; taking the mutex after it makes the
finalize J→M against the delete path's M→J — a real `40P01` deadlock, and exactly the
inversion `fencing.py:139-145` forbids.

## 8. Also confirmed while measuring

`switch_branch` is **not** the stop-the-world its comments imply. It stops drafting turns
and live editors. Its running-work guard queries only `turn_jobs` (`branches.py:90-97`) and
never consults `ReviewRun`, `CoherenceJob`, `EvaluatorJob`, `UncitedClaimsJob`,
`ClaimAnalysisJob`, `AttachmentJob` or `WorkspaceSetupJob`. **Five background pass families
run straight through a branch switch today**, and one of them commits to git unfenced (§7).

The version graph draws **nothing** from `ProposedChange` — it is `git log` via
`list_commits`, rendering a flat `<ul>`, and `gitio.py:646` requests
`%H%x00%aI%x00%s` with **no `%P`**. There is no parent data in the API at all, so any real
fork visualisation needs that format string changed first.

## 9. Still open

- **Concurrency cap.** Every background drain is `max_instances=1`, strictly serial, sharing
  one RTX 2080 Ti and a global FIFO claim. N directions is N+1 queues per user interleaving
  with every other tenant. The synthesis recommended a hard cap of 2 open directions per
  root plus excluding directions from auto-enqueue reconcile ticks (five lines) over
  building family-scoped budgets and a fair queue (a subsystem). D4 changes the scope of
  that cap from per-root to per-(root, creator); pick the scope before slice 3.
- **Family spend.** `llm_spend` caps are per-workspace, so a family of directions is
  invisible to them. Interacts with the `spend_slot` work
  (`2026-08-30-spend-slot-design.md`), which is where the per-workspace cap query lives.
- **Adopt UI.** Heading-aligned section election, applied back-to-front through
  `apply_pm_section_to_fragment`, with a normalize-and-compare no-op guard and the `b""`
  guard from `kernel.py:288-299` — replacing content identical to the live doc still emits a
  317-byte update that destroys every item id, and one `b""` row makes every future
  hydration of that artifact raise forever.
- **`WorkspaceEvent` rendering.** `activity/feed.py:172-175` silently **drops** unmapped
  kinds. `direction_created` / `direction_adopted` need `_RENDER` entries and the closed
  `Literal`s widened, or the one surface a co-author would check shows nothing.
- **`checkpoint_artifact`'s two workspace ids.** It locks `stamp.workspace_id` but reads and
  writes a separate `workspace_id` parameter, with nothing asserting they agree. Directions
  is the first two-workspace operation in the tree. Make it a hard assert plus a gate, not a
  convention.
