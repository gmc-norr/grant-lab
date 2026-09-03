# Phase-0 retirement and transaction boundaries

Date: 2026-08-11
Status: approved (design), revised after review; implementation plan to follow
Relationship: **prerequisite for** `docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md`

## 1. Why this slice exists

**Corrected framing.** The first draft of this spec claimed a Phase-0 job could
resurrect repo bytes that `scrub_workspace_bytes` had removed. That is wrong.
The scrub removes only `attachments/` and `references/`, and both storage
contracts state explicitly that the sibling `repo/` tree is untouched
(`attachments/storage.py:140`, `references/storage.py:106`).

The real defect is narrower but still real: **a Phase-0 job that claimed before
a delete creates a brand-new repo tree, git history and empty artifacts for a
workspace that is already deleted.** `_snapshot_and_init_repo` does
`repo_dir.mkdir(parents=True, exist_ok=True)`, `git init`, three empty files
and an initial commit (`setup.py:226`), and it also writes
`call_body_snapshot` / `profile_desc_snapshot` onto the deleted workspace row.
The guarantee this slice delivers is therefore **"no post-delete repo
creation"**, not "no resurrection of scrubbed bytes".

The severity claim is corrected with it: this is a post-delete write, not an
undoing of a compliance scrub.

The obvious fix — take the workspace row lock in `_snapshot_and_init_repo` — is
worse than the bug. `run_one` commits immediately after the claim to release
the claim lock, then calls `_run_steps` on that *same* session, whose next
commit comes only after every step finishes (`setup.py:155`), including two LLM
extractors. A `FOR UPDATE` taken inside `_snapshot_and_init_repo` would be held
across all of Phase 0. And because `acquire_commit_mutex` restores
`SET LOCAL lock_timeout = DEFAULT` after acquiring (`locking.py:201`), the
delete transaction's subsequent row UPDATE has **no timeout** — DELETE would
block indefinitely behind a Phase-0 job.

Making the lock safe means giving Phase 0 real transaction boundaries, which is
why this is its own slice.

### 1.1 Repo bytes are deliberately NOT scrubbed here

If deletion is *meant* to erase repo contents — the workspace's drafts, its git
history — that scrub **does not exist today**, for any workspace, deleted
before or after this slice.

**Decision: it stays out of Phase 0 and Slice C.** The product contract today
is explicit in the UI: *"Soft-deletes the workspace and scrubs uploaded file
bytes. Audit rows are kept."* (`frontend/src/cockpit/WorkspaceSettingsModal.tsx:78`).
Adding repo erasure to a retirement slice would silently change what delete
means to users, in a slice they would not associate with that change.

**But indefinite retention must not remain an accidental policy**, and deleting
`repo/` alone would not amount to workspace-content erasure in any case.
Reconstructable or sensitive content also lives in `YjsUpdate.update_bytes`,
`ChatTurn.content`, proposed-change payloads, `Workspace` snapshot columns
(`call_body_snapshot`, `section_extracts`, `rubric_*`) and event payloads.
Purging one store would produce a false sense of erasure.

This is therefore recorded as a **tracked workspace data-lifecycle decision**,
owned outside engineering, with this shape:

1. Immediate soft-delete and access revocation (already shipped).
2. Classify which records must be retained, and on what authority.
3. Give working content a documented `purge_after` period, unless a legal hold
   or records rule applies.
4. Purge `repo/` and the corresponding reconstructable database content
   *together*.
5. Sweep every already-deleted workspace, and account for backup expiry.
6. Retain only the explicitly approved minimal audit record.

It needs UmU archive/privacy ownership, not an engineering default: GDPR
storage limitation requires a defined purpose and retention period rather than
indefinite storage, while UmU may be legally required to retain qualifying
research data and its accompanying documentation under its records schedule.
Those two pull in opposite directions and the resolution is theirs to make.

The engineering bias to bring to that conversation is bounded purge of working
drafts, with preservation only where the records schedule or a legal hold
affirmatively requires it.

Slice C repeats the same incorrect premise and is amended alongside this spec.

## 2. Scope

In scope: the `live_ws` filter on the setup claim; splitting Phase 0 into
per-milestone transactions with a bounded lock; separating extractor
computation from persistence; Call-snapshot consistency across those
transactions; per-milestone idempotency and recovery; a `cancelled` job status;
the `"jobs"` retirement hook; and the reconciliation tick that hook depends on.

Out of scope: everything Slice C owns (the pass registry, claim-analysis, the
settings/rubric mutex routes, the `soft_delete_workspace` wrapper removal), and
repo-content scrubbing (§1.1).

## 3. The three milestones

| milestone | work | touches FS? | LLM? |
|---|---|---|---|
| **M1** `_snapshot_and_init_repo` | `mkdir` + `git init` + three empty artifacts + initial commit; then `call_body_snapshot`, `profile_desc_snapshot`, `snapshot_refreshed_at` | **yes** | no |
| **M2** `_ensure_section_extracts_snapshot` | section extracts → `CallSectionExtract` rows + `ws.section_extracts` | no | yes |
| **M3** `_ensure_rubric_snapshot` | rubric → `CallRubric` + `ws.rubric_*` + `rubric_initialized` event + **the job's terminal status** (§7) | no | yes |

M2 and M3 write their extractor rows against the **Call**, not the workspace.
Those rows are a shared cache legitimately kept after a workspace is deleted —
but being shared is also what makes them raceable, which §6 addresses.

## 4. Only M1 takes the workspace lock for the filesystem

M1 is the only milestone that touches the filesystem, so it is the only one
whose *filesystem* work must be linearized against delete.

M1: open a session → **`acquire_commit_mutex_live`** → load the workspace
`with_for_update` → do the FS work and the snapshot fields → commit, releasing
both.

**The lock is held across the FS work deliberately.** Either the delete waits
for M1, or the delete wins and M1 aborts having created nothing. Checking
liveness under the lock and *then* releasing before writing would restore the
exact race this slice exists to close.

**But a row lock alone is not a bound, and a duration test cannot make it one.**
The work inside is `subprocess.run`, which has no timeout, and git may invoke
configured hooks or a signing helper. A CI test asserting the lock is short
cannot stop a production DELETE from waiting indefinitely on a wedged git.

Three changes make it genuinely bounded:

1. **`acquire_commit_mutex_live` before the row lock.** This is the established
   mutex→row order, and `soft_delete_workspace_db` takes the same mutex before
   it mutates anything (`locking.py:207`). A DELETE blocked behind a wedged M1
   therefore hits `CommitMutexTimeout` and returns the house **429** rather
   than hanging on a row lock with no timeout. It also gives M1 the liveness
   re-read for free — `acquire_commit_mutex_live` raises `WorkspaceDeleted` on
   an already-deleted workspace, which is exactly M1's abort condition.

   **Stated precisely:** DELETE cannot reach the workspace row *while M1 owns
   it*, because both take the mutex first and DELETE loses at the mutex. The
   broader claim that the row lock is only reachable by a mutex holder would be
   false — M2, M3 and Slice C's pass prefixes all take the row lock without the
   mutex. DELETE can block on the row behind those, but each is a short
   DB-only transaction with no subprocess and no LLM inside it, which is the
   property that makes them safe and M1 the one case needing the mutex.
2. **Hermetic git.** Every command runs with hooks disabled and system/global
   config neutralized, so no site configuration can introduce a hook, a signing
   helper or a credential prompt into the locked region.
3. **A real `timeout=` on every `subprocess.run`.** A wedged git raises, M1
   rolls back, and both locks release.

M2 and M3 also take locks, but only around their persists (§5, §6), never
around an LLM call.

## 5. Compute out, persist in

`ensure_section_extracts_fresh` and `ensure_rubric_fresh` each take a session
and perform the LLM call *and* the database mutation as one unit under a
"caller commits" contract (`extractors/refresh.py:40`, `:96`). Short result
transactions are impossible while that holds.

Each splits into:

- `compute_*` — pure: takes the call text and a client, returns a payload. No
  session.
- `persist_*` — takes a session and a payload, writes rows. No LLM.

The freshness guard (`source_text_hash` + `extractor_version`) is evaluated
before computing, so "skip when fresh" is preserved and still avoids the LLM
call entirely.

Phase 0 is the **only production caller** of either function. The thirteen call
sites in `tests/proposal_assist/test_extractors_refresh.py` are the extractors'
own tests and move to the new shape, preserving every behaviour they assert —
including *preserve existing rows when the extractor raises*, which the split
makes structural rather than incidental.

## 6. Call-snapshot consistency

Splitting the session removes a guarantee the single session was providing
incidentally: its identity map effectively pinned one `Call` across M1, M2 and
M3. Re-ingestion overwrites `Call.full_text` (`ingestion/upsert.py:114`
coalesces only to survive a failed scrape; a successful re-scrape replaces the
text), so with separate sessions a job could commit M1's snapshot from Call
revision A, M2's sections from revision B and M3's rubric from revision C — and
then succeed permanently, with internally inconsistent workspace snapshots and
no error anywhere.

**The run pins a Call source hash.** M1 returns the `source_text_hash` of the
call text it snapshotted. This is an **immutable run-local value** threaded
through the worker for the duration of one `run_one` — *not* a new persisted
column, and not re-read from the database later. Every later persist
transaction:

1. locks the **workspace row and the Call row** — in that order, normatively,
   so two workspaces sharing a Call cannot deadlock and neither can invert
   against the delete transaction's workspace-row update;
2. revalidates the pinned hash against the Call's current text;
3. **fails the job with a typed error** if it changed (§6.1), rather than
   committing a mixed snapshot.

Locking the Call row also closes the second race the reviewer identified: two
workspaces can run setup concurrently against the *same* Call while both
mutate the shared `CallSectionExtract` / `CallRubric` rows. Locking only each
worker's own workspace row would leave those shared writes unserialized.

**A stale payload is discarded, not written.** If another worker made the cache
fresh while our computation was in flight, the persist snapshots the canonical
cache rather than overwriting it with our now-redundant payload. This is the
same "extract first, then check" discipline the current code applies within one
transaction, extended across the new boundary.

### 6.1 A changed Call is a bounded failure, not an internal loop

"Restart from M1" must **not** be implemented as a loop inside the worker — a
Call being re-ingested repeatedly would spin indefinitely. It is normatively a
typed failure that goes through the existing retry machinery:

1. Roll back the persist transaction.
2. Raise a typed `CallSourceChanged`.
3. In the clean failure transaction, re-check deletion and cancellation first —
   a deleted or cancelled job takes the terminal path (§9.4), not the retry
   path.
4. Otherwise increment `attempts` and requeue, recording a `last_error` that
   names the cause: ingestion kept changing the call while setup ran.
5. Dead-letter to `failed` at `MAX_SETUP_ATTEMPTS`, as any other failure does.

This converts a theoretical livelock into bounded retry, and the owner can use
the existing `retry_setup` route once ingestion has settled. Gate 5b pins it:
attempts 1–4 requeue, attempt 5 dead-letters, and the mutation that omits the
increment demonstrates the spin.

## 7. M3 and the job's terminal status are one transaction

The first draft left a crash boundary between a committed M3 and the job's
`waiting_for_rubric_review` transition. That boundary creates two problems, and
neither needs to exist:

- **A cancellation race.** The `"jobs"` hook can set the job to `cancelled`,
  after which the success finalizer overwrites it with
  `waiting_for_rubric_review` — un-cancelling a deleted workspace's job.
- **The duplicate `rubric_initialized` event.** A crash between M3's commit and
  the status write re-runs M3 and appends a second event.

So the `running → waiting_for_rubric_review` transition moves **inside M3's
persist transaction**, as a compare-and-set on `status == 'running'` so a
concurrent cancel wins rather than being overwritten. Rubric snapshot,
initialization event and terminal status become atomic.

**The existence guard alone was insufficient anyway.** Guarding on "no
`rubric_initialized` event exists" does not help if the Call changed before
recovery: M3 could write rubric B while retaining the initialization event for
rubric A, leaving the canonical replay base wrong without technically
duplicating it. §6's hash revalidation is what actually fixes that, by
restarting from M1 instead of persisting rubric B.

A **partial unique index** — one `rubric_initialized` event per workspace —
ships in the same migration as defence in depth. It is a backstop, not the
mechanism; the atomic transaction is the mechanism.

## 8. Liveness is checked before computing, not only before persisting

Checking liveness only in the post-LLM persist transaction still lets a delete
between M2 and M3 be followed by M3 starting a **new cloud call** for an
already-deleted workspace.

Each of M2 and M3 therefore runs a short pre-compute check of both workspace
liveness and job status, and keeps the locked persist-time recheck for the race
that opens after it. The pre-compute check saves the spend; the persist-time
check preserves correctness.

## 9. Cancellation

**New status `cancelled`** — Alembic migration `0061`, extending
`ck_workspace_setup_jobs_status` to
`('queued','running','waiting_for_rubric_review','done','failed','cancelled')`,
plus the partial unique index from §7. Reusing `failed` was considered and
rejected: a deleted workspace's setup would be indistinguishable from a
genuinely broken one in logs and metrics.

**The downgrade must convert data before restoring the constraint.** Any row in
`cancelled` has to be rewritten (to `failed`, with `last_error` preserved)
*before* the old CheckConstraint is re-applied, or the downgrade fails against
any database where a cancellation has occurred.

Four behavioural changes:

1. **`live_ws` filter on `claim_next_setup_job`**, matching the four pass claim
   queries. Durable in SQL, survives restart.
2. **Liveness checks per §8.**
3. **The `"jobs"` hook** (§10) sets active setup jobs to `cancelled` after the
   delete commits.
4. **The failure path must not resurrect a cancelled job.** `run_one`'s
   `except` handler currently sets `status = "queued"` whenever
   `attempts < MAX_SETUP_ATTEMPTS`, which would send a cancelled job straight
   back to the queue. It must write a terminal status when the job was
   cancelled or its workspace deleted.

`requeue_stuck_setup_jobs` needs no change: `cancelled` is terminal, so its
`status == "running"` predicate cannot touch it.

## 10. The `"jobs"` hook and its reconciliation tick

This slice introduces both; Slice C extends the hook with claim-analysis.

**The hook:**

- **Required.** `REQUIRED_HOOKS` becomes `("turns", "collab", "jobs")`, so an
  unwired cancel fails at boot — the structural guard that previously caught an
  unwired turns hook.
- **Runs its UPDATE via `asyncio.to_thread`**, the coordinator being async and
  this a sync DB write.
- **`FOR UPDATE SKIP LOCKED`.** A row held by a live worker is skipped, never
  waited on, so the hook cannot inherit a worker's transaction lifetime.
- **`_JOBS_CANCEL_SECONDS = 1.0`, enforced with `asyncio.wait_for`.** Declaring
  a constant does not bound a `to_thread` call; the `wait_for` is the bound. It
  is a timeout for pathology, not for expected waiting. Hooks run concurrently,
  so `max(collab 3.0, turns 5.0, jobs 1.0) = 5.0 < 6.0` — the barrier constant
  is unchanged.

**The reconciliation tick** owns convergence for rows the hook skipped. It is
scheduled **independently of the Phase-0 tick**, which is skipped entirely when
`DRAFTING_ENABLED=false` or `ANTHROPIC_API_KEY` is unset
(`scheduler.py:549,552`). Hiding reconciliation behind that path would strand
every skipped cancellation on a deployment with drafting off. The tick needs no
LLM client: it re-attempts the cancel UPDATE for setup jobs whose workspace is
soft-deleted.

This slice's tick handles **setup jobs only**. Slice C extends the same tick to
claim-analysis when it adds that population to the hook — the extension point
is named here so the obligation is not lost between the two specs.

## 11. Recovery

**No progress column is added.** Recovery is "re-run from M1", and each
milestone is a no-op when its work is already done:

- **M1** already checks `git rev-list --count HEAD` to detect a crash between
  `git init` and the initial commit, and rewrites the artifact files with
  `write_text("")`, idempotent by construction.
- **M2/M3** skip the LLM call entirely when `(source_text_hash,
  extractor_version)` matches, and their writes are upserts.
- **A changed Call forces a restart from M1** (§6) rather than a resumed
  partial run, which is what keeps "re-run from M1" a sufficient recovery
  story.

## 12. Open residuals

1. **Phase 0 cannot stop a running extractor call.** It is sync code in
   APScheduler's thread pool with no cancellation primitive. **DELETE does not
   wait for it** — the route returns, and the worker and its cloud spend
   continue until the call returns on its own. The milestone boundaries mean no
   *further* call starts, which bounds the residual to one extractor call; they
   do not shorten that call.
2. **A setup job row held by a live worker is skipped**, not cancelled, until
   the reconciliation tick converges it (§10).
3. **`CallSectionExtract` / `CallRubric` rows persist** after a workspace is
   deleted. Intentional — Call-scoped shared cache, not workspace data.
4. **Repo contents are never scrubbed** (§1.1), for deleted workspaces old or
   new. Unchanged by this slice, and an open question rather than a decision.

## 13. Non-goals

- No change to `MAX_SETUP_ATTEMPTS`, `STUCK_SETUP_AFTER`, or the stuck-job
  reaper.
- No change to what Phase 0 *produces* — same artifacts, same snapshots, same
  terminal states.
- No new progress/milestone column (§11).
- No change to `_RETIREMENT_BARRIER_SECONDS`.
- No repo-content scrub (§1.1).

## 14. Testing and acceptance

Acceptance is by **mutation**: each gate must be shown to fail when its fix is
reverted.

1. **No post-delete repo creation.** A setup job already past its claim must
   not create a repo tree for a deleted workspace. Mutation: drop M1's liveness
   check and the test must fail. Headline gate.
2. **DELETE is not blocked by Phase 0.** A DELETE issued while Phase 0 runs its
   extractors returns within the barrier, measured as **route wall-clock**.
   Mutation: hold the workspace row lock across `_run_steps` and the test must
   fail — the indefinite-block case, so the gate must assert latency, not that
   the delete eventually committed.
3. **A wedged M1 does not wedge DELETE.** Two separate assertions, because one
   test cannot carry both:

   *3a — the timeout works.* A hung git command inside M1 hits its
   `subprocess.run(timeout=…)`, M1 rolls back, and both the mutex and the row
   lock release.

   *3b — the mutex is what protects DELETE.* The wedge here is **an
   filesystem operation, not a subprocess** — patch M1's `mkdir`/`write_text`
   to block — precisely so no `subprocess.run` timeout can release the row
   early and make the result depend on which timeout fires first. With M1
   wedged that way, a concurrent DELETE returns 429 at the mutex timeout.
   Mutation: drop the mutex acquisition from M1 and DELETE hangs on the row
   lock past the observation window instead of returning 429.

   Gate 3b is the load-bearing one; 3a only proves the subprocess escape hatch
   exists. A hold-duration assertion is kept as a secondary signal but is
   explicitly *not* the bound.
4. **Re-ingestion between milestones.** Call text changes after M1; the job
   restarts from M1 rather than committing a mixed snapshot.
5. **Re-ingestion during an extractor call.** Call text changes while compute
   is in flight; the payload is discarded, not persisted.
5b. **A changed Call is bounded retry, not a spin.** Attempts 1–4 requeue with
   the ingestion-changed `last_error`; attempt 5 dead-letters to `failed`; and
   a cancelled or deleted job takes the terminal path instead of requeueing.
   Mutation: omit the `attempts` increment and the test must demonstrate
   unbounded restarts.
6. **Two workspaces, one Call.** Concurrent setup runs sharing a Call serialize
   on the Call row and neither corrupts the shared cache. Mutation: lock only
   the workspace row and the test must fail.
7. **M3 is atomic with the status transition.** A cancel landing between the
   rubric write and the status write cannot be overwritten by
   `waiting_for_rubric_review`. Mutation: move the status write back out of
   M3's transaction and the test must fail.
8. **The audit event does not double**, and does not survive a Call change
   (§7). Two mutations, matching the two mechanisms actually specified: move
   the status write back out of M3's transaction (covered by gate 7) and a
   crash-then-rerun must produce a second event; and drop the partial unique
   index, whereupon a direct second insert succeeds instead of raising. There
   is deliberately **no application-level existence guard** to mutate — the
   atomic transaction is the mechanism and the index is the backstop.
9. **Pre-compute liveness.** A delete between M2 and M3 means M3 starts no
   cloud call. Mutation: remove the pre-compute check and the test must fail
   (assert on the client, not on elapsed time).
10. **Cancelled jobs are not requeued.** Mutation: restore the unconditional
    `status = "queued"`.
11. **Claim filter.** A deleted workspace's queued setup job is never claimed.
12. **Hook wiring, end-to-end.** Replacing `main.py`'s `"jobs"` hook with a
    no-op must fail a test driving the real DELETE route against an active
    setup job.
13. **Reconciliation converges without an LLM client.** A row skipped by the
    hook becomes `cancelled` on a later tick with `DRAFTING_ENABLED=false` and
    no `ANTHROPIC_API_KEY`. Mutation: register the tick behind the Phase-0 path
    and the test must fail.
14. **The hook is actually bounded.** A wedged cancel returns at
    `_JOBS_CANCEL_SECONDS`. Mutation: drop the `wait_for` and the test must
    fail — the constant alone bounds nothing.
15. **Per-milestone recovery.** Crash after M1 and after M2: each re-run
    completes without duplicating work or re-calling the LLM.
16. **Extractor behaviour preserved across the split**, including
    preserve-on-failure.
17. **Migration round-trip.** Upgrade, cancel a job, downgrade — the downgrade
    converts the `cancelled` row before restoring the constraint. Mutation:
    drop the conversion and the downgrade must fail.
18. **Budget invariant.** The barrier test extended to read
    `_JOBS_CANCEL_SECONDS`.

## 15. Risks

- **Transaction splitting changes failure atomicity.** Today a failed job
  leaves no Phase-0 *database* writes — though its repo/git side effects
  already survive the rollback, since the filesystem is not transactional.
  Afterwards a crash can leave M1 and M2 committed and no rubric. §11 argues
  that is safe because re-running converges, and gate 15 pins it per boundary,
  but every boundary is a new partial state and the existing
  rollback-then-fail path must be re-checked against each one.
- **A hot Call dead-letters rather than completing.** §6.1 makes repeated
  re-ingestion consume attempts, which is the right trade — a bounded failure
  the owner can retry beats a spin — but it means a call being actively
  re-scraped can exhaust a workspace's setup attempts through no fault of the
  owner. The `last_error` names the cause so the retry route is an obvious
  remedy.
- **M1's lock covers subprocess calls.** Hermetic git plus per-command timeouts
  plus the mutex make this bounded rather than merely fast (§4), but M1 remains
  the one place where external processes run inside a database lock, and any
  step added there inherits that constraint.

## 16. Footprint

One Alembic migration (`0061`: status CheckConstraint with a data-converting
downgrade, plus the partial unique index on `rubric_initialized`);
`Phase0Worker` converted from a session to a session_factory with three
per-milestone transactions; M1 taking `acquire_commit_mutex_live` before the
row lock, with hermetic git and per-command `subprocess.run` timeouts;
`ensure_*_fresh` split into `compute_*`/`persist_*` with their tests moved to
the new shape; the run-local pinned Call hash with workspace-then-Call lock
ordering and a typed `CallSourceChanged` routed through the existing retry
machinery; the M3/status atomic transaction; pre-compute liveness checks; a
`live_ws` filter on the setup claim; the failure path's terminal-vs-requeue
fix; the `"jobs"` coordinator hook with `wait_for` enforcement; and an
independently-scheduled reconciliation tick.

Slice C is amended in the same series: it inherits the `"jobs"` hook rather
than introducing it, adds claim-analysis to it, and drops the incorrect
scrub-resurrection premise (§1).
