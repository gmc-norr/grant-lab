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
