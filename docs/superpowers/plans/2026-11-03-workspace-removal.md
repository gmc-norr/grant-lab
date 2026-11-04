# Workspace Removal (Piece 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the proposal workspace and the match/full edition split from master, so the match app is the only app, with no change users can see.

**Architecture:** Freeze today's master on a `workspace` branch and `workspace-v1` tag. Then, on a feature branch in `.worktrees/workspace-removal`, work in this order:
1. Make the three workspace-reading migrations self-contained.
2. Delete the workspace backend in one atomic commit, with its tests.
3. Park the 31 workspace-era table definitions in `db/models/workspace/`. This comes after the deletion, because `tests/fencing/` pins the old model paths.
4. Remove its settings.
5. Remove the workspace frontend and the edition split, with the CI steps, scripts and tests that break without it.
6. Replace the full-build browser tests with a real-backend smoke spec.
7. Remove the workspace-only Python libraries.
8. Update the operational docs.
9. Run CI on a draft pull request, then deploy (stop the app, merge, rebuild, start) and push master.

There is no database change.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, pytest + testcontainers Postgres, uv. React 19 + Vite, pnpm, vitest, Playwright, oxlint. GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-03-workspace-removal-design.md` (with its Errata section). Programme: `docs/superpowers/specs/2026-11-03-match-cleanup-programme-design.md`.

**Appendices** (exact edits, read-only mapping done at `388494c8`, code identical to `dfb6dbae`). They are the edit lists this plan points to. Each claim in them is labelled READ, MEASURED or INFERRED. Where an appendix offers options or asks a question, the **Decisions** section below rules.
- `docs/superpowers/plans/2026-11-03-workspace-removal/A-backend.md`: live backend edits (sections "Task A" to "Task E").
- `docs/superpowers/plans/2026-11-03-workspace-removal/B-database.md`: table move, frozen migrations, backfill tests, schema drift ("TASK 1" to "TASK 4").
- `docs/superpowers/plans/2026-11-03-workspace-removal/C-tests.md`: every test file to delete, trim, edit or rewrite ("TASK 1" to "TASK 4").
- `docs/superpowers/plans/2026-11-03-workspace-removal/D-frontend-ci-docs.md`: frontend, browser tests, CI, scripts, docs (sections 1 to 4).

Line numbers in the appendices are as of `388494c8`. They drift as soon as an earlier step edits a file, so **locate every edit by the enclosing function or class name the appendix gives**, and use the line number only as a hint.

## Global Constraints

- Users see the same match app. No new features.
- No database change. All 48 tables and their data stay, and there is no new migration.
- Never rename the DB role/name `call_manager`, the env var `CALL_MANAGER_DATA_DIR`, the session-cookie salt, the OAuth HKDF info string or `SCHEDULER_LOCK_NAME`.
- Never print, `cat`, `tail` or edit any `.env` file. Grep key names only.
- All work happens in the worktree `W=/home/parlar_ai/dev/grant-lab/.worktrees/workspace-removal`.
  - Start every shell command with `cd $W &&`, because the shell's cwd resets.
  - Use `git -C $W` for git.
  - Before each commit, check `git -C $W branch --show-current` prints `chore/workspace-removal`.
- The live app serves the **main tree** (`/home/parlar_ai/dev/grant-lab/src/grant_lab/web/spa`).
  - Never run `pnpm run package`, `scripts/package_spa.sh` or `E2E_SPA_MODE=prod scripts/e2e_run.sh` outside `$W`.
  - Never start a second app against the live database: the scheduler is single-instance.
  - Never touch `systemctl` until Task 11, and then only on the user's word.
- Full backend suite: `uv run pytest -n 16 --dist loadfile`. Never `-n auto`.
  - Save output to a file and read the exit code. `pytest | tail` hides failures.
- Never `ruff format` a directory. Format named files only.
- Mutation checks:
  - Copy the file to `/tmp/<name>.orig` first, and restore with `cp`. Never `trap` or `git stash`.
  - Run with `PYTHONDONTWRITEBYTECODE=1`.
  - Confirm restoration with `cmp /tmp/<name>.orig <file>`, which must print nothing. Never use `git diff` for this: the file may hold intended, uncommitted edits.
  - Mutate the file the test actually reads. A test that copies a repo file into a fixture reads the repo file, not a `/tmp` copy.
- Port 8000 belongs to the live app.
  - `scripts/e2e_run.sh` (the real-backend smoke) binds :8000, so it cannot run locally while the live app runs. Check with `ss -ltn | grep -c ':8000 '`.
  - Never stop the live app to make room.
  - The smoke and its must-reject run in CI on a pull request (Task 10).
- Every task ends by running `uv run python scripts/gen_architecture.py` and committing any change to `docs/ARCHITECTURE.md` with that task. The map-drift test is part of the full suite.
- Test totals: measure the base in Task 0 and carry **deltas**. Never write an expected total, and never adjust a number to make the arithmetic close.
- New tests that do not enter the app lifespan use the `make_app` fixture, never a bare `TestClient(create_app())`.
- Regenerate `docs/ARCHITECTURE.md` (`uv run python scripts/gen_architecture.py`) in the same commit as any code move it describes. Never hand-edit it.
- Commit titles name the change. Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Merge, push and redeploy happen only on the user's word (Task 11).
- Record every measured number in `docs/superpowers/plans/2026-11-03-workspace-removal/run-log.md`, in the worktree, committed with the task that measured it.

## Decisions (they override the appendices)

| # | Question | Decision |
|---|---|---|
| D1 | CSP `ws_origin` token (A: Option A/B) | **Keep it (Option B).** Rewrite the comments only. Headers stay byte-identical. Piece 3 may remove it. |
| D2 | `/api/me` `capabilities.retention_inspection` | **Drop it.** It is always false, and its only reader (`AdminDeletedWorkspaces.tsx`) is deleted. Edit `tests/test_api_me.py` (C, Task 4 "Other conditional edits"). |
| D3 | `web/api/events.py` and `/events/ping` | **Delete them**, with: the dev-only mount in `main.py`, `tests/test_events_ping.py`, and `("GET", "/events/ping")` in `PUBLIC_ROUTES` of `tests/architecture/test_auth_allowlist.py`. The only client was `CockpitChat.tsx` (verified). |
| D4 | Dead constants | **Delete** `net_safety.SSRF_REFUSAL_MESSAGE` and trim `tests/test_net_safety.py` where it is used. **Keep** the `BUSY_ERROR_MESSAGE` text byte-for-byte (it is wire-visible). |
| D5 | `frontend/routes.json` | **Drop** `"/cockpit"` from `spa_routes` and `"/workspaces"` from `backend_prefixes`. Regenerate with `uv run python scripts/gen_routes.py` in Task 5. `/cockpit` becomes a JSON 404. |
| D6 | Router tuple in `web/api/__init__.py` | **Rename** `_ALWAYS` to `_ROUTERS`. Update the comments that cite it (`router.simple.tsx`, `AdminDashboard.test.tsx`) in Task 5. |
| D7 | `sources/extraction.py`, `pymupdf`, `trafilatura` | **Out of scope.** Nothing in `src/` imports `extraction.py` (verified). It is dead code for piece 3. Both libraries stay in piece 1. |
| D8 | `?view` archive and `min_score` | **Out of scope** (piece 3). `api/dashboard.ts` keeps the `archive` view type. |
| D9 | `docs/ARCHITECTURE.md` coverage | **Option A.** `scripts/gen_architecture.py` also globs `db/models/workspace/*.py` and drops its fence section (`FENCING`, `INVENTORY`). The 15 map tests stay unchanged. |
| D10 | `scripts/package_spa.sh` repo-root check | **Use D section 3's script**: it checks `[ -d "$ROOT/src/grant_lab/web" ]`. Adapt C's `test_package_spa.py`: `_tree()` already creates `src/grant_lab/web/spa`, so drop `ROOT_MARKER` and its file. `test_it_refuses_a_tree_that_is_not_the_repo_root` deletes `src/grant_lab/web`, then asserts exit 1, the message `is not the repo root`, and that `src/grant_lab/web/spa` was not recreated. |
| D11 | Real-backend e2e runner | `git mv scripts/collab_e2e_seed.py scripts/e2e_seed.py` (rewritten per D 2.4: adds a Profile and sets `default_profile_id`, keeps the secret literal). `git mv scripts/collab_e2e_run.sh scripts/e2e_run.sh` (D 2.5). `git mv tests/architecture/test_collab_e2e_run_cleanup.py tests/architecture/test_e2e_run_cleanup.py`, re-pointed at the new script. |
| D12 | `noCockpitLinks.test.tsx` | Keep it in D's simplified form. |
| D13 | `index.html` colour scheme | Keep `data-mantine-color-scheme="dark"` (no UX change). |
| D14 | Dev dependencies `reportlab`, `pytest-httpx` | Remove them in Task 8, after a grep proves no kept test uses them. |
| D15 | `pyproject.toml` `"tests/**" = ["ASYNC221"]` | Delete the block (C measured that kept tests pass without it). |
| D16 | `test_auth_allowlist.py` unjudgeable dead-entry check | **Add it** (C, Task 3). |
| D17 | Pass counts | No total is asserted anywhere. Deltas come only from deleted or trimmed files, and each must be accounted for in the run log. |
| D18 | `tests/test_migration_0083_llm_spend_setup_kind.py` | Use B's shared helper `tests/migrations/support.py`, not C's inline rows. |
| D19 | Comments in model files that name `proposal_assist` | Reword them in Task 3, so G1 reaches 0. Find them with `git grep -n proposal_assist -- src/grant_lab/db`; B's open question 7 names `workspaces.py` lines 112-143. Only the comments change. Columns, constraints and indexes stay byte-identical, and Task 4's schema-drift test proves it. |
| D20 | Commit order for the database work | Follow B's open question 1: freeze (Task 2), then remove (Task 3), then park (Task 4). |
| D21 | Schema-drift hollowness | `Base.metadata` is per process, so in a full run another module registers the parked tables even if `db/models/__init__.py` stops importing them. Adopt B's new `tests/test_schema_drift.py` (B, 4b), which adds a fresh-subprocess check. G3's must-reject (B, 4c) is run against that check. |

## File map

- **Deleted:**
  - backend: `src/grant_lab/proposal_assist/`, `src/grant_lab/workspaces.py`, `src/grant_lab/web/http_headers.py`, `src/grant_lab/web/api/events.py`, and `src/grant_lab/web/api/{annotations,workspaces,turns,references,files,rubric,export,citation_audit,settings,admin_retention}.py`
  - tests: `tests/proposal_assist/`, `tests/fencing/`, and the test files C lists as DELETE
  - frontend: the files D 1.2 lists, including `router.tsx` (full), `edition.ts`, `virtual-router.*`, `cockpit/`, `collab/`, `editor/`, the full shell, `Dashboard*`, `CallRow*`, `DashboardHeader*` and `e2e/collab.spec.ts`
  - scripts: `scripts/check_match_bundle.mjs`, `frontend/scripts/*`, and the others D section 3 "Other scripts" lists
- **Moved:**
  - `db/models/{documents,jobs,references,spend,workspaces}.py` → `db/models/workspace/` (B, TASK 1); `CallSectionExtract` and `CallRubric` → new `db/models/workspace/call_extracts.py`
  - `router.simple.tsx` → `router.tsx`
  - `scripts/collab_e2e_*` → `scripts/e2e_*`
  - `test_edition_surface.py` → `test_route_surface.py`
  - `full_edition_routes.txt` → `app_routes.txt`
- **Created:**
  - `db/models/workspace/__init__.py`, `db/models/workspace/call_extracts.py`
  - `frontend/e2e/smoke.spec.ts`
  - `tests/migrations/{__init__,support,test_0066_backfill,test_0068_backfill,test_frozen_migrations_import_no_app_code}.py` (B, TASK 3); `tests/test_migration_0037_backfill.py` is rewritten in place
  - `run-log.md`
- **Edited:** the live backend files in A, the test files in C, the frontend and CI files in D, `config.py`, `scripts/gen_env_example.py`, `scripts/gen_architecture.py`, `.env.example`, `frontend/src/api/schema.gen.ts`, `pyproject.toml`, `uv.lock`, `frontend/package.json`, `frontend/pnpm-lock.yaml`, `README.md`, `frontend/README.md` and `CLAUDE.md`.

---

### Task 0: Worktree and baseline

**Files:**
- Create: `docs/superpowers/plans/2026-11-03-workspace-removal/run-log.md`

- [ ] **Step 1: Create the worktree from master**

```bash
cd /home/parlar_ai/dev/grant-lab && git fetch -q origin && git status --short
git -C /home/parlar_ai/dev/grant-lab worktree add .worktrees/workspace-removal -b chore/workspace-removal master
W=/home/parlar_ai/dev/grant-lab/.worktrees/workspace-removal
cd $W && git rev-parse --short HEAD && git branch --show-current
```

Expected:
- HEAD is master's tip, the commit that added this plan.
- The branch is `chore/workspace-removal`.

Record `BASE=$(git -C $W rev-parse HEAD)` in the run log.

- [ ] **Step 2: Install dependencies**

```bash
cd $W && uv sync --frozen && pnpm -C frontend install --frozen-lockfile
```

Expected: both succeed. A fresh worktree has no `node_modules`.

- [ ] **Step 3: Measure the backend base**

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-base-pytest.txt 2>&1; echo "exit=$?"; tail -3 /tmp/wr-base-pytest.txt
```

Expected: `exit=0`. Record the summary line verbatim (passed, skipped, xfailed). If it is red, stop: the base must be green before anything is removed. If the run hits a testcontainers "connection refused" flake, re-run once and record both runs.

- [ ] **Step 4: Measure the per-file base for the files this plan deletes or trims**

```bash
cd $W && uv run pytest --collect-only -q -p no:cacheprovider tests > /tmp/wr-base-collect.txt 2>&1; echo "exit=$?"
grep -cE '::' /tmp/wr-base-collect.txt
awk -F'::' '/::/{print $1}' /tmp/wr-base-collect.txt | sort | uniq -c | sort -rn > /tmp/wr-base-per-file.txt
wc -l < /tmp/wr-base-per-file.txt
```

Record the total collected count and keep `/tmp/wr-base-per-file.txt`. Task 10 uses it to account for every test that disappears.

- [ ] **Step 5: Measure the frontend base**

```bash
cd $W && pnpm -C frontend exec tsc --noEmit > /tmp/wr-base-tsc.txt 2>&1; echo "tsc exit=$?"
cd $W && pnpm -C frontend run lint > /tmp/wr-base-lint.txt 2>&1; echo "lint exit=$?"
cd $W && pnpm -C frontend run test > /tmp/wr-base-vitest.txt 2>&1; echo "vitest exit=$?"; grep -E "Test Files|Tests " /tmp/wr-base-vitest.txt
cd $W && pnpm -C frontend run e2e:match > /tmp/wr-base-e2e-match.txt 2>&1; echo "e2e:match exit=$?"; tail -5 /tmp/wr-base-e2e-match.txt
```

Expected: all exit 0. Record the vitest and Playwright pass counts. `e2e:match` builds the match bundle into `$W/frontend/dist` and serves it statically. It does not touch the live tree.

- [ ] **Step 6: Record the real-backend browser base from CI**

```bash
ss -ltn | grep -c ':8000 '
cd $W && gh run list --branch master --limit 1 --json headSha,conclusion,databaseId
```

A `1` from the first command means the live app holds the port, so do not run `scripts/collab_e2e_run.sh` locally. Record master's last CI run (sha and conclusion). Its `e2e` job is the real-backend base.

- [ ] **Step 7: Record the inventory baselines that the gates use**

```bash
cd $W && git grep -c proposal_assist -- 'src/*.py' ':!src/grant_lab/proposal_assist' | awk -F: '{s+=$2} END {print s" lines in", NR, "files"}'
cd $W && git ls-files 'src/*.py' | wc -l
cd $W && git ls-files tests/proposal_assist tests/fencing | wc -l
```

Expected (measured at `388494c8`): `192 lines in 29 files`, `311`. Record what you get, whatever it is.

- [ ] **Step 8: Commit the run log**

```bash
cd $W && git add docs/superpowers/plans/2026-11-03-workspace-removal/run-log.md && git commit -q -m "docs(plan): record the workspace-removal baseline measurements

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && git log --oneline -1
```

---

### Task 1: Freeze the workspace on a branch and tag (local)

**Files:** none (git refs only).

- [ ] **Step 1: Create the branch and the annotated tag at BASE**

```bash
cd $W && BASE=$(git rev-parse master) && echo $BASE
git -C $W branch workspace $BASE
git -C $W tag -a workspace-v1 $BASE -m "Proposal workspace frozen before removal from master (2026-11-03). Restore by porting onto current master, not by merging: see docs/superpowers/specs/2026-11-03-workspace-removal-design.md"
```

Use `master`, not `HEAD`: HEAD already carries Task 0's run-log commit. If master moved after Task 0, stop and ask.

- [ ] **Step 2: Gate G6, local half**

```bash
cd $W && git rev-parse workspace && git rev-parse 'workspace-v1^{commit}' && git rev-parse master
```

Expected: three identical shas. Record the sha in the run log. Must-reject check:

```bash
cd $W && git rev-parse 'workspace-v1^{commit}' | grep -qx "$(git rev-parse HEAD)" && echo "WRONG: tag at HEAD" || echo "ok: tag is not at the run-log commit"
```

Expected: `ok: …`.

Nothing is pushed here. The push is Task 11, on the user's word.

---

### Task 2: Make migrations 0037, 0066 and 0068 self-contained

**Files:**
- Modify: `alembic/versions/0037_backfill_reference_original_sha256.py`, `alembic/versions/0066_workspace_current_branch.py`, `alembic/versions/0068_branch_entity.py`
- Create/rewrite: the backfill tests, exactly as **B, TASK 3** gives them: `tests/migrations/*` and the rewritten `tests/test_migration_0037_backfill.py`. In the four places B 3b lists, use `from grant_lab.db.models import …`, because the models have not moved yet (Task 4 switches them).
- Delete: `tests/proposal_assist/test_branch_column_migration.py` and `tests/proposal_assist/test_branch_entity_backfill.py`. Their coverage moves to `tests/migrations/`.
- Modify: `tests/architecture/test_workspaces_dir_isolation.py` (C, Task 3: rewrite)

**Interfaces:**
- Produces: migrations that import nothing from `grant_lab.proposal_assist`. Their behaviour on real rows is unchanged.
- Consumes: nothing. The workspace package still exists in this task, so old and new can be compared.

- [ ] **Step 1: Re-point the backfill tests first (red)**

Apply B, TASK 3. The tests load the frozen copy from the migration file, not from `grant_lab.proposal_assist`. Run them:

```bash
cd $W && uv run pytest -q -p no:cacheprovider tests/migrations tests/test_migration_0037_backfill.py > /tmp/wr-t2-red.txt 2>&1; echo "exit=$?"; tail -5 /tmp/wr-t2-red.txt
```

Expected: FAIL or ERROR, because the frozen copies do not exist yet. Record what failed.

- [ ] **Step 2: Paste the frozen copies**

Apply B, TASK 2 to the three migrations, exactly as given:
- self-contained backfill code inside each file
- 0037's `try/except` around the backfill work only, not the import

- [ ] **Step 3: Run the backfill tests (green)**

Same command as Step 1. Expected: `exit=0`.

- [ ] **Step 4: Gate G2b, must-reject**

Apply each of the six mutations in B, TASK 3, section 3e, one at a time. This includes 0037's wiring mutation: the swallowed `NameError` that only the end-to-end test catches. Run it with pytest only, because ruff would flag the name.
- Copy the file to `/tmp` first, and run with `PYTHONDONTWRITEBYTECODE=1`.
- Run that file's test, record the red, then restore with `cp`.
- Confirm with `cmp /tmp/<migration>.orig <migration>` after restoring. The migration holds this task's uncommitted frozen copy, so `git diff` cannot be used here.

Expected: each mutation turns its test red, and the restored tree is clean. Record each red test name in the run log.

- [ ] **Step 5: Prove no migration imports the workspace package**

```bash
cd $W && git grep -n "grant_lab\.proposal_assist" -- 'alembic/versions/*.py' | grep -vE '^\S+:[0-9]+:\s*(#|""")' ; echo "exit=$?"
```

Expected: no import lines (`exit=1`). Docstring mentions in migration 0030 are allowed.

- [ ] **Step 6: Rewrite `tests/architecture/test_workspaces_dir_isolation.py`**

Use C, Task 3's file content. Then check its control literal against the frozen copies. If B changed how the copies read `WORKSPACES_DIR`, change the literal to match.

```bash
cd $W && grep -n 'os.environ.get("WORKSPACES_DIR", "var/workspaces")' alembic/versions/0037_*.py alembic/versions/0066_*.py alembic/versions/0068_*.py
```

- [ ] **Step 7: Full backend suite**

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-t2.txt 2>&1; echo "exit=$?"; tail -3 /tmp/wr-t2.txt
```

Expected: `exit=0`. The delta from base equals only the moved and new backfill tests. Record it.

- [ ] **Step 8: Commit**

```bash
cd $W && git branch --show-current && git add -A alembic/versions tests docs/superpowers/plans/2026-11-03-workspace-removal/run-log.md && git commit -q -F - <<'EOF'
fix(migrations): freeze the workspace backfills inside migrations 0037, 0066 and 0068

The three migrations imported their backfill code from grant_lab.proposal_assist,
which is about to leave master. Each now carries a self-contained copy, and the
backfill tests exercise those copies on representative rows and files. 0037's
best-effort try/except no longer wraps the import, so a missing module can no
longer pass silently.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git log --oneline -1
```

---

### Task 3: Delete the workspace backend (one atomic commit)

This is the core commit. The deletions and the edits to live code land together, because the live code imports what is being deleted, and the old tests import both.

**Files:**
- Delete, backend: `src/grant_lab/proposal_assist/`, `src/grant_lab/workspaces.py`, `src/grant_lab/web/http_headers.py`, `src/grant_lab/web/api/events.py` (D3), and `src/grant_lab/web/api/{annotations,workspaces,turns,references,files,rubric,export,citation_audit,settings,admin_retention}.py`
- Delete, tests:
  - `tests/proposal_assist/` and `tests/fencing/`
  - every file C's TASK 1 tables (1a and 1b) mark DELETE
  - `tests/test_events_ping.py` (D3)
  - `tests/architecture/{test_branch_entity_invariants,test_match_edition_process_cut,test_spa_edition_match}.py`
- Modify, live backend: A's Task A (A1–A4), Task B, Task C (C1–C3), Task E, applying D1–D6
  - **Not** Task D (settings): that is Task 5. `config.py` keeps its workspace fields for one more commit, but no live code reads them after this task.
- Modify, tests:
  - every TRIM and EDIT in C's TASK 1 and TASK 2
  - in C's TASK 3: `test_auth_allowlist.py` (with D3 and D16), `test_profile_access_single_source.py`, `test_no_import_cycles.py`, `test_boards_permission_single_source.py` and `test_decision_routes_require_a_profile.py`
  - `test_edition_surface.py` → `test_route_surface.py` (C's new file, **without** `test_leftover_edition_settings_change_nothing`, which comes in Task 5)
  - `full_edition_routes.txt` → `app_routes.txt`, regenerated
  - in C's TASK 4: delete `_close_helper_entered_lifespans` from `tests/conftest.py`, and keep and redocument `_workspaces_dir_is_never_the_real_one`
  - `tests/test_api_me.py` (D2) and `tests/test_net_safety.py` (D4)
- Modify: `scripts/gen_architecture.py` (drop `FENCING`, `INVENTORY` and the fence section: C Task 3, `test_architecture_map_is_current`), then regenerate `docs/ARCHITECTURE.md`.
- Modify: every comment under `src/grant_lab/db/` that still says `proposal_assist` (D19; A, Task E "Model files"; B, open question 7), so G1 can reach 0. The files are still at their old paths in this task.
- Modify: `tests/test_migration_0083_llm_spend_setup_kind.py` → use `tests.migrations.support` (D18).

**Interfaces:**
- Produces:
  - `build_scheduler(...)` without `workspace_controller`, `citation_service`, `analysis_embedder` or `app_loop` (A, Task B, final signature)
  - `build_api_router()` with no `edition` argument, mounting `_ROUTERS` (A, C2, with D6)
  - `mount_spa(app, *, spa_mode)` with no `edition` (A, C3)
  - `packaged_edition()` deleted
- Consumes: Task 2's frozen migrations and `tests/migrations/support.py`. The model files are still at their old paths; Task 4 moves them.

- [ ] **Step 1: Delete the backend modules and their tests**

```bash
cd $W && git rm -rq src/grant_lab/proposal_assist src/grant_lab/workspaces.py src/grant_lab/web/http_headers.py src/grant_lab/web/api/events.py \
  src/grant_lab/web/api/annotations.py src/grant_lab/web/api/workspaces.py src/grant_lab/web/api/turns.py src/grant_lab/web/api/references.py \
  src/grant_lab/web/api/files.py src/grant_lab/web/api/rubric.py src/grant_lab/web/api/export.py src/grant_lab/web/api/citation_audit.py \
  src/grant_lab/web/api/settings.py src/grant_lab/web/api/admin_retention.py
cd $W && git rm -rq tests/proposal_assist tests/fencing tests/test_events_ping.py \
  tests/architecture/test_branch_entity_invariants.py tests/architecture/test_match_edition_process_cut.py tests/architecture/test_spa_edition_match.py
```

Then `git rm` each file C marks DELETE in its tables 1a and 1b, one command per file, so every deletion is visible in the shell history. **Do not** delete `tests/test_migration_0037_backfill.py` or the backfill tests Task 2 moved. Their new homes are outside these lists.

- [ ] **Step 2: Edit the live backend**

Apply A's Task A, B, C (C1–C3) and E, in that order, using the decisions D1–D6. Two rules:
- Locate each edit by function or class name. Line numbers have drifted.
- After each file, run `uv run ruff check <that file>` and fix only what the edit caused.

- [ ] **Step 3: Edit the tests**

Apply C as listed under **Files** above. Regenerate the route baseline with C's command, written to `tests/architecture/app_routes.txt`. Then set `_BASELINE_SIZE` to the **measured** count:

```bash
cd $W && wc -l < tests/architecture/app_routes.txt
```

C's figure of 51 is INFERRED; use what you measure. Record it in the run log, and update the history comment above `_BASELINE` with that number.

- [ ] **Step 4: Fix the architecture generator and regenerate the map**

```bash
cd $W && uv run python scripts/gen_architecture.py && uv run pytest -q -p no:cacheprovider tests/architecture/test_architecture_map_is_current.py > /tmp/wr-t4-map.txt 2>&1; echo "exit=$?"
```

Expected: `exit=0`.

- [ ] **Step 5: Gate G1**

```bash
cd $W && git grep -n proposal_assist -- src ; echo "exit=$?"
```

Expected: no output, `exit=1`. The must-reject evidence is Task 0 Step 7's `192 lines in 29 files`.

**Gate G2 must-reject.** With the package now deleted, an unfrozen migration must break a fresh database build:
1. Copy `alembic/versions/0066_workspace_current_branch.py` to `/tmp/0066_workspace_current_branch.py.orig`.
2. Overwrite it with master's version: `git -C $W show master:alembic/versions/0066_workspace_current_branch.py > $W/alembic/versions/0066_workspace_current_branch.py`.
3. Run `cd $W && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider tests/test_schema_drift.py > /tmp/wr-g2-red.txt 2>&1; echo "exit=$?"; grep -m1 -E "ModuleNotFoundError" /tmp/wr-g2-red.txt`.
4. Expected: exit non-zero and `ModuleNotFoundError: No module named 'grant_lab.proposal_assist'`. Record it.
5. Restore with `cp`, then confirm with `cmp /tmp/0066_workspace_current_branch.py.orig alembic/versions/0066_workspace_current_branch.py`.

Also check the import-time links are gone:

```bash
cd $W && git grep -nE "signal_user_revocation|_full_only_routers|drafting_router|packaged_edition|app_loop" -- src ; echo "exit=$?"
```

Expected: `exit=1`.

- [ ] **Step 6: Lint and the full backend suite**

```bash
cd $W && uv run ruff check . && uv run ruff format --check $(git diff --name-only --diff-filter=AM HEAD -- '*.py')
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-t4.txt 2>&1; echo "exit=$?"; tail -3 /tmp/wr-t4.txt
```

Expected:
- `exit=0`.
- 0 skipped and 0 xfailed on this machine (PREDICTED; record what you get).
- Every lost test belongs to a deleted or trimmed file. Check this with Task 0's per-file list:

```bash
cd $W && uv run pytest --collect-only -q -p no:cacheprovider tests > /tmp/wr-t4-collect.txt 2>&1; awk -F'::' '/::/{print $1}' /tmp/wr-t4-collect.txt | sort | uniq -c | sort -rn > /tmp/wr-t4-per-file.txt
diff <(awk '{print $2, $1}' /tmp/wr-base-per-file.txt | sort) <(awk '{print $2, $1}' /tmp/wr-t4-per-file.txt | sort) > /tmp/wr-t4-delta.txt; wc -l < /tmp/wr-t4-delta.txt
```

Every `<` line must be a file C lists as DELETE, TRIM or REWRITE, or a Task 2 move. Every `>` line must be a new or renamed file. Paste the delta file's summary into the run log.

- [ ] **Step 7: Prove the match app still boots and serves the same API**

Every probe below must succeed, and produce a list of well over 20 routes, before any comparison is trusted. `comm` on an empty file prints nothing, which looks like success.

```bash
set -o pipefail
ENVV="DATABASE_URL=postgresql+psycopg://u:p@localhost:5999/nope SESSION_SECRET=test-secret-at-least-16-chars ALLOWED_EMAIL_DOMAINS=umu.se APP_BASE_URL=http://testserver SPA_MODE=disabled EMBEDDING_MODEL=bge-m3"
PROBE="from grant_lab.config import Settings; Settings.model_config['env_file']=None; from grant_lab.main import create_app; print(chr(10).join(sorted({r.path for r in create_app().routes})))"
cd $W && env $ENVV uv run python -c "$PROBE" > /tmp/wr-routes-after.txt; echo "after exit=$?"; wc -l < /tmp/wr-routes-after.txt
cd $W && cmp /tmp/wr-routes-after.txt tests/architecture/app_routes.txt && echo "baseline file matches the app"
git -C $W show master:tests/architecture/full_edition_routes.txt > /tmp/wr-full-routes.txt; wc -l < /tmp/wr-full-routes.txt
git -C /home/parlar_ai/dev/grant-lab worktree add -q /tmp/wr-master-tree master
cd /tmp/wr-master-tree && env $ENVV APP_EDITION=match DRAFTING_ENABLED=false uv run python -c "$PROBE" > /tmp/wr-match-routes-before.txt; echo "before exit=$?"; wc -l < /tmp/wr-match-routes-before.txt
git -C /home/parlar_ai/dev/grant-lab worktree remove --force /tmp/wr-master-tree
```

Expected:
- `after exit=0` and `before exit=0`, and every `wc -l` well above 20. If a probe fails or a file is short, stop: the comparisons below would pass vacuously.
- `baseline file matches the app`.
- Master refuses `APP_EDITION=match` with drafting on, so the before-probe needs `DRAFTING_ENABLED=false`.
- In the throwaway master tree, `uv run` builds its own venv. That is slow but harmless.

Then compare:

```bash
comm -13 <(sort /tmp/wr-full-routes.txt) <(sort /tmp/wr-routes-after.txt)
comm -23 <(sort /tmp/wr-match-routes-before.txt) <(sort /tmp/wr-routes-after.txt)
```

Expected:
- The first command prints nothing: no route is new.
- The second prints nothing, or only `/events/ping` (D3, dev-only, and absent here anyway because `SPA_MODE=disabled`). Any other line is a lost match route: stop.

- [ ] **Step 8: Commit**

```bash
cd $W && git branch --show-current && git add -A && git status --short | head -20 && git commit -q -F - <<'EOF'
refactor!: remove the proposal workspace backend and its tests from master

The workspace (grant_lab.proposal_assist, its web API, scheduler jobs, sign-out
hooks and start-up work) is frozen on the `workspace` branch and tag
`workspace-v1`. Master now serves only the match app: the edition switch in
build_api_router and mount_spa is gone, and the scheduler no longer registers
the ~15 workspace jobs a match process used to run against empty tables.
The session_epoch bump that signs a user out is unchanged on every route.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git log --oneline -1
```

---

### Task 4: Park the workspace-era table definitions

**Files:**
- Move: `src/grant_lab/db/models/{documents,jobs,references,spend,workspaces}.py` → `src/grant_lab/db/models/workspace/` (B, 1b gives the exact `mkdir` and `git mv` commands)
- Create: `src/grant_lab/db/models/workspace/__init__.py` and `src/grant_lab/db/models/workspace/call_extracts.py` (B, 1c)
- Modify: `src/grant_lab/db/models/__init__.py` (B, "New content of …") and `src/grant_lab/db/models/calls.py` (B, 1b/1f: delete the two classes)
- Modify: every import B, 1d lists that points at the old module paths, including the tests for migrations 0040, 0041, 0064, 0067 and 0069. Also the four `from grant_lab.db.models import …` lines Task 2 wrote, if B 1d says to switch them.
- Modify: `scripts/gen_architecture.py` (D9 and B, 1g: read `db/models/workspace/*.py` too, labelled as parked)
- Replace: `tests/test_schema_drift.py` with B, 4b (D21)
- Regenerate: `docs/ARCHITECTURE.md`

- [ ] **Step 1: Move the files and write the package `__init__`**

Run B, TASK 1's `git mv` commands and file contents.

- [ ] **Step 2: Gate G3 must-reject (before the green run)**

Apply each must-reject mutation in B, 4c, one at a time. Copy each file to `/tmp` first. Mutation 1 deletes one parked class. Mutation 2 removes the `workspace` import from `db/models/__init__.py`, which a full run alone would not catch (D21). For each one:

```bash
cd $W && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider tests/test_schema_drift.py > /tmp/wr-g3-red.txt 2>&1; echo "exit=$?"; tail -15 /tmp/wr-g3-red.txt
```

Expected: `exit=1` with the failure B 4c predicts. For mutation 2, also run the full suite once and confirm it goes red there too. Restore with `cp`, then run `git -C $W diff --stat` to confirm only the intended changes remain. Record each red in the run log.

- [ ] **Step 3: Schema drift green**

Same command without the mutation. Expected: `exit=0`.

- [ ] **Step 4: Make the architecture map cover the moved tables**

Apply B, 1g to `scripts/gen_architecture.py` (D9). Then:

```bash
cd $W && uv run python scripts/gen_architecture.py && git diff --stat docs/ARCHITECTURE.md && uv run pytest -q -p no:cacheprovider tests/architecture/test_architecture_map_is_current.py > /tmp/wr-t3-map.txt 2>&1; echo "exit=$?"
```

Expected:
- `exit=0`.
- The map still names every table. The diff shows only the moved paths and the parked labels B 1g adds. Check that no table vanished:

```bash
cd $W && for t in $(uv run python -c "from grant_lab.db.models import Base; print(' '.join(sorted(Base.metadata.tables)))"); do grep -q "$t" docs/ARCHITECTURE.md || echo "MISSING $t"; done; echo checked
```

Expected: no `MISSING` lines. Then read `git diff docs/ARCHITECTURE.md` against B 1g. Any change other than paths and parked labels is a defect.

- [ ] **Step 5: Full backend suite**

Same as Task 2 Step 7. Expected: `exit=0`. The delta from Task 3 is exactly the tests the new `tests/test_schema_drift.py` adds; record it.

- [ ] **Step 6: Commit**

```bash
cd $W && git branch --show-current && git add -A src/grant_lab/db scripts/gen_architecture.py docs/ARCHITECTURE.md tests docs/superpowers/plans/2026-11-03-workspace-removal/run-log.md && git commit -q -F - <<'EOF'
refactor(db): park the 31 workspace-era table definitions in db/models/workspace

No live code reads or writes these tables. Their definitions stay so the ORM
still matches the migrated database (test_schema_drift) and llm_spend's foreign
key to workspaces resolves. They now sit in one marked package instead of
among the live models.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git log --oneline -1
```

---

### Task 5: Remove the workspace and edition settings

**Files:**
- Modify: `src/grant_lab/config.py` (A, Task D: the fields, imports, `_LOCAL_SPEND_TAG`, both validators and the description rewrites)
- Modify: `scripts/gen_env_example.py` (`DAY_ONE`: drop the 13 keys A section 0 lists); regenerate `.env.example`
- Modify: `tests/test_env_example.py`, `tests/test_config.py`, `tests/test_web_filters_model_label.py`; delete `tests/test_config_extract_timeout.py` (all per C)
- Modify: `tests/architecture/test_route_surface.py`: add `test_leftover_edition_settings_change_nothing` (C, Task 3)
- Optional, cosmetic: drop dead `setenv`s of removed keys (C, Task 4 "Cosmetic only")

- [ ] **Step 1: Add the leftover-settings test first (red)**

```bash
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_route_surface.py -k leftover > /tmp/wr-t5-red.txt 2>&1; echo "exit=$?"
```

Expected: FAIL (`app_edition` is still a field).

- [ ] **Step 2: Edit `config.py` and the generator, then regenerate the example**

```bash
cd $W && uv run python scripts/gen_env_example.py && uv run python scripts/gen_architecture.py && git diff --stat .env.example docs/ARCHITECTURE.md
```

Expected:
- `.env.example` loses only the deleted keys and the two rewritten descriptions.
- The map loses only the removed settings. Regenerate it here, before the full suite, or its drift test fails.

- [ ] **Step 3: Prove nothing reads a removed setting**

```bash
cd $W && for f in drafting_enabled app_edition workspaces_dir collab_kill_switch retention_inspection_enabled citation_llm_provider proposal_assist_llm_model reference_ingest_concurrency turn_max_seconds; do git grep -n "settings\.$f\|\.$f\b" -- src tests scripts && echo "STILL READ: $f"; done; echo done
```

Expected: no `STILL READ` lines. `WORKSPACES_DIR` stays as an env var read by the migrations; that is not a `Settings` read.

- [ ] **Step 4: Tests green, then the full suite**

```bash
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_route_surface.py tests/test_env_example.py tests/test_config.py > /tmp/wr-t5-unit.txt 2>&1; echo "exit=$?"
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-t5.txt 2>&1; echo "exit=$?"; tail -3 /tmp/wr-t5.txt
```

Expected: both `exit=0`. Record the delta from Task 4: −3 in `test_config`, −2 for the deleted extract-timeout file, −1 in `test_web_filters_model_label`, +1 for the leftover-settings test. These counts come from C; use the measured numbers.

- [ ] **Step 5: Check that the live `.env` still loads, by key names only**

```bash
cd /home/parlar_ai/dev/grant-lab && grep -oE '^[A-Z_]+=' .env | tr -d '=' | sort > /tmp/wr-live-env-keys.txt; wc -l < /tmp/wr-live-env-keys.txt
cd $W && uv run python -c "
from grant_lab.config import Settings
known={ (f.alias or n).upper() for n,f in Settings.model_fields.items() }
keys=open('/tmp/wr-live-env-keys.txt').read().split()
print('ignored (harmless, extra=ignore):', sorted(k for k in keys if k not in known))"
```

Expected: `APP_EDITION` and `DRAFTING_ENABLED` appear among the ignored keys, which is harmless. No secret value is printed: only names are read.

- [ ] **Step 6: Commit**

```bash
cd $W && git branch --show-current && git add -A && git commit -q -F - <<'EOF'
refactor(config): drop the workspace and edition settings

APP_EDITION, DRAFTING_ENABLED and every proposal-workspace setting are gone from
Settings, .env.example and its generator. Settings ignores unknown keys, so a
deployed .env that still sets them keeps working unchanged.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git log --oneline -1
```

---

### Task 6: Remove the workspace frontend and the edition split

**Files:** everything in D section 1: deletes and moves (1.2), source edits (1.3), test edits (1.4), comment edits (1.5), `package.json` and libraries (1.6). Plus:
- `frontend/routes.json` (D5), regenerated `src/grant_lab/web/spa_routes.py` and `frontend/src/routes.gen.ts`
- `frontend/src/api/schema.gen.ts`, regenerated
- the `_ALWAYS` → `_ROUTERS` comment updates (D6)

These are also in this task, because the deletions and renames above break them otherwise:
- `frontend/playwright.config.ts` (D 2.2). Match mode's webServer must stop calling `build:match` and `check:bundle`.
- `scripts/package_spa.sh` (D section 3 full file, D10) and `tests/architecture/test_package_spa.py` (C Task 3, adapted per D10). The old fixture copies `check_match_bundle.mjs`, which this task deletes.
- `.github/workflows/ci.yml`: D section 3's edits (the bundle gate, the package job, the match-e2e build), plus `frontend/Dockerfile`, `scripts/serve_spa_static.mjs` and the other scripts D section 3 lists. **Not** the `e2e` job (D 2.6), which is Task 7.
- `tests/architecture/test_match_gates_are_wired.py`: delete `test_gate_1_the_bundle_check_runs_in_ci` and `test_the_match_direction_of_the_router_alias_runs_in_ci` (C Task 3).
- `tests/architecture/test_collab_e2e_run_cleanup.py` → `tests/architecture/test_e2e_run_cleanup.py`, re-pointed at `scripts/e2e_run.sh` (D11). D 1.2 renames that script in this task.
- `tests/test_config.py`: the comment that names the seed script.
- `tests/test_secret_file_exclusion.py`: drop the `frontend/.env.example` parameter. D 1.2 deletes that file.

D 1.2 also renames `scripts/collab_e2e_{seed.py,run.sh}` to `scripts/e2e_{seed.py,run.sh}` and deletes `collab.spec.ts` and `dashboard.spec.ts`. Their contents are rewritten in Task 7. Until then the CI `e2e` job is stale. That is harmless: CI only runs on the pull request's tip (Task 10).

- [ ] **Step 1: Delete and move files**

Run D 1.2's `git rm` / `git mv` commands exactly.

- [ ] **Step 2: Source, test and comment edits**

Apply D 1.3, 1.4 and 1.5, including D12 and D13.

- [ ] **Step 2b: Packaging test must-reject**

The fixture copies `$W/scripts/package_spa.sh` itself, so mutate that file:

```bash
cd $W && cp scripts/package_spa.sh /tmp/package_spa.sh.orig
```

Move the `rm -rf "$SPA"` line above the `[ -f "$DIST/index.html" ]` check, then:

```bash
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_package_spa.py -k missing_build > /tmp/wr-t6-pkg-red.txt 2>&1; echo "exit=$?"
cd $W && cp /tmp/package_spa.sh.orig scripts/package_spa.sh && cmp /tmp/package_spa.sh.orig scripts/package_spa.sh && echo restored
```

Expected: `exit=1` (the served tree was deleted on the way to failing), then `restored`.

- [ ] **Step 3: Routes**

Edit `frontend/routes.json` per D5, then:

```bash
cd $W && uv run python scripts/gen_routes.py && uv run pytest -q -p no:cacheprovider tests/test_gen_routes.py > /tmp/wr-t6-routes.txt 2>&1; echo "exit=$?"; git diff --stat src/grant_lab/web/spa_routes.py frontend/src/routes.gen.ts
```

Expected: `exit=0`. In `SPA_ROUTES`, `/cockpit` is gone and everything else is unchanged (compare with A, C4's expected tuple).

- [ ] **Step 4: Libraries**

Run D 1.6's `pnpm -C frontend remove …` command. Then:

```bash
cd $W && git diff -U0 frontend/pnpm-lock.yaml | grep -c '^+[^+]' ; pnpm -C frontend install --frozen-lockfile; echo "frozen exit=$?"
```

Expected:
- The `+` line count is `0`: pure deletions, nothing re-resolved.
- `frozen exit=0`.

If any `+` line appears, stop and report it. The 7-day release-age cooldown could block a re-resolution.

- [ ] **Step 5: Regenerate the OpenAPI types**

```bash
cd $W && bash scripts/gen_openapi_types.sh && git diff --stat frontend/src/api/schema.gen.ts && grep -cE "workspaces|drafting|citation-audit" frontend/src/api/schema.gen.ts
```

Expected:
- The schema shrinks.
- The grep count is `0`. A comment hit is allowed: read each one.
- Also edit `scripts/gen_openapi_types.sh` per D section 3: it no longer passes `APP_EDITION=full`.

- [ ] **Step 6: Frontend gates**

```bash
cd $W && pnpm -C frontend exec tsc --noEmit > /tmp/wr-t6-tsc.txt 2>&1; echo "tsc exit=$?"
cd $W && pnpm -C frontend run lint > /tmp/wr-t6-lint.txt 2>&1; echo "lint exit=$?"
cd $W && pnpm -C frontend run test > /tmp/wr-t6-vitest.txt 2>&1; echo "vitest exit=$?"; grep -E "Test Files|Tests " /tmp/wr-t6-vitest.txt
cd $W && pnpm -C frontend run build > /tmp/wr-t6-build.txt 2>&1; echo "build exit=$?"
cd $W && pnpm -C frontend run e2e:match > /tmp/wr-t6-e2e.txt 2>&1; echo "e2e:match exit=$?"; tail -5 /tmp/wr-t6-e2e.txt
```

Expected:
- All exit 0.
- `e2e:match` passes the same number of tests as Task 0 Step 5.
- `tests/architecture/test_match_gates_are_wired.py` still pins 36 scenarios and 185 `expect()` calls. If D changed `match.spec.ts`, update both in the same commit.
- Record the vitest delta, and account for it by the deleted test files.

- [ ] **Step 7: Prove the match bundle is the same app**

```bash
cd $W && ls frontend/dist/assets/*.js | head; du -b frontend/dist/assets/index-*.js
cd /home/parlar_ai/dev/grant-lab && ls -la src/grant_lab/web/spa/assets/index-*.js
```

Expected: the new main JS chunk is about the size of the live match bundle (780,075 bytes at `dfb6dbae`). Small differences come from renamed modules. A large jump means a workspace module got pulled in: stop and inspect with the bundle's module list.

- [ ] **Step 8: Full backend suite**

Run it anyway: frontend-only changes have broken backend architecture tests before.

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-t6.txt 2>&1; echo "exit=$?"; tail -3 /tmp/wr-t6.txt
```

Expected: `exit=0`.

- [ ] **Step 9: Commit**

```bash
cd $W && git branch --show-current && git add -A && git commit -q -F - <<'EOF'
refactor(frontend)!: make the match SPA the only build

The cockpit, editor and live-editing screens, the full-build shell and router,
the edition switch and the router alias are gone, along with 24 libraries only
they used. router.simple.tsx is now router.tsx; `pnpm build` and `pnpm run
package` replace build:match and package:match. The OpenAPI types and route
lists are regenerated from the backend.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git log --oneline -1
```

---

### Task 7: Real-backend browser smoke (G7)

**Files:**
- Rewrite: `scripts/e2e_seed.py` (D 2.4) and `scripts/e2e_run.sh` (D 2.5). Task 6 renamed both.
- Create: `frontend/e2e/smoke.spec.ts` (D 2.3)
- Modify: `.github/workflows/ci.yml`, the `e2e` job (D 2.6)
- Modify: `tests/architecture/test_match_gates_are_wired.py`: add C's three smoke wiring pins. Fill `SMOKE_SCENARIOS` with the **exact** titles from `smoke.spec.ts`.

- [ ] **Step 1: Write the wiring pins first (red)**

Add C's three tests, then:

```bash
cd $W && uv run pytest -q -p no:cacheprovider tests/architecture/test_match_gates_are_wired.py -k smoke > /tmp/wr-t7-red.txt 2>&1; echo "exit=$?"
```

Expected: FAIL. `smoke.spec.ts` does not exist yet, and the job still runs `collab.spec.ts`.

- [ ] **Step 2: Seed, runner, spec, config and CI job**

Apply D 2.3–2.6.

- [ ] **Step 3: Pins green**

Same command. Expected: `exit=0`.

- [ ] **Step 4: Run the smoke spec against a real backend, if port 8000 is free**

```bash
ss -ltn | grep -c ':8000 '
```

If this prints `1`, the live app holds the port: skip Steps 4 and 5. The smoke's first real run, and its must-reject, are then in CI (Task 10, Steps 5 and 6). Otherwise:

```bash
cd $W && bash scripts/e2e_run.sh > /tmp/wr-t7-smoke.txt 2>&1; echo "exit=$?"; tail -10 /tmp/wr-t7-smoke.txt
```

Expected: `exit=0`, and all 4 smoke tests pass.

- [ ] **Step 5: Gate G7 must-reject (only if Step 4 ran)**

The sign-out test checks that `/api/me` answers 401 afterwards. A server-side mutation that only skips the cookie deletion would leave the `session_epoch` bump in place, so the old cookie would still be revoked and the test would stay green. Mutate the control instead, so it navigates without signing out:
1. `cd $W && cp frontend/src/shell/SimpleShell.tsx /tmp/SimpleShell.tsx.orig`.
2. In `signOut`, replace `logout.mutate(undefined, { onSuccess: () => window.location.assign("/login") })` with `window.location.assign("/login")`. Match the call even if it spans several lines.
3. Re-run Step 4's command. Expected red: "signing out through the account menu lands on /login and ends the session", at `expect(me.status()).toBe(401)`, which gets 200.
4. Restore with `cp`, then run `cmp /tmp/SimpleShell.tsx.orig frontend/src/shell/SimpleShell.tsx`.

- [ ] **Step 6: Full backend suite, then commit**

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-t7.txt 2>&1; echo "exit=$?"; tail -3 /tmp/wr-t7.txt
cd $W && git branch --show-current && git add -A && git commit -q -F - <<'EOF'
test(e2e): replace the full-build browser runs with a real-backend match smoke

match.spec.ts mocks the API in the page, so dashboard.spec.ts was the only
browser check against a real FastAPI and Postgres. Its four checks (page loads
without critical a11y violations, unknown paths get a JSON 404, logout lands on
/login, call pages stay in the SPA) now run against the match screens, seeded
with a profile, in CI's e2e job.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git log --oneline -1
```

---

### Task 8: Python libraries (G5)

**Files:**
- Modify: `pyproject.toml`
  - Remove the 11 runtime dependencies: `claude-agent-sdk`, `markdown`, `python-docx`, `prosemirror`, `pycrdt`, `bleach`, `pillow`, `pdfminer.six`, `striprtf`, `openpyxl`, `defusedxml`.
  - Remove the `mcp` entry in `constraint-dependencies`.
  - Remove dev deps `reportlab` and `pytest-httpx` (D14).
  - Remove the `"tests/**" = ["ASYNC221"]` block (D15).
- Modify: `uv.lock`

- [ ] **Step 1: Prove nothing kept imports a library being removed**

```bash
cd $W && for m in claude_agent_sdk markdown docx prosemirror pycrdt bleach PIL pdfminer striprtf openpyxl defusedxml reportlab pytest_httpx; do git grep -nE "^\s*(import|from) $m\b" -- src tests scripts alembic && echo "STILL IMPORTED: $m"; done; git grep -n "httpx_mock" -- tests && echo "STILL USED: pytest-httpx fixture"; echo done
```

Expected: no `STILL` lines.

- [ ] **Step 2: Remove them and re-lock**

```bash
cd $W && uv remove claude-agent-sdk markdown python-docx prosemirror pycrdt bleach pillow pdfminer.six striprtf openpyxl defusedxml && uv remove --dev reportlab pytest-httpx
```

Then delete the `mcp` constraint line and the ASYNC221 block by hand, and run `uv lock`. Check what changed:

```bash
cd $W && git diff --stat pyproject.toml uv.lock && uv sync --frozen && uv run python -c "import grant_lab.main" && echo import-ok
cd $W && uv run ruff check . ; echo "ruff exit=$?"
```

- [ ] **Step 3: Full suite, then commit**

```bash
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-t8.txt 2>&1; echo "exit=$?"; tail -3 /tmp/wr-t8.txt
cd $W && git branch --show-current && git add -A && git commit -q -F - <<'EOF'
chore(deps): drop the workspace-only Python libraries

Removes claude-agent-sdk, pycrdt, python-docx, prosemirror, markdown, bleach,
pillow, pdfminer.six, striprtf, openpyxl and defusedxml, the mcp pin that
existed for the SDK, and the test-only reportlab and pytest-httpx. Nothing kept
imports any of them.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git log --oneline -1
```

- [ ] **Step 4: Gate G5, fresh install on a scratch tree**

```bash
git -C $W worktree add -q /tmp/wr-g5 HEAD && cd /tmp/wr-g5 && uv sync --frozen && pnpm -C frontend install --frozen-lockfile && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-g5.txt 2>&1; echo "exit=$?"; tail -3 /tmp/wr-g5.txt
cd /tmp/wr-g5 && pnpm -C frontend run build > /tmp/wr-g5-build.txt 2>&1; echo "build exit=$?"
```

Expected: `exit=0` and `build exit=0`. The real-boot half of G5 (lifespan and scheduler on a throwaway database) is the smoke run. That runs here only if port 8000 is free (Task 7 Step 4's check), and otherwise in CI (Task 10).

Must-reject:
1. In `/tmp/wr-g5`, add `import pycrdt` to the top of `src/grant_lab/web/filters.py`.
2. Run `cd /tmp/wr-g5 && uv run python -c "import grant_lab.main"`. Expected: `ModuleNotFoundError: No module named 'pycrdt'`. Record it.
3. Remove the scratch tree: `git -C $W worktree remove --force /tmp/wr-g5`.

---

### Task 9: Operational docs and spec errata

**Files:**
- Modify: `README.md` and `frontend/README.md` (D section 4, exact replacements)
- Modify: `CLAUDE.md` (D section 4)
  - Includes the skip note: "**0 skipped and 0 xfailed** is the expectation on a machine with Docker; the Docker-gated `tests/test_secret_file_exclusion.py` is the only skip marker."
  - Use the numbers Task 4 Step 6 measured, not this sentence, if they differ.
- Regenerate: `docs/ARCHITECTURE.md`, if anything changed since Task 4

- [ ] **Step 1: Apply the edits, then prove no stale command remains**

```bash
cd $W && git grep -nE "build:match|package:match|check:bundle|VITE_APP_EDITION|APP_EDITION|DRAFTING_ENABLED|collab_e2e|router\.simple" -- README.md frontend/README.md CLAUDE.md scripts .github frontend/package.json ; echo "exit=$?"
```

Expected: `exit=1`. `e2e:match` is allowed: it keeps its name.

- [ ] **Step 2: Commit**

```bash
cd $W && git branch --show-current && git add -A && git commit -q -F - <<'EOF'
docs: describe the single match build in the README, frontend README and CLAUDE.md

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
git log --oneline -1
```

---

### Task 10: Final verification

- [ ] **Step 1: All gates on the branch tip**

```bash
cd $W && git grep -n proposal_assist -- src ; echo "G1 exit=$? (want 1)"
cd $W && uv run ruff check . ; echo "ruff exit=$?"
cd $W && uv run pytest -n 16 --dist loadfile -q -p no:cacheprovider > /tmp/wr-final.txt 2>&1; echo "pytest exit=$?"; tail -3 /tmp/wr-final.txt
cd $W && pnpm -C frontend exec tsc --noEmit; echo "tsc exit=$?"; pnpm -C frontend run lint; echo "lint exit=$?"; pnpm -C frontend run test > /tmp/wr-final-vitest.txt 2>&1; echo "vitest exit=$?"
cd $W && pnpm -C frontend run e2e:match > /tmp/wr-final-e2e.txt 2>&1; echo "e2e:match exit=$?"
cd $W && bash scripts/gen_openapi_types.sh && git diff --exit-code frontend/src/api/schema.gen.ts; echo "openapi drift exit=$?"
cd $W && uv run python scripts/gen_architecture.py && git diff --exit-code docs/ARCHITECTURE.md; echo "map drift exit=$?"
```

Expected: G1 exit 1; every other exit 0. Record every line in the run log.

- [ ] **Step 2: Account for every test that disappeared**

Compare `/tmp/wr-base-per-file.txt` with a fresh per-file collection, as in Task 4 Step 6. Every removed test must come from a file C or D lists. Any other loss is a defect: stop.

- [ ] **Step 3: Size check against the spec**

```bash
cd $W && git ls-files 'src/*.py' | xargs cat | wc -l; git ls-files 'tests/*.py' | xargs cat | wc -l; git diff --stat master...HEAD | tail -1
```

Record the numbers in the run log for the programme's piece-1 entry.

- [ ] **Step 4: Request a code review**

Use superpowers:requesting-code-review on `master...HEAD`. Name the risk areas:
- the frozen migrations
- the sign-out routes
- `main.py` and `scheduler.py`
- the route surface
- the CI wiring

Fix what is confirmed, re-run Step 1, and commit the fixes with titles that name each fix.

- [ ] **Step 5: Open a draft pull request so CI runs the branch (on the user's word)**

CI runs only on pushes to master and on pull requests. Pushing the branch is an outward action, so ask the user first, naming both this step and Step 6. On OK:

```bash
cd $W && git push -u origin chore/workspace-removal
cd $W && gh pr create --draft --base master --head chore/workspace-removal --title "Remove the proposal workspace and the edition split (piece 1)" --body $'Piece 1 of the match clean-up. Spec: docs/superpowers/specs/2026-11-03-workspace-removal-design.md. Plan and run log: docs/superpowers/plans/2026-11-03-workspace-removal*.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)'
cd $W && gh pr checks --watch
```

Expected: every job green. That includes `e2e`, which is the real-backend smoke (G7 positive) and a real boot on a throwaway database (G5's boot half), and the backend and frontend jobs on a fresh runner.

- [ ] **Step 6: G7 must-reject in CI (if Task 7 Step 5 could not run locally)**

On the same OK:
1. Create `wr-g7-mutant` from the branch tip, and apply Task 7 Step 5's `SimpleShell.tsx` mutation.
2. Commit it, push the branch, and open a second draft PR from it.
3. Watch its `e2e` job. It must fail on the sign-out test.
4. Record the failing run's URL in the run log.
5. Clean up: `gh pr close <n> --delete-branch` and `git -C $W branch -D wr-g7-mutant`.

The mutant never touches `chore/workspace-removal`.

- [ ] **Step 7: Commit the run log**

```bash
cd $W && git add docs/superpowers/plans/2026-11-03-workspace-removal/run-log.md && git commit -q -m "docs(plan): record the workspace-removal verification

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" && git log --oneline -1 && git push
```

Expected: the PR's CI re-runs on the run-log commit and stays green.

---

### Task 11: Push the frozen branch, then deploy and merge (each on the user's word)

- [ ] **Step 1: Ask the user.** Show them:
  - the `workspace` sha
  - `git log --oneline master..chore/workspace-removal`
  - the green PR checks
  - the Task 10 results

  Explain that deploying means a few minutes of downtime.

- [ ] **Step 2: On OK, push the frozen branch and tag, then gate G6 remote**

```bash
cd $W && git push origin workspace workspace-v1
SHA=$(git -C $W rev-parse workspace)
git -C $W ls-remote origin refs/heads/workspace | cut -f1
git -C $W ls-remote origin 'refs/tags/workspace-v1^{}' | cut -f1
```

Expected: both print `$SHA`.

- [ ] **Step 3: Deploy: stop, merge, rebuild, start (on the user's word)**

The live app runs from the main checkout. The running process imports workspace modules lazily (the sign-out routes, the scheduler ticks), so deleting those files under it would make sign-out answer 500. So stop it before the merge touches the tree:

```bash
~/grant-lab-backups/backup.sh
systemctl --user stop grant-lab-app
cd /home/parlar_ai/dev/grant-lab && git status --short && git merge --ff-only chore/workspace-removal
cd /home/parlar_ai/dev/grant-lab && for d in src/grant_lab/proposal_assist tests/proposal_assist tests/fencing; do [ -z "$(git ls-files "$d")" ] && rm -rf "$d"; done; ls -d src/grant_lab/proposal_assist 2>&1   # git rm leaves gitignored __pycache__ behind (an importable ghost package); expect "No such file or directory"
cd /home/parlar_ai/dev/grant-lab && uv sync --frozen && pnpm -C frontend install --frozen-lockfile && pnpm -C frontend run build && pnpm -C frontend run package
systemctl --user start grant-lab-app && sleep 5 && curl -s http://127.0.0.1:8000/healthz
```

Expected: `{"status":"ok","scheduler_lock":true}`.
- If `--ff-only` fails because master moved, start the app again unchanged, and ask.
- Then, by hand: sign in, and go through the feed, a call page, save/hide, profiles, boards and admin.

- [ ] **Step 4: Push master and watch CI**

```bash
cd /home/parlar_ai/dev/grant-lab && git push origin master && gh run list --branch master --limit 1
```

Use `gh run watch <id>` and expect green. The PR shows as merged, because master now contains its commits.

- [ ] **Step 5: Rollback, if the hand check fails (code only, no database change)**

1. `systemctl --user stop grant-lab-app`.
2. Check out the previous master commit (the `workspace` sha).
3. Run `uv sync --frozen && pnpm -C frontend install --frozen-lockfile`.
4. Rebuild with that commit's own names: `pnpm -C frontend build:match && pnpm -C frontend package:match`.
5. `systemctl --user start grant-lab-app`, and check `/healthz`.

- [ ] **Step 6: Update memory and clean up**

- In memory, the restart recipe changes: `build:match`/`package:match` become `run build`/`run package`. Record that piece 1 is done.
- Run `git -C /home/parlar_ai/dev/grant-lab worktree remove .worktrees/workspace-removal`.
