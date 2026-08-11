# Workspace retirement — Slice C design

Date: 2026-08-11
Status: approved (design); implementation plan to follow
Predecessors: Slice A (`docs/superpowers/plans/2026-07-24-workspace-retirement-slice-a.md`, `7b16547`), Slice B-mutex (`docs/superpowers/plans/2026-07-24-workspace-retirement-slice-b-mutex.md`, `194d8ce`), Slice B-retire (`docs/superpowers/plans/2026-07-25-workspace-retirement-b-retire.md`, `245d7ba`, review fixes `01edf10`)

## 1. Why this slice exists

Slices A and B made deletion durable and linearized it against the two
subsystems that hold live per-workspace state: turns (SDK subprocesses) and
collab (Yjs rooms). Two named residuals were carried forward:

**(a) The analysis passes.** Four background passes — coherence, evaluator,
review, uncited-claims — drive their own LLM turns through a dedicated one-shot
adapter. Slice A's inventory covers SDK *clients* owned by the turns registry;
these passes launch their handles inline inside a scheduler tick coroutine that
nothing tracks. They are the only remaining live per-workspace state a delete
does not reach.

**(b) The settings writers.** `web/api/settings.py` mutates workspace rows
without taking the commit mutex, so those writes are not linearized against
delete. Slice B accepted this as a documented residual pending a decision.

## 2. What already exists (scope reduction)

Exploration shrank part (a) to roughly a third of its assumed size. Three
guarantees are already in place and need no new work:

1. **No new pass can start on a deleted workspace.** All four claim queries
   (`{coherence,evaluator,review,uncited_claims}/jobs.py`) join against
   `live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))`.
   This is a durable SQL gate: it survives process restart and needs no
   in-process tombstone.
2. **Job rows are already terminalized by the delete transaction.**
   `soft_delete_workspace_db` calls `cancel_coherence_jobs_for_workspace`,
   `cancel_evaluator_jobs_for_workspace`, `cancel_review_runs_for_workspace`
   and `cancel_uncited_claims_jobs_for_workspace`, each an atomic
   `UPDATE … WHERE status IN ('queued','running') → 'failed'`, inside the same
   transaction as the soft-delete stamp.
3. **Each runner already cleans up its own handle.** Every pass runner holds
   its `adapter.terminate(handle)` in a `finally`, itself bounded by a
   `wait_for` on a `*_terminate_timeout_seconds` setting.

**The single remaining gap:** the in-flight *asyncio task* keeps streaming its
LLM turn after the delete commits. Its job row already says `failed`, but the
subprocess is alive, tokens are being spent, and `scrub_workspace_bytes` may
delete files underneath a pass that is still reading them.

## 3. Part (a) — cancel the in-flight pass

### 3.1 The registry

A new module `src/call_manager/proposal_assist/passes.py` holds a
process-global `PASS_REGISTRY`, deliberately mirroring how collab exposes
`MANAGER` and turns exposes the controller's `registry`:

- `track(workspace_id)` — an async context manager that registers
  `asyncio.current_task()` in a `dict[uuid.UUID, set[asyncio.Task]]` on entry
  and discards it on exit (in a `finally`, so a cancelled pass still
  deregisters). Registration by `current_task()` means no plumbing has to be
  threaded from the scheduler down to the runner.
- `retire_workspace(workspace_id)` — cancels every task registered for that
  workspace, then awaits them under a bound (`_PASS_CANCEL_SECONDS = 2.0`) via
  `asyncio.wait(..., timeout=...)`. Awaiting (rather than fire-and-forget
  cancelling) is what makes the subprocess dead *before* the route proceeds to
  `scrub_workspace_bytes`. Returns a count, for testability, which the
  coordinator discards — matching `RoomManager.retire_workspace`.

No tombstone set is needed. Unlike turns and collab, nothing here needs a
"refuse to create new state" flag: finding 2.1 already provides that gate
durably in SQL, and duplicating it in memory would be a second source of truth.

### 3.2 Wrap points

All four runners establish `workspace_id` inside a short opening session read
(`coherence:50`, `evaluator:57`, `review:200`, `uncited_claims:84`). The
remainder of each function body — adapter build, launch, streaming, finalize —
is wrapped in `async with PASS_REGISTRY.track(workspace_id):`.

The uncovered prefix is that opening read: no subprocess, no LLM call, no file
access. Cancelling it would buy nothing.

Wrapping inside the *runner* rather than the *tick* is deliberate: it covers
every caller of the runner, not just the scheduler path.

**Review's persona fan-out needs no special handling.** `run_review_pass`
dispatches `_run_one_call` children through an `asyncio.Semaphore` and awaits
`asyncio.gather`. Cancelling the parent task cancels the gather, which cancels
each child, and each child's own `finally` terminates its handle. Registering
the parent is sufficient.

### 3.3 Why cancellation cleans up correctly

Verified against the runtime, because the whole design rests on it:

- `asyncio.CancelledError` inherits directly from `BaseException`. The runners'
  broad `except Exception` therefore does **not** swallow it: the `finally`
  runs (terminating the handle under its own bounded `wait_for`) and the
  cancellation propagates. **No change to any runner's error handling.**
- The failure path that `except Exception` normally takes — re-open a session,
  `_fail` the job, supersede partial rows — is correctly skipped, because the
  delete transaction has already terminalized that row (finding 2.2). A
  cancelled pass must not re-touch a deleted workspace's rows.

### 3.4 One logging wrinkle

`concurrent.futures.CancelledError` is a *different class* from
`asyncio.CancelledError` and, in the stdlib, **is** an `Exception` subclass. The
scheduler tick's `fut.result()` sits inside an `except Exception`, so a
deliberate retirement cancellation would be logged as `"coherence tick failed"`
with a traceback — misleading operators into chasing a non-failure.

Fix: catch `concurrent.futures.CancelledError` explicitly at the tick boundary
and log it at info level as a retirement, not an error.

### 3.5 Coordinator wiring

`main.py` registers a third hook, `"passes": PASS_REGISTRY.retire_workspace`,
and `REQUIRED_HOOKS` becomes `("turns", "collab", "passes")` — so a future
refactor that drops the wiring fails at boot rather than silently never
stopping passes again.

### 3.6 Barrier budget

Hooks run concurrently, so the budget is the max, not the sum:

| hook | worst case |
|---|---|
| collab | 3.0s (max over two concurrent rooms: 1.0 lock + 2.0 drain) |
| turns | 5.0s (3.0 disconnect + 2.0 reap) |
| passes | 2.0s (`_PASS_CANCEL_SECONDS`) |

`max(3.0, 5.0, 2.0) = 5.0 < 6.0` — **the barrier constant does not change.**
`test_the_barrier_exceeds_every_hooks_own_worst_case` is extended to read the
new constant, so raising `_PASS_CANCEL_SECONDS` past the headroom fails loudly.

The comment block at `retirement.py:35` is updated in the same edit: it
currently enumerates only two hooks, and one of the four unverified minor
review findings was that it overstates its coverage.

## 4. Part (b) — settings writers under the live mutex

`update_settings`, `add_member` and `remove_member` each take
`acquire_commit_mutex_live(session, ws.id)` before their writes, following the
existing precedent in `web/api/workspaces.py:537`.

Note the brainstorm scoped this to two routes; `remove_member`
(`settings.py:135`) is the same class of writer, and excluding it would leave
the invariant false. All three are included.

Error mapping reuses what already exists — no new response shapes:

- `WorkspaceDeleted` → main.py's app-level handler → 404 with code
  `workspace_deleted`.
- `CommitMutexTimeout` → the house 429 with `Retry-After: 2`.

This makes **"every workspace writer is linearized against delete"**
universally true, and retires the residual rather than documenting it further.

## 5. Removing the `soft_delete_workspace` foot-gun

`workspaces.py:213` still exports `soft_delete_workspace(session, …)` — the
pre-split wrapper that runs the DB half then the FS half. It has **zero
production callers** (the route calls the two halves directly so it can
interleave retirement between them) and about nine test call sites.

It is a foot-gun rather than merely dead code: it is the obvious-looking
"delete a workspace" entry point, and it skips retirement entirely. A future
caller reaching for it would silently reintroduce exactly the bug Slice
B-retire exists to fix.

Decision: delete it, and move the composition into a helper under `tests/` for
the nine call sites. Coverage migrates rather than being dropped — the lesson
from B-retire's sweep, where a test asserting a removed call was nearly
deleted instead of rewritten. The production surface then offers only
`soft_delete_workspace_db` and `scrub_workspace_bytes`, whose names do not
promise retirement.

## 6. Non-goals

- No new tombstone/refusal set for passes (2.1 covers it durably).
- No change to any pass's own timeout or terminate constants.
- No change to `_RETIREMENT_BARRIER_SECONDS`.
- No attempt to close the conceded in-flight-query race: a delete committing
  after a check can still meet a live streaming call. The reconciliation sweep
  remains the backstop, as documented on `WorkspaceRetired`.
- The remaining two unverified minor review findings (fixed-sleep races in two
  collab tests) are out of scope; the dead `soft_delete_workspace` wrapper and
  the `retirement.py:35` comment are folded in here.

## 7. Testing and acceptance

Acceptance is by **mutation**, per the lesson from Slice B-retire's post-merge
review: layer-by-layer unit tests do not prove wiring, so each new gate must be
shown to fail when its fix is reverted.

Required gates:

1. **Hook wiring, end-to-end.** Replacing `main.py`'s `"passes"` hook with a
   no-op must fail a test that drives the real DELETE route against a live
   registered pass task. This is the mutation that went undetected for turns.
2. **Cancellation terminates the handle.** With a fake adapter recording
   `terminate` calls, retiring mid-pass must record the terminate — proving the
   `finally` ran, not merely that the task ended.
3. **Deregistration.** A pass that completes normally, one that raises, and one
   that is cancelled all leave the registry empty for that workspace.
4. **Straddle.** Delete landing mid-pass: task cancelled, handle terminated,
   job row already `failed` (by the delete txn, not by the pass's own failure
   path), and the pass does not resurrect any row.
5. **Review fan-out.** Cancelling the parent terminates every child's handle.
6. **Budget invariant.** The extended barrier test fails if
   `_PASS_CANCEL_SECONDS` is raised past the headroom.
7. **Settings straddle.** Each of the three routes, against a workspace deleted
   while the mutex is held, returns 404 and writes nothing; under a wedged
   mutex, 429 with `Retry-After: 2`.

## 8. Risks

- **Cancelling the scheduler's tick task.** Each drain tick claims exactly one
  job and awaits it inline, so the registered task is doing nothing but this
  pass — cancelling it cancels only that pass. Tests must pin this, because a
  tick that later batches multiple jobs would silently widen the blast radius.
- **A pass that swallows cancellation.** Nothing does today (3.3), but a future
  `except BaseException` in a runner would defeat the mechanism silently. Gate
  2 catches it.

## 9. Footprint

One new module, one wrapping line in each of four runners, one explicit catch
at the tick boundary, three route changes, one hook-registration line, one
constant added to an existing test, the wrapper deletion plus its test helper,
and the `retirement.py:35` comment correction. No schema migration.
