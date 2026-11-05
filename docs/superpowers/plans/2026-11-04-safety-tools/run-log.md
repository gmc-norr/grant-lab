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

## Task 7 — CI wiring

- Step 1 (red, before ci.yml edit): `37 failed, 19 passed in 1.38s`
- Step 3: jobs `['audit', 'backend', 'e2e', 'frontend', 'match-e2e', 'package', 'typecheck']` (system python3 had PyYAML);
  `tests/architecture`: `219 passed in 13.89s`; ruff check clean, 3 files already formatted;
  full suite `uv run pytest -n 16 --dist loadfile`: `2380 passed in 49.92s` (2343 + 37).

### Task 7 — review fixes

Tests added (43): 7 new mutation cases x 5 pins (`step-shell`, `job-shell`, `workflow-shell`,
`job-needs`, `pipe-and`, `exit-before`, `set+e-around` = 35) + `add -h`/`add --help` x 2 floors
pins (4) + `test_the_banned_flag_lists_are_not_shortened` + the frontend-floors-last test
+ `test_an_abbreviated_flag_is_refused` x 2 (`--upd`, `--report-o`).

- RED on the old code (pins reading `run_lines`): 30 failed, 47 passed (the exit-before,
  set+e-around, step-shell, job-shell, workflow-shell and job-needs cases for all 5 pins);
  the checker test `--upd`/`--report-o`: 2 failed, 41 passed. The `|&` and `-h/--help`
  cases went red when their rule was removed (below).
- New `ci_workflow.sole_command_lines`: whole-step rule (one command line, no shell
  override at step/job/workflow, no `needs:`). `allow_abbrev=False` in the checker.
- Rule removals (each restored with `cp`, `cmp` clean): no `|&` -> 3 red (pipe-and for
  backend/frontend floors, backend coverage); pins ignoring -h/--help -> 4 red; no step-shell
  rule -> 5 red; no needs rule -> 5 red; no job/workflow shell rule -> 10 red; no one-line rule
  -> 5 `exit-before` red (plus set+e-around); `NEVER_IN_CI = ()` -> the literal test red;
  step appended after the frontend upload -> order test red; `allow_abbrev` removed -> 2 red.
- Frontend step order now ends: `... OSV-Scanner (pnpm-lock.yaml)`, `Coverage floors
  (frontend)`, `Upload the frontend coverage report`. Backend comment mentions the floors.
- ruff check clean; mypy `Success: no issues found in 120 source files`;
  `tests/architecture`: `260 passed`; full suite: `2423 passed in 51.27s` (2380 + 43).
- Final line: `NEVER_IN_CI = ("--update",)` (unchanged; `-h`/`--help` are in `EXITS_WITHOUT_CHECKING`).

### Task 7 — controller follow-up: frontend step order

The review fix put the frontend floors step and its upload at the very end of the job, after the
blocking `pnpm audit`. That breaks the job's own rule (review 2026-10-26 M-ops-1: the advisory
audit runs last, so an advisory cannot hide a gate on our own code). The floors step and its
upload now sit after the tests, build and OpenAPI drift check and directly before the blocking
audit. `test_the_frontend_floors_check_runs_after_our_own_gates_and_before_the_audit` pins it:
`1 passed` on the new order, `1 failed` on the previous one (ci.yml restored with cp, cmp clean).
`tests/architecture`: `260 passed`.

## Task 8 — docs

- `tests/architecture`: `260 passed in 17.29s` (README, CLAUDE.md and the programme spec amendment edited; 3 files changed).

## Task 9 — draft PR and CI measurement

Draft PR: https://github.com/parlar/grant-lab/pull/32 (head `91f240fd`). CI run `41983909064`: every job green except `dependency-review` (the known repo setting).

Job durations:

- package: 1m07s
- audit: 0m35s
- backend: 12m13s
- frontend: 2m09s
- match-e2e: 1m26s
- e2e (prod): 1m31s
- e2e (dev): 1m28s
- typecheck: 0m33s

**STOP GATE HIT.** The backend job took 12m13s, over the plan's 10m51s limit (spec: stop and ask the user). Its `Pytest (with coverage)` step took 11m48s. On master `c28a1073` (2294 tests, no coverage) the `Pytest` step took 7m26s and the job 7m51s. This branch runs 2423 tests (+129, +5.6%). So coverage plus the new tests added 4m22s (+59%) to the pytest step. Locally the same suite takes 49.76 s without coverage and 80.7 s with it (+62%), so most of the cost is branch coverage itself.

Local runs for the floors (same tree): backend `2423 passed` twice (80.72 s, 80.92 s); frontend coverage summaries saved twice.

**User decision (2026-11-05): accept the ~12 min backend job and continue** (option A). The alternative, splitting the backend tests over two parallel CI jobs and combining their coverage, was declined for now: it is a design change and doubles the runner minutes. Piece 4 (slim tests) is where suite time gets cut.

### Task 9 Steps 3–5 — the floors

CI's reports came from run `41983909064` (head `91f240fd`): `coverage-backend/coverage.json` and `coverage-frontend/coverage-summary.json`. `--update` with three reports per section (local 1, local 2, CI):

- `wrote backend floors from 3 report(s) to …/coverage-floors.json` (exit 0)
- `wrote frontend floors from 3 report(s) to …/coverage-floors.json` (exit 0)

**Measured noise: none.** Every file has the same line and branch counts in all three runs (backend 120 files, frontend 69 files; compared file by file). So the floors are the measured values rounded down to a tenth, with no headroom: a change that loses coverage in a group fails CI. Example: `<root>` is 630/692 lines (91.04%); one more uncovered line there gives 630/693 = 90.91%, shown as 90.9, below its 91.0 floor.

The six `--report-only` tables against the new floors (all exit 0):

### backend — local-1

### Coverage: backend

| group | line % | floor | diff | branch % | floor | diff |
|---|---|---|---|---|---|---|
| <root> | 91.0 | 91.0 | +0.0 | 87.8 | 87.8 | +0.0 |
| <total> | 95.0 | 95.0 | +0.0 | 88.5 | 88.5 | +0.0 |
| auth | 93.4 | 93.4 | +0.0 | 87.7 | 87.7 | +0.0 |
| boards | 96.1 | 96.1 | +0.0 | 92.6 | 92.6 | +0.0 |
| db | 100.0 | 100.0 | +0.0 | 100.0 | 100.0 | +0.0 |
| digest | 97.5 | 97.5 | +0.0 | 94.6 | 94.6 | +0.0 |
| email | 96.6 | 96.6 | +0.0 | 100.0 | 100.0 | +0.0 |
| embeddings | 97.7 | 97.7 | +0.0 | 93.5 | 93.5 | +0.0 |
| ingestion | 89.5 | 89.5 | +0.0 | 84.2 | 84.2 | +0.0 |
| profiles | 89.9 | 89.9 | +0.0 | 83.3 | 83.3 | +0.0 |
| ranking | 94.2 | 94.2 | +0.0 | 89.1 | 89.1 | +0.0 |
| sources | 92.8 | 92.8 | +0.0 | 84.6 | 84.6 | +0.0 |
| web | 97.0 | 97.0 | +0.0 | 89.5 | 89.5 | +0.0 |

exit=0

### backend — local-2

### Coverage: backend

| group | line % | floor | diff | branch % | floor | diff |
|---|---|---|---|---|---|---|
| <root> | 91.0 | 91.0 | +0.0 | 87.8 | 87.8 | +0.0 |
| <total> | 95.0 | 95.0 | +0.0 | 88.5 | 88.5 | +0.0 |
| auth | 93.4 | 93.4 | +0.0 | 87.7 | 87.7 | +0.0 |
| boards | 96.1 | 96.1 | +0.0 | 92.6 | 92.6 | +0.0 |
| db | 100.0 | 100.0 | +0.0 | 100.0 | 100.0 | +0.0 |
| digest | 97.5 | 97.5 | +0.0 | 94.6 | 94.6 | +0.0 |
| email | 96.6 | 96.6 | +0.0 | 100.0 | 100.0 | +0.0 |
| embeddings | 97.7 | 97.7 | +0.0 | 93.5 | 93.5 | +0.0 |
| ingestion | 89.5 | 89.5 | +0.0 | 84.2 | 84.2 | +0.0 |
| profiles | 89.9 | 89.9 | +0.0 | 83.3 | 83.3 | +0.0 |
| ranking | 94.2 | 94.2 | +0.0 | 89.1 | 89.1 | +0.0 |
| sources | 92.8 | 92.8 | +0.0 | 84.6 | 84.6 | +0.0 |
| web | 97.0 | 97.0 | +0.0 | 89.5 | 89.5 | +0.0 |

exit=0

### backend — CI

### Coverage: backend

| group | line % | floor | diff | branch % | floor | diff |
|---|---|---|---|---|---|---|
| <root> | 91.0 | 91.0 | +0.0 | 87.8 | 87.8 | +0.0 |
| <total> | 95.0 | 95.0 | +0.0 | 88.5 | 88.5 | +0.0 |
| auth | 93.4 | 93.4 | +0.0 | 87.7 | 87.7 | +0.0 |
| boards | 96.1 | 96.1 | +0.0 | 92.6 | 92.6 | +0.0 |
| db | 100.0 | 100.0 | +0.0 | 100.0 | 100.0 | +0.0 |
| digest | 97.5 | 97.5 | +0.0 | 94.6 | 94.6 | +0.0 |
| email | 96.6 | 96.6 | +0.0 | 100.0 | 100.0 | +0.0 |
| embeddings | 97.7 | 97.7 | +0.0 | 93.5 | 93.5 | +0.0 |
| ingestion | 89.5 | 89.5 | +0.0 | 84.2 | 84.2 | +0.0 |
| profiles | 89.9 | 89.9 | +0.0 | 83.3 | 83.3 | +0.0 |
| ranking | 94.2 | 94.2 | +0.0 | 89.1 | 89.1 | +0.0 |
| sources | 92.8 | 92.8 | +0.0 | 84.6 | 84.6 | +0.0 |
| web | 97.0 | 97.0 | +0.0 | 89.5 | 89.5 | +0.0 |

exit=0

### frontend — local-1

### Coverage: frontend

| group | line % | floor | diff | branch % | floor | diff |
|---|---|---|---|---|---|---|
| <root> | 84.6 | 84.6 | +0.0 | 92.8 | 92.8 | +0.0 |
| <total> | 93.8 | 93.8 | +0.0 | 87.2 | 87.2 | +0.0 |
| api | 97.8 | 97.8 | +0.0 | 95.2 | 95.2 | +0.0 |
| components | 90.9 | 90.9 | +0.0 | 88.8 | 88.8 | +0.0 |
| routes | 93.3 | 93.3 | +0.0 | 85.0 | 85.0 | +0.0 |
| shell | 100.0 | 100.0 | +0.0 | 100.0 | 100.0 | +0.0 |
| theme | 100.0 | 100.0 | +0.0 | - | - | - |

exit=0

### frontend — local-2

### Coverage: frontend

| group | line % | floor | diff | branch % | floor | diff |
|---|---|---|---|---|---|---|
| <root> | 84.6 | 84.6 | +0.0 | 92.8 | 92.8 | +0.0 |
| <total> | 93.8 | 93.8 | +0.0 | 87.2 | 87.2 | +0.0 |
| api | 97.8 | 97.8 | +0.0 | 95.2 | 95.2 | +0.0 |
| components | 90.9 | 90.9 | +0.0 | 88.8 | 88.8 | +0.0 |
| routes | 93.3 | 93.3 | +0.0 | 85.0 | 85.0 | +0.0 |
| shell | 100.0 | 100.0 | +0.0 | 100.0 | 100.0 | +0.0 |
| theme | 100.0 | 100.0 | +0.0 | - | - | - |

exit=0

### frontend — CI

### Coverage: frontend

| group | line % | floor | diff | branch % | floor | diff |
|---|---|---|---|---|---|---|
| <root> | 84.6 | 84.6 | +0.0 | 92.8 | 92.8 | +0.0 |
| <total> | 93.8 | 93.8 | +0.0 | 87.2 | 87.2 | +0.0 |
| api | 97.8 | 97.8 | +0.0 | 95.2 | 95.2 | +0.0 |
| components | 90.9 | 90.9 | +0.0 | 88.8 | 88.8 | +0.0 |
| routes | 93.3 | 93.3 | +0.0 | 85.0 | 85.0 | +0.0 |
| shell | 100.0 | 100.0 | +0.0 | 100.0 | 100.0 | +0.0 |
| theme | 100.0 | 100.0 | +0.0 | - | - | - |

exit=0


Exact counts, backend (identical in all three runs):

| group | lines | line % | branches | branch % |
|---|---|---|---|---|
| <root> | 630/692 | 91.04 | 144/164 | 87.80 |
| <total> | 7488/7877 | 95.06 | 1688/1906 | 88.56 |
| auth | 399/427 | 93.44 | 100/114 | 87.72 |
| boards | 297/309 | 96.12 | 63/68 | 92.65 |
| db | 800/800 | 100.00 | 4/4 | 100.00 |
| digest | 235/241 | 97.51 | 53/56 | 94.64 |
| email | 114/118 | 96.61 | 32/32 | 100.00 |
| embeddings | 305/312 | 97.76 | 101/108 | 93.52 |
| ingestion | 283/316 | 89.56 | 59/70 | 84.29 |
| profiles | 508/565 | 89.91 | 145/174 | 83.33 |
| ranking | 1148/1218 | 94.25 | 321/360 | 89.17 |
| sources | 549/591 | 92.89 | 188/222 | 84.68 |
| web | 2220/2288 | 97.03 | 478/534 | 89.51 |

Exact counts, frontend (identical in all three runs):

| group | lines | line % | branches | branch % |
|---|---|---|---|---|
| <root> | 138/163 | 84.66 | 26/28 | 92.86 |
| <total> | 4378/4663 | 93.89 | 1257/1440 | 87.29 |
| api | 763/780 | 97.82 | 239/251 | 95.22 |
| components | 192/211 | 91.00 | 56/63 | 88.89 |
| routes | 3158/3382 | 93.38 | 923/1085 | 85.07 |
| shell | 103/103 | 100.00 | 13/13 | 100.00 |
| theme | 24/24 | 100.00 | - | - |

## Task 10 — enforce the floors

**Step 1 (ban `--report-only` in CI first).** `NEVER_IN_CI = ("--update", "--report-only")`, then the wiring file: `36 failed, 44 passed`. Every failure is a floors pin: the two pins themselves plus 17 mutation checks on each (each runs the real workflow first as a control). The plan expected `18 failed, 21 passed`; that count predates the Task 5 and Task 7 review fixes, which added mutation cases. The rule held: only floors-pin tests failed, everything else passed.

**Step 2 (enforce).** ` --report-only` removed from both checker lines in `ci.yml` (`grep -c` → `0`). Wiring file: `80 passed` (78 before, +2: one "add `--report-only`" case per floors pin).

**Step 3 (backend must-reject).** `tests/test_profiles_pubmed.py` deleted: `2414 passed` (9 fewer). Checker `exit=1`:

- `- <total>: line 94.9% is below its floor 95.0%`
- `- <total>: branch 88.1% is below its floor 88.5%`
- `- profiles: line 87.9% is below its floor 89.9%`
- `- profiles: branch 79.3% is below its floor 83.3%`

Restored with `cp`, `cmp` clean. Good input (`/tmp/p2-local-backend-1.json`): `exit=0`.

**Step 4 (frontend must-reject).** `frontend/src/routes/AdminGoldenSet.test.tsx` deleted: `520 passed` (530 before). Checker `exit=1`:

- `- <total>: line 91.6% is below its floor 93.8%`
- `- routes: line 90.2% is below its floor 93.3%`

Branches went *up* slightly (routes 85.0 → 85.1, `<total>` 87.2 → 87.3): once no test imports `AdminGoldenSet.tsx`, its lines still count as uncovered but its branches drop out of the total. The line floor is what catches a lost test file on the frontend. Restored with `cp`, `cmp` clean; a fresh run gave `530 passed` and checker `exit=0`.

**Step 5 (full local check).** `2425 passed in 51.65s` (2423 + the 2 new cases), `Success: no issues found in 120 source files`, `All checks passed!`.

**Step 5 (CI with the floors enforced).** PR #32 at `7d1d1505`, run `42001021152`: every job green except `dependency-review` (the known repo setting). Both floors steps ran without `--report-only` (`check_coverage_floors.py --section backend --report coverage.json`, `… --section frontend --report frontend/coverage/coverage-summary.json`) and passed. Job durations: backend 10m34s, frontend 3m27s, e2e (dev) 1m38s, match-e2e 1m34s, e2e (prod) 1m31s, package 1m11s, audit 0m37s, typecheck 0m36s. The backend job took 12m13s on the previous run of nearly the same tree, so a single run's time varies by about 1.5 minutes.

**Step 6 (CI must-reject for mypy).** Throwaway branch `p2-mypy-mutant` from `7d1d1505`, one line changed: `is_lock_timeout(...) -> bool` became `-> str` in `src/grant_lab/errors.py`. Locally: `1 file changed, 1 insertion(+), 1 deletion(-)`; mypy `src/grant_lab/errors.py:23: error: Incompatible return value type (got "Any | bool", expected "str")  [return-value]` and `Found 1 error in 1 file (checked 120 source files)`; suite with coverage `2425 passed`, floors `exit=0`.

Draft PR https://github.com/parlar/grant-lab/pull/33 (commit `363d91ae`), run `42001346697`: `typecheck` **failed**, its log showing the same error line and `Found 1 error in 1 file (checked 120 source files)`. Every other job passed (backend and frontend floors included), except `dependency-review` as always. So only the type checker went red.

Cleanup: PR #33 closed; `gh pr close --delete-branch` refused the local branch while its worktree existed and left the remote branch, so the remote branch was deleted with `git push origin --delete p2-mypy-mutant`, the worktree removed and the local branch deleted. `git ls-remote origin refs/heads/p2-mypy-mutant | wc -l` → `0`; no mutant in `git worktree list`.
