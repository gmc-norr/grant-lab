# Phase-0 retirement and transaction boundaries

Date: 2026-08-11
Status: approved (design); implementation plan to follow
Relationship: **prerequisite for** `docs/superpowers/specs/2026-08-11-workspace-retirement-slice-c-design.md`

## 1. Why this slice exists

Deleting a workspace can bring it back.

`soft_delete_workspace_db` commits, `scrub_workspace_bytes` removes the
workspace's files, and a Phase-0 setup job that claimed before the delete then
runs `_snapshot_and_init_repo`, whose first act is
`repo_dir.mkdir(parents=True, exist_ok=True)` followed by `git init` and an
initial commit (`phases/setup.py:226`). The scrubbed tree is recreated. This is
a resurrection, not a leak, and it is the most severe defect found across four
review rounds of the Slice C design.

The obvious fix — take the workspace row lock in `_snapshot_and_init_repo` —
is worse than the bug. `run_one` commits immediately after the claim to release
the claim lock, then calls `_run_steps` on that *same* session, whose next
commit comes only after every step finishes (`setup.py:155`), including two LLM
extractors. A `FOR UPDATE` taken inside `_snapshot_and_init_repo` would be held
across all of Phase 0. And because `acquire_commit_mutex` restores
`SET LOCAL lock_timeout = DEFAULT` after acquiring (`locking.py:201`), the
delete transaction's subsequent row UPDATE has **no timeout** — DELETE would
block indefinitely behind a Phase-0 job.

Making the lock safe means giving Phase 0 real transaction boundaries, which is
why this is its own slice rather than a paragraph in Slice C.

## 2. Scope

In scope: the `live_ws` filter on the setup claim; splitting Phase 0 into
per-milestone transactions with a bounded lock; separating extractor
computation from persistence; per-milestone idempotency and recovery; a
`cancelled` job status; and the `"jobs"` retirement hook.

Out of scope: everything Slice C owns (the pass registry, claim-analysis, the
settings/rubric mutex routes, the `soft_delete_workspace` wrapper removal).

## 3. The three milestones

| milestone | work | touches FS? | LLM? |
|---|---|---|---|
| **M1** `_snapshot_and_init_repo` | `mkdir` + `git init` + three empty artifacts + initial commit; then `call_body_snapshot`, `profile_desc_snapshot`, `snapshot_refreshed_at` | **yes** | no |
| **M2** `_ensure_section_extracts_snapshot` | section extracts → `CallSectionExtract` rows + `ws.section_extracts` | no | yes |
| **M3** `_ensure_rubric_snapshot` | rubric → `CallRubric` + `ws.rubric_*` + a `rubric_initialized` `WorkspaceEvent` | no | yes |

After M3 the job moves to `waiting_for_rubric_review`; the owner's `activate`
call advances it to `done`.

Note that M2 and M3 write their extractor rows against the **Call**, not the
workspace — those rows are a shared cache legitimately kept after a workspace
is deleted. Only the snapshot copies onto the workspace are workspace-scoped.
That keeps the cancellation surface small: nothing needs unwinding.

## 4. Only M1 takes the lock

M1 is the only milestone that touches the filesystem, so it is the only one
that needs to be linearized against delete.

M1 becomes: open a session → load the workspace `with_for_update` → abort if
`soft_deleted_at` is set → do the FS work and the snapshot fields → commit,
releasing the lock.

**The lock is held across the FS work deliberately.** That is what makes the
guarantee hold: either the delete waits for M1, or the delete wins and M1
aborts having created nothing. Checking liveness under the lock and *then*
releasing before writing would restore the exact race this slice exists to
close.

The cost is bounded because the work inside the lock is `git init`, three
zero-byte files and one commit against a fresh repo — local disk, no network,
no LLM. It is nonetheless **I/O inside a lock**, so its duration is pinned by a
test rather than asserted here, mirroring the rule adopted for Slice C's
pass prefixes.

M2 and M3 hold no lock during their LLM calls (§5).

## 5. Compute out, persist in

`ensure_section_extracts_fresh` and `ensure_rubric_fresh` each take a session
and perform the LLM call *and* the database mutation as one unit under a
"caller commits" contract (`extractors/refresh.py:40`, `:96`). Short result
transactions are impossible while that holds.

Each splits into:

- `compute_*` — pure: takes the call text and a client, returns a payload.
  No session.
- `persist_*` — takes a session and a payload, writes rows. No LLM.

The freshness guard (`source_text_hash` + `extractor_version`) is evaluated
before computing, so the "skip when fresh" behaviour is preserved and still
avoids the LLM call entirely.

M2 and M3 therefore run: **compute with no transaction open** → open a short
session → lock the workspace row → re-check liveness → persist + snapshot →
commit.

This is safe to do: Phase 0 is the **only production caller** of either
function. The thirteen call sites in `tests/proposal_assist/test_extractors_refresh.py`
are the extractors' own tests and move to the new shape, preserving each
behaviour they currently assert (first-time create, skip-when-fresh,
re-extract on version bump, re-extract on source change, and — importantly —
*preserve existing rows when the extractor raises*, which the compute/persist
split makes structural rather than incidental).

## 6. Session per milestone

`Phase0Worker` currently holds a `session` as a dataclass field, used by every
step. It takes a `session_factory` instead, and each milestone opens, uses and
closes its own session.

This is a larger diff than committing between milestones on one session — every
step method changes — but it removes the long-lived session rather than working
around it. A future step that takes a lock then cannot recreate this bug by
accident, because there is no session left that spans the job.

## 7. Recovery

**No progress column is added.** Recovery is "re-run from M1", and each
milestone is a no-op when its work is already done:

- **M1** already checks `git rev-list --count HEAD` to detect a crash between
  `git init` and the initial commit, and rewrites the artifact files with
  `write_text("")`, which is idempotent by construction.
- **M2/M3** skip the LLM call entirely when `(source_text_hash,
  extractor_version)` matches, and their writes are upserts.

**One exception, and this slice creates it.** The `rubric_initialized`
`WorkspaceEvent` appended by M3 is *not* idempotent. Today that is invisible:
a failure rolls the whole job back, so the event can never double. Once M3
commits on its own, a crash after M3 but before the job's terminal status
re-runs M3 and appends a **second** event. That event is the canonical starting
state for forward-replay of rubric edits, so a duplicate corrupts the replay.

M3 therefore appends the event only when no `rubric_initialized` event exists
for that workspace. This is a genuine behaviour change caused by the split, and
it gets its own gate.

## 8. Cancellation

**New status `cancelled`** — Alembic migration `0061`, extending the
`ck_workspace_setup_jobs_status` CheckConstraint to
`('queued','running','waiting_for_rubric_review','done','failed','cancelled')`.
Reusing `failed` was considered and rejected: a deleted workspace's setup would
be indistinguishable from a genuinely broken one in logs and metrics.

Four changes:

1. **`live_ws` filter on `claim_next_setup_job`**, matching the four pass claim
   queries. Durable in SQL, survives restart.
2. **Liveness re-check at each milestone**, under M1's lock and in M2/M3's
   short persist transactions.
3. **The `"jobs"` retirement hook** (§9) sets active setup jobs to `cancelled`
   after the delete commits.
4. **The failure path must not resurrect a cancelled job.** `run_one`'s
   `except` handler currently sets `status = "queued"` whenever
   `attempts < MAX_SETUP_ATTEMPTS` (`setup.py:181-195`), which would send a
   cancelled job straight back to the queue. It must write a terminal status
   when the job was cancelled or its workspace deleted.

`requeue_stuck_setup_jobs` needs no change: `cancelled` is terminal, so its
`status == "running"` predicate cannot touch it. A job whose cancel was
*skipped* (§9) stays `running`, gets requeued, and is then refused by the
claim filter — inert, and converged by the hook's retry.

## 9. The `"jobs"` retirement hook

This slice introduces the hook; Slice C extends it with claim-analysis.

- **A required coordinator hook.** `REQUIRED_HOOKS` becomes
  `("turns", "collab", "jobs")`, so an unwired cancel fails at boot — the
  structural guard that previously caught an unwired turns hook.
- **Runs its UPDATE via `asyncio.to_thread`**; the coordinator is async and
  this is a sync DB write.
- **`FOR UPDATE SKIP LOCKED`.** A row held by a live worker is skipped, never
  waited on, so the hook cannot inherit a worker's transaction lifetime. In
  practice skips should be rare here — `run_one` releases the claim lock
  immediately — but the guarantee must not depend on that.
- **Skipped rows are retried** by a reconciliation tick, and the worker's own
  per-milestone liveness check aborts it meanwhile.
- **Budget `_JOBS_CANCEL_SECONDS = 1.0`**, a timeout for pathology rather than
  expected waiting. Hooks run concurrently, so
  `max(collab 3.0, turns 5.0, jobs 1.0) = 5.0 < 6.0` — the barrier constant is
  unchanged.

## 10. Open residuals

1. **Phase 0 cannot stop a running extractor call.** It is sync code in
   APScheduler's thread pool with no cancellation primitive, so a delete
   landing mid-extraction waits for that call to return. The milestone
   boundaries bound the residual to one extractor call; they do not remove it.
2. **A setup job row held by a live worker is skipped**, not cancelled, until
   the reconciliation tick retries (§9). Bounded by the worker's own liveness
   check, not by the delete.
3. **`CallSectionExtract` / `CallRubric` rows persist** after a workspace is
   deleted. Intentional — they are Call-scoped shared cache, not workspace
   data — but stated so it is not mistaken for an oversight.

## 11. Non-goals

- No change to `MAX_SETUP_ATTEMPTS`, `STUCK_SETUP_AFTER`, or the stuck-job
  reaper's behaviour.
- No change to what Phase 0 *produces* — same artifacts, same snapshots, same
  terminal states.
- No new progress/milestone column (§7).
- No change to `_RETIREMENT_BARRIER_SECONDS`.

## 12. Testing and acceptance

Acceptance is by **mutation**: each gate must be shown to fail when its fix is
reverted.

1. **No resurrection.** delete → scrub → a setup job already past its claim
   must not recreate the repo directory. Mutation: drop M1's liveness check and
   the test must fail. This is the headline gate.
2. **DELETE is not blocked by Phase 0.** A DELETE issued while Phase 0 runs its
   extractors returns within the barrier, measured as **route wall-clock**.
   Mutation: hold the workspace row lock across `_run_steps` and the test must
   fail — this is the indefinite-block case, so the gate must assert latency,
   not merely that the delete eventually committed.
3. **M1's lock is short.** The lock's hold duration is asserted bounded, so a
   future step added inside M1 that blocks (network, LLM) fails loudly.
4. **Hook wiring, end-to-end.** Replacing `main.py`'s `"jobs"` hook with a
   no-op must fail a test driving the real DELETE route against an active setup
   job.
5. **Cancelled jobs are not requeued.** A cancelled job passing through
   `run_one`'s failure path stays terminal. Mutation: restore the unconditional
   `status = "queued"` and the test must fail.
6. **Claim filter.** A deleted workspace's queued setup job is never claimed.
7. **Per-milestone recovery.** Crash after M1, after M2, and after M3: each
   re-run completes without duplicating work or re-calling the LLM.
8. **The audit event does not double.** Re-running M3 after a committed M3
   leaves exactly one `rubric_initialized` event. Mutation: remove the guard
   and the test must fail. Without this gate the split silently corrupts
   rubric-edit replay.
9. **Extractor behaviour is preserved across the split.** All five behaviours
   the current tests assert, including preserve-on-failure.
10. **Budget invariant.** The barrier test is extended to read
    `_JOBS_CANCEL_SECONDS`.

## 13. Risks

- **Transaction splitting changes failure atomicity.** This is the real risk of
  the slice: today a failed job leaves nothing behind, and afterwards a crash
  can leave a workspace with M1 and M2 committed but no rubric. §7 argues that
  is safe because re-running converges, and gate 7 pins it per boundary — but
  every new boundary is a new partial state, and the existing
  rollback-then-fail path must be re-checked against each one.
- **The `rubric_initialized` duplicate is the sharpest instance of that risk**
  and the one that does not self-heal, since a duplicate event is not
  overwritten by a later correct one.
- **M1's lock covers subprocess calls.** `git init`/`commit` are fast and
  local, but they are still external processes inside a database lock. Gate 3
  is the guard; if git ever becomes slow (a hook, a large template), the gate
  fails rather than DELETE quietly degrading.

## 14. Footprint

One Alembic migration (`0061`, status CheckConstraint); `Phase0Worker`
converted from a session to a session_factory with three per-milestone
transactions; `ensure_*_fresh` split into `compute_*`/`persist_*` with their
tests moved to the new shape; a `live_ws` filter on the setup claim; a liveness
check per milestone; the failure path's terminal-vs-requeue fix; a guard on the
`rubric_initialized` event; and the `"jobs"` coordinator hook with its
registration and budget constant.

Slice C is amended in the same series: it inherits the `"jobs"` hook rather
than introducing it, and adds claim-analysis to it.
