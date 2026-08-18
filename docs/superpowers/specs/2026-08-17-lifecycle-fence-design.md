# Epoch-Fenced Semantic Effects — design

**Status:** revision 4, 2026-08-17. Revision 1 was reviewed and found incomplete;
revision 2 corrected the audit unit and four further defects; revision 3 settles
the read-revocation contract; revision 4 folds in the F2 research findings: canonical
form, classified inventory, and ToolContext lifetime split. See §12 for the changelog.

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
| **Cleanup / accounting** | token totals, `estimated_dollars`, job → `cancelled`, reserving or releasing an admission slot | **Permitted** on a stale generation, via conditional (CAS) writes only. |
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
coverage gate (§9), and `web/_writes.py` documents a live trap where a call-time
keyword overrides a `functools.partial` keyword used by the straddle tests.

### 3.3 `WorkspaceGenerationStale`

Distinct from `WorkspaceDeleted` and `WorkspaceRetired`. It is **not** an error
condition: it means "your epoch is over; produce no further semantic effect."
It never reaches a user as a 5xx.

It does **not** mean "write nothing" — see §7.

### 3.4 Canonical form *(introduced in F3)*

The two primitives are reached through two wrapper forms, defined in F3:

- **`fenced_write(...)`** — wraps Primitive B (mutex + generation). Used for effects
  that span git, the filesystem, or multiple rows that must move together.
- **`fenced_execute(...)`** — wraps Primitive A (conditional write, DB-only). Used
  for pure DB effects where no lock is held.

A fenced site **must** be written in one of these two forms. The reason is the same
one `web/_writes.py` gives for `workspace_write()`: a rule that cannot be written
wrongly beats a checker that tries to prove arbitrary code correct. Requiring the
canonical form means containment is structural — a site outside the form is
syntactically absent, not subtly wrong.

F2's gate verifies **containment**: every classified semantic site in
`tests/fencing/fence_inventory.json` is checked to reside inside one of the two
canonical wrappers (or inside its own `caller_fenced` ancestor). F2 does NOT
attempt dataflow analysis of arbitrary call sequences — containment is sufficient
because the form itself enforces the generation check.

`fenced_write` and `fenced_execute` do not exist yet. F3 creates them and converts
every semantic site into the canonical form in the same commit.

---

## 4. Effect inventory

Organised by originating actor, not by file.

### 4.1 Effects inside long-running actors, before any finalizer *(new in rev 2; inventory classified in F2)*

This class was **missing from revision 1** and is the largest gap. These effects
commit on their own sessions, during agent execution, with no mutex, no liveness
check, and no reference to the originating job. Fencing the eventual finalizer
cannot retract them.

The canonical list lives in `tests/fencing/fence_inventory.json`; the table below
is a narrative summary that the gate keeps honest. Nine committing sites were
identified by the F2 research and source-verified by the gate:

| Qualified symbol | Category | Why |
|---|---|---|
| `citations.py::propose_citation_impl` | `semantic` | inserts a `ProposedChange` |
| `citations.py::_write_citekey_cache` | `caller_fenced` | best-effort citekey cache; caller `_insert_citation_locked` holds the critical section |
| `proposal.py::_propose_artifact_edit_impl` | `semantic` | inserts a `ProposedChange` |
| `references.py::fetch_reference_impl` | `accounting` | atomic cap-check + increment of `TurnJob.fetch_count` — reserves an admission slot, produces no result |
| `references.py::fetch_reference_impl._refund_fetch_slot` | `accounting` | refunds that slot when the fetch fails |
| `references.py::fetch_reference_impl._do_fetch_and_ingest` | `semantic` | ingests a reference — durable content plus embeddings |
| `review_submit.py::submit_review_impl` | `semantic` | writes the review result |
| `scorecard.py::submit_scorecard_impl` | `semantic` | writes the scorecard |
| `uncited_claims.py::submit_uncited_claims_impl` | `semantic` | writes the findings |

Six semantic, two accounting, one caller_fenced. `references.py` and `scorecard.py`
appeared nowhere in the revision 2 hand inventory. The accounting sites
(`fetch_reference_impl` and `_refund_fetch_slot`) must run on a stale generation —
they are slot bookkeeping, not semantic production — which is why the accounting
category is widened in §2 and deliberately separated here.

**Requirement:** the generation travels in `ToolContext` (`tools/registry.py:43`)
and every `semantic` tool write fences on it — Primitive A for the DB-only ones
(reached via `fenced_execute` in F3). `accounting` sites must NOT fence; they use
CAS writes only.

#### ToolContext lifetimes *(F2 finding)*

Seven `ToolContext(...)` constructions exist in the codebase; five are in scope.
Two pass `session_factory=None` and are excluded — they cannot originate a tool
commit:

- `proposals/accepting.py` — request-driven, holds its own session.
- `tools/registry.py::collect_mcp_tool_names` — static tool-name collector, no
  session or generation.

Two consequences follow from this classification:

1. **The generation is a property of the ctx, not the tool.** Each tool receives
   the generation via its ctx; there is no per-tool stamp.

2. **The five in-scope constructions split by lifetime:**

   - **`one_shot`** (coherence, review, evaluator, uncited-claims background
     agents): the generation is known at construction and required there. The gate
     verifies construction-time propagation for these.

   - **`retained`** (`drafting/factory.py::tools_factory`, the interactive ctx
     built during `controller.launch(...)`): launch deliberately runs *before* the
     claim (`turns/processor.py:159` — the orphan sweep must run first), so it
     cannot carry a generation at construction; requiring one would bless a
     placeholder. Instead, it is stamped unconditionally at `set_turn_provenance`
     each turn, mirroring the identical provenance trap already documented at
     `registry.py:80`. A generation stamped once at launch is a stale belief
     wearing the fence's uniform.

     **F3's runtime two-turn test is authoritative for the retained ctx.** Drive
     two turns through one retained ctx across a generation change and assert the
     second carries the new generation. A static gate cannot verify per-turn
     re-stamps; see §9.

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

## 6. Read revocation *(rewritten in rev 2, contract settled in rev 3)*

Revision 1 specified the read fence as a comparison against "a value refreshed by
the same mechanism that drives eviction." That is a second in-memory authority —
the exact defect this architecture exists to remove. If notification is delayed or
fails (the condition motivating the work), room and cache both sit at *G* and keep
serving after Postgres reached *G+1*. Withdrawn.

Instantaneous durable read revocation is also not achievable: bytes already
rendered in a browser cannot be recalled, and a disconnected tab cannot be
reached at all.

**The contract, confirmed 2026-08-17.** Postgres is checked at every boundary
where checking it is affordable, and open sockets hold a short lease instead:

1. Postgres is authoritative for authorization and mutations.
2. A workspace past its epoch cannot produce another **durable semantic write**.
   Unconditional, immediate, enforced by §3.
3. Every **durable read** — every HTTP read, every WebSocket **handshake**,
   hydration, artifact fetch, export, citation source — takes a real Postgres
   generation check. No new durable read leaks.
4. **Every new HTTP request and every reconnect is refused**, whatever the client
   believes. Established sockets are revoked within five seconds (point 5).
   These are deliberately different sentences: a blanket "the next server
   interaction is always refused" would implicitly require a per-frame database
   check, which is the cost this contract exists to avoid.
5. An **established socket is revoked within 5 seconds**, as a real bound, not a
   healthy-path target. See §6.1.
6. Already-delivered bytes are not revocable. Stated, not engineered around.

### 6.1 The lease, and why the sweep alone is not enough

The fast path stays as it is: the existing retirement signal reaches rooms
immediately. It is an accelerator, and it may fail silently — which is why it
cannot be the contract.

The durable backstop is a **batched Postgres sweep**, at least every 5 seconds:
one query over the open rooms' workspaces, comparing each room's **captured
generation** against `workspaces.lifecycle_generation`. Exact-equality, not
`soft_deleted_at` — so a delete→restore cycle closes the old room, which a
liveness comparison would leave open on a workspace that is live again.

**The fail-closed half must be enforced by the room, not by the sweep.** A sweep
that dies closes nothing; a design that relies on the sweep to disconnect fails
*open* in exactly the case that motivates it. So the sweep does not disconnect —
it **renews a lease**. Each room holds a `valid_until`; the sweep extends it on
successful revalidation; a room whose lease expires disconnects itself. Expiry is
the default and renewal is the exception, which is what makes "within 5 seconds"
a bound rather than a hope.

This also removes a threading problem. The sweep is a periodic DB job on the
scheduler — the same shape as the tail-bounding checkpoint job (Slice 3b-5c),
which deliberately never touches the asyncio room manager. Lease **renewal** is a
data write the scheduler can make safely; lease **expiry** is enforced inside the
event loop by the room itself. Neither side reaches across.

Monitoring: sweep lag and the count of lease-expiry disconnects are the two
signals that say whether the fast path is healthy. A rising expiry count means
the signal is failing and the backstop is carrying the contract.

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
records a generation, every belief-carrying semantic write is contained inside the
canonical fenced form (§3.4), and every in-scope `ToolContext` construction site
carries a generation at construction. Slice C2 established this pattern after a
correct fix silently disarmed the runtime gate proving the rule it fixed.

F2 verifies **construction-time propagation for one-shot contexts only**. A static
gate cannot observe a per-turn re-stamp: the retained interactive ctx is built
before the generation is known (launch precedes the claim by design), so its
construction cannot carry a real generation. The per-turn re-stamp at
`set_turn_provenance` is **F3's runtime obligation**, not F2's static obligation.
F3 must prove it with a runtime test: drive two turns through one retained ctx
across a generation change and assert the second carries the new generation.

**The coverage gate moves early** — revision 1 scheduled it last. Generation
propagation through `ToolContext`, rooms and nested worker sessions has a silent
failure mode identical to the provenance trap already documented at
`registry.py:80`. A missed stamp does not fail; it fences against the wrong epoch.
That risk is highest while sites are being converted, not after.

---

### 9.1 What F2 actually delivers, and what it does not *(new in rev 5)*

**F2 is an inventory plus a verifier for a form nothing yet uses. It is not an
enforcement gate.** An external review found five reachable bypasses, each
reproduced against the merged verifier. The honest statement of coverage:

**What F2 does prove.** The classification of all 20 sites is correct and
mechanically re-derivable (9 `tool_commit` / 6 `claim_fn` / 5 `tool_context`,
matching an independent hand count). A site cannot silently vanish, be renamed
away, be left unclassified, take an exemption it was not pinned for, or have its
slice flag forgotten. The verifier's containment rule is right for the shapes it
sees, and every one of its rules has a negative fixture proven non-decorative by
mutation.

**What F2 does not prove — F3's first obligation, before it converts any site.**

1. **The detector is calibrated to the pre-conversion world, and goes blind as
   conversion proceeds.** It emits a `tool_commit` site only where it sees a
   literal `.commit()` under `tools/`. The canonical form of §3.4 *removes* that
   commit — `fenced_write` owns it, and `fenced_execute` needs none. A correctly
   converted function therefore stops being detectable, and a *newly written*
   canonical-form function is never detected at all. Confirmed: a function using
   `with ctx.session_factory.begin()` yields zero sites. (Converting an
   *existing* site is caught — the inventory's identity goes stale and the gate
   fails — but new code has no stale entry to trip.) **A gate that gets greener
   the more converted the codebase is inverts its own purpose**, and Global
   Constraint 5 of the F2 plan names exactly this failure: a gate that cannot
   fail is worse than no gate.

2. **`_is_effect` recognises an open set incompletely.** It sees `.commit()`,
   `.add()`, and `execute(update|insert|delete(...))` written inline. It does not
   see `session.execute(stmt)` where `stmt` was built earlier, `flush`,
   `add_all`, `delete`, `merge`, bulk operations, `sessionmaker.begin()`,
   `engine.begin()`, raw connections, filesystem or git helpers, or any effectful
   helper in another module. Each was confirmed to pass verification while the
   write lands outside the fence.

3. **Cross-module transactions cannot be proven by lexical containment at the
   caller — and one already exists on a semantic path.**
   `tools/references.py::_do_fetch_and_ingest` calls `ingest_bytes(...)`, which
   commits the caller's session at `references/ingest.py:389` and then commits
   six more times on *fresh* sessions. The first of those commits also releases
   the transaction-scoped advisory mutex. So the plausible conversion —

   ```python
   ingest_result = ingest_bytes(...)          # durable writes already landed
   with fenced_write(s, ws, generation=gen):
       s.commit()                             # documented as a harmless no-op
   ```

   passes the gate with every semantic write outside the fence, and moving
   `ingest_bytes` inside the block does not fix it. F3 must fence those internal
   transactions explicitly or restructure the path.

4. **`caller_fenced` proves the helper is enclosed, not the critical section.**
   The rule requires the named ancestor to contain a call to the site inside a
   fenced region. It does not require the fence to enclose the ancestor's *whole*
   semantic critical section, so a fence placed *after* the real effect passes —
   confirmed against the shape `_insert_citation_locked` actually has, where the
   git write at `tools/citations.py:844` precedes the cache helper. The ancestor's
   call is also matched by bare name, so `obj._write_citekey_cache()` would
   satisfy a declaration naming the module-level function. Not reachable today
   (all three call sites are bare), but not excluded either.

**The direction of the fix, and the constraint it must respect.** §3.4 exists
because proving arbitrary Python is fenced is not decidable in a maintainable
form; the answer to a shape containment cannot catch is to **narrow the accepted
syntax, never to widen the analysis**. Enumerating effects is an open set that
must be complete to be sound, and it cannot be. The sound version inverts it:
make the session **unobtainable outside a fence**, so there is nothing to contain
because there is no unfenced handle to write through. That is `workspace_write()`
carried all the way, and it also dissolves finding 3 — `ingest_bytes` takes a
`session_factory`, so its fresh sessions become visible violations rather than
invisible ones.

Until F3 does that work, no slice may be marked `complete` in `SLICE_STATUS`.

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
| **F8** | Durable read checks (§6 point 3: HTTP reads + WebSocket handshake) and refuse-on-reconnect (point 4). |
| **F9** | The room lease and the batched revalidation sweep (§6.1), plus its two monitoring signals. Separate from F8 because it is the only part with a scheduler component and an in-loop expiry path. |

F1 is the only slice with a migration. F3 precedes the job families deliberately:
it fences the effects that are reachable *today*, where the job-family fences
guard defects that only become reachable once step 3 removes cancellation.

---

## 12. Changelog

**Revision 5 (2026-08-18).** Added §9.1 after an external review found five
reachable bypasses in the merged F2 gate, each reproduced against the verifier.
F2 is demoted from "coverage gate" to "inventory plus a verifier for a form
nothing yet uses". The root cause is that the detector keys on `.commit()` while
the canonical form removes it, so the gate goes blind exactly as conversion
proceeds. F3 must redesign detection — toward making the session unobtainable
outside a fence — before converting any site. The two silent-downgrade holes
(unpinned exemptions, unread `slice` field) were closed on the F2 branch.


**Revision 4** — F2 research findings folded in. Changes:

- **Audit unit corrected a second time.** The revision 2 hand inventory of §4.1
  was wrong in three places: `references.py` (two accounting + one semantic) and
  `scorecard.py` (one semantic) were absent; the old generic rows for "evaluator /
  review / uncited-claims tools" were not individually verified. F2's mechanical
  gate identified nine classified sites; the inventory is now in that form, with the
  canonical list in `tests/fencing/fence_inventory.json`.
- **Canonical form decided and why recorded (§3.4).** F3 introduces
  `fenced_write(...)` and `fenced_execute(...)` as the required form for fenced
  sites. A rule that cannot be written wrongly beats a checker that tries to prove
  arbitrary code correct. F2's gate verifies containment — not dataflow analysis.
- **Accounting category widened (§2).** "Freeing an active-unique slot" did not
  cover the slot-reservation side (`references.py::fetch_reference_impl`). Widened
  to "reserving or releasing an admission slot". No fourth category invented.
- **ToolContext lifetime split recorded (§4.1, §9).** Seven constructions, five in
  scope; two excluded (`proposals/accepting.py` and
  `tools/registry.py::collect_mcp_tool_names`, both `session_factory=None`).
  Consequence: the generation is a property of the ctx, not the tool. The five
  split into `one_shot` (construction-time generation required and verified by F2)
  and `retained` (`drafting/factory.py::tools_factory`, stamped per-turn at
  `set_turn_provenance`; F3's runtime two-turn test is authoritative).

**Revision 3** — read-revocation contract settled. Changes:

- **"Maximum lag 5 s, degrading to close-on-next-interaction" was not a maximum.**
  The signal it depended on can fail silently, so the stated bound was
  unenforceable. Replaced with a batched Postgres revalidation sweep and a real
  bound (§6.1).
- **Fail-closed moved from the sweep to the room.** A sweep that dies disconnects
  nothing, so a sweep-side design fails *open* in precisely the case that
  motivates it. Rooms hold a `valid_until` lease that the sweep renews; expiry is
  the default. This also keeps the scheduler out of the asyncio room manager —
  renewal is a data write, expiry is enforced in-loop.
- **Sweep compares exact generations**, not `soft_deleted_at`, so delete→restore
  closes the old room rather than leaving it open against a live workspace.
- **Split point 4 into two sentences.** "The next server interaction is always
  refused" contradicted allowing an established socket a 5-second window, and
  implied the per-frame database check this contract exists to avoid. Now: every
  new HTTP request and reconnect is refused; established sockets are revoked
  within five seconds.
- **WebSocket handshake** named explicitly as a durable read check.
- New slice **F9** for the lease and sweep.

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
