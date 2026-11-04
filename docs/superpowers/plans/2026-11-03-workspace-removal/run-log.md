# Workspace removal — run log

Measured numbers only. Every line was produced by a command in the plan.

## Task 0 — baseline (2026-11-04)

- BASE = `fca2c678` (master tip; branch `chore/workspace-removal` in `.worktrees/workspace-removal`)
- Backend suite (`uv run pytest -n 16 --dist loadfile -q`): **6178 passed, 1 skipped, 6 xfailed** in 175 s, exit 0
- Collected tests: **6185** in **614** files (per-file list: `/tmp/wr-base-per-file.txt`)
- Frontend: `tsc --noEmit` exit 0; `lint` exit 0; vitest **157 files, 1371 tests** passed
- `e2e:match`: **38 passed** (mocked API, static server on :4173)
- Real-backend base: port 8000 held by the live app (`ss -ltn | grep -c ':8000 '` = 1), so not run locally.
  Master's last CI run: `41259468734`, head `dfb6dbae`, conclusion **success**.
- G1 must-reject baseline: `proposal_assist` outside the package = **192 lines in 29 files**
- Python files in `src/`: **311**; files in `tests/proposal_assist` + `tests/fencing`: **396**

## Task 1 — freeze (local)

- `workspace` = `workspace-v1^{commit}` = master = **`fca2c67893c7e895fc9d8bb98625b9e4aeb085b3`**; tag is not at the run-log commit. Not pushed (Task 11).

## Task 2 — freeze the migrations (0037, 0066, 0068)

- Red (before the frozen copies): 44 failed, 5 passed in `tests/migrations` + `tests/test_migration_0037_backfill.py`. Green after: 49 passed.
- G2b, each mutation red then restored (`cmp` against the /tmp copy printed nothing):
  - 0037 logic (digest of `storage_path`): `test_backfill_fills_present_skips_absent`, `test_each_row_gets_the_hash_of_its_own_file`, `test_backfill_idempotent`, `test_backfill_skips_unreadable_file`, `test_upgrade_0037_hashes_rows_that_predate_it`.
  - 0037 wiring (`_backfill_original_sha25(`, NameError swallowed): only `test_upgrade_0037_hashes_rows_that_predate_it` (1 failed, 6 passed).
  - 0066 logic (`name = "main"`): `test_backfill_reads_the_repos_actual_branch`, `test_each_workspace_gets_its_own_repos_branch`, `test_backfill_records_an_unborn_repos_branch_rather_than_failing`, `test_backfill_RAISES_on_a_detached_head`, `test_upgrade_0066_backfills_rows_that_predate_it`.
  - 0066 fidelity (drop `updated_at=now()`): `test_the_backfill_restamps_updated_at_as_the_orm_update_did`, `test_upgrade_0066_backfills_rows_that_predate_it`.
  - 0068 (`vacant = True`): `test_running_it_twice_changes_nothing`, `test_a_surviving_root_pointer_is_not_cleared_by_an_unrelated_orphan`, `test_reconciling_after_a_switch_does_not_move_the_root`.
  - 0068 alt (`orphans = set()`): `test_a_row_whose_ref_is_gone_is_deleted`, `test_reconciling_survives_the_root_branchs_ref_being_deleted`, `test_a_rootless_workspace_whose_root_ref_vanished_is_left_honestly_rootless`, `test_a_surviving_root_pointer_is_not_cleared_by_an_unrelated_orphan`, `test_a_repo_whose_last_ref_vanished_is_swept`.
- Full suite: **6175 passed, 1 skipped, 6 xfailed** (154.8 s). Delta vs base 6178: **-3**. Accounted: removed 6 (old 0037 test) + 2 (old isolation) + 4 (column migration) + 42 (entity backfill, collected) = 54; added 51 (49 migration tests + 2 isolation). 51 - 54 = -3.
- `docs/ARCHITECTURE.md` regenerated: no change.
- Plan fix: B's `test_the_backfill_restamps_updated_at...` used `dict(s.execute(...))`, which raises TypeError; changed to `.all()`.

## Task 3 — delete the workspace backend

- Route baseline (`tests/architecture/app_routes.txt`, regenerated with C's command): **51** paths (`wc -l`), so `_BASELINE_SIZE = 51`. Old `full_edition_routes.txt`: 120. All 69 removed paths are under `REMOVED_PREFIXES`; 0 added.
- G1 `git grep -n proposal_assist -- src`: **8 lines, all in `src/grant_lab/config.py`** (lines 30, 37, 245, 251, 264, 630, 638, 1005), exit 0. They are the `_LOCAL_SPEND_TAG` comment, the three `proposal_assist_*` fields, two `review_price_*` descriptions and the `_match_edition_disables_drafting` comment, all on Task 5's delete list. With `':!src/grant_lab/config.py'`: no output, **exit=1**. Must-reject baseline (Task 0): 192 lines in 29 files.
- Import-time links (`signal_user_revocation|_full_only_routers|drafting_router|packaged_edition|app_loop` in `src`): no output, **exit=1**.
- G2 must-reject (master's 0066 over the frozen copy, `tests/test_schema_drift.py`): **exit=1**, `ModuleNotFoundError: No module named 'grant_lab.proposal_assist'`. Restored with `cp`; `cmp` printed nothing; re-run green (1 passed).
  - First attempt printed `No module named 'grant_lab.proposal_assist.branch_backfill'` instead: `git rm` had left 539 ignored `.pyc` files in `src/grant_lab/proposal_assist/`, `tests/proposal_assist/` and `tests/fencing/`, so the directory imported as a namespace package. Removed (0 non-`.pyc` files there), then re-ran.
- Lint: `ruff check .` clean; `ruff format --check` on the 58 A/M `.py` files + `test_route_surface.py` clean.
- Full suite (`-n 16 --dist loadfile`): **2301 passed** in 50.1 s, exit 0, **0 skipped, 0 xfailed** (the skip and the 6 xfails were in `tests/proposal_assist/`).
- Per-file delta vs Task 0 (`/tmp/wr-base-per-file.txt`, 6185 collected / 614 files → 2301 / 247): 435 diff lines, 385 files changed, **-3884**, none unexplained:
  - `tests/proposal_assist/` (315 files): 3191 → 0 (-3191; 46 of them already left in Task 2)
  - `tests/fencing/` (11 collected files): 255 → 0
  - 44 deleted files: 455 → 0 (C 1a/1b DELETE rows, `test_content_disposition` (A, Task E), `test_events_ping` (D3), and 3 architecture files)
  - 9 trimmed: 128 → 105 (-23): auth_allowlist 10→6, profile_access_single_source 49→47, auth_domain_policy 7→6, embeddings_truncation_defect 7→3, test_main 5→4, test_net_safety 22→21, pool_exhaustion_event_loop 3→1, test_scheduler 15→8, web_filters_model_label 10→9
  - rename `test_edition_surface` 13 → `test_route_surface` 10 (C's 11 minus the Task 5 test)
  - Task 2 moves/rewrites: 6 → 49 (+43)
  - Task 3 alone, vs Task 2's 6182 collected: 2301 - 6182 = -3881 = 3145 + 255 + 455 + 23 + 3.
- Step 7 probes (all non-empty): after = **51** routes, exit 0; `cmp` with `app_routes.txt`: matches. `master:full_edition_routes.txt`: 120. Master (`fca2c678`, detached throwaway tree; `master` itself is checked out in the main tree) with `APP_EDITION=match DRAFTING_ENABLED=false`: **51**, exit 0.
  - `comm -13 full after`: empty (no new route).
  - `comm -23 match-before after`: empty (no lost match route). The two lists are byte-identical (`cmp`).
- `docs/ARCHITECTURE.md` regenerated: -26 lines (the epoch-fence and armed-gate allowlist sections).

## Task 4: park the 31 workspace-era table definitions

- Moved `documents`, `jobs`, `references`, `spend`, `workspaces` to `db/models/workspace/` with `git mv` (contents unchanged); wrote `workspace/__init__.py`, `workspace/call_extracts.py` (`CallSectionExtract`, `CallRubric`, cut from `calls.py`, which now ends at line 144) and the new `db/models/__init__.py` (imports `workspace`, does not re-export its classes; B 1c).
- Imports switched to `grant_lab.db.models.workspace` in `tests/migrations/{support,test_0066_backfill,test_0068_backfill}.py` and the 0037, 0040, 0041, 0064, 0067, 0069 tests. An AST scan of all tracked `.py` files then found one remaining importer of a moved class from `grant_lab.db.models`: `scripts/collab_e2e_seed.py:109`, which already imports deleted `proposal_assist` modules and is replaced by Task 6/D11. Left alone.
- `scripts/gen_architecture.py`: B 1g version (the fence section was already gone); parked tables labelled "(parked)".
- `tests/test_schema_drift.py` replaced by B 4b: 1 test -> 2 (+1).
- G3 (each run alone, `PYTHONDONTWRITEBYTECODE=1`, restored with `cp`, `cmp` silent):
  - Mutation 1 (delete `SpecLockEvent` from `workspaces.py` and `workspace/__init__.py`): exit=1, 2 failed. Drift: `remove_index: ix_spec_lock_events_ws_created` + `remove_table: spec_lock_events`. Subprocess test: `importing grant_lab.db.models does not register ['spec_lock_events']`.
  - Mutation 2 (delete `from grant_lab.db.models import workspace` from `db/models/__init__.py`): alone, exit=1, 2 failed, `remove_table:` for all 31 tables. Full suite (`-n 16`): exit=1, `2 failed, 2300 passed`: `test_the_models_package_alone_registers_every_parked_table` (the D21 subprocess check) and `test_architecture_map_is_current`. `test_orm_metadata_matches_the_migrated_database` stayed GREEN in the full run, as D21 predicts; only the subprocess check catches it there.
- Map: regenerated, 48 mapped tables, 31 parked; `test_architecture_map_is_current` exit=0; the MISSING-table loop over `Base.metadata.tables` printed nothing. The diff's removed lines are the same rows re-added with "(parked)", plus the new intro paragraph and the "48 mapped tables, 31 of them parked." line.
- `ruff check .` clean.
- Full suite (`-n 16 --dist loadfile`): **2302 passed**, exit 0, 0 skipped. Delta vs Task 3 (2301): **+1**, the added subprocess test.

## Task 5: drop the workspace and edition settings

- Step 1 RED (`test_leftover_edition_settings_change_nothing`, added first): exit=1, `AssertionError: assert 'app_edition' not in {... Settings.model_fields ...}`, `1 failed, 10 deselected`.
- 72 `Settings` fields removed (the 10+8+54 of A, Task D), plus `from decimal import Decimal`, `_LOCAL_SPEND_TAG` and the validators `_citation_model_cannot_forge_the_local_tag` and `_match_edition_disables_drafting`. `profile_bootstrap_tick_seconds` and `scheduler_standby_ok` kept. Descriptions rewritten: `app_base_url`, `scheduler_standby_ok`, and the `max_request_body_bytes` comment (default 64 MiB unchanged).
- `scripts/gen_env_example.py` `DAY_ONE`: 13 keys dropped. `.env.example` regenerated (46 settings); `docs/ARCHITECTURE.md` regenerated (192 lines).
- G1 (`git grep -n proposal_assist -- src`): no output, **exit=1** (was 8 lines, all in config.py).
- Step 3 ("STILL READ" loop over 9 names, src+tests+scripts): printed only `done`; no `STILL READ` line.
- Step 5, live `.env` has 20 keys; ignored by Settings (names only): `APP_EDITION`, `DRAFTING_ENABLED`, `REMOTE_RANK_COSINE_THRESHOLD`, `VINNOVA_API_KEY` (the last two were already unknown before this task).
- Tests: `test_config.py` -3 (citation provider default, reference ingest defaults, dead batch settings); `test_env_example.py` assertion block swapped for the `remote_rank_model` guard (5 tests unchanged); `test_route_surface.py` +1. `test_config_extract_timeout.py` and the `test_web_filters_model_label.py` drafting-model test were already gone (removed in Task 2).
- Unit run (route_surface, env_example, config): 32 passed, exit 0. `ruff check .` clean.
- Full suite (`-n 16 --dist loadfile`): **2300 passed**, exit 0, 0 skipped. Delta vs Task 4 (2302): **-2** = -3 (`test_config`) +1 (leftover-settings test).

## Task 6: remove the workspace frontend and the edition split

- D 1.2 `git rm` / `git mv` run verbatim: 241 deleted, 5 moved (`router.tsx` / `router.test.tsx` show as modified, because the moves land on paths just deleted). Frontend unchanged since 388494c8 (`git log 388494c8..HEAD -- frontend` empty).
- Base `VITE_APP_EDITION=match pnpm run test` (D 1.4's pre-measure): 6 failed / 1365 passed. The failures were ProfileDetail ×1, ProfileEdit ×1 and ProfileNew ×2, which is D's prediction, plus writepath ×2, which this task deletes.
- **Libraries.** `pnpm remove` of D's 24: exit 0. **Lockfile `+` lines: 3, not 0.**
  - All 3 add `optional: true`: one under the existing `@types/node@26.1.0` snapshot, two turning `undici-types@8.3.0: {}` into a block. Both packages were reached through the removed `@types/ws` and are now reached only through optional peers.
  - Nothing was re-resolved. Package keys went 395 → 328 and snapshot keys 395 → 328: 0 added and 67 removed in each.
  - `pnpm -C frontend install --frozen-lockfile`: exit 0 ("resolution step is skipped").
  - The brief's gate (`+` count 0) still failed, so this task was not committed by its executor. The decision went back to the controller.
- **Routes.** D5 applied: `gen_routes.py` + `tests/test_gen_routes.py` exit 0 (4 passed). `spa_routes.py` lost `"/cockpit"` and `/workspaces`; `routes.gen.ts` changed 2 lines. Both tuples equal A C4's expected values.
- **Router must-reject** (base `routes.gen.ts`, measured): exit 1, `expected [ '/cockpit' ] to deeply equal []`. Restored with `cp`; `cmp` silent; 7 passed.
- **OpenAPI.** `APP_EDITION=full` comment and line removed from `gen_openapi_types.sh`. Regeneration exit 0; `schema.gen.ts` went 18688 → 9414 lines. `grep -cE "workspaces|drafting|citation-audit"`: **0**.
- **Frontend gates:**
  - tsc: exit 0. oxlint: exit 0. `pnpm run build`: exit 0.
  - vitest: exit 0, **55 files / 524 tests** (base 157 / 1371, so **-102 files / -847 tests**).
  - The deleted files account for 102 files and -815 tests. The rest: router.simple 5 → router.test 7 (+2), CallDetailBody 17 → MatchCallOverview 9 (-8), CallDetail 34 → 12 (-22), AdminNav 4 → 2 (-2), AdminDashboard 10 → 9 (-1), api/adminDashboard 6 → 5 (-1).
- **`e2e:match`:** exit 0, **38 passed** (base 38). `match.spec.ts` comments only: still 36 scenarios and 185 `expect()`.
- **Bundle vs live.** Main JS **779,487** B against live **780,075** B (-588). AdminLayout 1,006 vs 1,185 B. CSS 273,244 vs 273,248 B: the only differences are CSS-module class suffixes and one rule's position. The same 17 assets, all other sizes identical.
- **Packaging must-reject (Step 2b).** `rm -rf "$SPA"` moved above the dist check: `-k missing_build` **exit=1**, "the served directory was deleted on the way to failing". Restored with `cp`; `cmp` silent; 10 passed.
- **Lint.**
  - `ruff check .` flagged `scripts/e2e_seed.py` I001 once the move gave it a new path. The same bytes fail under the old name: since Task 3 the file was lint-red, and only ruff's content-keyed local cache hid it. CI runs with no cache.
  - Fixed with `ruff check --fix`, an import reorder only. `ruff check --no-cache .` passes.
- `docs/ARCHITECTURE.md` regenerated: no change.
- **Backend suite** (`-n 16 --dist loadfile`): **2290 passed**, exit 0, 0 skipped, 48.8 s. Delta against Task 5 (2300): **-10** = `test_package_spa` -7 (17 → 10) + `test_match_gates_are_wired` -2 (18 → 16) + `test_secret_file_exclusion` -1 (the `frontend/.env.example` case). `test_e2e_run_cleanup` (renamed): 1 → 1.
- Controller decision on the lockfile gate: the 3 `+` lines add only `optional: true` to two entries already in the lockfile (`@types/node@26.1.0`, `undici-types@8.3.0`); 0 package/snapshot keys added, 67 removed, `--frozen-lockfile` install passes. The gate guards against re-resolution (the 7-day cooldown); none happened, so accepted.

## Task 7: real-backend browser smoke (G7)

- Pins RED (`-k smoke`, before the spec/job existed): `3 failed, 6 passed, 10 deselected` (the 3 new pins fail with FileNotFoundError on smoke.spec.ts). GREEN after: `tests/architecture/test_match_gates_are_wired.py test_e2e_run_cleanup.py test_package_spa.py`: 30 passed.
- `E2E_MODE=dev pnpm exec playwright test --list e2e/smoke.spec.ts`: `Total: 4 tests in 1 file`, titles exactly SMOKE_SCENARIOS.
- `pnpm run lint` (oxlint src e2e): All checks passed. `ruff check` + `ruff format --check` on scripts/e2e_seed.py and the pins file: clean. `bash -n scripts/e2e_run.sh`: ok. shellcheck not installed. ast.parse of the seed: ok. ci.yml parses as YAML.
- Port 8000: `ss -ltn | grep -c ':8000 '` printed 1 (live app). Steps 4 and 5 (real run, G7 must-reject) SKIPPED; they run in CI (Task 10).
- Deviations: CI step sets `E2E_SPECS: e2e/smoke.spec.ts` (D 2.6 omitted it, but C's pin requires the path in the step env/run); seed drops `kind="api"` from the fallback `Source(...)` (Source has no `kind` column; latent TypeError, only reachable when the vinnova row is absent).
- Full suite: 2293 passed (base 2290, +3 smoke pins). ARCHITECTURE.md regenerated, no change.

## Task 8: Python libraries (G5)

- Step 1 grep (13 module names + `httpx_mock`): no `STILL` lines. It missed one thing: `jsonschema`, which is not in the brief's list. `tests/test_ranking_response_schema.py` imports it, and it was only present transitively via `mcp`. The first full run collected 2281 passed + 1 collection error (`No module named 'jsonschema'`). Fix: `uv add --dev jsonschema` (4.26.0, the previously locked version); `rpds-py` re-resolved to 2026.9.1 and was pinned back with `uv lock --upgrade-package rpds-py==0.30.0`.
- Removed: the 11 runtime deps, the `mcp` constraint (and its comment), `reportlab`, `pytest-httpx`, the ASYNC221 block; `scripts/gen_no_build.py` regenerated the no-build list.
- uv.lock check (before vs after, script over `[[package]]` name/version): kept packages with a changed version: none; packages added: none. Removed: bleach, claude-agent-sdk, cssselect, et-xmlfile, httpx-sse, markdown, mcp, openpyxl, pdfminer-six, pillow, prosemirror, pycrdt, pyjwt, pytest-httpx, python-docx, reportlab, sse-starlette, striprtf, webencodings. pymupdf, trafilatura, anthropic, segno, joserfc, jinja2 kept.
- `import grant_lab.main`: import-ok. `ruff check .`: All checks passed. ARCHITECTURE.md regenerated, no change.
- Suite: 2293 passed (exit 0).
- G5 fresh tree /tmp/wr-g5 (`uv sync --frozen`, `pnpm install --frozen-lockfile`): suite 2293 passed (exit 0); `pnpm run build` exit 0. Smoke half (`scripts/e2e_run.sh`) skipped: port 8000 held by the live app; CI covers it in Task 10.
- G5 must-reject: `import pycrdt` added after the `from __future__` line of `src/grant_lab/web/filters.py` in the scratch tree only: `ModuleNotFoundError: No module named 'pycrdt'`. (A first attempt put it above `from __future__` and gave a SyntaxError instead; that was not a valid rejection and was redone.) Scratch tree removed.

## Task 9: operational docs

- Edited README.md, frontend/README.md, CLAUDE.md per D section 4; docs/ARCHITECTURE.md regenerated, no diff.
- Stale-command grep (Step 1): exit=1 (no matches).
- `uv run pytest -q -p no:cacheprovider tests/architecture tests/test_env_example.py`: 180 passed.
- CLAUDE.md skip note uses the measured 2293 passed / 0 skipped / 0 xfailed.

## Task 10 — final verification (controller, local)

- G1 `git grep -n proposal_assist -- src`: exit 1 (nothing). `ruff check .`: exit 0.
- Backend: **2293 passed** (0 skipped, 0 xfailed) in 49 s, exit 0. Base was 6178 passed / 1 skipped / 6 xfailed in 175 s.
- Frontend: tsc 0, lint 0, vitest **55 files / 524 tests** (base 157 / 1371), `e2e:match` **38 passed** (base 38).
- Drift: OpenAPI types exit 0, architecture map exit 0.
- Real-backend smoke: not run locally (:8000 held by the live app) → CI on the draft PR.
- Size: Python src **120 files / 25,187 lines** (base 311 / 71,067); Python tests **253 files / 56,178 lines** (base 661 / 156,450); frontend non-test **17,352 lines** (base 38,624). Branch: 1036 files changed, +5,050 / −196,304.

## Final-review fixes

- I1: leftover-settings test now loads a real .env (APP_EDITION, DRAFTING_ENABLED, WORKSPACES_DIR) via model_config env_file. With extra="forbid" it went RED (extra_forbidden); restored, GREEN.
- F2: prefix check covers every route path incl. mounts. A /workspaces mount in main.py turned it RED (2 failed); restored, GREEN.
- F3: smoke pin requires scripts/e2e_run.sh and matrix {dev, prod}. Matrix reduced to [dev] turned it RED; restored, GREEN.
- F4: CLAUDE.md build command and the jsonschema comment in pyproject.toml.
- Suite: tests/architecture 175 passed; full 2293 passed (unchanged, no tests added).

## Task 10 — CI on the draft PR (2026-11-04)

- Draft PR https://github.com/parlar/grant-lab/pull/27 at `30bd5225`, run https://github.com/parlar/grant-lab/actions/runs/41818748404:
  backend **2293 passed** (same as local), audit, frontend, package, match-e2e, e2e (dev), e2e (prod) all pass.
  Real-backend smoke: **4 passed** in dev and **4 passed** in prod.
- `dependency-review` (separate workflow) fails in 5 s: "Dependency review is not supported on this repository" —
  the private repo has no GitHub Advanced Security. A repo setting, unrelated to this branch.
- G7 must-reject: branch `wr-g7-mutant` (`d6362803`), draft PR https://github.com/parlar/grant-lab/pull/28,
  run https://github.com/parlar/grant-lab/actions/runs/41818899343. `signOut` in `SimpleShell.tsx` skipped
  `logout.mutate` (`void logout;` kept tsc clean). e2e (dev) and e2e (prod) each **1 failed / 3 passed**, the failure
  being "signing out through the account menu lands on /login and ends the session" at
  `expect(me.status()).toBe(401)`, received 200. Also red: frontend (2 SimpleShell sign-out tests), match-e2e (1).
  PR closed and the branch deleted.
