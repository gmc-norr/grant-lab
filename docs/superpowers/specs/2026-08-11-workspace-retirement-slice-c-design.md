# Workspace retirement — Slice C design

Date: 2026-08-11
Status: approved (design), revision 7 after two review rounds and three implementation corrections (see Revisions); implementation plan to follow
Predecessors: Slice A (`docs/superpowers/plans/2026-07-24-workspace-retirement-slice-a.md`, `7b16547`), Slice B-mutex (`docs/superpowers/plans/2026-07-24-workspace-retirement-slice-b-mutex.md`, `194d8ce`), Slice B-retire (`docs/superpowers/plans/2026-07-25-workspace-retirement-b-retire.md`, `245d7ba`, review fixes `01edf10`)

## Revisions

- revision 8 (2026-08-14, Slice C2 residual-8 audit): residual 8's account of
  what happens when a bound makes `_m1` raise was wrong in both halves and
  self-contradictory — a row marked `'failed'` is exactly the row
  `requeue_stuck_setup_jobs` can never requeue. `_fail` IS the requeue
  (`attempts + 1`, back to `'queued'`, dead-lettered to `'failed'` only at
  `MAX_SETUP_ATTEMPTS`); `requeue_stuck_setup_jobs` filters `status ==
  'running'` in both UPDATEs and is not on this path at all. The conclusion
  (contention → bounded retry) survives, the mechanism is now named correctly.
  Two smaller corrections from a full re-check of every claim in the residual:
  the delegation line is not the 28th line of that grep's output, and
  `soft_delete_workspace_db` has one *production* caller (tests override the
  timeout via `functools.partial`). §8's `main.py` line reference for the
  generic 500 handler corrected 566 → 570. Everything else in residual 8 was
  re-verified against the tree and is accurate.
- revision 7 (2026-08-14, Slice C2 review fixes): revision 6's freshness fix
  **disarmed** gate 11(b)'s commit-mutation for the two rubric routes, silently.
  The re-read takes the `workspaces` row `FOR UPDATE` as the first statement
  inside the bound, so a holder's 55P03 now fires there — inside any plausible
  narrow wrap — instead of at COMMIT, and dedenting `session.commit()` out of
  the bound in either rubric route leaves all 50 tests in
  `test_api_writers_mutex_straddle.py` + the rubric/settings API suites green.
  §12 gains gate 16, a static AST check that re-arms §8's bound-through-COMMIT
  rule for all six routes; 11(b)'s commit-mutation obligation moves there. The
  runtime hazard those two routes retain is narrower but real — see §8.
- revision 6 (2026-08-14, Slice C2 review fixes): §8 gains a third normative
  rule — **freshness**. Serializing the commit is not enough for a route whose
  write is a *diff*: `require_owner` loads the `Workspace` before the mutex,
  `acquire_commit_mutex_live` refreshes `soft_deleted_at` and nothing else, and
  `expire_on_commit=False` means nothing else expires it either — so
  `_apply_rubric` was computing `content_changed` and the `rubric_edited` event's
  `old_*` payload from state as old as the request. C2 is what made that window
  large (a few milliseconds → the whole mutex wait), so C2 owns the fix. §12
  gains gate 15.
- revision 5 (2026-08-13, Slice C2): §8's bound rule was wrong about its own
  scope and, read literally, asserted an invariant the codebase does not have.
  Its parenthetical named "both setup-job routes"; in fact all six routes hold
  the mutex across a row wait, and C2 now bounds all six. The rule is restated
  as a requirement on those six. §10 gains residual 8, naming the root cause —
  the deliberate `SET LOCAL lock_timeout = DEFAULT` restore at
  `drafting/locking.py:207` — which leaves the post-mutex tail unbounded at 27
  call sites across 14 modules, of which this slice bounds 6, and why the
  central fix is a design pass rather than a follow-up commit. §12 gate 11(b)
  widened from two routes to six, gate 13 disambiguated from it, §14 updated.
- revision 4 (2026-08-13, Slice C2): §8's lock-order rationale and §12 gate 11
  replaced. The rule stands; the deadlock justifying it does not exist. Verified
  against the merged tree at 53fa9fc — soft_delete_workspace_db never touches
  WorkspaceSetupJob, and setup-job cancellation is post-commit and SKIP LOCKED.
  The rule's real basis is convergence (a held row is skipped by the hook), and
  C2 bounds the mutex→row wait it newly introduces.

## 1. Why this slice exists

Slices A and B made deletion durable and linearized it against the two
subsystems holding live per-workspace state that they inventoried: turns (SDK
subprocesses) and collab (Yjs rooms). Two residuals were carried forward: the
analysis passes, and the non-mutex writers in `web/api/settings.py`.

Three review rounds corrected this design. Round 1 found the residuals were
scoped too narrowly and the teardown bound proved nothing. Round 2 found that
the corrected mechanisms introduced their own defects. Round 3 found that
post-commit cancellation was named but never operationally defined, that
unconfirmed subprocesses could be orphaned on *ordinary* pass exit, and that
the Phase-0 transaction split was too large to ride along here.

**Slice C narrows the gap substantially and does not close it.** §10 states
exactly what remains open, including races this slice cannot remove.

### Prerequisite: Phase-0 is now its own slice

Phase-0 setup is removed from Slice C and becomes a separate slice,
**"Phase-0 retirement and transaction boundaries"**, landed immediately before
this one. Slice C depends on it.

The reason is that the proposed boundaries are not a rearrangement. Both
extractors take a session and perform the LLM call *and* the database mutation
as one unit of work under a "caller commits" contract
(`proposal_assist/extractors/refresh.py:40` and `:96`). Genuinely short result
transactions require separating computation from persistence and defining crash
recovery across partially-completed milestones — with the one asset that both
extractors are already idempotent under a `(source_text_hash,
extractor_version)` guard, which is the natural basis for that recovery story.

That slice owns: the `live_ws` filter on `claim_next_setup_job`, the
transaction split, per-milestone idempotency and recovery, cancelling setup
jobs without requeueing them, and the post-delete-write gate (a claimed setup
job must not create a repo directory for a deleted workspace). Slice C assumes
it has landed.

## 2. What Slices A/B already guarantee — and its exact limits

Three guarantees already hold **for the four named passes only** (coherence,
evaluator, review, uncited-claims):

1. **No new pass claims a deleted workspace.** All four claim queries in
   `{coherence,evaluator,review,uncited_claims}/jobs.py` join against
   `live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))`.
   Durable in SQL, survives restart.
2. **Their job rows are terminalized by the delete transaction**
   (`workspaces.py:125-131`), in the same transaction as the soft-delete stamp.
3. **Each runner terminates its own handle** in a `finally`, bounded by a
   `wait_for` on a `*_terminate_timeout_seconds` setting.

All three stop at the boundary of those four tables, and none of them covers
the pre-launch prefix inside the runners themselves.

## 3. Live-work inventory

| workload | claim filters live ws? | cancelled by delete txn? | cancellable in-process? | this slice |
|---|---|---|---|---|
| turns (SDK) | n/a | yes | yes (Slice B registry) | unchanged |
| collab rooms | n/a | n/a | yes (Slice B manager) | unchanged |
| coherence / evaluator / review / uncited — **launch+drive** | yes | yes | **no** | §4 |
| the same four — **pre-launch prefix** | at claim time only | yes | no | §5 |
| claim-analysis (`analysis/queue.py:162`) | **no** | **no** | **no — sync, APScheduler threadpool** | §6 |
| phase-0 setup (`phases/setup.py:123`) | **no** | **no** | no | prerequisite slice (§1) |

Claim-analysis claims on `status == "queued"` alone, reads workspace files,
performs LLM work, and runs **synchronously** in APScheduler's thread pool via
`_analysis_drain_tick` (`scheduler.py:989`) — an asyncio registry cannot cancel
it at all. Phase-0 setup is worse in kind: `_snapshot_and_init_repo` does
`repo_dir.mkdir(parents=True, exist_ok=True)` (`setup.py:226`), so a setup job
running after a delete **creates a brand-new repo tree, git history and empty
artifacts for an already-deleted workspace**. Note this is *not* resurrection
of scrubbed bytes: `scrub_workspace_bytes` removes only `attachments/` and
`references/`, and both storage contracts preserve the sibling `repo/`
(`attachments/storage.py:140`, `references/storage.py:106`). The defect is a
post-delete write, which is why the prerequisite slice states its guarantee as
"no post-delete repo creation".

## 4. Cancelling in-flight passes

### 4.1 The registry

A new module `src/call_manager/proposal_assist/passes.py` holds a
process-global `PASS_REGISTRY`, mirroring how collab exposes `MANAGER`.

`track(workspace_id, adapter=None)` is an async context manager registering an
`Entry` — `(task, adapter | None, handle | None)` — in a
`dict[uuid.UUID, set[Entry]]`, removing it in a `finally` so a cancelled pass
still deregisters. The task is `asyncio.current_task()`, which avoids threading
a handle from the scheduler down to the runner.

`adapter` is optional because **review's orchestration task owns no adapter**:
`run_review_pass` launches nothing, so its entry is task-only and retirement
cancels it without terminating anything. Round 3 caught this — the earlier
`track(workspace_id, adapter)` signature could not represent the parent at all.

**Removal is not discard.** See §4.4: an entry whose generation is unconfirmed
at exit is *transferred*, not dropped.

**The adapter is bound at entry, not at launch.** Round 2 established that an
entry is unbound for the whole of `adapter.launch()`, not merely between its
return and a later `bind` call: `launch` registers the client and then awaits
`client.connect()` (`claude_adapter.py:212`), which is unbounded. Binding the
adapter up front means the registry always has *something* to act on;
`entry.bind_handle(handle)` on the line after launch adds the handle when it
exists.

No in-memory tombstone set: §2.1 already provides the "refuse to claim" gate
durably in SQL.

### 4.2 One entry per live task, all retired concurrently

Round 2 corrected the fan-out model. `run_review_pass` **launches nothing** —
every `_run_one_call` constructs its own adapter and its own handle
(`review/pass_runner.py:95`), so each child has a distinct adapter-local
generation. There is no parent handle to bind and no shared generation to
inherit.

So: the orchestration task is tracked, **and each child task is tracked as its
own entry** with its own adapter and handle.

`retire_workspace(workspace_id)` therefore retires **all** entries for the
workspace **concurrently** (`asyncio.gather`), not in sequence. Concurrency is
what keeps the hook's cost independent of entry count: five review personas
plus a coherence pass is six entries, and serial retirement would blow the
budget six times over.

### 4.3 The bound, and what awaiting it does *not* prove

The first draft cancelled the task and awaited it for 2 seconds. That was wrong
because `ClaudeAgentSDKAdapter.terminate` routes to `_retire_generation`, which
does `await asyncio.shield(record.teardown)` (`claude_adapter.py:562`): under
cancellation the shield delivers `CancelledError` to the awaiter while teardown
keeps running. The adapter's own budget is `disconnect_deadline_seconds=3.0` +
`reap_deadline_seconds=2.0` = 5.0s (`claude_adapter.py:167`).

Revision 2 then split this into a 1.0s task-exit bound and a 5.0s teardown
bound. Round 2 found two problems with that: a cancellation landing inside
`launch`'s own `except BaseException: await self._retire_generation(...)`
cleanup needs the *full* teardown budget, but was only given 1.0s; and a 5.0s
outer timeout around an inner 3+2=5.0s teardown is a coin flip, not a bound.

**Final shape.** Per entry: `task.cancel()`, then await **concurrently**, both
under a single `_PASS_RETIRE_SECONDS = 5.5`:

- the task's exit; and
- `adapter.terminate(handle)`, when a handle is bound.

5.5s exceeds the adapter's internal 5.0s worst case with real margin and still
fits under the 6.0s barrier. Concurrent awaiting is safe by design:
`_retire_generation` is documented as "shared by retirement, capacity eviction,
idle sweep and reconciliation, so concurrent callers never spawn a second kill
for the same generation" — a second caller *joins* the single teardown task
rather than racing it. Because the registry's await is not itself cancelled, it
observes the shielded task to completion.

**What this proves, precisely: teardown ran to completion. Not that the process
died.** `_teardown_record` returns normally when death is *unconfirmed*, logging
`drafting_termination_unconfirmed` and retaining the record
(`claude_adapter.py:573`). Revision 2 claimed such records "fall to the
reconciliation sweep"; they do not. `WorkspaceController.reconcile_live_clients`
(`controller.py:302`) sweeps the *interactive registry's* adapter, and these
one-shot pass adapters are built per-pass by `build_keeper_adapter` and friends
— nothing owns or sweeps them. §4.4 fixes that; §10.1 records what still leaks.

### 4.4 Unconfirmed generations get their own reconciliation

Confirmation is read from the adapter's own live-record inventory — the same
state `confirm_dead` writes. An unconfirmed generation moves to a module-level
`PASS_REGISTRY` unconfirmed list, drained by a new scheduler tick that retries
`terminate` and drops the entry once confirmed.

**The transfer happens on every `track` exit, not only on retirement-driven
exit.** Round 3 caught the hole: revision 3 transferred unconfirmed adapters
only when `retire_workspace` handled them, while the context manager removed
its entry unconditionally. So an *ordinary* completion — or a pass timeout —
that calls `terminate()`, gets an unconfirmed result, and exits before any
deletion snapshots the registry would leave the subprocess ownerless, with
nothing for the new tick to inspect. Deleting the workspace a moment later
would then find nothing to retire.

So the `finally` in `track` checks confirmation before removing the entry, and
transfers when unconfirmed. Retirement-driven exit is just one path into the
same check.

This mirrors `reconcile_live_clients` for a population it does not cover,
rather than modifying it. It is a *remediation attempt each tick*, not a
guarantee — exactly the wording the controller's own sweep uses.

### 4.5 Wrap points

All four runners establish `workspace_id` in a short opening session
(`coherence:50`, `evaluator:57`, `review:200`, `uncited_claims:84`). Everything
from the adapter build onward is wrapped in
`async with PASS_REGISTRY.track(workspace_id, adapter) as entry:`, with
`entry.bind_handle(handle)` after launch. For review, the orchestration body is
wrapped with `track(workspace_id)` (task-only, no adapter) and each
`_run_one_call` body with the adapter form. The prefixes preceding all this are
handled in §5.

Wrapping inside the runner rather than the tick covers every caller, not only
the scheduler path.

### 4.6 Why cancellation reaches the `finally` at all

`asyncio.CancelledError` inherits directly from `BaseException`, so the
runners' broad `except Exception` does not swallow it: the `finally` runs and
the cancellation propagates. **No change to any runner's error handling.** The
failure path `except Exception` normally takes — re-open a session, `_fail` the
job, supersede partial rows — is correctly skipped, because the delete
transaction already terminalized that row (§2.2).

### 4.7 Logging wrinkle

`concurrent.futures.CancelledError` is a different class from
`asyncio.CancelledError` and **is** an `Exception` subclass, so the scheduler
tick's `fut.result()` inside `except Exception` would log
`"coherence tick failed"` with a traceback for a deliberate cancellation. Catch
it explicitly at the tick boundary and log it at info level as a retirement.

### 4.8 Coordinator wiring and barrier budget

`main.py` registers `"passes": PASS_REGISTRY.retire_workspace`, so
`REQUIRED_HOOKS` becomes `("turns", "collab", "jobs", "passes")` and dropping
the wiring fails at boot. The `"jobs"` hook already exists — the prerequisite
slice (§1) introduces it for setup jobs, and Slice C adds claim-analysis to it
rather than inventing a second cancellation path.

Hooks run concurrently, so the budget is the max, not the sum:

| hook | worst case |
|---|---|
| collab | 3.0s (max over two concurrent rooms: 1.0 lock + 2.0 drain) |
| turns | 5.0s (3.0 disconnect + 2.0 reap) |
| passes | 5.5s (`_PASS_RETIRE_SECONDS`, independent of entry count per §4.2) |
| jobs | 1.0s (`_JOBS_CANCEL_SECONDS`; SKIP LOCKED, so it never waits — §6) |

`max(3.0, 5.0, 5.5, 1.0) = 5.5 < 6.0` — **the barrier constant does not
change.** `test_the_barrier_exceeds_every_hooks_own_worst_case` is extended to
read both new constants, and must also assert the *concurrency* property, since
a serial retirement would silently make the budget entry-count-dependent.

The comment block at `retirement.py:35` is corrected in the same edit.

## 5. The pre-launch prefix

Every runner's prefix reads workspace files through
`assemble_*_context(..., workspaces_dir=...)` — coherence's docstring says so
explicitly (`coherence/pass_runner.py:38`) — and three of the four commit
durable writes before any tracking point exists: evaluator commits
`evaluated_draft_sha` / `evaluated_spec_sha` / `evaluated_rubric_updated_at`;
review inserts `ReviewRunItem` rows and commits `run.draft_sha`;
uncited-claims commits `evaluated_draft_sha`.

**Guard:** each prefix loads the workspace with `with_for_update=True` and
aborts if `soft_deleted_at` is set, holding that row lock through its commit.
The delete transaction's `ws.soft_deleted_at = ...` takes the same row lock, so
only two orders exist — the prefix commits and the delete waits briefly behind
it, or the delete wins and the prefix aborts having written nothing.

This is safe **only because the prefix is short and its session commits
promptly**. The Phase-0 slice (§1) shows the same idiom being unsafe when the
holding session lives for minutes; the difference is not stylistic, and the
prefix's lock duration is pinned by a test rather than assumed.

### 5.1 Review's children have a second prefix

Round 3 found that review has *two* prefixes, not one. Each `_run_one_call`
commits `item.status = "running"` before its adapter is constructed or tracked
(`review/pass_runner.py:65-92`). It does re-read `run.status` first, but that
read takes no lock, so it is a TOCTOU window: a DELETE can cancel the parent
run between the read and the commit, after which the child commits
`status = "running"` and is then cancelled — leaving a `ReviewRunItem`
stranded in `running` on a deleted workspace.

The child prefix is linearized on the **parent `ReviewRun` row**, loaded
`with_for_update`, rather than on the workspace row. The `ReviewRun` row is the
tighter choice: the delete transaction already updates review-run rows through
`cancel_review_runs_for_workspace`, so locking it linearizes against exactly
the write that matters, without adding N persona-shaped contenders to the
workspace row every delete must pass through.

## 6. Claim-analysis

Claim-analysis gets the durable half only. It cannot be cancelled mid-call: it
is sync code in a thread pool with no cancellation primitive.

- **Live filter** on `claim_queued_jobs`, matching the other four.
- **Cancel after the delete commits, not inside its transaction.** Round 2
  established why: `touch_running_job`'s UPDATE takes a row lock **held for the
  whole per-job transaction** (`queue.py:190`, and its docstring says so). A
  cancel UPDATE inside the delete transaction would block on that row while
  holding the uncommitted workspace deletion — stalling DELETE and making the
  worker's own liveness re-read unable to observe the deletion. So the cancel
  moves to the post-commit retirement path.
- **Liveness checks run in short independent sessions**, never the worker's
  per-job session, for the same reason.

### 6.1 What "post-commit" means operationally

Round 3 was right that "moved after commit" is not a design. It is the
`"jobs"` coordinator hook — introduced by the prerequisite slice (§1) for setup
jobs, and extended here to claim-analysis — awaited alongside the others:

- **It is a required hook**, so an unwired cancel fails at boot. That
  structural guard is the one that previously caught an unwired turns hook.
- **It runs its UPDATEs via `asyncio.to_thread`** — the coordinator is async,
  these are sync DB writes.
- **Every cancel UPDATE uses `FOR UPDATE SKIP LOCKED`.** This is what makes the
  hook bounded: a row a live worker holds is *skipped*, never waited on, so the
  hook cannot inherit the worker's transaction lifetime. Without SKIP LOCKED
  the reviewer's objection stands — the wait is unbounded.
- **Skipped rows are retried by the reconciliation tick**, which re-attempts
  the cancel once the lock frees. In the meantime the worker's own liveness
  checkpoint aborts it, so the skip is not the only defence.

  **This is an explicit extension, not an assumption.** The prerequisite slice
  ships that tick handling *setup jobs only*; Slice C extends the same tick to
  terminalize `ClaimAnalysisJob` rows for soft-deleted workspaces. It uses
  **`failed`**, not a new status: `ck_claim_analysis_jobs_status` allows
  `queued/running/done/failed/superseded` and Slice C ships no migration. Both
  the tick change and its gate (10b) are in this slice's footprint — without
  them a skipped row is never cancelled by anything, since the worker's
  checkpoint only aborts a *live* worker and a crashed one leaves the row
  behind.
- **Budget `_JOBS_CANCEL_SECONDS = 1.0`** — a pair of indexed UPDATEs that by
  construction never block. It is a timeout for pathology, not for expected
  waiting.

Because a bounded delete transaction is not the same as a bounded route, gate 14
measures **end-to-end DELETE latency** with a worker mid-job, not merely that
the delete transaction committed.
- **A callback inside EVERY per-row LLM loop — there are two.** One checkpoint
  per job is not "bounded by one LLM call": `validate_citations_semantic`
  iterates evidence rows making per-row judgements (`citations/audit.py:583`),
  and `analyze_job`'s uncited phase makes a classify (plus a retrieve-and-judge
  fan-out) per *sentence* (`pipeline.py:418`). The callback must be threaded
  into both, at the top of each loop and before its row-shape filters. Threading
  it into the semantic audit alone leaves a section with no `ev:` markers
  consulting it zero times — the audit is guarded by `if cited_evs`.
- **Neither terminal path may resurrect a cancelled job.** `mark_failed`
  returns a job to `queued` whenever `attempts < MAX_ATTEMPTS`
  (`queue.py:216-230`), so the generic handler at `pipeline.py:578` would
  requeue a job the deletion just cancelled: it must detect the
  deleted/cancelled case and write a terminal status instead. The SUCCESS path
  needs the mirror-image guard — the hook can win the job row whenever the
  worker's session holds no lock on it (every sentence a cache hit, so
  `touch_running_job` never ran), so `mark_done` re-reads the row FOR UPDATE and
  returns unless it is still `running`. Without that, 'done' is flushed over the
  hook's 'failed' while `last_error='cancelled: workspace deleted'` stays behind.

## 7. Phase-0 setup — carved out

Phase-0 is handled by its own prerequisite slice (§1). The finding that forced
the split is recorded here because it is the sharpest constraint the new slice
inherits:

**A row lock must not be taken on the long-lived setup session.** `run_one`
commits immediately after the claim to release the claim lock, then calls
`_run_steps` on that *same* session, whose next commit is only after every step
finishes (`setup.py:155`) — including two potentially slow extractors. A
`FOR UPDATE` on the workspace row taken inside `_snapshot_and_init_repo` would
be held across the whole of Phase 0. That is not merely slow:
`acquire_commit_mutex` restores `SET LOCAL lock_timeout = DEFAULT` after
acquiring (`locking.py:201`), so the delete transaction's subsequent row UPDATE
has **no timeout and would block indefinitely**.

## 8. Writers under the live mutex

Six owner-write routes are brought under `acquire_commit_mutex_live`, following
the precedent at `web/api/workspaces.py:537`:

| route | file:line | why |
|---|---|---|
| `update_settings` | `settings.py:69` | title/status/deadline/citation_style |
| `add_member` | `settings.py:97` | inserts `WorkspaceViewer` + audit event |
| `remove_member` | `settings.py:135` | deletes `WorkspaceViewer` + audit event |
| `update_rubric` | `rubric.py:148` | mutates rubric fields on `Workspace` |
| `activate` | `rubric.py:206` | rubric fields + phase transition |
| `retry_setup` | `rubric.py:168` | re-queues a setup job (a resurrection risk until the §1 slice lands) |

**Lock-order rule (normative).** `acquire_commit_mutex_live` is taken
**immediately after `require_owner`, before any other lock**. Lock-free work may
sit between the two — and semantic body validation specifically SHOULD, so that
authorization decides before a validation error can answer a caller who has no
access, and so a malformed body does not queue behind the mutex. What may not
sit there is another lock. `activate` and `retry_setup` both currently take
`WorkspaceSetupJob … FOR UPDATE` (`rubric.py:180`, `rubric.py:217`) as their
first lock, and that order must change.

The lock-order rule's reason is not deadlock. As of the pre-C2 tree, no AB-BA
cycle is reachable in either order: `soft_delete_workspace_db` never touches
`WorkspaceSetupJob`
(its mutex transaction locks the workspace row, the four pass-job tables and
`TurnJob`), setup-job cancellation runs in a separate post-commit transaction
with `FOR UPDATE SKIP LOCKED`, and every other setup-job acquisition is either
`SKIP LOCKED` or mutexless — nothing held the mutex and then waited on a
setup-job row. C2 introduces the first such edge itself, and bounds it (the
bound rule below), so the property is maintained by construction rather than by
luck.

This does **not** license inverting `phases/setup.py`'s own documented order
(`:498-503`, job → workspace → Call): that ordering answers a different,
real 40P01 against `_m2`/`_m3`, and it is what `activate` matches.

The lock-order rule's reason is **convergence**. A route that takes the
setup-job row before blocking on the mutex holds that row for the whole wait — up to
`timeout_seconds`, default 10s. The delete's retirement hook cancels setup jobs
with `SKIP LOCKED`, so it *skips* the held row and the job stays non-terminal
until the reconciliation tick catches it (up to `jobs_reconcile_tick_seconds`,
default 60s). With the mutex first, the route raises `WorkspaceDeleted` before
touching the row, the hook finds it free, and cancellation is immediate.

**Bound rule (normative — a requirement on these six routes, not a property of
the codebase).** Each of the six holds the mutex across a subsequent row wait,
and each of those waits must carry its own bound. It is **not** only the two
setup-job routes: `update_settings` and `update_rubric` (through `_apply_rubric`)
UPDATE the `workspaces` row itself, `add_member` and `remove_member` take
`FOR KEY SHARE` on it as the FK parent of their `WorkspaceViewer` /
`WorkspaceEvent` inserts, `activate` waits on its `WorkspaceSetupJob` row *and*
on the `workspaces` row through `_apply_rubric`, and `retry_setup` waits on its
`WorkspaceSetupJob` row. The reason is the one `row_locks.py`'s module docstring
gives: `acquire_commit_mutex` restores `lock_timeout = DEFAULT` on success
(`drafting/locking.py:207`), so an unbounded wait under the mutex pins it and
blows the DELETE route's < 6.0s budget
(`tests/proposal_assist/test_phase0_transaction_boundaries.py:1155`) — the
hazard §4 closed for the pass prefixes.

Two constants carry it, both 2s and both in `row_locks.py`:
`SETUP_JOB_LOCK_TIMEOUT_SECONDS` for the two setup-job routes and
`WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS` for the four that wait on the workspace row
directly. Read the guarantee at its real strength: `lock_timeout` caps each
acquisition **individually**, so `activate` — which waits on its job row and then
on the `workspaces` row — can stall up to 2 × 2s under the mutex, and a DELETE
queued behind it waits that long before starting its own work. The constants
prevent an unbounded pin; they do not compose into a transaction-wide deadline,
and §10.8 records that gap. Their values (and that arithmetic) are pinned by
`tests/proposal_assist/test_row_lock_ceilings.py` — every contention test
monkeypatches them down to stay fast, so nothing else exercises the production
numbers. In every case the bound must wrap the remainder of the transaction
**through `session.commit()`**, not merely the `with_for_update()`:
`bounded_lock_waits` leaves the `SET LOCAL` in force on exit but translates
55P03 → `RowLockTimeout` only inside its own body, so a narrow wrap leaves the
flush bounded yet untranslated and answers 500 where the route contracts 429.

Because that is a *lexical* requirement — this statement inside that block — it
is enforced lexically, by gate 16's AST check, and not only by interleaving
tests. The freshness rule below is why: it made the interleaving tests stop
discriminating for the two routes it touches (revision 7), and a rule whose only
enforcement can be disarmed by an unrelated correct fix is not enforced.

Read this rule at exactly its stated scope. The restore at `locking.py:207`
un-bounds the post-mutex tail at **every** call site in the tree, and this slice
re-imposes a bound at six of them; the rest are untouched and stay unbounded.
That is §10.8 — the single root cause, and why closing it centrally is a
separate design pass rather than a wider edit here.

**Freshness rule (normative — for the routes whose write is a diff).** A route
that decides *what* to write by comparing the request against current state must
re-read that state **under the mutex**, before the comparison. Winning the mutex
does not refresh anything: `require_owner` loads the `Workspace` instance before
the mutex; `acquire_commit_mutex_live`'s guard is a scalar
`select(Workspace.soft_deleted_at)` that deliberately round-trips the *column*
and so repopulates no attribute; and `db/session.py` builds the sessionmaker with
`expire_on_commit=False`. Everything except `soft_deleted_at` is therefore as old
as the request — and as of C2, "as old as the request" means up to the mutex's
full 10s wait.

Of the six, only `update_rubric` and `activate` are exposed, and only through
`_apply_rubric`'s change detection. `update_settings` writes absolute values from
the body with no old-vs-new comparison; `add_member` and `remove_member` re-query
the `WorkspaceViewer`/`User` rows they act on inside the transaction, and the one
pre-mutex attribute they read, `ws.owner_id`, is never reassigned anywhere in the
tree (workspace ownership cannot transfer). `_apply_rubric`'s own logic is
correct and unchanged; only the freshness of its input was wrong. Symptoms of the
stale read: an identical concurrent write records a *second* `rubric_edited`
event for one edit, and a distinct one records `old_criteria` from a rubric two
versions back, dropping the intervening revision out of the audit chain.

The re-read goes **after** any lock the route already takes, preserving the order
above: `update_rubric` is M → W, `activate` is M → J → W, which is
`phases/setup.py` `_m3`'s documented order (job → workspace → Call). Both sit
inside the route's `bounded_lock_waits`, so the new wait is bounded and its 55P03
maps to the same 429 as every other wait in the transaction.

**Side effect on the bound rule's coverage, stated because it cost a gate.**
Holding the `workspaces` row from the re-read through COMMIT removes the
*designed* slow holder — the four pass prefixes — from these two routes'
commit-time waits, which is why dedenting their `session.commit()` out of the
bound stopped failing the `hold_workspace_row` gates. It does **not** make the
bound decorative for them. What still waits at their COMMIT is the
`WorkspaceEvent` insert's `actor_id` FK: it takes `FOR KEY SHARE` on a `users`
row, and `FOR KEY SHARE` conflicts with `FOR UPDATE`, which
`web/api/admin_users.py`'s `_lock_active_admin_count` takes over every active
admin row.

Measured — and the measurement is now *in the tree*, which it was not when
revision 7 first asserted it (that measurement came from a scratch test that was
deleted, leaving the claim unreproduced). Hold the acting user's `users` row,
dedent the commit out of the bound, and `PUT /api/workspaces/{id}/rubric` raises
`OperationalError (psycopg.errors.LockNotAvailable): canceling statement due to
lock timeout / CONTEXT: while locking tuple in relation "users" / SQL statement
"SELECT 1 FROM ONLY public.users x WHERE id = $1 FOR KEY SHARE OF x"` — a 500.
With the commit inside the bound the same interleaving answers 429.
`tests/proposal_assist/test_api_writers_mutex_straddle.py`'s
`test_update_rubric_maps_a_commit_time_users_fk_wait_to_429_not_500` and
`test_activate_maps_a_commit_time_users_fk_wait_to_429_not_500` *are* that
interleaving, so gate 11(b)'s runtime half is restored for the two routes the
freshness rule disarmed — alongside gate 16's static check, not instead of it.

The holder is short-lived: a lock-and-count transaction, with
`signal_user_revocation` and its 5s barrier outside the session. So this is a
narrow hazard rather than the pass-prefix one — but it is a real 55P03 at
COMMIT, and it is the reason the rule is re-armed rather than relaxed for these
two routes.

Short-lived is also why `row_locks.py` and
`tests/proposal_assist/test_row_lock_ceilings.py` leave `users` rows OUT of the
contended-row count that turns a per-lock cap into a worst-case stall. That is
not a contradiction of the paragraph above, and neither statement should be
"corrected" against the other: they answer different questions about the same
lock. *How long can this stall?* — not long enough to add another whole cap,
because no holder keeps a `users` row across slow work the way a pass prefix
keeps the `workspaces` row. *Can a 55P03 fire here at all?* — yes, at COMMIT,
and untranslated it is a 500. The first governs the ceilings' arithmetic; the
second governs the bound. If that 5s barrier ever moves inside the admin guard's
session, the first answer changes and the row count has to change with it.

Mechanism, because the obvious spelling does not work: use
`session.refresh(ws, with_for_update=True)`, **not**
`session.get(Workspace, id, with_for_update=True)`. A non-`None`
`with_for_update` makes `Session.get` skip the identity lookup and emit the
`SELECT … FOR UPDATE`, so the row lock is taken — but the loader does not
overwrite attributes that are already populated, so the instance keeps its stale
values and the bug survives with a lock around it. (Verified empirically against
SQLAlchemy 2.0.49, and by mutation: swapping `refresh` for that `get` leaves all
four gate-15 tests red. `get` needs `populate_existing=True` to refresh.)

**Error mapping is written per route, explicitly.** There is no app-level
`CommitMutexTimeout` handler — every route returning 429 today does so with its
own `except CommitMutexTimeout` (e.g. `web/api/workspaces.py:446`), and an
uncaught one falls through to the generic 500 handler (`main.py:570`). Each of
the six gets `except CommitMutexTimeout` → 429 with `BUSY_ERROR_MESSAGE` and
`Retry-After: 2`; `WorkspaceDeleted` propagates to main.py's handler → 404. The
bound rule adds a second per-route catch on the same terms: `RowLockTimeout` →
the identical 429, since "the mutex was wedged" and "the row was held" are the
same answer to the caller — retry in 2s.

**The claim is narrow.** This does *not* make every workspace writer
linearized. A file-level survey shows twenty modules with mutating routes that
never call `acquire_commit_mutex`; many are linearized indirectly through the
collab kernel or `save_section`, others may not be linearized at all, and
separating the two needs a per-route inventory that is out of scope. The
accurate claim is: **these six routes are linearized.**

## 9. Removing the `soft_delete_workspace` foot-gun

`workspaces.py:213` still exports the pre-split wrapper that runs the DB half
then the FS half. It has zero production callers (the route calls the halves
directly so it can interleave retirement) and about nine test call sites.

It is a foot-gun, not merely dead code: it is the obvious-looking "delete a
workspace" entry point and it skips retirement entirely. Delete it and move the
composition into a helper under `tests/` — coverage migrates rather than being
dropped. The production surface then offers only `soft_delete_workspace_db` and
`scrub_workspace_bytes`, whose names promise nothing about retirement.

## 10. Open residuals after this slice

1. **Teardown completion ≠ process death.** When `_disconnect_bounded` cannot
   confirm death, §4.4's tick retries, but the DELETE route does not wait for
   it. The residual is **not** an rmtree-versus-cwd conflict — the scrub never
   touches `repo/` (`attachments/storage.py:140`,
   `references/storage.py:106`), so nothing is pulled out from under the
   process. It is that an unconfirmed subprocess keeps running against the
   *retained* repo after the workspace is deleted: it can still read workspace
   content, and can write or recreate sibling data, with no owner and no
   session. This slice reduces the window and makes it observable
   (`drafting_termination_unconfirmed`).

   **Blocking scrub would not close it.** Since the scrub never touches
   `repo/`, delaying an unrelated operation changes nothing about the live
   process. Real closure needs one of: confirmed process death before the
   delete completes; filesystem or process isolation so the subprocess cannot
   outlive its workspace; or a deletion protocol that actively revokes the
   subprocess's access to the repo. All three are larger than this slice, and
   naming them is the point — the residual has no cheap fix.
2. **Claim-analysis cannot be cancelled mid-call.** Sync code in a thread pool.
   §6 bounds the LLM-spend residual to the interval between abort callbacks —
   one evidence row's judgement in the cited audit, or one sentence's classify
   plus retrieve-and-judge in the uncited loop. Two caveats on that bound. The
   annotation fan-out (`pipeline.py:501-553`) has no callback, but it makes no
   LLM call, so it costs nothing beyond a short DB tail. And the abort's
   `s.rollback()` discards only the per-job transaction: `store_verdict` and
   `_persist_classification` commit their caches in independent sessions and
   survive by design.
3. **Phase-0 cannot stop a running extractor call**, for the same reason — it
   is sync code with no cancellation primitive. **DELETE does not wait for it:**
   the route returns, and the worker and its cloud spend continue until the
   call returns on its own. The prerequisite slice bounds the residual to one
   extractor call by ensuring no *further* call starts; it does not shorten
   that call.
4. **A job row held by a live worker is skipped, not cancelled**, until the
   reconciliation tick retries it (§6.1). Bounded by the worker's own
   checkpoint, not by the delete.
5. **A full per-route writer inventory is not done.** Six routes are
   linearized; the rest of the mutating surface is uncategorized.
6. **The conceded in-flight-query race** is unchanged: a delete committing
   after a check can still meet a live streaming call, with the reconciliation
   sweep as backstop, as documented on `WorkspaceRetired`.
7. **Two unverified minor findings** from the B-retire review (fixed-sleep
   races in two collab tests) stay out of scope.
8. **One line un-bounds the post-mutex tail everywhere; this slice bounds six of
   the places it lands.** `acquire_commit_mutex` executes
   `SET LOCAL lock_timeout = DEFAULT` after it wins the advisory lock
   (`proposal_assist/drafting/locking.py:207`). That line is deliberate, not an
   oversight: the restore arrived in the same commit as the `timeout_seconds`
   bound itself (`63bd9cf`). That bound was written *for the acquisition* — so a
   wedged holder cannot pin FastAPI workers indefinitely — and the restore keeps it
   scoped there, so the caller's own later statements, which never asked for a
   bound and carry no 55P03 translation, are not silently made failable. The
   price is that from the acquisition onward the transaction runs at
   `lock_timeout = 0` again.

   `acquire_commit_mutex` / `acquire_commit_mutex_live` are called at **27 sites
   across 14 modules**:

   ```
   grep -rn "acquire_commit_mutex_live(\|acquire_commit_mutex(" src --include=*.py \
     | grep -v "def acquire_commit_mutex"
   ```

   returns 28 lines; one of them — `locking.py:230` — is
   `acquire_commit_mutex_live` delegating to `acquire_commit_mutex`, not a
   caller, which is where 28 lines becomes 27 sites. This slice
   re-imposes a bound at **6** of the 27 (`web/api/settings.py` ×3,
   `web/api/rubric.py` ×3, §8). The other 21 — `collab/kernel.py` ×5,
   `tools/citations.py` ×4, `web/api/workspaces.py` ×3 (the spec-artifact
   routes `post_init_spec`/`post_lock_spec`/`post_unlock_spec` at `:537`,
   `:648`, `:736` — *not* the DELETE route), `workspaces.py` (which is where
   the DELETE's own `soft_delete_workspace_db` write lives, `:124`),
   `editing/save.py`,
   `turns/submit.py`, `citations/human_cite.py`, `analysis/insert.py`,
   `collab/hydrate.py`, `collab/checkpoint.py`, `collab/flip.py`, and
   `phases/setup.py`'s `_m1` — run their whole post-acquisition tail unbounded.
   Whether each of those actually meets a contended lock today has not been
   audited; the point of the residual is that nothing bounds them if it does.

   **Why it matters.** A mutex holder blocked on a row pins the mutex. Every
   other mutex-participating writer for that workspace then burns its own
   acquisition budget and gives up — 429 for the routes that map
   `CommitMutexTimeout`, a tool error or a skipped tick elsewhere. That budget is
   **not uniform**. Of the 27 sites, 24 take `acquire_commit_mutex`'s
   `timeout_seconds` default of 10s implicitly; two pass their own **5s** —
   `collab/flip.py:61` (`_FLIP_MUTEX_TIMEOUT_SECONDS`) and `collab/hydrate.py:56`
   (`_HYDRATE_MUTEX_TIMEOUT_SECONDS`); and one, `workspaces.py:124`, forwards
   `soft_delete_workspace_db`'s own `timeout_seconds` parameter
   (`workspaces.py:88`), whose default is 10s and which its single production
   caller — the DELETE route at `web/api/workspaces.py:1508` — does not override
   (tests do, via `functools.partial`). So the real
   distribution is **25 × 10s and 2 × 5s**, and the DELETE is one of the 10s
   ones: a pin long enough to exhaust it costs the owner the deletion outright —
   and the
   DELETE route's < 6.0s budget
   (`tests/proposal_assist/test_phase0_transaction_boundaries.py:1155`) is blown
   for as long as the block lasts. The holders are real rather than
   hypothetical: the four pass prefixes take the `workspaces` row `FOR UPDATE`
   while holding no mutex and keep it across context assembly's filesystem reads
   (`row_locks.py`, `PASS_PREFIX_LOCK_TIMEOUT_SECONDS`).

   **Why this is not a patch.** The obvious central fix — have
   `acquire_commit_mutex` leave a bound in force instead of restoring DEFAULT —
   converts a wait into a timeout at all 27 sites simultaneously, and a timeout
   there surfaces as a raw `OperationalError` (55P03) → 500 unless every site
   translates it, or unless an app-level handler maps it to 429. §8 records that
   there is deliberately **no** app-level `CommitMutexTimeout` handler: each
   route writes its own, and an uncaught one falls through to `main.py`'s
   generic `Exception` handler. Adding one is a change to this codebase's
   error-mapping architecture, with blast radius well past this slice. Two
   further constraints any central fix must answer:

   - The bound has to be re-imposed *after* the acquisition. An outer
     `bounded_lock_waits` around a transaction that later takes the mutex has
     its `SET LOCAL` wiped by the DEFAULT restore, so "wrap the transaction" is
     not a safe generic remedy — the six routes here wrap *after* winning the
     mutex for exactly this reason.
   - Background and scheduler callers are affected differently from routes, and
     differently from each other. A route that starts raising returns 429 to a
     caller who can retry. `phases/setup.py`'s `_m1` (the setup worker) and
     `collab/checkpoint.py`'s scheduler tick (`scheduler.py:363`) today *wait*
     where a bound would make them *raise* — but the raise lands in two different
     places. `_m1` raises into `run_one`'s `except Exception` (`setup.py:335`),
     which calls `_fail` — and `_fail` is itself the requeue: on a non-terminal,
     not-deleted job it increments `attempts` (`setup.py:606`) and sets the row
     back to `'queued'` with `started_at = None` (`setup.py:633-634`),
     dead-lettering to `'failed'` only once `attempts >= MAX_SETUP_ATTEMPTS`
     (`setup.py:624-626`).
     So contention there really does become requeue, bounded at 5 attempts — but
     by that path, not by `requeue_stuck_setup_jobs`, which filters
     `status == 'running'` in both of its UPDATEs (`setup.py:226`, `:241`) and so
     can never touch a row `_fail` has already moved off `'running'`; `_fail`'s
     own never-resurrect list treats `'failed'` as terminal too
     (`setup.py:599`). `checkpoint_active_tails` has neither: it
     catches generically per candidate (`checkpoint.py:223`), rolls back, logs
     `collab_checkpoint_tail_failed` and moves on. There is no job row and no
     requeue — the artifact simply stays a candidate and the next scan retries
     it, so a persistently contended log would go on being skipped, silently
     except for the warning, while its tail keeps growing. Two different
     behaviour changes to be designed, not discovered.

   So this residual is one line and one architectural question, not a to-do list
   of 21 call sites. It needs its own design pass; it is explicitly not a
   follow-up commit on Slice C.

## 11. Non-goals

- No new in-memory tombstone for passes (§2.1 covers it durably).
- No change to any pass's own timeout or terminate constants.
- No change to `_RETIREMENT_BARRIER_SECONDS`.
- No change to `reconcile_live_clients` (§4.4 adds a sibling, not an edit).
- No schema migration.

## 12. Testing and acceptance

Acceptance is by **mutation**: each gate must be shown to fail when its fix is
reverted.

1. **Hook wiring, end-to-end.** Replacing `main.py`'s `"passes"` hook with a
   no-op must fail a test driving the real DELETE route against a live pass;
   the same for `"jobs"` against a live claim-analysis job.
2. **Scrub waits for teardown completion.** With an adapter whose teardown
   outlasts its task's exit, assert `scrub_workspace_bytes` is not entered
   until teardown *finishes* — not merely that `terminate` was called.
3. **Unconfirmed death is retained, not swallowed — on every exit path.** Two
   cases, both required: (a) retirement-driven exit; and (b) **ordinary
   completion** whose `terminate()` returns unconfirmed, followed by a delete
   *afterwards*, which must still find the generation on the reconciliation
   list. Case (b) is the hole round 3 found; a test covering only (a) passes
   against the broken design. The gate asserts retention, **not** that scrub
   waited — §10.1 is a documented residual and the test encodes it as such.
4. **Retirement during `terminate()`.** A delete landing while the runner is
   already inside its own `finally` still ends with teardown joined, via the
   shared generation in `_retire_generation`.
5. **Retirement during `launch`.** A delete landing while `client.connect()` is
   awaited must still complete teardown within the bound — the case revision 2
   gave only 1.0s.
6. **Review fan-out.** Every child task and handle is tracked independently,
   and the adapterless parent is tracked task-only; cancelling retires all of
   them. Mutation: make children share the parent entry and the test must fail.
   A second case covers the child prefix (§5.1): a DELETE cancelling the run
   while a child is between its `run.status` read and its commit must leave no
   `ReviewRunItem` stranded in `running`.
7. **Concurrent retirement.** N entries for one workspace retire in
   ~`_PASS_RETIRE_SECONDS`, not N×. Mutation: serialize the gather and the test
   must fail.
8. **Deregistration.** Normal completion, raise, and cancellation each leave
   the live registry empty for that workspace — with any unconfirmed
   generation on the reconciliation list rather than gone (gate 3b).
9. **Prefix guard.** A delete committing while a prefix is mid-flight leaves
   zero prefix writes, and the prefix's lock duration is asserted bounded.
10. **Claim-analysis.** The live filter refuses at claim time; an abort
    callback fires inside the semantic-audit loop; a cancelled job is not
    returned to `queued` by `mark_failed`; and the delete transaction does not
    block on a running job's row.
10b. **Tick convergence for skipped rows.** The full path, not just the hook: a
    running claim-analysis job holds its row → the hook's `SKIP LOCKED` cancel
    skips it → the worker's session ends *without* the worker terminalizing it
    (simulating a crash, so the cooperative checkpoint cannot be what saves us)
    → a later reconciliation tick marks the row `failed`. Mutation: leave the
    tick handling setup jobs only and the row stays `running` forever.
11. **Lock order, and the bound on the wait it creates.** Gate (a) applies to
    `activate` and `retry_setup`; gate (b) applies to **all six** routes. Both
    are required.

    (a) *Order.* While the route is blocked on the commit mutex, the retirement
    hook's `cancel_setup_jobs_for_workspace` must still be able to cancel the
    setup job — proving the route holds no row lock while it waits. Mutation:
    take the setup-job lock first and the test must fail (the `SKIP LOCKED`
    cancel skips the held row and returns 0). Asserted *while* the route is
    still blocked; sampling after the mutex is released lets the route's own
    rollback repair the thing under test. Seed the job in a **cancellable**
    status — `_CANCELLABLE` excludes terminal states, so a `'failed'` row makes
    this gate red in both orders and its mutation vacuous.
    Note: a "resolves without deadlock" gate would NOT discriminate — no AB-BA
    cycle exists in either order. See §8.

    (b) *Bound.* Every one of the six holds the mutex across a row wait (§8), so
    each needs its own bounded case: `activate` and `retry_setup` against a held
    `WorkspaceSetupJob` row (`row_locks.SETUP_JOB_LOCK_TIMEOUT_SECONDS`),
    `update_settings`, `add_member`, `remove_member` and `update_rubric` against
    a held `workspaces` row (`row_locks.WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS`).
    In each case the holder takes the row and NO mutex, so the route wins the
    mutex and blocks afterwards — which is the state under test. The request
    must RETURN 429 rather than block. Mutation: remove the bound from one route
    and that route's test must fail. Asserted as "the request came back", not as
    elapsed time — an elapsed-time assertion can pass for the opposite reason.
    A second mutation is required on at least one route: move `session.commit()`
    outside the bound and the test must fail with an untranslated
    `OperationalError` → 500 rather than the contracted 429 (§8's
    "through `session.commit()`").
    That second mutation discriminates on `update_settings`, `add_member` and
    `remove_member` against the holder described above. It does **not** on
    `update_rubric` or `activate`: gate 15's under-mutex re-read takes the
    `workspaces` row at the TOP of their bound, so a holder's 55P03 fires there
    rather than at COMMIT (revision 7, §8). Those two need a different holder —
    the acting user's `users` row, which their commit-time `WorkspaceEvent`
    insert waits on through `actor_id`'s FK (§8) — and they have one.
    It does **not** discriminate on `retry_setup` either, and there the
    obligation is not merely unmet but UNMEETABLE. Its bound OPENS with the
    setup-job row's `.with_for_update()`, so contention fires at the
    acquisition; it writes no `WorkspaceEvent`, so it has no commit-time FK
    check; and its only flush updates the job row it already holds. There is no
    reachable interleaving in which its COMMIT contends for anything. Measured:
    dedenting its `session.commit()` out of the bound leaves every runtime gate
    green and fails only gate 16's static `[rubric-retry_setup]` case.
    So the runtime obligation is met for **five** routes, and for `retry_setup`
    gate 16 is the sole carrier. Gate 16 carries the rule structurally for all
    six, because a static check cannot be disarmed by a change to WHERE
    contention happens to fire — which is precisely what happened here twice:
    once to the rubric pair (revision 7) and once, permanently, to
    `retry_setup`.
    Note: the constant must be patched on the *route* module, not on
    `row_locks`, or the patch is a no-op — the routes import the value by name.
12. **Budget invariant.** The extended barrier test fails if
    `_PASS_RETIRE_SECONDS` or `_JOBS_CANCEL_SECONDS` is raised past the
    headroom.
13. **Route straddles.** Each of the six routes: 404 with nothing written
    against a workspace deleted under the mutex; 429 with `Retry-After: 2` and
    `BUSY_ERROR_MESSAGE` under a wedged mutex — the *acquisition* failure. The
    second 429 path, where the route wins the mutex and then blocks on a row, is
    gate 11(b); the two are separate failures with the same response, and a
    suite that covers only this one passes with every bound removed.
14. **DELETE route latency, end-to-end.** With a claim-analysis worker mid-job
    holding its row lock, the DELETE *request* must return within the barrier —
    measured as wall-clock on the route, not as "the delete transaction
    committed". Mutation: drop `SKIP LOCKED` from the cancel and the test must
    fail. This is the gate that would have caught the unbounded wait round 3
    identified.

15. **Under-mutex freshness of the rubric diff (§8's freshness rule).** For both
    `update_rubric` and `activate`, two cases each. A holder takes the commit
    mutex; the route queues behind it (established with the keyed `pg_locks`
    probe, never a sleep, so the route's pre-mutex load provably precedes the
    holder's write); the holder then commits a rubric edit R0 → R1 through the
    route's own `_apply_rubric` and releases.
    (a) *Identical.* The route submits the same R1 → exactly **one**
    `rubric_edited` event exists, not two.
    (b) *Distinct.* The route submits R2 → its event carries
    `old_criteria == R1`, so the chain R0 → R1 → R2 is continuous.
    The holder must be the mutex holder, not a second request: a second request
    would queue *behind* the route, which arrived first, and the interleaving
    could not be produced at all. The holder's payload is normalized through the
    route's own `_validate_criteria`, or "identical" would compare shapes and
    case (a) would pass with the bug present. Mutations: remove the re-read and
    all four go red; replace it with
    `session.get(…, with_for_update=True)` and all four stay red (§8).

16. **Bound-through-COMMIT, structurally (§8's bound rule).** A DB-free AST
    check over the real source of `web/api/settings.py` and `web/api/rubric.py`:
    for each of the six routes, the `session.commit()` call must sit lexically
    inside the `with bounded_lock_waits(…)` block, with none outside it. Parsed,
    never matched as text — this repo has already shipped a source-text
    assertion that was a tautology (`src.index(f"def {name}(")` matches inside
    `"async def …"`). The six names are asserted as an explicit set against what
    **those two modules** contain, in both directions, so a deleted or renamed
    route fails instead of leaving the gate covering five, and a *new* bounded
    writer **in one of those two modules** fails until it is listed. Failure
    names the offending route and why. Mutations: six, one per route — dedent
    that route's commit and exactly that route's case must fail; plus a rename,
    which must fail the presence check.
    Scope, stated because the gate's table is also its reach: those two modules
    are all it reads. A `bounded_lock_waits` block added elsewhere in the tree
    (`web/api/workspaces.py`, say) is invisible to it and may put its commit
    outside the bound with the gate green — verified, not assumed. That matches
    §8's bound rule, which is normative for these six routes and explicitly not a
    property of the codebase; §10.8 carries the wider gap. Extending the rule to
    another module means adding that module to this gate's table.
    Static rather than runtime deliberately: the rule is structural, so this is
    its direct encoding, and unlike the interleaving gates it survives a change
    to *where* contention happens to fire — which is precisely what revision 7
    had to repair.

Phase-0's gates (no long-held lock; a claimed setup job must not create a repo
directory for a deleted workspace; a cancelled setup job is not requeued) move
to the prerequisite slice (§1) along with the work.

## 13. Risks

- **Cancelling the scheduler's tick task.** Each drain tick claims one job and
  awaits it inline, so the registered task does nothing but this pass. Pinned
  by test, because a tick that later batches jobs would widen the blast radius.
- **A pass that swallows cancellation.** Nothing does today (§4.6), but a
  future `except BaseException` would defeat the mechanism silently. Gate 2
  catches it.
- **The prerequisite slice is a hard dependency, not a nice-to-have.** Until
  Phase-0 lands, a claimed setup job can still create a repo tree for a deleted
  workspace. Slice C on its own leaves that path open, so the two must land in
  order and the risk of shipping C alone should be stated when planning.
- **`_JOBS_CANCEL_SECONDS` measures pathology, not waiting.** If SKIP LOCKED is
  ever dropped or a cancel query is rewritten to plain `FOR UPDATE`, the
  constant silently becomes a real wait and the barrier erodes. Gate 14 is the
  guard, and it must assert route latency rather than transaction success.
- **`_PASS_RETIRE_SECONDS` and the adapter's internals are coupled.** 5.5s is
  chosen against 3.0+2.0. Gate 14 reads the adapter's signature defaults, as
  the existing barrier test already does, so raising either fails loudly.

## 14. Footprint

One new module and one new scheduler tick; a tracking wrapper in each of four
runners plus review's task-only parent and per-child wrappers; a row-lock guard
in each of four prefixes plus review's child prefix; one explicit catch at the
tick boundary; one claim-query filter; a `"jobs"` hook doing SKIP LOCKED cancels
off the delete transaction; an abort callback threaded into BOTH per-row LLM
loops (semantic audit and uncited classify); one failure-path special case and
one success-path `mark_done` guard; six route changes carrying a normative lock
order plus a bounded post-mutex row wait through commit, and one new `row_locks`
constant (`WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS`) for the four that wait on the
`workspaces` row; one hook registration plus a second population added to **both** the
inherited `"jobs"` hook **and** its reconciliation tick; the wrapper deletion
plus its test helper; and the `retirement.py:35` comment correction. No schema
migration — claim-analysis terminalizes to the existing `failed`.

Phase-0's share — a second claim-query filter, the transaction split, its
recovery story and its cancel path — moves to the prerequisite slice.
