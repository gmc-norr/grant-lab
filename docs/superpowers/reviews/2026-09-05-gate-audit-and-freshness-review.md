# Grant Lab Review: 05bd151c

Reviewed 2026-09-05. Target: `05bd151c511d97e91ef180da557b7c7213d411f6`, `master`.

## Findings

### 1. P1: Profile edits can permanently lose matching refresh work

**Editions: match and full.** This reproduces brief finding 2, including its previously unverified crash window. A separate deterministic empty-description case also reproduces.

The update handler commits the profile before scheduling an in-memory background task at [src/grant_lab/web/api/profiles.py:454](../../../src/grant_lab/web/api/profiles.py#L454). Neither invalidating the old vector nor recording pending matching work is part of that transaction. The background refresh calls the embedding helper without `force=True` at [src/grant_lab/profiles/post_edit.py:17](../../../src/grant_lab/profiles/post_edit.py#L17). After an embedding error, a non-NULL vector with the current model name remains invisible to the daily selector at [src/grant_lab/embeddings/refresh.py:322](../../../src/grant_lab/embeddings/refresh.py#L322).

Database-backed reproductions:

- **Outage:** an injected embedding failure retains the old vector; the subsequent healthy daily tick makes zero profile embedding calls. Nulling the vector/model in the control causes that same tick to repair it.
- **Process exit:** a spawned process executes the real update handler and exits with `os._exit(77)` before executing its BackgroundTasks. The parent observes the committed description, the old vector and zero ranking jobs after the daily repair. The completed-background-task control has the new vector and a ranking job.
- **No crash required:** clearing the description while retaining embeddable areas skips the entire background task at [src/grant_lab/web/api/profiles.py:456](../../../src/grant_lab/web/api/profiles.py#L456). The handler saves the edit, but neither re-embeds nor backfills.

**Impact:** successful edits can leave matching based on superseded profile data. A healthy embedding service on the next daily run does not repair the demonstrated state. A manually forced refresh/backfill remains a possible intervention; this is not a claim that no intervention could repair it.

**Action:** preserve the already-decided durability fix, and include the blank-description/remaining-areas case in its acceptance coverage. The requirement is durable invalidation and recoverable matching work for every relevant edit, not merely a background-task scheduling assertion.

**Not verifiable here:** production incidence, affected-row counts and how frequently the crash window occurs. No live database was surveyed.

Evidence: command E1 below, `3 failed, 2 passed`. The failures assert the desired repaired state; both distinguishing controls pass.

### 2. P1: A rolled-back acceptance can remain in Git and become a reconciled version

**Edition: full, collab-enabled proposal acceptance.** This is a concrete reproduction of brief finding 5, not a claim about every Git/Postgres write path.

The kernel appends its Yjs update without committing, then checkpoints within the same database transaction: [src/grant_lab/proposal_assist/collab/kernel.py:301](../../../src/grant_lab/proposal_assist/collab/kernel.py#L301). The checkpoint performs the independent Git commit at [src/grant_lab/proposal_assist/collab/checkpoint.py:244](../../../src/grant_lab/proposal_assist/collab/checkpoint.py#L244). A subsequent failure can therefore roll back the document update and proposal status without undoing Git.

The real async acceptance wrapper evicts the room after the failure at [src/grant_lab/proposal_assist/proposals/kernel_accept.py:131](../../../src/grant_lab/proposal_assist/proposals/kernel_accept.py#L131). That repairs in-memory state, not the Git commit. The read contract explicitly makes the durable Yjs log canonical at [src/grant_lab/proposal_assist/collab/canonical.py:3](../../../src/grant_lab/proposal_assist/collab/canonical.py#L3).

The probe injects an exception immediately after the real Git commit, then runs Git-drift reconciliation and a fresh checkpoint. It reproduces through both the kernel and `accept_via_kernel`, including room eviction:

```text
entrypoint=orchestrator fault=True status=pending backfilled=2 retry_committed=False provenance=['reconciled', 'reconciled']
durable='## Background\n\nOriginal durable body.\n' git='## Background\n\nAccepted but not durable.\n'
```

The two backfilled rows include the fixture's initial commit; only the acceptance commit is the problematic one. The successful-commit control reports `status=accepted`, and Git and the durable projection agree.

**Impact:** the declared canonical document and versioned Git content disagree after the normal rollback/eviction recovery. Reconciliation promotes the orphan acceptance into `ArtifactVersion` history without recovering the accepted status or durable edit. A subsequent checkpoint does not necessarily correct it: the watermark/unauthored-seed no-op paths at [src/grant_lab/proposal_assist/collab/checkpoint.py:215](../../../src/grant_lab/proposal_assist/collab/checkpoint.py#L215) and [src/grant_lab/proposal_assist/collab/checkpoint.py:219](../../../src/grant_lab/proposal_assist/collab/checkpoint.py#L219) allow the demonstrated disagreement to persist.

**Action:** make rollback recovery distinguish a committed collaboration checkpoint from a Git side effect of an aborted transaction. Do not treat every orphan Git commit as an accepted document version. Preserve this scenario as a regression test at the async wrapper boundary.

**Limits:** the failure was injected at the real post-Git boundary, not caused by an actual disk failure or database outage. Production frequency and effects on every downstream export/history consumer were not measured.

Evidence: E2, `2 failed, 2 passed`; both entry points fail the agreement assertion under the fault and pass without it.

### 3. P2: The whole fencing suite misses an unfenced Git commit when argv is a variable

**Edition: full. Coverage defect, not a claim that this mutant exists in production.**

[tests/fencing/git_fence.py:118](../../../tests/fencing/git_fence.py#L118) classifies `_run_git` as a commit sink only when its first argument is an inline `ast.List` beginning with `"commit"`. The `ast.List` condition at [tests/fencing/git_fence.py:129](../../../tests/fencing/git_fence.py#L129) silently drops a call whose command list is bound to a local variable.

Added only to the external snapshot, immediately after `MAX_SETUP_ATTEMPTS` in the existing setup module:

```python
def _review_unfenced_commit(repo):
    arguments = ["commit", "--allow-empty", "-m", "review-unfenced"]
    return _run_git(arguments, cwd=repo)
```

The **entire fencing suite passed: 239 passed**. Invoking that function against a disposable Git repository produced:

```text
git_exit=0 head_changed=True database_session=none fence=none
```

The identical unfenced function with an inline argument list produced **5 failed, 234 passed**. The failures name the new sink in holder-count, position, inventory, regeneration and live-mutex checks. This distinguishes a discovery failure from a harmless non-commit helper.

**Action:** unknown argv at a known Git executor must not mean "not a commit." Resolve supported local constants, or classify unknown command arguments conservatively and require an explicit reviewed exception. Add the variable-argv case to discovery and full-tree coverage tests.

Evidence: E3 and E4. Both source mutants were removed, and the snapshot file was compared byte-for-byte with the original.

### 4. P2: CI wiring gates accept jobs and steps that cannot enforce the smoke test

**Edition: match release checks. Coverage defect; the reviewed workflow was not shown to be disabled.**

[tests/architecture/test_match_gates_are_wired.py:40](../../../tests/architecture/test_match_gates_are_wired.py#L40) parses YAML, but [tests/architecture/test_match_gates_are_wired.py:49](../../../tests/architecture/test_match_gates_are_wired.py#L49) reads `run` bodies without considering job/step conditions or failure-tolerance settings.

Applied separately to a parsed temporary copy of the real workflow, each of these passes every test function in the wiring module:

- `if: false` on `match-e2e`.
- `if: false` on its smoke-test step.
- `continue-on-error: true` on the job.

```text
ci_mutation=remove-command checks=12 rejected=['test_the_match_browser_smoke_runs_in_ci']
ci_mutation=disabled-job checks=12 rejected=[]
ci_mutation=disabled-step checks=12 rejected=[]
ci_mutation=nonblocking-job checks=12 rejected=[]
```

The removed-command control is rejected, so the probe is exercising the real assertions. Parsing YAML closed the comment loophole but does not establish that a checked command will run or block failure.

**Action:** pin the execution and failure-enforcement conditions of the required jobs/steps, with explicit disabled and nonblocking controls. Do not infer execution from command text alone.

Evidence: E5.

### 5. P2: The no-import-cycles gate ignores relative import cycles

**Editions: match and full. Coverage defect; no new current production cycle is alleged.**

[tests/architecture/test_no_import_cycles.py:60](../../../tests/architecture/test_no_import_cycles.py#L60) processes `ImportFrom` only when `level == 0`. Equivalent relative imports disappear from its graph.

The probe creates the same two-module circular import using absolute and relative spellings. A fresh Python process fails to import both with a partially initialized-module `ImportError`. The scanner reports:

```text
relative=False runtime_exit=1 cycles=[['cycleprobe.first', 'cycleprobe.second']]
relative=True runtime_exit=1 cycles=[]
```

**Action:** resolve relative imports against the importing module/package, or explicitly reject unsupported import forms instead of excluding them. Keep the runtime import-failure comparison as a control.

Evidence: E5. This is the scanner's actual result on a small package, not a guessed production import failure.

## Calibration And Known Issues

**The profile structural gate overclaims, but neighboring coverage catches the demonstrated regression.** A real snapshot mutation of `update_profile` to `if False and not can_read_profile(...)` passes all **47** tests in [tests/architecture/test_profile_access_single_source.py](../../../tests/architecture/test_profile_access_single_source.py). Its predicate-use detector at [tests/architecture/test_profile_access_single_source.py:309](../../../tests/architecture/test_profile_access_single_source.py#L309) sees syntax, not reachability. However, the existing stranger-access behavior test fails correctly: the private profile returns 403 while a nonexistent profile returns 404. After restoring the guard, the same test passes. This is not evidence that the authorization behavior is globally untested. E6 records both results.

**The previously known match process-boundary problem remains on this requested commit.** The subprocess gate measures `create_app()` at [tests/architecture/test_match_edition_process_cut.py:41](../../../tests/architecture/test_match_edition_process_cut.py#L41), not lifespan startup. E7 uses a real disposable database and enters `TestClient`'s lifespan:

```text
construction={'proposal_assist': 0, 'grant_lab': 92, 'pycrdt': False, 'claude_agent_sdk': False, 'prosemirror': False, 'routes': 44}
lifespan={'proposal_assist': 45, 'grant_lab': 139, 'pycrdt': True, 'claude_agent_sdk': True, 'prosemirror': False, 'routes': 44}
```

The brief says this is fixed on another branch. That branch was not inspected. This is verification of the old finding at `05bd151c`, not a new discovery or a judgment on the branch fix.

**Other previous findings:** scheduler-lock-loss enforcement was not independently fault-injected again. The full-edition event-loop finding was not broadened to match. The review does not claim those previous issues were fixed, nor does it repeat them as newly verified findings.

## Gate Audit Ledger

All 14 architecture gate files were inspected. This table records the specific adverse input exercised, not an assertion that every conceivable mutation is covered. E5-E7 contain the external controls; the unmodified full suite also ran the repository's existing negative fixtures.

| Gate file under tests/architecture | Adverse input | Measured result |
| --- | --- | --- |
| test_architecture_map_is_current.py | Stale generated map | Rejected |
| test_auth_allowlist.py | New private route without auth dependency | Rejected |
| test_branch_entity_invariants.py | No-op branch backfill | Rejected for missing root pointer |
| test_criterion_vocabulary.py | Embedding chip removed from frontend vocabulary | Rejected |
| test_decision_routes_require_a_profile.py | Optional profile_id in generated schema | Rejected |
| test_edition_surface.py | Workspace route present in match | Rejected |
| test_investigations_index.py | Missing index links to real investigations | Rejected |
| test_match_edition_process_cut.py | Observe actual lifespan, not construction | Existing gate misses heavy imports; known issue |
| test_match_gates_are_wired.py | Disabled/nonblocking smoke job | Missed; finding 4 |
| test_no_import_cycles.py | Runtime-failing relative-import cycle | Missed; finding 5 |
| test_package_spa.py | Full bundle requested as match | Rejected |
| test_profile_access_single_source.py | Unreachable read predicate in real update route | Structural gate misses; behavior test catches |
| test_spa_edition_match.py | Full bundle paired with match backend | Rejected |
| test_workspaces_dir_isolation.py | Remove WORKSPACES_DIR test override | Rejected without writing to the default directory |

The fencing review examined sink discovery/completeness, inventory enforcement, lexical mutex rules, stamp rules, import boundaries and spend-gate limitations. The strongest new whole-suite mutation is finding 3. This was not an exhaustive mutation campaign against every workspace actor or every concurrency interleaving. In particular, a green static gate is not presented as a runtime stale-writer proof.

## Validation And Safety

The original checkout was clean at the start and end. No original project file was changed. `.worktrees/` was not entered. `.env` was not read, written or copied. Tests ran in a secretless archive of the exact commit with a separate Python environment, separate frontend dependencies and disposable testcontainers databases. `ANTHROPIC_API_KEY` was removed from execution environments; the backend's live-SDK test remained skipped.

Measured unmodified baselines:

```text
Backend:  5137 passed, 1 skipped in 142.28s (0:02:22)
Frontend: Test Files 140 passed (140); Tests 1062 passed (1062)
Ruff:     All checks passed!
```

Full browser E2E, live deployment behavior, production error frequency and backup/disaster recovery were not verified. Frontend validation here is Vitest, not a browser smoke execution. No dependency-security assessment is implied by Ruff passing.

The diagnostic tests deliberately assert the desired guarantee and go red where the application or gate violates it. These failures are separate from the green unmodified baselines. Initial external-test import-path and handler-argument mistakes were corrected and rerun; those harness errors are not used as evidence.

## Reproduction Commands

Executable probes and raw logs are retained in `/tmp/grant-lab-review-05bd151c/`. They are temporary artifacts and are not in the project. The report itself is `/home/parlar_ai/grant-lab-review-2026-09-05.md`.

The snapshot was created with this exact extraction command after creating its directory:

```bash
git archive 05bd151c511d97e91ef180da557b7c7213d411f6 | tar --extract --directory=/tmp/grant-lab-review-05bd151c/snapshot --exclude='.env' --exclude='*/.env' --exclude='api_key.txt' --exclude='.worktrees'
```

Dependency setup used `uv sync --frozen --offline` against the snapshot and a dedicated `UV_PROJECT_ENVIRONMENT`. The initial frontend offline install lacked the pinned oxlint package; `pnpm install --frozen-lockfile --ignore-scripts` completed it without lifecycle scripts. No dependency manifest was edited.

### Baselines

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -n 16 --dist loadfile -q 2>&1 | tee /tmp/grant-lab-review-05bd151c/backend-baseline.log
```

Output: `5137 passed, 1 skipped in 142.28s (0:02:22)`.

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot/frontend && set -o pipefail && env -u ANTHROPIC_API_KEY pnpm vitest run 2>&1 | tee /tmp/grant-lab-review-05bd151c/frontend-baseline.log
```

Output: `Test Files 140 passed (140)`, `Tests 1062 passed (1062)`.

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline ruff check src tests alembic scripts 2>&1 | tee /tmp/grant-lab-review-05bd151c/lint-baseline.log
```

Output: `All checks passed!`.

### E1: Freshness

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY PYTHONPATH="$PWD" UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -q -s --tb=short /tmp/grant-lab-review-05bd151c/test_review_freshness.py 2>&1 | tee /tmp/grant-lab-review-05bd151c/freshness.log | tail -n 40
```

Relevant output:

```text
invalidate=False repair_calls=0 vector=[np.float32(1.0), np.float32(0.0)]
invalidate=True repair_calls=1 vector=[np.float32(0.0), np.float32(1.0)]
mode=crash child_exit=77 description='Updated neuroscience' vector=[1.0, 0.0] jobs=0
mode=complete child_exit=0 description='Updated neuroscience' vector=[0.0, 1.0] jobs=1
mode=clear child_exit=0 description='' vector=[1.0, 0.0] jobs=0
3 failed, 2 passed in 12.03s
```

The first two displayed lines omit the trailing summary dictionary; the complete lines are in the raw log.

### E2: Git/Postgres Recovery

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY PYTHONPATH="$PWD" UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -q -s --tb=short /tmp/grant-lab-review-05bd151c/test_review_recovery.py 2>&1 | tee /tmp/grant-lab-review-05bd151c/recovery.log | tail -n 45
```

Output: `2 failed, 2 passed in 6.69s`. Both failures are the explicit Git-versus-durable-document agreement assertion, under the injected post-Git failure. See finding 2 for the exact state output.

### E3: Whole Fencing Suite

With the variable-argv function shown in finding 3 present only in the snapshot:

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -q tests/fencing 2>&1 | tee /tmp/grant-lab-review-05bd151c/fencing-mutant.log
```

Output: `239 passed in 38.29s`.

Changed only its body to `return _run_git(["commit", "--allow-empty", "-m", "review-unfenced"], cwd=repo)` and ran:

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -q --tb=short tests/fencing 2>&1 | tee /tmp/grant-lab-review-05bd151c/fencing-control.log | tail -n 30
```

```text
FAILED tests/fencing/test_f3e1a2_slice.py::test_the_real_tree_still_has_exactly_nine_sink_holders
FAILED tests/fencing/test_f3e1a2_slice.py::test_every_real_sink_gets_a_credited_position
FAILED tests/fencing/test_fence_coverage.py::test_the_inventory_matches_the_tree_exactly
FAILED tests/fencing/test_inventory_merge.py::test_regeneration_preserves_every_human_slice_assignment
FAILED tests/fencing/test_live_mutex_gate.py::test_the_tree_has_no_unpinned_live_caller
5 failed, 234 passed in 38.31s
```

The mutants are no longer present. Rerunning these commands without reintroducing the stated snapshot-only mutation checks the restored baseline instead. E5 retains a directly executable minimal version of the discovery comparison.

### E4: Real Git Effect

With the variable-argv snapshot mutant still present, the exact invocation was:

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && env -u ANTHROPIC_API_KEY PYTHONPATH="$PWD" UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline python -c 'from pathlib import Path
from tempfile import TemporaryDirectory
from tests.proposal_assist.collab.test_collab_checkpoint import _init_repo
from grant_lab.proposal_assist.citations.gitio import current_head
from grant_lab.proposal_assist.phases.setup import _review_unfenced_commit
with TemporaryDirectory() as directory:
    repo = Path(directory)
    _init_repo(repo, "## Review\n\nInitial content.\n")
    before = current_head(repo)
    result = _review_unfenced_commit(repo)
    after = current_head(repo)
    print(f"git_exit={result.returncode} head_changed={before != after} database_session=none fence=none")
    assert result.returncode == 0 and before != after'
```

Output: `git_exit=0 head_changed=True database_session=none fence=none`.

### E5: Gate Mutations

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY PYTHONPATH="$PWD" UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -q -s --tb=short /tmp/grant-lab-review-05bd151c/test_review_gates.py 2>&1 | tee /tmp/grant-lab-review-05bd151c/gate-probes.log
```

Output: `6 failed, 4 passed in 2.57s`. The failed desired-guarantee assertions are the three inert CI variants, the relative-import cycle, the dead access predicate and the variable-argv Git sink. Removed-command, absolute-cycle, live-access and literal-argv controls pass.

### E6: Profile Coverage Cross-Check

With the snapshot's real `update_profile` read guard changed to `if False and not can_read_profile(session, user, p.id):`:

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -q tests/architecture/test_profile_access_single_source.py 2>&1 | tee /tmp/grant-lab-review-05bd151c/profile-architecture-mutant.log
```

Output: `47 passed in 3.00s`.

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -q --tb=short tests/test_profile_access.py::test_gate6_update_does_not_leak_existence_to_a_stranger 2>&1 | tee /tmp/grant-lab-review-05bd151c/profile-behavior-mutant.log | tail -n 15
```

Output: `1 failed in 5.97s`; logged responses are 403 for the private existing profile and 404 for the missing one. The same behavior test after restoration: `1 passed in 5.90s`.

### E7: Remaining Controls And Real Startup

```bash
cd /tmp/grant-lab-review-05bd151c/snapshot && set -o pipefail && env -u ANTHROPIC_API_KEY PYTHONPATH="$PWD" UV_PROJECT_ENVIRONMENT=/tmp/grant-lab-review-05bd151c/venv uv run --frozen --offline pytest -q -s --tb=short /tmp/grant-lab-review-05bd151c/test_review_controls.py 2>&1 | tee /tmp/grant-lab-review-05bd151c/gate-controls.log | tail -n 35
```

Output: `1 failed, 10 passed in 11.38s`. The sole failure is the previously known match lifespan import leak. Each of the other controls confirms rejection of the adverse input listed in the ledger.

### Final Worktree Check

```bash
git -C /home/parlar_ai/dev/grant-lab status --short && cmp /home/parlar_ai/dev/grant-lab/src/grant_lab/proposal_assist/phases/setup.py /tmp/grant-lab-review-05bd151c/snapshot/src/grant_lab/proposal_assist/phases/setup.py && cmp /home/parlar_ai/dev/grant-lab/src/grant_lab/web/api/profiles.py /tmp/grant-lab-review-05bd151c/snapshot/src/grant_lab/web/api/profiles.py
```

Successful, no output: original worktree clean; both mutated snapshot source files restored exactly.
---

# Verification and disposition — added 2026-09-05 by the session that received this review

The reviewer's text above is unedited except that its source links were rewritten for this
directory (`src/…` → `../../../src/…`) and the file was renamed, because
`grant-lab-review-2026-09-05.md` collided with two other reviews from the same day. Its
own temporary artefacts under `/tmp/grant-lab-review-05bd151c/` are not preserved.

This was the **second** pass by this reviewer, run against a brief that told it to stop
surveying and start reproducing. The difference is worth recording: the first pass
produced five findings from reading, of which one was overstated and one unverified; this
one worked from a `git archive` snapshot with its own environment and disposable
databases, ran the full suite, and reproduced its own two open findings **including the
crash window it had previously left unverified**.

**Every disposition below says how it was reached.** Reading the code is not running it,
and this file should not blur the two.

| # | Finding | How checked here | Disposition |
| --- | --- | --- | --- |
| 1 | Lost refresh/ranking work | **Read**, incl. a new case | **CONFIRMED — scheduled** |
| 2 | Rolled-back accept survives in Git | **Not re-run here** | **Reviewer-reproduced — deferred** |
| 3 | Fencing misses variable argv | **Read + mutant reproduced** | **FIXED** `77e746eb` |
| 4 | CI gate accepts inert jobs | **Read** | **FIXED** `77e746eb` |
| 5 | Cycle gate ignores relative imports | **Read + measured** | **FIXED** `77e746eb` |

## 1 — CONFIRMED by reading, and the sharpest part is new

The reviewer's third sub-case is the one to act on, because it needs no crash and no
outage. `web/api/profiles.py` computes the correct signal and then guards it with the
wrong one:

```python
new_embed_text = build_profile_text(p)
embed_text_changed = old_embed_text.strip() != new_embed_text.strip()
session.commit()

if body.description.strip():          # <-- the wrong predicate
    background.add_task(... refresh_embedding=embed_text_changed)
```

`build_profile_text` is *"description + `areas` + org fields"*
(`embeddings/similarity.py:59`). So clearing the description on a profile that still has
areas commits an edit whose embed text really did change, schedules **nothing**, and
leaves a vector that is neither NULL nor model-mismatched — which is exactly what the
daily repair selects on. Deterministic, no fault injection required.

**Not re-run here:** the outage and process-exit reproductions. The reviewer's E1 output
is the evidence for those, and it is consistent with what the code plainly does.

**Disposition.** Folds into the freshness fix already decided in
`2026-09-05-runtime-ownership-and-state-consistency-review.md` — hash `build_profile_text`
onto the row, add one arm to the repair predicate — with **this case added to its
acceptance coverage**, as the reviewer asks. Still sequenced after Plan C, because both
need a migration and two branches numbering off one head is a collision this repo has
already paid for. The no-crash case raises the urgency; it does not make the work
parallelisable.

## 2 — Reviewer-reproduced, deferred, and deliberately not re-run here

This is the first reproduction of the Git/Postgres window, at both the kernel and the
`accept_via_kernel` boundary, with room eviction, and with a successful-commit control
that agrees. The sharp part is not the disagreement itself but what happens next:
reconciliation promotes the orphan Git commit into `ArtifactVersion` history without
recovering the accepted status or the durable edit.

**Deferred, not dismissed.** It is entirely within proposal-assist, which release 1 does
not ship, and the fix is a design question — what "recoverable" means across two stores —
rather than a patch. It deserves its own decision. The reviewer is explicit that the fault
was injected at the real post-Git boundary rather than caused by a real outage, and that
production frequency was not measured.

## 3, 4, 5 — FIXED in `77e746eb`

All three were confirmed by reading first, then fixed, then **proved by reverting the fix
and checking that only the right cases go red**. Twenty new tests, 5137 → 5157.

- **3 (fencing, variable argv).** The reviewer's exact mutant was reproduced here after
  the fix and now reddens the same five tests across four files that its inline-list
  control did. `_run_git` was measured across `src/`: 1 inline-commit call, 3 inline
  non-commit, **0 variable-argv** — so failing closed reclassifies nothing that exists.
- **4 (CI wiring).** Fixed in the parser rather than by adding parallel assertions, so
  every existing check inherits it. Any `if:` disqualifies a node from carrying a gate,
  scoped to the nodes whose commands the checks depend on — the trace-upload step's
  `if: failure()` is legitimate and shows why a file-wide ban would be wrong.
- **5 (relative-import cycles).** Not hypothetical: **four** relative imports live in
  `proposal_assist/collab/`. The real tree still reports zero cycles after the fix, so
  nothing was hiding behind them. This was the **second** hole in that gate in one week;
  the first was found by validating it against a tree where the failure was reproducible,
  this one by writing the same cycle a different way.

## The calibration item, and what is still unchecked

The reviewer also showed that `tests/architecture/test_profile_access_single_source.py`
passes all **47** of its cases when the real `update_profile` read guard is mutated to
`if False and not can_read_profile(...)` — its detector sees syntax, not reachability —
while the behavioural test at `tests/test_profile_access.py` correctly fails. **Not
re-verified here.** It is a genuine overclaim by a structural gate, and it is the natural
next target after these three, but the reviewer was careful to show the behavioural
coverage that catches it rather than presenting it as an open hole.

Also unchecked here, and named by the reviewer as unchecked: production incidence for
findings 1 and 2, scheduler-lock fault injection (still open from the previous review),
browser E2E, and any dependency-security claim.

## What this review did that the first one did not

Recorded because it is the difference worth asking for next time. It worked from a
snapshot rather than the live tree and proved the tree was untouched at the end; it ran
the baselines and matched them exactly (**5137 passed, 1 skipped**; 1062 frontend); it
gave a named adverse input for **all 14** architecture gate files, not only the ones that
failed; it scoped every finding to an edition; it stated plainly which of its own probes
were fault-injected rather than naturally occurring; and it flagged its own harness errors
as excluded from the evidence.
