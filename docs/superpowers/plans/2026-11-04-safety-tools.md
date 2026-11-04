# Piece 2: Safety Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** mypy runs in CI over `src/grant_lab` and reports zero errors, and CI measures backend and frontend coverage (lines and branches, per package), shows it on every run, and fails below the floors in `coverage-floors.json`.

**Architecture:**

- **mypy.** It runs in a new parallel `typecheck` job. Its 65 errors are fixed with
  pre-verified patches.
- **One checker for both coverage reports.** `scripts/check_coverage_floors.py` uses
  only the standard library and reads coverage.py JSON and vitest's
  `coverage-summary.json`. It compares integer tenths computed from raw counts against
  one committed floors file.
- **Floors come last.** CI first runs the checker report-only. The floors are measured
  on the final tree, and enforcing is turned on in the last commit.

**Tech Stack:** mypy 2.4.0 (with the pydantic plugin), pytest-cov 7.1.0 / coverage 7.17.0, @vitest/coverage-v8 2.0.5, GitHub Actions, uv, pnpm.

**Spec:** `docs/superpowers/specs/2026-11-04-safety-tools-design.md` (commit `49f63485`;
its gates table was updated after an outside review, in the commit that adds this plan).
It is the one home for the baseline numbers. This plan states only each step's own
expected output.

**Appendix and files:** everything is in `docs/superpowers/plans/2026-11-04-safety-tools/`.

- `A-mypy-triage.md`: the kind, cause and fix of every mypy error.
- `fix-*.patch`: the source fixes.
- `match-gates-helper.patch`: the CI-wiring test refactor.
- `files/`: the new files, copied verbatim. Each ends in `.txt`, so `ruff check .`
  does not lint it where it sits: a file that is appended to a test has undefined names
  on its own, and import sorting depends on where a file lives. Ruff checks each copy
  where it lands.

**All of it was run before this plan was written,** in a throwaway worktree at
`49f63485`, in this plan's task order. Every "Expected" line below is the output that
run produced, unless it is labelled PREDICTED.

## Global Constraints

### The spec's rules

- **mypy settings:** normal mode, `plugins = ["pydantic.mypy"]`, `warn_unused_ignores`,
  `warn_redundant_casts`, `files = ["src/grant_lab"]`, and `ignore_missing_imports` only
  for `pgvector.*` and `apscheduler.*`.
- **Running mypy:** CI runs `uv run mypy --config-file pyproject.toml`.
- **Fix the code, don't hide the problem.** An inline ignore must have exactly the form
  `# type: ignore[<code>]  # <reason of at least three words>`.
- **One floors file.** `coverage-floors.json` at the repo root is the one home for
  the floors.
  - Keys are the top-level packages and folders, plus `<root>` and `<total>`.
  - Each entry has `line`, and `branch` only when the group has branches.
- **The checker:**
  - It never reads a tool's own percentage.
  - Percentages are integer tenths (`covered * 1000 // total`) compared with `>=`.
  - A missing floor, a stale floor, or a group that gained or lost branches all fail.
- **Floors** are the lowest of two local full runs and the draft PR's CI run, on the
  final tree, rounded down.
- **Every new gate is seen red once,** and the run log records how.

### This session's standing rules

- **Where to work.** Work only in the worktree
  `W=/home/parlar_ai/dev/grant-lab/.worktrees/safety-tools`, branch
  `chore/safety-tools`. Start every Bash call with `cd $W &&`, because the shell's
  directory resets.
- **The live app.** It runs from `/home/parlar_ai/dev/grant-lab`. Never stop, restart
  or edit anything there before Task 11, which happens only on the user's word. Never
  use `pkill`. Never start a second app. Port 8000 belongs to the live app.
- **Never print an `.env` file.** No calls to Ollama or Anthropic.
- **Formatting and git.**
  - Never run `ruff format` on a directory; give it file names only.
  - Never run a bare `git stash`.
  - Never use `pytest -n auto`. The local suite runs as `uv run pytest -n 16 --dist loadfile`.
- **Mutations** are restored with `cp` and confirmed with `cmp`, and run with
  `PYTHONDONTWRITEBYTECODE=1`.
- **Commits:**
  - Titles name the change.
  - Every message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - PR bodies end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **Pushing, merging and deploying** happen only on the user's word. Task 9's draft PR
  push and Task 10's throwaway CI branch are covered by the user's approval of this plan.
- **The run log** is `docs/superpowers/plans/2026-11-04-safety-tools/run-log.md`. Every
  measured number goes there in the task that measured it.

---

### Task 0: Worktree and baseline

**Files:**
- Create: `docs/superpowers/plans/2026-11-04-safety-tools/run-log.md`

- [ ] **Step 1: Create the worktree from master**

```bash
cd /home/parlar_ai/dev/grant-lab && git worktree add .worktrees/safety-tools -b chore/safety-tools master
W=/home/parlar_ai/dev/grant-lab/.worktrees/safety-tools
cd $W && git log --oneline -1 && uv sync --frozen && pnpm -C frontend install --frozen-lockfile
```

Expected: the log line is the commit that added this plan, and both installs finish.

- [ ] **Step 2: Measure the baseline**

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -1
cd $W/frontend && pnpm exec vitest run 2>&1 | grep -E 'Tests '
cd $W/frontend && pnpm run e2e:match 2>&1 | grep -E '[0-9]+ passed'
```

Expected: `2294 passed`, `530 passed`, `40 passed`.

- [ ] **Step 3: Start the run log and commit it**

Write `run-log.md` with a `## Task 0 — baseline` section holding the three lines
above and the base sha.

```bash
cd $W && git add docs/superpowers/plans/2026-11-04-safety-tools/run-log.md && git commit -m "docs(plan): record the piece-2 baseline"
```

---

### Task 1: Add the tools and the mypy settings

**Files:**
- Modify: `pyproject.toml` (the `[dependency-groups].dev` group, the `no-build-package`
  list, and a new `[tool.mypy]` block)
- Modify: `uv.lock`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `uv run mypy --config-file pyproject.toml` works and reports the 65-error
  baseline.
- Produces: `pytest-cov` is installed for Task 6.

- [ ] **Step 1: Add the dev dependencies**

```bash
cd $W && uv add --dev mypy types-lxml pytest-cov 2>&1 | grep -E '^\s*\+ (mypy|pytest-cov|types-lxml|coverage)=='
cd $W && uv run python scripts/gen_no_build.py
```

Expected:

- `+ coverage==7.17.0`
- `+ mypy==2.4.0`
- `+ pytest-cov==7.1.0`
- `+ types-lxml==2026.2.16`
- then `Updated no-build-package with 125 packages.`

- [ ] **Step 2: Append the mypy settings to `pyproject.toml`**

Add this at the end of the file:

```toml

[tool.mypy]
python_version = "3.12"
files = ["src/grant_lab"]
plugins = ["pydantic.mypy"]
warn_unused_ignores = true
warn_redundant_casts = true

[[tool.mypy.overrides]]
module = ["pgvector.*", "apscheduler.*"]
ignore_missing_imports = true
```

- [ ] **Step 3: Ignore the coverage output in `.gitignore`**

Add these lines under the existing `.mypy_cache/` entry:

```
coverage.json
.coverage
.coverage.*
frontend/coverage/
```

- [ ] **Step 4: Verify**

```bash
cd $W && uv run mypy --config-file pyproject.toml 2>&1 | tail -1
cd $W && uv lock --check && uv run python scripts/gen_no_build.py --check
cd $W && uv run pytest -q -p no:cacheprovider tests/test_gen_no_build.py 2>&1 | tail -1
```

Expected:

- `Found 65 errors in 28 files (checked 120 source files)`
- `no-build-package is in sync with uv.lock.`
- the `test_gen_no_build.py` tests pass

Record the mypy line in the run log.

- [ ] **Step 5: Commit**

```bash
cd $W && git add pyproject.toml uv.lock .gitignore docs/superpowers/plans/2026-11-04-safety-tools/run-log.md
cd $W && git commit -m "build(dev): add mypy, types-lxml and pytest-cov with the mypy settings"
```

---

### Task 2: Ignores need reasons; fix the web and root errors

**Files:**
- Create: `tests/architecture/test_type_ignores_have_reasons.py`, copied from `files/test_type_ignores_have_reasons.py.txt`
- Modify (by patch): `src/grant_lab/web/{api/auth.py,api/boards.py,api/calls.py,dashboard.py,routes/health.py,routes/oauth_google.py,spa_serving.py}` (`fix-web.patch`)
- Modify (by patch): `src/grant_lab/{config.py,main.py,net_safety.py}` (`fix-root.patch`)
- Modify: `tests/test_net_safety.py` (append `files/net_safety_test_addition.py.txt`)
- Modify: `pyproject.toml` and `uv.lock` (add `httpcore` as a direct dependency)

**Interfaces:**
- Produces: `tests/architecture/test_type_ignores_have_reasons.py` with
  `ignore_comments(source) -> list[tuple[int, str]]` and `unexplained(source) -> list[str]`.
- Produces: `net_safety.PinnedNetworkBackend` subclasses `httpcore.NetworkBackend` and
  has `connect_tcp`, `connect_unix_socket` and `sleep`. The `__getattr__` fallback is
  gone.

- [ ] **Step 1: Write the ignore-reason test**

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && cp $D/files/test_type_ignores_have_reasons.py.txt tests/architecture/test_type_ignores_have_reasons.py
```

- [ ] **Step 2: Run it and watch it fail on the three existing ignores**

```bash
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_type_ignores_have_reasons.py 2>&1 | grep -E 'passed|failed|first extra item'
```

Expected:

- `1 failed, 6 passed`
- The failure lists 3 items, the first being
  `src/grant_lab/config.py line 484: # type: ignore[prop-decorator]`. The other two
  are `config.py` line 503 and `web/spa_serving.py` line 23.
- The 6 passing tests are the test's own checks: it accepts a valid ignore, refuses
  four bad ones, and doesn't count text inside a string.

- [ ] **Step 3: Apply the web and root fixes**

Read `A-mypy-triage.md` first, sections "web" and "root".

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && git apply $D/fix-web.patch && git apply $D/fix-root.patch
cd $W && uv run mypy --config-file pyproject.toml 2>&1 | tail -1
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_type_ignores_have_reasons.py 2>&1 | tail -1
```

Expected: `Found 40 errors in 19 files (checked 120 source files)`, then `7 passed`.

- [ ] **Step 4: Add the forwarding test for `PinnedNetworkBackend`**

This test pins every argument and method the backend forwards. It passes on the old
code too, because the old `__getattr__` forwarded everything. It guards the rewrite,
and Step 5 proves it can fail.

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && cat $D/files/net_safety_test_addition.py.txt >> tests/test_net_safety.py
cd $W && uv run ruff format tests/test_net_safety.py && uv run pytest -q -p no:cacheprovider tests/test_net_safety.py 2>&1 | tail -1
```

Expected: `1 file left unchanged`, then `22 passed`.

- [ ] **Step 5: Prove the forwarding test can fail, two ways**

```bash
cd $W && F=src/grant_lab/net_safety.py && B=/tmp/net_safety.orig && cp $F $B
cd $W && python3 - <<'EOF'
p = "src/grant_lab/net_safety.py"; s = open(p).read()
old = "    def sleep(self, seconds: float) -> None:\n        self._inner.sleep(seconds)\n"
assert s.count(old) == 1; open(p, "w").write(s.replace(old, ""))
EOF
cd $W && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider tests/test_net_safety.py -k forwards 2>&1 | tail -1
cd $W && cp /tmp/net_safety.orig src/grant_lab/net_safety.py && cmp /tmp/net_safety.orig src/grant_lab/net_safety.py && echo restored
cd $W && python3 - <<'EOF'
p = "src/grant_lab/net_safety.py"; s = open(p).read()
old = "            timeout=timeout,\n            local_address=local_address,"
assert s.count(old) == 1; open(p, "w").write(s.replace(old, "            local_address=local_address,"))
EOF
cd $W && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider tests/test_net_safety.py -k forwards 2>&1 | tail -1
cd $W && cp /tmp/net_safety.orig src/grant_lab/net_safety.py && cmp /tmp/net_safety.orig src/grant_lab/net_safety.py && echo restored
```

Expected: `1 failed, 21 deselected` twice, and `restored` twice. Record both in the
run log.

- [ ] **Step 6: Make `httpcore` a direct dependency**

`net_safety.py` now imports it directly.

```bash
cd $W && uv add httpcore && grep -n '"httpcore' pyproject.toml && uv run python scripts/gen_no_build.py --check
```

Expected:

- `"httpcore>=1.0.9",` in `[project].dependencies`
- `no-build-package is in sync with uv.lock.`
- `uv.lock` gains only two metadata lines and no package version changes. Check with
  `git diff uv.lock`.

- [ ] **Step 7: Check the API types did not move**

`health.py` changed its return annotation and sets `response_model`.

```bash
cd $W && pnpm -C frontend run gen:api && git diff --exit-code frontend/src/api/schema.gen.ts && echo "no drift"
```

Expected: `no drift`.

- [ ] **Step 8: Run the tests that cover the changed modules**

```bash
cd $W && uv run pytest -q -p no:cacheprovider -n 4 --dist loadfile tests/test_web_health.py tests/test_web_oauth_google.py tests/test_spa_serving.py tests/test_api_call_detail.py tests/test_dashboard_readmodel.py tests/test_api_calls_feed_behavior.py tests/test_api_auth.py tests/test_boards_qr_at_approval.py tests/test_board_auto_selection.py tests/test_net_safety.py tests/test_config.py tests/architecture 2>&1 | tail -1
```

Expected: all passed and 0 failed. Record the line.

- [ ] **Step 9: Commit**

```bash
cd $W && git add -A src tests pyproject.toml uv.lock
cd $W && git commit -m "fix(types): fix the web and root mypy errors and require a reason on every type ignore"
```

---

### Task 3: Fix the ranking and boards errors

**Files:**
- Modify (by patch): `src/grant_lab/ranking/{anthropic_client.py,criteria.py,freshness.py,ollama_client.py,queue.py,rank_pair.py,worker.py}` (`fix-ranking.patch`)
- Modify (by patch): `src/grant_lab/boards/{alerts.py,auto_publish.py,qr.py,queue.py}` (`fix-boards.patch`)

- [ ] **Step 1: Apply both patches and count**

Read `A-mypy-triage.md`, sections "ranking" and "boards".

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && git apply $D/fix-ranking.patch && git apply $D/fix-boards.patch
cd $W && uv run mypy --config-file pyproject.toml 2>&1 | tail -1
```

Expected: `Found 18 errors in 9 files (checked 120 source files)`.

- [ ] **Step 2: Run the tests that cover them**

```bash
cd $W && uv run pytest -q -p no:cacheprovider -n 4 --dist loadfile tests/test_ranking_*.py tests/test_ingestion_enqueues_rankings.py tests/test_scheduler_ranking_logprobs.py tests/test_call_rank_fingerprint.py tests/test_boards_*.py tests/test_board_*.py 2>&1 | tail -1
```

Expected: all passed and 0 failed. Record the line.

- [ ] **Step 3: Commit**

```bash
cd $W && git add -A src && git commit -m "fix(types): fix the ranking and boards mypy errors"
```

---

### Task 4: Fix the rest; mypy reaches zero

**Files:**
- Modify (by patch): `src/grant_lab/ingestion/{alerts.py,orchestrator.py,upsert.py}` (`fix-ingestion.patch`)
- Modify (by patch): `src/grant_lab/{auth/oidc.py,digest/assemble.py,profiles/bootstrap_worker.py,sources/extraction.py,sources/types.py}` (`fix-small.patch`)
- Modify (by patch): `src/grant_lab/ingestion/alerts.py`, `src/grant_lab/ingestion/orchestrator.py`, `src/grant_lab/scheduler.py` (`fix-alerter.patch`)

**Interfaces:**
- Produces: `grant_lab.ingestion.alerts.FailureAlerter`, a Protocol with
  `record_failure(source_slug: str, *, error: str | None = None) -> None` and
  `record_success(source_slug: str) -> None`.
- Produces: `run_ingestion(alerter: FailureAlerter, ...)` and
  `build_scheduler(alerter: FailureAlerter, ...)`. The scheduler's private `_Alerter`
  is removed.

- [ ] **Step 1: Apply the ingestion and small-group patches**

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && git apply $D/fix-ingestion.patch && git apply $D/fix-small.patch
cd $W && uv run mypy --config-file pyproject.toml 2>&1 | tail -2
```

Expected: one error left, the alerter:

```
src/grant_lab/scheduler.py:426: error: Argument "alerter" to "run_ingestion" has incompatible type "ConsecutiveFailureTracker | _Alerter"; expected "ConsecutiveFailureTracker"  [arg-type]
Found 1 error in 1 file (checked 120 source files)
```

- [ ] **Step 2: Apply the alerter fix**

Read `A-mypy-triage.md`, section "The alerter".

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && git apply $D/fix-alerter.patch && uv run mypy --config-file pyproject.toml 2>&1 | tail -1
```

Expected: `Success: no issues found in 120 source files`.

- [ ] **Step 3: Lint, then run the full suite**

```bash
cd $W && uv run ruff check . | tail -1
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -1
```

Expected: `All checks passed!`, then `2302 passed`. That is the 2294 baseline plus the
7 ignore-reason tests and 1 forwarding test. Record it in the run log.

- [ ] **Step 4: Commit**

```bash
cd $W && git add -A src && git commit -m "fix(types): fix the remaining mypy errors and type the ingestion alerter as a protocol"
```

---

### Task 5: The coverage-floors checker

**Files:**
- Create: `scripts/check_coverage_floors.py`, copied from `files/check_coverage_floors.py.txt`
- Test: `tests/test_check_coverage_floors.py`, copied from `files/test_check_coverage_floors.py.txt`

**Interfaces:**
- Produces: the command line
  `check_coverage_floors.py --section {backend,frontend} --report PATH [--report PATH ...] [--floors PATH] [--report-only | --update]`.
  - Exit codes: 0 pass, 1 a floor problem, 2 a bad or missing input.
  - Writes a markdown table to stdout, and appends it to `$GITHUB_STEP_SUMMARY` when
    that is set.
  - `--update` writes the lowest value over all `--report`s and keeps the other
    section.
- Produces: the floors-file format,
  `{"backend": {"<group>": {"line": 95.0, "branch": 88.5}, ...}, "frontend": {...}}`.

- [ ] **Step 1: Write the tests**

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && cp $D/files/test_check_coverage_floors.py.txt tests/test_check_coverage_floors.py
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd $W && uv run pytest -q -p no:cacheprovider tests/test_check_coverage_floors.py 2>&1 | tail -3
```

Expected: a collection error, because the script does not exist yet (`FileNotFoundError`
for `scripts/check_coverage_floors.py`).

- [ ] **Step 3: Add the script**

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && cp $D/files/check_coverage_floors.py.txt scripts/check_coverage_floors.py
cd $W && uv run pytest -q -p no:cacheprovider tests/test_check_coverage_floors.py 2>&1 | tail -1
cd $W && uv run ruff check scripts/check_coverage_floors.py tests/test_check_coverage_floors.py && uv run ruff format --check scripts/check_coverage_floors.py tests/test_check_coverage_floors.py
```

Expected: `19 passed`, `All checks passed!`, and `2 files already formatted`.

Each must-reject from the spec's table is one of these tests:

- `test_one_tenth_below_a_line_floor_fails`
- `test_a_branch_below_its_floor_fails`
- `test_a_group_with_no_floor_fails`
- `test_a_floor_with_no_group_fails_and_a_rename_is_suggested`
- `test_a_group_that_gained_branches_fails`
- `test_the_tools_own_percentage_is_never_read`
- `test_frontend_groups_by_folder_and_ignores_an_unknown_pct`

One more guards `--update`: `test_update_refuses_runs_that_disagree_on_branches`.
Two runs where a group has branches in one and none in the other are refused with
exit 2, not divided by zero.

- [ ] **Step 4: Commit**

```bash
cd $W && git add scripts/check_coverage_floors.py tests/test_check_coverage_floors.py
cd $W && git commit -m "feat(ci): add a checker that holds coverage at per-package floors"
```

---

### Task 6: Measure coverage locally, backend and frontend

**Files:**
- Modify: `pyproject.toml` (a new `[tool.coverage.run]` block)
- Modify: `frontend/package.json` (a `test:coverage` script and the
  `@vitest/coverage-v8` devDependency)
- Modify: `frontend/pnpm-lock.yaml`
- Modify: `frontend/vite.config.ts` (a `coverage` block inside `test:`)

- [ ] **Step 1: Backend coverage settings**

Append to `pyproject.toml`:

```toml

[tool.coverage.run]
source = ["grant_lab"]
branch = true
```

- [ ] **Step 2: Run the suite with coverage, then the checker in report-only mode**

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:coverage.json --cov-report=term:skip-covered 2>&1 | grep -E 'passed|failed|^TOTAL'
cd $W && uv run python scripts/check_coverage_floors.py --section backend --report coverage.json --report-only --floors /tmp/no-floors.json; echo "exit=$?"
```

Expected:

- `2321 passed`: 2302 plus the 19 checker tests.
- A table with the rows `<root>, <total>, auth, boards, db, digest, email, embeddings, ingestion, profiles, ranking, sources, web`.
- `exit=0`.

The PREDICTED values are near the spec's baseline: total line about 95%, branch about
88.5%. Copy the whole table into the run log as "local run 1, backend".

- [ ] **Step 3: Add the frontend coverage provider and its settings**

```bash
cd $W && pnpm -C frontend add -D @vitest/coverage-v8@2.0.5 2>&1 | grep -E 'coverage-v8'
```

Expected: `+ @vitest/coverage-v8 2.0.5`. The pnpm 7-day cooldown accepts it.

In `frontend/vite.config.ts`, inside the `test: { ... }` block, after the line
`exclude: ["**/node_modules/**"],`, add:

```ts
    // `pnpm run test:coverage` measures app code only: tests and the generated
    // API types are left out. CI checks the summary against coverage-floors.json.
    coverage: {
      provider: "v8",
      include: ["src/**"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/api/schema.gen.ts"],
      reportsDirectory: "./coverage",
      reporter: ["json-summary", "text-summary"],
    },
```

In `frontend/package.json` `scripts`, after `"test": "vitest run",`, add:

```json
    "test:coverage": "vitest run --coverage",
```

- [ ] **Step 4: Run frontend coverage and the checker**

```bash
cd $W/frontend && pnpm run test:coverage 2>&1 | grep -E 'Tests |Branches|Lines'
cd $W && uv run python scripts/check_coverage_floors.py --section frontend --report frontend/coverage/coverage-summary.json --report-only --floors /tmp/no-floors.json; echo "exit=$?"
cd $W/frontend && pnpm exec tsc --noEmit && pnpm run lint
```

Expected:

- `530 passed`, then `Branches : 87.33% ( 1262/1445 )` and `Lines : 93.91% ( 4397/4682 )`.
- A table with the rows `<root>, <total>, api, components, routes, shell, theme`.
  `theme` shows `-` for branch.
- `exit=0`.
- tsc and lint are clean.

Copy the table into the run log as "local run 1, frontend".

- [ ] **Step 5: Commit**

```bash
cd $W && git add pyproject.toml frontend/package.json frontend/pnpm-lock.yaml frontend/vite.config.ts docs/superpowers/plans/2026-11-04-safety-tools/run-log.md
cd $W && git commit -m "build(coverage): measure backend and frontend coverage with branch data"
```

---

### Task 7: CI runs the three checks (coverage report-only for now)

**Files:**
- Create: `tests/architecture/ci_workflow.py`, copied from `files/ci_workflow.py.txt`
- Modify (by patch): `tests/architecture/test_match_gates_are_wired.py` (`match-gates-helper.patch`)
- Create: `tests/architecture/test_safety_gates_are_wired.py`, copied from `files/test_safety_gates_are_wired.py.txt`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `tests.architecture.ci_workflow.enforces(node) -> bool` and
  `run_lines(path=CI, *, job=None) -> list[str]`.
  - `test_match_gates_are_wired.py` keeps its `_enforces` / `_run_lines` names as thin
    wrappers.
  - `_run_lines()` reads that module's own `CI` when it is called, so its mutation
    tests still work.
- Produces: `test_safety_gates_are_wired.NEVER_IN_CI = ("--update",)`, the flags a floors
  check in CI must not carry. Task 10 adds `"--report-only"` to it, on that one line.

- [ ] **Step 1: Move the helpers and add the new pins**

```bash
D=docs/superpowers/plans/2026-11-04-safety-tools
cd $W && cp $D/files/ci_workflow.py.txt tests/architecture/ci_workflow.py && git apply $D/match-gates-helper.patch
cd $W && cp $D/files/test_safety_gates_are_wired.py.txt tests/architecture/test_safety_gates_are_wired.py
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_match_gates_are_wired.py tests/architecture/test_safety_gates_are_wired.py 2>&1 | tail -1
```

Expected: `37 failed, 19 passed`.

- The 19 are the existing match-gates tests, unchanged by the refactor.
- The 37 are the 5 pins plus their mutation checks. Each mutation check runs the
  real workflow first as a control, so it fails while no step exists.
- Every pin has 6 mutation checks: the step removed, the step `if: false`, the job
  `if: false`, `continue-on-error: true`, `|| true` after the command, and `echo` in
  front of it. The two floors pins have a 7th: `--update` added.

- [ ] **Step 2: Edit `.github/workflows/ci.yml` (three edits)**

These edits were dry-run by applying each block below, as written, to a fresh copy of
`ci.yml`. Each "replace this" block occurs exactly once.

**Edit 1, the header comment.** Replace `# WHY FOUR JOBS AND NOT ONE` with
`# WHY SEPARATE JOBS AND NOT ONE`. Then replace this:

```
vitest regression would stay invisible until the advisory was waived. The
# four jobs below have no `needs:` between them on purpose: they run in parallel and
```

with this:

```
vitest regression would stay invisible until the advisory was waived. The
# jobs below have no `needs:` between them on purpose: they run in parallel and
```

**Edit 2, the backend job's pytest step.** Replace this:

```yaml
      - name: Pytest
        run: uv run pytest -v -n 2 --dist loadfile
```

with this, which also adds the new `typecheck` job right after the backend job:

```yaml
      - name: Pytest (with coverage)
        run: uv run pytest -v -n 2 --dist loadfile --cov --cov-report=json:coverage.json --cov-report=term:skip-covered

      # Lines and branches per package, held at coverage-floors.json. The table
      # also goes to the run's summary page, which is where the numbers are recorded.
      - name: Coverage floors (backend)
        run: uv run python scripts/check_coverage_floors.py --section backend --report coverage.json --report-only

      - name: Upload the backend coverage report
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: coverage-backend
          path: coverage.json
          if-no-files-found: ignore

  # mypy over src/grant_lab with the [tool.mypy] settings in pyproject.toml. Its own
  # job, so the slow backend job does not get slower and a type error is reported
  # even when the tests fail.
  typecheck:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4

      - name: Install uv
        uses: astral-sh/setup-uv@caf0cab7a618c569241d31dcd442f54681755d39 # v3
        with:
          version: "0.11.x"

      - name: Set up Python
        run: uv python install 3.12

      - name: Sync deps (frozen)
        run: uv sync --all-extras --frozen

      # --config-file is explicit: without it, a stray mypy.ini would be read
      # instead of pyproject.toml.
      - name: Type check (mypy)
        run: uv run mypy --config-file pyproject.toml
```

**Edit 3, the frontend job's test step.** Replace this:

```yaml
      - name: Frontend unit tests (vitest)
        run: cd frontend && pnpm run test
```

with this:

```yaml
      - name: Frontend unit tests with coverage (vitest)
        run: cd frontend && pnpm run test:coverage

      - name: Coverage floors (frontend)
        run: uv run python scripts/check_coverage_floors.py --section frontend --report frontend/coverage/coverage-summary.json --report-only

      - name: Upload the frontend coverage report
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: coverage-frontend
          path: frontend/coverage/coverage-summary.json
          if-no-files-found: ignore
```

- [ ] **Step 3: Verify**

```bash
cd $W && python3 -c "import yaml; print(sorted(yaml.safe_load(open('.github/workflows/ci.yml'))['jobs']))"
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture 2>&1 | tail -1
cd $W && uv run ruff check . | tail -1 && uv run ruff format --check tests/architecture/ci_workflow.py tests/architecture/test_match_gates_are_wired.py tests/architecture/test_safety_gates_are_wired.py
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -1
```

Expected:

- `['audit', 'backend', 'e2e', 'frontend', 'match-e2e', 'package', 'typecheck']`
- All architecture tests pass.
- Ruff is clean, and the 3 files are already formatted.
- `2358 passed`: 2321 plus the 37 new pin tests.

Record it in the run log.

- [ ] **Step 4: Commit**

```bash
cd $W && git add tests/architecture .github/workflows/ci.yml
cd $W && git commit -m "ci: run mypy in a typecheck job and report backend and frontend coverage per package"
```

---

### Task 8: Docs

**Files:**
- Modify: `README.md` (the "Tests and checks" section)
- Modify: `CLAUDE.md` (the Stack bullets)
- Modify: `docs/superpowers/specs/2026-11-03-match-cleanup-programme-design.md` (one amendment line)

- [ ] **Step 1: README**

In "Tests and checks", change the Backend code block to:

```bash
uv run pytest -n 16 --dist loadfile
uv run ruff check .
uv run mypy                    # type check src/grant_lab (settings in pyproject.toml)
```

After the existing paragraph about fewer pytest workers, add (the outer `~~~` marks this
plan's quote; the README gets what is inside it):

~~~markdown
Coverage, as CI measures it (lines and branches per package):

```bash
uv run pytest -n 16 --dist loadfile --cov --cov-report=json:coverage.json
uv run python scripts/check_coverage_floors.py --section backend --report coverage.json
cd frontend && pnpm run test:coverage && cd ..
uv run python scripts/check_coverage_floors.py --section frontend --report frontend/coverage/coverage-summary.json
```

CI fails when a package drops below its floor in `coverage-floors.json`. When coverage
goes up, raise the floors on purpose: run the same command with `--update`, then commit
the changed file.
~~~

- [ ] **Step 2: CLAUDE.md**

Replace the first Stack bullet:

```markdown
- Python 3.12 — `uv` for packages, `ruff` for lint/format, `pytest` (+ testcontainers Postgres) for tests.
```

with:

```markdown
- Python 3.12 — `uv` for packages, `ruff` for lint/format, `pytest` (+ testcontainers Postgres) for tests,
  `mypy` for types (`uv run mypy`; CI's `typecheck` job must report 0 errors).
- **Coverage floors:** CI measures backend and frontend coverage (lines and branches, per package) and
  fails below `coverage-floors.json`. A `# type: ignore` needs a code and a reason:
  `# type: ignore[<code>]  # <why>`. See README "Tests and checks".
```

- [ ] **Step 3: Amend the programme spec**

At the end of the "Pieces" table section, after the line beginning
`The match web screens are part of pieces 2–5.`, add:

```markdown

**Amendment (2026-11-04, user decision):** piece 2 also measures the frontend. Piece 4's
"not below piece 2's numbers" means the floors in `coverage-floors.json`, both the
`backend` and `frontend` sections.
```

- [ ] **Step 4: Commit**

```bash
cd $W && git add README.md CLAUDE.md docs/superpowers/specs/2026-11-03-match-cleanup-programme-design.md
cd $W && git commit -m "docs: describe mypy and the coverage floors in the README and CLAUDE.md"
```

---

### Task 9: Draft PR, the CI measurement, and the floors

**Files:**
- Modify: `docs/superpowers/plans/2026-11-04-safety-tools/run-log.md`

- [ ] **Step 1: Push and open a draft PR (covered by the plan approval)**

```bash
cd $W && git push -u origin chore/safety-tools
cd $W && gh pr create --draft --base master --head chore/safety-tools --title "Piece 2: mypy in CI and per-package coverage floors" --body $'Piece 2 of the match clean-up. Spec: docs/superpowers/specs/2026-11-04-safety-tools-design.md. Plan: docs/superpowers/plans/2026-11-04-safety-tools.md.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)'
cd $W && gh pr checks --watch --interval 30
```

Expected: every job green except `dependency-review`, the known repo setting.
`typecheck` is green. `backend` and `frontend` show their coverage tables on the run's
summary page.

- [ ] **Step 2: Record the CI timings, and stop if the backend job grew too much**

```bash
cd $W && RUN=$(gh run list --branch chore/safety-tools --workflow ci.yml --limit 1 --json databaseId -q '.[0].databaseId') && gh run view $RUN --json jobs -q '.jobs[] | "\(.name) \(.startedAt) \(.completedAt)"'
```

Record each job's duration.

- The baseline backend job took 7 min 51 s (spec).
- **If it now takes more than 10 min 51 s, stop and ask the user** before going on.

- [ ] **Step 3: Download CI's coverage reports**

```bash
cd $W && mkdir -p /tmp/p2-ci && gh run download $RUN -n coverage-backend -D /tmp/p2-ci/backend && gh run download $RUN -n coverage-frontend -D /tmp/p2-ci/frontend && ls /tmp/p2-ci/*/
```

Expected: `coverage.json` and `coverage-summary.json`.

- [ ] **Step 4: Two local full runs of each**

```bash
cd $W && for i in 1 2; do uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:/tmp/p2-local-backend-$i.json 2>&1 | tail -1; done
cd $W/frontend && for i in 1 2; do pnpm run test:coverage >/dev/null 2>&1 && cp coverage/coverage-summary.json /tmp/p2-local-frontend-$i.json; done; ls /tmp/p2-local-*
```

- [ ] **Step 5: Write the floors from the lowest of the three runs**

```bash
cd $W && uv run python scripts/check_coverage_floors.py --section backend --update --report /tmp/p2-local-backend-1.json --report /tmp/p2-local-backend-2.json --report /tmp/p2-ci/backend/coverage.json
cd $W && uv run python scripts/check_coverage_floors.py --section frontend --update --report /tmp/p2-local-frontend-1.json --report /tmp/p2-local-frontend-2.json --report /tmp/p2-ci/frontend/coverage-summary.json
cd $W && cat coverage-floors.json
```

Expected:

- `wrote backend floors from 3 report(s)` and `wrote frontend floors from 3 report(s)`
- If CI's groups differ from the local ones, the script refuses (exit 2,
  `the reports disagree on their groups`). It also refuses when a group has branches
  in some reports and none in others. Either way, stop and investigate.

Then run each report through `--report-only` against the new file, and copy all six
tables into the run log. Note every group where the three runs differ: that spread is
the measured noise. The floors file holds the rounded-down minimum; the run log holds
the unrounded numbers.

- [ ] **Step 6: Commit the run log (not the floors yet)**

```bash
cd $W && git add docs/superpowers/plans/2026-11-04-safety-tools/run-log.md && git commit -m "docs(plan): record the CI timings and three coverage measurements"
```

---

### Task 10: Enforce the floors and prove every gate red

**Files:**
- Create: `coverage-floors.json` (written in Task 9 Step 5)
- Modify: `.github/workflows/ci.yml` (remove the two `--report-only` flags)
- Modify: `tests/architecture/test_safety_gates_are_wired.py` (add `--report-only` to
  `NEVER_IN_CI`)

- [ ] **Step 1: Ban `--report-only` in CI first, and watch the pins fail**

`--report-only` exits 0 below a floor. It was for measuring the floors before
`coverage-floors.json` existed. From now on a floors check in CI must not carry it.

```bash
cd $W && sed -i 's/^NEVER_IN_CI = ("--update",)$/NEVER_IN_CI = ("--update", "--report-only")/' tests/architecture/test_safety_gates_are_wired.py && grep -n '^NEVER_IN_CI' tests/architecture/test_safety_gates_are_wired.py
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_safety_gates_are_wired.py 2>&1 | tail -1
```

Expected: `NEVER_IN_CI = ("--update", "--report-only")` on one line, then
`18 failed, 21 passed`.

- The failures are the two floors pins and every mutation check on them, because each
  mutation check first runs the real workflow as a control.
- The other three pins, and their mutation checks, still pass.

- [ ] **Step 2: Enforce in CI**

Delete ` --report-only` from the end of the two `check_coverage_floors.py` lines in
`.github/workflows/ci.yml`.

```bash
cd $W && grep -c -- '--report-only' .github/workflows/ci.yml
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_safety_gates_are_wired.py 2>&1 | tail -1
```

Expected: `0`, then `39 passed`. That is two more than in Task 7: each floors pin
gained an "add `--report-only`" mutation check.

- [ ] **Step 3: Backend must-reject: delete one profiles test file**

```bash
cd $W && B=/tmp/test_profiles_pubmed.py.orig && cp tests/test_profiles_pubmed.py $B && rm tests/test_profiles_pubmed.py
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:/tmp/p2-mutant.json 2>&1 | tail -1
cd $W && uv run python scripts/check_coverage_floors.py --section backend --report /tmp/p2-mutant.json | grep -E '^- '; echo "exit=${PIPESTATUS[0]}"
cd $W && cp /tmp/test_profiles_pubmed.py.orig tests/test_profiles_pubmed.py && cmp /tmp/test_profiles_pubmed.py.orig tests/test_profiles_pubmed.py && echo restored
cd $W && uv run python scripts/check_coverage_floors.py --section backend --report /tmp/p2-local-backend-1.json >/dev/null; echo "good input exit=$?"
```

Expected:

- `exit=1`, with a `- profiles: line …% is below its floor …%` line.
- In the throwaway run, profiles fell from 89.9% to 87.9% of lines and from 83.3% to
  79.3% of branches.
- Then `restored` and `good input exit=0`.

Record both lines in the run log.

- [ ] **Step 4: Frontend must-reject: delete one routes test file**

```bash
cd $W && T=frontend/src/routes/AdminGoldenSet.test.tsx && cp $T /tmp/AdminGoldenSet.test.tsx.orig && rm $T
cd $W/frontend && pnpm run test:coverage 2>&1 | grep -E 'Tests '
cd $W && uv run python scripts/check_coverage_floors.py --section frontend --report frontend/coverage/coverage-summary.json | grep -E '^- '; echo "exit=${PIPESTATUS[0]}"
cd $W && cp /tmp/AdminGoldenSet.test.tsx.orig frontend/src/routes/AdminGoldenSet.test.tsx && cmp /tmp/AdminGoldenSet.test.tsx.orig frontend/src/routes/AdminGoldenSet.test.tsx && echo restored
```

Expected:

- `520 passed`, then `exit=1`, with a `- routes: line …% is below its floor …%` line.
- In the throwaway run, routes fell from 93.3% to 90.2%.
- Then `restored`.

Record it in the run log.

- [ ] **Step 5: Full local check, then commit and push**

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -1
cd $W && uv run mypy --config-file pyproject.toml | tail -1 && uv run ruff check . | tail -1
cd $W && git status --short
cd $W && git add coverage-floors.json .github/workflows/ci.yml tests/architecture/test_safety_gates_are_wired.py docs/superpowers/plans/2026-11-04-safety-tools/run-log.md
cd $W && git commit -m "ci: enforce the coverage floors in coverage-floors.json"
cd $W && git push && gh pr checks --watch --interval 30
```

Expected:

- `2360 passed`, `Success: no issues found in 120 source files` and `All checks passed!`.
- `git status` shows only the four paths named in the commit.
- CI is green except `dependency-review`, and both coverage checks pass against the
  floors.

- [ ] **Step 6: CI must-reject for mypy, on a throwaway branch (covered by the plan approval)**

The planted error changes the return type of `is_lock_timeout` from `bool` to `str`, on
a line that already exists and already runs. Its one caller only tests it with `not`,
so mypy reports exactly one error. No line is added, so every coverage count stays the
same and only `typecheck` can go red. (An added function would add an uncovered line.
`<root>` measured 630 of 692 lines, 91.0%, and one more uncovered line makes it 90.9%,
below its floor.)

First plant it and check it locally:

```bash
M=/home/parlar_ai/dev/grant-lab/.worktrees/p2-mypy-mutant
cd /home/parlar_ai/dev/grant-lab && git worktree add $M -b p2-mypy-mutant chore/safety-tools
cd $M && sed -i 's/^def is_lock_timeout(exc: BaseException) -> bool:/def is_lock_timeout(exc: BaseException) -> str:/' src/grant_lab/errors.py && git diff --stat
cd $M && uv run mypy --config-file pyproject.toml 2>&1 | tail -2
cd $M && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:/tmp/p2-mypy-mutant.json 2>&1 | tail -1
cd $M && uv run python scripts/check_coverage_floors.py --section backend --report /tmp/p2-mypy-mutant.json >/dev/null; echo "floors exit=$?"
```

Expected:

- `1 file changed, 1 insertion(+), 1 deletion(-)`
- `src/grant_lab/errors.py:23: error: Incompatible return value type (got "Any | bool", expected "str")  [return-value]`
- `Found 1 error in 1 file (checked 120 source files)`
- `2360 passed`, then `floors exit=0`

Then push it:

```bash
cd $M && git commit -qam "test(ci): plant a type error to prove the typecheck job goes red (do not merge)" && git push -u origin p2-mypy-mutant
cd $M && gh pr create --draft --base master --head p2-mypy-mutant --title "DO NOT MERGE: planted type error" --body $'Throwaway: the typecheck job must fail on src/grant_lab/errors.py. Closed and deleted once CI shows that.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)'
cd $M && gh pr checks --watch --interval 30; gh run view $(gh run list --branch p2-mypy-mutant --workflow ci.yml --limit 1 --json databaseId -q '.[0].databaseId') --log-failed | grep -E 'error:|Found [0-9]+ error' | head -3
```

Expected:

- The `typecheck` job fails, and its log shows the same error line as above.
- Every other job passes, except `dependency-review` as always.

Record the PR URL and the error line in the run log. Then clean up. If GitHub has an
outage, retry with `gh api -X PATCH repos/parlar/grant-lab/pulls/<n> -f state=closed`
and `git push origin --delete p2-mypy-mutant`.

```bash
cd /home/parlar_ai/dev/grant-lab && gh pr close <n> --delete-branch; git worktree remove --force $M; git branch -D p2-mypy-mutant; git ls-remote origin refs/heads/p2-mypy-mutant | wc -l
```

Expected: `0`.

- [ ] **Step 7: Commit and push the run log**

```bash
cd $W && git add docs/superpowers/plans/2026-11-04-safety-tools/run-log.md && git commit -m "docs(plan): record the must-reject runs" && git push
```

---

### Task 11: Merge, deploy, push (each on the user's word)

- [ ] **Step 1: Ask the user.** Show the green PR checks, the run log's measurements
  and the floors. The deploy restarts the app for about a minute, because `src/`
  changed.

- [ ] **Step 2: Deploy.** Same recipe as the last two deploys. Run one line at a time,
  and stop on any failure.

```bash
~/grant-lab-backups/backup.sh || { echo "backup failed: stop"; exit 1; }
systemctl --user stop grant-lab-app
cd /home/parlar_ai/dev/grant-lab && git status --short && git merge --ff-only chore/safety-tools || { echo "merge failed: restarting the unchanged app"; systemctl --user start grant-lab-app; exit 1; }
cd /home/parlar_ai/dev/grant-lab && uv sync --frozen && pnpm -C frontend install --frozen-lockfile && pnpm -C frontend run build && pnpm -C frontend run package && systemctl --user start grant-lab-app && sleep 5 && curl -s http://127.0.0.1:8000/healthz
```

Expected: `{"status":"ok","scheduler_lock":true}`. Then check the log has no errors since
the start.

- [ ] **Step 3: Push master and watch CI**

```bash
cd /home/parlar_ai/dev/grant-lab && git push origin master
```

Watch the `master` CI run until it is green.

- [ ] **Step 4: Clean up.** Remove the worktree with
  `git -C /home/parlar_ai/dev/grant-lab worktree remove .worktrees/safety-tools`.
  Update memory: piece 2 done, the floors file, and the new `uv run mypy` command.
