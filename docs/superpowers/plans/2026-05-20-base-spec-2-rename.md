# Base Spec B2 — Rename `call-monitor` → `call-manager` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the Python package `call_monitor` → `call_manager` (underscored, used in imports) and the project name `call-monitor` → `call-manager` (hyphenated, used in pyproject + docs + env vars + DB user/password/db name). Pure refactor. CI + tests green against the renamed package. A separate deployment-time SQL script handles `ALTER DATABASE call_monitor RENAME TO call_manager`.

**Architecture:** No new architecture. This is mechanical bulk renaming. Risk is (a) missing a reference, (b) sed false positives. Mitigation: a final grep that confirms zero remaining `call_monitor` / `call-monitor` strings outside generated files.

**Tech Stack:** `git mv` for directory rename (preserves history); `sed -i` for bulk in-place replace; `uv sync` to re-register the package after pyproject rename; existing pytest + testcontainers suite to verify nothing broken.

**Spec:** [docs/superpowers/specs/2026-04-18-call-manager-proposal-assist-design.md](../specs/2026-04-18-call-manager-proposal-assist-design.md), §Rename `call-monitor` → `call-manager` (lines 16–17) + §Deployment / Rename cutover (lines 375–409).

**Overview:** [docs/superpowers/plans/2026-05-19-base-spec-overview.md](2026-05-19-base-spec-overview.md), §B2.

---

## Prerequisites and scope honesty

**Touchpoint surface (verified via grep — 2026-05-20 snapshot):**
- 191 files mention `call_monitor` or `call-monitor` (excluding `.venv`, `.worktrees`, `.git`, `docs/`).
- 58 of those are under `src/`; 122 are under `tests/`. Remainder: `pyproject.toml`, `.env.example`, `docker-compose.yml`, `alembic/env.py`, `README.md`, `CLAUDE.md`, `scripts/seed_dev_data.py`.
- Docs under `docs/` deliberately retain the historical `call-monitor` references (they record decisions made when the project had that name) — exclude from the bulk rename.

**Two namespaces to rename:**
- `call_monitor` (underscored, Python package + DB user/password/db name)
- `call-monitor` (hyphenated, project name + docker volume + URL slugs in docs)

**What B2 does NOT do:**
- A compatibility shim (`call_monitor` alias package that re-exports `call_manager`). The overview labels this "if needed" — for this internal hospital tool there are no external consumers; skip.
- Migrate live production data. The deployment-time `ALTER DATABASE` script is documented but not automated; it requires manual approval + a brief downtime window.
- Any behavior changes. This is pure rename.

---

## Conventions reference

- `git mv` for the directory rename so git tracks it as a rename (clean blame).
- Bulk sed via `find … -exec sed -i 's/call_monitor/call_manager/g' {} +` (with the correct file-type filter).
- The two namespaces (`call_monitor` underscored, `call-monitor` hyphenated) are renamed in separate passes so there's no risk of double-substitution.
- After every sed pass, run `git diff --stat` to confirm the change set looks sane (no surprise files touched).
- Final verification: a grep that returns zero `call_monitor` / `call-monitor` matches outside `docs/`, `.git/`, `.venv/`, `.worktrees/`.

---

## File map

**Renamed (directory move):**
- `src/call_monitor/` → `src/call_manager/`

**Modified (string replacement):**
- All `.py` files in `src/` + `tests/` (~180 files) — `from call_monitor.X` → `from call_manager.X`.
- `pyproject.toml` — `name = "call-monitor"` → `"call-manager"`, `packages = ["src/call_monitor"]` → `["src/call_manager"]`.
- `.env.example` — `DATABASE_URL` user/password/db all `call_monitor` → `call_manager`.
- `docker-compose.yml` — `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` + `call_monitor_pgdata` volume.
- `alembic/env.py` — `from call_monitor.db.base import Base` → `from call_manager.db.base import Base`.
- `README.md`, `CLAUDE.md` — project name + file path references.
- `scripts/seed_dev_data.py` — imports.

**Created:**
- `scripts/cutover_rename_database.sh` — deployment-time helper that does the `ALTER DATABASE` + `ALTER USER … RENAME TO` in a single transaction. Documents the dependency order (stop app → rename DB → restart app with new env).

---

## Revision history

- **rev1 (2026-05-20)** — initial plan.

---

## Task 1: discovery + sed plan validation

Confirm the touchpoint list is complete and the sed strategy won't double-substitute. No code changes — read-only verification.

**Files:**
- None modified. Output is a freshly-generated discovery doc (saved as the task's commit message body, NOT a tracked file).

- [ ] **Step 1: Enumerate every file touching either namespace**

```bash
# Both namespaces, exclude dirs we deliberately don't touch.
EXCL='--exclude-dir=.venv --exclude-dir=.worktrees --exclude-dir=.git --exclude-dir=docs --exclude-dir=__pycache__'
grep -rln 'call_monitor\|call-monitor' $EXCL > /tmp/b2_touchpoints.txt
wc -l /tmp/b2_touchpoints.txt
```

Expected count: ~190 files (snapshot was 191 on 2026-05-20). If the count is dramatically different, surface — a new dependency may have introduced references.

- [ ] **Step 2: Bucket the files**

```bash
# Python files (will get `from call_monitor.X` → `from call_manager.X`).
grep -rln 'call_monitor' $EXCL --include='*.py' | wc -l

# Hyphenated namespace (project name + docker + docs).
grep -rln 'call-monitor' $EXCL --include='*.toml' --include='*.yml' --include='*.yaml' --include='*.md' --include='Dockerfile*' --include='*.env*' | wc -l

# Underscore namespace in non-Python files (DB user/password/db, env vars).
grep -rln 'call_monitor' $EXCL --include='*.toml' --include='*.yml' --include='*.yaml' --include='*.md' --include='Dockerfile*' --include='*.env*' | wc -l
```

Record the three counts. They become the verification budget for Task 2's sed passes.

- [ ] **Step 3: Identify any unsafe matches**

```bash
# Any string match in a comment / docstring that would be incorrect to rename?
grep -rn 'call_monitor\|call-monitor' $EXCL --include='*.py' | grep -E '^\s*#|"""' | head -30
```

Look for things like "the old call-monitor project" or "renamed from call_monitor" that should stay as historical references. If found, list them — Task 2 needs to skip those lines.

For this snapshot: no such references in non-docs files (verified). All hits are imports + symbol references.

- [ ] **Step 4: Sanity-check non-obvious files**

Special cases worth eyeballing:
- `alembic/env.py` — confirm it's the only `from call_monitor` reference in alembic/. (Migrations are SQL-only; they don't import Python.)
- `pyproject.toml` — confirm both `name = "call-monitor"` and `packages = ["src/call_monitor"]` appear.
- `docker-compose.yml` — confirm all four references (`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `call_monitor_pgdata` volume name) are present.
- `.env.example` — confirm the connection string is the only place `call_monitor` appears.

- [ ] **Step 5: Document + commit (docs-only)**

Write a tiny scope note inline as a markdown comment in the plan file (NOT a new file — this is just to confirm Task 1's deliverable). The body of the next task's commit message references this discovery.

No commit needed — Task 1 is verification only. Proceed to Task 2.

---

## Task 2: atomic rename — directory + all imports + configs + docs

**ONE big commit.** Partial states (directory renamed but imports not updated; imports updated but configs not) break the test suite, so we don't split. Within this task, the steps are sequential — don't run tests until the end.

**Files:** ~190 modified, 1 directory moved.

- [ ] **Step 1: Move the directory**

```bash
git mv src/call_monitor src/call_manager
```

Verify:
```bash
ls src/
# expect: call_manager (and nothing called call_monitor)
git status
# expect: every .py file under src/call_monitor/ shown as a rename to src/call_manager/
```

- [ ] **Step 2: Bulk-replace `call_monitor` → `call_manager` in all .py files**

```bash
find src tests alembic scripts -name '*.py' -exec sed -i 's/call_monitor/call_manager/g' {} +
```

Check the diff stat:
```bash
git diff --stat | tail -5
# expect ~180+ files changed (mostly tests and src imports)
```

Spot-check a representative file:
```bash
grep -n 'call_manager\|call_monitor' src/call_manager/scheduler.py
# expect: only `call_manager` references; zero `call_monitor`
```

- [ ] **Step 3: Update pyproject.toml**

Two changes:

```bash
sed -i 's/name = "call-monitor"/name = "call-manager"/' pyproject.toml
sed -i 's|src/call_monitor|src/call_manager|g' pyproject.toml
```

Verify:
```bash
grep -n 'name\|packages' pyproject.toml | head -5
# expect: name = "call-manager", packages = ["src/call_manager"]
```

- [ ] **Step 4: Update .env.example**

```bash
sed -i 's/call_monitor/call_manager/g' .env.example
```

Verify:
```bash
grep DATABASE_URL .env.example
# expect: postgresql+psycopg://call_manager:call_manager@localhost:5432/call_manager
```

- [ ] **Step 5: Update docker-compose.yml**

```bash
sed -i 's/call_monitor/call_manager/g' docker-compose.yml
```

Verify all four substitutions landed:
```bash
grep -n 'call_manager\|call_monitor' docker-compose.yml
# expect: POSTGRES_USER: call_manager, POSTGRES_PASSWORD: call_manager,
#         POSTGRES_DB: call_manager, call_manager_pgdata volume — and ZERO call_monitor
```

- [ ] **Step 6: Update CLAUDE.md + README.md (both namespaces)**

```bash
# README.md has both `call-monitor` (project name) and `call_monitor`-rooted file paths.
sed -i 's/call_monitor/call_manager/g' README.md CLAUDE.md
sed -i 's/call-monitor/call-manager/g' README.md CLAUDE.md
```

The order matters in CLAUDE.md: `# call-monitor` on line 1 needs the hyphenated replacement; the project description doesn't reference paths so the underscored pass is mostly a no-op there. README has many `src/call_monitor/` path references that the underscored pass catches.

**Don't sweep docs/** — those are dated decision records that should preserve the historical name. The plan's prerequisites note exempts them.

- [ ] **Step 7: Run uv sync**

The package name in pyproject.toml changed. uv needs to re-register:

```bash
uv sync
```

Expect: "Built call-manager @ ..." (NOT call-monitor). If it fails to build, check `[tool.hatch.build.targets.wheel]` packages path matches the new directory.

- [ ] **Step 8: Verify the zero-`call_monitor`-anywhere invariant**

```bash
EXCL='--exclude-dir=.venv --exclude-dir=.worktrees --exclude-dir=.git --exclude-dir=docs --exclude-dir=__pycache__'
grep -rln 'call_monitor\|call-monitor' $EXCL .
# expect: NO matches outside docs/
```

If matches remain, locate + fix them. Common stragglers:
- `*.pyc` cache files inside `__pycache__/` (we exclude `__pycache__` so this should be empty)
- `.coverage` / `pytest.ini` cache artifacts (exclude or delete)
- `tests/conftest.py` or shared fixtures with a literal string that the sed missed (rare but possible if quoted oddly)

If `.git/` has remnants (e.g., reflog) — that's normal; git's history is preserved and accessible by the old name. Don't touch `.git/`.

- [ ] **Step 9: Run the full test suite**

```bash
uv run pytest -q
# expect: all passing (no regressions — this is pure rename)
```

If failures: check the first one's traceback. Most likely cause is a missed sed substitution (e.g., a dynamic import via `importlib.import_module("call_monitor.X")` that the literal-string sed handled correctly, OR a string in a test fixture that references the old name).

- [ ] **Step 10: Ruff**

```bash
uv run ruff format src tests
uv run ruff check src tests
# expect: clean
```

If ruff complains about unused imports or formatting in any of the touched files: address only those. Don't sweep pre-existing debt.

- [ ] **Step 11: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(b2): rename call-monitor → call-manager

Pure mechanical rename. Two namespaces:
- call_monitor → call_manager (Python package, DB user/password/db,
  ~190 .py imports across src/ + tests/, alembic/env.py)
- call-monitor → call-manager (project name in pyproject.toml,
  docker volume name, README + CLAUDE.md headings)

Touch surface:
- src/call_monitor/ → src/call_manager/ (git mv preserves history)
- pyproject.toml: name + packages
- .env.example: DATABASE_URL
- docker-compose.yml: POSTGRES_USER/PASSWORD/DB + volume name
- alembic/env.py: Base import
- README.md, CLAUDE.md: project name + file path references

Out of scope: docs/ retains historical references (decision records
made when the project had the old name); compatibility shim
(no external consumers); live production DB rename (handled by a
separate deployment-time SQL script, Task 3).

EOF
)"
```

Don't push.

---

## Task 3: cutover SQL script + deployment note

The Postgres `ALTER DATABASE` can't run from inside the database being renamed (Postgres blocks the rename while any session is connected to it). It runs from a `postgres` admin connection, after the app process is stopped.

**Files:**
- Create: `scripts/cutover_rename_database.sh`

- [ ] **Step 1: Write the script**

`scripts/cutover_rename_database.sh`:

```bash
#!/usr/bin/env bash
# Cutover script for B2 (call-monitor → call-manager) deployment.
#
# Renames the Postgres database + role from call_monitor to call_manager.
# MUST be run from a session NOT connected to either database (use psql -d postgres).
# MUST be run after the app process is stopped and before the new app process starts
# with DATABASE_URL pointing at call_manager.
#
# Idempotent: re-running after a successful cutover is a no-op (the IF EXISTS guards
# turn the second run into nothing).
#
# Required env vars:
#   PGHOST, PGPORT, PGUSER (a superuser, NOT call_monitor — that role is being renamed),
#   PGPASSWORD.

set -euo pipefail

if [[ -z "${PGUSER:-}" ]]; then
    echo "PGUSER must be set to a superuser (NOT call_monitor)" >&2
    exit 1
fi

echo "Connecting as PGUSER=$PGUSER to PGHOST=${PGHOST:-localhost}…"

# Step 1: rename the database. Requires no active connections to call_monitor.
psql -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_database WHERE datname = 'call_monitor') THEN
        ALTER DATABASE call_monitor RENAME TO call_manager;
        RAISE NOTICE 'Renamed database call_monitor → call_manager';
    ELSE
        RAISE NOTICE 'call_monitor database does not exist (already renamed?); skipping';
    END IF;
END
\$\$;
SQL

# Step 2: rename the role.
psql -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'call_monitor') THEN
        ALTER ROLE call_monitor RENAME TO call_manager;
        RAISE NOTICE 'Renamed role call_monitor → call_manager';
    ELSE
        RAISE NOTICE 'call_monitor role does not exist; skipping';
    END IF;
END
\$\$;
SQL

# Step 3: Postgres invalidates the password on RENAME; restore it.
# (The password matches the dev-env default; production deployments
# rotate this via the env-var pattern documented in .env.example.)
psql -d postgres -v ON_ERROR_STOP=1 -c "ALTER ROLE call_manager WITH PASSWORD 'call_manager';"

echo "Cutover complete. Bring up the new app with DATABASE_URL pointing at call_manager."
```

```bash
chmod +x scripts/cutover_rename_database.sh
```

- [ ] **Step 2: Document the cutover in README.md**

Add a section near the deployment docs:

```markdown
## Renamed from `call-monitor` (B2 cutover)

The project was renamed from `call-monitor` to `call-manager` in 2026-05.
For production deployments crossing the rename boundary:

1. Stop the running app process (so no sessions hold the old database open).
2. Run the cutover script as a Postgres superuser:
   ```bash
   PGUSER=postgres PGHOST=... bash scripts/cutover_rename_database.sh
   ```
3. Update `DATABASE_URL` in the deployment's env to point at `call_manager`
   (user, password, and database name all `call_manager`; the dev default
   matches `.env.example`).
4. Start the new app process with the updated env.

The cutover script is idempotent: a second invocation after success is a no-op.
```

- [ ] **Step 3: Commit**

```bash
git add scripts/cutover_rename_database.sh README.md
git commit -m "chore(b2): cutover SQL script + deployment-doc note"
```

---

## Task 4: milestone — verify rename completeness

Confirm the rename is fully done, no regressions, and the public surface is consistent.

- [ ] **Step 1: Final grep**

```bash
EXCL='--exclude-dir=.venv --exclude-dir=.worktrees --exclude-dir=.git --exclude-dir=docs --exclude-dir=__pycache__'
grep -rn 'call_monitor\|call-monitor' $EXCL .
```

Expect: ZERO matches outside `docs/`. If anything appears, fix and commit a fixup.

- [ ] **Step 2: Full test suite**

```bash
uv run pytest -q
```

Expect: ~788 passing, 1 skipped (the live Claude round-trip), matching the pre-rename baseline.

- [ ] **Step 3: Ruff**

```bash
uv run ruff check src tests
```

Expect: clean.

- [ ] **Step 4: Surface checks**

```bash
# The package is importable under the new name only:
uv run python -c "import call_manager; print('OK')"
uv run python -c "import call_monitor" 2>&1 | head -2
# expect: ModuleNotFoundError: No module named 'call_monitor'

# The package's own internal imports work end-to-end (smoke):
uv run python -c "from call_manager.proposal_assist.drafting import WorkspaceController, build_workspace_controller; print('OK')"
```

- [ ] **Step 5: Surface for the user**

> B2 complete. The codebase is now `call-manager` (hyphenated) / `call_manager` (underscored). No new features; pure refactor. The cutover SQL script in `scripts/cutover_rename_database.sh` handles the deployment-time DB rename (idempotent; documented in README).
>
> **Out of scope (handled separately):**
> - Compatibility shim (`call_monitor` alias package) — not needed for an internal tool.
> - Live production DB rename — manual cutover, see README.
> - Historical decision-record docs under `docs/` deliberately keep `call-monitor` references.

---

## Self-review notes (for the engineer running this plan)

1. **Two namespaces.** `call_monitor` (Python package, env var values) and `call-monitor` (project name, docker volume). Sed handles each in a separate pass — never combine the two regexes; that risks double-substituting.

2. **`git mv` matters.** It registers the directory rename as a single rename op so `git log --follow` works. A naïve `mv` would lose blame history.

3. **uv sync after pyproject.** The package name change means uv has to rebuild the wheel against the new name. Without this, `uv run pytest` would still find the old install.

4. **Docs are exempt.** `docs/` contains decision records from when the project had the old name. Renaming them would rewrite history; keep them.

5. **The cutover script is idempotent.** Re-running after a successful cutover is a no-op (the `IF EXISTS` guards). Safe to re-run if a deployment retries.

6. **Postgres password is reset by ALTER ROLE RENAME.** The script re-sets it to the dev-env value. Production must use the env-var-based password mechanism (see `.env.example`).

7. **No alembic migration for the rename.** `ALTER DATABASE … RENAME TO` can't run from inside the renaming database, so alembic — which connects to it — can't host this op. The shell script connects to `postgres` instead.

8. **Test count expectation: 788 passing, 1 skipped.** Matches the pre-rename master baseline. Any failure is a missed sed substitution or a dynamic-import construct.

---

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-20-base-spec-2-rename.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, two-stage review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
