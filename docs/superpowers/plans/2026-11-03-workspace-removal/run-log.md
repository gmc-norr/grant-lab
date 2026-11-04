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
