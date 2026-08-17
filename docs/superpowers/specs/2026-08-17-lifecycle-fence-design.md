# Epoch-Fenced Semantic Effects — design

**Status:** revision 2, 2026-08-17. Revision 1 was reviewed and found incomplete;
the effect inventory was built from the wrong audit unit and four further defects
were identified. See §11 for the changelog.

**Step 1 of a four-step sequence** (fence → prove → remove → relax). This
document specifies step 1 only.

**Goal:** make Postgres the single authority on workspace liveness, by making
every *semantic* effect unable to take place on a stale belief about it. Nothing
is removed in step 1.

---

## 1. The problem this exists to fix

Workspace liveness is currently decided in two places:

- **Postgres** — `workspaces.soft_deleted_at` and `workspaces.lifecycle_generation`
  (Slice C0, migration 0062).
- **In-process** — `WorkspaceLifecycle` (`proposal_assist/lifecycle.py`), whose
  `is_retired()` gates room hydration, room operations, and the drafting registry.

Two authorities means no authority. Everything expensive in the retention work so
far is compensation for the gap: the generation mechanism, the 6-second barriers,
the 503-after-commit contract, `reconcile_cancelled_jobs`, cancel-on-delete across
six writers, `cancelled_from`, and the interleaving-test matrix proving the
compensations work.

### 1.1 The current design pays for a promise it does not keep

The synchronous contract — "when the route answers, database and in-process state
have converged" — is bounded at `_RETIREMENT_BARRIER_SECONDS = 6.0` and
`_RESTORE_BARRIER_SECONDS = 6.0`. Past that bound the route answers 503 with the
database already committed. And on the restore retry path the workspace is by then
already live, so the route takes the already-live branch and answers **204
regardless of whether the in-memory update succeeded**
(`web/api/admin_retention.py`, the `live_gen is not None` branch).

The system already has eventual consistency. It is paying
synchronous-coordination costs for it and documenting it as a guarantee.

### 1.2 Restore raised the requirement everywhere; only C1's own sites were upgraded

Before Slice C1, deletion was terminal. "Still live" therefore implied "same
lifecycle epoch", and a liveness check was a sufficient fence.

Restore breaks that implication. An actor that formed a belief at generation *G*
can now survive a delete **and** a restore, observe `soft_deleted_at IS NULL`, and
produce work belonging to an epoch the workspace has left.

For job *finalizers*, that defect is not currently reachable: cancel-on-delete
terminalizes the job first and the finalizer's `job.status != "running"` check
stops it. This is the sequencing argument in miniature — **the compensating
machinery is load-bearing precisely because the fence is only a liveness fence.**

For effects that occur *during* execution rather than at finalization, it is
reachable today. See §4.1.

---

## 2. The invariant

> A **semantic effect** produced from a belief formed earlier must carry the
> lifecycle generation of that belief and validate it, durably in Postgres, at
> the moment the effect takes place.

Three categories, deliberately separated. Revision 1 conflated them and that was
its central error.

| Category | Examples | Rule |
|---|---|---|
| **Semantic effect** | proposed-change insert, claim classification, artifact/git write, job → `done`, chat message | **Fenced.** Prohibited on a stale generation. |
| **Cleanup / accounting** | token totals, `estimated_dollars`, job → `cancelled`, freeing an active-unique slot | **Permitted** on a stale generation, via conditional (CAS) writes only. |
| **Delivered reads** | bytes already rendered in a browser | **Unrecoverable.** Not a fence problem. §6. |

The generation fence is strictly stronger than a liveness check: it rejects a
delete→restore→delete cycle that liveness waves through.

### 2.1 What is already correct

The **liveness** fence is total for mutex-held writes. Verified 2026-08-17:
17 direct `acquire_commit_mutex_live` call sites, plus the `workspace_write()`
wrapper (`proposal_assist/web/_writes.py:88`) with 6 route callers behind it.
Each re-reads `soft_deleted_at` after winning the mutex and raises
`WorkspaceDeleted`.

Actors with **no prior belief** — a request that forms its belief and acts on it
inside one transaction — are correct as they stand and are not changed by this
spec: `web/api/workspaces.py` (3 sites), the 6 `workspace_write()` routes,
`citations/human_cite.py`, `editing/save.py`, `analysis/insert.py`,
`collab/flip.py`, and `workspaces.py`'s delete and restore.

This is **not** the same as saying those *files* are unchanged. `tools/citations.py`
and `collab/kernel.py` each contain mutex sites that are correct for
request-driven callers and belief-carrying for agent- or room-driven callers. The
unit of audit is the **effect and its originating actor**, never the file and
never the mutex call site. Revision 1 got this wrong in both directions.

---

## 3. Two fence primitives

Forcing every fence through the mutex would introduce contention and — as §5
shows — real deadlocks. Two mechanisms, chosen by what the effect touches.

### 3.1 Primitive A — conditional write (DB-only effects)

For an effect that is purely one or more DB rows, the fence is a predicate on the
**write statement itself**, with no new lock:

```sql
UPDATE/INSERT ... WHERE EXISTS (
    SELECT 1 FROM workspaces
    WHERE id = :ws AND soft_deleted_at IS NULL AND lifecycle_generation = :gen
)
```

One statement. This is the rule established in `28abbc4` and re-proved in
`701fbb7`: a predicate on a candidate SELECT with the write in a separate
statement does not guard the write, because each statement takes a fresh
READ COMMITTED snapshot.

Primitive A adds no lock and therefore **cannot** create a lock-order problem.
It is the default. Prefer it wherever the effect is DB-only.

### 3.2 Primitive B — mutex + generation

For effects that span git, the filesystem, or multiple rows that must move
together:

```python
def acquire_commit_mutex_at_generation(
    session, workspace_id, *, generation: int, timeout_seconds: int = 10
) -> None:
    """Acquire the commit mutex, then re-read soft_deleted_at AND
    lifecycle_generation in ONE statement under that mutex.

    Raises WorkspaceDeleted if not live, WorkspaceGenerationStale if live but
    current_generation != generation.
    """
```

**Equality, not `>`.** A generation lower than expected is impossible under
correct operation; treating it as acceptable would wave through exactly the
corruption the fence exists to detect.

Both columns come from one statement, for the reason `restore_workspace_db` was
fixed in `701fbb7`: an instance loaded before the mutex carries a pre-mutex
generation.

A **new function**, not an optional parameter on `_live`: it is greppable for the
coverage gate (§8), and `web/_writes.py` documents a live trap where a call-time
keyword overrides a `functools.partial` keyword used by the straddle tests.

### 3.3 `WorkspaceGenerationStale`

Distinct from `WorkspaceDeleted` and `WorkspaceRetired`. It is **not** an error
condition: it means "your epoch is over; produce no further semantic effect."
It never reaches a user as a 5xx.

It does **not** mean "write nothing" — see §7.

---

## 4. Effect inventory

Organised by originating actor, not by file.

### 4.1 Effects inside long-running actors, before any finalizer *(new in rev 2)*

This class was **missing from revision 1** and is the largest gap. These effects
commit on their own sessions, during agent execution, with no mutex, no liveness
check, and no reference to the originating job. Fencing the eventual finalizer
cannot retract them.

| Site | Effect |
|---|---|
| `tools/proposal.py` `_propose_artifact_edit_impl` | inserts a pending `ProposedChange` on its own `ctx.session_factory()` session |
| evaluator / review / uncited-claims tools | commit during execution, same shape |
| `analysis/pipeline.py:320` `_upsert_classification` | independent per-sentence commit, one session per sentence |

**Requirement:** the generation travels in `ToolContext`
(`tools/registry.py:43`) and every semantic tool write fences on it — Primitive A
for the DB-only ones.

**Requirement, non-obvious:** `ToolContext` is built once per workspace launch and
**reused across turns**. `registry.py:80` already documents this trap for
provenance: *"Stamped UNCONDITIONALLY every turn because the subprocess (and this
ctx) is reused across turns — a missed stamp would leak the prior turn's
provenance."* The generation has the identical failure mode and must be
re-stamped by the same mechanism (`set_turn_provenance` / the background
`tools_factory`), unconditionally, every turn. A generation stamped once at launch
is a stale belief wearing the fence's uniform.

That this trap is already documented for a neighbouring field is the strongest
available evidence the risk is real rather than theoretical, and it is why the
coverage gate moves earlier (§9).

### 4.2 Background job families (6)

Claim records the generation; semantic milestones fence on it.

| Family | Claim | Semantic commit |
|---|---|---|
| Coherence | `coherence/jobs.py:64` | `coherence/pass_runner.py` |
| Evaluator | `evaluator/jobs.py:56` | `evaluator/pass_runner.py` |
| Uncited claims | `uncited_claims/jobs.py:59` | `uncited_claims/pass_runner.py` |
| Review | `review/jobs.py:57` | `review/pass_runner.py` |
| Claim analysis | `analysis/queue.py:179` | `analysis/pipeline.py` |
| Setup (Phase 0) | `phases/setup.py:279` | `phases/setup.py` milestones |

### 4.3 Turn jobs

`drafting/registry.py` gates launches on `is_retired()` (`:129`, `:321`) — an
in-process authority consulted as truth. `TurnJob` records the generation at
claim; the finalize path fences. The `is_retired()` checks **stay** in step 1 but
stop being load-bearing.

### 4.4 Collab rooms

`collab/room.py` carries **six** `is_retired()` checks (`:498`, `:513`, `:526`,
`:548`, `:563`, `:577`), each defending a different window around hydration and
the out-of-lock `await load()`. They exist because an in-memory flag is being used
as authority and the windows are genuinely racy.

Step 1 deletes none of them. It adds: generation captured on `Room` at hydration
from the loading transaction; `collab/kernel.py`'s five mutex sites fenced on the
room's generation when the write originates from a room rather than a live
request; `collab/hydrate.py:58` fenced so hydration cannot publish a room for an
epoch that ended during the load; `collab/checkpoint.py:91` (tail-bounding — a
scheduler job that scanned earlier, a textbook belief-carrier) fenced on the
generation observed during the scan, replacing its compensating liveness re-check
at `:207`.

Read revocation is **not** in this section. See §6.

### 4.5 Not in scope

`RankingJob` and `ProfileBootstrapJob` are not workspace-scoped.

`AttachmentJob` is **dead schema** — resolved 2026-08-17: no production reference
outside `db/models.py`; the only consumers are model tests. It has no claim path
and no belief-carrying commit, so it is excluded from the migration entirely.
Revision 1 listed it in error.

---

## 5. Lock ordering *(new in rev 2)*

Primitive B acquires a lock and therefore participates in lock ordering.
Inserting it into existing finalizers naively creates real Postgres 40P01
deadlocks:

- `coherence/pass_runner.py:151` takes the **job** row (`with_for_update=True`)
  first. Deletion takes **mutex then job**. Adding M after J gives J→M against
  M→J.
- Pass-runner prefixes already hold the **workspace** row
  (`s.get(Workspace, ..., with_for_update=True)`) before committing. Adding M
  after gives W→M against the delete path's M→W.
- `review/pass_runner.py` documents its own AB-BA history at run-then-item.

**Normative rules:**

1. Where a transaction **already holds the workspace row** under `FOR UPDATE`, do
   not acquire the mutex. Assert the generation **on that already-locked row**.
   The lock that makes the assertion sound is already held; adding M buys nothing
   and inverts an order.
2. Where the effect is DB-only and no workspace row lock is held, use
   Primitive A. No lock, no ordering.
3. Where Primitive B is genuinely required (git / filesystem / multi-row), the
   mutex is acquired **first**, before any job or workspace row lock, preserving
   the established **M → J → W** order (`phases/setup.py:500`,
   `web/api/rubric.py:341`).
4. Never acquire the mutex across context assembly or an LLM call.

Each family's slice must state its ordering explicitly and cite the delete path
it is being ordered against. A slice that cannot satisfy rule 3 must use rule 1
or 2 instead — not reorder the delete path.

---

## 6. Read revocation *(rewritten in rev 2)*

Revision 1 specified the read fence as a comparison against "a value refreshed by
the same mechanism that drives eviction." That is a second in-memory authority —
the exact defect this architecture exists to remove. If notification is delayed or
fails (the condition motivating the work), room and cache both sit at *G* and keep
serving after Postgres reached *G+1*. Withdrawn.

Instantaneous durable read revocation is also not achievable: bytes already
rendered in a browser cannot be recalled, and a disconnected tab cannot be
reached at all.

**The honest contract, and what step 1 commits to:**

1. Postgres is authoritative for authorization and mutations.
2. A workspace past its epoch cannot produce another **durable semantic write**.
   Unconditional, immediate, enforced by §3.
3. Every **durable read** — hydration, artifact fetch, export, citation source —
   takes a real Postgres generation check. No new durable read leaks.
4. Connected clients are notified promptly and switched to a deleted-state
   screen, with an explicit websocket close. **Stated maximum lag: 5 seconds**,
   monitored, degrading to close-on-next-interaction if the notification path is
   down.
5. The next server interaction or reconnect is **always** refused, whatever the
   client believes.
6. Already-delivered bytes are not revocable. Stated, not engineered around.

This is weaker than "the editor goes dark the moment someone deletes", which was
the option chosen on 2026-08-17 against revision 1's description. That option was
not achievable as described. Point 4 is the strongest achievable version and the
decision should be re-confirmed against this text rather than against the
withdrawn one.

---

## 7. Stale convergence *(new in rev 2)*

"Abandon without writing" was wrong on two counts: it strands the job in
`running`, holding its active-unique slot until a reaper fires, and it destroys
deliberate late accounting — `review/pass_runner.py:164` explicitly keeps tokens
spent by a sub-call whose run was cancelled, *"so the late cost is still counted
by `review_dollars_today`."*

On `WorkspaceGenerationStale`, an actor must:

- produce **no** semantic effect;
- **still** record cleanup and accounting, via conditional writes only —
  tokens, dollars, terminal status, slot release.

Per-family convergence must be defined before the corresponding cancellation is
removed in step 3:

| Family | Stale convergence |
|---|---|
| Setup | requeue (not cancel), attempts uncharged — the shape Task 2's `_fail` fix already established |
| Turns | terminalize as cancelled, `usage_unknown` preserved per Slice A |
| Coherence / evaluator / review / uncited-claims / claim-analysis | terminalize, roll up tokens, release the active-unique slot |

**Sequencing consequence:** the stale-job reclaimer and the outbox are
prerequisites of step 3, not step 4. Removing transactional cancellation before a
reclaimer exists replaces a working mechanism with a leak.

---

## 8. What step 1 does not do

- **Removes nothing.** Not cancel-on-delete, `reconcile_cancelled_jobs`,
  `cancelled_from`, the restore mapping, the barriers, the 503-after-commit
  contract, or one `is_retired()` check.
- **Changes no HTTP contract.** No status code, body, or header moves.
- **Adds no outbox.** That is step 4.

Step 1 is purely additive, which is what makes it shippable against production
with collab live. It will temporarily **increase** total code: both systems
coexist by design. The simplification arrives at step 3.

---

## 9. How step 1 is proven

**Per-site.** For each fenced site, a test drives it with a stale generation and
asserts no semantic effect landed — **while the compensation for that site is
disabled in the test.** That is the only way to observe the fence rather than the
compensation. If the test passes with the fence removed, it is measuring the
compensation. This is the technique that exposed the decorative tests in C1.

**Coverage, mechanically.** An AST gate asserting that every claim function
records a generation, every belief-carrying semantic write fences, and every
`ToolContext` construction site stamps the generation per turn. Slice C2
established this pattern after a correct fix silently disarmed the runtime gate
proving the rule it fixed.

**The coverage gate moves early** — revision 1 scheduled it last. Generation
propagation through `ToolContext`, rooms and nested worker sessions has a silent
failure mode identical to the provenance trap already documented at
`registry.py:80`. A missed stamp does not fail; it fences against the wrong epoch.
That risk is highest while sites are being converted, not after.

---

## 10. Where the principle stops

**`rm -rf` cannot be fenced.** No DB predicate stops a holder mid-write. Purge
needs real quiescence — Slice D's filesystem-activity barrier, which survives this
architecture untouched. **Fence what is frequent; barrier what is destructive.**

**Spent tokens cannot be un-spent.** The fence stops the write, not the cost.
Best-effort cancellation keeps a legitimate role as an optimization inside step
4's outbox, never inside the lifecycle transaction.

**Already-delivered bytes cannot be recalled.** §6.

---

## 11. Slice breakdown

| Slice | Content |
|---|---|
| **F1** | Both primitives (§3.1, §3.2), `WorkspaceGenerationStale`, migration adding `claimed_at_generation` to the **7** live job tables. No call sites converted. |
| **F2** | The coverage gate skeleton (§9), asserting nothing yet — so every later slice lands against a gate that already exists. |
| **F3** | `ToolContext` generation propagation + per-turn re-stamp, and the tool-write fences (§4.1). The largest slice and the one revision 1 omitted. |
| **F4** | The 5 non-setup background job families, with the per-family lock ordering of §5. |
| **F5** | Setup (Phase 0) — separate, because it carries M→J→W and the `cancelled_from` machinery. |
| **F6** | Turn jobs. |
| **F7** | Collab write path: `Room` generation, `kernel.py`, `hydrate.py`, `checkpoint.py`. |
| **F8** | Durable read checks (§6 points 3 and 5) and the revocation path (point 4). |

F1 is the only slice with a migration. F3 precedes the job families deliberately:
it fences the effects that are reachable *today*, where the job-family fences
guard defects that only become reachable once step 3 removes cancellation.

---

## 12. Changelog

**Revision 2** — after review. Changes:

- **Audit unit corrected** from "mutex call site" to "effect and its originating
  actor". Added §4.1: tool writes and per-sentence commits that occur before any
  finalizer and take no mutex. This was the largest gap.
- **Split one primitive into two** (§3), because forcing every fence through the
  mutex creates contention and deadlocks.
- **Added §5, lock ordering.** Naive insertion of the mutex into existing
  finalizers creates J→M and W→M inversions against the delete path.
- **Rewrote §6.** Revision 1's read fence was a second in-memory authority.
  Replaced with an explicit bounded-revocation contract; the decision taken
  against revision 1's description needs re-confirming.
- **Added §7.** "Abandon without writing" stranded jobs and destroyed deliberate
  late token accounting; the reclaimer and outbox move into step 3's
  prerequisites.
- **Fence asserts equality**, not "moved past".
- **`AttachmentJob` resolved** as dead schema and removed; 7 tables, not 8.
- **Counts corrected** to 17 direct `_live` calls + wrapper + 6 `workspace_write`
  callers. Revision 1 said 21 + 7, having counted imports and docstrings.
- **Fixed an internal contradiction**: revision 1 listed `collab/kernel.py` as
  both unchanged (§2.1) and requiring five changes (§4.3). The file contains both
  request-driven and room-driven callers; §2.1 now says so explicitly.
- **Coverage gate moved early** (F2), on the evidence of the `registry.py:80`
  provenance trap.
- **Invariant renamed** to epoch-fenced *semantic* effects, so cleanup,
  accounting, notification and delivered reads are scoped out by definition
  rather than by exception.
