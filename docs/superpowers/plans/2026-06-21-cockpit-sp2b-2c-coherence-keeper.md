# SP2b-2c — Coherence-Keeper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A manual-triggered, fresh-stateless background agent pass ("the coherence-keeper") that holistically compares the workspace SPEC vs the DRAFT and emits `proposed_change` rows (`origin_kind='coherence_session'`) into the existing SP2b-2b review inboxes.

**Architecture:** A dedicated `coherence_jobs` queue (mirroring `turn_jobs`) drained by an APScheduler tick that runs one fresh-stateless Claude SDK pass via its **own** one-shot adapter (never `WorkspaceController`). The pass emits proposals via the existing propose-only tools (now provenance-aware), then deterministically supersedes the prior pass. Output is proposals-only — surfaced in the existing `<ProposalReview>` strips, gated so in-flight rows stay hidden until the job completes. No Problems panel (that's SP2b-3).

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL 16, pytest + testcontainers (`uv run pytest`). Frontend React 18 + Vite + TanStack Query + Vitest, pnpm 10.16 (Node 22 via nvm); the gate is `pnpm exec tsc --noEmit` (no TS linter); schema regen = `EMBEDDING_MODEL=bge-m3 pnpm gen:api`. All agent-pass tests use the existing `FakeAgentAdapter` (`drafting/fake_adapter.py`) — **no real Claude/network in CI.**

**Spec:** `docs/superpowers/specs/2026-06-21-cockpit-sp2b-2c-coherence-keeper-design.md`.

**Working tree:** the worktree `/home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-2c` on branch `cockpit-sp2b-2c` (off master `bc017a4`). Run all `git`/`uv`/`pnpm` there. Every commit message ends with the trailer:
```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

**Note on task order vs spec §6:** the jobs helpers (supersede / cap / reaper) are sequenced as **Task 5, before** the pass handler (Task 6) that consumes them — the correct TDD dependency order. The spec's §1.3 "reaper inside the tick" is realized as a **separate sync reaper tick** (Task 7), mirroring the proven turns subsystem (`_turn_drain_tick` + `_turn_reaper_tick`); a separate reaper is strictly better — it fires even while the drain tick is blocked inside a hung pass (the in-pass timeout, Task 6, is the primary bound; the reaper is crash/restart recovery).

---

## File Structure

**Create:**
- `src/call_manager/proposal_assist/coherence/__init__.py` — package marker
- `src/call_manager/proposal_assist/coherence/context.py` — `assemble_coherence_context`
- `src/call_manager/proposal_assist/coherence/jobs.py` — enqueue/claim/supersede/cap/reaper/latest helpers
- `src/call_manager/proposal_assist/coherence/agent.py` — keeper allowed-tools + one-shot adapter builder
- `src/call_manager/proposal_assist/coherence/pass_runner.py` — `run_coherence_pass`, `drain_coherence_queue_tick` (filename avoids the `pass` keyword)
- `src/call_manager/proposal_assist/web/coherence.py` — the two endpoints
- `alembic/versions/0053_coherence_jobs.py` — the table + indexes
- `frontend/src/api/coherence.ts` (+ `coherence.test.tsx`)
- Tests under `tests/proposal_assist/coherence/`

**Modify:**
- `src/call_manager/db/models.py` — `CoherenceJob` model
- `src/call_manager/proposal_assist/tools/registry.py` — `ToolContext.origin_kind` + `origin_job_id`
- `src/call_manager/proposal_assist/tools/proposal.py` — propose plumbing reads `ctx.origin_kind`/`ctx.origin_job_id`
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — optional `allowed_tools` constructor override
- `src/call_manager/proposal_assist/web/proposals.py` — `list_proposed_changes` visibility gate
- `src/call_manager/scheduler.py` — register `_coherence_tick` (async drain) + `_coherence_reaper_tick` (sync)
- `src/call_manager/config.py` — 4 coherence settings
- `src/call_manager/main.py` — mount the coherence router
- `frontend/src/cockpit/SpecPanel.tsx` — "Check coherence" control
- `frontend/src/api/schema.gen.ts` — regen
- `frontend/src/styles/cockpit.css` — small add

---

### Task 1: `coherence_jobs` model + migration

**Files:**
- Modify: `src/call_manager/db/models.py` (add `CoherenceJob` after `TurnJob`, ~line 882)
- Create: `alembic/versions/0053_coherence_jobs.py`
- Test: `tests/proposal_assist/coherence/test_coherence_jobs_model.py`

- [ ] **Step 1: Write the failing test**

Create `tests/proposal_assist/coherence/__init__.py` (empty) and `tests/proposal_assist/coherence/test_coherence_jobs_model.py`:

```python
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import CoherenceJob
from tests.proposal_assist.conftest import make_workspace


def _job(ws_id, status="queued"):
    return CoherenceJob(workspace_id=ws_id, status=status)


def test_active_unique_rejects_second_active_row(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(_job(ws.id, "queued"))
        s.flush()
        s.add(_job(ws.id, "running"))
        with pytest.raises(IntegrityError):
            s.flush()  # uq_coherence_jobs_active covers queued OR running


def test_enqueue_allowed_after_prior_completed(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        done = _job(ws.id, "completed")
        s.add(done)
        s.flush()
        # A new active row is fine once the prior left the active set.
        s.add(_job(ws.id, "queued"))
        s.flush()  # no IntegrityError
        assert s.query(CoherenceJob).filter_by(workspace_id=ws.id).count() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_coherence_jobs_model.py -q`
Expected: FAIL — `ImportError: cannot import name 'CoherenceJob'`.

- [ ] **Step 3: Add the model**

In `src/call_manager/db/models.py`, immediately after the `TurnJob` class (it ends ~line 882, before `class ChatSummary`), add:

```python
class CoherenceJob(Base):
    __tablename__ = "coherence_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_coherence_jobs_status",
        ),
        Index("ix_coherence_jobs_ws_queued", "workspace_id", "queued_at"),
        # Stronger than turn_jobs (which is running-only): at most ONE active
        # (queued OR running) coherence job per workspace. Enforces one-pass-at-
        # a-time AND makes the POST "create-or-return-active" race-safe.
        Index(
            "uq_coherence_jobs_active",
            "workspace_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    requested_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sdk_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    n_proposals: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
```

(All names used — `CheckConstraint`, `Index`, `text`, `UUID`, `Mapped`, `mapped_column`, `ForeignKey`, `Text`, `BigInteger`, `DateTime`, `Integer`, `func`, `datetime`, `uuid` — are already imported at the top of `models.py`.)

- [ ] **Step 4: Write the migration**

Create `alembic/versions/0053_coherence_jobs.py`:

```python
"""coherence_jobs queue

Revision ID: 0053
Revises: 0052

The coherence-keeper's background-pass queue. Mirrors turn_jobs, but the
active-unique index covers status IN ('queued','running') (turn_jobs is
running-only): at most one coherence pass per workspace may be pending+running.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coherence_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sdk_session_id", sa.Text(), nullable=True),
        sa.Column("n_proposals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_coherence_jobs_ws"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], ondelete="SET NULL", name="fk_coherence_jobs_requested_by"
        ),
    )
    op.create_check_constraint(
        "ck_coherence_jobs_status",
        "coherence_jobs",
        "status IN ('queued','running','completed','failed')",
    )
    op.create_index("ix_coherence_jobs_ws_queued", "coherence_jobs", ["workspace_id", "queued_at"])
    op.create_index(
        "uq_coherence_jobs_active",
        "coherence_jobs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_coherence_jobs_active", table_name="coherence_jobs")
    op.drop_index("ix_coherence_jobs_ws_queued", table_name="coherence_jobs")
    op.drop_table("coherence_jobs")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_coherence_jobs_model.py -q`
Expected: PASS (2 passed). The `migrated_engine` fixture runs all migrations including `0053`, so the table exists.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/0053_coherence_jobs.py tests/proposal_assist/coherence/
git commit -m "feat(coherence): coherence_jobs model + migration (active-unique queued|running)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `ToolContext` provenance fields + propose plumbing

Make the propose-only tools stamp `origin_kind`/`origin_job_id` from the context (today `_propose_artifact_edit_impl` hardcodes `"interactive_turn"`), so a coherence pass can emit `coherence_session` rows. Defaults preserve the interactive path exactly.

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`ToolContext`, ~line 42-107)
- Modify: `src/call_manager/proposal_assist/tools/proposal.py` (`_propose_artifact_edit_impl`, ~line 105-149)
- Test: `tests/proposal_assist/coherence/test_propose_provenance.py`

- [ ] **Step 1: Write the failing test**

`tests/proposal_assist/coherence/test_propose_provenance.py`:

```python
import uuid
from pathlib import Path

import pytest

from call_manager.config import Settings
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.tools.proposal import propose_edit_impl, read_proposal_impl
from call_manager.proposal_assist.tools.registry import ToolContext


def _ctx(repo_dir, session_factory, **overrides):
    return ToolContext(
        workspace_id=overrides.pop("workspace_id"),
        repo_dir=repo_dir,
        session_factory=session_factory,
        settings=Settings(),
        **overrides,
    )


def _init_repo(repo_dir: Path) -> None:
    import subprocess

    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_dir, check=True)
    (repo_dir / PROPOSAL_FILENAME).write_text("## Aims\n\nOriginal.\n")
    subprocess.run(["git", "add", PROPOSAL_FILENAME], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_dir, check=True)


@pytest.mark.asyncio
async def test_propose_defaults_to_interactive(tmp_path, pg_session_factory, _settings_env):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    _init_repo(repo)
    ctx = _ctx(repo, pg_session_factory, workspace_id=ws_id)
    await read_proposal_impl(ctx)
    await propose_edit_impl(ctx, section_index=0, new_text="## Aims\n\nReworded.\n")

    from call_manager.db.models import ProposedChange

    with pg_session_factory() as s:
        pc = s.query(ProposedChange).filter_by(workspace_id=ws_id).one()
        assert pc.origin_kind == "interactive_turn"
        assert pc.origin_job_id is None


@pytest.mark.asyncio
async def test_propose_stamps_coherence_provenance(tmp_path, pg_session_factory, _settings_env):
    from tests.proposal_assist.conftest import make_workspace

    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    _init_repo(repo)
    job_id = uuid.uuid4()
    ctx = _ctx(
        repo,
        pg_session_factory,
        workspace_id=ws_id,
        origin_kind="coherence_session",
        origin_job_id=job_id,
    )
    await read_proposal_impl(ctx)
    await propose_edit_impl(ctx, section_index=0, new_text="## Aims\n\nReworded.\n")

    from call_manager.db.models import ProposedChange

    with pg_session_factory() as s:
        pc = s.query(ProposedChange).filter_by(workspace_id=ws_id).one()
        assert pc.origin_kind == "coherence_session"
        assert pc.origin_job_id == job_id
```

(The repo uses `@pytest.mark.asyncio` with `asyncio_mode = "auto"` (`pyproject.toml:216`), matching `tests/proposal_assist/tools/test_propose_edit.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_propose_provenance.py -q`
Expected: FAIL — `TypeError: ToolContext.__init__() got an unexpected keyword argument 'origin_kind'`.

- [ ] **Step 3: Add the ToolContext fields**

In `src/call_manager/proposal_assist/tools/registry.py`, inside the `ToolContext` dataclass, after the `origin_session_id` field (the SP2a-2 provenance block, ~line 89), add:

```python
    # SP2b-2c: provenance for non-interactive (background) origins. Defaults keep
    # the interactive path stamping "interactive_turn" with no origin_job_id. The
    # coherence-keeper's tools_factory sets origin_kind="coherence_session" +
    # origin_job_id=<coherence_jobs.id> at factory time (NOT via set_turn_provenance).
    origin_kind: str = field(default="interactive_turn")
    origin_job_id: uuid.UUID | None = field(default=None)
```

- [ ] **Step 4: Plumb them through the propose path**

In `src/call_manager/proposal_assist/tools/proposal.py`, inside `_propose_artifact_edit_impl`, change the `propose_prose_edit(...)` call: replace the hardcoded `origin_kind="interactive_turn",` line with `origin_kind=ctx.origin_kind,` and add `origin_job_id=ctx.origin_job_id,` (right after it). The full call becomes:

```python
        pc = propose_prose_edit(
            session=session,
            workspace_id=ctx.workspace_id,
            section_index=section_index,
            new_text=new_text,
            base_commit_sha=head,
            base_section_sha=base_section_sha,
            target_artifact=target_artifact,
            origin_kind=ctx.origin_kind,
            origin_job_id=ctx.origin_job_id,
            origin_session_id=ctx.origin_session_id,
            triggering_turn_id=ctx.triggering_turn_id,
            triggering_prompt=ctx.triggering_prompt,
            created_by=ctx.created_by,
        )
```

(`propose_prose_edit` already accepts `origin_job_id` — `proposals/proposing.py:55`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_propose_provenance.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the interactive regression suite**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/tools/test_propose_edit.py tests/proposal_assist/test_web_proposed_changes.py -q`
Expected: PASS (defaults keep interactive behavior unchanged).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/tools/registry.py src/call_manager/proposal_assist/tools/proposal.py tests/proposal_assist/coherence/test_propose_provenance.py
git commit -m "feat(tools): ToolContext origin_kind/origin_job_id + propose plumbing (interactive default preserved)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Context assembly

A pure function that builds the keeper's instruction text from criteria + full spec + full draft + lock state. No LLM, no I/O beyond reading the two artifact files.

**Files:**
- Create: `src/call_manager/proposal_assist/coherence/__init__.py` (empty)
- Create: `src/call_manager/proposal_assist/coherence/context.py`
- Test: `tests/proposal_assist/coherence/test_context.py`

- [ ] **Step 1: Write the failing test**

`tests/proposal_assist/coherence/test_context.py`:

```python
from pathlib import Path

from call_manager.proposal_assist.coherence.context import assemble_coherence_context
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir: Path, ws_id, *, draft: str, spec: str) -> None:
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(draft)
    (repo / "proposal-spec.md").write_text(spec)


def test_assembles_criteria_spec_draft_and_lock_state(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = [{"name": "Novelty", "description": "How new", "weight": 3}]
        ws.rubric_funder_notes = "Favors translational work."
        s.commit()
        ws_id = ws.id
    _seed_repo(tmp_path, ws_id, draft="## Aims\n\nCure X.\n", spec="## Aims\n\nIntend to cure X.\n")

    with pg_session_factory() as s:
        text = assemble_coherence_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )

    assert "Novelty" in text
    assert "Favors translational work." in text
    assert "Cure X." in text  # draft
    assert "Intend to cure X." in text  # spec
    assert "unlocked" in text.lower()  # lock state surfaced (spec_locked_at is None)


def test_handles_missing_artifact_files(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    # No repo written at all.
    with pg_session_factory() as s:
        text = assemble_coherence_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )
    assert isinstance(text, str) and len(text) > 0  # no crash; empty artifacts noted
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_context.py -q`
Expected: FAIL — `ModuleNotFoundError: ...coherence.context`.

- [ ] **Step 3: Implement**

Create `src/call_manager/proposal_assist/coherence/__init__.py` (empty file), and `src/call_manager/proposal_assist/coherence/context.py`:

```python
"""Assemble the coherence-keeper's instruction: criteria + full spec + full
draft + spec-lock state. Pure (DB read + two file reads); no LLM."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.paths import workspace_repo_dir

_ROLE = (
    "You are the COHERENCE-KEEPER for a grant proposal. Compare the team's SPEC "
    "(statement of intent) against the DRAFT prose. Propose CONSERVATIVE, per-section, "
    "full-text reconciliation edits ONLY where there is genuine drift — do not restate "
    "agreement, and do not rewrite for style. Read an artifact (read_proposal / read_spec) "
    "before proposing an edit to it.\n"
)
_UNLOCKED = (
    "The spec is UNLOCKED (divergent mode): you MAY propose draft edits to realize the "
    "spec AND spec edits to capture intent the draft has grown that the spec lacks.\n"
)
_LOCKED = (
    "The spec is LOCKED (convergent mode): the spec is authoritative and frozen. Propose "
    "DRAFT edits only, to pull the draft back into agreement with the spec.\n"
)


def _read(repo_dir: Path, filename: str) -> str:
    path = repo_dir / filename
    return path.read_text() if path.exists() else ""


def assemble_coherence_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> str:
    ws = session.get(Workspace, workspace_id)
    locked = ws is not None and ws.spec_locked_at is not None
    criteria = json.dumps(ws.rubric_criteria, indent=2) if ws and ws.rubric_criteria else "[]"
    funder_notes = (ws.rubric_funder_notes if ws else None) or "(none)"
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft = _read(repo_dir, PROPOSAL_FILENAME) or "(draft is empty)"
    spec = _read(repo_dir, SPEC_FILENAME) or "(spec is empty)"

    return (
        f"{_ROLE}{_LOCKED if locked else _UNLOCKED}"
        f"\n# Funder criteria\n{criteria}\n\n# Funder notes\n{funder_notes}\n"
        f"\n# SPEC (proposal-spec.md)\n{spec}\n"
        f"\n# DRAFT (proposal.md)\n{draft}\n"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_context.py -q`
Expected: PASS (2 passed). (Confirm `workspace_repo_dir`'s signature in `proposal_assist/paths.py` — it is `workspace_repo_dir(workspaces_dir, workspace_id)`, used the same way in `staleness.py:22`.)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/coherence/__init__.py src/call_manager/proposal_assist/coherence/context.py tests/proposal_assist/coherence/test_context.py
git commit -m "feat(coherence): context assembly (criteria + spec + draft + lock state)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Adapter allowlist override + keeper one-shot adapter builder

The interactive adapter hardcodes its 22-tool allowlist. Add an optional `allowed_tools` constructor override (interactive default unchanged), and a builder that constructs the keeper's **own** one-shot adapter with a lock-aware allowlist and a coherence-stamped tool-context factory — never `WorkspaceController`.

**Files:**
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py` (`__init__` ~line 151; `_build_options_kwargs` ~line 245-281)
- Create: `src/call_manager/proposal_assist/coherence/agent.py`
- Test: `tests/proposal_assist/coherence/test_keeper_agent.py`

- [ ] **Step 1: Write the failing test**

`tests/proposal_assist/coherence/test_keeper_agent.py`:

```python
import uuid

from call_manager.config import Settings
from call_manager.proposal_assist.coherence.agent import (
    build_keeper_adapter,
    keeper_allowed_tools,
)
from call_manager.proposal_assist.drafting.contract import LaunchContext


def test_allowlist_excludes_spec_propose_when_locked():
    assert "propose_spec_edit" in keeper_allowed_tools(locked=False)
    assert "propose_edit" in keeper_allowed_tools(locked=False)
    assert "read_spec" in keeper_allowed_tools(locked=False)
    assert "propose_spec_edit" not in keeper_allowed_tools(locked=True)
    assert "propose_edit" in keeper_allowed_tools(locked=True)


def test_keeper_adapter_uses_lock_aware_allowlist(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    job_id = uuid.uuid4()
    ws_id = uuid.uuid4()
    adapter = build_keeper_adapter(
        settings=settings,
        session_factory=pg_session_factory,
        workspace_id=ws_id,
        requested_by=1,
        job_id=job_id,
        locked=True,
    )
    kwargs = adapter._build_options_kwargs(LaunchContext(workspace_id=ws_id))
    assert "propose_spec_edit" not in kwargs["allowed_tools"]
    assert "propose_edit" in kwargs["allowed_tools"]
    # No resume → fresh session.
    assert "resume" not in kwargs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_keeper_agent.py -q`
Expected: FAIL — `ModuleNotFoundError: ...coherence.agent`.

- [ ] **Step 3: Add the adapter override**

In `src/call_manager/proposal_assist/drafting/claude_adapter.py`, add an `allowed_tools` parameter to `__init__` (keyword-only, after `tool_context_sink`):

```python
    def __init__(
        self,
        *,
        model: str,
        workspaces_dir: str,
        permission_mode: str = "default",
        tools_factory: Callable[[Path, uuid.UUID], McpSdkServerConfig] | None = None,
        tool_context_sink: dict[uuid.UUID, Any] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self._model = model
        self._workspaces_dir = Path(workspaces_dir)
        self._permission_mode = permission_mode  # type: ignore[assignment]
        self._tools_factory = tools_factory
        # SP2b-2c: role-scoped allowlist override. None = the interactive default
        # (the hardcoded list below). The coherence-keeper passes a smaller,
        # lock-aware list so e.g. propose_spec_edit is unreachable when locked.
        self._allowed_tools = allowed_tools
```

(Keep the existing `self._tool_context_sink = ...` assignment below it.)

In `_build_options_kwargs`, inside the `if self._tools_factory is not None:` block, replace the hardcoded `options_kwargs["allowed_tools"] = [ ... ]` assignment with:

```python
            options_kwargs["allowed_tools"] = self._allowed_tools or [
                "read_proposal",
                "propose_edit",
                "read_spec",
                "propose_spec_edit",
                "propose_citation",
                "get_artifact_history",
                "read_artifact_at",
                "diff_artifact",
                "get_call_details",
                "get_profile",
                "list_references",
                "search_references",
                "navigate_reference",
                "read_reference",
                "read_reference_section",
                "summarize_data",
                "fetch_reference",
                "list_citations",
                "validate_citations",
                "list_unsupported_claims",
                "build_bibliography",
            ]
```

- [ ] **Step 4: Add the keeper agent builder**

Create `src/call_manager/proposal_assist/coherence/agent.py`:

```python
"""Build the coherence-keeper's OWN one-shot SDK adapter — never WorkspaceController.

The keeper gets a dedicated adapter + MCP server + ToolContext stamped with
coherence provenance at factory time (origin_kind/origin_job_id), launched with no
resume, terminated by the pass runner in a finally. This keeps it isolated from the
interactive session (no shared subprocess, no last_sdk_session_id, no chat thread).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from call_manager.proposal_assist.tools.factory_helpers import build_proposal_assist_mcp_server
from call_manager.proposal_assist.tools.registry import ToolContext

# Lock-aware: when the spec is locked, propose_spec_edit is removed so a spec edit
# is structurally unreachable. read_* are required to set the anti-clobber snapshot.
_BASE = ["read_proposal", "read_spec", "propose_edit"]


def keeper_allowed_tools(*, locked: bool) -> list[str]:
    return list(_BASE) if locked else [*_BASE, "propose_spec_edit"]


def build_keeper_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    requested_by: int | None,
    job_id: uuid.UUID,
    locked: bool,
) -> ClaudeAgentSDKAdapter:
    def tools_factory(repo_dir: Path, ws_id: uuid.UUID):
        ctx = ToolContext(
            workspace_id=ws_id,
            repo_dir=repo_dir,
            session_factory=session_factory,
            settings=settings,
            origin_kind="coherence_session",
            origin_job_id=job_id,
            created_by=requested_by,
        )
        return build_proposal_assist_mcp_server(ctx)

    return ClaudeAgentSDKAdapter(
        model=settings.proposal_assist_llm_model,
        workspaces_dir=settings.workspaces_dir,
        tools_factory=tools_factory,
        allowed_tools=keeper_allowed_tools(locked=locked),
    )
```

(Confirm the import path of `build_proposal_assist_mcp_server` — the grounding read it from `drafting/factory.py`; import it from wherever it is defined. If it lives in `drafting/factory.py`, use `from call_manager.proposal_assist.drafting.factory import build_proposal_assist_mcp_server`. The MCP server registers ALL collected tools; the allowlist is what gates which the model may call — so removing `propose_spec_edit` from the allowlist makes it unreachable even though it is registered.)

- [ ] **Step 5: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_keeper_agent.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the adapter regression**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_tools_claude_adapter_wiring.py -q`
Expected: PASS (the default allowlist is unchanged when `allowed_tools=None`).

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/drafting/claude_adapter.py src/call_manager/proposal_assist/coherence/agent.py tests/proposal_assist/coherence/test_keeper_agent.py
git commit -m "feat(coherence): adapter allowlist override + lock-aware keeper one-shot adapter

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Jobs helpers (enqueue / claim / supersede / cap / reaper / latest)

Pure-DB helpers the pass runner and endpoints compose. Easy to TDD in isolation.

**Files:**
- Create: `src/call_manager/proposal_assist/coherence/jobs.py`
- Test: `tests/proposal_assist/coherence/test_jobs.py`

- [ ] **Step 1: Write the failing test**

`tests/proposal_assist/coherence/test_jobs.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, ProposedChange
from call_manager.proposal_assist.coherence import jobs
from tests.proposal_assist.conftest import make_user, make_workspace


def _pending_coherence(s, ws_id, job_id, section=0):
    pc = ProposedChange(
        workspace_id=ws_id,
        target_artifact="draft",
        section_index=section,
        base_commit_sha="deadbeef",
        base_section_sha="cafe",
        kind="prose_edit",
        payload={"new_text": "x"},
        origin_kind="coherence_session",
        origin_job_id=job_id,
        status="pending",
    )
    s.add(pc)
    s.flush()
    return pc


def test_enqueue_create_or_return_active(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        u = make_user(s, email="req@ki.se")
        s.flush()
        first = jobs.enqueue_coherence_run(s, workspace_id=ws.id, requested_by=u.id)
        s.flush()
        again = jobs.enqueue_coherence_run(s, workspace_id=ws.id, requested_by=u.id)
        assert again.id == first.id  # active job returned, not stacked
        assert s.query(CoherenceJob).filter_by(workspace_id=ws.id).count() == 1


def test_claim_skips_workspace_with_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="running"))
        # Different workspace has a queued job that SHOULD be claimable.
        ws2 = make_workspace(s)
        s.flush()
        q = CoherenceJob(workspace_id=ws2.id, status="queued")
        s.add(q)
        s.flush()
        claimed = jobs.claim_next_coherence_job(s)
        assert claimed is not None and claimed.workspace_id == ws2.id
        assert claimed.status == "running" and claimed.started_at is not None


def test_supersede_success_targets_prior_only(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        old_job, new_job = uuid.uuid4(), uuid.uuid4()
        prior = _pending_coherence(s, ws.id, old_job, section=0)
        current = _pending_coherence(s, ws.id, new_job, section=1)
        jobs.supersede_prior_coherence(s, workspace_id=ws.id, job_id=new_job)
        s.refresh(prior)
        s.refresh(current)
        assert prior.status == "superseded"
        assert current.status == "pending"


def test_failure_cleanup_targets_own_only(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        old_job, this_job = uuid.uuid4(), uuid.uuid4()
        prior = _pending_coherence(s, ws.id, old_job, section=0)
        mine = _pending_coherence(s, ws.id, this_job, section=1)
        jobs.supersede_own_coherence(s, workspace_id=ws.id, job_id=this_job)
        s.refresh(prior)
        s.refresh(mine)
        assert mine.status == "superseded"
        assert prior.status == "pending"  # prior assessment preserved


def test_daily_cap_blocks_over_budget(pg_session_factory):
    settings = Settings()
    settings.coherence_daily_token_cap = 100
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(
            CoherenceJob(
                workspace_id=ws.id, status="completed", input_tokens=80, output_tokens=40
            )
        )
        s.flush()
        assert jobs.coherence_cap_exceeded(s, workspace_id=ws.id, settings=settings) is True


def test_reaper_fails_stale_running_and_supersedes_its_rows(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        stale = CoherenceJob(
            workspace_id=ws.id,
            status="running",
            started_at=datetime.now(UTC) - timedelta(seconds=999),
        )
        s.add(stale)
        s.flush()
        partial = _pending_coherence(s, ws.id, stale.id)
        n = jobs.reap_stuck_coherence_jobs(s, timeout_seconds=300)
        s.refresh(stale)
        s.refresh(partial)
        assert n == 1 and stale.status == "failed"
        assert partial.status == "superseded"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_jobs.py -q`
Expected: FAIL — `ModuleNotFoundError: ...coherence.jobs`.

- [ ] **Step 3: Implement**

Create `src/call_manager/proposal_assist/coherence/jobs.py`:

```python
"""Pure-DB helpers for the coherence_jobs queue. The caller owns the transaction
(these flush, never commit), mirroring proposals/proposing.py + accepting.py."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, ProposedChange
from call_manager.proposal_assist.turns.caps import _stockholm_day_start_utc


def enqueue_coherence_run(
    session: Session, *, workspace_id: uuid.UUID, requested_by: int | None
) -> CoherenceJob:
    """Create a queued job, or return the existing active (queued|running) one.

    Race-safe against the uq_coherence_jobs_active partial-unique index: on a
    concurrent-insert violation, roll back to a SAVEPOINT and re-read the winner.
    """
    existing = session.scalar(
        select(CoherenceJob).where(
            CoherenceJob.workspace_id == workspace_id,
            CoherenceJob.status.in_(("queued", "running")),
        )
    )
    if existing is not None:
        return existing
    job = CoherenceJob(workspace_id=workspace_id, status="queued", requested_by=requested_by)
    session.add(job)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # Lost the race: another request inserted the active row first.
        return session.scalar(
            select(CoherenceJob).where(
                CoherenceJob.workspace_id == workspace_id,
                CoherenceJob.status.in_(("queued", "running")),
            )
        )
    return job


def claim_next_coherence_job(session: Session) -> CoherenceJob | None:
    """Claim the oldest queued job whose workspace has no running job. Flips it to
    running + stamps started_at. FOR UPDATE SKIP LOCKED so parallel ticks don't fight."""
    running_ws = select(CoherenceJob.workspace_id).where(CoherenceJob.status == "running")
    job = session.scalar(
        select(CoherenceJob)
        .where(
            CoherenceJob.status == "queued",
            CoherenceJob.workspace_id.not_in(running_ws),
        )
        .order_by(CoherenceJob.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    session.flush()
    return job


def _supersede(session: Session, *, workspace_id: uuid.UUID, where_job, equals: bool) -> int:
    cond = (
        ProposedChange.origin_job_id == where_job
        if equals
        else ProposedChange.origin_job_id != where_job
    )
    result = session.execute(
        update(ProposedChange)
        .where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.origin_kind == "coherence_session",
            ProposedChange.status == "pending",
            cond,
        )
        .values(status="superseded")
    )
    session.flush()
    return result.rowcount or 0


def supersede_prior_coherence(session: Session, *, workspace_id: uuid.UUID, job_id: uuid.UUID) -> int:
    """On success: supersede prior passes' pending rows (origin_job_id != job_id)."""
    return _supersede(session, workspace_id=workspace_id, where_job=job_id, equals=False)


def supersede_own_coherence(session: Session, *, workspace_id: uuid.UUID, job_id: uuid.UUID) -> int:
    """On failure: supersede THIS job's partial pending rows (origin_job_id == job_id)."""
    return _supersede(session, workspace_id=workspace_id, where_job=job_id, equals=True)


def coherence_cap_exceeded(
    session: Session, *, workspace_id: uuid.UUID, settings: Settings
) -> bool:
    """True if today's (Stockholm-day) coherence in+out tokens are at/over the cap."""
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(
            func.coalesce(func.sum(CoherenceJob.input_tokens + CoherenceJob.output_tokens), 0)
        ).where(
            CoherenceJob.workspace_id == workspace_id,
            CoherenceJob.queued_at >= day_start,
        )
    )
    return int(total or 0) >= settings.coherence_daily_token_cap


def reap_stuck_coherence_jobs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running job older than the TTL and supersede
    its partial rows, unwedging the workspace's active-unique slot. Pure DB."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(CoherenceJob)
        .where(CoherenceJob.status == "running", CoherenceJob.started_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in rows:
        job.status = "failed"
        job.error = "timed out"
        job.completed_at = datetime.now(UTC)
        supersede_own_coherence(session, workspace_id=job.workspace_id, job_id=job.id)
    session.flush()
    return len(rows)


def latest_coherence_job(session: Session, *, workspace_id: uuid.UUID) -> CoherenceJob | None:
    return session.scalar(
        select(CoherenceJob)
        .where(CoherenceJob.workspace_id == workspace_id)
        .order_by(CoherenceJob.queued_at.desc())
        .limit(1)
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_jobs.py -q`
Expected: PASS (6 passed). (`_stockholm_day_start_utc` is imported from `turns/caps.py` — confirm the name; the grounding shows it defined there.)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/coherence/jobs.py tests/proposal_assist/coherence/test_jobs.py
git commit -m "feat(coherence): jobs helpers (enqueue/claim/supersede/cap/reaper/latest)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Pass runner + in-pass timeout + success/failure finalization

Ties Tasks 3-5 together: assemble context → run one fresh keeper turn (bounded by `coherence_pass_timeout_seconds`) → on success record tokens/session + supersede prior + complete; on error/timeout terminate the adapter in `finally`, fail the job, and supersede its own partial rows.

**Files:**
- Create: `src/call_manager/proposal_assist/coherence/pass_runner.py`
- Test: `tests/proposal_assist/coherence/test_pass_runner.py`

- [ ] **Step 1: Write the failing test** (uses the existing `FakeAgentAdapter` + scripted events)

`tests/proposal_assist/coherence/test_pass_runner.py`:

```python
import uuid

import pytest

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, ProposedChange
from call_manager.proposal_assist.coherence import jobs
from call_manager.proposal_assist.coherence.pass_runner import run_coherence_pass
from call_manager.proposal_assist.drafting.contract import (
    AssistantTextEvent,
    TurnDoneEvent,
)
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir, ws_id):
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text("## Aims\n\nDraft.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nSpec.\n")


def _make_job(s, ws_id):
    job = CoherenceJob(workspace_id=ws_id, status="running")
    s.add(job)
    s.flush()
    return job


@pytest.mark.asyncio
async def test_success_records_tokens_supersedes_prior_and_completes(
    tmp_path, pg_session_factory, _settings_env
):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        # a prior pass's pending row that THIS pass should supersede
        prior_job = uuid.uuid4()
        s.add(
            ProposedChange(
                workspace_id=ws.id, target_artifact="draft", section_index=0,
                base_commit_sha="x", base_section_sha="y", kind="prose_edit",
                payload={"new_text": "old"}, origin_kind="coherence_session",
                origin_job_id=prior_job, status="pending",
            )
        )
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()
    fake.script_next_turn([
        AssistantTextEvent(text="checking"),
        TurnDoneEvent(tokens_in=120, tokens_out=30, model="claude-x", session_id="sess-1"),
    ])

    await run_coherence_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: fake,
    )

    with pg_session_factory() as s:
        done = s.get(CoherenceJob, job_id)
        assert done.status == "completed"
        assert done.input_tokens == 120 and done.output_tokens == 30
        assert done.model == "claude-x" and done.sdk_session_id == "sess-1"
        prior = s.query(ProposedChange).filter_by(origin_job_id=prior_job).one()
        assert prior.status == "superseded"
    assert len(fake.terminations) == 1  # adapter closed


@pytest.mark.asyncio
async def test_failure_marks_failed_and_supersedes_own_partial(
    tmp_path, pg_session_factory, _settings_env
):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    # a partial row already emitted by THIS job before the crash
    with pg_session_factory() as s:
        s.add(
            ProposedChange(
                workspace_id=ws_id, target_artifact="draft", section_index=0,
                base_commit_sha="x", base_section_sha="y", kind="prose_edit",
                payload={"new_text": "partial"}, origin_kind="coherence_session",
                origin_job_id=job_id, status="pending",
            )
        )
        s.commit()

    class _Boom(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            raise RuntimeError("sdk exploded")
            yield  # pragma: no cover  (make it an async generator)

    fake = _Boom()
    await run_coherence_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: fake,
    )

    with pg_session_factory() as s:
        failed = s.get(CoherenceJob, job_id)
        assert failed.status == "failed" and failed.error
        partial = s.query(ProposedChange).filter_by(origin_job_id=job_id).one()
        assert partial.status == "superseded"
    assert len(fake.terminations) == 1  # terminated in finally even on error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_pass_runner.py -q`
Expected: FAIL — `ModuleNotFoundError: ...coherence.pass_runner`.

- [ ] **Step 3: Implement**

Create `src/call_manager/proposal_assist/coherence/pass_runner.py`:

```python
"""Run one coherence pass for a claimed (running) coherence_jobs row, and the
async drain tick that claims+runs one job. The pass is fresh-stateless: a dedicated
one-shot adapter, no resume, terminated in finally; output is proposed_change rows."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.db.models import CoherenceJob, Workspace
from call_manager.proposal_assist.coherence.agent import build_keeper_adapter
from call_manager.proposal_assist.coherence.context import assemble_coherence_context
from call_manager.proposal_assist.coherence.jobs import (
    claim_next_coherence_job,
    coherence_cap_exceeded,
    supersede_own_coherence,
    supersede_prior_coherence,
)
from call_manager.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent

logger = logging.getLogger(__name__)


async def run_coherence_pass(
    *,
    job_id: uuid.UUID,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_keeper_adapter,
) -> None:
    """Run the pass for an already-claimed (running) job. Never raises."""
    # 1. Read job + workspace state (lock, requested_by) in a short session.
    with session_factory() as s:
        job = s.get(CoherenceJob, job_id)
        if job is None or job.status != "running":
            return
        ws = s.get(Workspace, job.workspace_id)
        workspace_id = job.workspace_id
        requested_by = job.requested_by
        locked = ws is not None and ws.spec_locked_at is not None
        if coherence_cap_exceeded(s, workspace_id=workspace_id, settings=settings):
            _fail(s, job, "daily coherence budget reached")
            s.commit()
            return
        instruction = assemble_coherence_context(
            session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
        )

    adapter = adapter_builder(
        settings=settings,
        session_factory=session_factory,
        workspace_id=workspace_id,
        requested_by=requested_by,
        job_id=job_id,
        locked=locked,
    )
    handle = None
    done: TurnDoneEvent | None = None
    try:
        handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))

        async def _drive() -> TurnDoneEvent | None:
            last = None
            async for event in adapter.send_turn(handle, instruction):
                if isinstance(event, TurnDoneEvent):
                    last = event
            return last

        done = await asyncio.wait_for(_drive(), timeout=settings.coherence_pass_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — terminal: fail the job, never propagate
        logger.exception("coherence pass failed for job %s", job_id)
        with session_factory() as s:
            job = s.get(CoherenceJob, job_id)
            if job is not None:
                _fail(s, job, str(exc)[:500] or type(exc).__name__)
                supersede_own_coherence(s, workspace_id=workspace_id, job_id=job_id)
            s.commit()
        return
    finally:
        if handle is not None:
            try:
                await adapter.terminate(handle)
            except Exception:  # noqa: BLE001
                logger.exception("coherence adapter terminate failed for job %s", job_id)

    # 2. Success: supersede prior + record telemetry + count this pass's rows.
    with session_factory() as s:
        from sqlalchemy import func, select

        from call_manager.db.models import ProposedChange

        supersede_prior_coherence(s, workspace_id=workspace_id, job_id=job_id)
        n = s.scalar(
            select(func.count())
            .select_from(ProposedChange)
            .where(
                ProposedChange.workspace_id == workspace_id,
                ProposedChange.origin_job_id == job_id,
                ProposedChange.status == "pending",
            )
        )
        job = s.get(CoherenceJob, job_id)
        if job is not None:
            job.status = "completed"
            job.completed_at = datetime.now(UTC)
            job.n_proposals = int(n or 0)
            if done is not None:
                job.input_tokens = done.tokens_in or 0
                job.output_tokens = done.tokens_out or 0
                job.model = done.model
                job.sdk_session_id = done.session_id
        s.commit()


def _fail(session, job: CoherenceJob, message: str) -> None:
    job.status = "failed"
    job.error = message
    job.completed_at = datetime.now(UTC)
    session.flush()


async def drain_coherence_queue_tick(
    *,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_keeper_adapter,
) -> None:
    """Claim one job and run it. The reaper is a separate sync tick (Task 7)."""
    with session_factory() as s:
        job = claim_next_coherence_job(s)
        s.commit()
        job_id = job.id if job is not None else None
    if job_id is None:
        return
    await run_coherence_pass(
        job_id=job_id, session_factory=session_factory, settings=settings,
        adapter_builder=adapter_builder,
    )
```

- [ ] **Step 3b: Add an in-pass-timeout test** (append to `test_pass_runner.py`)

```python
@pytest.mark.asyncio
async def test_in_pass_timeout_fails_and_terminates(tmp_path, pg_session_factory, _settings_env):
    import asyncio

    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.coherence_pass_timeout_seconds = 0.1  # force the wait_for to trip
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _Hang(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            await asyncio.sleep(5)  # longer than the 0.1s pass timeout
            yield  # pragma: no cover

    fake = _Hang()
    await run_coherence_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: fake,
    )
    with pg_session_factory() as s:
        failed = s.get(CoherenceJob, job_id)
        assert failed.status == "failed" and failed.error
    assert len(fake.terminations) == 1  # adapter terminated in finally on timeout
```

(`coherence_pass_timeout_seconds` is an `int` field, but `Settings` is not frozen — assigning `0.1` at runtime is fine, mirroring how the other tests assign `settings.workspaces_dir`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_pass_runner.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/coherence/pass_runner.py tests/proposal_assist/coherence/test_pass_runner.py
git commit -m "feat(coherence): pass runner + in-pass timeout + success/failure finalization

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Scheduler wiring — async drain tick + sync reaper tick + settings

**Files:**
- Modify: `src/call_manager/config.py` (4 settings near `phase0_tick_seconds`, ~line 122)
- Modify: `src/call_manager/scheduler.py` (register two jobs near `_turn_drain_tick`, ~line 491)
- Test: `tests/proposal_assist/coherence/test_scheduler_wiring.py`

- [ ] **Step 1: Add the settings**

In `src/call_manager/config.py`, after `phase0_tick_seconds` (~line 122-129), add:

```python
    coherence_tick_seconds: int = Field(
        default=20,
        alias="COHERENCE_TICK_SECONDS",
        description="How often the coherence drain tick claims+runs one queued "
        "coherence pass. Manual-only, so this just bounds latency from click to run.",
    )
    coherence_pass_timeout_seconds: int = Field(
        default=180,
        alias="COHERENCE_PASS_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the keeper SDK turn. On "
        "timeout the one-shot adapter is terminated and the job is failed.",
    )
    coherence_job_timeout_seconds: int = Field(
        default=300,
        alias="COHERENCE_JOB_TIMEOUT_SECONDS",
        description="Reaper TTL: a running coherence job older than this is failed "
        "(crash/restart recovery), unwedging the workspace's active-unique slot.",
    )
    coherence_daily_token_cap: int = Field(
        default=200_000,
        alias="COHERENCE_DAILY_TOKEN_CAP",
        description="Per-workspace daily (Stockholm-day) in+out token budget for "
        "coherence passes. Separate from the chat-turn cap (which sums chat_turns).",
    )
```

- [ ] **Step 2: Write the failing test** (unit-test the tick functions directly; `app_loop=None` path uses `asyncio.run`)

`tests/proposal_assist/coherence/test_scheduler_wiring.py`:

```python
from call_manager.config import Settings
from call_manager.db.models import CoherenceJob
from call_manager.proposal_assist.coherence.pass_runner import drain_coherence_queue_tick
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from tests.proposal_assist.conftest import make_workspace


def test_drain_tick_claims_and_runs_a_queued_job(tmp_path, pg_session_factory, _settings_env):
    import asyncio

    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(CoherenceJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = ws.id
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True)
    (repo / "proposal.md").write_text("## Aims\n\nD.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nS.\n")

    fake = FakeAgentAdapter()
    fake.script_next_turn([TurnDoneEvent(tokens_in=10, tokens_out=5, model="m", session_id="s")])

    asyncio.run(
        drain_coherence_queue_tick(
            session_factory=pg_session_factory, settings=settings,
            adapter_builder=lambda **kw: fake,
        )
    )

    with pg_session_factory() as s:
        job = s.query(CoherenceJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "completed"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_scheduler_wiring.py -q`
Expected: it may already PASS (drain tick exists from Task 6). If so, this test pins the behavior; proceed to wire the scheduler. If `Settings()` rejects the new fields, that means Step 1 wasn't applied — fix and re-run.

- [ ] **Step 4: Register the two scheduler jobs**

In `src/call_manager/scheduler.py`, after the `_turn_drain_tick` registration block (the `sched.add_job(_turn_drain_tick, ...)` ending ~line 497), add:

```python
    # SP2b-2c: coherence drain tick (async — runs one fresh-stateless keeper pass).
    # Same app_loop dispatch pattern as _turn_drain_tick: the SDK client + asyncio
    # primitives are bound to the FastAPI lifespan loop, so a worker-thread
    # asyncio.run() would create a new loop and break them.
    def _coherence_tick() -> None:
        import asyncio

        from call_manager.proposal_assist.coherence.pass_runner import drain_coherence_queue_tick

        if not settings.drafting_enabled or not settings.anthropic_api_key:
            return
        coro = drain_coherence_queue_tick(session_factory=session_factory, settings=settings)
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("coherence tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=settings.coherence_pass_timeout_seconds + 60)
        except Exception:
            _logger.exception("coherence tick failed")

    sched.add_job(
        _coherence_tick,
        trigger=IntervalTrigger(seconds=settings.coherence_tick_seconds),
        id="coherence_drain",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-2c: coherence reaper tick (sync, pure DB). Runs even while _coherence_tick
    # is blocked inside a live pass, so a crashed/hung running job can't wedge the
    # workspace's active-unique slot forever. Mirrors _turn_reaper_tick.
    def _coherence_reaper_tick() -> None:
        from call_manager.proposal_assist.coherence.jobs import reap_stuck_coherence_jobs

        try:
            with session_factory() as s:
                reap_stuck_coherence_jobs(
                    s, timeout_seconds=settings.coherence_job_timeout_seconds
                )
                s.commit()
        except Exception:
            _logger.exception("coherence reaper tick failed")

    sched.add_job(
        _coherence_reaper_tick,
        trigger=IntervalTrigger(seconds=settings.coherence_job_timeout_seconds),
        id="coherence_reaper",
        max_instances=1,
        coalesce=True,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_scheduler_wiring.py -q`
Expected: PASS (1 passed).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/config.py src/call_manager/scheduler.py tests/proposal_assist/coherence/test_scheduler_wiring.py
git commit -m "feat(coherence): scheduler async drain tick + sync reaper tick + settings

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: List-visibility gate — hide in-flight coherence rows

A `coherence_session` row is listed only if its `coherence_jobs` row is `completed` — otherwise a half-complete pass would flash into `<ProposalReview>`.

**Files:**
- Modify: `src/call_manager/proposal_assist/web/proposals.py` (`list_proposed_changes`, ~line 47-69)
- Test: `tests/proposal_assist/coherence/test_list_visibility.py`

- [ ] **Step 1: Write the failing test**

`tests/proposal_assist/coherence/test_list_visibility.py`:

```python
from call_manager.db.models import CoherenceJob, ProposedChange
from tests.proposal_assist.conftest import make_workspace


def _coherence_row(s, ws_id, job_id):
    # target_artifact="spec" + NO proposal-spec.md on disk: refresh_staleness skips
    # rows whose artifact file is absent (staleness.py), so these stay 'pending' and
    # the ONLY filter exercised is the §3.3 visibility gate — not staleness.
    pc = ProposedChange(
        workspace_id=ws_id, target_artifact="spec", section_index=0,
        base_commit_sha="x", base_section_sha="y", kind="prose_edit",
        payload={"new_text": "z"}, origin_kind="coherence_session",
        origin_job_id=job_id, status="pending",
    )
    s.add(pc)
    s.flush()
    return pc


def test_running_coherence_rows_hidden_completed_shown(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    # No repo files written: with proposal.md AND proposal-spec.md absent,
    # refresh_staleness is a no-op (absent artifact -> skip), isolating the gate.
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        running = CoherenceJob(workspace_id=ws.id, status="running")
        completed = CoherenceJob(workspace_id=ws.id, status="completed")
        s.add_all([running, completed])
        s.flush()
        _coherence_row(s, ws.id, running.id)   # hidden (job running)
        _coherence_row(s, ws.id, completed.id)  # shown (job completed)
        s.commit()
        ws_id = str(ws.id)

    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes?target_artifact=spec")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1  # only the completed-job row


def test_interactive_rows_always_shown(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        # an interactive (non-coherence) spec row is never gated by job status
        s.add(
            ProposedChange(
                workspace_id=ws.id, target_artifact="spec", section_index=0,
                base_commit_sha="x", base_section_sha="y", kind="prose_edit",
                payload={"new_text": "z"}, origin_kind="interactive_turn", status="pending",
            )
        )
        s.commit()
        ws_id = str(ws.id)
    resp = client_authed_owner.get(f"/api/workspaces/{ws_id}/proposed-changes?target_artifact=spec")
    assert len(resp.json()["items"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_list_visibility.py -q`
Expected: `test_running_coherence_rows_hidden_completed_shown` FAILS — both coherence rows returned (len == 2) because the running row isn't hidden yet. (`test_interactive_rows_always_shown` already passes — it's a guard that the gate doesn't over-filter.)

- [ ] **Step 3: Implement the gate**

In `src/call_manager/proposal_assist/web/proposals.py`, add `CoherenceJob` to the model imports, then in `list_proposed_changes`, after the existing `.where(... status == "pending")` and before `.order_by(...)`, add a predicate so a coherence row shows only when a completed job matches its `origin_job_id`:

```python
    from sqlalchemy import exists  # add to the top-level imports

    stmt = stmt.where(
        (ProposedChange.origin_kind != "coherence_session")
        | exists().where(
            CoherenceJob.id == ProposedChange.origin_job_id,
            CoherenceJob.status == "completed",
        )
    )
```

(Place it as an additional `.where(...)` on the existing `stmt`. Non-coherence rows pass the left disjunct unchanged; coherence rows require a completed matching job.)

- [ ] **Step 4: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_list_visibility.py -q`
Expected: PASS.

- [ ] **Step 5: Run the SP2b-2b inbox regression**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/test_web_proposed_changes.py -q`
Expected: PASS (interactive/human rows are untouched by the predicate).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/proposals.py tests/proposal_assist/coherence/test_list_visibility.py
git commit -m "feat(coherence): hide in-flight coherence proposals until their job completes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Endpoints (`web/coherence.py`) with exact gate codes

**Files:**
- Create: `src/call_manager/proposal_assist/web/coherence.py`
- Modify: `src/call_manager/main.py` (mount the router near the proposals router, ~line 530)
- Test: `tests/proposal_assist/coherence/test_web_coherence.py`

- [ ] **Step 1: Write the failing test**

`tests/proposal_assist/coherence/test_web_coherence.py`:

```python
from call_manager.db.models import CoherenceJob
from tests.proposal_assist.conftest import make_user, make_workspace


def test_post_create_or_return_active(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    _app.state.settings.drafting_enabled = True
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)  # active phase + ack
        s.commit()
        ws_id = str(ws.id)

    r1 = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r1.status_code == 200, r1.text
    job_id = r1.json()["id"]
    r2 = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r2.json()["id"] == job_id  # returned active, not stacked


def test_setup_phase_returns_409(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, current_phase="setup", ack_disclosure=True)
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r.status_code == 409


def test_drafting_disabled_returns_503(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    _app.state.settings.drafting_enabled = False
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=True)
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r.status_code == 503


def test_missing_disclosure_returns_403(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    _app.state.settings.drafting_enabled = True
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=False)
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r.status_code == 403


def test_latest_returns_null_then_job(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.commit()
        ws_id = str(ws.id)
    r0 = client_authed_owner.get(f"/api/workspaces/{ws_id}/coherence-runs/latest")
    assert r0.status_code == 200 and r0.json() is None
    with pg_session_factory() as s:
        s.add(CoherenceJob(workspace_id=ws_id, status="completed", n_proposals=2))  # type: ignore[arg-type]
        s.commit()
    r1 = client_authed_owner.get(f"/api/workspaces/{ws_id}/coherence-runs/latest")
    assert r1.json()["status"] == "completed" and r1.json()["n_proposals"] == 2


def test_non_member_403(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        stranger = make_user(s, email="stranger2@ki.se")
        ws = make_workspace(s, owner=stranger, ack_disclosure=True)
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/coherence-runs")
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_web_coherence.py -q`
Expected: FAIL — 404s (router not mounted).

- [ ] **Step 3: Implement the router**

Create `src/call_manager/proposal_assist/web/coherence.py`:

```python
"""Member-gated manual coherence-pass endpoints. POST enqueues (create-or-return-
active); GET latest is polled by the SPA. Cloud-LLM gates mirror the interactive
turns route (web/workspaces.py:779-814) with the SAME status codes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import Workspace, WorkspaceEvent
from call_manager.proposal_assist.coherence.jobs import (
    enqueue_coherence_run,
    latest_coherence_job,
)
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/api/workspaces", tags=["coherence"])


def _serialize(job) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "n_proposals": job.n_proposals,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _gate_cloud_llm(*, ws: Workspace, request: Request, session: Session, user) -> None:
    # Auth-first: require_member already ran. Then mirror web/workspaces.py:779-814.
    if ws.current_phase == "setup":
        raise HTTPException(status_code=409, detail="Workspace is still setting up.")
    if not request.app.state.settings.drafting_enabled:
        raise HTTPException(status_code=503, detail="Drafting is currently disabled.")
    prior_ack = session.scalar(
        select(WorkspaceEvent.id).where(
            WorkspaceEvent.workspace_id == ws.id,
            WorkspaceEvent.kind == "disclosure_acknowledged",
            WorkspaceEvent.actor_id == user.id,
        ).limit(1)
    )
    if prior_ack is None:
        raise HTTPException(
            status_code=403, detail="Please acknowledge the data-disclosure notice."
        )


@router.post("/{workspace_id}/coherence-runs")
def start_coherence_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    _gate_cloud_llm(ws=ws, request=request, session=session, user=user)
    job = enqueue_coherence_run(session, workspace_id=workspace_id, requested_by=user.id)
    session.commit()
    return _serialize(job)


@router.get("/{workspace_id}/coherence-runs/latest")
def latest_coherence_run(
    workspace_id: uuid.UUID,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    require_member(workspace_id=workspace_id, user=user, session=session)
    job = latest_coherence_job(session, workspace_id=workspace_id)
    session.commit()
    return _serialize(job) if job is not None else None
```

In `src/call_manager/main.py`, near the `app.include_router(proposals_router)` line (~530), add an import alongside the proposals router import (~line 41) and mount it:

```python
from call_manager.proposal_assist.web.coherence import router as coherence_router
...
app.include_router(coherence_router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/test_web_coherence.py -q`
Expected: PASS (6 passed). (Confirm `require_member` import path is `proposal_assist/web/_perms.py` and `_session_dep` is in `web/workspaces.py` — both used by `web/proposals.py`.)

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/web/coherence.py src/call_manager/main.py tests/proposal_assist/coherence/test_web_coherence.py
git commit -m "feat(coherence): POST coherence-runs (gated) + GET latest endpoints

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: Frontend — hooks + Spec-panel control + schema regen

**Files:**
- Create: `frontend/src/api/coherence.ts` (+ `frontend/src/api/coherence.test.tsx`)
- Modify: `frontend/src/cockpit/SpecPanel.tsx` (Check-coherence control)
- Modify: `frontend/src/api/schema.gen.ts` (regen)
- Modify: `frontend/src/styles/cockpit.css` (small add)

- [ ] **Step 1: Regenerate the OpenAPI types** (the two endpoints must exist in the schema)

Run (from `frontend/`, Node 22 via nvm):
```bash
EMBEDDING_MODEL=bge-m3 pnpm gen:api
```
Confirm `frontend/src/api/schema.gen.ts` now contains `"/api/workspaces/{workspace_id}/coherence-runs"` and `.../coherence-runs/latest`.

- [ ] **Step 2: Write the failing hook test**

`frontend/src/api/coherence.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "./client";
import { useStartCoherence } from "./coherence";

afterEach(() => vi.restoreAllMocks());

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}

it("useStartCoherence POSTs and invalidates both inboxes on completed", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { id: "j1", status: "completed", n_proposals: 1, error: null,
            started_at: null, completed_at: null },
    error: undefined,
  } as never);
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const { result } = renderHook(() => useStartCoherence("ws-1"), { wrapper: wrapper(qc) });
  await result.current.mutateAsync();
  await waitFor(() => {
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "spec"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["proposed-changes", "ws-1", "draft"] });
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run (from `frontend/`): `pnpm exec vitest run src/api/coherence.test.tsx`
Expected: FAIL — cannot resolve `./coherence`.

- [ ] **Step 4: Implement the hooks**

`frontend/src/api/coherence.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type CoherenceRun = {
  id: string;
  status: "queued" | "running" | "completed" | "failed";
  n_proposals: number;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
};

/** GET latest coherence run (or null). Polls while queued/running. */
export function useLatestCoherence(id: string) {
  return useQuery({
    queryKey: ["coherence-latest", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/coherence-runs/latest", {
          params: { path: { workspace_id: id } },
        }),
      ) as CoherenceRun | null,
    refetchInterval: (q) => {
      const s = (q.state.data as CoherenceRun | null)?.status;
      return s === "queued" || s === "running" ? 1500 : false;
    },
  });
}

/** POST a coherence run (create-or-return-active). Refresh both inboxes when it completes. */
export function useStartCoherence(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/coherence-runs", {
          params: { path: { workspace_id: id } },
        }),
      ) as CoherenceRun,
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["coherence-latest", id] });
      if (run.status === "completed") {
        qc.invalidateQueries({ queryKey: ["proposed-changes", id, "spec"] });
        qc.invalidateQueries({ queryKey: ["proposed-changes", id, "draft"] });
      }
    },
  });
}
```

(Polling: when the GET-latest poll observes a `completed` transition, the SpecPanel control — Step 6 — also invalidates the inboxes; the `onSuccess` above covers the synchronous create-or-return-active case where a prior run already completed.)

- [ ] **Step 5: Run hook test to verify it passes**

Run (from `frontend/`): `pnpm exec vitest run src/api/coherence.test.tsx`
Expected: PASS.

- [ ] **Step 6: Add the Spec-panel control**

In `frontend/src/cockpit/SpecPanel.tsx`, import the hooks and render a control in the spec header (next to the lock bar). Add near the top:

```tsx
import { useLatestCoherence, useStartCoherence } from "../api/coherence";
```

Inside `SpecPanel`, after the existing `const unlock = ...` hook calls, add:

```tsx
  const coherence = useLatestCoherence(id);
  const startCoherence = useStartCoherence(id);
  const runStatus = coherence.data?.status;
  const checking = runStatus === "queued" || runStatus === "running";
```

Then, inside the returned `<div className="spec-shell">` (the non-empty branch), just before `<ProposalReview ... />`, add:

```tsx
      <div className="coherence-bar">
        <button
          type="button"
          disabled={checking || startCoherence.isPending}
          onClick={() => startCoherence.mutate()}
        >
          {checking ? "Checking coherence…" : "Check coherence"}
        </button>
        {runStatus === "completed" && coherence.data && (
          <span className="coherence-status" role="status">
            Last check: {coherence.data.n_proposals} proposal(s)
          </span>
        )}
        {runStatus === "failed" && (
          <span className="coherence-status coherence-error" role="status">
            {coherence.data?.error ?? "Coherence check failed."}
          </span>
        )}
      </div>
```

Add a matching effect so that when a poll observes `completed`, both inboxes refresh (in case the run completed via the background tick rather than the POST response). After the hook calls, add:

```tsx
  const qc = useQueryClient();
  useEffect(() => {
    if (runStatus === "completed") {
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, "spec"] });
      qc.invalidateQueries({ queryKey: ["proposed-changes", id, "draft"] });
    }
  }, [runStatus, id, qc]);
```

(Add `import { useEffect } from "react";` and `import { useQueryClient } from "@tanstack/react-query";` at the top.)

- [ ] **Step 7: Add minimal CSS**

In `frontend/src/styles/cockpit.css`, append:

```css
.coherence-bar { display: flex; align-items: center; gap: 0.75rem; margin: 0.5rem 0; }
.coherence-status { font-size: 0.85rem; color: var(--muted, #555); }
.coherence-error { color: var(--error, #b00020); }
```

- [ ] **Step 8: Type-check + run the frontend suite**

Run (from `frontend/`):
```bash
pnpm exec tsc --noEmit
pnpm exec vitest run
```
Expected: `tsc` exit 0; vitest all green (the new test + existing SpecPanel tests — if a SpecPanel test now needs a `useLatestCoherence` mock to avoid a real fetch, mock `api.GET` to resolve `{ data: null }` in that test's setup, matching the existing mock pattern).

- [ ] **Step 9: Commit**

```bash
git add frontend/src/api/coherence.ts frontend/src/api/coherence.test.tsx frontend/src/cockpit/SpecPanel.tsx frontend/src/styles/cockpit.css frontend/src/api/schema.gen.ts
git commit -m "feat(spa): coherence hooks + Spec-panel Check-coherence control

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] Backend targeted suite:
```bash
EMBEDDING_MODEL=bge-m3 uv run pytest tests/proposal_assist/coherence/ tests/proposal_assist/test_web_proposed_changes.py tests/proposal_assist/tools/test_propose_edit.py tests/proposal_assist/test_tools_claude_adapter_wiring.py -q
```
Expected: all green. (Re-run any connection-refused failure in isolation — the full suite is testcontainer-flaky.)
- [ ] `EMBEDDING_MODEL=bge-m3 uv run ruff check src tests` → clean.
- [ ] Frontend (from `frontend/`): `pnpm exec tsc --noEmit` (exit 0) + `pnpm exec vitest run` (green).
- [ ] No schema drift: re-run `EMBEDDING_MODEL=bge-m3 pnpm gen:api` → `git diff --quiet frontend/src/api/schema.gen.ts`.
