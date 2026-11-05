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
