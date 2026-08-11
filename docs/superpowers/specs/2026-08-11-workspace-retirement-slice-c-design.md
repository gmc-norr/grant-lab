# Workspace retirement — Slice C design

Date: 2026-08-11
Status: approved (design), revised after review; implementation plan to follow
Predecessors: Slice A (`docs/superpowers/plans/2026-07-24-workspace-retirement-slice-a.md`, `7b16547`), Slice B-mutex (`docs/superpowers/plans/2026-07-24-workspace-retirement-slice-b-mutex.md`, `194d8ce`), Slice B-retire (`docs/superpowers/plans/2026-07-25-workspace-retirement-b-retire.md`, `245d7ba`, review fixes `01edf10`)

## 1. Why this slice exists

Slices A and B made deletion durable and linearized it against the two
subsystems holding live per-workspace state that they inventoried: turns (SDK
subprocesses) and collab (Yjs rooms). Two residuals were carried forward: the
analysis passes, and the non-mutex writers in `web/api/settings.py`.

A review of the first draft of this design found that both residuals were
described too narrowly, and that the proposed teardown bound did not prove what
it claimed. This revision corrects that. **The honest summary is that Slice C
narrows the gap substantially but does not close it**, and §9 states exactly
what remains open.

## 2. What Slices A/B already guarantee — and its exact limits

Three guarantees already hold **for the four named passes only** (coherence,
evaluator, review, uncited-claims):

1. **No new pass claims a deleted workspace.** All four claim queries in
   `{coherence,evaluator,review,uncited_claims}/jobs.py` join against
   `live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))`.
   Durable in SQL, survives restart.
2. **Their job rows are terminalized by the delete transaction.**
   `workspaces.py:125-131` calls the four `cancel_*_for_workspace` helpers in
   the same transaction as the soft-delete stamp.
3. **Each runner terminates its own handle** in a `finally`, bounded by a
   `wait_for` on a `*_terminate_timeout_seconds` setting.

All three stop at the boundary of those four tables. They do **not** cover
claim-analysis, phase-0 setup, or the pre-launch prefix inside each of the four
runners themselves. §3 inventories that.

## 3. Live-work inventory

| workload | claim filters live ws? | cancelled by delete txn? | cancellable in-process? | this slice |
|---|---|---|---|---|
| turns (SDK) | n/a | yes | yes (Slice B registry) | unchanged |
| collab rooms | n/a | n/a | yes (Slice B manager) | unchanged |
| coherence / evaluator / review / uncited — **launch+drive** | yes | yes | **no** | §4 adds it |
| the same four — **pre-launch prefix** | yes at claim time only | yes | no | §5 guards it |
| claim-analysis (`analysis/queue.py:162`) | **no** | **no** | **no — sync, APScheduler threadpool** | §6 |
| phase-0 setup (`phases/setup.py:123`) | **no** | **no** | no | §6 |

The claim-analysis worker is the sharpest correction to the first draft: it
claims on `status == "queued"` alone, reads workspace files, performs LLM work,
and runs **synchronously** in APScheduler's thread pool via
`_analysis_drain_tick` (`scheduler.py:989`). An asyncio task registry cannot
cancel it at all. Phase-0 setup is worse in kind: `_snapshot_and_init_repo`
does `repo_dir.mkdir(parents=True, exist_ok=True)` (`setup.py:226`), so a setup
job running after a delete **recreates the directory tree that
`scrub_workspace_bytes` just removed** — a resurrection, not merely a leak.

## 4. Cancelling the in-flight pass

### 4.1 The registry

A new module `src/call_manager/proposal_assist/passes.py` holds a
process-global `PASS_REGISTRY`, mirroring how collab exposes `MANAGER`:

- `track(workspace_id)` — an async context manager registering
  `asyncio.current_task()` in a `dict[uuid.UUID, set[Entry]]`, discarding it in
  a `finally` so a cancelled pass still deregisters. Registration via
  `current_task()` avoids threading a handle from the scheduler to the runner.
  It yields an `Entry` on which the runner calls `entry.bind(adapter, handle)`
  immediately after `adapter.launch(...)` returns.
- `retire_workspace(workspace_id)` — see §4.2.

No in-memory tombstone set: §2.1 already provides the "refuse to claim" gate
durably in SQL, and a second copy would be a second source of truth.

### 4.2 Awaiting *teardown*, not task exit — the review's High finding

The first draft cancelled the task and awaited it for 2 seconds, then declared
teardown complete. That is wrong twice over:

- `ClaudeAgentSDKAdapter.terminate` routes to `_retire_generation`, which does
  `await asyncio.shield(record.teardown)` (`claude_adapter.py:562`). Under
  cancellation the shield delivers `CancelledError` to the *awaiter* while the
  teardown task **keeps running independently**. The runner's `finally` can
  therefore complete — and the task exit — with the subprocess still alive.
- The adapter's own budget is `disconnect_deadline_seconds=3.0` +
  `reap_deadline_seconds=2.0` = **5.0s** (`claude_adapter.py:167`), well over
  the proposed 2.0s.

So a test asserting "terminate was called" proves nothing. `retire_workspace`
must await teardown *completion*:

For each entry, `task.cancel()`, then await **concurrently**:

- the task's exit, bounded `_PASS_CANCEL_SECONDS = 1.0`; and
- `adapter.terminate(handle)`, bounded `_PASS_TEARDOWN_SECONDS = 5.0`.

Concurrently, not sequentially, and this is safe by design:
`_retire_generation` is documented as "shared by retirement, capacity eviction,
idle sweep and reconciliation, so concurrent callers never spawn a second kill
for the same generation" — it marks retiring, reuses the single teardown task,
and awaits it. A second caller therefore *joins* the same teardown rather than
racing it. Because the registry's await is **not** cancelled, it observes the
shielded task to completion.

Hook worst case is `max(1.0, 5.0) = 5.0`, not their sum.

### 4.3 Wrap points

All four runners establish `workspace_id` in a short opening session
(`coherence:50`, `evaluator:57`, `review:200`, `uncited_claims:84`). Everything
from the adapter build onward is wrapped in
`async with PASS_REGISTRY.track(workspace_id) as entry:`, with
`entry.bind(adapter, handle)` on the line after launch. The prefix that
precedes it is handled separately in §5 — it is *not* left uncovered.

Wrapping inside the runner rather than the tick covers every caller, not only
the scheduler path.

**Review's persona fan-out needs no special handling.** `run_review_pass`
dispatches `_run_one_call` children through an `asyncio.Semaphore` and awaits
`asyncio.gather`; cancelling the parent cancels the gather, which cancels each
child, and each child's `finally` terminates its own handle. The registry's
concurrent `terminate` covers the parent's handle; children are bound by the
same generation.

### 4.4 Why cancellation reaches the `finally` at all

`asyncio.CancelledError` inherits directly from `BaseException`, so the
runners' broad `except Exception` does not swallow it: the `finally` runs and
the cancellation propagates. **No change to any runner's error handling.** The
failure path that `except Exception` normally takes — re-open a session,
`_fail` the job, supersede partial rows — is correctly skipped, because the
delete transaction already terminalized that row (§2.2).

### 4.5 One logging wrinkle

`concurrent.futures.CancelledError` is a different class from
`asyncio.CancelledError` and **is** an `Exception` subclass. The scheduler
tick's `fut.result()` sits inside an `except Exception`, so a deliberate
retirement cancellation would log `"coherence tick failed"` with a traceback.
Catch it explicitly at the tick boundary and log it at info level as a
retirement.

### 4.6 Coordinator wiring and barrier budget

`main.py` registers `"passes": PASS_REGISTRY.retire_workspace`, and
`REQUIRED_HOOKS` becomes `("turns", "collab", "passes")` so dropping the wiring
fails at boot.

Hooks run concurrently, so the budget is the max, not the sum:

| hook | worst case |
|---|---|
| collab | 3.0s (max over two concurrent rooms: 1.0 lock + 2.0 drain) |
| turns | 5.0s (3.0 disconnect + 2.0 reap) |
| passes | 5.0s (`max(_PASS_CANCEL_SECONDS, _PASS_TEARDOWN_SECONDS)`) |

`max(3.0, 5.0, 5.0) = 5.0 < 6.0` — **the barrier constant does not change**,
because §4.2 awaits the two sub-phases concurrently. Had they been sequential
the hook would cost 6.0 and the barrier would have had to rise to 7.0, widening
the DELETE route's worst-case block. `test_the_barrier_exceeds_every_hooks_own_worst_case`
is extended to read both new constants.

The comment block at `retirement.py:35` is corrected in the same edit — it
enumerates only two hooks, one of the unverified minor findings from the
B-retire review.

## 5. The pre-launch prefix

Contrary to the first draft's claim that the uncovered prefix does "no
subprocess, no LLM call, no file access", every runner's prefix reads workspace
files through `assemble_*_context(..., workspaces_dir=...)` — coherence's
docstring says so explicitly (`coherence/pass_runner.py:38`) — and three of the
four commit durable writes before any tracking point exists:

- evaluator commits `evaluated_draft_sha`, `evaluated_spec_sha`,
  `evaluated_rubric_updated_at`;
- review inserts `ReviewRunItem` rows and commits `run.draft_sha`;
- uncited-claims commits `evaluated_draft_sha`.

A delete can commit while the prefix is mid-flight, so those writes can land on
an already-deleted workspace.

**Guard:** each prefix loads the workspace with `with_for_update=True` and
aborts if `soft_deleted_at` is set, holding that row lock through its commit.
This is a genuine linearization point without touching the advisory mutex: the
delete transaction's `ws.soft_deleted_at = ...` takes the same row lock, so the
two orders are the only two outcomes — the prefix commits and the delete waits
behind it, or the delete wins and the prefix aborts having written nothing. It
follows the `with_for_update=True` idiom the runners already use when
finalizing job rows, and generalizes the pre-launch live recheck
`uncited_claims/pass_runner.py` already performs inside `_run`.

## 6. Claim-analysis and phase-0 setup

Both get the durable half of the same treatment; neither gets in-process
cancellation, and §9 records why.

**Claim-analysis** (`analysis/queue.py:162`):
- add the `live_ws` filter to `claim_queued_jobs`, matching the other four;
- cancel active claim-analysis jobs in the delete transaction, alongside the
  existing four `cancel_*_for_workspace` calls;
- abort cooperatively at the existing `touch_running_job` checkpoint, which the
  worker already calls between LLM calls — re-read liveness there and stop.

It cannot be cancelled mid-LLM-call: it is sync code in APScheduler's thread
pool with no cancellation primitive. The residual is therefore bounded by one
LLM call rather than by the whole job.

**Phase-0 setup** (`phases/setup.py:123`):
- add the `live_ws` filter to `claim_next_setup_job`;
- cancel active setup jobs in the delete transaction;
- re-check liveness under the workspace row lock immediately before
  `_snapshot_and_init_repo` writes, so a job already past its claim cannot
  recreate a scrubbed tree.

The last point is the one that matters: without it, delete → scrub → setup
resurrects the repo directory.

## 7. Removing the `soft_delete_workspace` foot-gun

`workspaces.py:213` still exports the pre-split wrapper that runs the DB half
then the FS half. It has **zero production callers** (the route calls the two
halves directly so it can interleave retirement between them) and about nine
test call sites.

It is a foot-gun, not merely dead code: it is the obvious-looking "delete a
workspace" entry point and it skips retirement entirely, so a future caller
reaching for it silently reintroduces the bug Slice B-retire exists to fix.

Delete it and move the composition into a helper under `tests/`. Coverage
migrates rather than being dropped — the lesson from B-retire's sweep. The
production surface then offers only `soft_delete_workspace_db` and
`scrub_workspace_bytes`, whose names promise nothing about retirement.

## 8. Writers under the live mutex

Five owner-write routes mutate workspace-scoped state without the commit mutex
and are brought under `acquire_commit_mutex_live(session, ws.id)`, following
the precedent at `web/api/workspaces.py:537`:

| route | file:line | why |
|---|---|---|
| `update_settings` | `settings.py:69` | mutates title/status/deadline/citation_style |
| `add_member` | `settings.py:97` | inserts `WorkspaceViewer` + audit event |
| `remove_member` | `settings.py:135` | deletes `WorkspaceViewer` + audit event |
| `update_rubric` | `rubric.py:148` | mutates rubric fields on `Workspace` |
| `activate` | `rubric.py:206` | mutates rubric fields + phase transition |

`retry_setup` (`rubric.py:168`) is included for a second reason: it re-queues a
setup job, and §6 makes setup a resurrection risk. Re-queueing on a deleted
workspace must 404, not enqueue.

**Error mapping must be written per route, explicitly.** There is no app-level
`CommitMutexTimeout` handler — every route that returns 429 today does so with
its own `except CommitMutexTimeout` (e.g. `web/api/workspaces.py:446`), and an
uncaught one falls through to the generic 500 handler (`main.py:566`). So each
of the six routes gets:

- `except CommitMutexTimeout` → 429 with `BUSY_ERROR_MESSAGE` and
  `Retry-After: 2`, matching the existing shape;
- `WorkspaceDeleted` → left to propagate to main.py's app-level handler → 404
  `workspace_deleted`.

**The claim is narrowed.** The first draft asserted that this makes "every
workspace writer linearized against delete" universally true. It does not: a
file-level survey shows twenty modules with mutating routes that never call
`acquire_commit_mutex`, and many of them are linearized only indirectly (they
route through the collab kernel or `save_section`, which take the mutex
themselves) while others may not be linearized at all. Distinguishing the two
requires a per-route inventory that is out of scope here. The accurate claim is
narrower: **the six routes enumerated above are linearized.** §9 keeps the rest
open.

## 9. Open residuals after this slice

Stated plainly, because the first draft's "single remaining gap" was wrong:

1. **Claim-analysis cannot be cancelled mid-LLM-call.** Sync code in a thread
   pool. Bounded to one LLM call by the cooperative checkpoint (§6). Closing it
   properly means moving the worker onto the loop or adding a real abort
   primitive — its own slice.
2. **A full per-route writer inventory is not done.** Six routes are
   linearized (§8); the rest of the mutating surface is uncategorized.
3. **The bind window.** If retirement lands between `adapter.launch` returning
   and `entry.bind(...)` executing, the registry's `terminate` is a no-op and
   the subprocess falls to the reconciliation sweep — the same degrade-to-
   eventual path as a barrier timeout.
4. **The conceded in-flight-query race** is unchanged: a delete committing
   after a check can still meet a live streaming call, and the reconciliation
   sweep remains the backstop, as documented on `WorkspaceRetired`.
5. **Two unverified minor findings** from the B-retire review (fixed-sleep
   races in two collab tests) stay out of scope.

## 10. Non-goals

- No new in-memory tombstone for passes (§2.1 covers it durably).
- No change to any pass's own timeout or terminate constants.
- No change to `_RETIREMENT_BARRIER_SECONDS`.
- No schema migration.

## 11. Testing and acceptance

Acceptance is by **mutation**, per the lesson from B-retire's post-merge
review: layer-by-layer unit tests do not prove wiring, so each gate must be
shown to fail when its fix is reverted.

1. **Hook wiring, end-to-end.** Replacing `main.py`'s `"passes"` hook with a
   no-op must fail a test driving the real DELETE route against a live
   registered pass. This is the mutation that went undetected for turns.
2. **Scrub waits for teardown completion.** With an adapter whose teardown
   takes longer than the task's exit, assert `scrub_workspace_bytes` has not
   been entered until teardown *finishes* — not merely that `terminate` was
   called. This is the gate the first draft would have passed while broken.
3. **Retirement arriving during `terminate()`.** A delete landing while the
   runner is already inside its own `finally` must still end with the
   subprocess confirmed dead, exercising the shared-generation join in
   `_retire_generation`.
4. **Deregistration.** Normal completion, raise, and cancellation each leave
   the registry empty for that workspace.
5. **Prefix guard.** A delete committing while a prefix is mid-flight leaves
   zero prefix writes (no `ReviewRunItem` rows, no staleness fields) — proved
   by holding the row lock from the test.
6. **Review fan-out.** Cancelling the parent terminates every child's handle.
7. **Setup cannot resurrect.** delete → scrub → a setup job already past its
   claim must not recreate the repo directory.
8. **Claim-analysis.** The live filter refuses a deleted workspace at claim
   time; the `touch_running_job` checkpoint aborts a running job.
9. **Budget invariant.** The extended barrier test fails if either new
   passes constant is raised past the headroom.
10. **Route straddles.** Each of the six routes: 404 with nothing written
    against a workspace deleted under the mutex; 429 with `Retry-After: 2`
    under a wedged mutex.

## 12. Risks

- **Cancelling the scheduler's tick task.** Each drain tick claims exactly one
  job and awaits it inline, so the registered task does nothing but this pass.
  Tests must pin this, because a tick that later batches jobs would silently
  widen the blast radius.
- **A pass that swallows cancellation.** Nothing does today (§4.4), but a
  future `except BaseException` in a runner would defeat the mechanism
  silently. Gate 2 catches it.
- **Prefix row-lock contention.** The prefix now holds a `FOR UPDATE` lock on
  the workspace row across context assembly, which reads files. If assembly is
  slow, a concurrent delete blocks behind it. The prefix is short by
  construction, but a test should pin the lock's duration rather than assume
  it.

## 13. Footprint

One new module; one wrapping line plus one bind line in each of four runners;
a row-lock guard in each of four prefixes; one explicit catch at the tick
boundary; two claim-query filters and two delete-transaction cancels; one
cooperative checkpoint; six route changes; one hook registration; two constants
added to an existing test; the wrapper deletion plus its test helper; and the
`retirement.py:35` comment correction. No schema migration.
