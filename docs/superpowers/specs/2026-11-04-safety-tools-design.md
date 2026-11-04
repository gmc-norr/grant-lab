# Piece 2: safety tools — type checking and coverage floors — design

Date: 2026-11-04. Piece 2 of `2026-11-03-match-cleanup-programme-design.md`.
All numbers were measured at `c28a1073` on 2026-11-04 unless labelled otherwise.

## Goal

Give the next pieces a safety net before they change code and delete tests:

- A Python type checker runs in CI over `src/grant_lab` and passes.
- CI measures test coverage (lines and branches, per package) for the backend AND the
  frontend, shows the numbers on every run, and fails when a package drops below its
  recorded floor.

## Decisions (user, 2026-11-04)

1. **mypy**, not ty or pyright.
2. **Coverage holds the line:** a committed floor per package, and CI fails below it.
3. **The frontend too**, although the programme spec named only Python for this piece.

Why mypy:

- It is the most widely used Python checker, and it is stable.
- pyright's pip package downloads Node.js when it runs, which breaks the repo's
  wheels-only install rule (`[tool.uv] no-build-package`).
- ty (0.0.97) is fast (1.2 s) but pre-1.0, so a release can add rules and turn CI red
  overnight.

## Measured baseline

### Type checkers on `src/grant_lab` (120 files)

| Run | Errors | Wall time |
|---|---|---|
| mypy 2.4.0, no config | 104 | 15 s |
| mypy, `--strict` | 437 | 14 s |
| **mypy, the configuration in this spec** | **65 in 28 files** | **10 s** |
| pyright 1.1.414 basic / standard / strict | 116 / 122 / 2416 | 12–15 s |
| ty 0.0.97 | 63 errors + 1 warning | 1.2 s |

**The configuration in this spec** means:

- normal mode
- the `pydantic.mypy` plugin
- `warn_unused_ignores` and `warn_redundant_casts`
- `ignore_missing_imports` for `pgvector.*` and `apscheduler.*`
- the `types-lxml` stubs

It was run from the project environment after `uv add --dev mypy pytest-cov types-lxml`
in a throwaway worktree. Its 65 errors by package:

| Package | Errors |
|---|---|
| web | 19 |
| ranking | 13 |
| boards | 9 |
| modules directly in `grant_lab/` | 7 |
| ingestion | 7 |
| digest | 4 |
| sources | 3 |
| auth | 2 |
| profiles | 1 |

By error code: `arg-type` 20, `attr-defined` 13, `union-attr` 10, `assignment` 6,
`return-value` 5, other codes 1–2 each. One error is a stale `# type: ignore` at
`src/grant_lab/web/spa_serving.py:23`, which now hides nothing.

**Third-party typing:**

- `pgvector` and `apscheduler` 3.11.2 ship no type information, and PyPI has no stub
  package for either (`types-pgvector` and `types-apscheduler` both return 404).
- `lxml` has stubs in `types-lxml`.

**The cooldown resolves the measured versions.** Under the repo's
`exclude-newer = 2026-10-24`, `uv add --dev mypy pytest-cov types-lxml` locks:

- mypy 2.4.0
- pytest-cov 7.1.0
- coverage 7.17.0
- types-lxml 2026.2.16

types-lxml also brings five packages: beautifulsoup4, cssselect, soupsieve,
types-html5lib and types-webencodings. After `scripts/gen_no_build.py`, both
`gen_no_build.py --check` and `uv lock --check` pass, with 125 packages in the list.

### Backend coverage

Measured with pytest-cov 7.1.0, `--cov=grant_lab --cov-branch`, `-n 16 --dist loadfile`:

- 2294 passed, with and without coverage.
- Wall time: 84.7 s with coverage, 63.9 s without.

| Package | Statements | Line % | Branch % |
|---|---|---|---|
| **total** | 7834 | **95.1** | **88.6** (1676/1892) |
| modules directly in `grant_lab/` | 686 | 91.1 | 88.1 |
| auth | 428 | 93.5 | 87.7 |
| boards | 299 | 96.0 | 92.6 |
| db | 800 | 100.0 | 100.0 (4 branches) |
| digest | 241 | 97.5 | 94.6 |
| email | 118 | 96.6 | 100.0 |
| embeddings | 312 | 97.8 | 93.5 |
| ingestion | 312 | 89.4 | 84.3 |
| profiles | 563 | 89.9 | 83.3 |
| ranking | 1205 | 94.2 | 88.9 |
| sources | 587 | 93.0 | 85.0 |
| web | 2283 | 97.0 | 89.5 |

**Known blind spots:**

- **db is inflated.** It is mostly table definitions that run at import time.
- **Subprocess runs are not counted.** Some tests start a child Python process, for
  example the import-cycle and app-boot gates in `tests/architecture`, and coverage
  does not see the code that runs there.

This table was measured ONCE. The floors file comes from the repeated measurement
under "Floors" below.

### Frontend coverage

Measured with `@vitest/coverage-v8` 2.0.5, which matches the installed vitest 2.0.5:

- The pnpm 7-day cooldown accepted it.
- All 530 tests passed.
- Wall time: 16.5 s with coverage, 12.2 s without.

| Folder | Statements | Line % | Branch % |
|---|---|---|---|
| **total** (69 files) | 4682 | **93.9** | **87.3** |
| routes | 3382 | 93.4 | 85.1 |
| api | 780 | 97.8 | 95.2 |
| components | 211 | 91.0 | 88.9 |
| shell | 103 | 100 | 100 |
| theme | 24 | 100 | no branches |
| files directly in `src/` | 182 | 86.3 | 93.9 |

These figures exclude test files and `src/api/schema.gen.ts`.

### CI today

- The last master run, 41882338456, had 7 job runs from 6 job definitions (e2e is a
  dev/prod matrix), and no `needs:`.
- The header comment at `ci.yml:21` still says "four jobs". This piece adds a job, so it
  corrects that comment too.
- The backend job took 7 min 51 s, which is the critical path. The next-longest job
  took 2 min 12 s.
- Nothing writes to `$GITHUB_STEP_SUMMARY`.
- Artefacts are uploaded only when e2e or match-e2e fail.

## Design

### 1. Type checking

- **Tools:** add `mypy` and `types-lxml` to `[dependency-groups].dev`, then regenerate
  `uv.lock` and the `no-build-package` list.
- **Settings:** `[tool.mypy]` in `pyproject.toml` holds exactly the configuration
  measured above, plus `files = ["src/grant_lab"]`, so plain `uv run mypy` checks the
  app.
- **CI pins the config file.** CI runs `uv run mypy --config-file pyproject.toml`.
  Verified: when a `mypy.ini` is present and no `--config-file` is given, mypy reads
  the `mypy.ini` and ignores `[tool.mypy]`.
- **New `typecheck` job.** It runs in parallel with the others and has no `needs:`, like
  every other job. The backend job is already the slowest, so putting mypy there would
  lengthen every run.
  - It repeats the other jobs' setup block: checkout, install uv,
    `uv python install 3.12`, `uv sync --all-extras --frozen`.
  - Its own step is `uv run mypy --config-file pyproject.toml`.
  - It has a `timeout-minutes` like the other jobs.
  - The pydantic plugin needs the project's packages installed, so mypy runs in the
    project environment.
- **Fix the code to zero errors.** Triage each of the 65 errors into one of three kinds:
  - **(a) typing-only:** a wrong or missing annotation, or a needed narrowing. Fix it.
  - **(b) a real bug:** a test that fails first, then the fix, in its own commit.
  - **(c) a third-party typing gap that config cannot handle:** a
    `# type: ignore[<code>]` with a reason. The libraries with no type information are
    handled by config (`ignore_missing_imports`), so their objects become `Any`. An
    inline ignore is only for a call that mypy still cannot type.

  The run log records the triage table.
- **Every ignore names its code and its reason.**
  - The one accepted form is `# type: ignore[<code>]  # <reason, at least three words>`.
  - A `tests/architecture` test reads `src/` with Python's `tokenize`, so text inside
    strings or docstrings never counts. It fails on any ignore without a code, or
    without the trailing reason.
  - The test has its own controls:
    - it walked more than 100 files
    - a fixture with a valid ignore passes
    - bare, code-only and reason-only fixtures each fail
  - The three ignores in `src/` today have no reason. The two in `config.py` (lines 484
    and 503, `prop-decorator`) get one, and the stale one at `spa_serving.py:23` is
    removed.
  - `warn_unused_ignores` makes mypy itself fail on an ignore that stops hiding
    anything.
- **Out of scope:** strict mode and type-checking `tests/`. Annotations go
  package by package in piece 3.

### 2. Coverage floors (one file, one checker)

- **The floors file.** `coverage-floors.json` at the repo root is the ONE home for the
  floors. It has two sections:
  - `backend`: the total plus one entry per top-level package of `grant_lab`, with a
    separate entry for the modules directly in it.
  - `frontend`: the total plus one entry per folder of `frontend/src`, with a separate
    entry for the files directly in it.

  Each entry holds `line` and `branch` percentages to one decimal.
  - **The root key.** The group for files directly in a package root (`src/grant_lab/`
    or `frontend/src/`) is the reserved key `<root>`. No real package can have that
    name.
  - **Groups with branches.** A group has branches when its summed branch total is
    above 0. A floor entry carries `branch` exactly when the measured group has
    branches. A mismatch fails, with a message saying the group gained or lost its
    branches.
  - **Empty groups.** Groups with 0 statements are skipped. That avoids division by
    zero, for example for a package that is only an empty `__init__.py`.
- **The checker.** `scripts/check_coverage_floors.py` uses only the standard library.
  Both CI jobs run it as `uv run python scripts/check_coverage_floors.py`; both jobs
  already install uv and Python 3.12. It takes the report path as an argument and fails
  if the file is missing.
- **It never reads a tool's own percentage.** coverage.py's `percent_covered` mixes
  lines and branches when branch coverage is on (measured: 66.67, against 75.0 for
  lines alone). vitest reports `pct` as the string `"Unknown"` when a total is 0.
- **Backend input** is coverage.py's JSON.
  - Group each file by the path segment after `src/grant_lab/`.
  - Sum `covered_lines`/`num_statements` and `covered_branches`/`num_branches` from
    each file's `summary`. Never average percentages.
  - The grouped total must equal the report's `totals`.
- **Frontend input** is vitest's `coverage-summary.json`. Its keys are absolute file
  paths, plus a `total` key.
  - Group each key by the text after the last `/frontend/src/` (Python's
    `rpartition`), taking the first path segment, or `<root>` when there is none.
  - Sum `lines.covered`/`lines.total` and `branches.covered`/`branches.total`.
  - The grouped total must equal `total`.
- **Rounding.** Percentages are integer tenths, computed with integers:
  `covered * 1000 // total`, which rounds down. That value is compared with `>=`
  against the stored tenths, with no floating-point equality anywhere. `--update`
  rounds the same way.
- **It fails when:**
  - a value is below its floor
  - a measured group has no floor (a new package must be given one)
  - a floor has no measured group (a stale entry)
  - a group gained or lost branches

  When a group is missing on one side and a floor on the other, the message suggests
  they may be a rename.
- **A 100.0 floor is intended.** In a package at 100% today, any new untested line
  fails CI. That is the "hold the line" decision; the fix is a test.
- **Renaming or splitting a package** (piece 3 will) means editing its key in
  `coverage-floors.json` in the same commit, with the reason in the commit message.
- **The job summary.** The checker always writes a table to `$GITHUB_STEP_SUMMARY`
  showing the measured value, the floor and the difference. That table is how CI
  "records the numbers" on every run.
  - The raw coverage file is uploaded as an artefact on every run (`if: always()`).
  - The checker step itself has no `if:`, so the wiring pin can accept it (see 5).
- **Report-only mode.** `--report-only` writes the table and exits 0. It is used only
  before the floors file exists (see "Order of work").
- **Raising a floor.** `--update` rewrites the file to the current numbers. It is used
  only on purpose, and the diff shows the change in review.
- **Floors.** A floor is the lowest of three measurements, rounded down to whole tenths:
  two local full runs and the CI run on the draft PR.
  - All three are taken on the final tree, after the mypy fixes, never at `c28a1073`.
  - If the runs differ, the run log records the spread and the unrounded numbers, so
    piece 4 can see both.
  - A floor is never set above the lowest measurement.
  - It is never lowered without the reason written in the commit.

### 3. Backend coverage

- **Tools:** add `pytest-cov` to the dev group.
- **Settings:** `[tool.coverage.run]` sets `source = ["grant_lab"]` and `branch = true`.
- **The backend job** runs its existing pytest step with
  `--cov --cov-report=json:coverage.json --cov-report=term:skip-covered`, then the
  checker. The terminal table goes in the log; the JSON feeds the checker.
- **One merged file under xdist.** With xdist, pytest-cov merges the workers' data in
  the controller and writes one `coverage.json` (READ from the pytest-cov docs). The
  draft PR's CI run must show the merged file at `-n 2`.
- **Ignore the output:** add `coverage.json` and `.coverage*` to `.gitignore`.
- **Subprocess runs stay uncounted.** The known blind spot above is accepted and
  documented. Instrumenting child processes is out of scope.

### 4. Frontend coverage

- **Tools:** add `@vitest/coverage-v8` at the vitest version (2.0.5).
- **Settings:** the coverage block goes in the `test:` block of
  `frontend/vite.config.ts`. There is no separate vitest config, and none is added.
  - provider v8
  - `include: ['src/**']`, excluding test files and `src/api/schema.gen.ts`
  - `reportsDirectory: './coverage'`, set explicitly
  - reporters `json-summary` and `text-summary`
- **Ignore the output:** add `frontend/coverage/` to `.gitignore`.
- **New script:** `"test:coverage": "vitest run --coverage"` in `frontend/package.json`.
- **The frontend job REPLACES its existing `pnpm run test` step** with
  `pnpm run test:coverage`, so the tests run once, not twice. The checker step follows.
- `README.md` keeps `pnpm run test` as the everyday local command.

### 5. Wiring pins

A `tests/architecture` test parses `.github/workflows/ci.yml`.

- **It reuses the existing hardened helpers.** `_run_lines` and `_enforces` in
  `tests/architecture/test_match_gates_are_wired.py` drop comments and refuse any job or
  step with an `if:` or a truthy `continue-on-error`. They move to a shared helper
  module, so both tests use them. A naive parser would pass for `if: false`,
  `continue-on-error: true` or `echo mypy`.
- **It matches whole commands**, each inside its own job:
  - in job `typecheck`: a line starting `uv run mypy --config-file pyproject.toml`
  - in job `backend`: the pytest line carries `--cov`, and a line runs
    `uv run python scripts/check_coverage_floors.py` on the backend report. Checking
    the `--cov` flag means the checker cannot pass on a stale report.
  - in job `frontend`: `pnpm run test:coverage`, and the checker on the frontend report
- **It must fail** when each of these is removed in turn, or wrapped in `if: false`.

### 6. Docs

`README.md` and `CLAUDE.md` gain three things:

- how to run mypy
- how to see coverage locally
- how to raise a floor

`docs/ARCHITECTURE.md` is regenerated if the generator's output changes.

## Order of work

The floors must be measured on the final code, and the checker refuses to run without a
floors file. So the work goes in this order:

1. **Fix mypy to zero first.** These fixes change code, and kind (b) adds tests.
2. **Wire coverage, report-only.** Both CI jobs run the checker with `--report-only`,
   on the draft PR.
3. **Measure the floors.** Two local full runs plus the draft PR's CI run, all on that
   final tree.
4. **Turn on enforcing as the LAST commit.** Commit `coverage-floors.json`, remove
   `--report-only` from CI, then prove each gate red.

## Gates, each proven able to fail

| Gate | Passes on | Must reject (run once, record the red) |
|---|---|---|
| mypy in CI | the fixed `src/` | a planted type error on a throwaway draft PR: the `typecheck` job goes red |
| ignore-reason test | `src/` after the fixes | a bare `# type: ignore` added to a scratch copy |
| floors checker (unit tests) | reports at or above the floors | each of the following, as a fixture: below a floor by one tenth; a group with no floor; a floor with no group; a group that gained branches; a report whose `percent_covered` disagrees with its line figure (the checker must use the line figure); a vitest file whose `pct` is `"Unknown"` |
| backend floor in CI-like use | the full suite | the full suite with one profiles test file deleted (scratch copy): the `profiles` entry goes red |
| frontend floor | the full vitest run | the run with one routes test file deleted (scratch copy) |
| wiring pins | the real `ci.yml` | `ci.yml` with each pinned step removed in turn, and again with each wrapped in `if: false` |

## Unmeasured (decided on the draft PR)

- **CI time with coverage on GitHub's 2-core runner is unmeasured.**
  - Locally coverage added 33% (63.9 → 84.7 s).
  - `PREDICTED (not run)`: the backend job grows from 7 min 51 s to about 10 min.
  - If it grows by more than 3 minutes, stop and ask the user before merging.
- **Whether CI's coverage numbers match local runs is unmeasured.** The floors take the
  lowest, so a difference costs only a lower floor.

## Done when

1. `uv run mypy` reports 0 errors locally and in the `typecheck` job.
2. The backend and frontend jobs show their coverage table on every run, upload the raw
   JSON, and fail below a floor.
3. Every gate in the table above has been seen red once, and the run log records how.
4. Piece 4 can read its baseline from `coverage-floors.json`.
5. **The programme spec is amended in the same branch.** One line in
   `2026-11-03-match-cleanup-programme-design.md` records two changes:
   - piece 2 also covers the frontend (the user's decision on 2026-11-04)
   - piece 4's "not below piece 2's numbers" means `coverage-floors.json`, both sections

## Out of scope

- strict typing
- type-checking `tests/`
- raising coverage
- subprocess coverage
- the review's parked UI ideas
