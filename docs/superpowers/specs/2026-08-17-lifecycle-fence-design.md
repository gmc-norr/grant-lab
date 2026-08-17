# Total Lifecycle Fence — design

**Status:** design agreed 2026-08-17. Step 1 of a four-step architectural
sequence (fence → prove → remove → relax). This document specifies step 1 only.

**Goal:** make Postgres the single authority on workspace liveness, by making
every effect in the system unable to take place on a stale belief about that
liveness. Nothing is removed in step 1. The system runs belt-and-braces
afterwards; that is the point.

---

## 1. The problem this exists to fix

Workspace liveness is currently decided in two places:

- **Postgres** — `workspaces.soft_deleted_at`, plus `workspaces.lifecycle_generation`
  (added in Slice C0, migration 0062).
- **In-process** — `WorkspaceLifecycle` (`proposal_assist/lifecycle.py`), whose
  `is_retired()` gates room hydration, room operations, and the drafting registry.

Two authorities means no authority. Everything expensive in the retention work so
far is compensation for the gap between them: the generation mechanism, the 6-second
restore/retirement barriers, the 503-after-commit contract, `reconcile_cancelled_jobs`,
cancel-on-delete across six writers, `cancelled_from`, and the interleaving-test
matrix that proves the compensations work.

### 1.1 The current design pays for a promise it does not keep

The synchronous contract — "when the route answers, the database and in-process
state have converged" — is bounded at `_RETIREMENT_BARRIER_SECONDS = 6.0` and
`_RESTORE_BARRIER_SECONDS = 6.0`. Past that bound the route answers 503 with the
database already committed and in-process state behind. Worse, on the restore
retry path the workspace is by then already live, so the route takes the
already-live branch and answers **204 regardless of whether the in-memory update
succeeded** (`web/api/admin_retention.py`, the `live_gen is not None` branch).

So the system already has eventual consistency. It is paying
synchronous-coordination costs for it and documenting it as a guarantee.

### 1.2 Shipping restore raised the requirement everywhere, and we only upgraded C1

Before Slice C1, deletion was terminal. "Still live" therefore implied "same
lifecycle epoch", and a liveness check was a sufficient fence.

Restore breaks that implication. A worker that formed a belief at generation *G*
can now survive a delete **and** a restore, observe `soft_deleted_at IS NULL` at
commit time, and commit work belonging to an epoch the workspace has left.

That defect is not currently reachable, because cancel-on-delete terminalizes the
job first and the worker's `job.status != "running"` check stops it. This is the
sequencing argument in miniature: **the compensating machinery is load-bearing
precisely because the fence is only a liveness fence.** Removing cancel-on-delete
before the fence is a generation fence would open a real defect.

---

## 2. The invariant

> In-process and in-memory state may be arbitrarily stale. It must never be
> **trusted**. Every effect is fenced, at the moment it takes effect, against
> durable state in Postgres.

An **effect** is anything the outside world can observe: a database commit, a git
commit, bytes written to the filesystem, a token spent with a model provider, or
content served to a user.

Two fence kinds follow, and the distinction is the whole design:

| | Question asked | Correct for |
|---|---|---|
| **Liveness fence** | Is this workspace live *right now*, under the mutex? | Actors with **no prior belief** — a request that forms its belief and acts on it inside one transaction. |
| **Generation fence** | Is this workspace still at generation *G*, under the mutex? | Actors that **formed a belief earlier** and are acting on it later. |

The generation fence is strictly stronger: it rejects a delete→restore→delete
cycle that a liveness check waves through.

### 2.1 What is already correct

**The liveness fence is already total.** Every mutex-held write in the system goes
through `acquire_commit_mutex_live` (`proposal_assist/drafting/locking.py`), which
re-reads `soft_deleted_at` after winning the mutex and raises `WorkspaceDeleted`.
Verified 2026-08-17: 21 direct call sites plus 7 more behind `workspace_write()`
(`proposal_assist/web/_writes.py`).

Route handlers, `editing/save.py`, `citations/human_cite.py`,
`tools/citations.py`, `analysis/insert.py` and the socket-driven
`collab/kernel.py` writes are therefore **already correct and are not changed by
this spec.** Their belief is formed and acted on inside one transaction; liveness
is exactly the right question for them.

This is why step 1 is far smaller than a "fence every site" reading suggests. The
work is to upgrade the belief-carrying minority.

---

## 3. Mechanism

### 3.1 `acquire_commit_mutex_at_generation`

A third mutex variant beside `acquire_commit_mutex` and `acquire_commit_mutex_live`:

```python
def acquire_commit_mutex_at_generation(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    generation: int,
    timeout_seconds: int = 10,
) -> None:
    """Acquire the commit mutex, then re-read BOTH soft_deleted_at and
    lifecycle_generation in ONE statement under that mutex.

    Raises WorkspaceDeleted if the workspace is not live, and
    WorkspaceGenerationStale if it is live but has moved past *generation*.
    """
```

A **new function**, not an optional parameter on `_live`. Three reasons:

1. It is greppable, so a lint gate can assert that every belief-carrying site
   uses it (see §6).
2. `web/_writes.py` documents a live trap: a call-time keyword **overrides** a
   `functools.partial` keyword, which is how the straddle tests shorten the mutex
   wait. An optional `expect_generation=` on `_live` sits in that same blast radius.
3. The two exceptions map to different caller behaviour, and a separate function
   makes that visible at the call site.

Both columns must come from one statement, for the reason
`restore_workspace_db` was fixed in `701fbb7`: an instance loaded before the mutex
carries a pre-mutex generation.

### 3.2 `WorkspaceGenerationStale`

A new exception, distinct from `WorkspaceDeleted` and `WorkspaceRetired`.

It is **not** an error condition. It means "the epoch you belong to is over;
abandon quietly." Background workers catch it, abandon the pass without writing,
and do not count an attempt or dead-letter the job — the same shape as Task 2's
`_fail` fresh-read fix. It never reaches a user as a 5xx.

`WorkspaceDeleted` keeps its existing meaning and its existing 404 mapping.

### 3.3 Carrying the generation

**Database-backed actors** record the generation on their own row at claim time,
in the claiming transaction. Eight workspace-scoped job tables, verified against
`db/models.py` on 2026-08-17:

| Model | Table |
|---|---|
| `TurnJob` | `turn_jobs` |
| `CoherenceJob` | `coherence_jobs` |
| `EvaluatorJob` | `evaluator_jobs` |
| `UncitedClaimsJob` | `uncited_claims_jobs` |
| `AttachmentJob` | `attachment_jobs` |
| `WorkspaceSetupJob` | `workspace_setup_jobs` |
| `ReviewRun` | `review_runs` |
| `ClaimAnalysisJob` | `claim_analysis_jobs` |

Each gains `claimed_at_generation INTEGER NULL`. Nullable, because rows already in
flight when the migration lands have no generation to record. A NULL is treated as
"unfenced, legacy" and falls back to the liveness fence — this is the only place
in the design where the weaker check survives, it applies to a bounded set of
pre-existing rows, and it disappears as those rows terminalize.

The claim statement must set it in the **same** statement that flips the row to
`running`, read from `workspaces` via a subquery — never read into Python first,
for the reason `bump_lifecycle_generation` documents.

**In-memory actors** (collab rooms) carry the generation on the `Room` object, set
at hydration from the same transaction that loads the update log.

### 3.4 Read fencing

Decided 2026-08-17: **reads are fenced.** A room whose generation is stale must
refuse to serve, not merely refuse to commit.

Consequences accepted with that decision:
- Deletion means an open editor goes dark rather than lingering until the
  evictor runs.
- The read path takes a generation check. It must not take the commit mutex —
  reads are frequent — so it compares against a value refreshed by the same
  mechanism that drives eviction, and treats "unknown" as stale.

---

## 4. Site inventory

Every site below is **belief-carrying**: it forms a belief at one moment and
produces an effect at a later one.

### 4.1 Background job families (6)

Each has a claim function and a pass runner that commits much later, after an LLM
call:

| Family | Claim | Commit |
|---|---|---|
| Coherence | `coherence/jobs.py:64` | `coherence/pass_runner.py` |
| Evaluator | `evaluator/jobs.py:56` | `evaluator/pass_runner.py` |
| Uncited claims | `uncited_claims/jobs.py:59` | `uncited_claims/pass_runner.py` |
| Review | `review/jobs.py:57` | `review/pass_runner.py` |
| Claim analysis | `analysis/queue.py:179` | `analysis/pipeline.py` |
| Setup (Phase 0) | `phases/setup.py:279` | `phases/setup.py` milestones |

Claim records the generation; every milestone commit uses
`acquire_commit_mutex_at_generation`.

Setup already uses `acquire_commit_mutex_live` at `phases/setup.py:363` — that
call becomes the generation variant, and `_m3`'s M→J→W ordering is preserved
exactly.

### 4.2 Turn jobs

`drafting/registry.py` gates launches on `is_retired()` at two sites (`:129`,
`:321`) — an in-process authority consulted as truth. `TurnJob` records the
generation at claim; the finalize path fences on it. The `is_retired()` checks
**stay** in step 1 (nothing is removed) but stop being load-bearing.

### 4.3 Collab rooms

Included in step 1 by decision on 2026-08-17.

`collab/room.py` currently carries **six** `is_retired()` checks (`:498`, `:513`,
`:526`, `:548`, `:563`, `:577`), each defending a different interleaving window
around hydration and the out-of-lock `await load()`. They exist because an
in-memory flag is being used as authority and the windows are genuinely racy.

Under the fence, a stale room cannot commit — so the windows stop being
correctness-critical. Step 1 does not delete any of the six. It adds:

- generation captured on `Room` at hydration, from the loading transaction;
- `collab/kernel.py`'s five mutex sites fenced on the room's generation rather
  than plain liveness, when the write originates from a room rather than a live
  request;
- `collab/checkpoint.py:91` (tail-bounding, a scheduler job that scanned earlier)
  fenced on the generation observed during the scan — this is a textbook
  belief-carrier, and its current liveness re-check at `:207` is the compensation
  the fence replaces;
- `collab/hydrate.py:58` fenced, so hydration cannot publish a room for an epoch
  that ended during the load;
- the read path per §3.4.

### 4.4 Explicitly out of scope

Not belief-carriers; already correct via the liveness fence:
`web/api/workspaces.py` (3 sites), `web/_writes.py` and its 7 routes,
`citations/human_cite.py`, `tools/citations.py`, `editing/save.py`,
`analysis/insert.py`, `collab/flip.py`, `workspaces.py` delete and restore.

`RankingJob` and `ProfileBootstrapJob` are not workspace-scoped.

---

## 5. What step 1 does not do

Stated explicitly because the sequencing is the safety argument:

- **Removes nothing.** Not cancel-on-delete, not `reconcile_cancelled_jobs`, not
  `cancelled_from`, not the restore mapping, not the barriers, not the
  503-after-commit contract, not one `is_retired()` check. Every existing
  compensation keeps running.
- **Changes no HTTP contract.** No status code, no response body, no header
  moves. The OpenAPI schema is unchanged except for nothing at all.
- **Adds no outbox.** That is step 4.

Step 1 is purely additive, which is what makes it shippable against a production
system with collab live. Each site can land independently and is independently
verifiable.

---

## 6. How step 1 is proven

Two gates, because "we added checks" is not evidence.

**Per-site, at build time.** For each fenced site, a test drives the site with a
stale generation and asserts no effect landed — while the compensation for that
site is disabled in the test. That is the only way to observe the fence rather
than the compensation. This is the same technique that exposed the decorative
tests in C1: if the test passes with the fence removed, it is measuring the
compensation.

**Coverage, mechanically.** A lint/AST gate asserting that every claim function
records a generation, and every commit path for a belief-carrying actor calls
`acquire_commit_mutex_at_generation`. Slice C2 established this pattern after a
correct fix silently disarmed the runtime gate proving the rule it fixed. A
grep-able function name (§3.1) is what makes the gate cheap.

**The redundancy proof (step 2, not step 1)** then becomes possible: disable a
compensation *in production configuration* and demonstrate nothing escapes.
Only after that does step 3 delete it.

---

## 7. Where the principle stops

Two honest limits. Naming them is part of the design.

**`rm -rf` cannot be fenced.** You cannot predicate a directory deletion on a
generation; a holder mid-write is not stopped by a DB predicate. Purge needs real
quiescence, which is what Slice D's filesystem-activity barrier is for. That slice
survives this architecture untouched. **Fence what is frequent; barrier what is
destructive.**

**Spent tokens cannot be un-spent.** A generation fence stops the write, not the
cost. Best-effort cancellation therefore keeps a legitimate role — as an
optimization, moved into step 4's outbox, never as part of the lifecycle
transaction.

---

## 8. Slice breakdown

Each slice is independently shippable and additive.

| Slice | Content |
|---|---|
| **F1** | `acquire_commit_mutex_at_generation`, `WorkspaceGenerationStale`, migration adding `claimed_at_generation` to all 8 job tables. No call sites converted. |
| **F2** | The 5 non-setup background job families: claim records generation, milestones fenced. |
| **F3** | Setup (Phase 0) — separate from F2 because it carries the M→J→W lock order and the `cancelled_from` machinery. |
| **F4** | Turn jobs: `TurnJob` generation at claim, finalize fenced. |
| **F5** | Collab write path: `Room` generation at hydration, `kernel.py` and `hydrate.py` fenced. |
| **F6** | Collab read fencing (§3.4) and `checkpoint.py` tail-bounding. |
| **F7** | The coverage gate (§6) — last, so it has something to assert about. |

F1 is the only slice with a migration.

---

## 9. Open items

- The exact refresh mechanism for the room read-side generation (§3.4) is
  deliberately unresolved here; it is F6's design question and depends on whether
  the evictor already provides a suitable signal.
- Whether `AttachmentJob` has a belief-carrying commit path at all, or only short
  transactions, is unverified. If short, it needs the column for uniformity but
  no fenced commit site.
