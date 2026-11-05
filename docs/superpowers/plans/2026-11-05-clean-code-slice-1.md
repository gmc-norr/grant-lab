# Piece 3, slice 1 — repeated and dead code: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** each rule in the match app is written once, and dead code is gone, with no
change users can see.

**Architecture:** behaviour-preserving refactors, one per task, each guarded by the
existing tests plus a mutation check. A new script compares uncovered lines before and
after, so a coverage floor drops only when no surviving code lost its tests. A new
architecture test keeps the deleted module and its two libraries out.

**Tech stack:** Python 3.12, FastAPI, SQLAlchemy 2, pytest + testcontainers, mypy,
ruff, React 19 + Vite + vitest, uv, pnpm.

**Spec:** `docs/superpowers/specs/2026-11-05-clean-code-design.md`, sections "Slice 1",
"How each slice ships" and "Rules for every slice". The spec is the one home for every
rule. This plan points at it instead of restating it, except where a task needs an
exact value.

## Global Constraints

### The spec's rules

- **No user-visible change.** The API is unchanged: same paths, methods, status codes
  and error messages. The OpenAPI document may differ only in property order.
- **Tests that may change.** Only the five cases in the spec's "Tests that may change",
  plus the new tests it allows. Each task below names the exact test changes it makes.
  Any other test change: stop and ask the user.
- **No code loses its tests.** `scripts/compare_uncovered.py` (Task 1) must exit 0 for
  backend and frontend, comparing the slice's baseline against its final tree. A floor
  may then go down, written with `--update`.
- **A real bug** found on the way gets its own failing test and its own fix commit, and
  is reported to the user. It is never folded into a refactor commit.
- **Every new gate is seen red once.** The run log records how.

### This session's standing rules

- **Where to work.** Worktree `W=/home/parlar_ai/dev/grant-lab/.worktrees/clean-slice-1`,
  branch `refactor/clean-slice-1`. Start every Bash call with `cd $W &&`.
- **The live app.** It runs from `/home/parlar_ai/dev/grant-lab`. Never stop, restart or
  edit anything there before the deploy task, which happens only on the user's word.
  Never use `pkill`. Never start a second app. Port 8000 belongs to the live app.
- **Never print an `.env` file.** No calls to Ollama or Anthropic.
- **Formatting and git.**
  - Never run `ruff format` on a directory; give it file names only.
  - Never run a bare `git stash`.
  - Never use `pytest -n auto`. The local suite is
    `uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider`.
- **Mutations** are restored with `cp` and confirmed with `cmp`, and run with
  `PYTHONDONTWRITEBYTECODE=1`.
- **Commits:**
  - Titles name the change.
  - Every message ends with EXACTLY `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - PR bodies end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **Pushing, merging and deploying** happen only on the user's word. The draft PR push is
  covered by the user's approval of this plan.
- **The run log** is `docs/superpowers/plans/2026-11-05-clean-code-slice-1/run-log.md`.
  Every measured number goes there, in the task that measured it.
- **mypy stays at 0** at every commit:
  `uv run mypy --config-file pyproject.toml` → `Success: no issues found in N source files`.
  N is 120 before Task 6 and 119 after it, because Task 6 deletes one module.
- **Architecture map.** Regenerate `docs/ARCHITECTURE.md` with
  `uv run python scripts/gen_architecture.py` in any commit that moves, adds or deletes a
  module, and commit it with that change.

---

## How this plan is built

Every task's code was prototyped in throwaway worktrees at `3c1c0f31` and then applied in
order, as scratch commits, in one integrated dry-run (see `run-log.md`, "Dry-run").
The exact code of each task is a patch in the plan folder
`P=/home/parlar_ai/dev/grant-lab/.worktrees/clean-slice-1/docs/superpowers/plans/2026-11-05-clean-code-slice-1/`.
A task applies its patch, then proves it: tests, a mutation check, lint and types.
**Read the patch before applying it.** It is the task's code, and the reviewer reviews it
as code.

| Task | Patch | What it does |
|---|---|---|
| 1 | `01-compare-uncovered.patch` | adds the per-line coverage check, its tests, and the vitest `json` reporter |
| 2 | `03-calls-decisions.patch` | one decision upsert and one undo delete in `web/api/calls.py` |
| 3 | `04a-profile-fields-guard-test.patch`, `04b-profile-fields.patch` | a guard test, then the 13 structured profile fields declared once |
| 4 | `05-scheduler-guard-test.patch` | a new test pinning the tick, batch and daily cap the drain job gets |
| 5 | `06-scheduler-settings-once.patch` | `build_scheduler` reads its settings once |
| 6 | `07-remove-extraction.patch` | removes `sources/extraction.py`, `pymupdf` and `trafilatura`; adds `lxml`; adds the lock test |
| 7 | `08-remove-ws-origin.patch` | removes the CSP `ws_origin` token; keeps the `APP_BASE_URL` check |
| 8 | `09a-feed-alert-test.patch`, `09b-feed-alert-fix.patch` | the bug fix: a failed decision's alert clears when an undo succeeds |

Item 5 of the spec (merging the feed's decision code) is **skipped**, with its evidence in
the spec. It has no task. Task 9 records the skip in the run log.

---

### Task 0: Worktree and the starting totals

**Files:**
- Modify: `docs/superpowers/plans/2026-11-05-clean-code-slice-1/run-log.md` (append a
  "Run" section; the "Dry-run" section above it stays)

- [ ] **Step 1: Create the worktree from master**

```bash
cd /home/parlar_ai/dev/grant-lab && git status --short && git worktree add .worktrees/clean-slice-1 -b refactor/clean-slice-1 master
W=/home/parlar_ai/dev/grant-lab/.worktrees/clean-slice-1
cd $W && git branch --show-current && git log --oneline -1 && uv sync && pnpm -C frontend install --frozen-lockfile
```

Expected: `refactor/clean-slice-1`, and the head is master's plan commit. `git status`
in the main tree shows only the user's four research files under `docs/research/`.

- [ ] **Step 2: Measure the starting totals**

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider 2>&1 | tail -1
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
cd $W && pnpm -C frontend run test 2>&1 | grep -E "Test Files|Tests "
cd $W && uv run mypy --config-file pyproject.toml | tail -1
```

Expected, measured in the dry-run: `2436 passed`, `273 passed`, `Test Files  55 passed (55)` and `Tests  530 passed (530)`, and
`Success: no issues found in 120 source files`. If a number differs, write down the
number you measured and carry the plan's DELTAS from it. Never adjust a number to make
the arithmetic close.

- [ ] **Step 3: Start the run log and commit it**

Append a "## Run" section to `run-log.md` with the base sha (`git rev-parse HEAD`) and
the four lines above.

```bash
cd $W && git add docs/superpowers/plans/2026-11-05-clean-code-slice-1/run-log.md && git commit -qm "docs(plan): start the slice 1 run log" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 1: The per-line coverage check

**Files:**
- Create: `scripts/compare_uncovered.py`, `tests/test_compare_uncovered.py`
- Modify: `frontend/vite.config.ts` (`coverage.reporter` gains `"json"`)

**Interfaces:**
- Produces: `uv run python scripts/compare_uncovered.py --section backend|frontend --before REPORT --before-rev SHA --after REPORT [--after-rev SHA|WORKTREE]`.
  - Exit 0: nothing newly uncovered. Exit 1: something is; it prints each item as `group  path:line  text`. Exit 2: bad input.
  - Backend reports are coverage.py JSON (`--cov-report=json:PATH`).
  - Frontend reports are `frontend/coverage/coverage-final.json`, not the summary.
- The matching rule is in the spec, "No code loses its tests". The script's docstring
  states it too.

- [ ] **Step 1: Apply the patch and run its tests**

```bash
cd $W && git apply --index $P/01-compare-uncovered.patch && git status --short
cd $W && uv run pytest -q -p no:cacheprovider tests/test_compare_uncovered.py 2>&1 | tail -1
```

Expected: three paths staged, then `47 passed`.

- [ ] **Step 2: Mutation check, to prove the tests guard the reason the tool exists**

Make the comparison ignore each item's identity, so it only counts how many uncovered
items each group has (mutant M1 in the prototype). Then run the tests.

```bash
cd $W && cp scripts/compare_uncovered.py /tmp/s1-cu.bak
cd $W && uv run python - <<'PY'
p = "scripts/compare_uncovered.py"
t = open(p).read()
old = "out[(item.group, item.kind, item.identity)][item.path].append(item)"
assert t.count(old) == 1, t.count(old)
open(p, "w").write(t.replace(old, "out[(item.group, item.kind, ())][item.path].append(item)"))
PY
cd $W && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider tests/test_compare_uncovered.py 2>&1 | grep -E "^FAILED|passed|failed" | sed 's/ - .*//'
cd $W && cp /tmp/s1-cu.bak scripts/compare_uncovered.py && cmp /tmp/s1-cu.bak scripts/compare_uncovered.py && echo restored
```

Expected: `test_deleting_an_uncovered_line_does_not_offset_a_new_one` and
`test_a_frontend_branch_arm_is_part_of_its_identity` are red, then `restored`.

- [ ] **Step 3: The frontend summary is unchanged by the new reporter**

```bash
cd $W && pnpm -C frontend run test:coverage >/dev/null 2>&1; ls frontend/coverage/
cd $W && uv run python scripts/check_coverage_floors.py --section frontend --report frontend/coverage/coverage-summary.json >/dev/null; echo "floors exit=$?"
```

Expected: `coverage-final.json` and `coverage-summary.json` listed, then `floors exit=0`.

- [ ] **Step 4: Lint and commit**

```bash
cd $W && uv run ruff check . | tail -1 && uv run ruff format --check scripts/compare_uncovered.py tests/test_compare_uncovered.py
cd $W && git commit -qm "feat(scripts): compare uncovered lines before and after a change" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Measure the coverage baseline at this commit**

This commit changes no app code, so its coverage is the slice's baseline. Run the
backend twice; the second run proves that two runs of the same code give the same lines.

```bash
cd $W && BASE=$(git rev-parse HEAD) && echo $BASE > /tmp/s1-base-rev
cd $W && for i in 1 2; do uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:/tmp/s1-base-be-$i.json 2>&1 | tail -1; done
cd $W && uv run python scripts/compare_uncovered.py --section backend --before /tmp/s1-base-be-1.json --before-rev $BASE --after /tmp/s1-base-be-2.json --after-rev $BASE | tail -1; echo "exit=${PIPESTATUS[0]}"
cd $W && pnpm -C frontend run test:coverage >/dev/null 2>&1 && cp frontend/coverage/coverage-final.json /tmp/s1-base-fe.json && cp frontend/coverage/coverage-summary.json /tmp/s1-base-fe-summary.json
```

Expected: `2483 passed` (2436 + the 47 new tests) twice, then `exit=0`. Record all of it in the run log.

---

### Task 2: One decision upsert and one undo delete

**Files:**
- Modify: `src/grant_lab/web/api/calls.py`. It adds `_upsert_decision` and
  `_delete_decision`, and `hide_call`, `save_call`, `unhide_call` and `unsave_call` call
  them.

**What must not change** (spec, slice 1 item 1):
- `hide_call` and `save_call` still call `_readable_active_profile_or_404` themselves,
  before the upsert. `unhide_call` and `unsave_call` still do not.
- The four endpoint docstrings are unchanged, word for word. They are the OpenAPI
  descriptions; slice 2 rewrites them.

- [ ] **Step 1: Apply and run the decision tests**

```bash
cd $W && git apply --index $P/03-calls-decisions.patch
cd $W && uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider tests/web/test_call_decisions_api.py tests/test_api_call_detail.py tests/test_api_calls_feed_behavior.py tests/test_api_calls_hide.py tests/test_api_calls_note.py tests/test_calls_jinja_surface_absent.py tests/test_migration_0071_call_decisions.py tests/web/test_calls_decision_note_projection.py 2>&1 | tail -1
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
```

Expected: `140 passed`, then the architecture count from Task 0, unchanged.
`test_profile_access_single_source.py`'s delegation pins for `hide_call` and `save_call`
pass unchanged.

- [ ] **Step 2: Mutation checks. Each merged rule must stay guarded for both endpoints.**

Back up `src/grant_lab/web/api/calls.py` with `cp`. Run each mutation with
`PYTHONDONTWRITEBYTECODE=1` against the eight files above, restore with `cp`, and confirm
with `cmp`.

| Mutation | Expected red |
|---|---|
| in `_upsert_decision`, `"note": None` (unconditional) | `tests/test_api_calls_note.py::test_re_hiding_an_already_hidden_call_keeps_the_note` and `::test_re_saving_an_already_saved_call_keeps_the_note` |
| in `_delete_decision`, drop the `CallDecision.state == state` predicate | `tests/web/test_call_decisions_api.py::test_unhide_touches_neither_a_saved_row_nor_another_profiles_row` and `::test_unsave_does_not_touch_a_hidden_row` |
| in `_delete_decision`, drop the `CallDecision.user_id == user_id` predicate | `::test_unhide_does_not_touch_a_strangers_hidden_row_on_the_same_call_and_profile` and `::test_unsave_does_not_touch_a_strangers_saved_row_on_the_same_call_and_profile` |

- [ ] **Step 3: Lint, types, commit**

```bash
cd $W && uv run mypy --config-file pyproject.toml | tail -1 && uv run ruff check . | tail -1 && uv run ruff format --check src/grant_lab/web/api/calls.py
cd $W && git commit -qm "refactor(calls): one decision upsert and one undo delete" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The structured profile fields, declared once

Today no test notices if a structured field is dropped on its way into a profile.
Dropping `"country"` from the conversion left 243 tests green, across every test file
that mentions `country` or `/api/profiles`. This task rewrites that conversion, so a
guard test comes first, in its own commit.

**Files:**
- Create: `tests/test_api_profile_structured_fields.py`, with 5 tests.
  - Create and update are each parametrized over both profile kinds, because no single
    kind uses all 13 fields: `career_stage` is individual-only, and `org_type`,
    `capabilities`, `patient_populations` and `scale` are organizational-unit-only.
  - Each sends all 13 distinct values and reads them back from `Profile.structured`.
  - A fifth test asserts the 13 values are distinct, so a swapped key cannot pass.
- Modify: `src/grant_lab/web/api/profiles.py`. It adds `StructuredFieldsIn`, with the
  13 fields and `structured_raw()`. `ProfileCreateIn` and `ProfileUpdateIn` inherit it,
  and `create_profile` and `update_profile` call `parse_structured(kind=..., raw=body.structured_raw())`.

- [ ] **Step 1: The guard test passes on today's code**

```bash
cd $W && git apply --index $P/04a-profile-fields-guard-test.patch
cd $W && uv run pytest -q -p no:cacheprovider tests/test_api_profile_structured_fields.py 2>&1 | tail -1
```

Expected: `5 passed`.

- [ ] **Step 2: It catches a dropped field, on today's code**

Back up `profiles.py`. In `create_profile`, delete the `"country": body.country,` line from
the `raw={...}` dict, run the guard file, then restore with `cp` and confirm with `cmp`.
Do the same in `update_profile`.

Expected: create's two tests go red, then update's two.

```bash
cd $W && uv run ruff format --check tests/test_api_profile_structured_fields.py && git commit -qm "test(profiles): pin that every structured field reaches the stored profile" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Apply the refactor and run the profile tests**

```bash
cd $W && git apply --index $P/04b-profile-fields.patch
cd $W && uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider tests/test_api_profile_detail.py tests/test_api_profile_name_privacy.py tests/test_api_profile_transfer.py tests/test_api_profile_transfer_concurrency.py tests/test_api_profile_update.py tests/test_api_profile_update_scheduling.py tests/test_api_profiles_create.py tests/test_api_profiles_list.py tests/test_boards_alerts.py tests/test_profile_access.py tests/test_profile_fingerprint.py tests/test_profiles_e2e.py tests/test_profiles_forms.py tests/test_profiles_fields.py tests/test_profiles_redesign_e2e.py tests/test_profiles_jinja_surface_absent.py 2>&1 | tail -1
cd $W && uv run pytest -q -p no:cacheprovider tests/test_api_profile_structured_fields.py 2>&1 | tail -1
```

Expected: `156 passed`, then `5 passed`.

- [ ] **Step 4: The OpenAPI types do not change**

```bash
cd $W && bash scripts/gen_openapi_types.sh >/dev/null && git diff --exit-code --stat frontend/src/api/schema.gen.ts && echo "schema unchanged"
```

Expected: `schema unchanged`. The generator sorts keys, so the base class's field order
does not show.

- [ ] **Step 5: Mutation check on the shared conversion**

Back up `profiles.py`. Make `StructuredFieldsIn.structured_raw` skip `"country"`, by adding
`if name != "country"` to its comprehension. Run the guard file, then restore with `cp`
and confirm with `cmp`.

Expected: 4 red, create twice and update twice, all in
`tests/test_api_profile_structured_fields.py`. One known exception: `update_profile`
stores `areas` from `split_lines(body.areas)`, not from the parsed result. So dropping
`areas` from the conversion reddens only create, and that is today's behaviour.

- [ ] **Step 6: Lint, types, commit**

```bash
cd $W && uv run mypy --config-file pyproject.toml | tail -1 && uv run ruff check . | tail -1 && uv run ruff format --check src/grant_lab/web/api/profiles.py
cd $W && git commit -qm "refactor(profiles): declare the structured profile fields once" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Pin what the drain job gets from the scheduler

Today no test notices if `build_scheduler` wires the tick interval, the batch size or the
daily cap to the wrong value. Task 5 rewrites exactly that wiring, so this test comes first.

**Files:**
- Modify: `tests/test_ranking_freshness_wiring.py`. It adds
  `test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job`, which passes tick
  `37`, batch `11` and cap `223`, and asserts the `rank:drain` trigger interval and the
  drain kwargs.

- [ ] **Step 1: Apply and run. It passes on the current code.**

```bash
cd $W && git apply --index $P/05-scheduler-guard-test.patch
cd $W && uv run pytest -q -p no:cacheprovider "tests/test_ranking_freshness_wiring.py::test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job" 2>&1 | tail -1
```

Expected: `1 passed`.

- [ ] **Step 2: Mutation checks. Each swap must turn it red.**

Back up `src/grant_lab/scheduler.py`. In `build_scheduler`, make each swap in turn, run
the test, then restore with `cp` and confirm with `cmp`.

| Swap | Expected |
|---|---|
| the drain job's `IntervalTrigger` seconds ← `ranking_queue_batch` | red: `11.0 == 37` |
| the drain's `batch_size` ← `ranking_tick_seconds` | red: `37 == 11` |
| the drain's `remote_daily_cap` ← `ranking_queue_batch` | red: `11 == 223` |

- [ ] **Step 3: Commit**

```bash
cd $W && uv run ruff format --check tests/test_ranking_freshness_wiring.py && git commit -qm "test(scheduler): pin the tick, batch and daily cap the drain job gets" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Pass the scheduler its settings once

**Files:**
- Modify: `src/grant_lab/scheduler.py`. `build_scheduler` loses the ten value
  parameters and `digest_settings`, and reads each value from `settings`.
- Modify: `src/grant_lab/main.py`, which passes `settings` only.
- Modify: 9 test files.
  - 8 hold the 15 call sites. One of them,
    `tests/test_ranking_freshness_wiring.py`, also holds the Task 4 test, whose call
    changes the same way.
  - The 9th, `tests/test_config_escalation_settings.py`, loses only its
    `inspect.signature(build_scheduler)` parts.

**The reviewer's checklist:** `$P/06-call-sites.md` lists every call site with its old
and new values. Each must be identical. Two explicit values keep the old behaviour:

- `embedding_hard_fail_threshold=None`: the old parameter defaulted to `None`, and
  `Settings` defaults to `0.20`;
- `anthropic_api_key=None`: `Settings` reads `.env`, so a developer's key would otherwise
  turn on a remote client in tests.

**Behaviour notes for the PR:**
- The `digest_settings is not None` guard is gone, so the digest job is registered when
  `digest_email_client` is given. `main.py` always passed both, so production does not
  change.
- `embedding_hard_fail_threshold` now always comes from `Settings`, default `0.20`.
  Production already passed `settings.embedding_hard_fail_threshold`.

- [ ] **Step 1: Apply and run**

```bash
cd $W && git apply --index $P/06-scheduler-settings-once.patch
cd $W && uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider tests/test_ranking_freshness_wiring.py tests/test_ranking_scoring_config.py tests/test_config_escalation_settings.py $(git grep -l "build_scheduler(" tests) 2>&1 | tail -1
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
cd $W && uv run python scripts/gen_architecture.py && git status --short docs/ARCHITECTURE.md
```

Expected: `78 passed`, then the architecture count unchanged, then no change to
`docs/ARCHITECTURE.md`.

- [ ] **Step 2: Mutation checks on the new code**

Repeat Task 4's three swaps, now reading from `settings.*` inside `build_scheduler`. Each
must turn `test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job` red.
Restore with `cp` and confirm with `cmp`.

- [ ] **Step 3: Lint, types, commit**

```bash
cd $W && uv run mypy --config-file pyproject.toml | tail -1 && uv run ruff check . | tail -1
cd $W && uv run ruff format --check $(git diff --cached --name-only -- '*.py')
cd $W && git commit -qm "refactor(scheduler): read the ranking and digest settings from Settings once" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Remove `sources/extraction.py` and its two libraries

**Files:**
- Delete: `src/grant_lab/sources/extraction.py`, `tests/test_sources_extraction.py`
- Modify:
  - `pyproject.toml`: `pymupdf` and `trafilatura` out, `"lxml>=6.0"` in, and the
    no-build list regenerated;
  - `uv.lock`: 13 packages leave, `lxml` stays at 6.1.1;
  - the comments that named the module: the `sources/__init__.py` docstring, a
    `vinnova.py` comment and an `eu_portal.py` docstring.
- Create: `tests/architecture/test_removed_modules_stay_gone.py`, 8 tests: the lock.

**Why `lxml`:** `sources/eu_portal.py` imports `lxml` directly, but it was installed only
because `trafilatura` depends on it. Without the explicit dependency the EU source breaks.

- [ ] **Step 1: Apply, sync, and check the generated list**

```bash
cd $W && git apply --index $P/07-remove-extraction.patch && uv sync 2>&1 | tail -2
cd $W && uv run python scripts/gen_no_build.py --check
cd $W && uv run python -c "import lxml.html; print('lxml ok')"
cd $W && git grep -nE "^\s*(import|from)\s+(fitz|pymupdf|trafilatura)" -- src scripts alembic | wc -l
```

Expected: `uv sync` uninstalling 13 packages (babel, courlan, dateparser, htmldate, justext, lxml-html-clean, pymupdf, python-dateutil, pytz, regex, six, tld, trafilatura), then the no-build list in sync, `lxml ok`, and `0`.

- [ ] **Step 2: Run the lock and the source tests**

```bash
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
cd $W && uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider $(ls tests/test_sources_*.py) tests/test_gen_no_build.py 2>&1 | tail -1
cd $W && uv run mypy --config-file pyproject.toml | tail -1
```

Expected: the architecture count +8, then `112 passed`, then
`Success: no issues found in 119 source files`.

- [ ] **Step 3: Prove the lock goes red**

Use `.venv/bin/python -m pytest`, not `uv run`. `uv run` re-locks and re-syncs when
`pyproject.toml` changes, which would undo the mutation. For each mutation, back up the
file with `cp`, run `tests/architecture/test_removed_modules_stay_gone.py`, then restore
with `cp` and confirm with `cmp`.

| Mutation | Expected red |
|---|---|
| add `import trafilatura` to `src/grant_lab/sources/vinnova.py` | `test_no_python_file_imports_a_removed_library` |
| add `"trafilatura>=2.0.0",` to `[project] dependencies` | `test_pyproject_does_not_depend_on_a_removed_library` |
| append a `[[package]]` with `name = "pymupdf"` to `uv.lock` | `test_the_lock_does_not_contain_a_removed_library` |
| `touch src/grant_lab/sources/extraction.py` (restore: `rm`) | `test_the_extraction_module_is_gone` |

- [ ] **Step 4: Commit**

```bash
cd $W && uv run ruff check . | tail -1 && uv run ruff format --check tests/architecture/test_removed_modules_stay_gone.py
cd $W && git commit -qm "chore(deps): remove the unused source-extraction module, pymupdf and trafilatura" -m "lxml becomes a direct dependency: eu_portal imports it, and it was installed only through trafilatura." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Remove the CSP `ws_origin` token

**Files:**
- Modify: `src/grant_lab/main.py`. `_derive_ws_origin` becomes
  `_validate_app_base_url(app_base_url) -> None`, with the same two `RuntimeError`s. It is
  called unconditionally in `create_app`, **before the database engine is created**, like
  the other refuse-to-start checks.
- Modify: `src/grant_lab/web/middleware.py`. `build_csp` loses its `ws_origin`
  parameter, and production `connect-src` becomes `'self'`.
- Modify: `tests/test_security_headers.py`, `tests/test_spa_serving.py`. The exact
  changes are the spec's "Tests that may change", cases 3 and 5, plus one new test,
  `test_create_app_refuses_a_malformed_app_base_url`.

**Header change for the PR:** production `Content-Security-Policy` `connect-src` goes from
`'self' wss://<host>` to `'self'`. The dev CSP is unchanged; its `ws:` token serves Vite's
hot reload.

- [ ] **Step 1: Apply and run**

```bash
cd $W && git apply --index $P/08-remove-ws-origin.patch
cd $W && git grep -nE "WebSocket|wss?://" -- 'frontend/src/*.ts' 'frontend/src/*.tsx' | grep -vE "\.(test|spec)\.|schema\.gen" | wc -l
cd $W && uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider tests/test_security_headers.py tests/test_spa_serving.py tests/test_auth_csrf.py tests/test_config.py tests/test_env_example.py 2>&1 | tail -1
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
```

Expected: `0` (no frontend WebSocket), then `60 passed`, then the architecture count from
Task 6.

- [ ] **Step 2: Prove the wiring test bites**

Back up `main.py`. Replace the `_validate_app_base_url(settings.app_base_url)` call in
`create_app` with `pass`. Run `tests/test_security_headers.py`, then restore with `cp`
and confirm with `cmp`.

Expected: `test_create_app_refuses_a_malformed_app_base_url` is red. Before this slice,
nothing guarded that call except the ws-token assertions.

- [ ] **Step 3: Lint, types, commit**

```bash
cd $W && uv run mypy --config-file pyproject.toml | tail -1 && uv run ruff check . | tail -1 && uv run ruff format --check src/grant_lab/main.py src/grant_lab/web/middleware.py tests/test_security_headers.py tests/test_spa_serving.py
cd $W && git commit -qm "fix(csp): drop the WebSocket source nothing uses from production connect-src" -m "The APP_BASE_URL host check that lived in _derive_ws_origin stays, as _validate_app_base_url, and now runs before the database engine is created." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Fix: a failed decision's alert stays after a successful undo

**The bug (user-visible, feed only).** A save or hide fails and the red alert says
"… Your call is unchanged. Please try again." The user then undoes successfully, with
`u`, "Return to inbox" or the strip's Undo, and the alert stays up, although the call has
changed. `TriageFeed::decide` resets the four decision mutations; `undecide` and `undo` do
not. The call page clears its error correctly.

**Files:**
- Modify: `frontend/src/routes/TriageFeed.test.tsx`: three new tests in the "gate 9"
  describe.
- Modify: `frontend/src/routes/TriageFeed.tsx`: a `resetDecisionErrors` callback, used by
  `decide`, `undecide` and `undo`.

- [ ] **Step 1: The failing tests**

```bash
cd $W && git apply --index $P/09a-feed-alert-test.patch
cd $W && pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx 2>&1 | grep -E "Tests |✗|×|FAIL" | head -8
```

Expected: `3 failed`. They are "a failed decision's alert goes away once u succeeds",
"… once Return to inbox succeeds" and "… once the strip's Undo succeeds".

- [ ] **Step 2: The fix**

```bash
cd $W && git apply --index $P/09b-feed-alert-fix.patch
cd $W && pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx 2>&1 | grep -E "Tests "
cd $W && pnpm -C frontend run test 2>&1 | grep -E "Test Files|Tests "
cd $W && pnpm -C frontend exec tsc -p tsconfig.json && pnpm -C frontend run lint 2>&1 | tail -2
```

Expected: TriageFeed `92 passed`, then `Tests  533 passed (533)` in 55 files, then a clean
tsc and lint.

- [ ] **Step 3: Mutation checks**

Back up `TriageFeed.tsx`. Remove the `resetDecisionErrors()` call from `undecide`: the
`u` and Return to inbox tests go red. Restore, then remove it from `undo`: the Undo test
goes red. Restore with `cp` and confirm with `cmp` each time.

- [ ] **Step 4: Commit. The test and the fix go in one commit, its own.**

```bash
cd $W && git commit -qm "fix(feed): clear a failed decision's alert when an undo succeeds" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Full check, the per-line coverage check, and the floors

- [ ] **Step 1: The whole suite, twice, with coverage**

```bash
cd $W && AFTER=$(git rev-parse HEAD) && BASE=$(cat /tmp/s1-base-rev)
cd $W && for i in 1 2; do uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:/tmp/s1-final-be-$i.json 2>&1 | tail -1; done
cd $W && for i in 1 2; do pnpm -C frontend run test:coverage >/dev/null 2>&1 && cp frontend/coverage/coverage-final.json /tmp/s1-final-fe-$i.json && cp frontend/coverage/coverage-summary.json /tmp/s1-final-fe-$i-summary.json; done
```

Expected: `2475 passed` twice. The delta from Task 0 is named term by term in the run log:
2436 → 2475 is +39: Task 1 +47 (`test_compare_uncovered.py`), Task 3 +5 (`test_api_profile_structured_fields.py`), Task 4 +1, Task 6 −21 (`test_sources_extraction.py`) +8 (the lock), and Task 7 −4 +3 (two `ws_origin` tests deleted, two startup-check tests renamed, one wiring test added).

- [ ] **Step 2: No code lost its tests**

```bash
cd $W && uv run python scripts/compare_uncovered.py --section backend --before /tmp/s1-base-be-1.json --before-rev $BASE --after /tmp/s1-final-be-1.json --after-rev $AFTER; echo "backend exit=$?"
cd $W && uv run python scripts/compare_uncovered.py --section frontend --before /tmp/s1-base-fe.json --before-rev $BASE --after /tmp/s1-final-fe-1.json --after-rev $AFTER; echo "frontend exit=$?"
```

Expected: `backend exit=0` and `frontend exit=0`. Paste both full outputs into the run log,
including the per-group table and every "matched in another file" row, each with a
one-line reason why it is a move. If either exits 1, stop: a surviving line lost its
tests. Fix it with a test, never with a lower floor.

- [ ] **Step 3: The check goes red on a lost test, on the full suite**

```bash
cd $W && cp tests/test_profiles_pubmed.py /tmp/s1-pubmed.bak && rm tests/test_profiles_pubmed.py
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:/tmp/s1-mutant-be.json 2>&1 | tail -1
cd $W && uv run python scripts/compare_uncovered.py --section backend --before /tmp/s1-final-be-1.json --before-rev $AFTER --after /tmp/s1-mutant-be.json --after-rev WORKTREE | tail -3; echo "exit=${PIPESTATUS[0]}"
cd $W && cp /tmp/s1-pubmed.bak tests/test_profiles_pubmed.py && cmp /tmp/s1-pubmed.bak tests/test_profiles_pubmed.py && git status --short && echo restored
```

Expected: `2464 passed` (11 fewer), then `exit=1` with `profiles` lines listed, then `restored` with a
clean status.

- [ ] **Step 4: The floors**

```bash
cd $W && uv run python scripts/check_coverage_floors.py --section backend --report /tmp/s1-final-be-1.json | grep -E "^- "; echo "backend floors exit=${PIPESTATUS[0]}"
cd $W && uv run python scripts/check_coverage_floors.py --section frontend --report /tmp/s1-final-fe-1-summary.json | grep -E "^- "; echo "frontend floors exit=${PIPESTATUS[0]}"
cd $W && uv run python scripts/check_coverage_floors.py --section backend --update --report /tmp/s1-final-be-1.json --report /tmp/s1-final-be-2.json
cd $W && uv run python scripts/check_coverage_floors.py --section frontend --update --report /tmp/s1-final-fe-1-summary.json --report /tmp/s1-final-fe-2-summary.json
cd $W && git diff coverage-floors.json
```

Expected: backend `sources: line 92.0% is below its floor 92.8%` and `sources: branch 83.3% is below its floor 84.6%` (exit 1), and frontend exit 0. The `--update` diff, measured in the dry-run:
  - backend `sources`: line 92.8 → 92.0 and branch 84.6 → 83.3. Down, because deleting `extraction.py` removed 84 covered lines; `sources` went from 42 to 40 uncovered lines and from 34 to 33 uncovered branches;
  - backend `web`: branch 89.5 → 89.8. Up, from the new tests;
  - frontend `<total>`: branch 87.2 → 87.3. Up, from the Task 8 tests. Step 2's exit 0 is what permits a lower floor. For every group
whose floor goes down, the run log records its uncovered lines and branches before and
after, and the reason: deleted covered code. Every group whose floor goes up is recorded
too.

- [ ] **Step 5: Everything else**

```bash
cd $W && uv run mypy --config-file pyproject.toml | tail -1 && uv run ruff check . | tail -1
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
cd $W && pnpm -C frontend run lint 2>&1 | tail -1 && pnpm -C frontend exec tsc -p tsconfig.json && echo tsc ok
cd $W && pnpm -C frontend run e2e:match 2>&1 | tail -3
cd $W && bash scripts/gen_openapi_types.sh >/dev/null && git diff --exit-code --stat frontend/src/api/schema.gen.ts && echo "schema unchanged"
cd $W && uv run python scripts/gen_no_build.py --check && uv run python scripts/gen_architecture.py && git status --short
```

Expected: `Success: no issues found in 119 source files`, `All checks passed!`,
`281 passed` (273 + the 8 lock tests), a clean lint and tsc, `e2e:match` `40 passed`, `schema unchanged`, and a status
that shows only `coverage-floors.json`.

- [ ] **Step 6: Record and commit**

The run log gets:
- every number above;
- item 5's skip, with its evidence from the spec;
- the call page's test gaps for piece 4.

```bash
cd $W && git add coverage-floors.json docs/superpowers/plans/2026-11-05-clean-code-slice-1/run-log.md && git commit -qm "chore(coverage): floors after removing dead and repeated code" -m "No surviving line or branch lost its tests (scripts/compare_uncovered.py, exit 0 for backend and frontend); a floor drops only where covered code was deleted." -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Draft PR and CI

- [ ] **Step 1: Push and open a draft PR (covered by the plan approval)**

```bash
cd $W && git push -u origin refactor/clean-slice-1
cd $W && gh pr create --draft --base master --head refactor/clean-slice-1 --title "Piece 3, slice 1: one rule per place, dead code out" --body $'Piece 3 slice 1 of the match clean-up. Spec: docs/superpowers/specs/2026-11-05-clean-code-design.md. Plan: docs/superpowers/plans/2026-11-05-clean-code-slice-1.md.\n\nBehaviour changes, all deliberate:\n- production CSP connect-src is now \'self\' (no WebSocket source; nothing opens one);\n- the feed clears a failed decision\'s alert when an undo succeeds (bug fix);\n- pymupdf and trafilatura are gone; lxml is a direct dependency.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)'
cd $W && gh pr checks --watch --interval 30
```

Expected: every job green except `dependency-review`, the known repo setting. If a floors
step fails in CI, CI measured lower than local. Download its report (`gh run download <run>
-n coverage-backend`), rewrite the floors with all three reports, and push again.

- [ ] **Step 2: Record the CI job times in the run log, and commit and push it**

---

### Task 11: Merge, deploy, push (each on the user's word)

- [ ] **Step 1: Ask the user.** Show the green PR checks, the run log's coverage check and
  floors, and the three behaviour changes. The deploy restarts the app for about a minute.
  `uv sync --frozen` removes 13 Python packages.

- [ ] **Step 2: Deploy.** The same recipe as piece 2. Run one line at a time, and stop on
  any failure.

```bash
~/grant-lab-backups/backup.sh || { echo "backup failed: stop"; exit 1; }
systemctl --user stop grant-lab-app
cd /home/parlar_ai/dev/grant-lab && git status --short && git merge --ff-only refactor/clean-slice-1 || { echo "merge failed: restarting the unchanged app"; systemctl --user start grant-lab-app; exit 1; }
cd /home/parlar_ai/dev/grant-lab && uv sync --frozen && pnpm -C frontend install --frozen-lockfile && pnpm -C frontend run build && pnpm -C frontend run package && systemctl --user start grant-lab-app && sleep 5 && curl -s http://127.0.0.1:8000/healthz
```

Expected: `{"status":"ok","scheduler_lock":true}`. Then the log since the start has no
errors, and the production CSP header has `connect-src 'self'`:
`curl -sI http://127.0.0.1:8000/ | grep -i content-security-policy`.

- [ ] **Step 3: Push master, and watch its CI until green**

```bash
cd /home/parlar_ai/dev/grant-lab && git push origin master
```

- [ ] **Step 4: Clean up.** Remove the worktree with
  `git -C /home/parlar_ai/dev/grant-lab worktree remove .worktrees/clean-slice-1`.
  Update memory: slice 1 done, and slice 2 next.
