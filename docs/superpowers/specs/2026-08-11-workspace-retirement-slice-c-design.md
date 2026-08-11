# Workspace retirement — Slice C design

Date: 2026-08-11
Status: approved (design), revision 3 after two review rounds; implementation plan to follow
Predecessors: Slice A (`docs/superpowers/plans/2026-07-24-workspace-retirement-slice-a.md`, `7b16547`), Slice B-mutex (`docs/superpowers/plans/2026-07-24-workspace-retirement-slice-b-mutex.md`, `194d8ce`), Slice B-retire (`docs/superpowers/plans/2026-07-25-workspace-retirement-b-retire.md`, `245d7ba`, review fixes `01edf10`)

## 1. Why this slice exists

Slices A and B made deletion durable and linearized it against the two
subsystems holding live per-workspace state that they inventoried: turns (SDK
subprocesses) and collab (Yjs rooms). Two residuals were carried forward: the
analysis passes, and the non-mutex writers in `web/api/settings.py`.

Two review rounds corrected this design. Round 1 found the residuals were
scoped too narrowly and the teardown bound proved nothing. Round 2 found that
the corrected mechanisms introduced their own defects — most seriously, a
Phase-0 row lock that could block DELETE indefinitely, and a "confirmed dead"
guarantee resting on a reconciliation sweep that never sees pass adapters.

**Slice C narrows the gap substantially and does not close it.** §10 states
exactly what remains open, including one race this slice cannot remove.

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
| phase-0 setup (`phases/setup.py:123`) | **no** | **no** | no | §7 |

Claim-analysis claims on `status == "queued"` alone, reads workspace files,
performs LLM work, and runs **synchronously** in APScheduler's thread pool via
`_analysis_drain_tick` (`scheduler.py:989`) — an asyncio registry cannot cancel
it at all. Phase-0 setup is worse in kind: `_snapshot_and_init_repo` does
`repo_dir.mkdir(parents=True, exist_ok=True)` (`setup.py:226`), so a setup job
running after a delete **recreates the tree `scrub_workspace_bytes` just
removed** — resurrection, not merely a leak.

## 4. Cancelling in-flight passes

### 4.1 The registry

A new module `src/call_manager/proposal_assist/passes.py` holds a
process-global `PASS_REGISTRY`, mirroring how collab exposes `MANAGER`.

`track(workspace_id, adapter)` is an async context manager registering an
`Entry` — `(task, adapter, handle | None)` — in a
`dict[uuid.UUID, set[Entry]]`, discarding it in a `finally` so a cancelled pass
still deregisters. The task is `asyncio.current_task()`, which avoids threading
a handle from the scheduler down to the runner.

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

`retire_workspace` asks each adapter whether the generation it just tore down
is confirmed dead (a small read over the adapter's own live-record inventory —
the same state `confirm_dead` writes). Unconfirmed entries move to a
module-level `PASS_REGISTRY` unconfirmed list, drained by a new scheduler tick
that retries `terminate` and drops the entry once confirmed.

This mirrors `reconcile_live_clients` for a population it does not cover,
rather than modifying it. It is a *remediation attempt each tick*, not a
guarantee — exactly the wording the controller's own sweep uses.

### 4.5 Wrap points

All four runners establish `workspace_id` in a short opening session
(`coherence:50`, `evaluator:57`, `review:200`, `uncited_claims:84`). Everything
from the adapter build onward is wrapped in
`async with PASS_REGISTRY.track(workspace_id, adapter) as entry:`, with
`entry.bind_handle(handle)` after launch. For review, both the orchestration
body and each `_run_one_call` body are wrapped. The prefix preceding all this
is handled in §5.

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

`main.py` registers `"passes": PASS_REGISTRY.retire_workspace`, and
`REQUIRED_HOOKS` becomes `("turns", "collab", "passes")` so dropping the wiring
fails at boot.

Hooks run concurrently, so the budget is the max, not the sum:

| hook | worst case |
|---|---|
| collab | 3.0s (max over two concurrent rooms: 1.0 lock + 2.0 drain) |
| turns | 5.0s (3.0 disconnect + 2.0 reap) |
| passes | 5.5s (`_PASS_RETIRE_SECONDS`, independent of entry count per §4.2) |

`max(3.0, 5.0, 5.5) = 5.5 < 6.0` — **the barrier constant does not change.**
`test_the_barrier_exceeds_every_hooks_own_worst_case` is extended to read the
new constant, and must also assert the *concurrency* property, since a serial
retirement would silently make the budget entry-count-dependent.

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
promptly**. §7 shows the same idiom being unsafe when the holding session lives
for minutes; the difference is not stylistic, and the prefix's lock duration is
pinned by a test rather than assumed.

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
- **A callback inside the semantic-audit loop.** One checkpoint per job is not
  "bounded by one LLM call": `validate_citations_semantic` iterates evidence
  rows making per-row judgements (`citations/audit.py:583`), so the abort
  callback must be threaded into that loop.
- **The failure path must not resurrect a cancelled job.** `mark_failed`
  returns a job to `queued` whenever `attempts < MAX_ATTEMPTS`
  (`queue.py:216-230`), so the generic handler at `pipeline.py:578` would
  requeue a job the deletion just cancelled. The handler must detect the
  deleted/cancelled case and write a terminal status instead.

## 7. Phase-0 setup

**Do not take a row lock on the long-lived setup session.** `run_one` commits
immediately after the claim to release the claim lock, then calls `_run_steps`
on that *same* session, whose next commit is only after every step finishes
(`setup.py:155`) — including two potentially slow extractors. A `FOR UPDATE` on
the workspace row taken inside `_snapshot_and_init_repo` would therefore be
held across the whole of Phase 0. That is not merely slow: `acquire_commit_mutex`
restores `SET LOCAL lock_timeout = DEFAULT` after acquiring
(`locking.py:201`), so the delete transaction's subsequent row UPDATE has **no
timeout and would block indefinitely**.

Instead:

- **Live filter** on `claim_next_setup_job`.
- **Split Phase 0 into short linearized transactions**: one around filesystem
  initialization (take the row lock, re-check liveness, `mkdir` + git init,
  commit, release), and short result-commit transactions after each extractor,
  each re-checking liveness. The long extractor work holds no locks.
- **Cancel setup jobs in the post-commit retirement path**, for the same
  row-lock reason as §6.
- **Special-case deletion in the exception path** so a cancelled job is not
  requeued.

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
| `retry_setup` | `rubric.py:168` | re-queues a setup job (§7 makes this a resurrection risk) |

**Lock-order rule (normative).** `acquire_commit_mutex_live` is taken
**immediately after `require_owner`, before any other lock**. `activate` and
`retry_setup` both currently take `WorkspaceSetupJob … FOR UPDATE`
(`rubric.py:180`, `rubric.py:217`) as their first lock; once delete also
cancels setup jobs, that order inverts against delete's mutex→job-row order and
deadlocks. The rule is stated here because it is not locally obvious in either
route.

**Error mapping is written per route, explicitly.** There is no app-level
`CommitMutexTimeout` handler — every route returning 429 today does so with its
own `except CommitMutexTimeout` (e.g. `web/api/workspaces.py:446`), and an
uncaught one falls through to the generic 500 handler (`main.py:566`). Each of
the six gets `except CommitMutexTimeout` → 429 with `BUSY_ERROR_MESSAGE` and
`Retry-After: 2`; `WorkspaceDeleted` propagates to main.py's handler → 404.

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
   it — so `scrub_workspace_bytes` can run while an unconfirmed subprocess is
   still alive with the repo as its cwd. This slice reduces the window and
   makes it observable (`drafting_termination_unconfirmed`); it does not close
   it. Closing it means either blocking scrub on confirmation or making scrub
   tolerate a live cwd, both larger than this slice.
2. **Claim-analysis cannot be cancelled mid-call.** Sync code in a thread pool.
   §6 bounds the residual to the interval between abort callbacks.
3. **A full per-route writer inventory is not done.** Six routes are
   linearized; the rest of the mutating surface is uncategorized.
4. **The conceded in-flight-query race** is unchanged: a delete committing
   after a check can still meet a live streaming call, with the reconciliation
   sweep as backstop, as documented on `WorkspaceRetired`.
5. **Two unverified minor findings** from the B-retire review (fixed-sleep
   races in two collab tests) stay out of scope.

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
   no-op must fail a test driving the real DELETE route against a live pass.
2. **Scrub waits for teardown completion.** With an adapter whose teardown
   outlasts its task's exit, assert `scrub_workspace_bytes` is not entered
   until teardown *finishes* — not merely that `terminate` was called.
3. **Unconfirmed death is retained, not swallowed.** An adapter reporting
   unconfirmed must leave the entry on the reconciliation list and the tick
   must retry it. The gate asserts retention, **not** that scrub waited —
   §10.1 is a documented residual and the test encodes it as such.
4. **Retirement during `terminate()`.** A delete landing while the runner is
   already inside its own `finally` still ends with teardown joined, via the
   shared generation in `_retire_generation`.
5. **Retirement during `launch`.** A delete landing while `client.connect()` is
   awaited must still complete teardown within the bound — the case revision 2
   gave only 1.0s.
6. **Review fan-out.** Every child task and handle is tracked independently;
   cancelling retires all of them. Mutation: make children share the parent
   entry and the test must fail.
7. **Concurrent retirement.** N entries for one workspace retire in
   ~`_PASS_RETIRE_SECONDS`, not N×. Mutation: serialize the gather and the test
   must fail.
8. **Deregistration.** Normal completion, raise, and cancellation each leave
   the registry empty for that workspace.
9. **Prefix guard.** A delete committing while a prefix is mid-flight leaves
   zero prefix writes, and the prefix's lock duration is asserted bounded.
10. **Phase-0 holds no long lock.** A DELETE issued while Phase 0 runs its
    extractors must complete promptly. Mutation: hold the row lock across
    `_run_steps` and the test must fail (this is the indefinite-block case).
11. **Phase-0 cannot resurrect.** delete → scrub → a setup job already past its
    claim must not recreate the repo directory, and must not be requeued.
12. **Claim-analysis.** The live filter refuses at claim time; an abort
    callback fires inside the semantic-audit loop; a cancelled job is not
    returned to `queued` by `mark_failed`; and the delete transaction does not
    block on a running job's row.
13. **Lock order.** For `activate` and `retry_setup`, a delete concurrent with
    the route resolves without deadlock. Mutation: take the setup-job lock
    first and the test must fail.
14. **Budget invariant.** The extended barrier test fails if
    `_PASS_RETIRE_SECONDS` is raised past the headroom.
15. **Route straddles.** Each of the six routes: 404 with nothing written
    against a workspace deleted under the mutex; 429 with `Retry-After: 2`
    under a wedged mutex.

## 13. Risks

- **Cancelling the scheduler's tick task.** Each drain tick claims one job and
  awaits it inline, so the registered task does nothing but this pass. Pinned
  by test, because a tick that later batches jobs would widen the blast radius.
- **A pass that swallows cancellation.** Nothing does today (§4.6), but a
  future `except BaseException` would defeat the mechanism silently. Gate 2
  catches it.
- **Phase-0 transaction splitting is the largest behavioural change here.**
  Breaking one long transaction into several changes the failure atomicity of
  setup: a crash between transactions can now leave a partially-initialized
  workspace. The split must keep filesystem init and its status write in the
  same transaction, and the existing rollback-then-fail path must be re-checked
  against each new boundary.
- **`_PASS_RETIRE_SECONDS` and the adapter's internals are coupled.** 5.5s is
  chosen against 3.0+2.0. Gate 14 reads the adapter's signature defaults, as
  the existing barrier test already does, so raising either fails loudly.

## 14. Footprint

One new module and one new scheduler tick; a tracking wrapper in each of four
runners plus review's per-child wrapper; a row-lock guard in each of four
prefixes; one explicit catch at the tick boundary; two claim-query filters; two
cancels moved to the post-commit retirement path; a Phase-0 transaction split;
an abort callback threaded into the semantic-audit loop; two failure-path
special cases; six route changes with a normative lock order; one hook
registration; the wrapper deletion plus its test helper; and the
`retirement.py:35` comment correction. No schema migration.
