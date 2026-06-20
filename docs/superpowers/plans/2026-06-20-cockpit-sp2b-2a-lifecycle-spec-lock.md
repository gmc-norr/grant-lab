# Cockpit SP2b-2a — Two-mode lifecycle + spec lock — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `Workspace.current_phase` from the dormant five-value lifecycle to the real two-mode `{setup, active}` model, and make the cockpit Spec **lockable** by its owner so it can serve as a frozen, agreed basis for downstream work.

**Architecture:** A single Alembic migration (`0052`) flips the `current_phase` CHECK, adds `spec_locked_at`/`spec_locked_by` columns, and creates a `spec_lock_events` audit table. Enforcement is one chokepoint — a `SpecLockedError` guard inside `save_section` (read under the commit mutex) plus an explicit guard in the non-`save_section` `post_init_spec` path. Two owner-gated endpoints toggle the lock; the read-model (`SpecOut`) folds the lock into `can_edit`; the frontend reuses the editor's existing read-only path and adds a banner + one toggle.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL 16 (testcontainers), pytest; React 18 + Vite + TanStack Query + TypeScript + Vitest/jsdom, pnpm.

**Spec:** `docs/superpowers/specs/2026-06-20-cockpit-sp2b-2a-lifecycle-spec-lock-design.md`

**Worktree:** all work happens in `/home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a` on branch `cockpit-sp2b-2a` (off master `dd36994`). Every git command must run against this worktree and the branch must read `cockpit-sp2b-2a` before any commit.

---

## Conventions every task must follow

- **TDD:** write the failing test, run it and SEE it fail for the expected reason, implement the minimal change, run it and see it pass, then commit.
- **Test flakiness (known):** the full `uv run pytest` is testcontainer-flaky under load (connection-refused). Run the **specific touched test file(s)**, not the whole suite. If a test fails with a connection error, re-run that file in isolation before treating it as real.
- **Commit trailer:** every commit message ends with exactly:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **Frontend:** there is **no TS linter** — the static gate is `pnpm exec tsc --noEmit` (NOT `pnpm lint`). Schema regen is `EMBEDDING_MODEL=bge-m3 pnpm gen:api`. pnpm needs Node 22 (via nvm); if pnpm is missing, source nvm first. Run frontend commands from `frontend/`.
- **Status mapping (existing house rule):** 403 forbidden, 404 not_found, 409 conflict, 400 bad_request, 429 too_many_requests, 500 internal_error. The global ≥500 handler **sanitizes any uncaught exception to `internal_error`** — so a new domain error MUST be explicitly caught and mapped, or it becomes a 500.

---

## File Structure

**Backend — modified:**
- `src/call_manager/db/models.py` — `Workspace` CHECK `{setup,active}` + `spec_locked_at`/`spec_locked_by` columns; new `SpecLockEvent` model.
- `alembic/versions/0052_workspace_two_mode_lifecycle_and_spec_lock.py` — **create** the migration (repo-root `alembic/versions/`, per `alembic.ini` `script_location = %(here)s/alembic`; beside `0051_proposed_changes.py`).
- `src/call_manager/proposal_assist/editing/save.py` — `SpecLockedError`; the spec-lock guard inside `save_section`.
- `src/call_manager/web/api/workspaces.py` — `SpecLockOut`; `POST /spec/lock` + `/spec/unlock`; `post_init_spec` guard; `post_save_spec_section` 409 mapping; `SpecOut` lock fields + folded `can_edit`; `get_spec` updates.
- `src/call_manager/proposal_assist/web/rubric_review.py` — `"spec"` → `"active"` (lines 147, 153).
- `src/call_manager/proposal_assist/phases/setup.py` — comment at line 372 (`current_phase='spec'`) → `'active'` (non-functional, accuracy only).
- `src/call_manager/web/filters.py` — `phase_label` map → `{setup, active}`.

**Backend — tests created/modified:**
- `tests/test_migration_0052_lifecycle_spec_lock.py` — **create**.
- `tests/test_editing_save.py` — add `save_section` lock-guard unit tests.
- `tests/test_api_spec_lock.py` — **create** (lock/unlock endpoints + enforcement).
- `tests/test_api_spec.py` — extend GET /spec for the read-model.
- `tests/test_web_filters.py` — update `phase_label` expectations.
- `tests/proposal_assist/conftest.py` — `make_workspace` default `current_phase` → `"active"`.
- Test fixture sweep (see Task 1): explicit `current_phase="spec"` literals + `== "spec"` assertions across ~10 files.

**Frontend — modified:**
- `frontend/src/api/editor.ts` — `useLockSpec` / `useUnlockSpec`.
- `frontend/src/api/schema.gen.ts` — regenerated.
- `frontend/src/cockpit/SpecPanel.tsx` — lock banner + Lock/Unlock control.
- `frontend/src/styles/cockpit.css` — `.spec-lock-banner`, `.spec-lock-bar`.
- `frontend/src/api/editor.test.tsx` — hook tests.
- `frontend/src/cockpit/SpecPanel.test.tsx` — lock UI tests.

---

## Task 1: Two-mode lifecycle schema + rename sweep

The migration that flips the `current_phase` CHECK breaks **every** insert that writes a non-`{setup,active}` value. Because `make_workspace` *defaults* `current_phase="spec"` and ~22 call sites pass it explicitly, the schema change and the fixture/assertion sweep must land in **one commit** — there is no green intermediate (before the CHECK change, `"active"` violates the old constraint; after it, `"spec"` violates the new one). This task ships the whole schema (including the lock columns/table, which are inert until later tasks) plus the rename.

**Files:**
- Modify: `src/call_manager/db/models.py` (Workspace `__table_args__` CHECK at lines 591–594; add columns after `current_phase` at lines 657–661; add `SpecLockEvent` after `PhaseApproval` ~line 1163)
- Create: `alembic/versions/0052_workspace_two_mode_lifecycle_and_spec_lock.py`
- Modify: `src/call_manager/proposal_assist/web/rubric_review.py:147,153`
- Modify: `src/call_manager/proposal_assist/phases/setup.py:372` (comment)
- Modify: `src/call_manager/web/filters.py:81–97`
- Modify: `tests/proposal_assist/conftest.py` (`make_workspace` default)
- Sweep (literals `current_phase="spec"` → `"active"`): `tests/proposal_assist/test_kill_switch.py:36,65`; `tests/test_api_workspaces.py:57,144,167,228,230,441,473,699,1110`; `tests/proposal_assist/citations/test_human_cite.py:39`; `tests/proposal_assist/test_web_workspaces_turn_errors.py:19,39`; `tests/proposal_assist/test_web_workspaces_turns.py:663,717,796,861,919`; `tests/proposal_assist/test_global_cap.py:119,171`
- Sweep (assertions `current_phase == "spec"` → `"active"`): `tests/proposal_assist/test_rubric_review_endpoint.py:217,287`; `tests/proposal_assist/test_web_workspace_setup_gate.py:394`; `tests/proposal_assist/test_phase0_e2e.py:199` (and the docstring at line 8)
- Modify: `tests/test_web_filters.py`
- Create: `tests/test_migration_0052_lifecycle_spec_lock.py`

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_migration_0052_lifecycle_spec_lock.py`:

```python
"""Migration 0052: two-mode lifecycle + spec lock.

Asserts (a) the new schema objects exist at head, (b) the new CHECK rejects an
old phase value, and (c) the data migration maps every pre-existing
spec/plan/draft/review workspace to 'active' (unlocked) while leaving 'setup'.

pg_url / migrated_engine are the SHARED session container; the round-trip test
seeds at 0051, upgrades, asserts, then restores head + cleans its rows in a
finally (mirrors tests/test_migration_0035_user_fk_restrict.py)."""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command


def _cfg(pg_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_lock_schema_present_at_head(migrated_engine):
    insp = inspect(migrated_engine)
    assert "spec_lock_events" in insp.get_table_names()
    ws_cols = {c["name"] for c in insp.get_columns("workspaces")}
    assert {"spec_locked_at", "spec_locked_by"} <= ws_cols


def test_new_check_rejects_old_phase(pg_url):
    cfg = _cfg(pg_url)
    command.upgrade(cfg, "head")
    eng = create_engine(pg_url)
    try:
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('lck@ki.se')"))
            uid = c.execute(text("SELECT id FROM users WHERE email='lck@ki.se'")).scalar_one()
            c.execute(
                text(
                    "INSERT INTO workspaces (id, title, owner_id, status, current_phase) "
                    "VALUES (gen_random_uuid(), 'W', :u, 'drafting', 'setup')"
                ),
                {"u": uid},
            )
        raised = False
        try:
            with eng.begin() as c:
                c.execute(
                    text(
                        "UPDATE workspaces SET current_phase='draft' "
                        "WHERE owner_id IN (SELECT id FROM users WHERE email='lck@ki.se')"
                    )
                )
        except IntegrityError:
            raised = True
        assert raised, "the {setup,active} CHECK must reject a legacy phase value"
    finally:
        with eng.begin() as c:
            c.execute(
                text(
                    "DELETE FROM workspaces WHERE owner_id IN "
                    "(SELECT id FROM users WHERE email='lck@ki.se')"
                )
            )
            c.execute(text("DELETE FROM users WHERE email='lck@ki.se'"))
        eng.dispose()


def test_data_migration_maps_old_phases_to_active(pg_url):
    cfg = _cfg(pg_url)
    eng = create_engine(pg_url)
    try:
        command.downgrade(cfg, "0051")  # before this migration
        with eng.begin() as c:
            c.execute(text("INSERT INTO users (email) VALUES ('mig@ki.se')"))
            uid = c.execute(text("SELECT id FROM users WHERE email='mig@ki.se'")).scalar_one()
            for phase in ("setup", "spec", "plan", "draft", "review"):
                c.execute(
                    text(
                        "INSERT INTO workspaces (id, title, owner_id, status, current_phase) "
                        "VALUES (gen_random_uuid(), :t, :u, 'drafting', :p)"
                    ),
                    {"t": phase, "u": uid, "p": phase},
                )
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            rows = c.execute(
                text(
                    "SELECT title, current_phase, spec_locked_at FROM workspaces "
                    "WHERE owner_id IN (SELECT id FROM users WHERE email='mig@ki.se')"
                )
            ).all()
        by_title = {r.title: r for r in rows}
        assert by_title["setup"].current_phase == "setup"
        for phase in ("spec", "plan", "draft", "review"):
            assert by_title[phase].current_phase == "active", phase
        assert all(r.spec_locked_at is None for r in rows)
    finally:
        with eng.begin() as c:
            c.execute(
                text(
                    "DELETE FROM workspaces WHERE owner_id IN "
                    "(SELECT id FROM users WHERE email='mig@ki.se')"
                )
            )
            c.execute(text("DELETE FROM users WHERE email='mig@ki.se'"))
        command.upgrade(cfg, "head")  # restore head for later tests
        eng.dispose()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_migration_0052_lifecycle_spec_lock.py -v`
Expected: FAIL — `command.upgrade(..., "head")` can't resolve `0052` (no such revision) / `spec_lock_events` absent.

- [ ] **Step 3: Update the `Workspace` model — CHECK + columns**

In `src/call_manager/db/models.py`, change the `current_phase` CHECK (lines 591–594) to:

```python
        CheckConstraint(
            "current_phase IN ('setup','active')",
            name="ck_workspaces_current_phase",
        ),
```

And immediately after the `current_phase` column (the `mapped_column` ending at line 661) add:

```python
    spec_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    spec_locked_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
```

- [ ] **Step 4: Add the `SpecLockEvent` model**

In `src/call_manager/db/models.py`, after the `PhaseApproval` class (ends ~line 1163), add:

```python
class SpecLockEvent(Base):
    """Append-only audit of spec lock/unlock toggles (cockpit SP2b-2a).

    One row per real state transition (idempotent no-ops write nothing).
    actor_id is SET NULL on user delete so the audit row survives.
    """

    __tablename__ = "spec_lock_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('lock','unlock')",
            name="ck_spec_lock_events_action",
        ),
        Index(
            "ix_spec_lock_events_ws_created",
            "workspace_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
```

(All names used — `CheckConstraint`, `Index`, `text`, `UUID`, `ForeignKey`, `BigInteger`, `Text`, `DateTime`, `func`, `Mapped`, `mapped_column`, `uuid`, `datetime` — are already imported and used by `PhaseApproval`/`Workspace`; no new imports.)

- [ ] **Step 5: Write the migration**

Create `alembic/versions/0052_workspace_two_mode_lifecycle_and_spec_lock.py` (repo-root `alembic/versions/`, beside `0051_proposed_changes.py`):

```python
"""two-mode lifecycle + spec lock

Revision ID: 0052
Revises: 0051

current_phase {setup,spec,plan,draft,review} -> {setup,active}; add
spec_locked_at/spec_locked_by to workspaces; create spec_lock_events.

The old CHECK is dropped BEFORE the data UPDATE because 'active' is not a
permitted value under it. Downgrade is LOSSY: the old four-way distinction
among spec/plan/draft/review is unrecoverable (active -> 'draft' best-effort).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Drop the old CHECK first so the UPDATE to 'active' is permitted.
    op.drop_constraint("ck_workspaces_current_phase", "workspaces", type_="check")
    # 2. Collapse the legacy working phases to the single 'active' mode.
    op.execute(
        "UPDATE workspaces SET current_phase='active' "
        "WHERE current_phase IN ('spec','plan','draft','review')"
    )
    # 3. Recreate the CHECK with the two-mode vocabulary.
    op.create_check_constraint(
        "ck_workspaces_current_phase",
        "workspaces",
        "current_phase IN ('setup','active')",
    )
    # 4. Lock state columns (NULL on every existing row => unlocked).
    op.add_column(
        "workspaces",
        sa.Column("spec_locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("workspaces", sa.Column("spec_locked_by", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_workspaces_spec_locked_by",
        "workspaces",
        "users",
        ["spec_locked_by"],
        ["id"],
        ondelete="SET NULL",
    )
    # 5. Audit table.
    op.create_table(
        "spec_lock_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("artifact_commit_sha", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_sle_workspace"
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], ondelete="SET NULL", name="fk_sle_actor"
        ),
    )
    op.create_check_constraint(
        "ck_spec_lock_events_action",
        "spec_lock_events",
        "action IN ('lock','unlock')",
    )
    op.create_index(
        "ix_spec_lock_events_ws_created",
        "spec_lock_events",
        ["workspace_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    # LOSSY: spec/plan/draft/review were collapsed to 'active' and cannot be
    # recovered; map back to a single representative phase ('draft').
    op.drop_index("ix_spec_lock_events_ws_created", table_name="spec_lock_events")
    op.drop_table("spec_lock_events")
    op.drop_constraint("fk_workspaces_spec_locked_by", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "spec_locked_by")
    op.drop_column("workspaces", "spec_locked_at")
    op.drop_constraint("ck_workspaces_current_phase", "workspaces", type_="check")
    op.execute("UPDATE workspaces SET current_phase='draft' WHERE current_phase='active'")
    op.create_check_constraint(
        "ck_workspaces_current_phase",
        "workspaces",
        "current_phase IN ('setup','spec','plan','draft','review')",
    )
```

- [ ] **Step 6: Production touch-ups (lifecycle writers + label)**

In `src/call_manager/proposal_assist/web/rubric_review.py`:
- line 147: `ws.current_phase = "spec"` → `ws.current_phase = "active"`
- line 153: `if ws.current_phase == "spec":` → `if ws.current_phase == "active":`

In `src/call_manager/proposal_assist/phases/setup.py` line 372, update the comment `current_phase='spec'` → `current_phase='active'` (comment only).

In `src/call_manager/web/filters.py`, replace the `phase_label` body (lines 81–97) with the two-mode map:

```python
def phase_label(value: str | None) -> str:
    """Human-readable label for `Workspace.current_phase`. Stored values are
    the two-mode lifecycle ('setup','active'); the UI chip should read as
    words. Unknown values pass through unchanged so a future mode isn't
    swallowed into a blank chip."""
    if not value:
        return "—"
    return {
        "setup": "Setting up",
        "active": "Active",
    }.get(value, value)
```

- [ ] **Step 7: Fixture default + sweep**

In `tests/proposal_assist/conftest.py` `make_workspace`: change the parameter default `current_phase: str = "spec"` → `current_phase: str = "active"`, and in its docstring change `defaults to "spec" (post-setup)` → `defaults to "active" (post-setup)`.

In every file/line listed under **Files → Sweep (literals)**, replace `current_phase="spec"` with `current_phase="active"` (use Edit `replace_all` per file). In every file/line under **Sweep (assertions)**, replace `current_phase == "spec"` with `current_phase == "active"` and update the `test_phase0_e2e.py` docstring mention.

- [ ] **Step 8: Update the filters test**

In `tests/test_web_filters.py`, update the `phase_label` expectations to the two-mode map. The file should assert:

```python
from call_manager.web.filters import phase_label


def test_phase_label_two_mode():
    assert phase_label("setup") == "Setting up"
    assert phase_label("active") == "Active"


def test_phase_label_unknown_passthrough():
    assert phase_label("future_mode") == "future_mode"
    assert phase_label(None) == "—"
    assert phase_label("") == "—"
```

(Replace any existing assertions that reference the old `spec`/`plan`/`draft`/`review` labels.)

- [ ] **Step 9: Run the migration test + the swept suites**

Run: `uv run pytest tests/test_migration_0052_lifecycle_spec_lock.py tests/test_web_filters.py -v`
Expected: PASS.

Run the suites whose fixtures were swept (per file, to dodge the flaky whole-suite run):
`uv run pytest tests/test_api_workspaces.py tests/test_api_spec.py tests/proposal_assist/test_rubric_review_endpoint.py tests/proposal_assist/test_web_workspace_setup_gate.py tests/proposal_assist/test_phase0_e2e.py tests/proposal_assist/test_kill_switch.py tests/proposal_assist/test_global_cap.py tests/proposal_assist/test_web_workspaces_turns.py tests/proposal_assist/test_web_workspaces_turn_errors.py tests/proposal_assist/citations/test_human_cite.py -q`
Expected: PASS (re-run any connection-refused file in isolation).

- [ ] **Step 10: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a
git branch --show-current   # must print: cockpit-sp2b-2a
git add -A
git commit -m "feat(db,lifecycle): two-mode current_phase + spec-lock schema (0052)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `SpecLockedError` + the `save_section` spec-lock guard

The single enforcement chokepoint for spec **edits**. Read the lock state from a row refreshed under the commit mutex so a lock that committed while this save waited on the mutex is observed. Gated on `artifact is Artifact.SPEC` so the draft path is byte-for-byte unchanged.

**Files:**
- Modify: `src/call_manager/proposal_assist/editing/save.py` (new exception near `StaleSectionError` ~line 50; guard after the mutex at lines 173–175)
- Test: `tests/test_editing_save.py`

- [ ] **Step 1: Write the failing unit tests**

Add to `tests/test_editing_save.py` (it already has `_init_repo` and a `pg_session_factory`/`tmp_path` pattern; reuse the file's existing helpers for building a workspace + repo). Use `make_user`/`make_workspace` from `tests.proposal_assist.conftest` to build the row, and set the lock field directly:

```python
from datetime import UTC, datetime

import pytest

from call_manager.proposal_assist.artifacts import Artifact
from call_manager.proposal_assist.editing.save import SpecLockedError, save_section
from tests.proposal_assist.conftest import make_user, make_workspace


def test_save_section_blocks_locked_spec(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = owner.id
        s.commit()
        _init_repo(tmp_path, ws.id, doc="## Aims\nbody.\n", filename="proposal-spec.md")
        with pytest.raises(SpecLockedError):
            save_section(
                session=s,
                workspace=ws,
                user_id=owner.id,
                section_index=1,
                new_text="## Aims\nedited.\n",
                base_commit_sha="0" * 40,
                base_section_sha="0" * 64,
                workspaces_dir=str(tmp_path),
                artifact=Artifact.SPEC,
            )


def test_save_section_allows_draft_while_spec_locked(pg_session_factory, tmp_path):
    with pg_session_factory() as s:
        owner = make_user(s)
        ws = make_workspace(s, owner=owner)
        ws.spec_locked_at = datetime.now(UTC)
        ws.spec_locked_by = owner.id
        s.commit()
        _init_repo(tmp_path, ws.id, doc="preamble\n\n## Methods\nm.\n")
        draft = save_section(
            session=s,
            workspace=ws,
            user_id=owner.id,
            section_index=1,
            new_text="## Methods\nm2.\n",
            base_commit_sha=_head(tmp_path, ws.id),
            base_section_sha=_section_sha(tmp_path, ws.id, 1),
            workspaces_dir=str(tmp_path),
            artifact=Artifact.DRAFT,
        )
        s.commit()
        assert draft.changed is True  # draft is unaffected by a spec lock
```

> Implementer note: `_init_repo` in `tests/test_editing_save.py` writes `proposal.md` by default; pass/extend it to also seed `proposal-spec.md` for the spec case (mirror `tests/test_api_spec.py::_add_spec`). `_head`/`_section_sha` are whatever helpers that file already uses to read HEAD and a section sha — reuse them; if absent, read HEAD via `current_head(workspace_repo_dir(tmp_path, ws.id))` and the sha from `split_sections`. Keep the draft assertion's bases valid so it exercises the real write path (the point is that the guard does NOT fire for the draft).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_editing_save.py -k "locked_spec or draft_while_spec_locked" -v`
Expected: FAIL — `ImportError: cannot import name 'SpecLockedError'`.

- [ ] **Step 3: Add the exception + guard**

In `src/call_manager/proposal_assist/editing/save.py`, after `SectionStructureError` (line 58) add:

```python
class SpecLockedError(RuntimeError):
    """The spec is locked (workspace.spec_locked_at set); a spec write is
    refused until it is unlocked. Web routes map this to 409."""
```

Then in `save_section`, right after the mutex + normalization (after line 175 `artifact = Artifact(artifact)`), add:

```python
    # Spec-lock guard: a lock may have committed while we waited on the mutex,
    # so re-read the row under the lock before deciding. Gated on SPEC so the
    # draft path is unchanged (no extra query, no behavior change).
    if artifact is Artifact.SPEC:
        session.refresh(workspace, ["spec_locked_at"])
        if workspace.spec_locked_at is not None:
            raise SpecLockedError(str(workspace.id))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_editing_save.py -k "locked_spec or draft_while_spec_locked" -v`
Expected: PASS.

- [ ] **Step 5: Run the full editing-save file (no regressions)**

Run: `uv run pytest tests/test_editing_save.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a
git branch --show-current   # cockpit-sp2b-2a
git add src/call_manager/proposal_assist/editing/save.py tests/test_editing_save.py
git commit -m "feat(editing): SpecLockedError guard in save_section (spec only)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Lock / unlock endpoints + `SpecLockOut`

Two owner-gated endpoints toggle the lock under the commit mutex, refreshing the workspace row after acquiring it so idempotency reads committed truth.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (imports; `SpecLockOut`; the two routes; a `_locker_email` helper)
- Test: `tests/test_api_spec_lock.py` (create)

- [ ] **Step 1: Write the failing endpoint tests**

Create `tests/test_api_spec_lock.py`:

```python
"""Cockpit spec lock/unlock endpoints + enforcement. Reuses the _app/_seed/
_client helpers + _add_spec from tests/test_api_spec.py."""

from __future__ import annotations

from call_manager.db.models import SpecLockEvent
from tests.test_api_spec import _add_spec
from tests.test_api_workspaces import _app, _client, _seed


def _events(pg_session_factory, ws_id):
    with pg_session_factory() as s:
        return s.query(SpecLockEvent).filter(SpecLockEvent.workspace_id == ws_id).all()


def test_lock_owner_writes_one_event_and_pins_head(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    head = c.get(f"/api/workspaces/{ws_id}/spec").json()["head"]
    r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 200
    body = r.json()
    assert body["locked"] is True and body["locked_by"] == "owner@ki.se"
    evs = _events(pg_session_factory, ws_id)
    assert len(evs) == 1
    assert evs[0].action == "lock" and evs[0].artifact_commit_sha == head


def test_lock_empty_spec_is_409_no_event(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "   \n")  # whitespace-only
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 409
    assert _events(pg_session_factory, ws_id) == []


def test_lock_is_idempotent_no_second_event(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    r2 = c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r2.status_code == 200 and r2.json()["locked"] is True
    assert len(_events(pg_session_factory, ws_id)) == 1


def test_unlock_clears_state_and_audits(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    r = c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    assert r.status_code == 200 and r.json()["locked"] is False
    actions = [e.action for e in _events(pg_session_factory, ws_id)]
    assert actions == ["lock", "unlock"]


def test_unlock_when_unlocked_is_noop(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    r = c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    assert r.status_code == 200 and r.json()["locked"] is False
    assert _events(pg_session_factory, ws_id) == []


def test_lock_member_forbidden(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    assert _client(app, member_id).post(f"/api/workspaces/{ws_id}/spec/lock").status_code == 403


def test_lock_no_head_is_404(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # A GitCommitError from current_head (unborn HEAD) maps to 404, not a
    # sanitized 500 — mirrors the /spec read/save routes. Patch current_head to
    # raise so the test is independent of git's unborn-branch behaviour.
    from call_manager.proposal_assist.citations.gitio import GitCommitError

    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")  # file exists so the pre-check passes

    def _boom(*args, **kwargs):
        raise GitCommitError("no HEAD")

    monkeypatch.setattr("call_manager.web.api.workspaces.current_head", _boom)
    r = _client(app, owner_id).post(f"/api/workspaces/{ws_id}/spec/lock")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_spec_lock.py -q`
Expected: FAIL — the routes 404 (not defined) and `SpecLockEvent` import is the only thing that resolves.

- [ ] **Step 3: Add imports + `SpecLockOut` + helper**

In `src/call_manager/web/api/workspaces.py`:

Add `from datetime import UTC, datetime` to the imports (`datetime` types `SpecLockOut`; `UTC` is used by `datetime.now(UTC)` in the route). Add `SpecLockEvent` to the `db.models` import block (lines 19–25). **Do NOT import `SpecLockedError` yet** — it is unused until Task 4, and an unused import fails ruff (F401) at this commit.

After `SpecOut` (line 107), add:

```python
class SpecLockOut(BaseModel):
    locked: bool
    locked_at: datetime | None = None
    locked_by: str | None = None
```

Near the other module helpers (e.g. after `_sections_out`), add:

```python
def _locker_email(session, user_id: int | None) -> str | None:
    """Display string for a spec locker. user_id is non-null only while the row
    is set, and the FK is SET NULL on user delete, so the lookup never dangles."""
    if user_id is None:
        return None
    u = session.get(User, user_id)
    return u.email if u is not None else None
```

- [ ] **Step 4: Add the lock + unlock routes**

Add to `src/call_manager/web/api/workspaces.py` (after `post_init_spec`):

```python
@router.post("/workspaces/{workspace_id}/spec/lock", response_model=SpecLockOut)
def post_lock_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecLockOut:
    """Lock the spec (owner-gated). Refuses an empty spec (409). Idempotent: a
    re-lock returns the current state and writes no new audit row."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        spec_path = repo_dir / SPEC_FILENAME
        if not spec_path.exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            acquire_commit_mutex(session, ws.id)
            # Re-read under the lock: a concurrent toggle may have committed
            # while we waited on the mutex.
            session.refresh(ws, ["spec_locked_at", "spec_locked_by"])
            if ws.spec_locked_at is None:
                if spec_path.read_text().strip() == "":
                    raise HTTPException(status_code=409, detail="cannot lock an empty spec")
                ws.spec_locked_at = datetime.now(UTC)
                ws.spec_locked_by = user.id
                session.add(
                    SpecLockEvent(
                        workspace_id=ws.id,
                        action="lock",
                        actor_id=user.id,
                        artifact_commit_sha=current_head(repo_dir),
                    )
                )
            locked_at, locked_by_id = ws.spec_locked_at, ws.spec_locked_by
            session.commit()
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            # Spec file present but no committable HEAD (unborn branch): mirror
            # the /spec read/save routes' 404 rather than a sanitized 500.
            session.rollback()
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
        locked_by = _locker_email(session, locked_by_id)
    return SpecLockOut(locked=True, locked_at=locked_at, locked_by=locked_by)


@router.post("/workspaces/{workspace_id}/spec/unlock", response_model=SpecLockOut)
def post_unlock_spec(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SpecLockOut:
    """Unlock the spec (owner-gated). Idempotent: unlocking an unlocked spec is
    a no-op that writes no audit row."""
    settings = request.app.state.settings
    sf = request.app.state.session_factory
    with sf() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
        if not (repo_dir / SPEC_FILENAME).exists():
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet")
        try:
            acquire_commit_mutex(session, ws.id)
            session.refresh(ws, ["spec_locked_at", "spec_locked_by"])
            if ws.spec_locked_at is not None:
                ws.spec_locked_at = None
                ws.spec_locked_by = None
                session.add(
                    SpecLockEvent(
                        workspace_id=ws.id,
                        action="unlock",
                        actor_id=user.id,
                        artifact_commit_sha=current_head(repo_dir),
                    )
                )
            session.commit()
        except CommitMutexTimeout as e:
            session.rollback()
            raise HTTPException(
                status_code=429, detail=BUSY_ERROR_MESSAGE, headers={"Retry-After": "2"}
            ) from e
        except GitCommitError as e:
            session.rollback()
            raise HTTPException(status_code=404, detail="no proposal-spec.md yet") from e
    return SpecLockOut(locked=False, locked_at=None, locked_by=None)
```

Confirm `current_head`, `GitCommitError`, `acquire_commit_mutex`, `CommitMutexTimeout`, `BUSY_ERROR_MESSAGE`, `SPEC_FILENAME`, `require_owner`, `User` are already imported (they are, per the existing spec routes); the only new imports this task adds are `UTC, datetime` and `SpecLockEvent` (Step 3).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_spec_lock.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a
git branch --show-current   # cockpit-sp2b-2a
git add src/call_manager/web/api/workspaces.py tests/test_api_spec_lock.py
git commit -m "feat(api): owner spec lock/unlock endpoints + audit

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Enforcement wiring (save route 409 + init guard)

Make the spec write routes honour the lock: map `SpecLockedError → 409` on the save route, and add the explicit guard to `post_init_spec` (which writes directly, not via `save_section`).

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (`post_save_spec_section` except clause; `post_init_spec` guard + except)
- Test: `tests/test_api_spec_lock.py` (extend)

- [ ] **Step 1: Write the failing enforcement tests**

Append to `tests/test_api_spec_lock.py`:

```python
def test_locked_spec_save_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\nedited.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


def test_locked_spec_init_is_409(monkeypatch, pg_url, pg_session_factory, tmp_path):
    # Lock a seeded (non-empty) spec, then call init: post_init_spec bypasses
    # save_section, so its OWN guard must fire. The guard runs before the
    # whitespace/no-op check, so 409 regardless of file content (no need to
    # blank the file).
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    assert c.post(f"/api/workspaces/{ws_id}/spec").status_code == 409


def test_draft_unaffected_by_spec_lock(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "preamble\n\n## Methods\nm.\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    draft = c.get(f"/api/workspaces/{ws_id}/draft").json()
    methods = draft["sections"][1]
    r = c.post(
        f"/api/workspaces/{ws_id}/sections/1",
        json={
            "content": "## Methods\nm2.\n",
            "base_commit_sha": draft["head"],
            "base_section_sha": methods["sha"],
        },
    )
    assert r.status_code == 200 and r.json()["changed"] is True


def test_unlock_reenables_spec_save(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nbe great.\n")
    c = _client(app, owner_id)
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    c.post(f"/api/workspaces/{ws_id}/spec/unlock")
    spec = c.get(f"/api/workspaces/{ws_id}/spec").json()
    aims = spec["sections"][1]
    r = c.post(
        f"/api/workspaces/{ws_id}/spec/sections/1",
        json={
            "content": "## Aims\nedited after unlock.\n",
            "base_commit_sha": spec["head"],
            "base_section_sha": aims["sha"],
        },
    )
    assert r.status_code == 200 and r.json()["changed"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_spec_lock.py -k "locked_spec or unaffected or reenables" -q`
Expected: FAIL — locked save returns a sanitized 500 (SpecLockedError uncaught) and locked init returns 200 (no guard).

- [ ] **Step 3: Map `SpecLockedError → 409` on the save route**

First add `SpecLockedError` to the `from call_manager.proposal_assist.editing.save import (...)` block (lines 46–53) — this task is its first use in the file. Then, in `post_save_spec_section`, add an except clause alongside the existing ones (after `StaleSectionError`, before `CommitMutexTimeout`):

```python
        except SpecLockedError as e:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail="spec is locked — unlock to edit",
            ) from e
```

- [ ] **Step 4: Guard `post_init_spec`**

In `post_init_spec`, right after `acquire_commit_mutex(session, ws.id)` (line 348), add:

```python
            session.refresh(ws, ["spec_locked_at"])
            if ws.spec_locked_at is not None:
                raise SpecLockedError(str(ws.id))
```

And add an except clause in `post_init_spec` (alongside its `CommitMutexTimeout`/`GitCommitError` handlers):

```python
        except SpecLockedError as e:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail="spec is locked — unlock to edit",
            ) from e
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_spec_lock.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a
git branch --show-current   # cockpit-sp2b-2a
git add src/call_manager/web/api/workspaces.py tests/test_api_spec_lock.py
git commit -m "feat(api): enforce spec lock on save + init (409)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `SpecOut` read-model (lock fields + folded `can_edit`)

Expose the lock to the frontend: add `locked`/`locked_at`/`locked_by`/`can_manage_lock` and make `can_edit` mean owner-AND-unlocked so the editor's existing read-only path engages.

**Files:**
- Modify: `src/call_manager/web/api/workspaces.py` (`SpecOut`; `get_spec`)
- Test: `tests/test_api_spec.py` (extend)

- [ ] **Step 1: Write the failing read-model tests**

Append to `tests/test_api_spec.py`:

```python
def test_get_spec_exposes_lock_fields(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, _, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    c = _client(app, owner_id)
    before = c.get(f"/api/workspaces/{ws_id}/spec").json()
    assert before["locked"] is False
    assert before["can_edit"] is True and before["can_manage_lock"] is True
    c.post(f"/api/workspaces/{ws_id}/spec/lock")
    after = c.get(f"/api/workspaces/{ws_id}/spec").json()
    assert after["locked"] is True
    assert after["can_edit"] is False  # locked -> read-only even for the owner
    assert after["can_manage_lock"] is True  # owner can still unlock
    assert after["locked_by"] == "owner@ki.se"


def test_get_spec_member_cannot_manage_lock(monkeypatch, pg_url, pg_session_factory, tmp_path):
    app = _app(monkeypatch, pg_url, tmp_path)
    owner_id, member_id, _, ws_id = _seed(app, pg_session_factory, tmp_path, "draft\n")
    _add_spec(tmp_path, ws_id, "## Aims\nx\n")
    _client(app, owner_id).post(f"/api/workspaces/{ws_id}/spec/lock")
    body = _client(app, member_id).get(f"/api/workspaces/{ws_id}/spec").json()
    assert body["can_manage_lock"] is False
    assert body["can_edit"] is False and body["locked"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_api_spec.py -k "lock_fields or manage_lock" -q`
Expected: FAIL — `KeyError: 'locked'` (field absent).

- [ ] **Step 3: Extend `SpecOut`**

Replace the `SpecOut` model (lines 104–107) with:

```python
class SpecOut(BaseModel):
    head: str
    can_edit: bool
    sections: list[SectionOut]  # no citations: the spec is citation-free
    locked: bool
    locked_at: datetime | None = None
    locked_by: str | None = None
    can_manage_lock: bool
```

- [ ] **Step 4: Populate the fields in `get_spec`**

`get_spec` currently (lines 234–255) does: `ws = require_member(...)` → `can_edit = ws.owner_id == user.id` → repo/`spec.exists()` 404 → `head = current_head(...)` (404 on `GitCommitError`) → `sections = _sections_out(spec.read_text())` → `return SpecOut(head=..., can_edit=..., sections=...)`.

Make two edits, keeping the repo-read body **unchanged**:

1. Replace the single `can_edit` assignment line with:
```python
        is_owner = ws.owner_id == user.id
        locked = ws.spec_locked_at is not None
        can_edit = is_owner and not locked
```
2. Resolve the display fields **while the session is still open** (just before the existing `return`, inside the `with sf() as session:` block) and replace the `return`:
```python
        locked_by = _locker_email(session, ws.spec_locked_by)
        locked_at = ws.spec_locked_at
    return SpecOut(
        head=head,
        can_edit=can_edit,
        sections=sections,
        locked=locked,
        locked_at=locked_at,
        locked_by=locked_by,
        can_manage_lock=is_owner,
    )
```
(`_locker_email` queries `session`, so it must run before the `with` block closes.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_api_spec.py -q`
Expected: PASS (existing GET /spec tests still green — they don't assert on the absence of the new fields).

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a
git branch --show-current   # cockpit-sp2b-2a
git add src/call_manager/web/api/workspaces.py tests/test_api_spec.py
git commit -m "feat(api): SpecOut lock read-model (can_edit folds the lock)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Frontend lock hooks + schema regen

Regenerate the typed client (the backend now emits the new `SpecOut`/`SpecLockOut` shapes and routes) and add the two mutation hooks.

**Files:**
- Modify: `frontend/src/api/schema.gen.ts` (regenerated — do not hand-edit)
- Modify: `frontend/src/api/editor.ts` (`useLockSpec`, `useUnlockSpec`)
- Test: `frontend/src/api/editor.test.tsx`

- [ ] **Step 1: Regenerate the schema**

From `frontend/` (source nvm if pnpm is missing):

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a/frontend
EMBEDDING_MODEL=bge-m3 pnpm gen:api
```

Expected: `src/api/schema.gen.ts` now contains `/api/workspaces/{workspace_id}/spec/lock` + `/unlock` paths and `SpecOut` with `locked`/`can_manage_lock`. (If `gen:api` runs a live server, follow the existing repo recipe; the `EMBEDDING_MODEL=bge-m3` prefix avoids the mxbai stdout warning that corrupts the output.)

- [ ] **Step 2: Write the failing hook tests**

Add to `frontend/src/api/editor.test.tsx`. The file already imports `renderHook`/`waitFor`, defines a `wrapper(qc)` helper, and spies via `vi.spyOn(clientMod.api, "POST")` — reuse all of them, exactly mirroring the existing `useSaveSpecSection` test (it uses `await result.current.mutateAsync(...)` + `vi.spyOn(qc, "invalidateQueries")`). Add `useLockSpec, useUnlockSpec` to the existing `from "./editor"` import. Both hooks take no mutate vars:

```tsx
it("useLockSpec POSTs /spec/lock and invalidates ['spec', id]", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { locked: true }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useLockSpec("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  expect(post).toHaveBeenCalledWith("/api/workspaces/{workspace_id}/spec/lock", {
    params: { path: { workspace_id: "ws-1" } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["spec", "ws-1"] });
});

it("useUnlockSpec POSTs /spec/unlock and invalidates ['spec', id]", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { locked: false }, error: undefined } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useUnlockSpec("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  expect(post).toHaveBeenCalledWith("/api/workspaces/{workspace_id}/spec/unlock", {
    params: { path: { workspace_id: "ws-1" } },
  });
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["spec", "ws-1"] });
});
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a/frontend
pnpm test -- src/api/editor.test.tsx
```
Expected: FAIL — `useLockSpec`/`useUnlockSpec` are not exported.

- [ ] **Step 4: Add the hooks**

Append to `frontend/src/api/editor.ts`:

```ts
/** POST /api/workspaces/{id}/spec/lock — owner locks the spec. Invalidates ['spec', id]. */
export function useLockSpec(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/spec/lock", { params: { path: { workspace_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spec", id] }),
  });
}

/** POST /api/workspaces/{id}/spec/unlock — owner unlocks the spec. Invalidates ['spec', id]. */
export function useUnlockSpec(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/workspaces/{workspace_id}/spec/unlock", { params: { path: { workspace_id: id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spec", id] }),
  });
}
```

- [ ] **Step 5: Run the tests + the type gate**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a/frontend
pnpm test -- src/api/editor.test.tsx
pnpm exec tsc --noEmit
```
Expected: tests PASS; `tsc` clean.

- [ ] **Step 6: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a
git branch --show-current   # cockpit-sp2b-2a
git add frontend/src/api/editor.ts frontend/src/api/editor.test.tsx frontend/src/api/schema.gen.ts
git commit -m "feat(spa): useLockSpec/useUnlockSpec + regen schema

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: SpecPanel lock UI + CSS

The minimal surface: a locked banner, the editor read-only (free, via `can_edit`), and an owner Lock/Unlock toggle.

> **Intentional deviation from the design spec (§3).** The design called for an owner Lock button *disabled with a hint* when the spec is whitespace-only. This plan instead shows **no lock control at all in the empty-spec branch** — that branch already renders only the "Start spec from skeleton" CTA (not the editor), so a disabled "Lock spec" button there would be locking nothing. Hiding it is cleaner UX and enforces the same invariant ("never lock an empty spec") more strictly; the backend 409 precondition remains the source of truth. The lock control therefore appears only once a spec has content (the non-empty editor branch), where it is always enabled. The SpecPanel test "the empty-spec view shows no lock control" pins this decision.

**Files:**
- Modify: `frontend/src/cockpit/SpecPanel.tsx`
- Modify: `frontend/src/styles/cockpit.css`
- Test: `frontend/src/cockpit/SpecPanel.test.tsx`

- [ ] **Step 1: Write the failing component tests**

Add to `frontend/src/cockpit/SpecPanel.test.tsx` (the file already defines `renderPanel`, `EMPTY`, `SEEDED`). Add a locked fixture and tests:

```tsx
const LOCKED = {
  data: {
    head: "c".repeat(40),
    can_edit: false,
    locked: true,
    locked_at: "2026-06-20T10:00:00Z",
    locked_by: "owner@ki.se",
    can_manage_lock: true,
    sections: [
      { index: 0, heading: "", text: "", sha: "p" },
      { index: 1, heading: "Aims", text: "## Aims\nbody\n", sha: "a" },
    ],
  },
  error: undefined,
};

it("a locked spec shows the lock banner and an Unlock control for the owner", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(LOCKED as never);
  renderPanel();
  await screen.findByText(/Spec locked by owner@ki.se/i);
  expect(screen.getByRole("button", { name: /unlock spec/i })).toBeInTheDocument();
});

it("an owner of an unlocked, non-empty spec sees a Lock control", async () => {
  // SEEDED is non-empty + can_edit true; extend it with unlocked lock fields.
  const unlocked = { data: { ...SEEDED.data, locked: false, can_manage_lock: true }, error: undefined };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(unlocked as never);
  renderPanel();
  await screen.findByRole("button", { name: /lock spec/i });
});

it("a member does not see any lock toggle", async () => {
  const memberView = { data: { ...SEEDED.data, locked: false, can_manage_lock: false }, error: undefined };
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(memberView as never);
  renderPanel();
  await screen.findByRole("tab", { name: /full spec/i });
  expect(screen.queryByRole("button", { name: /lock spec/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /unlock spec/i })).not.toBeInTheDocument();
});

it("the empty-spec view shows no lock control", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue(EMPTY as never);
  renderPanel();
  await screen.findByRole("button", { name: /start spec from skeleton/i });
  expect(screen.queryByRole("button", { name: /lock spec/i })).not.toBeInTheDocument();
});
```

> Implementer note: the existing `SEEDED`/`EMPTY` fixtures lack the new lock fields; the tests above add them where needed. If TypeScript complains about the `SEEDED.data` spread missing fields, widen the local fixtures with `locked`/`can_manage_lock` (they are mocked `as never` so runtime is unaffected, but keep `tsc` green).

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a/frontend
pnpm test -- src/cockpit/SpecPanel.test.tsx
```
Expected: FAIL — no banner text, no Lock/Unlock buttons.

- [ ] **Step 3: Implement the lock UI in `SpecPanel`**

Replace `frontend/src/cockpit/SpecPanel.tsx` with:

```tsx
import { useInitSpec, useLockSpec, useSaveSpecSection, useSpec, useUnlockSpec } from "../api/editor";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function SpecPanel({ id }: { id: string }) {
  const spec = useSpec(id);
  const save = useSaveSpecSection(id);
  const init = useInitSpec(id);
  const lock = useLockSpec(id);
  const unlock = useUnlockSpec(id);

  if (spec.isLoading) return <main aria-busy="true">Loading spec…</main>;
  if (spec.isError || !spec.data) {
    return (
      <main role="alert">
        <h1>Could not load the spec</h1>
      </main>
    );
  }
  const { head, can_edit, sections, locked, locked_at, locked_by, can_manage_lock } = spec.data;
  // Explicit skeleton CTA — no silent commit-on-open. Same whitespace-only
  // predicate the init endpoint uses, so the CTA and the server seed agree.
  const isEmpty = sections.every((s) => s.text.trim() === "");
  if (isEmpty) {
    return (
      <main className="spec-empty">
        <h1>Spec</h1>
        <p>This proposal has no spec yet.</p>
        <button type="button" disabled={!can_edit || init.isPending} onClick={() => init.mutate()}>
          Start spec from skeleton
        </button>
      </main>
    );
  }
  const lockedOn = locked_at ? new Date(locked_at).toLocaleDateString() : "";
  return (
    <div className="spec-shell">
      {locked && (
        <p className="spec-lock-banner" role="status">
          🔒 Spec locked{locked_by ? ` by ${locked_by}` : ""}{lockedOn ? ` on ${lockedOn}` : ""}
        </p>
      )}
      {can_manage_lock && (
        <div className="spec-lock-bar">
          {locked ? (
            <button type="button" disabled={unlock.isPending} onClick={() => unlock.mutate()}>
              Unlock spec
            </button>
          ) : (
            <button type="button" disabled={lock.isPending} onClick={() => lock.mutate()}>
              Lock spec
            </button>
          )}
        </div>
      )}
      <ArtifactSectionEditor
        workspaceId={id}
        queryKey={["spec", id]}
        head={head}
        canEdit={can_edit}
        sections={sections}
        save={save}
        refetch={spec.refetch}
        citations={false}
        fullViewLabel="Full spec"
      />
    </div>
  );
}
```

- [ ] **Step 4: Add the CSS**

Append to `frontend/src/styles/cockpit.css`:

```css
/* SP2b-2a: spec lock banner + owner toggle bar. */
.spec-shell {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.spec-lock-banner {
  margin: 0;
  padding: 0.4rem 0.75rem;
  border: 1px solid var(--cm-border);
  border-left: 3px solid var(--cm-accent, #2c7a7b);
  background: var(--cm-surface-sunken);
  border-radius: 6px;
  color: var(--cm-ink-strong);
  font-weight: 600;
}
.spec-lock-bar {
  display: flex;
  gap: 0.5rem;
}
```

(Use existing `--cm-*` tokens; `--cm-accent` falls back to a teal if undefined.)

- [ ] **Step 5: Run the tests + the type gate**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a/frontend
pnpm test -- src/cockpit/SpecPanel.test.tsx
pnpm exec tsc --noEmit
```
Expected: tests PASS; `tsc` clean.

- [ ] **Step 6: Run the whole frontend suite (no regressions)**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a/frontend
pnpm test
```
Expected: PASS (Cockpit.test.tsx + the rest still green).

- [ ] **Step 7: Commit**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a
git branch --show-current   # cockpit-sp2b-2a
git add frontend/src/cockpit/SpecPanel.tsx frontend/src/styles/cockpit.css frontend/src/cockpit/SpecPanel.test.tsx
git commit -m "feat(spa): SpecPanel lock banner + owner Lock/Unlock control

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after Task 7)

- [ ] Backend: re-run each touched test file in isolation (avoids the flaky whole-suite run):
  `uv run pytest tests/test_migration_0052_lifecycle_spec_lock.py tests/test_editing_save.py tests/test_api_spec.py tests/test_api_spec_lock.py tests/test_web_filters.py -q`
- [ ] Frontend: `cd frontend && pnpm test && pnpm exec tsc --noEmit`
- [ ] Schema drift: `git -C /home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2a diff --exit-code -- frontend/src/api/schema.gen.ts` (should be clean — the regen was committed in Task 6).
- [ ] Dispatch one final whole-feature adversarial code review; address findings.
- [ ] STOP before merge. Hand back to the human (finishing-a-development-branch).

---

## Self-review notes (plan author)

- **Spec coverage:** §1 schema → Task 1; §2 endpoints/guard/read-model → Tasks 3/2+4/5; §3 frontend → Tasks 6+7; §4 tests → every task's tests + the migration test. The Codex-fixed points (migration `0052`/down-rev `0051`; drop-check-before-update; refresh-under-mutex in both `save_section` and the endpoints; explicit `post_init_spec` guard) are each implemented and tested.
- **Scope expansion vs. the design:** the design assumed a small blast radius; the test surface is larger because `make_workspace` defaults `current_phase="spec"`. Task 1 handles it (flip the default + sweep), enumerated by file:line, and is the only task that cannot have a green intermediate — hence schema + sweep ship together.
- **Type consistency:** `SpecLockedError` (save.py) is imported by workspaces.py; `SpecLockOut`/`SpecOut` field names match the frontend destructure (`locked`, `locked_at`, `locked_by`, `can_manage_lock`, `can_edit`); `useLockSpec`/`useUnlockSpec` names match SpecPanel's imports; the lock event `action` values (`'lock'`/`'unlock'`) match the model CHECK.
- **Confirm-before-relying notes for implementers:** (a) `tests/test_editing_save.py`'s exact helper names for seeding a repo + reading HEAD/section-sha (the plan reuses them but names may differ); (b) that `gen:api`'s output is byte-stable so the committed `schema.gen.ts` doesn't drift. *(Resolved during planning: Alembic versions dir is repo-root `alembic/versions/`; `editor.test.tsx` already exports a `wrapper(qc)` helper + `renderHook`/`waitFor`.)*
