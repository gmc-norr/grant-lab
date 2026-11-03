# Piece 1: move the proposal workspace out of master — design

Date: 2026-11-03. Part of `2026-11-03-match-cleanup-programme-design.md`.
All numbers measured at `dfb6dbae` unless labelled otherwise. The plan re-measures
them on its own base before acting.

## Why

The live app runs the match edition (`APP_EDITION=match`, `DRAFTING_ENABLED=false`).
The proposal workspace is switched off, but it is over half of the Python source and
over half of the tests. Every change to shared code must keep it working, and it keeps
a two-build setup alive (match and full).

The user wants the workspace back in a future version, months away. Its design has
open problems already recorded: data loss in the markdown bridge, and the same
document kept in two stores. So it will be reworked before it returns anyway.

## 1. Freeze it on a branch

- Create the branch `workspace` and the annotated tag `workspace-v1` at the master
  commit just before the removal's first commit. The plan records that sha.
- Push both to `origin` before the removal is merged, after the user's OK.
- Do not develop on the branch. To restore the workspace later, port it onto the
  then-current master. `git merge workspace` would not bring it back: files that
  master deleted and the branch never touched would stay deleted.

## 2. What leaves master

### Python

- `src/grant_lab/proposal_assist/` (38,414 lines).
- `src/grant_lab/workspaces.py`.
- The web API modules that only `_full_only_routers()` in `web/api/__init__.py`
  registers: `annotations`, `workspaces`, `turns`, `references`, `files`, `rubric`,
  `export`, `citation_audit`, `settings`, `admin_retention`. Also
  `admin.drafting_router` and `_drafting_panel_context` in `web/api/admin.py`.
- The workspace parts of shared code. Outside the package, 29 files contain 192 lines
  that name `proposal_assist`
  (`git grep -c proposal_assist -- 'src/*.py' ':!src/grant_lab/proposal_assist'`).
  `main.py` has 27 such lines and `scheduler.py` has 22. Known items:
  - **Five calls to `signal_user_revocation`**: `admin_users.py` ×3, `auth.py` ×1,
    `me.py` ×1. They close live-editing websockets, and the match edition has none.
    The `session_epoch` bump in those same routes stays: that bump is what signs a
    user out. The hook's own docstring says "the DB epoch bump already invalidated
    future auth".
  - The start-up reset of `Reference.text_status == "running"` in `main.py`.
  - The workspace jobs in `scheduler.py`.
  - The workspace settings in `config.py`. Deleting them is safe for the live
    `.env`: `Settings` uses `extra="ignore"`, so its leftover lines are ignored.
  - `phase_label` in `web/filters.py`.

### The two-build setup

- **Python:** the edition checks. 14 files outside the package mention the edition
  (`grep -rlE "app_edition|APP_EDITION|\bedition\b" src --include=*.py`, minus
  `proposal_assist`).
- **Frontend:**
  - `router.tsx` (the full build's router) goes, and `router.simple.tsx` becomes the
    only router.
  - `edition.ts`, the router alias and the edition switch in `vite.config.ts` go.
  - `build:match` and `package:match` become `build` and `package`.
  - `scripts/check_match_bundle.mjs` goes.
- **Full-build routes:** `cockpit`, `cockpit/:id`, `cockpit/new`,
  `deleted-workspaces`, `deleted-workspaces/:workspaceId` and `drafting` are removed.
  So is the full build's home page, `Dashboard.tsx` (with `CallRow` and
  `DashboardHeader`). The match feed does not change.
- **Workspace-only frontend:** 77 non-test files with 9,050 lines, plus 12,389 lines
  of their tests:
  - `src/cockpit/`, `src/collab/` and `src/editor/`
  - `routes/Cockpit*`, `routes/Workspace*`, `routes/AdminDrafting*` and
    `routes/AdminDeletedWorkspace*`
  - `shell/{ActivityBar,AppShell,Sidebar,StatusBar,TitleBar,nav-items}`

  `src/api/` also holds workspace clients. The plan removes the ones that no kept
  file imports.

### Tests and CI

- `tests/proposal_assist/` (80,031 lines).
- 54 test files outside it import `grant_lab.proposal_assist`. Each is either deleted
  or trimmed to its match part.
- 14 test files mention the edition.
- 12 of the 17 architecture tests mention the edition, `proposal_assist` or the
  workspace: `test_auth_allowlist`, `test_boards_permission_single_source`,
  `test_branch_entity_invariants`, `test_decision_routes_require_a_profile`,
  `test_edition_surface`, `test_match_edition_process_cut`,
  `test_match_gates_are_wired`, `test_no_import_cycles`, `test_package_spa`,
  `test_profile_access_single_source`, `test_spa_edition_match`,
  `test_workspaces_dir_isolation`. The plan reads each one and decides whether to
  delete it or trim it. A gate that guards a live rule stays.
- Browser tests:
  - `collab.spec.ts` goes, because live editing goes.
  - `match.spec.ts` and the CI `match-e2e` job stay. But that spec mocks the API
    inside the page (`E2E_MODE=match` in `frontend/playwright.config.ts`), so it
    cannot replace the real-backend checks.
  - `dashboard.spec.ts` targets the full build's home page, but its four checks are
    the only browser checks that hit the real backend. They are rewritten against the
    match screens as a small real-backend smoke spec:
    - `/` loads with no critical a11y violations
    - an unknown path gets FastAPI's JSON 404, not `index.html`
    - logout through the UI lands on `/login`
    - opening a call detail page stays in the SPA
  - The CI `e2e` job keeps its seeded user and runs that smoke spec in place of
    `collab.spec.ts` and `dashboard.spec.ts`. The plan reads its seed step and drops
    whatever that step seeds only for collab.
- CI: the match bundle-gate steps in the `frontend` job go. The `package` job's
  full-edition build becomes the only build.

### Libraries

- **Python.** These have no imports outside the workspace in `src/`, `alembic/` or
  `scripts/`: `claude-agent-sdk`, `markdown`, `python-docx`, `prosemirror`, `pycrdt`,
  `bleach`, `pillow`, `pdfminer.six`, `striprtf`, `openpyxl`, `defusedxml`. The `mcp`
  entry in `constraint-dependencies` exists only for `claude-agent-sdk`, so it goes
  too. The plan re-checks `tests/` before removing each one.
- **JS runtime.** These 18 are imported only by workspace files:
  - `@codemirror/{commands,lang-markdown,language,state,view}` and `@lezer/highlight`
  - `@tiptap/{core,extension-collaboration,extension-collaboration-caret,extension-table,pm,react}`
  - `@uiw/codemirror-theme-vscode`, `lib0`, `react-resizable-panels`
  - `y-prosemirror`, `y-protocols`, `yjs`
- **JS dev.** `ws`, `@types/ws`, `markdown-it` and `prosemirror-*`: the plan checks
  them against the remaining tests and scripts.

## 3. What stays

- **All 48 tables and their data. There is no migration.** The live database
  currently holds 11 workspaces and 211 chat turns, and both stay.
- **The definitions of the 31 workspace-era tables** move, unchanged, into one
  folder: `src/grant_lab/db/models/workspace/`.
  - It holds the files `documents.py`, `jobs.py`, `references.py`, `spend.py` and
    `workspaces.py`, plus `CallSectionExtract` and `CallRubric` from `calls.py`.
  - Its `__init__.py` says that no live code reads or writes these tables.
  - Why keep the definitions: `tests/test_schema_drift.py` compares the ORM with the
    migrated database (`compare_metadata`), and `llm_spend` has a foreign key to
    `workspaces`.
  - Why a folder, not one file: together they are 1,734 lines.
  - No live table links to them; checked by grepping the live model files for
    relationships and foreign keys.
- **The 17 live tables keep their definitions where they are:**
  - `users`, `sources`, `calls`
  - `profiles`, `profile_viewers`, `subscriptions`, `profile_bootstrap_jobs`
  - `call_decisions`, `call_rankings`, `ranking_jobs`, `digest_runs`,
    `call_ranking_criteria`, `ranking_remote_attempts`, `golden_set_triples`
  - `boards`, `board_items`, `board_suppressions`
- **All 85 migrations.**
  - Migrations `0037`, `0066` and `0068` import `grant_lab.proposal_assist` inside
    their upgrade code. Each gets a frozen copy of the code it calls, placed in the
    migration file itself, so no import can fail.
  - `0037` wraps its import *and* its backfill in `try/except Exception` and only
    logs a warning. So today a missing import would be swallowed, and G2 alone
    would pass for it.
    - With the frozen copy inside the file, the import cannot fail.
    - The `try/except` stays around the backfill work only: that is its original,
      best-effort purpose.
  - Their backfill tests are kept and re-pointed at the frozen copies.
    - `tests/test_migration_0037_backfill.py` stays where it is.
    - The tests for `0066` and `0068` move out of the deleted folder. Going by their
      names, they are `tests/proposal_assist/test_branch_column_migration.py` and
      `tests/proposal_assist/test_branch_entity_backfill.py`; the plan confirms
      this by reading them.
    - Each one must exercise representative rows and temporary workspace files, not
      just an empty database.
  - Migrations `0077` and `0082` import live code (`boards`, `profiles`) and are left
    as they are.
- **Libraries the live app uses:** `pymupdf`, `trafilatura`, `anthropic`, `segno`,
  `joserfc`, `jinja2`; and on the JS side `@fontsource/noto-sans` (the board fonts,
  via `url()` in `BoardScreen.fonts.css`), `@vscode/codicons` and `ics`.
- **Files on disk** under the data directory, and the live `.env`. Nobody edits it.
- **Docs.** The broader documentation rewrite belongs to piece 5. But piece 1 does
  update the operational instructions and the generated files, because the commands
  they name change:
  - **`README.md`.** Its "Choose the edition first" section and its build commands
    (`pnpm -C frontend build:match` / `package:match`, and the Docker
    `--build-arg VITE_APP_EDITION=match`).
  - **`.env.example`.** It is regenerated by `scripts/gen_env_example.py` once the
    workspace settings go; `tests/test_env_example.py` checks it.
  - **`frontend/src/api/schema.gen.ts`.** The OpenAPI types are regenerated by
    `scripts/gen_openapi_types.sh` once the workspace routes go.
  - **Build and test scripts that know about the edition:**
    `scripts/package_spa.sh`, `scripts/serve_spa_static.mjs`,
    `scripts/gen_openapi_types.sh`, `scripts/collab_e2e_run.sh` (goes with collab)
    and `frontend/Dockerfile`.
  - **`CLAUDE.md` and the generated `docs/ARCHITECTURE.md`.**
  - In `CLAUDE.md`, the "`1 skipped` is the expectation" note must change. That skip
    lives in `tests/proposal_assist/`. The only other skip marker
    (`tests/test_secret_file_exclusion.py`) needs a Docker daemon, which this machine
    has. `PREDICTED (not run): 0 skipped after the removal on this machine.`
  - `scripts/gen_architecture.py` has 2 lines that mention the workspace or edition.

## 4. Gates

Each gate lists what it must reject, as well as what it must pass.

| # | Gate | Must pass | Must reject |
|---|---|---|---|
| G1 | `git grep -n proposal_assist -- src` returns nothing | after the removal | `dfb6dbae`: 192 lines in 29 files outside the package, plus the package. Measured. |
| G2 | The suite's `migrated_engine` fixture runs `alembic upgrade head` on an empty database (`tests/conftest.py`) | after the migrations get their frozen copies | the package deleted, migrations unchanged: `ModuleNotFoundError` from `0066` and `0068`. `PREDICTED (not run)`; the plan runs it. G2 cannot reject a broken `0037`, which swallows the error. That is G2b's job. |
| G2b | The three backfill tests pass against the frozen copies, using representative rows and temporary workspace files | after the re-pointing | a frozen copy that is broken (for example, one that writes the wrong hash or skips a row): the test goes red. `PREDICTED (not run)`; the plan breaks each copy once and records the red. |
| G3 | `tests/test_schema_drift.py` passes | with the definitions moved into `db/models/workspace/` | one workspace definition deleted: a `remove_table` diff. `PREDICTED (not run)`; the plan runs it. |
| G4 | Full backend suite, `vitest`, `oxlint`, `tsc` and `pnpm run e2e:match` pass | after the removal | no total is asserted here. The plan measures its base, and its deltas come only from deleted or trimmed files. |
| G5 | A fresh `uv sync` and `pnpm install --frozen-lockfile` without the removed libraries, then the app imports and boots on a throwaway database | after the removal | a kept file still importing a removed library. The plan proves this on a scratch tree. |
| G6 | These all equal the recorded sha: `git rev-parse workspace`, `git rev-parse workspace-v1^{commit}`, `git ls-remote origin refs/heads/workspace`, and the peeled remote tag `git ls-remote origin 'refs/tags/workspace-v1^{}'` | after the push | a branch or tag made from the wrong commit, or a tag that was never pushed. |
| G7 | The real-backend smoke spec passes in the CI `e2e` job | after the rewrite | logout broken on purpose: the spec goes red. `PREDICTED (not run)`; the plan breaks it once and records the red. |

G1 also catches imports inside functions that the tests never run. Those imports
fail only when that function runs, so a green suite alone would not catch them.

## 5. Rollout

- Work on a branch in `.worktrees/workspace-removal`.
- Merge when the user says so. Then rebuild the UI with the new script names and
  restart with `systemctl --user restart grant-lab-app`, again on the user's word.
  Update the restart recipe in memory to match.
- Rollback is code-only, because there is no database change. In the main tree,
  in this order:
  1. Check out the previous master commit.
  2. Restore its dependencies from its own lockfiles: `uv sync --frozen`, then
     `pnpm -C frontend install --frozen-lockfile`. The removal deletes libraries,
     so the current environment would not match the old code.
  3. Rebuild its UI with its own commands: `pnpm -C frontend build:match &&
     pnpm -C frontend package:match`.
  4. Restart with `systemctl --user restart grant-lab-app`.

## 6. Risks

- **A shared code path that only the full build exercised** keeps dead branches.
  Covered by G1 and by reading each of the 29 files.
- **A removed library is still imported inside a function somewhere.** Covered by
  grepping each library in G5, and by the suite.
- **The test total drops sharply.** That is expected. The plan carries deltas, not
  totals.
- **The `workspace` branch ages.** Accepted: restoring it is a port, not a merge.
