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

### Task 6 — remove `sources/extraction.py`, `pymupdf`, `trafilatura`; add `lxml` direct

Base commit `8cc310e8` (Task 5's commit; worktree clean before this task). Patch
`07-remove-extraction.patch`: deletes `src/grant_lab/sources/extraction.py` (251
lines, no production importer) and its test file `tests/test_sources_extraction.py`
(302 lines, 21 tests); drops `pymupdf` and `trafilatura` from `[project]
dependencies`, adds `"lxml>=6.0"` (it was only present transitively, via
`trafilatura`, and `sources/eu_portal.py` imports it directly); regenerates the
`no-build-package` list; rewrites the three comments that named the removed module
(`sources/__init__.py` docstring, a `vinnova.py` comment, `eu_portal.py` docstring);
adds `tests/architecture/test_removed_modules_stay_gone.py` (8 tests — the lock).

`git apply --check` on the patch passed cleanly before applying; `git apply --stat`
matched the brief's file list exactly (8 files, 145 insertions, 840 deletions).

**Step 1 — apply, sync, check the generated list:**

```
$ uv sync 2>&1 | tail -16
Resolved 113 packages in 1ms
   Building grant-lab @ file:///.../clean-slice-1
      Built grant-lab @ file:///.../clean-slice-1
Prepared 1 package in 381ms
Uninstalled 14 packages in 105ms
Installed 1 package in 2ms
 - babel==2.18.0
 - courlan==1.3.2
 - dateparser==1.4.0
 ~ grant-lab==0.1.0 (from file:///.../clean-slice-1)
 - htmldate==1.9.4
 - justext==3.0.2
 - lxml-html-clean==0.4.5
 - pymupdf==1.27.2.2
 - python-dateutil==2.9.0.post0
 - pytz==2026.1.post1
 - regex==2026.4.4
 - six==1.17.0
 - tld==0.13.2
 - trafilatura==2.0.0
```

The "Uninstalled 14 packages" count includes the local `grant-lab` package itself
(shown with `~`, reinstalled because `pyproject.toml` changed). The 13 packages with
a `-` prefix are: babel, courlan, dateparser, htmldate, justext, lxml-html-clean,
pymupdf, python-dateutil, pytz, regex, six, tld, trafilatura — an **exact match** to
the brief's named 13, no more, no fewer.

```
$ uv run python scripts/gen_no_build.py --check
no-build-package is in sync with uv.lock.
$ uv run python -c "import lxml.html; print('lxml ok')"
lxml ok
$ git grep -nE "^\s*(import|from)\s+(fitz|pymupdf|trafilatura)" -- src scripts alembic | wc -l
0
```

**lxml verified directly, not on faith:**

```
$ grep -n "lxml" src/grant_lab/sources/eu_portal.py | head -1
11:from lxml import html as lxml_html
$ grep -n '"lxml' pyproject.toml
22:    "lxml>=6.0",
172:    "lxml",
$ awk '/^\[\[package\]\]$/{p=0} /^name = "lxml"$/{p=1} p' uv.lock | head -2
name = "lxml"
version = "6.1.1"
```

`eu_portal.py` imports `lxml` directly; `pyproject.toml` now declares `"lxml>=6.0"`
as a direct `[project]` dependency; `uv.lock` keeps `lxml` at `6.1.1` (unchanged
version — only its dependents changed, not the pin); `import lxml.html` works after
the sync that removed `trafilatura`.

**Step 2 — lock tests, source tests, mypy:**

```
$ uv run pytest -q -p no:cacheprovider tests/architecture/
281 passed in 18.40s
$ uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider $(ls tests/test_sources_*.py) tests/test_gen_no_build.py
112 passed in 13.11s
$ uv run mypy --config-file pyproject.toml | tail -1
Success: no issues found in 119 source files
```

273 (this worktree's measured architecture baseline) + 8 (the new lock) = 281,
matching exactly. 112 passed matches the brief's prediction exactly. mypy: 120 → 119
source files, as the brief specified (one module deleted).

**Step 3 — prove the lock goes red (four mutations).** Every mutation backed up with
`cp`, run with `PYTHONDONTWRITEBYTECODE=1`, restored with `cp`/`rm`, confirmed with
`cmp`/`git status --porcelain`. The two mutations touching `pyproject.toml`/`uv.lock`
were run with `.venv/bin/python -m pytest` (not `uv run`, which would re-lock and
silently undo the mutation); all four were in fact run that way for consistency.

Mutation 1 — `import trafilatura` prepended to `src/grant_lab/sources/vinnova.py`:

```
$ PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/test_removed_modules_stay_gone.py
.F......                                                                 [100%]
FAILED tests/architecture/test_removed_modules_stay_gone.py::test_no_python_file_imports_a_removed_library
1 failed, 7 passed in 0.62s
```

Matches the brief's predicted test exactly, one FAILED. `cp` restore then `cmp`:
identical.

Mutation 2 — `"trafilatura>=2.0.0",` inserted into `[project] dependencies` in
`pyproject.toml`:

```
$ PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/test_removed_modules_stay_gone.py
FAILED tests/architecture/test_removed_modules_stay_gone.py::test_pyproject_does_not_depend_on_a_removed_library
FAILED tests/architecture/test_removed_modules_stay_gone.py::test_the_dependency_check_refuses_a_readded_requirement
2 failed, 6 passed in 0.60s
```

The brief names `test_pyproject_does_not_depend_on_a_removed_library` as the
expected red, and it fired. A SECOND test also reddened:
`test_the_dependency_check_refuses_a_readded_requirement` — its own first line is
`assert declared_dependencies(ROOT / "pyproject.toml") == set(), "control"`, which
reads the real (mutated) `pyproject.toml`, not a copy, so mutating the live file
trips that test's own control assertion before it ever gets to building its tmp_path
mutants. This is not the lock test naming the wrong file — it is a second, correctly
-behaving test sharing the same ROOT fixture. Noted rather than suppressed. `cp`
restore then `cmp`: identical.

Mutation 3 — a `[[package]] name = "pymupdf"` block appended to `uv.lock`:

```
$ PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/test_removed_modules_stay_gone.py
FAILED tests/architecture/test_removed_modules_stay_gone.py::test_the_lock_does_not_contain_a_removed_library
FAILED tests/architecture/test_removed_modules_stay_gone.py::test_the_lock_check_refuses_a_readded_package
2 failed, 6 passed in 0.50s
```

Same shape as Mutation 2: the brief's predicted test fired
(`test_the_lock_does_not_contain_a_removed_library`), plus the sibling test's own
"control" line against the live `uv.lock`. `cp` restore then `cmp`: identical.

Mutation 4 — `touch src/grant_lab/sources/extraction.py` (file was already staged as
deleted):

```
$ PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/test_removed_modules_stay_gone.py
F.......                                                                 [100%]
FAILED tests/architecture/test_removed_modules_stay_gone.py::test_the_extraction_module_is_gone
1 failed, 7 passed in 0.60s
```

Matches the brief's predicted test exactly, one FAILED. Restored with `rm`;
`git status --porcelain src/grant_lab/sources/extraction.py` printed `D` (staged
deletion) both before and after, confirming the file is gone again.

No mutation reddened nothing; no mutation showed the mass-collection-ERROR/
zero-FAILED shape.

**Post-mutation re-sync:**

```
$ uv sync 2>&1 | tail -6
Resolved 113 packages in 1ms
Uninstalled 1 package in 0.98ms
Installed 1 package in 2ms
 ~ grant-lab==0.1.0 (from file:///.../clean-slice-1)
$ git diff pyproject.toml uv.lock | wc -l
0
```

Only the local package was rebuilt (expected after any `uv sync`); `pyproject.toml`
and `uv.lock` are byte-identical to the staged post-patch state.

**Lint, format, architecture, full suite:**

```
$ uv run ruff check . | tail -1
All checks passed!
$ uv run ruff format --check tests/architecture/test_removed_modules_stay_gone.py
1 file already formatted
$ uv run python scripts/gen_architecture.py
wrote docs/ARCHITECTURE.md (192 lines)
$ diff <(git show 8cc310e8:docs/ARCHITECTURE.md) docs/ARCHITECTURE.md | wc -l
0
```

`docs/ARCHITECTURE.md` is unchanged: `extraction.py` was never referenced in it
(grep for "extraction" on the pre-task version returns nothing) — the map tracks
routes, tables, FKs and cap settings, none of which this module touched. Regenerated
anyway per the global constraint (a module was deleted), but no diff to add.

```
$ uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider
2488 passed in 50.20s
```

2501 (Task 5's measured full-suite total) − 21 (deleted `test_sources_extraction.py`)
+ 8 (the new `test_removed_modules_stay_gone.py` lock) = **2488**, matching exactly.
`0 skipped`, `0 xfailed` — no testcontainers `connection refused` noise observed in
this run.

**Scope held to the brief.** Only `tests/test_sources_extraction.py` was deleted
(pre-approved: the test file of the removed module); no other test was touched. The
three comment rewrites (`sources/__init__.py`, `vinnova.py`, `eu_portal.py`) matched
the brief's file list exactly. Nothing in the patch diverged from the brief; no
judgment calls were needed.

### Task 6 — review fixes

A review of Task 6's new lock test file returned one Important and two Minor
findings. Fixed on top of `034f5a1d`, one commit.

**Finding 1 (Important) — `declared_dependencies` ignored `[dependency-groups]` and
`[project.optional-dependencies]`.** Widened the helper to collect requirement
strings from all three locations `uv` installs from, skipping non-string entries in
a dependency-group list (`{include-group = "..."}`). Signature and return type
unchanged; no new test function added — the existing
`test_pyproject_does_not_depend_on_a_removed_library` now exercises the wider
surface via the mutation evidence below.

**Finding 2 (Minor) — `locked_packages` did not normalise case.** Routed the lock
side through the existing `_dist_name()` so `"Trafilatura"` and `"trafilatura"`
collide, matching the pyproject side's normalisation.

**Finding 3 (Minor) — orphaned fixture.** `git grep -n 'sample_call'` across the
whole tree hit only the historical patch file in the plan folder
(`docs/superpowers/plans/2026-11-05-clean-code-slice-1/07-remove-extraction.patch`);
no live reference in `src`, `tests`, `scripts`, `alembic`, `frontend` or config.
Removed `tests/fixtures/sample_call.pdf` with `git rm`.

**Not fixed, by design:** the reviewer's note that a dynamic import
(`importlib.import_module("trafilatura")`, `__import__("fitz")`) evades
`forbidden_imports()`. `forbidden_imports()` scans AST import statements; matching
string literals too would flag any file that merely mentions the name, including
the lock test itself. Left as a known, accepted limit — `forbidden_imports()`
unchanged.

**Mutation evidence** (each: `cp` backup, mutate, `.venv/bin/python -m pytest`
— never `uv run`, which re-syncs on a `pyproject.toml` change — then `cp` restore,
`cmp` confirm):

1. Added `"pymupdf>=1.25.0",` to `[dependency-groups].dev`:
   ```
   FAILED tests/architecture/test_removed_modules_stay_gone.py::test_pyproject_does_not_depend_on_a_removed_library
   AssertionError: assert {'pymupdf'} == set()
   ```
   Red where it was green before the fix — confirms the dependency-groups path is
   now covered.

2. Added `[project.optional-dependencies]` with `extractor = ["trafilatura>=2.0.0"]`:
   ```
   FAILED tests/architecture/test_removed_modules_stay_gone.py::test_pyproject_does_not_depend_on_a_removed_library
   AssertionError: assert {'trafilatura'} == set()
   ```
   Red — confirms the optional-dependencies path is now covered.

3. Added `"trafilatura>=2.0.0",` to `[project].dependencies` (the original shape
   Task 6 already proved):
   ```
   FAILED tests/architecture/test_removed_modules_stay_gone.py::test_pyproject_does_not_depend_on_a_removed_library
   AssertionError: assert {'trafilatura'} == set()
   ```
   Still red — the widening did not break the original shape.

4. Appended `[[package]]\nname = "Trafilatura"\nversion = "1.0.0"` (capital T) to
   `uv.lock`:
   ```
   FAILED tests/architecture/test_removed_modules_stay_gone.py::test_the_lock_does_not_contain_a_removed_library
   AssertionError: assert {'trafilatura'} == set()
   ```
   Red — case asymmetry closed.

5. Added a `test = [{include-group = "dev"}, "pytest>=8.3"]` group to
   `[dependency-groups]` (no removed library present): full
   `tests/architecture/test_removed_modules_stay_gone.py` run stayed **8 passed**,
   no crash on the non-string `{include-group = ...}` entry.

After each mutation, `pyproject.toml`/`uv.lock` were restored from the `cp` backup
and reconfirmed byte-identical with `cmp`; final `git status --porcelain` showed
only the intended `test_removed_modules_stay_gone.py` edit and the `sample_call.pdf`
deletion. No `uv run` command touched `pyproject.toml` or `uv.lock` during the
mutation round, so no re-sync was needed afterward.

**Post-fix verification:**
- `PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider tests/architecture/`
  → **281 passed** (unchanged — helpers widened, no test added).
- `uv run python scripts/gen_no_build.py --check` → `no-build-package is in sync
  with uv.lock.`
- `uv run mypy --config-file pyproject.toml` → `Success: no issues found in 119
  source files`.
- `uv run ruff check .` → `All checks passed!`. `uv run ruff format --check
  tests/architecture/test_removed_modules_stay_gone.py` → `1 file already
  formatted`.
- `PYTHONDONTWRITEBYTECODE=1 uv run pytest -n 16 --dist loadfile -q -p
  no:cacheprovider` → **2488 passed, 0 skipped, 0 xfailed** — unchanged from
  Task 6's own total.

### Task 7 — remove the CSP `ws_origin` token

Base commit `6df4974c`. Patch: `08-remove-ws-origin.patch`. `_derive_ws_origin`
becomes `_validate_app_base_url(app_base_url) -> None` (no longer returns a token),
called unconditionally in `create_app` before the database engine is created.
`build_csp` loses its `ws_origin` parameter; production `connect-src` becomes
`'self'`.

**Step 1 — apply and run:**

```
$ git apply --index $P/08-remove-ws-origin.patch
$ git status --porcelain
M  src/grant_lab/main.py
M  src/grant_lab/web/middleware.py
M  tests/test_security_headers.py
M  tests/test_spa_serving.py

$ git grep -nE "WebSocket|wss?://" -- 'frontend/src/*.ts' 'frontend/src/*.tsx' | grep -vE "\.(test|spec)\.|schema\.gen" | wc -l
0
```

Also checked recursively (`frontend/src/**/*.{ts,tsx}` and a plain `git grep -l`
over the whole `frontend/src` tree): still 0 matches outside test/spec/schema.gen
files. Nothing in the frontend opens a WebSocket.

```
$ uv run pytest -n 4 --dist loadfile -q -p no:cacheprovider tests/test_security_headers.py tests/test_spa_serving.py tests/test_auth_csrf.py tests/test_config.py tests/test_env_example.py 2>&1 | tail -1
60 passed in 10.49s

$ uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
281 passed in 17.99s
```

Both match the brief's expectation (`60 passed`, Task 6's `281 passed`).

**Orderings, read off `create_app` (`src/grant_lab/main.py`):**

1. Unconditional: line 302, `_validate_app_base_url(settings.app_base_url)`, is a bare
   statement at the top level of `create_app` — not inside an `if`.
2. Before the engine: line 302 precedes line 338's `engine = make_engine(settings.database_url)`
   by 36 lines (the email-sender selection sits between them, unconditionally executed
   either way). Confirmed by reading the function body directly.

**The two `RuntimeError`s, old (`_derive_ws_origin`, base `6df4974c`) vs new
(`_validate_app_base_url`):**

```
OLD:
    if not _base.scheme or not _base.netloc:
        raise RuntimeError(f"APP_BASE_URL is not a valid URL: {app_base_url!r}")
    if not _HOST_PORT_RE.match(_base.netloc):
        raise RuntimeError(f"APP_BASE_URL host is not a valid host[:port]: {_base.netloc!r}")
    return f"{'wss' if _base.scheme == 'https' else 'ws'}://{_base.netloc}"

NEW:
    if not _base.scheme or not _base.netloc:
        raise RuntimeError(f"APP_BASE_URL is not a valid URL: {app_base_url!r}")
    if not _HOST_PORT_RE.match(_base.netloc):
        raise RuntimeError(f"APP_BASE_URL host is not a valid host[:port]: {_base.netloc!r}")
```

Both `raise` lines are byte-identical between old and new; only the trailing
`return` (which produced the now-removed token) is gone.

**Step 2 — mutation check.** `src/grant_lab/main.py` backed up with `cp` to
`/tmp/main.py.bak`. The call `_validate_app_base_url(settings.app_base_url)` at
line 302 replaced with `pass  # MUTATED`, run with `PYTHONDONTWRITEBYTECODE=1`:

```
$ PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider tests/test_security_headers.py
.........F.........                                                      [100%]
=================================== FAILURES ===================================
_______________ test_create_app_refuses_a_malformed_app_base_url _______________
    with pytest.raises(RuntimeError, match="APP_BASE_URL host"):
>       create_app()
E       Failed: DID NOT RAISE <class 'RuntimeError'>
1 failed, 18 passed in 8.62s
```

Exactly the predicted test reddened, no other test affected. Restored:

```
$ cp /tmp/main.py.bak src/grant_lab/main.py
$ cmp src/grant_lab/main.py /tmp/main.py.bak && echo "IDENTICAL (cmp exit 0)"
IDENTICAL (cmp exit 0)
$ PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider tests/test_security_headers.py
19 passed in 8.25s
```

**The two CSP strings, measured directly via `build_csp`:**

```
PROD CSP:
"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'"

DEV CSP:
"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self' ws: http://localhost:5173; form-action 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'"

PROD connect-src: 'self'
DEV connect-src:  'self' ws: http://localhost:5173
```

Production lost the `wss://<host>` token entirely; dev is byte-for-byte unchanged
(still carries the broad `ws:` token for Vite HMR). Also confirmed `build_csp`
rejects a stray `ws_origin=` kwarg with `TypeError: build_csp() got an unexpected
keyword argument 'ws_origin'` — the parameter is gone, not just unused.

**Test changes, classified against the approved list:**

- `tests/test_security_headers.py`: deleted `test_build_csp_prod_has_same_origin_ws_token`
  and `test_build_csp_http_base_gets_ws_token` (spec case 3 — tests whose only subject
  was the deleted `ws_origin` parameter); deleted the one `ws_origin` assertion line
  inside `test_build_csp_dev_output_unchanged` (same case 3); renamed
  `test_ws_origin_injection_rejected_at_startup` →
  `test_app_base_url_injection_rejected_at_startup` and
  `test_ws_origin_whitespace_netloc_rejected_at_startup` →
  `test_app_base_url_whitespace_netloc_rejected_at_startup`, assertions unchanged
  (spec case 5, "renaming 2 startup tests"); added
  `test_create_app_refuses_a_malformed_app_base_url` (new wiring test, pre-approved).
- `tests/test_spa_serving.py`: `test_prod_mode_serves_index_for_spa_routes`'s CSP
  assertion changed from `connect_src.strip() == "'self' ws://testserver"` (plus a
  negative check) to `connect_src.strip() == "'self'"` (spec case 5).

No other test assertion was touched. Nothing beyond the approved list.

**App-construction form in the new/edited tests:** every existing test in
`tests/test_security_headers.py` that builds an app uses `app = create_app()` then
`with TestClient(app) as client:` (lifespan-aware context-manager form), e.g.
`test_security_headers_present_on_response`, `test_hsts_absent_over_http`,
`test_oversized_body_rejected_with_413`. The new
`test_create_app_refuses_a_malformed_app_base_url` calls bare `create_app()` with
no `TestClient` at all — correct, because the whole point of the test is that
`create_app()` raises `RuntimeError` before any app/TestClient object exists. No
bare `TestClient(create_app())` (unwrapped) was introduced anywhere.
`tests/test_spa_serving.py`'s edited test only changed an assertion string; its
app-construction (the `c` fixture, via the repo's `test_client_factory`) was not
touched.

**Step 3 — lint, types:**

```
$ uv run mypy --config-file pyproject.toml | tail -1
Success: no issues found in 119 source files

$ uv run ruff check . | tail -1
All checks passed!

$ uv run ruff format --check src/grant_lab/main.py src/grant_lab/web/middleware.py tests/test_security_headers.py tests/test_spa_serving.py
4 files already formatted
```

**Full backend suite:**

```
$ uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider
2487 passed in 49.44s
```

Matches the brief's arithmetic exactly: 2488 (Task 6's measured base in this
worktree) − 4 (two `ws_origin` tests deleted: `test_build_csp_prod_has_same_origin_ws_token`,
`test_build_csp_http_base_gets_ws_token`; two startup tests counted as removed
under their old names: `test_ws_origin_injection_rejected_at_startup`,
`test_ws_origin_whitespace_netloc_rejected_at_startup`) + 3 (the same two startup
tests re-added under their new names, plus the one new
`test_create_app_refuses_a_malformed_app_base_url`) = 2487. Re-ran grepping the
full output for `skip|xfail|error|warn`: no matches — `0 skipped, 0 xfailed`, no
testcontainers flake observed, so no file needed a standalone re-run.

**Commit:** `fix(csp): drop the WebSocket source nothing uses from production connect-src`,
plus this run-log entry in the same commit.

**Nothing in the patch diverged from the brief.** Both orderings (unconditional call;
before the engine) held as specified. The two `RuntimeError` messages and trigger
conditions are unchanged from `_derive_ws_origin`. The test changes stayed exactly
inside the approved set — no judgment call was needed.

#### Task 7 — review fix: a dead test helper

`tests/test_security_headers.py::_connect_src_directive` existed only for the two tests
Task 7 deletes (`test_build_csp_prod_has_same_origin_ws_token` and
`test_build_csp_http_base_gets_ws_token` — its only two call sites at `6df4974c`). After
the deletion `git grep '_connect_src_directive' -- src tests scripts` found the definition
and nothing else. Neither `ruff check` nor `mypy` flags an unused private top-level
function (checked: `ruff check --select F401,F811,F841` is clean on the file), so the only
signal was reading it. Removed — this slice's own rule is that dead code goes.

Before removing it, its protective purpose was checked rather than assumed. Its docstring
warned it existed "instead of a bare substring check so a match can't be fooled by junk
appended after the expected token". Both surviving `connect-src` assertions are exact, not
fool-able:

- `tests/test_spa_serving.py` extracts the directive and compares for **equality**:
  `connect_src = prod_csp.split("connect-src")[1].split(";")[0]`, then
  `assert connect_src.strip() == "'self'"`;
- `tests/test_security_headers.py` asserts `"connect-src 'self';" in build_csp() + ";"` —
  the trailing semicolon means an appended `wss://…` token would **not** match.

After: `29 passed` in the two files, `All checks passed!`, `1 file already formatted`, and
`Success: no issues found in 119 source files`.

### Task 8 — fix: a failed decision's alert stays after a successful undo

Base commit `e9f80672`. Patches: `09a-feed-alert-test.patch` (three new tests),
`09b-feed-alert-fix.patch` (the fix). A real bug, not a refactor: `TriageFeed::decide`
resets the four decision mutations (`save.reset(); hide.reset(); unsave.reset();
unhide.reset();`) before issuing a new write; `undecide` and `undo` did not, so a failed
save/hide's red alert ("… Your call is unchanged. Please try again.") survived a
successful `u`, "Return to inbox", or the strip's Undo — even though the call had, by
then, changed.

**Step 1 — apply the test patch, see it fail:**

```
$ git apply --index $P/09a-feed-alert-test.patch
$ git status --porcelain
M  frontend/src/routes/TriageFeed.test.tsx

$ pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx 2>&1 | grep -E "Tests |✗|×|FAIL" | head -8
 × gate 9 — u un-decides in Saved and Hidden, and does nothing in the Inbox (D2) > a failed decision's alert goes away once u succeeds
 × gate 9 — u un-decides in Saved and Hidden, and does nothing in the Inbox (D2) > a failed decision's alert goes away once Return to inbox succeeds
 × gate 9 — u un-decides in Saved and Hidden, and does nothing in the Inbox (D2) > a failed decision's alert goes away once the strip's Undo succeeds
 FAIL  src/routes/TriageFeed.test.tsx > gate 9 … > a failed decision's alert goes away once u succeeds
 FAIL  src/routes/TriageFeed.test.tsx > gate 9 … > a failed decision's alert goes away once Return to inbox succeeds
 FAIL  src/routes/TriageFeed.test.tsx > gate 9 … > a failed decision's alert goes away once the strip's Undo succeeds
      Tests  3 failed | 89 passed (92)
```

All three fail for the same, correct reason — the alert `<div role="alert">` is
still mounted after the undo succeeds:

```
× … a failed decision's alert goes away once u succeeds
  → expected <div …(5)><div …(1)>…(1)</div></div> to be null
× … a failed decision's alert goes away once Return to inbox succeeds
  → expected <div …(5)><div …(1)>…(1)</div></div> to be null
× … a failed decision's alert goes away once the strip's Undo succeeds
  → expected <div …(5)><div …(1)>…(1)</div></div> to be null
```

Matches the brief exactly: `3 failed`, the three named tests, red for the right reason
(a `waitFor` around `expect(screen.queryByRole("alert")).toBeNull()` timing out because
the node is still there), not a crash or a missing fixture.

**Waiting form used by all three new tests** (the repo's known `findBy*`-races-on-presence
hazard does NOT apply here): each ends with

```ts
await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
```

— `waitFor` wrapped around the assertion, polling until the alert is actually gone,
not a `findBy*` on an always-mounted node. (Earlier `expect(await screen.findByRole("alert"))…`
lines in the same tests are deliberate: they wait for the alert to **appear**, which is a
presence wait and is fine for that direction.)

**Step 2 — apply the fix:**

```
$ git apply --index $P/09b-feed-alert-fix.patch
$ git status --porcelain
M  frontend/src/routes/TriageFeed.test.tsx
M  frontend/src/routes/TriageFeed.tsx

$ pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx 2>&1 | grep -E "Tests "
      Tests  92 passed (92)

$ pnpm -C frontend run test 2>&1 | grep -E "Test Files|Tests "
 Test Files  55 passed (55)
      Tests  533 passed (533)

$ pnpm -C frontend exec tsc -p tsconfig.json && pnpm -C frontend run lint 2>&1 | tail -2
> grant-lab-frontend@ lint …
> oxlint --deny-warnings src e2e
```

`tsc` printed nothing (clean exit) and lint reported no warnings/errors. Matches the
brief's `92 passed`, `Tests 533 passed (533)` in `55` files exactly — 530 measured in this
worktree before this task (per the controller's note) + the 3 new tests = 533, not a
re-derivation of the old `533` figure.

**Step 3 — mutation checks.** `frontend/src/routes/TriageFeed.tsx` backed up:

```
$ cp frontend/src/routes/TriageFeed.tsx /tmp/TriageFeed.tsx.orig
$ cmp frontend/src/routes/TriageFeed.tsx /tmp/TriageFeed.tsx.orig && echo BACKUP_MATCHES
BACKUP_MATCHES
```

*Mutation 1 — remove the `resetDecisionErrors()` call from `undecide` (line 435, the
occurrence between `decide`'s at line 369 and `undo`'s at line 457 — targeted by line
number since the three calls are textually identical):*

```
$ sed -i '435d' frontend/src/routes/TriageFeed.tsx
$ PYTHONDONTWRITEBYTECODE=1 pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx 2>&1 | grep -E "Tests |✗|×|FAIL"
 × gate 9 … > a failed decision's alert goes away once u succeeds
 × gate 9 … > a failed decision's alert goes away once Return to inbox succeeds
 FAIL  src/routes/TriageFeed.test.tsx > gate 9 … > a failed decision's alert goes away once u succeeds
 FAIL  src/routes/TriageFeed.test.tsx > gate 9 … > a failed decision's alert goes away once Return to inbox succeeds
      Tests  2 failed | 90 passed (92)
```

Exactly the predicted two tests (`u`, "Return to inbox") go red; the strip's Undo test
stays green, as it must — this mutation only touches `undecide`. Restored:

```
$ cp /tmp/TriageFeed.tsx.orig frontend/src/routes/TriageFeed.tsx
$ cmp frontend/src/routes/TriageFeed.tsx /tmp/TriageFeed.tsx.orig && echo RESTORE_1_MATCHES
RESTORE_1_MATCHES
```

*Mutation 2 — remove the `resetDecisionErrors()` call from `undo` (line 457):*

```
$ sed -i '457d' frontend/src/routes/TriageFeed.tsx
$ PYTHONDONTWRITEBYTECODE=1 pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx 2>&1 | grep -E "Tests |✗|×|FAIL"
 × gate 9 … > a failed decision's alert goes away once the strip's Undo succeeds
 FAIL  src/routes/TriageFeed.test.tsx > gate 9 … > a failed decision's alert goes away once the strip's Undo succeeds
      Tests  1 failed | 91 passed (92)
```

Exactly the predicted test (the strip's Undo) goes red. Restored:

```
$ cp /tmp/TriageFeed.tsx.orig frontend/src/routes/TriageFeed.tsx
$ cmp frontend/src/routes/TriageFeed.tsx /tmp/TriageFeed.tsx.orig && echo RESTORE_2_MATCHES
RESTORE_2_MATCHES
$ pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx 2>&1 | grep -E "Tests "
      Tests  92 passed (92)
```

Both mutations reddened exactly the tests they were predicted to, and only those —
no mutation reddened nothing, and neither reddened the third (uninvolved) test.

**Backend side-effect check (architecture gate, per the brief's "frontend-only diffs
have reddened the backend suite before" rule):**

```
$ uv run pytest -q -p no:cacheprovider tests/architecture/ 2>&1 | tail -1
281 passed in 18.15s

$ uv run mypy --config-file pyproject.toml | tail -1
Success: no issues found in 119 source files
```

Both unchanged from Task 7's measured base (`281 passed`, `119 source files`) — this
task touches no backend file, so no backend number was expected to move, and none did.

**Commit:** `fix(feed): clear a failed decision's alert when an undo succeeds` — the test
and the fix in one commit, its own, plus this run-log entry. Nothing in either patch
diverged from the brief: both `undecide` and `undo` gained the same
`resetDecisionErrors()` call `decide` already had, factored out into one `useCallback`
rather than copy-pasted three times. No judgment call was needed beyond targeting the
mutation's `sed -i` by line number (the three `resetDecisionErrors();` call sites are
textually identical, so a text-pattern mutation could not have picked one of them).

### Task 9 — Steps 1 to 3

`BASE=f22019eb823333383f19f008fe613031b8b6a618` (slice base, `$(cat /tmp/s1-base-rev)`).
`AFTER=ecdc53d461be448da2f3c2f669d0948913c4ff55` (HEAD at the start of this task).
Branch `refactor/clean-slice-1` confirmed; worktree clean before starting.

#### Step 1 — the whole suite, twice, with coverage

| Run | Command | Result |
|---|---|---|
| Backend 1 | `uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:/tmp/s1-final-be-1.json` | `2487 passed in 79.26s (0:01:19)` |
| Backend 2 | same, `-be-2.json` | `2487 passed in 77.95s (0:01:17)` |
| Frontend 1 | `pnpm -C frontend run test:coverage` | `Test Files 55 passed (55)`, `Tests 533 passed (533)` |
| Frontend 2 | same | `Test Files 55 passed (55)`, `Tests 533 passed (533)` |

`2487 passed` matches the expected total exactly: 2436 (Task 0 base) + 51
(+59 Task 1, +5 Task 3, +1 Task 4, −21+8 Task 6, −4+3 Task 7) = 2487. No term needed
renaming — the measured delta matches the brief's term-by-term accounting as given.

`0 skipped`, `0 xfailed` on all four runs (backend ".....[100%]" summary lines carried
no skip/xfail counts; frontend's `Tests 533 passed (533)` carries no skipped count
either). No `connection refused` ERRORs in either backend run — both were clean on the
first attempt, so no file needed a lone re-run.

Backend coverage summary (from the run's own stdout, unchanged both runs):
`Coverage JSON written to file /tmp/s1-final-be-{1,2}.json`.
Frontend coverage summary (identical both runs):
```
Statements   : 93.89% ( 4383/4668 )
Branches     : 87.3% ( 1259/1442 )
Functions    : 87.3% ( 392/449 )
Lines        : 93.89% ( 4383/4668 )
```

Reports copied to `/home/parlar_ai/grant-lab-s1-run/reports/` (evidence copy, `/tmp`
is volatile):

```
$ ls -la /home/parlar_ai/grant-lab-s1-run/reports/
total 595
drwxrwx--- 2 parlar_ai parlar_ai     13 nov  6 02:57 .
drwxrwx--- 3 parlar_ai parlar_ai     10 nov  6 00:17 ..
-rw-rw---- 1 parlar_ai parlar_ai 795282 nov  6 00:27 s1-base-be-1.json
-rw-rw---- 1 parlar_ai parlar_ai 795282 nov  6 00:27 s1-base-be-2.json
-rw-rw---- 1 parlar_ai parlar_ai 737249 nov  6 00:27 s1-base-fe.json
-rw-rw---- 1 parlar_ai parlar_ai  22904 nov  6 00:27 s1-base-fe-summary.json
-rw-rw---- 1 parlar_ai parlar_ai     41 nov  6 00:27 s1-base-rev
-rw-rw---- 1 parlar_ai parlar_ai 791303 nov  6 02:57 s1-final-be-1.json
-rw-rw---- 1 parlar_ai parlar_ai 791303 nov  6 02:57 s1-final-be-2.json
-rw-rw---- 1 parlar_ai parlar_ai 738069 nov  6 02:57 s1-final-fe-1.json
-rw-rw---- 1 parlar_ai parlar_ai  22905 nov  6 02:57 s1-final-fe-1-summary.json
-rw-rw---- 1 parlar_ai parlar_ai 738069 nov  6 02:57 s1-final-fe-2.json
-rw-rw---- 1 parlar_ai parlar_ai  22905 nov  6 02:57 s1-final-fe-2-summary.json
```

(The four `s1-base-*` files there were already present — copied in by an earlier step
before this task started; this task added only the six `s1-final-*` files.)

#### Step 2 — no code lost its tests, in both reports that could set a floor

All four compares ran to completion; all four `exit=0`.

**Backend run 1** (`--before /tmp/s1-base-be-1.json --before-rev $BASE --after
/tmp/s1-final-be-1.json --after-rev $AFTER`), full output:

```
### Uncovered code: backend

| group | lines before | after | new | branches before | after | new |
|---|---|---|---|---|---|---|
| <root> | 62 | 62 | 0 | 20 | 20 | 0 |
| auth | 28 | 28 | 0 | 14 | 14 | 0 |
| boards | 12 | 12 | 0 | 5 | 5 | 0 |
| db | 0 | 0 | 0 | 0 | 0 | 0 |
| digest | 6 | 6 | 0 | 3 | 3 | 0 |
| email | 4 | 4 | 0 | 0 | 0 | 0 |
| embeddings | 7 | 7 | 0 | 7 | 7 | 0 |
| ingestion | 33 | 33 | 0 | 11 | 11 | 0 |
| profiles | 57 | 57 | 0 | 29 | 29 | 0 |
| ranking | 70 | 70 | 0 | 39 | 39 | 0 |
| sources | 42 | 40 | 0 | 34 | 33 | 0 |
| web | 68 | 68 | 0 | 56 | 54 | 0 |

OK: 0 new uncovered line(s), 0 new uncovered branch(es); 0 matched in another file of its group, 0 branch(es) in code that did not run before
```
`backend run 1 exit=0`.

Zero "matched in another file of its group" rows, zero `[k of n]` partial-match rows,
zero "branch in code that did not run before" rows were printed — the tool found
nothing to forgive. `sources` dropping from 42/34 to 40/33 uncovered lines/branches
and `web` branches dropping from 56 to 54 are drops in the *uncovered* count (i.e. more
code got covered or some got deleted), which is exactly what a "new" column of 0 means:
no net loss, some net gain. There is nothing here that needs a move explained, because
the tool reported no moves.

**Backend run 2** (`--before /tmp/s1-base-be-1.json --before-rev $BASE --after
/tmp/s1-final-be-2.json --after-rev $AFTER`): identical table to run 1, byte-for-byte
(`sources` 42→40/34→33, `web` 56→54, all other groups unchanged, `0 new` everywhere).
`OK: 0 new uncovered line(s), 0 new uncovered branch(es); 0 matched in another file of
its group, 0 branch(es) in code that did not run before`. `backend run 2 exit=0`.

**Frontend run 1** (`--before /tmp/s1-base-fe.json --before-rev $BASE --after
/tmp/s1-final-fe-1.json --after-rev $AFTER`), full output:

```
### Uncovered code: frontend

| group | lines before | after | new | branches before | after | new |
|---|---|---|---|---|---|---|
| <root> | 25 | 25 | 0 | 2 | 2 | 0 |
| api | 17 | 17 | 0 | 12 | 12 | 0 |
| components | 19 | 19 | 0 | 7 | 7 | 0 |
| routes | 224 | 224 | 0 | 162 | 162 | 0 |
| shell | 0 | 0 | 0 | 0 | 0 | 0 |
| theme | 0 | 0 | 0 | 0 | 0 | 0 |

OK: 0 new uncovered line(s), 0 new uncovered branch(es); 0 matched in another file of its group, 0 branch(es) in code that did not run before
```
`frontend run 1 exit=0`.

Again zero moved/partial/did-not-run-before rows. The `routes` group's uncovered
line/branch counts (224/162) are unchanged before vs. after at the group total, even
though Task 8 changed `TriageFeed.tsx` inside that group — this is addressed directly
in the branch-arm-swap check below, since a same-line-pair arm swap is exactly the
failure mode this coarse table cannot see and the per-group "new" column alone cannot
rule out.

**Frontend run 2** (`--before /tmp/s1-base-fe.json --before-rev $BASE --after
/tmp/s1-final-fe-2.json --after-rev $AFTER`): identical table to run 1
(`<root>` 25/2, `api` 17/12, `components` 19/7, `routes` 224/162, `shell` 0/0,
`theme` 0/0, all `new` columns 0). Same `OK:` line, zero moved/partial/did-not-run-before
rows. `frontend run 2 exit=0`.

**Summary: 4/4 compares exit=0. 0 moved rows, 0 partial `[k of n]` rows, 0
did-not-run-before rows across all four — there is nothing to explain because the tool
printed nothing to explain.**

#### Step 3 — the check goes red on a lost test, on the full suite

```
$ cp tests/test_profiles_pubmed.py /tmp/s1-pubmed.bak && rm tests/test_profiles_pubmed.py
$ uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider --cov --cov-report=json:/tmp/s1-mutant-be.json
2476 passed in 77.54s (0:01:17)
```
11 fewer than 2487, exactly as expected.

```
$ uv run python scripts/compare_uncovered.py --section backend --before /tmp/s1-final-be-1.json --before-rev $AFTER --after /tmp/s1-mutant-be.json --after-rev WORKTREE
### Uncovered code: backend

New uncovered lines and branches:
profiles  src/grant_lab/profiles/pubmed.py:62->63  if self._api_key:  ->  params["api_key"] = self._api_key
profiles  src/grant_lab/profiles/pubmed.py:63  params["api_key"] = self._api_key
profiles  src/grant_lab/profiles/pubmed.py:64->65  if self._tool:  ->  params["tool"] = self._tool
profiles  src/grant_lab/profiles/pubmed.py:65  params["tool"] = self._tool
profiles  src/grant_lab/profiles/pubmed.py:66->67  if self._email:  ->  params["email"] = self._email
profiles  src/grant_lab/profiles/pubmed.py:67  params["email"] = self._email
profiles  src/grant_lab/profiles/pubmed.py:97->98  if i > 0:  ->  time.sleep(self._sleep)
profiles  src/grant_lab/profiles/pubmed.py:98  time.sleep(self._sleep)
profiles  src/grant_lab/profiles/pubmed.py:134->136  if self._retry_backoff:  ->  continue
profiles  src/grant_lab/profiles/pubmed.py:224  cleaned = _ENTREZ_METACHARS.sub(" ", value)
profiles  src/grant_lab/profiles/pubmed.py:225  cleaned = _ENTREZ_BOOLEANS.sub(" ", cleaned)
profiles  src/grant_lab/profiles/pubmed.py:226  return " ".join(cleaned.split())
profiles  src/grant_lab/profiles/pubmed.py:230->240  if member.orcid:  ->  name = _sanitize_entrez_value(member.name or "")
profiles  src/grant_lab/profiles/pubmed.py:240  name = _sanitize_entrez_value(member.name or "")
profiles  src/grant_lab/profiles/pubmed.py:241  affiliation = _sanitize_entrez_value(member.affiliation or "")
profiles  src/grant_lab/profiles/pubmed.py:242  return f'"{name}"[au] AND "{affiliation}"[ad]'
profiles  src/grant_lab/profiles/seed.py:23->24  if self.orcid is not None and not _ORCID_RE.match(self.orcid):  ->  raise ValueError(f"invalid ORCID format: {self.orcid!r}")
profiles  src/grant_lab/profiles/seed.py:24  raise ValueError(f"invalid ORCID format: {self.orcid!r}")

| group | lines before | after | new | branches before | after | new |
|---|---|---|---|---|---|---|
| <root> | 62 | 62 | 0 | 20 | 20 | 0 |
| auth | 28 | 28 | 0 | 14 | 14 | 0 |
| boards | 12 | 12 | 0 | 5 | 5 | 0 |
| db | 0 | 0 | 0 | 0 | 0 | 0 |
| digest | 6 | 6 | 0 | 3 | 3 | 0 |
| email | 4 | 4 | 0 | 0 | 0 | 0 |
| embeddings | 7 | 7 | 0 | 7 | 7 | 0 |
| ingestion | 33 | 33 | 0 | 11 | 11 | 0 |
| profiles | 57 | 68 | 11 | 29 | 36 | 7 |
| ranking | 70 | 70 | 0 | 39 | 39 | 0 |
| sources | 40 | 40 | 0 | 33 | 33 | 0 |
| web | 68 | 68 | 0 | 54 | 54 | 0 |

FAIL: 11 new uncovered line(s), 7 new uncovered branch(es); 0 matched in another file of its group, 0 branch(es) in code that did not run before
```
Exit code (captured via `${PIPESTATUS[0]}` since the first attempt piped to a shell
construct): **`exit=1`**. All 18 new-uncovered rows are under the `profiles` group,
exactly as expected — the deleted `tests/test_profiles_pubmed.py` was the only test
file covering `pubmed.py`'s header-building, rate-limit sleep, retry-continue, Entrez
query sanitising, and `seed.py`'s ORCID validation. **The gate does NOT stay green on
a lost test — it goes red exactly where the test was deleted.**

```
$ cp /tmp/s1-pubmed.bak tests/test_profiles_pubmed.py
$ cmp /tmp/s1-pubmed.bak tests/test_profiles_pubmed.py && echo "cmp identical"
cmp identical
$ git status --short
(empty)
```
Restored, byte-identical, worktree clean.

#### Extra check — frontend branch-arm swap (fail-open verification)

Throwaway script (`/tmp/branch_swap_check.py`, not committed, not added to the repo):

```python
import json
import sys

BASE = "/tmp/s1-base-fe.json"
FINAL = "/tmp/s1-final-fe-1.json"

with open(BASE) as f:
    base = json.load(f)
with open(FINAL) as f:
    final = json.load(f)

changes = []

common_files = set(base.keys()) & set(final.keys())
for file in sorted(common_files):
    b = base[file]
    a = final[file]
    b_branch_map = b.get("branchMap", {})
    a_branch_map = a.get("branchMap", {})
    b_b = b.get("b", {})
    a_b = a.get("b", {})
    common_bids = set(b_branch_map.keys()) & set(a_branch_map.keys())
    for bid in sorted(common_bids, key=lambda x: int(x)):
        b_counts = b_b.get(bid)
        a_counts = a_b.get(bid)
        if b_counts is None or a_counts is None:
            continue
        if len(b_counts) != len(a_counts):
            changes.append((file, bid, b_branch_map[bid], a_branch_map[bid], b_counts, a_counts, "ARM-COUNT-CHANGED"))
            continue
        b_zero = frozenset(i for i, c in enumerate(b_counts) if c == 0)
        a_zero = frozenset(i for i, c in enumerate(a_counts) if c == 0)
        if b_zero != a_zero:
            changes.append((file, bid, b_branch_map[bid], a_branch_map[bid], b_counts, a_counts, "ZERO-ARM-SET-CHANGED"))

print(f"Total common files: {len(common_files)}")
print(f"Total changed (file,bid) pairs: {len(changes)}")
print()
for file, bid, b_loc, a_loc, b_counts, a_counts, kind in changes:
    def loc_str(loc):
        if loc is None:
            return "None"
        locs = loc.get("locations")
        if locs:
            return "locations=" + str([f"{l.get('start',{}).get('line')}-{l.get('end',{}).get('line')}" for l in locs])
        l = loc.get("loc")
        if l:
            return f"loc={l.get('start',{}).get('line')}-{l.get('end',{}).get('line')}"
        return str(loc)
    print(f"[{kind}] file={file} bid={bid}")
    print(f"  before loc: {loc_str(b_loc)}")
    print(f"  after  loc: {loc_str(a_loc)}")
    print(f"  before counts: {b_counts}")
    print(f"  after  counts: {a_counts}")
    print()
```

Compared `/tmp/s1-base-fe.json` (baseline, rev `$BASE`) against `/tmp/s1-final-fe-1.json`
(this task's frontend run 1, rev `$AFTER`) — per-branch-id zero-arm-set comparison,
columns genuinely ignored as the finding predicted.

Full output: 68 files in common between the two reports; **18 `(file, bid)` pairs**
changed their zero-arm set, **all 18 in
`frontend/src/routes/TriageFeed.tsx`** (bids 122, 123, 133, 134, 145, 146, 149, 151,
158, 160, 173, 175, 176, 178, 183, 187, 203, 205). No `ARM-COUNT-CHANGED` rows (every
matched bid kept the same arm count, so the "same `(from-line,to-line)` pair" shape of
the hole was testable directly as a zero/nonzero comparison, not confounded by a
width mismatch). Sample rows (full list is 18, all same shape — a bid whose location
shifted with the surrounding code edit and whose single reported arm count flipped
between `0` and nonzero):

```
[ZERO-ARM-SET-CHANGED] file=.../frontend/src/routes/TriageFeed.tsx bid=122
  before loc: locations=['358-358']
  after  loc: locations=['361-367']
  before counts: [0]
  after  counts: [55]

[ZERO-ARM-SET-CHANGED] file=.../frontend/src/routes/TriageFeed.tsx bid=123
  before loc: locations=['358-379']
  after  loc: locations=['367-367']
  before counts: [50]
  after  counts: [0]

[ZERO-ARM-SET-CHANGED] file=.../frontend/src/routes/TriageFeed.tsx bid=203
  before loc: locations=['730-730']
  after  loc: locations=['740-740']
  before counts: [0]
  after  counts: [469]

[ZERO-ARM-SET-CHANGED] file=.../frontend/src/routes/TriageFeed.tsx bid=205
  before loc: locations=['738-738']
  after  loc: locations=['738-738']
  before counts: [2]
  after  counts: [0]
```

**Verdict: clean.** Every one of the 18 changed pairs is in `TriageFeed.tsx`, the
slice's only frontend source change (Task 8) — exactly the file the brief says to
expect changes in. Zero changed zero-arm sets in any other file, so the fail-open
documented in the finding (branch identity ignoring columns, 341/1440 baseline branch
entries sharing a `(from-line,to-line)` pair with a sibling in the same file) is not,
in fact, hiding a same-line arm swap anywhere this slice's `compare_uncovered.py` runs
reported `exit=0`. This does not change the fail-open itself — fixing the gate's
matching rule is out of scope for this slice — it only confirms the hole has nothing
in it for this diff.

#### Housekeeping

`git status --porcelain` at the end of this task shows exactly one modified file: this
run log (`docs/superpowers/plans/2026-11-05-clean-code-slice-1/run-log.md`). No other
file in the worktree changed; `tests/test_profiles_pubmed.py` is byte-identical to
before the mutation (confirmed by `cmp`, above). No commits were made — Steps 4–6 were
explicitly out of scope for this task and were not run; `coverage-floors.json` was not
touched.

### Task 9 — Steps 4, 5 and 6 (the floors and the rest)

#### Step 4 — the floors

Before the update, with the final reports:

```
$ check_coverage_floors.py --section backend --report /tmp/s1-final-be-1.json
- sources: line 92.0% is below its floor 92.8%
- sources: branch 83.3% is below its floor 84.6%
backend floors exit=1
$ check_coverage_floors.py --section frontend --report /tmp/s1-final-fe-1-summary.json
frontend floors exit=0
```

Both final backend runs and both final frontend runs had already passed the per-line
check against the slice baseline (Step 2, four times `exit=0`), which is what permits a
floor to go down. `--update` was then run with **both** reports per section, as the plan
requires, because `--update` reads both and a report that has not passed the per-line
check must never set a floor.

```
$ git diff coverage-floors.json
     "sources": {
-      "branch": 84.6,
-      "line": 92.8
+      "branch": 83.3,
+      "line": 92.0
     },
     "web": {
-      "branch": 89.5,
+      "branch": 89.8,
     "<total>": {           (frontend)
-      "branch": 87.2,
+      "branch": 87.3,
```

Four floors moved, and every one matches the plan's dry-run prediction exactly.

**The one group whose floor goes DOWN, with its reason measured, not asserted.**
`sources` line 92.8 → 92.0 and branch 84.6 → 83.3. Measured from the two reports:

| `sources` | before | after | delta |
|---|---|---|---|
| statements | 591 | 505 | **−86** |
| **covered** lines | 549 | 465 | **−84** |
| uncovered lines | 42 | 40 | −2 |
| line % | 92.8 | 92.0 | −0.8 |
| branches | 222 | 198 | −24 |
| files in group | 6 | 5 | −1 |

(`line %` is the floor value, not ordinary rounding: `check_coverage_floors.py`'s
`tenths()` is `covered * 1000 // total`, integer floor division, i.e. it truncates.
549/591 = 92.893…% truncates to 92.8; 465/505 = 92.079…% truncates to 92.0 — both
round to 92.9/92.1 under ordinary rounding, which is why an earlier draft of this
table used those instead.)

So the percentage falls because deleting `sources/extraction.py` removed **84 covered
lines** and only 2 uncovered ones: the numerator shrank proportionally more than the
denominator. **No surviving line or branch lost its tests** — the group's uncovered count
went *down*, and the per-line check reported `0 new` for `sources` in both runs.

**Groups whose floor goes UP**, recorded as the plan requires: backend `web` branch
89.5 → 89.8 (the new tests of Tasks 2, 3 and 7; `web` uncovered branches 56 → 54 with
uncovered lines unchanged at 68), and frontend `<total>` branch 87.2 → 87.3 (Task 8's
three tests).

Re-checked after the update: backend `exit=0`, frontend `exit=0`.

#### Step 5 — everything else

```
mypy                       Success: no issues found in 119 source files
ruff check .               All checks passed!
tests/architecture/        281 passed in 18.00s
pnpm run lint              (clean, no output)
tsc -p tsconfig.json       tsc ok
gen_no_build.py --check    no-build-package is in sync with uv.lock.
gen_openapi_types.sh       schema unchanged
gen_architecture.py        wrote docs/ARCHITECTURE.md (192 lines) — no diff
pnpm run e2e:match         40 passed (39.0s)
git status --porcelain      M coverage-floors.json
                            M docs/.../run-log.md
```

`e2e:match` is the one browser run that is safe to do locally: `E2E_MODE=match` builds the
bundle and serves it statically on **port 4173** with the API mocked in-page. It needs no
backend and never binds port 8000, which belongs to the live app. The real-backend smoke
is CI's `e2e (prod)` and `e2e (dev)`, in Task 10.

#### Step 6 — the records the plan asks for

**Spec item 5 (merging the feed's decision code) was SKIPPED**, and the spec carries the
evidence (`2026-11-05-clean-code-design.md`, item 5, "Outcome of the prototype"):

- two merges were built and tested at `3c1c0f31` — a shared hook plus one combined undo
  (**+3 lines**, 5 rules de-duplicated), and the combined undo alone (**−6 lines**,
  2 rules);
- the repeated rules are one-line expressions, so a shared home costs about as much as it
  saves;
- the larger duplicates cannot be merged without a test change or a visible change: the
  `expectedState` binding on note saves, the note-editor toggle, and the error wording and
  clearing.

So item 5 has no task in this slice. What it *did* produce is Task 8's bug fix.

**The call page's test gaps go to piece 4.** From the spec: the call page's decision code
has test gaps, and the worst is that **swapping its Save and Hide endpoints passes every
test**.

**Two escalations this run recorded rather than fixed** (full detail in the run
directory's `PLAN-DEFECTS.md`):

- **E1** — `scripts/compare_uncovered.py` is fail-open for frontend sibling branch arcs
  that share a (from-line, to-line) pair: a *swap* between two such arcs exits 0. Fixing it
  would change the gate's documented matching rule. Instead this slice checked the hole
  directly: a throwaway per-branch-entry comparison of the baseline and final frontend
  reports found **18** entries whose uncovered-arm set changed, **all 18 inside
  `frontend/src/routes/TriageFeed.tsx`** — the slice's only frontend source change — and
  **zero** in any other file. The fail-open is real but hides nothing in this diff.
- **E2** — a *second* stale-alert bug, on the navigation paths: because `decisionError` is
  global and `TriageFeed` is not remounted by an in-place query-param navigation, a failed
  decision's alert also survives a view switch, a page change, a profile switch and
  search/sort. Reproduced with a throwaway test and no undo involved. Out of scope: it is a
  pre-existing bug on a different path class, and fixing it would add a user-visible change
  beyond this PR's three.

### Final review — fixes

Fixes for the final whole-branch review (base `5490bbf7`, head before this commit
`24f3fc52`). One commit for everything.

#### F1 — undecide() cleared a failed alert in the Inbox without firing a request

`frontend/src/routes/TriageFeed.tsx`: moved the Inbox early return (`view !== "saved"
&& view !== "hidden"`) ahead of `resetDecisionErrors()` in `undecide`, so pressing `u`
in the Inbox is a true no-op again, exactly as before Task 8. Renamed the three Task 8
alert tests ("goes away once X succeeds" implied success was the trigger; it is not —
`resetDecisionErrors()` runs synchronously before `mutate()`, so the alert clears on
attempt) to "clears on the next X, before the undo itself resolves". Added one test to
the "gate 9" describe: in the Inbox, fail a save, confirm the alert, press `u`, assert
both that neither `post` nor `del` gained a call AND that the alert is still present.

Mutation proof — reverted the `undecide` fix alone (test kept):

```
$ pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx
 FAIL  src/routes/TriageFeed.test.tsx > gate 9 ... > u in the Inbox after a failed
       decision leaves the alert in place and fires nothing
AssertionError: expected <alert> ... but it was null  (screen.getByRole("alert") threw)
 Test Files  1 failed (1)
      Tests  1 failed | 92 passed (93)
```

Restored the fix with `cp` + `cmp`:

```
$ pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx
 ✓ src/routes/TriageFeed.test.tsx  (93 tests) 4974ms
 Test Files  1 passed (1)
      Tests  93 passed (93)
```

#### F2 — the Task 6 dependency gate's own widening was untested

`tests/architecture/test_removed_modules_stay_gone.py`: `test_the_dependency_check_
refuses_a_readded_requirement` now loops over THREE insertion points —
`[project].dependencies`, a synthetic `[project.optional-dependencies]` extra, and
`dependency-groups.dev` — each with the same four requirement strings, instead of
only the first. Same test function, same name; test count in the file is unchanged
at **8** (the loop runs 3× as many assertions inside the one test).

Mutation proof — typo'd `declared_dependencies`'s dependency-groups key to
`"dependency-groupz"`:

```
$ .venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/test_removed_modules_stay_gone.py -v
......F.
AssertionError: not refused via [dependency-groups]: trafilatura>=2.0.0
1 failed, 7 passed in 0.63s
```

Restored (`cp` + `cmp`), then typo'd the optional-dependencies key to
`"optional-dependenciez"`:

```
$ .venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/test_removed_modules_stay_gone.py -v
......F.
AssertionError: not refused via [project.optional-dependencies]: trafilatura>=2.0.0
1 failed, 7 passed in 0.62s
```

Restored (`cp` + `cmp`); green again:

```
$ .venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/test_removed_modules_stay_gone.py
........
8 passed in 0.58s
```

#### F3 — stale docstring on ScoringConfig.from_settings (docstring only, no code)

`src/grant_lab/ranking/freshness.py`: `ScoringConfig.from_settings`'s docstring said
it "Mirrors the one-to-one mapping main.py hands build_scheduler" — stale since Task 5
removed those parameters from `build_scheduler` entirely. Rewritten in present tense:
"Builds the same ScoringConfig from Settings that build_scheduler builds inline, so a
pair's fingerprint is the same whichever path computed it." — keeping the equivalence
invariant (the fingerprint-matching fact the human is escalating separately as F3's
`build_scheduler`-call-site question) findable. No code touched, per the brief.

#### F6a — stale "derivation site" comment

`tests/test_security_headers.py`: the dev-CSP golden-pin comment referenced "the
ws_origin-validation fix at the derivation site", which no longer exists (`Task 7`
replaced `_derive_ws_origin` with `_validate_app_base_url`, an unrelated APP_BASE_URL
check). Rewritten to reference what now exists: prod's `connect-src` is a bare
`'self'`, carrying no `ws_origin` at all. Confirmed by grep that this was, and remains,
the only `ws_origin` mention left in the tree (the new comment's own explanatory use
aside).

#### F6b — the "every Python file" gate only scanned src/scripts/alembic

`tests/architecture/test_removed_modules_stay_gone.py`: `SCANNED_DIRS` widened to
`("src", "scripts", "alembic", "tests")` so `test_no_python_file_imports_a_removed_
library`'s name matches its scope. Checked first that `tests/` is clean under the
AST-based import scan (confirmed empty list via a standalone script) before widening.

Proof — created `tests/zz_scope_probe.py` containing `import trafilatura`:

Before the widening: `8 passed` (the reviewer's exact measurement — the probe was
invisible to the old scope).

After the widening:

```
$ .venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/test_removed_modules_stay_gone.py -v
.F......
AssertionError: assert ['tests/zz_scope_probe.py:1 import trafilatura'] == []
1 failed, 7 passed in 1.19s
```

`rm`'d the probe; `git status --porcelain` showed no trace of it afterward.

#### F6c — run-log table numbers didn't match the floors they justify (docs only)

`docs/superpowers/plans/2026-11-05-clean-code-slice-1/run-log.md`, Task 9 Steps 4-5-6's
`sources` table: `line %` read `92.9 → 92.1` (ordinary rounding) while the floors it
justifies are `92.8 → 92.0` (the checker's rounding). Read `scripts/check_coverage_
floors.py`'s `tenths()` first: `covered * 1000 // total`, integer floor division —
truncates, does not round. Verified the raw fractions: 549/591 = 92.893…% truncates to
92.8; 465/505 = 92.079…% truncates to 92.0. Corrected the table's `line %` row to
`92.8 → 92.0` and added a parenthetical explaining the truncation with both raw
fractions, so a future reader checking the floor against this table sees them agree.

#### Post-fix verification (full commands and output in task-9-report.md)

- `pnpm -C frontend exec vitest run src/routes/TriageFeed.test.tsx` → **93 passed**
  (was 92; +1 is the new F1 Inbox test).
- `pnpm -C frontend run test` → **Test Files 55 passed (55)**, **Tests 534 passed
  (534)** (was 533; the term that changed is Tests, +1, the same new F1 test).
- `pnpm -C frontend exec tsc -p tsconfig.json` → clean (no output, exit 0).
- `pnpm -C frontend run lint` → clean (`oxlint --deny-warnings src e2e`, no output).
- `uv run pytest -q -p no:cacheprovider tests/architecture/` → **281 passed** (was 281;
  unchanged — F2's fix reused the same test function, no new test added).
- `uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider` → **2487 passed, 0
  skipped, 0 xfailed** (was 2487; unchanged — none of these fixes added or removed a
  backend test function).
- `uv run mypy --config-file pyproject.toml` → `Success: no issues found in 119 source
  files`.
- `uv run ruff check .` → `All checks passed!`.
- `uv run ruff format --check src/grant_lab/ranking/freshness.py tests/test_security_
  headers.py tests/architecture/test_removed_modules_stay_gone.py` → `3 files already
  formatted`.
- `uv run python scripts/gen_architecture.py` → wrote 192 lines, `git status --short
  docs/ARCHITECTURE.md` empty (no change).
- `uv run python scripts/gen_no_build.py --check` → `no-build-package is in sync with
  uv.lock.`, exit 0.

No coverage suite was run and `coverage-floors.json` was not touched, per instruction.
