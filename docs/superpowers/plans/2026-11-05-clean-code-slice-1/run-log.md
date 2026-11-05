# Slice 1 run log

## Dry-run (plan writing, 2026-11-05)

These measurements were taken while the plan was written. Each task's code was first
prototyped in its own throwaway worktree at `3c1c0f31`. Then all patches were applied in
order as scratch commits in one worktree. The real run, from Task 0 on, appends its own
sections below and re-measures everything.

**At `3c1c0f31`:**
- backend `2436 passed in 54.00s`, without coverage;
- `tests/architecture/` `273 passed`;
- vitest 55 files, `530 passed`;
- mypy 120 source files, 0 errors.

**Baseline at the Task 1 scratch commit `cba5df68`:**
- backend with coverage: `2483 passed` twice (81.69 s, 81.89 s);
- `compare_uncovered.py` on the two runs, backend and frontend: exit 0,
  `0 new uncovered line(s), 0 new uncovered branch(es)`;
- `check_coverage_floors.py`: exit 0, every diff +0.0.

**Per task, at its scratch commit:** each task's own tests passed, the architecture tests
passed, and mypy and ruff were clean.
- Architecture: 273 through Task 5, then 281 from Task 6 (the lock).
- mypy: 120 files through Task 5, then 119 from Task 6.
- Task 8: 3 red tests on the test-only state, then vitest `533 passed`.

**Final tree:**
- The chain was rebuilt with every patch, including the Task 3 guard test (`1be9d2ba`).
- Backend with coverage: `2475 passed in 85.83s`.
- `compare_uncovered.py` against the baseline: exit 0, `0 new uncovered line(s), 0 new
  uncovered branch(es); 0 matched in another file of its group, 0 branch(es) in code that
  did not run before`. Only the `sources` row changed: lines 42 → 40 and branches 34 → 33
  uncovered.
- The frontend compare, from the dry-run without the guard test, which touches no
  frontend code: exit 0, with every row identical.
- Floors check against the committed floors: backend exit 1, on `sources` line 92.0 <
  92.8 and `sources` branch 83.3 < 84.6. Frontend exit 0.
- `--update` diff, from this tree's report:
  - `sources` line 92.8 → 92.0 and branch 84.6 → 83.3;
  - `web` branch 89.5 → 89.8;
  - frontend `<total>` branch 87.2 → 87.3, from the dry-run.
- Other checks:
  - `e2e:match` `40 passed`;
  - the OpenAPI drift check found no diff;
  - `gen_no_build.py --check` reported "in sync";
  - `gen_architecture.py` left `docs/ARCHITECTURE.md` unchanged;
  - lint and tsc were clean.
- **Must-reject:** with `tests/test_profiles_pubmed.py` deleted, `2464 passed`, and
  `compare_uncovered.py` exits 1 with `FAIL: 11 new uncovered line(s), 7 new uncovered
  branch(es)`, all in `profiles`.

**Gaps the dry-run found and the plan closes:**
- No test noticed a dropped structured profile field. With `"country"` dropped, 243
  tests in 19 files stayed green. Task 3 now starts with a guard test.
- No test noticed the scheduler's tick, batch or daily cap wired to the wrong value.
  Task 4 adds a guard test.
- The `APP_BASE_URL` host check ran after the database engine was created, and only the
  ws-token assertions guarded its wiring. Task 7 moves the check before the engine and
  adds a wiring test.
- `lxml` was an undeclared direct dependency of `eu_portal`, installed only through
  `trafilatura`. Task 6 declares it.

**Found, not fixed here; for piece 4:** the call page's decision code has untested
copies of rules. Each break below leaves all 530 vitest tests green:
- swapping its Save and Hide endpoints;
- its decision-in-flight guard;
- its copy of "a flip clears the note";
- Undo's binding to its decision's profile;
- Undo's busy guard;
- the error reset before a new decision;
- `undecide`'s no-profile guard.

Details are in the spec's slice 1 item 5.

## Revision after the outside plan review (2026-11-05)

The outside review found two holes in `compare_uncovered.py`. Both were fixed test-first.
A third hole of the same kind was then found and closed:
- A line still in place that is covered now excused a same-text line that lost its tests.
  Now a before item still in place matches there or nowhere.
- A line still in place that lost its tests could be excused by a deleted same-text line.
  Now an after item still in place matches there or is new.
- A frontend report that left out a surviving file passed. Now it exits 2.

The tool's tests went from 47 to 59. The dry-run numbers above were measured with 47;
the plan now carries the numbers below.

**Re-measured on the full chain** (scratch commit `da6ad0d5`, all 11 patches applied
to `9a34774b`):
- backend with coverage: `2487 passed in 83.78s` (2436 + 51);
- `compare_uncovered.py` against the Task 1 baseline report (`cba5df68`): exit 0,
  `0 new uncovered line(s), 0 new uncovered branch(es)`;
- must-reject, with `tests/test_profiles_pubmed.py` deleted: `2476 passed`, exit 1,
  `FAIL: 11 new uncovered line(s), 7 new uncovered branch(es)`;
- `tests/architecture/` `281 passed`; mypy 119 files, 0 errors;
- Task 1's mutation (identity ignored): `2 failed, 57 passed`.

The tool's four real-data compares (both sections, slice and same-tree pairs) print
the same output as before the fix.

**Known limit (in the script's docstring):** when same-text lines sit close together,
the line diff picks which one stayed. A wrong pick can report a moved line as new
(fails closed) or, in one narrow case, let a lost line match. The narrow case is two
same-text lines next to each other that differ only in indentation, with one deleted.

---

## Run (2026-11-06, unattended)

Worktree `/home/parlar_ai/dev/grant-lab/.worktrees/clean-slice-1`, branch
`refactor/clean-slice-1`, created from local `master`.

**Base sha:** `5490bbf7e2c8f8d1a15a9f243123871a2688741a`

### Task 0 — starting totals, measured in this worktree

| What | Command | Measured | Plan's dry-run |
|---|---|---|---|
| Backend suite | `uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider` | `2436 passed in 54.50s` | 2436 |
| Architecture | `uv run pytest -q -p no:cacheprovider tests/architecture/` | `273 passed in 17.85s` | 273 |
| Frontend | `pnpm -C frontend run test` | `Test Files 55 passed (55)`, `Tests 530 passed (530)` | 55 / 530 |
| Types | `uv run mypy --config-file pyproject.toml` | `Success: no issues found in 120 source files` | 120 |

All four match the dry-run exactly, so the plan's deltas are carried from these numbers
unchanged. `0 skipped`, `0 xfailed` on both pytest runs.

### Task 1 — the per-line coverage check

Code commit: `fe7feb44` (`feat(scripts): compare uncovered lines before and after a
change`), four paths staged: `.github/workflows/ci.yml`, `frontend/vite.config.ts`,
new `scripts/compare_uncovered.py`, new `tests/test_compare_uncovered.py`.

**Step 1 — apply and run the new tests:**
- `git apply --index` both patches: `git status --short` showed exactly
  `M  .github/workflows/ci.yml`, `M  frontend/vite.config.ts`,
  `A  scripts/compare_uncovered.py`, `A  tests/test_compare_uncovered.py`.
- `uv run pytest -q -p no:cacheprovider tests/test_compare_uncovered.py`:
  `59 passed in 1.75s`.

**Step 2 — mutation check (M1: identity ignored, grouping collapses to count-only):**
- backed up with `cp scripts/compare_uncovered.py /tmp/s1-cu.bak`;
- mutated `out[(item.group, item.kind, item.identity)][item.path].append(item)` to
  `out[(item.group, item.kind, ())][item.path].append(item)` (exactly one occurrence);
- `PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider
  tests/test_compare_uncovered.py`: `2 failed, 57 passed in 1.96s`, the two FAILED
  being exactly `test_a_deleted_line_does_not_excuse_an_added_line_of_other_text` and
  `test_a_frontend_branch_arm_is_part_of_its_identity` — matches the brief exactly;
- restored with `cp /tmp/s1-cu.bak scripts/compare_uncovered.py`, confirmed with
  `cmp /tmp/s1-cu.bak scripts/compare_uncovered.py` (no output, i.e. identical),
  printed `restored`; re-ran the suite afterwards: `59 passed in 1.72s` again, and
  `git status --short` showed no stray modification.

**Step 3 — frontend summary unchanged by the new `json` reporter:**
- `pnpm -C frontend run test:coverage`: `Test Files 55 passed (55)`,
  `Tests 530 passed (530)`; `frontend/coverage/` then listed exactly
  `coverage-final.json` and `coverage-summary.json`;
- `uv run python scripts/check_coverage_floors.py --section frontend --report
  frontend/coverage/coverage-summary.json`: `floors exit=0`, every row's diff `+0.0`.

**Step 4 — lint and commit:**
- `uv run ruff check .`: `All checks passed!`;
- `uv run ruff format --check scripts/compare_uncovered.py
  tests/test_compare_uncovered.py`: `2 files already formatted`;
- `uv run mypy --config-file pyproject.toml`: `Success: no issues found in 120 source
  files` (the global constraint's pre-Task-6 N).
- Committed as `fe7feb44`.

**Step 5 — the slice's coverage baseline, measured at this commit (no app code changed):**

Note: one more docs-only commit (`f22019eb`, this run-log entry for Steps 1–4) landed
between the code commit `fe7feb44` and this measurement; `git diff --stat
fe7feb44..HEAD` shows only `run-log.md` changed, so the baseline is identical to one
measured at `fe7feb44`.

- **BASE** = `f22019eb823333383f19f008fe613031b8b6a618` (`/tmp/s1-base-rev`).
- Backend with coverage, run twice: `2495 passed in 80.29s` and `2495 passed in
  80.32s` (2436 + the 59 new tests in `tests/test_compare_uncovered.py`, matching the
  brief's expectation exactly). `0 skipped`, `0 xfailed` both times. The two
  `--cov-report=json` files are not byte-identical (coverage.py's `meta` carries a
  timestamp), but every one of the 120 measured files' `missing_lines` and
  `missing_branches` matches between the two runs — checked directly and via the tool.
- `compare_uncovered.py --section backend --before s1-base-be-1.json --before-rev
  $BASE --after s1-base-be-2.json --after-rev $BASE`: **exit 0**,
  `OK: 0 new uncovered line(s), 0 new uncovered branch(es); 0 matched in another file
  of its group, 0 branch(es) in code that did not run before`. Per-group uncovered
  counts (before = after on every row): `<root>` 62 lines/20 branches, `auth` 28/14,
  `boards` 12/5, `db` 0/0, `digest` 6/3, `email` 4/0, `embeddings` 7/7, `ingestion`
  33/11, `profiles` 57/29, `ranking` 70/39, `sources` 42/34, `web` 68/56 — matching the
  "Final tree" dry-run's `sources` figures (42 lines, 34 branches) exactly.
- Frontend: `pnpm -C frontend run test:coverage` → `Test Files 55 passed (55)`,
  `Tests 530 passed (530)`; coverage summary `Statements 93.88% (4378/4663)`,
  `Branches 87.29% (1257/1440)`, `Functions 87.3% (392/449)`, `Lines 93.88%
  (4378/4663)` — unchanged from Task 0 (no frontend app code changed in Task 1).

**Durable copies** (`/tmp` can be cleared; Task 9 needs these), copied byte-for-byte
to `/home/parlar_ai/grant-lab-s1-run/reports/` under the same file names:
`s1-base-rev`, `s1-base-be-1.json`, `s1-base-be-2.json`, `s1-base-fe.json`,
`s1-base-fe-summary.json`. Verified present with `ls -la`.
