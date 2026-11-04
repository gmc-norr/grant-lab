# Piece 2 run log

Every number measured while executing `docs/superpowers/plans/2026-11-04-safety-tools.md`,
in the task that measured it.

## Task 0 — baseline

Base: `2e359466` (master, the commit that added the plan), worktree
`.worktrees/safety-tools`, branch `chore/safety-tools`.

- `uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider` → `2294 passed in 55.92s`
- `pnpm exec vitest run` → `Tests  530 passed (530)`
- `pnpm run e2e:match` → `40 passed (39.8s)`

## Task 1 — tools and mypy settings

Step 1 (`uv add --dev mypy types-lxml pytest-cov`, then `gen_no_build.py`):

```
 + coverage==7.17.0
 + mypy==2.4.0
 + pytest-cov==7.1.0
 + types-lxml==2026.2.16
Updated no-build-package with 125 packages.
```

Step 4 (`uv run mypy --config-file pyproject.toml`, last line):

```
Found 65 errors in 28 files (checked 120 source files)
```

Step 4 (`uv lock --check`, `gen_no_build.py --check`):

```
Resolved 126 packages in 1ms
no-build-package is in sync with uv.lock.
```

Step 4 (`pytest tests/test_gen_no_build.py`):

```
8 passed in 0.06s
```

## Task 2 — ignore reasons, web and root fixes

Step 2 (RED, before patches): `1 failed, 6 passed in 0.19s`; first extra item
`'src/grant_lab/config.py line 484: # type: ignore[prop-decorator]'` (3 items).

Step 3: `Found 40 errors in 19 files (checked 120 source files)`, then `7 passed in 0.14s`.

Step 4: `1 file left unchanged`, then `22 passed in 0.39s`.

Step 5 (mutations on net_safety.py, restored with cp + cmp):

```
mutant 1 (sleep removed):  1 failed, 21 deselected in 0.66s   -> restored
mutant 2 (timeout dropped): 1 failed, 21 deselected in 0.16s   -> restored
```

Both fail test_the_pinned_backend_forwards_every_argument_and_every_backend_method.
`git diff --stat` after restore equals the post-Step-3 state: 40 insertions, 11 deletions.

Step 6: `"httpcore>=1.0.9",` at pyproject.toml line 27; `no-build-package is in sync with uv.lock.`;
uv.lock gained only two `httpcore` lines (dependencies + requires-dist), no version changes.

Step 7: `no drift`.

Step 8: `354 passed in 31.92s`.

## Task 3 — ranking and boards

Step 1 (both patches applied with `git apply`, no conflicts):

```
Found 18 errors in 9 files (checked 120 source files)
```

Step 2 (ranking and boards tests, `-n 4 --dist loadfile`):

```
505 passed in 19.34s
```

## Task 4 — mypy reaches zero

Step 1 (`uv run mypy --config-file pyproject.toml | tail -2`, after fix-ingestion and fix-small):
```
src/grant_lab/scheduler.py:426: error: Argument "alerter" to "run_ingestion" has incompatible type "ConsecutiveFailureTracker | _Alerter"; expected "ConsecutiveFailureTracker"  [arg-type]
Found 1 error in 1 file (checked 120 source files)
```

Step 2 (after fix-alerter, `tail -1`):
```
Success: no issues found in 120 source files
```

Step 3 (`uv run ruff check . | tail -1`):
```
All checks passed!
```

Step 3 (`uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider | tail -1`):
```
2302 passed in 49.92s
```
No skipped, xfailed or failed in the summary line.

## Task 5 — coverage-floors checker

- RED (Step 2): `uv run pytest -q -p no:cacheprovider tests/test_check_coverage_floors.py` on the tests alone. Collection ERROR, `FileNotFoundError: [Errno 2] No such file or directory: '.../scripts/check_coverage_floors.py'` raised at `tests/test_check_coverage_floors.py:22` during `exec_module`. `Interrupted: 1 error during collection`, exit 2.
- GREEN (Step 3): `uv run pytest -q -p no:cacheprovider tests/test_check_coverage_floors.py` gives `19 passed in 0.08s`. `ruff check` gives `All checks passed!`. `ruff format --check` gives `2 files already formatted`.
- Full suite: `uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider` gives `2321 passed in 49.61s` (2302 + 19).

### Task 5 — review fixes

- Tests: 19 -> 41 (22 added: 6 empty-report, 2 wrong-container, 9 malformed-floors, 1 difference column, 4 missing-coverage).
- RED (tests added before the fix): `18 failed, 23 passed`. The 18 were the bad-input cases (traceback / exit 1 instead of exit 2) and the difference-column test. The 4 "missing" tests passed on the unchanged code (23 = 19 + 4), as expected: they pin behaviour that already worked.
- GREEN: `41 passed` (the old row assertion in `test_a_report_at_its_floors_passes` was updated for the new diff column).
- Mutants, each leaving exactly one test red, restored with `cp` and confirmed with `cmp`:
  - gained-only branch check -> `test_a_group_that_lost_branches_fails`
  - delete the one-report guard -> `test_checking_takes_exactly_one_report`
  - delete the no-floors-file guard -> `test_checking_without_a_floors_file_is_refused`
  - `rpartition` -> `partition` -> `test_a_frontend_path_that_repeats_frontend_src_groups_by_the_last_one`
- ruff check/format --check clean; mypy `Success: no issues found in 120 source files`.
- Full suite: `2343 passed in 50.03s` (2321 + 22).

## Task 6 — local coverage

### local run 1, backend

TOTAL                                         7877    389   1906    190    94%
2343 passed in 80.74s (0:01:20)

### Coverage: backend

| group | line % | floor | diff | branch % | floor | diff |
|---|---|---|---|---|---|---|
| <root> | 91.0 | - | - | 87.8 | - | - |
| <total> | 95.0 | - | - | 88.5 | - | - |
| auth | 93.4 | - | - | 87.7 | - | - |
| boards | 96.1 | - | - | 92.6 | - | - |
| db | 100.0 | - | - | 100.0 | - | - |
| digest | 97.5 | - | - | 94.6 | - | - |
| email | 96.6 | - | - | 100.0 | - | - |
| embeddings | 97.7 | - | - | 93.5 | - | - |
| ingestion | 89.5 | - | - | 84.2 | - | - |
| profiles | 89.9 | - | - | 83.3 | - | - |
| ranking | 94.2 | - | - | 89.1 | - | - |
| sources | 92.8 | - | - | 84.6 | - | - |
| web | 97.0 | - | - | 89.5 | - | - |

exit=0

### local run 1, frontend

      Tests  530 passed (530)
Branches     : 87.33% ( 1262/1445 )
Lines        : 93.91% ( 4397/4682 )

### Coverage: frontend

| group | line % | floor | diff | branch % | floor | diff |
|---|---|---|---|---|---|---|
| <root> | 86.2 | - | - | 93.9 | - | - |
| <total> | 93.9 | - | - | 87.3 | - | - |
| api | 97.8 | - | - | 95.2 | - | - |
| components | 90.9 | - | - | 88.8 | - | - |
| routes | 93.3 | - | - | 85.0 | - | - |
| shell | 100.0 | - | - | 100.0 | - | - |
| theme | 100.0 | - | - | - | - | - |

exit=0

### Task 6 — review fix: test helpers out of frontend coverage

The Task 6 review found `src/test-setup.ts` and `src/test-utils.tsx` counted as app code
(the plan's `exclude` replaced vitest's defaults). They are now excluded, along with
`*.spec.*` to mirror the test `include`. Re-run (`pnpm run test:coverage`):

- `Tests  530 passed (530)`
- `Branches     : 87.29% ( 1257/1440 )` (was 87.33%, 1262/1445)
- `Lines        : 93.88% ( 4378/4663 )` (was 93.91%, 4397/4682)
- checker `--report-only`: `<root>` line 84.6 (was 86.2), branch 92.8; `<total>` 93.8 / 87.2;
  api 97.8 / 95.2; components 90.9 / 88.8; routes 93.3 / 85.0; shell 100.0 / 100.0;
  theme 100.0 / -; `exit=0`. tsc and lint clean.
