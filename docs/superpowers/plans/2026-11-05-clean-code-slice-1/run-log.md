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

#### Task 1 — the gate itself seen red (global constraint "every new gate is seen red once")

The task's own mutation check (M1) proves the *tests* guard the tool. It does not show
the **tool** refusing a real loss. The outside review of Task 1 raised that gap, so the
gate was exercised against the slice's own baseline reports with losses planted in
copies (the baseline files themselves untouched; `--before-rev` and `--after-rev` both
`f22019eb`, so every planted line still exists at the after-rev).

```
### 1. backend, a surviving line loses its coverage
<root>  src/grant_lab/admin_bootstrap.py:9  from grant_lab.auth.passwords import hash_password
FAIL: 1 new uncovered line(s), 0 new uncovered branch(es); 0 matched in another file of its group, 0 branch(es) in code that did not run before
exit=1

### 2. backend, a partial after report (4 surviving files dropped)
error: /tmp/s1-partial-be.json: 4 file(s) of f22019eb8233 are not in the report, such as src/grant_lab/__init__.py; was it measured on that revision, with --cov?
exit=2

### 3. frontend, a surviving line AND a branch arm lose their coverage
<root>  frontend/src/App.tsx:1  import { MantineProvider } from "@mantine/core";
<root>  frontend/src/App.tsx:45->45  retry: (n, e) => !is4xx(e) && n < 1,
FAIL: 1 new uncovered line(s), 1 new uncovered branch(es); 0 matched in another file of its group, 0 branch(es) in code that did not run before
exit=1

### 4. control, the two baseline backend runs against each other
OK: 0 new uncovered line(s), 0 new uncovered branch(es); 0 matched in another file of its group, 0 branch(es) in code that did not run before
exit=0
```

This is the first run of the **frontend** path of the gate on a real report; the task's
own steps exercised only the backend path. Both are now seen red once and green once.

**Known fail-open, carried forward, not fixed in this slice.** Frontend branch identity
is `(from-text, to-text, "arm i")` and position is `(from-line, to-line)`; columns are
never read. The v8 provider emits every branch entry as single-arm, and **341 of the
baseline's 1440 branch entries share a (from-line, to-line) pair with another entry in
the same file**. So a *swap* on such a line — one arc gains coverage while its sibling
loses it — matches by count and exits 0. A *net* loss at the same shape is still caught
(run 3 above is exactly that shape on `App.tsx:45->45`). Making the identity
column-precise would change the gate's documented matching rule, which is a design
change and outside this slice; Task 9 instead checks the slice's frontend change for a
hidden swap directly, per branch entry.

### Task 2 — one decision upsert and one undo delete

Base for this task: `925fd129` (`docs(plan): record the coverage gate refusing a real
planted loss`). Patch: `03-calls-decisions.patch`, one file,
`src/grant_lab/web/api/calls.py`. Read before applying: it adds `_upsert_decision` and
`_delete_decision` and points `hide_call`, `save_call`, `unhide_call` and `unsave_call`
at them, with no change to any of the four docstrings and no change to which two of the
four call `_readable_active_profile_or_404`.

**Step 1 — apply and run the decision tests:**

```
$ git apply --check docs/superpowers/plans/2026-11-05-clean-code-slice-1/03-calls-decisions.patch && echo "CHECK OK" && git apply --index docs/superpowers/plans/2026-11-05-clean-code-slice-1/03-calls-decisions.patch && echo APPLIED
CHECK OK
APPLIED
```

```
$ uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider tests/web/test_call_decisions_api.py tests/test_api_call_detail.py tests/test_api_calls_feed_behavior.py tests/test_api_calls_hide.py tests/test_api_calls_note.py tests/test_calls_jinja_surface_absent.py tests/test_migration_0071_call_decisions.py tests/web/test_calls_decision_note_projection.py
bringing up nodes...
........................................................................ [ 51%]
....................................................................     [100%]
140 passed in 31.47s
```

```
$ uv run pytest -q -p no:cacheprovider tests/architecture/
........................................................................ [ 26%]
........................................................................ [ 52%]
........................................................................ [ 79%]
.........................................................                [100%]
273 passed in 17.51s
```

Both match the brief exactly: `140 passed`, and `273 passed` for the architecture suite
(Task 0's measured count in this worktree, unchanged). `0 skipped`, `0 xfailed`.
`tests/architecture/test_profile_access_single_source.py` is one of the 273 — it still
pins `"web/api/calls.py::hide_call": "_readable_active_profile_or_404"` and
`"web/api/calls.py::save_call": "_readable_active_profile_or_404"` and still lists
`unhide_call`/`unsave_call` as deliberately absent from that map — and it passed.

**Step 2 — mutation checks.** For each: `cp src/grant_lab/web/api/calls.py
/tmp/calls.py.orig` once before the three, mutate with a small Python script, run the
eight files above with `PYTHONDONTWRITEBYTECODE=1`, restore with `cp
/tmp/calls.py.orig src/grant_lab/web/api/calls.py`, confirm with `cmp`.

*M1 — in `_upsert_decision`, replace the conditional `"note": case(...)` with an
unconditional `"note": None`:*

```
FAILED tests/test_api_calls_note.py::test_re_hiding_an_already_hidden_call_keeps_the_note
FAILED tests/test_api_calls_note.py::test_re_saving_an_already_saved_call_keeps_the_note
2 failed, 138 passed in 31.42s
```

Matches the brief's expected-red column exactly, for both callers (hide-side and
save-side both broke). `cmp src/grant_lab/web/api/calls.py /tmp/calls.py.orig` after
restore: no output — identical.

*M2 — in `_delete_decision`, drop the `CallDecision.state == state` predicate from the
`delete(...).where(...)`:*

```
FAILED tests/web/test_call_decisions_api.py::test_unhide_touches_neither_a_saved_row_nor_another_profiles_row
FAILED tests/web/test_call_decisions_api.py::test_unsave_does_not_touch_a_hidden_row
2 failed, 138 passed in 32.08s
```

Matches the brief's expected-red column exactly, for both callers (unhide and unsave).
`cmp` after restore: no output — identical.

*M3 — in `_delete_decision`, drop the `CallDecision.user_id == user_id` predicate from
the same `where(...)`:*

```
FAILED tests/web/test_call_decisions_api.py::test_unhide_does_not_touch_a_strangers_hidden_row_on_the_same_call_and_profile
FAILED tests/web/test_call_decisions_api.py::test_unsave_does_not_touch_a_strangers_saved_row_on_the_same_call_and_profile
2 failed, 138 passed in 31.26s
```

Matches the brief's expected-red column exactly, for both callers. `cmp` after restore:
no output — identical.

All three mutations reddened exactly the two expected test names, no more and no fewer,
with the same `2 failed, 138 passed` total (140 - 2) each time, and zero collection
ERRORs — not a broken import. Each merged rule (the conditional note-clear, the state
filter, the user_id filter) stayed guarded for *both* endpoints that now share it.

After all three restores, re-ran the eight files once more as a final check:
`140 passed in 31.88s` — and `git diff --stat 925fd129 -- src/grant_lab/web/api/calls.py`
showed the file was back to the applied-patch state (93 insertions, 82 deletions versus
base), not the pre-patch state and not a mutated state.

**What must not change — evidence:**

- `hide_call` and `save_call` still call `_readable_active_profile_or_404(session, user,
  profile_id)` immediately before `_upsert_decision(...)`, inside their own bodies; the
  patch's diff touches only the lines after that call (the old inline `pg_insert`/
  `on_conflict_do_update` block, replaced by the one-line helper call). Confirmed by
  reading the post-patch file at both sites (`hide_call` line range ~458–478, `save_call`
  line range ~547–567) and by the architecture pin above, which asserts the same
  delegation and passed.
- `unhide_call` and `unsave_call` call `_delete_decision(...)` directly after their
  `session.get(Call, call_id)` 404 check, with no `_readable_active_profile_or_404` call
  anywhere in either body — confirmed by reading both post-patch bodies, and by
  `test_profile_access_single_source.py` listing both as deliberately absent from the
  delegation map.
- The four docstrings (`hide_call`, `unhide_call`, `save_call`, `unsave_call`) are
  byte-identical to the pre-patch file: the patch's diff hunks for all four endpoints
  start and end inside the function body, below the closing `"""` of each docstring —
  no docstring line appears in any `-`/`+` line of the patch. Verified by reading the
  full pre- and post-patch text of each docstring side by side; they match word for
  word, including the em-dashes and double-hyphens.

**Step 3 — lint, types:**

```
$ uv run mypy --config-file pyproject.toml | tail -1
Success: no issues found in 120 source files
```

```
$ uv run ruff check . | tail -1
All checks passed!
```

```
$ uv run ruff format --check src/grant_lab/web/api/calls.py
1 file already formatted
```

mypy's 120 matches the global constraint's pre-Task-6 N.

**Commit:** `refactor(calls): one decision upsert and one undo delete`, plus this
run-log entry in the same commit (see report for the exact sha).

**Nothing in the patch diverged from the brief.** The only judgment call: the brief's
Step 1 expected-output line asks for `tail -1` on the architecture run; the full dotted
progress output is included above as well, for the same reason the eight-file run's
dots are included — to show `0 skipped`/`0 xfailed` and no collection errors, not just
the last line.

### Task 3 — the structured profile fields, declared once

Base for this task: `df1b393a` (`refactor(calls): one decision upsert and one undo
delete`). Two patches: `04a-profile-fields-guard-test.patch` (new file,
`tests/test_api_profile_structured_fields.py`, 5 tests, its own commit first) and
`04b-profile-fields.patch` (`src/grant_lab/web/api/profiles.py`: adds
`StructuredFieldsIn` with the 13 fields and `structured_raw()`; `ProfileCreateIn` and
`ProfileUpdateIn` inherit it; `create_profile` and `update_profile` call
`parse_structured(kind=..., raw=body.structured_raw())` instead of each hand-listing
the 13 keys).

**Step 1 — guard test passes on today's code:**

```
$ git apply --index docs/superpowers/plans/2026-11-05-clean-code-slice-1/04a-profile-fields-guard-test.patch
$ uv run pytest -q -p no:cacheprovider tests/test_api_profile_structured_fields.py
.....                                                                    [100%]
5 passed in 6.52s
```

Matches the brief exactly: `5 passed`.

**Step 2 — mutation checks on today's (pre-refactor) code.** Backed up with `cp
src/grant_lab/web/api/profiles.py /tmp/profiles.py.orig` once before both mutations,
restored with `cp` and confirmed with `cmp` after each, ran with
`PYTHONDONTWRITEBYTECODE=1`.

*Mutation A — delete the `"country": body.country,` line (line 407) from the `raw={...}`
dict inside `create_profile`:*

```
FAILED tests/test_api_profile_structured_fields.py::test_create_stores_every_structured_field[individual]
FAILED tests/test_api_profile_structured_fields.py::test_create_stores_every_structured_field[organizational_unit]
2 failed, 3 passed in 6.53s
```

`AssertionError: geo.country: stored None, sent 'Norway'` — the create side only, both
kinds. `cmp src/grant_lab/web/api/profiles.py /tmp/profiles.py.orig` after restore: no
output — identical.

*Mutation B — same deletion, this time the `"country": body.country,` line (line 579)
inside `update_profile`'s `raw={...}` dict:*

```
FAILED tests/test_api_profile_structured_fields.py::test_update_stores_every_structured_field[individual]
FAILED tests/test_api_profile_structured_fields.py::test_update_stores_every_structured_field[organizational_unit]
2 failed, 3 passed in 6.75s
```

The update side only, both kinds. `cmp` after restore: no output — identical.

Matches the brief's expectation exactly: "create's two tests go red, then update's
two." Both mutations reddened exactly the two tests named for that endpoint — no more,
no fewer — and neither run showed a collection ERROR.

```
$ uv run ruff format --check tests/test_api_profile_structured_fields.py
1 file already formatted
```

Committed: `43c7155c` — `test(profiles): pin that every structured field reaches the
stored profile`.

**Step 3 — apply the refactor, run the profile tests:**

```
$ git apply --check docs/superpowers/plans/2026-11-05-clean-code-slice-1/04b-profile-fields.patch && echo CHECK_OK
CHECK_OK
$ git apply --index docs/superpowers/plans/2026-11-05-clean-code-slice-1/04b-profile-fields.patch
```

```
$ uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider tests/test_api_profile_detail.py tests/test_api_profile_name_privacy.py tests/test_api_profile_transfer.py tests/test_api_profile_transfer_concurrency.py tests/test_api_profile_update.py tests/test_api_profile_update_scheduling.py tests/test_api_profiles_create.py tests/test_api_profiles_list.py tests/test_boards_alerts.py tests/test_profile_access.py tests/test_profile_fingerprint.py tests/test_profiles_e2e.py tests/test_profiles_forms.py tests/test_profiles_fields.py tests/test_profiles_redesign_e2e.py tests/test_profiles_jinja_surface_absent.py
bringing up nodes...
........................................................................ [ 46%]
........................................................................ [ 92%]
............                                                             [100%]
156 passed in 35.71s
```

```
$ uv run pytest -q -p no:cacheprovider tests/test_api_profile_structured_fields.py
.....                                                                    [100%]
5 passed in 6.56s
```

Both match the brief exactly: `156 passed`, then `5 passed`. `0 skipped`, `0 xfailed`,
no collection ERRORs.

**Step 4 — OpenAPI types do not change:**

```
$ bash scripts/gen_openapi_types.sh
✨ openapi-typescript 7.13.0
🚀 /tmp/tmp.E35mjmCLBP → src/api/schema.gen.ts [276ms]
wrote frontend/src/api/schema.gen.ts
$ git diff --exit-code --stat frontend/src/api/schema.gen.ts && echo "schema unchanged"
schema unchanged
```

No `EMBEDDING_MODEL` workaround was needed: `scripts/gen_openapi_types.sh` already
pins `EMBEDDING_MODEL=bge-m3` internally (see its comment on the calibration-warning
gotcha), so the gotcha did not fire. No network call was attempted — `DATABASE_URL` is
a dummy psycopg3 URL the engine never connects with, and nothing in the generator
reaches Ollama or Anthropic.

**Step 5 — mutation check on the shared conversion.** Backed up with `cp
src/grant_lab/web/api/profiles.py /tmp/profiles.py.refactored`. Edited
`StructuredFieldsIn.structured_raw`'s comprehension to add `if name != "country"`:

```python
    def structured_raw(self) -> dict[str, str]:
        """The `raw` mapping `parse_structured` takes, built from the 13 fields."""
        return {
            name: getattr(self, name)
            for name in StructuredFieldsIn.model_fields
            if name != "country"
        }
```

```
$ uv run pytest -q -p no:cacheprovider tests/test_api_profile_structured_fields.py
FAILED tests/test_api_profile_structured_fields.py::test_create_stores_every_structured_field[individual]
FAILED tests/test_api_profile_structured_fields.py::test_create_stores_every_structured_field[organizational_unit]
FAILED tests/test_api_profile_structured_fields.py::test_update_stores_every_structured_field[individual]
FAILED tests/test_api_profile_structured_fields.py::test_update_stores_every_structured_field[organizational_unit]
4 failed, 1 passed in 6.52s
```

Matches the brief's expectation exactly: 4 red, create twice and update twice, all in
`tests/test_api_profile_structured_fields.py` (the 5th, the distinctness test, stayed
green — it never calls the endpoints). `cp /tmp/profiles.py.refactored
src/grant_lab/web/api/profiles.py` then `cmp` after restore: no output — identical.

**The `areas` exception — confirmed, and no other field shares it.** Read
`update_profile` post-patch: it builds `base = {"areas": split_lines(body.areas),
"budget_min": ..., "budget_max": ..., "trl_min": ..., "trl_max": ...,
"must_include": ..., "hard_exclusions": ...}`, calls `new_ns =
parse_structured(kind=p.kind, raw=body.structured_raw())`, then `new_ns.pop("areas",
None)` (comment: `# base owns areas`) before `p.structured = {**base, **new_ns}`. Of
`StructuredFieldsIn`'s 13 field names, `areas` is the only one that also appears as a
key in `base`; the other 12 (`career_stage`, `applicant_types`, `role`,
`years_since_phd`, `org_type`, `capabilities`, `patient_populations`, `scale`,
`funding_instruments`, `funder_exclusions`, `country`, `host_institution`) are absent
from `base`, so for all 12 of them `update_profile`'s stored value comes only from
`new_ns`, i.e. only from `structured_raw()`. Did not re-run the `areas`-dropped
mutation (the brief names it as today's already-known behaviour, not something to
re-prove), but confirmed by reading the code that no field besides `areas` has a
`base`-sourced fallback that would mask a `structured_raw()` drop on the update side.

**Step 6 — lint, types:**

```
$ uv run mypy --config-file pyproject.toml | tail -1
Success: no issues found in 120 source files
```

```
$ uv run ruff check . | tail -1
All checks passed!
```

```
$ uv run ruff format --check src/grant_lab/web/api/profiles.py
1 file already formatted
```

mypy's 120 matches the global constraint's pre-Task-6 N (unchanged by this task — no
module added, moved or deleted, so `docs/ARCHITECTURE.md` was not regenerated).

**Commit:** `refactor(profiles): declare the structured profile fields once`, plus this
run-log entry in the same commit.

**Nothing in the patches diverged from the brief.** One judgment call: Step 4's
`EMBEDDING_MODEL=bge-m3` workaround the brief flags as a known gotcha turned out to be
unnecessary here, because the script pins it itself; reported that instead of running
the workaround unconditionally.

### Task 4 — pin what the drain job gets from the scheduler

Base commit `4b022efe` (same tree Task 3 landed on; worktree was clean before this
task). Pure test-addition task: adds
`test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job` to
`tests/test_ranking_freshness_wiring.py`; no `src/` file touched.

**Step 1 — apply and run unmutated:**

```
$ git apply --index $P/05-scheduler-guard-test.patch
$ uv run pytest -q -p no:cacheprovider "tests/test_ranking_freshness_wiring.py::test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job" 2>&1 | tail -1
1 passed in 3.94s
```

**Step 2 — mutation checks.** `src/grant_lab/scheduler.py` backed up to
`/tmp/scheduler.py.orig` with `cp` once; each swap made directly in `build_scheduler`
(lines 480/481/490, the `_rank_drain_job` body and its `add_job` trigger), run with
`PYTHONDONTWRITEBYTECODE=1`, restored with `cp` from the same backup, confirmed with
`cmp` after each restore.

Mutation 1 — the `rank:drain` trigger's `IntervalTrigger(seconds=ranking_tick_seconds)`
→ `IntervalTrigger(seconds=ranking_queue_batch)`:

```
            assert job is not None
            job.func()
>           assert job.trigger.interval.total_seconds() == 37
E           AssertionError: assert 11.0 == 37
E            +  where 11.0 = <built-in method total_seconds of datetime.timedelta object at 0x7a4675b75290>()
E            +    where <built-in method total_seconds of datetime.timedelta object at 0x7a4675b75290> = datetime.timedelta(seconds=11).total_seconds
E            +      where datetime.timedelta(seconds=11) = <IntervalTrigger (interval=datetime.timedelta(seconds=11), start_date='2026-11-06 01:14:57 CET', timezone='Europe/Stockholm')>.interval
E            +        where <IntervalTrigger (interval=datetime.timedelta(seconds=11), start_date='2026-11-06 01:14:57 CET', timezone='Europe/Stockholm')> = <Job (id=rank:drain name=Drain ranking job queue)>.trigger

tests/test_ranking_freshness_wiring.py:227: AssertionError
1 failed in 3.96s
```

Matches the brief's predicted `11.0 == 37` exactly. `cp /tmp/scheduler.py.orig
src/grant_lab/scheduler.py` then `cmp` against the backup: no output — identical. Test
re-run unmutated: `1 passed in 3.77s`.

Mutation 2 — the drain's `batch_size=ranking_queue_batch` → `batch_size=ranking_tick_seconds`:

```
>       assert seen["drain"]["batch_size"] == 11
E       assert 37 == 11

tests/test_ranking_freshness_wiring.py:231: AssertionError
1 failed in 4.06s
```

Matches the brief's predicted `37 == 11` exactly. `cp` restore then `cmp`: identical.
Test re-run unmutated: `1 passed in 3.28s`.

Mutation 3 — the drain's `remote_daily_cap=ranking_remote_daily_cap` →
`remote_daily_cap=ranking_queue_batch`:

```
        assert seen["drain"]["batch_size"] == 11
>       assert seen["drain"]["remote_daily_cap"] == 223
E       assert 11 == 223

tests/test_ranking_freshness_wiring.py:232: AssertionError
1 failed in 3.84s
```

Matches the brief's predicted `11 == 223` exactly. `cp` restore then `cmp`: identical.
Test re-run unmutated: `1 passed in 3.86s`.

All three swaps reddened on the exact predicted comparison, with the FAILED test name
in each case being the new test itself — not a collection error, not a different test.
After the third restore, the whole file was re-run: `tests/test_ranking_freshness_wiring.py`
→ `7 passed in 5.00s`.

**Lint:**

```
$ uv run ruff format --check tests/test_ranking_freshness_wiring.py
1 file already formatted
```

`git status --porcelain` after the mutation round showed only
`M  tests/test_ranking_freshness_wiring.py` — no `src/` file left dirty, confirming the
backup/restore cycle left `scheduler.py` byte-identical to the base commit.

```
$ uv run mypy --config-file pyproject.toml | tail -1
Success: no issues found in 120 source files
```

N unchanged at 120 (matches the pre-Task-6 global constraint): no module added, moved
or deleted, so `docs/ARCHITECTURE.md` was not regenerated.

**Commit:** `test(scheduler): pin the tick, batch and daily cap the drain job gets`,
plus this run-log entry in the same commit.

**Nothing in the patch diverged from the brief.** No judgment calls were needed: the
patch's test body, the three mutation targets' line locations, and all three predicted
comparisons matched on the first try.

### Task 5 — pass the scheduler its settings once

Base commit `5f293d28` (Task 4's commit; worktree clean before this task). Patch
`06-scheduler-settings-once.patch`: `build_scheduler` loses its ten value parameters
and `digest_settings`, reading each from `settings` instead; `main.py` passes
`settings` only; 8 test files' call sites updated to match (17 real
`build_scheduler(` invocations found and fixed — the checklist's own Notes already
flagged the raw grep total as 17, "the task said ~18"; this run measured 17, not the
task text's "15", and reports the measured number rather than forcing the arithmetic
to close); `tests/test_config_escalation_settings.py` loses only its two
`inspect.signature(build_scheduler)` asserts (both asserts of a parameter *default*
value, the approved category).

**Step 1 — apply and run:**

```
$ git apply --index $P/06-scheduler-settings-once.patch
$ uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider tests/test_ranking_freshness_wiring.py tests/test_ranking_scoring_config.py tests/test_config_escalation_settings.py $(git grep -l "build_scheduler(" tests) 2>&1 | tail -1
78 passed in 9.54s
$ uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
273 passed in 17.68s
$ uv run python scripts/gen_architecture.py && git status --short docs/ARCHITECTURE.md
wrote docs/ARCHITECTURE.md (192 lines)
(no output from git status --short — unchanged)
```

Matches the brief's expectation: `78 passed`, architecture count unchanged at the
measured baseline of `273 passed`, no change to `docs/ARCHITECTURE.md`.

**Call-site walk-through.** Every call site in the 8 files carries the common NEW
block (`ollama_url="http://localhost:11434"`, `gemma_model="gemma4:e4b-q8"`,
`ranking_tick_seconds=60`, `ranking_queue_batch=10`,
`embedding_pregate_threshold=0.30`, `embedding_hard_fail_threshold=None`,
`anthropic_api_key=None`) applied via `settings.model_copy(update={...})`, except
where the checklist calls out a deliberately different value:
`test_profiles_bootstrap_worker.py` keeps `gemma_model="g"`;
`test_ranking_freshness_wiring.py`'s `test_the_scheduler_hands_enqueue_the_drain_values`
keeps its own 8 distinct values (pregate 0.27, hard_fail 0.18, api_key=`api_key`,
escalation_min_score=50) and its guard-test sibling keeps tick=37/batch=11/cap=223
with hard_fail/api_key None; `test_ranking_remote_effort.py` drops the old
self-referential `ollama_url=settings.ollama_url` / `gemma_model=settings.gemma_model`
kwargs entirely (checklist: "dropped") and keeps its own tick/batch/pregate/api_key/
remote_rank_model; `test_scheduler_digest.py` sets `digest_dry_run=False` and
`admin_dashboard_url="http://app/"` directly on `settings` for both the `with_digest`
and non-digest branches (same object either way — the non-digest case never reads
them). All read and confirmed directly in the files, not assumed from the patch.

**Step 2 — mutation checks on the new code.** `src/grant_lab/scheduler.py` backed up
to `/tmp/scheduler_task5.py.orig` with `cp`; `md5sum` confirmed the backup matched the
live file before any mutation. Each swap targeted the same three lines Task 4 targeted
(now reading `settings.ranking_tick_seconds` / `settings.ranking_queue_batch` /
`settings.ranking_remote_daily_cap`), run with `PYTHONDONTWRITEBYTECODE=1`, restored
with `cp` from the backup, confirmed with `cmp` after each restore.

Mutation 1 — `IntervalTrigger(seconds=settings.ranking_tick_seconds)` →
`IntervalTrigger(seconds=settings.ranking_queue_batch)`:

```
tests/test_ranking_freshness_wiring.py:235: in test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job
    assert job.trigger.interval.total_seconds() == 37
E   AssertionError: assert 11.0 == 37
E    +  where 11.0 = <built-in method total_seconds of datetime.timedelta object at 0x7cce05e70150>()
E    +    where <built-in method total_seconds of datetime.timedelta object at 0x7cce05e70150> = datetime.timedelta(seconds=11).total_seconds
E    +      where datetime.timedelta(seconds=11) = <IntervalTrigger (interval=datetime.timedelta(seconds=11), start_date='2026-11-06 01:24:37 CET', timezone='Europe/Stockholm')>.interval
E    +        where <IntervalTrigger (interval=datetime.timedelta(seconds=11), start_date='2026-11-06 01:24:37 CET', timezone='Europe/Stockholm')> = <Job (id=rank:drain name=Drain ranking job queue)>.trigger
1 failed in 3.87s
```

`11.0 == 37` — identical shape to Task 4's mutation 1. `cp` restore then `cmp`: no
output (identical).

Mutation 2 — `batch_size=settings.ranking_queue_batch` →
`batch_size=settings.ranking_tick_seconds`:

```
tests/test_ranking_freshness_wiring.py:239: in test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job
    assert seen["drain"]["batch_size"] == 11
E   assert 37 == 11
1 failed in 3.84s
```

`37 == 11` — identical shape to Task 4's mutation 2. `cp` restore then `cmp`: identical.

Mutation 3 — `remote_daily_cap=settings.ranking_remote_daily_cap` →
`remote_daily_cap=settings.ranking_queue_batch`:

```
tests/test_ranking_freshness_wiring.py:240: in test_the_scheduler_wires_tick_batch_and_daily_cap_to_the_drain_job
    assert seen["drain"]["remote_daily_cap"] == 223
E   assert 11 == 223
1 failed in 3.95s
```

`11 == 223` — identical shape to Task 4's mutation 3. `cp` restore then `cmp`:
identical.

All three swaps reddened on the exact comparison Task 4's mutations predicted, each
time on the new test itself (not a collection error, not a different test). After the
third restore, the whole file was re-run: `tests/test_ranking_freshness_wiring.py` →
`7 passed in 4.36s` — `0 skipped`, `0 xfailed`, confirming the restore left
`scheduler.py` byte-identical and nothing else in the file was perturbed.

**The two explicit values.** `embedding_hard_fail_threshold=None` and
`anthropic_api_key=None` appear in every common-NEW `model_copy(update={...})` block
across all 8 test files (confirmed by direct read, not patch-trust) — without them,
`Settings()`'s real defaults (`0.20` and whatever a developer's `.env` sets) would
leak into tests that expect the old parameter defaults of `None`.

**The two behaviour notes, confirmed by reading `main.py` (not inferred):**
- `main.py`'s `build_scheduler(...)` call, both before and after the patch, always
  passed `digest_email_client=digest_email_client` and (pre-patch)
  `digest_settings=settings` together — the same `settings` object already used
  everywhere else in the call. Removing the `digest_settings is not None` guard in
  `scheduler.py` cannot change production behaviour: `main.py` never passed `None` for
  either.
- `main.py`'s pre-patch call already read
  `embedding_hard_fail_threshold=settings.embedding_hard_fail_threshold` — identical to
  what `build_scheduler` now reads directly off `settings` post-patch. No behaviour
  change in production.

**Step 3 — lint, types:**

```
$ uv run mypy --config-file pyproject.toml | tail -1
Success: no issues found in 120 source files
$ uv run ruff check . | tail -1
All checks passed!
$ uv run ruff format --check $(git diff --cached --name-only -- '*.py')
11 files already formatted
```

N unchanged at 120 (pre-Task-6 global constraint). No module added, moved or deleted,
but `gen_architecture.py` was still run per Step 1 and produced no diff (confirmed
above), so nothing further to commit there.

**Full backend suite** (beyond the brief's narrower Step 1 selection, run because this
task touches `main.py`'s lifespan wiring and 8 test files — the widest surface of any
task so far): `uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider` →
`2501 passed in 49.59s`. `0 skipped`, `0 xfailed`.

**Nothing in the patch diverged from the brief or the checklist.** Every call site
matched its prescribed old/new values on direct read. The only measured discrepancy
was the checklist's own stated raw-grep count (17, not the task text's approximate
"15") — already flagged in the checklist's own Notes section, not a defect in the
patch.

### Task 5 — review fixes

Review of Task 5 (`8bfb4b9c`) returned two Important findings: four of the ten
`settings.*` reads inside `build_scheduler` were guarded by nothing (measured: swapping
`embedding_hard_fail_threshold` → `embedding_pregate_threshold` gave `78 passed` on
the narrow selection and `2501 passed` on the full suite — silently green), and the
comment above `freshness = FreshnessContext(...)` still said "Built from these
locals, never from Settings" when both sides now read `settings`. Base `8bfb4b9c`, one
commit on top.

**Fix 1 — absolute pins on two of the four unguarded reads.** Added two assertions to
the existing test `tests/test_ranking_freshness_wiring.py::test_the_scheduler_hands_enqueue_the_drain_values`
(it already passes hard_fail `0.18` and escalation `50`, and already captures the
drain kwargs as `d`):

```python
assert d["embedding_hard_fail_threshold"] == 0.18
assert d["escalation_min_score"] == 50
```

Both compare `d[...]` against the literal number, not against anything derived from
`d`/`want` — the self-referential `want = ScoringConfig.for_drain(**{k: d[k] ...})`
check a few lines above cannot catch a wrong-value swap on either side of itself
because it rebuilds `want` from `d`.

**Secondary — the Anthropic-key guard, added.** The test is already parametrized over
`api_key` in `[None, "sk-ant-test-not-a-real-key"]`. Using only what the test already
has in scope (`api_key`, `d["remote_client"]`), added:

```python
if api_key is None:
    assert d["remote_client"] is None
```

Two lines, no new fixture, no new helper, no new test function — closes
`anthropic_api_key`. `ollama_url` is left unguarded per the brief (lowest blast
radius, not growing this fix).

**Fix 2 — comment correction.** In `src/grant_lab/scheduler.py`, the comment above
`freshness = FreshnessContext(` was rewritten in the present tense to state what the
code guarantees now, keeping the `gate G26` reference findable:

Before:
```
    # Ranking freshness (spec D3/D4): ingest and reconcile judge "current" with
    # the SAME scorer values the rank:drain job below hands drain_queue. Built
    # from these locals, never from Settings, so the two cannot drift (gate G26).
```

After:
```
    # Ranking freshness (spec D3/D4): ingest and reconcile judge "current" with
    # the SAME scorer values the rank:drain job below hands drain_queue. Read
    # from `settings` exactly once here, and that one ScoringConfig is handed
    # to both ingest and reconcile, so the two cannot drift (gate G26).
```

No code changed on that path.

**Proof — each guard seen refusing the thing it now guards.** `src/grant_lab/scheduler.py`
backed up to `/tmp/scheduler_fix_task5.py.orig` with `cp` (md5sum matched) before any
mutation; each mutation run with `PYTHONDONTWRITEBYTECODE=1`, restored with `cp`,
confirmed with `cmp` after every restore.

Mutation A — `embedding_hard_fail_threshold=settings.embedding_hard_fail_threshold` →
`embedding_hard_fail_threshold=settings.embedding_pregate_threshold` (both sites: the
`ScoringConfig.for_drain(...)` call building `freshness`, and the `drain_queue(...)`
call in `_rank_drain_job`):

```
tests/test_ranking_freshness_wiring.py:204: in test_the_scheduler_hands_enqueue_the_drain_values
    assert d["embedding_hard_fail_threshold"] == 0.18
E   assert 0.27 == 0.18
FAILED tests/test_ranking_freshness_wiring.py::test_the_scheduler_hands_enqueue_the_drain_values[None]
FAILED tests/test_ranking_freshness_wiring.py::test_the_scheduler_hands_enqueue_the_drain_values[sk-ant-test-not-a-real-key]
2 failed, 5 passed in 5.01s
```

Where the reviewer measured `78 passed` before this fix, both parametrizations of the
guard test now go red. `cp` restore then `cmp`: identical.

Mutation B — `escalation_min_score=settings.ranking_escalation_min_score` →
`escalation_min_score=settings.ranking_queue_batch` (both sites, same two call sites
as above):

```
tests/test_ranking_freshness_wiring.py:205: in test_the_scheduler_hands_enqueue_the_drain_values
    assert d["escalation_min_score"] == 50
E   assert 10 == 50
2 failed, 5 passed in 4.73s
```

Red. `cp` restore then `cmp`: identical.

Mutation C — `settings.anthropic_api_key` → `settings.gemma_model` (both reads: the
`if settings.anthropic_api_key:` guard and `api_key=settings.anthropic_api_key` inside
the `AnthropicClient(...)` construction):

```
tests/test_ranking_freshness_wiring.py:207: in test_the_scheduler_hands_enqueue_the_drain_values
    assert d["remote_client"] is None
E   assert <grant_lab.ranking.anthropic_client.AnthropicClient object at 0x74e013f60a40> is None
FAILED tests/test_ranking_freshness_wiring.py::test_the_scheduler_hands_enqueue_the_drain_values[None]
1 failed, 6 passed in 4.77s
```

With `gemma_model` (always truthy) substituted for `anthropic_api_key`, a remote
`AnthropicClient` is now built even when `api_key=None` — exactly the bug the finding
named, caught by the new guard. `cp` restore then `cmp`: identical.

Every mutation produced a named FAILED assertion on the guard test itself — never a
collection error (no mass-ERROR/zero-FAILED shape), never a different, unrelated test.

**Post-fix sanity:**

```
$ uv run pytest -q -p no:cacheprovider tests/test_ranking_freshness_wiring.py
.......                                                                  [100%]
7 passed in 4.92s / 5.04s (re-run after each restore)
```

`0 skipped`, `0 xfailed` — test count unchanged (7), only assertions added inside an
existing test.

**Full backend suite:**

```
$ uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider
2501 passed in 50.16s
```

`0 skipped`, `0 xfailed` — unchanged from Task 5's own measured total, as expected
(assertions added, no test added or removed).

**Types, lint, architecture:**

```
$ uv run mypy --config-file pyproject.toml | tail -1
Success: no issues found in 120 source files
$ uv run ruff check . | tail -1
All checks passed!
$ uv run ruff format --check src/grant_lab/scheduler.py tests/test_ranking_freshness_wiring.py
2 files already formatted
$ uv run python scripts/gen_architecture.py && git status --short docs/ARCHITECTURE.md
wrote docs/ARCHITECTURE.md (192 lines)
(no output — unchanged; no module moved)
```

**Scope held to the brief.** Only the named test function gained assertions (no new
test function, no deletion/skip/weakening/rename of any test, no other test file
touched); `ollama_url` left deliberately unguarded per the brief's instruction.
