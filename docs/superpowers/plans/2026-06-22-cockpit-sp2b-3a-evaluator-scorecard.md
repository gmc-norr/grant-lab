# Cockpit SP2b-3a — Continuous Evaluator + Scorecard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A manual-triggered background AI pass that scores a proposal's draft against each funder criterion (Strong/Adequate/At-risk/Gap + note + deep-link) with a server-computed overall readiness band, surfaced as a third **Scorecard** cockpit tab.

**Architecture:** Copy the SP2b-2c coherence-keeper substrate into a parallel `evaluator/` package + `evaluator_jobs` table. The agent fills a structured `submit_scorecard` tool (its only tool); the server validates index-based coverage against the rubric, computes a deterministic weighted overall band with a gap floor, and stores the scorecard JSONB on the job row. The frontend reads the latest completed job's scorecard.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.x · Alembic · PostgreSQL 16 (+JSONB) · pytest (+testcontainers) · React 18 + TanStack Query + openapi-fetch · Vitest.

**Spec:** `docs/superpowers/specs/2026-06-22-cockpit-sp2b-3a-evaluator-scorecard-design.md` (read §2.3 / §2.4 / §3 before Tasks 5/8/9).

---

## Project Conventions Reference

- **Worktree / branch:** all work in `/home/parlar_ai/dev/call-monitor/.worktrees/cockpit-sp2b-3` on branch `cockpit-sp2b-3` (off master `e145f84`). Every git command must run there (`git -C <worktree>` if you are a subagent invoked from the main tree) — verify `git branch --show-current` prints `cockpit-sp2b-3` before committing.
- **Run backend tests:** `uv run pytest <path> -q`. The **whole** suite is testcontainer-flaky (`connection refused` under load) — run the affected files, capture the exit code (`... ; echo $?`), never pipe through `tail` (it masks pytest's exit code). New tests live under `tests/proposal_assist/evaluator/` (create `__init__.py`).
- **Lint/format:** `uv run ruff check <files>` + `uv run ruff format <files>` before every commit.
- **Frontend:** `cd frontend`, Node 22 via nvm, `pnpm install` after pull. Type gate (no linter): `pnpm exec tsc --noEmit`. Tests: `pnpm test`. **Regenerate the OpenAPI types after the endpoints land:** `EMBEDDING_MODEL=bge-m3 pnpm gen:api` (the `.env` `mxbai` model logs a WARNING to stdout that corrupts the generated file — the `bge-m3` override avoids it).
- **The coherence-keeper is the template.** Mirror its files exactly and carry forward every SP2b-2c hardening: the **atomic `UPDATE ... WHERE status IN (...) RETURNING` cancel** (not ORM load-then-write), **launch+drive bounded together in one `asyncio.wait_for`** (launch INSIDE it), a **bounded `terminate`** wait_for, the **`FOR UPDATE` + `status=='running'` finalize guard**, and the separate **sync reaper**.
- **Commit trailer:** end every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Differences from coherence to keep straight:** the evaluator (a) outputs ONE scorecard on the job row, not inbox rows → **no `supersede_*` logic**; (b) has a **single tool** (`submit_scorecard`), no lock-awareness; (c) finalize **requires a scorecard was written** (else fail); (d) captures **per-artifact content hashes + rubric timestamp** for staleness.

---

## File Structure

**New (backend):**
- `alembic/versions/0054_evaluator_jobs.py` — the table migration.
- `src/call_manager/proposal_assist/evaluator/__init__.py` — empty package marker.
- `src/call_manager/proposal_assist/evaluator/scoring.py` — pure deterministic roll-up (band ordinals, thresholds, gap floor).
- `src/call_manager/proposal_assist/evaluator/context.py` — instruction assembly (criteria+spec+draft, indexed; reads each artifact once and returns its content `content_sha`).
- `src/call_manager/proposal_assist/evaluator/jobs.py` — queue helpers (enqueue/claim/cancel/reap/cap/latest).
- `src/call_manager/proposal_assist/evaluator/agent.py` — one-shot adapter factory.
- `src/call_manager/proposal_assist/evaluator/pass_runner.py` — run one pass + async drain tick.
- `src/call_manager/proposal_assist/tools/scorecard.py` — `submit_scorecard` tool impl.
- `src/call_manager/proposal_assist/web/evaluator.py` — POST/GET endpoints.

**Modified (backend):**
- `src/call_manager/db/models.py` — `EvaluatorJob` model.
- `src/call_manager/config.py` — six `evaluator_*` settings.
- `src/call_manager/proposal_assist/tools/registry.py` — `ToolContext` section-count fields + register `submit_scorecard`.
- `src/call_manager/scheduler.py` — `_evaluator_tick` + `_evaluator_reaper_tick`.
- `src/call_manager/main.py` — mount the evaluator router.
- `src/call_manager/proposal_assist/web/workspaces.py` — soft-delete cancels evaluator jobs.

**New (frontend):**
- `frontend/src/api/evaluator.ts` — hooks.
- `frontend/src/cockpit/ScorecardPanel.tsx` — the tab.
- `frontend/src/cockpit/ScorecardPanel.test.tsx` — vitest.

**Modified (frontend):**
- `frontend/src/routes/Cockpit.tsx` — third view tab.
- `frontend/src/styles/cockpit.css` — band-pill styles.
- `frontend/src/api/schema.gen.ts` — regenerated.

**New (tests):** `tests/proposal_assist/evaluator/{__init__.py,test_evaluator_jobs_model.py,test_context.py,test_scoring.py,test_submit_scorecard.py,test_jobs.py,test_evaluator_agent.py,test_pass_runner.py,test_web_evaluator.py,test_scheduler_wiring.py,test_soft_delete_cancels_evaluator.py}`.

---

## Task 1: `EvaluatorJob` model + migration 0054

**Files:**
- Modify: `src/call_manager/db/models.py` (add `EvaluatorJob` after `CoherenceJob`, ~line 921)
- Create: `alembic/versions/0054_evaluator_jobs.py`
- Create: `tests/proposal_assist/evaluator/__init__.py` (empty)
- Test: `tests/proposal_assist/evaluator/test_evaluator_jobs_model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_evaluator_jobs_model.py
import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import EvaluatorJob
from tests.proposal_assist.conftest import make_workspace


def test_active_unique_rejects_second_active_job(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        s.commit()
        s.add(EvaluatorJob(workspace_id=ws.id, status="running"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_completed_does_not_block_a_new_active(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="completed", scorecard={"criteria": []}))
        s.commit()
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        s.commit()  # no IntegrityError — completed is outside the partial index
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_evaluator_jobs_model.py -q ; echo $?`
Expected: FAIL — `ImportError: cannot import name 'EvaluatorJob'`.

- [ ] **Step 3: Add the model** to `src/call_manager/db/models.py` immediately after the `CoherenceJob` class (after line 921). Mirrors `CoherenceJob` with `n_proposals` dropped and the scorecard/staleness columns added. (`JSONB`, `text`, `CheckConstraint`, `Index`, `ForeignKey`, `Text`, `Integer`, `BigInteger`, `DateTime`, `func`, `UUID`, `uuid`, `datetime` are all already imported in this file.)

```python
class EvaluatorJob(Base):
    __tablename__ = "evaluator_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_evaluator_jobs_status",
        ),
        Index("ix_evaluator_jobs_ws_queued", "workspace_id", "queued_at"),
        # At most ONE active (queued OR running) evaluator job per workspace —
        # makes POST "create-or-return-active" race-safe. Independent of the
        # coherence_jobs slot (separate table): a workspace may run one of each.
        Index(
            "uq_evaluator_jobs_active",
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
    # Written by the submit_scorecard tool while running; read only when completed.
    scorecard: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Per-artifact CONTENT hashes (not the shared repo HEAD) + rubric timestamp,
    # captured at run start so /scorecard can flag draft / spec / rubric staleness
    # independently.
    evaluated_draft_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated_spec_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated_rubric_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sdk_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
```

> **Verify before writing:** confirm `JSONB` is imported at the top of `models.py` (it backs `Workspace.rubric_criteria` at line 686). If the import is `from sqlalchemy.dialects.postgresql import UUID` only, extend it to `import ... UUID, JSONB`.

- [ ] **Step 4: Create the migration** `alembic/versions/0054_evaluator_jobs.py`:

```python
"""evaluator_jobs queue

Revision ID: 0054
Revises: 0053

The continuous-evaluator's background-pass queue. Mirrors coherence_jobs
(active-unique over status IN ('queued','running')) but drops n_proposals and
adds the scorecard JSONB + per-artifact staleness markers.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluator_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("scorecard", JSONB(), nullable=True),
        sa.Column("evaluated_draft_sha", sa.Text(), nullable=True),
        sa.Column("evaluated_spec_sha", sa.Text(), nullable=True),
        sa.Column("evaluated_rubric_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sdk_session_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE", name="fk_evaluator_jobs_ws"
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], ondelete="SET NULL",
            name="fk_evaluator_jobs_requested_by",
        ),
    )
    op.create_check_constraint(
        "ck_evaluator_jobs_status",
        "evaluator_jobs",
        "status IN ('queued','running','completed','failed')",
    )
    op.create_index("ix_evaluator_jobs_ws_queued", "evaluator_jobs", ["workspace_id", "queued_at"])
    op.create_index(
        "uq_evaluator_jobs_active",
        "evaluator_jobs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_evaluator_jobs_active", table_name="evaluator_jobs")
    op.drop_index("ix_evaluator_jobs_ws_queued", table_name="evaluator_jobs")
    op.drop_table("evaluator_jobs")
```

Also create the empty `tests/proposal_assist/evaluator/__init__.py`.

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_evaluator_jobs_model.py -q ; echo $?`
Expected: PASS (2 passed). The testcontainer fixture runs migrations, so `evaluator_jobs` exists.

Then confirm a single Alembic head:
Run: `uv run alembic heads` → Expected: `0054 (head)`.

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/db/models.py alembic/versions/0054_evaluator_jobs.py \
        tests/proposal_assist/evaluator/__init__.py \
        tests/proposal_assist/evaluator/test_evaluator_jobs_model.py
git commit -m "feat(evaluator): EvaluatorJob model + migration 0054"
```

---

## Task 2: `evaluator_*` config settings

**Files:**
- Modify: `src/call_manager/config.py` (after the `coherence_terminate_timeout_seconds` field, ~line 251)
- Test: `tests/proposal_assist/evaluator/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_config.py
from call_manager.config import Settings


def test_evaluator_settings_defaults(_settings_env):
    s = Settings()
    assert s.evaluator_tick_seconds == 20
    assert s.evaluator_pass_timeout_seconds == 180
    assert s.evaluator_job_timeout_seconds == 300
    assert s.evaluator_reaper_tick_seconds == 60
    assert s.evaluator_terminate_timeout_seconds == 30
    assert s.evaluator_daily_token_cap == 200_000
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_config.py -q ; echo $?`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'evaluator_tick_seconds'`.

- [ ] **Step 3: Add the settings** to `src/call_manager/config.py` immediately after the `coherence_terminate_timeout_seconds` Field:

```python
    evaluator_tick_seconds: int = Field(
        default=20,
        alias="EVALUATOR_TICK_SECONDS",
        description="How often the evaluator drain tick claims+runs one queued evaluator "
        "pass. Manual-only, so this just bounds latency from click to run.",
    )
    evaluator_pass_timeout_seconds: int = Field(
        default=180,
        alias="EVALUATOR_PASS_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the evaluator SDK turn (launch+drive). "
        "On timeout the one-shot adapter is terminated and the job is failed.",
    )
    evaluator_job_timeout_seconds: int = Field(
        default=300,
        alias="EVALUATOR_JOB_TIMEOUT_SECONDS",
        description="Reaper TTL (age cutoff): a running evaluator job older than this is failed "
        "(crash/restart recovery), unwedging the workspace's active-unique slot.",
    )
    evaluator_reaper_tick_seconds: int = Field(
        default=60,
        alias="EVALUATOR_REAPER_TICK_SECONDS",
        description="How often the evaluator reaper sweep RUNS (decoupled from the TTL age "
        "cutoff above), capping worst-case unwedge at ~TTL + this interval.",
    )
    evaluator_terminate_timeout_seconds: int = Field(
        default=30,
        alias="EVALUATOR_TERMINATE_TIMEOUT_SECONDS",
        description="In-pass asyncio.wait_for bound on the one-shot evaluator adapter's "
        "terminate() cleanup. A hung SDK disconnect must not leak the pass coroutine.",
    )
    evaluator_daily_token_cap: int = Field(
        default=200_000,
        alias="EVALUATOR_DAILY_TOKEN_CAP",
        description="Per-workspace daily ceiling on combined input+output tokens spent by "
        "evaluator passes (Europe/Stockholm day). A pass is blocked pre-run once reached.",
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_config.py -q ; echo $?` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/config.py tests/proposal_assist/evaluator/test_config.py
git commit -m "feat(evaluator): evaluator_* config settings"
```

---

## Task 3: `evaluator/context.py` — instruction assembly + content-hash helper

**Files:**
- Create: `src/call_manager/proposal_assist/evaluator/__init__.py` (empty)
- Create: `src/call_manager/proposal_assist/evaluator/context.py`
- Test: `tests/proposal_assist/evaluator/test_context.py`

> **Verify before writing:** the `Section` dataclass from `split_sections` (`editing/sectionizer.py:43`) exposes `.index` and `.text` (the editor renders section text; `proposal.py:74` uses `.index`/`.sha`). Confirm `.text` is the attribute name; if it differs (e.g. `.body`), adjust `_render_sections`.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_context.py
from pathlib import Path

from call_manager.proposal_assist.evaluator.context import (
    assemble_evaluator_context,
    content_sha,
)
from tests.proposal_assist.conftest import make_workspace


def _seed(tmp_path, ws_id, draft, spec):
    repo = tmp_path / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(draft)
    (repo / "proposal-spec.md").write_text(spec)
    return repo


def test_assembles_criteria_sections_and_counts(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = [
            {"name": "Scientific quality", "description": "Rigour", "weight": 0.6},
            {"name": "Impact", "description": "Reach", "weight": 0.4},
        ]
        ws.rubric_funder_notes = "Single-PI only."
        s.flush()
        ws_id = ws.id
        repo = _seed(tmp_path, ws_id, "## Aims\n\nA draft.\n", "## Aims\n\nThe spec.\n")
        ctx = assemble_evaluator_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )
    assert "[0] Scientific quality" in ctx.instruction
    assert "[1] Impact" in ctx.instruction
    assert "Single-PI only." in ctx.instruction
    assert "[§0]" in ctx.instruction and "[§1]" in ctx.instruction  # index labels
    assert "A draft." in ctx.instruction and "The spec." in ctx.instruction
    # split_sections yields section 0 (empty preamble) + section 1 (## Aims): count 2.
    assert ctx.draft_section_count == 2 and ctx.spec_section_count == 2
    # The returned sha hashes the SAME bytes the endpoint will re-hash.
    assert ctx.draft_sha == content_sha(repo / "proposal.md")
    assert ctx.spec_sha == content_sha(repo / "proposal-spec.md")


def test_empty_rubric_and_missing_files(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = []
        s.flush()
        ctx = assemble_evaluator_context(
            session=s, workspace_id=ws.id, workspaces_dir=str(tmp_path)
        )
    assert "no criteria" in ctx.instruction.lower()
    assert "(draft is empty)" in ctx.instruction
    # missing files -> 0 sections, '' hashes
    assert ctx.draft_section_count == 0 and ctx.spec_section_count == 0
    assert ctx.draft_sha == "" and ctx.spec_sha == ""


def test_content_sha_changes_with_content(tmp_path):
    p = tmp_path / "f.md"
    p.write_text("one")
    a = content_sha(p)
    p.write_text("two")
    assert a != content_sha(p)
    assert content_sha(tmp_path / "missing.md") == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_context.py -q ; echo $?`
Expected: FAIL — `ModuleNotFoundError: ...evaluator.context`.

- [ ] **Step 3: Create `evaluator/__init__.py`** (empty) and **`evaluator/context.py`**:

```python
"""Assemble the evaluator's instruction (criteria + indexed spec + indexed draft)
and capture per-artifact content hashes for staleness. Pure (DB read + file reads)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

_ROLE = (
    "You are the CHAIR of a mock review panel for a grant proposal. Assess the DRAFT "
    "against EACH funder criterion below, considering the SPEC (statement of intent). "
    "For each criterion assign a band — strong | adequate | at_risk | gap — with a one-"
    "paragraph note (the rationale; for at_risk/gap, the specific deficiency) and, when "
    "useful, a deep_link to the most relevant draft or spec section to act on. Call "
    "submit_scorecard EXACTLY ONCE, covering every criterion by its [index]. You have no "
    "other tools; everything you need is below.\n"
)


@dataclass
class EvaluatorContext:
    instruction: str
    draft_section_count: int
    spec_section_count: int
    # Hashes of the EXACT bytes assembled into the instruction (read once — no
    # TOCTOU re-read), so the job's evaluated_*_sha matches what the model saw.
    draft_sha: str
    spec_sha: str


def content_sha(path: Path) -> str:
    """sha256 of a file's bytes, or '' if it does not exist. Per-artifact content
    identity (the draft and spec share one git repo HEAD, which can't distinguish
    a draft-only edit from a spec-only edit). Used by the /scorecard endpoint to
    recompute current hashes — must match _read_artifact's hashing exactly."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_artifact(repo_dir: Path, filename: str) -> tuple[str, str]:
    """Read the file's bytes EXACTLY ONCE; return (decoded_text, content_sha).
    Reading once closes the TOCTOU gap where a concurrent edit between hashing and
    re-reading would make the recorded sha disagree with the assembled bytes.
    Missing file -> ('', '') so '' marks 'no artifact' on both sides of staleness."""
    path = repo_dir / filename
    if not path.exists():
        return "", ""
    raw = path.read_bytes()
    return raw.decode(), hashlib.sha256(raw).hexdigest()


def _render_criteria(rubric: list[dict]) -> str:
    if not rubric:
        return "(no criteria for this call — submit an empty scorecard)"
    lines: list[str] = []
    for i, c in enumerate(rubric):
        lines.append(f"[{i}] {c.get('name', '')} (weight {c.get('weight', '?')})")
        if c.get("description"):
            lines.append(f"    {c['description']}")
        for sub in c.get("subcriteria") or []:
            lines.append(f"      - {sub.get('name', '')}: {sub.get('description', '')}")
    return "\n".join(lines)


def _render_sections(sections: list) -> str:
    return "\n\n".join(f"[§{s.index}]\n{s.text}" for s in sections)


def assemble_evaluator_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> EvaluatorContext:
    ws = session.get(Workspace, workspace_id)
    rubric = list(ws.rubric_criteria) if ws and ws.rubric_criteria else []
    funder_notes = (ws.rubric_funder_notes if ws else None) or "(none)"
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft_text, draft_sha = _read_artifact(repo_dir, PROPOSAL_FILENAME)
    spec_text, spec_sha = _read_artifact(repo_dir, SPEC_FILENAME)
    # split_sections ALWAYS returns >=1 section (section 0 is the preamble, possibly
    # empty). For an empty/whitespace artifact we render the "(empty)" placeholder and
    # count 0 (nothing to deep-link); for real content we keep the sectionizer's exact
    # indexes so deep_link.section_index lines up with the editor.
    draft_sections = split_sections(draft_text) if draft_text.strip() else []
    spec_sections = split_sections(spec_text) if spec_text.strip() else []
    draft_block = _render_sections(draft_sections) if draft_sections else "(draft is empty)"
    spec_block = _render_sections(spec_sections) if spec_sections else "(spec is empty)"
    instruction = (
        f"{_ROLE}"
        f"\n# Funder criteria\n{_render_criteria(rubric)}\n"
        f"\n# Funder notes\n{funder_notes}\n"
        f"\n# SPEC (proposal-spec.md)\n{spec_block}\n"
        f"\n# DRAFT (proposal.md)\n{draft_block}\n"
    )
    return EvaluatorContext(
        instruction=instruction,
        draft_section_count=len(draft_sections),
        spec_section_count=len(spec_sections),
        draft_sha=draft_sha,
        spec_sha=spec_sha,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_context.py -q ; echo $?` → Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/evaluator/__init__.py \
        src/call_manager/proposal_assist/evaluator/context.py \
        tests/proposal_assist/evaluator/test_context.py
git commit -m "feat(evaluator): instruction context assembly + content-hash helper"
```

---

## Task 4: `evaluator/scoring.py` — deterministic weighted roll-up + gap floor

**Files:**
- Create: `src/call_manager/proposal_assist/evaluator/scoring.py`
- Test: `tests/proposal_assist/evaluator/test_scoring.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_scoring.py
import pytest

from call_manager.proposal_assist.evaluator.scoring import compute_overall_band


def test_empty_is_gap():
    assert compute_overall_band([]) == "gap"


def test_all_strong_is_strong():
    assert compute_overall_band([("strong", 1.0), ("strong", 1.0)]) == "strong"


def test_all_gap_is_gap():
    assert compute_overall_band([("gap", 1.0)]) == "gap"  # score 0 (< 0.75)


def test_threshold_2_50_inclusive_is_strong():
    # (2 + 3)/2 = 2.50 -> strong (>= 2.50). No gap, so the floor is inert.
    assert compute_overall_band([("adequate", 1.0), ("strong", 1.0)]) == "strong"


def test_just_below_2_50_is_adequate():
    # (2*3 + 3*1)/4 = 2.25 -> adequate
    assert compute_overall_band([("adequate", 3.0), ("strong", 1.0)]) == "adequate"


def test_threshold_1_75_inclusive_is_adequate():
    # 1*0.25 + 2*0.75 = 1.75 -> adequate (>= 1.75). Fractional weights, no gap.
    assert compute_overall_band([("at_risk", 0.25), ("adequate", 0.75)]) == "adequate"


def test_just_below_1_75_is_at_risk():
    # (1 + 2)/2 = 1.50 -> at_risk
    assert compute_overall_band([("at_risk", 1.0), ("adequate", 1.0)]) == "at_risk"


def test_low_weight_gap_caps_at_adequate():
    # avg ~ (3*0.9 + 0*0.1) = 2.7 -> provisional strong; one gap (norm 0.1 < 0.25)
    # -> capped at adequate.
    assert compute_overall_band([("strong", 0.9), ("gap", 0.1)]) == "adequate"


def test_high_weight_gap_caps_at_at_risk():
    # one gap at norm weight 0.4 (>= 0.25) -> capped at at_risk even though the
    # weighted average is high.
    assert compute_overall_band([("strong", 0.6), ("gap", 0.4)]) == "at_risk"


def test_weight_fallback_is_handled_by_caller():
    # compute_overall_band receives already-effective weights (> 0); equal weights work.
    assert compute_overall_band([("adequate", 1.0), ("adequate", 1.0)]) == "adequate"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_scoring.py -q ; echo $?`
Expected: FAIL — `ModuleNotFoundError: ...evaluator.scoring`.

- [ ] **Step 3: Create `evaluator/scoring.py`**:

```python
"""Deterministic overall-readiness roll-up. The agent judges each criterion's
band; the server computes ONE overall band from the per-criterion bands weighted
by effective weight, with a gap floor (a pure average can read 'strong' while a
meaningful criterion is a gap). Pure — no DB, no LLM, no I/O."""

from __future__ import annotations

BAND_ORDINAL = {"gap": 0, "at_risk": 1, "adequate": 2, "strong": 3}
_ORDINAL_BAND = ["gap", "at_risk", "adequate", "strong"]

# Tunable threshold block. Score in [0,3] -> provisional band.
_THRESHOLDS = ((0.75, "gap"), (1.75, "at_risk"), (2.50, "adequate"))
# A gap on a criterion whose normalized effective weight is >= this caps overall
# at at_risk; any gap at all caps at adequate.
GAP_FLOOR_NORM_WEIGHT = 0.25


def _provisional(score: float) -> str:
    for upper, band in _THRESHOLDS:
        if score < upper:
            return band
    return "strong"


def compute_overall_band(items: list[tuple[str, float]]) -> str:
    """items: list of (band, effective_weight) where effective_weight > 0.
    Empty -> 'gap'."""
    if not items:
        return "gap"
    total_w = sum(w for _, w in items)
    score = sum(BAND_ORDINAL[band] * w for band, w in items) / total_w
    cap = BAND_ORDINAL["strong"]
    for band, w in items:
        if band == "gap":
            cap = min(cap, BAND_ORDINAL["adequate"])
            if (w / total_w) >= GAP_FLOOR_NORM_WEIGHT:
                cap = min(cap, BAND_ORDINAL["at_risk"])
    return _ORDINAL_BAND[min(BAND_ORDINAL[_provisional(score)], cap)]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_scoring.py -q ; echo $?` → Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/evaluator/scoring.py \
        tests/proposal_assist/evaluator/test_scoring.py
git commit -m "feat(evaluator): deterministic weighted roll-up with gap floor"
```

---

## Task 5: `submit_scorecard` tool + registry wiring

**Files:**
- Create: `src/call_manager/proposal_assist/tools/scorecard.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (add 2 `ToolContext` fields; register the tool)
- Test: `tests/proposal_assist/evaluator/test_submit_scorecard.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_submit_scorecard.py
import uuid

import pytest

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl
from tests.proposal_assist.conftest import make_workspace


def _ctx(tmp_path, session_factory, ws_id, job_id, *, draft_n=2, spec_n=1):
    return ToolContext(
        workspace_id=ws_id,
        repo_dir=tmp_path,
        session_factory=session_factory,
        settings=Settings(),
        origin_kind="evaluator_session",
        origin_job_id=job_id,
        eval_draft_section_count=draft_n,
        eval_spec_section_count=spec_n,
    )


def _setup(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = [
            {"name": "Quality", "weight": 0.7},
            {"name": "Impact", "weight": 0.3},
        ]
        s.flush()
        job = EvaluatorJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        return ws.id, job.id


@pytest.mark.asyncio
async def test_valid_scorecard_is_written(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_scorecard_impl(
        ctx,
        criteria=[
            {"index": 0, "name": "Quality", "band": "strong", "note": "Rigorous."},
            {"index": 1, "name": "Impact", "band": "gap", "note": "No KPIs.",
             "deep_link": {"artifact": "draft", "section_index": 1}},
        ],
        summary="Mostly there.",
    )
    assert res["stored"] is True
    with pg_session_factory() as s:
        job = s.get(EvaluatorJob, job_id)
        sc = job.scorecard
    # Impact is a gap at normalized effective weight 0.3 (>= 0.25) -> gap floor caps
    # overall at at_risk regardless of the weighted average.
    assert sc["overall_band"] == "at_risk"
    assert sc["criteria"][0]["effective_weight"] == 0.7
    assert sc["criteria"][1]["band"] == "gap"
    assert sc["summary"] == "Mostly there."


@pytest.mark.asyncio
async def test_missing_index_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_scorecard_impl(
        ctx,
        criteria=[{"index": 0, "name": "Quality", "band": "strong", "note": "x"}],
        summary="",
    )
    assert "error" in res  # index 1 not covered


@pytest.mark.asyncio
async def test_name_mismatch_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_scorecard_impl(
        ctx,
        criteria=[
            {"index": 0, "name": "WRONG", "band": "strong", "note": "x"},
            {"index": 1, "name": "Impact", "band": "adequate", "note": "y"},
        ],
        summary="",
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_bad_band_and_out_of_range_deeplink_rejected(
    tmp_path, pg_session_factory, _settings_env
):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id, draft_n=1)
    bad_band = await submit_scorecard_impl(
        ctx,
        criteria=[
            {"index": 0, "name": "Quality", "band": "excellent", "note": "x"},
            {"index": 1, "name": "Impact", "band": "gap", "note": "y"},
        ],
        summary="",
    )
    assert "error" in bad_band
    bad_link = await submit_scorecard_impl(
        ctx,
        criteria=[
            {"index": 0, "name": "Quality", "band": "strong", "note": "x",
             "deep_link": {"artifact": "draft", "section_index": 5}},
            {"index": 1, "name": "Impact", "band": "gap", "note": "y"},
        ],
        summary="",
    )
    assert "error" in bad_link


@pytest.mark.asyncio
async def test_empty_rubric_accepts_empty_criteria(tmp_path, pg_session_factory, _settings_env):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.rubric_criteria = []
        s.flush()
        job = EvaluatorJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        ws_id, job_id = ws.id, job.id
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_scorecard_impl(ctx, criteria=[], summary="No criteria.")
    assert res["stored"] is True
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).scorecard["criteria"] == []
```

> Note on the first test's `overall_band`: Impact is `gap` at norm effective weight 0.3 (≥ 0.25), so the gap floor caps overall at `at_risk` regardless of the weighted average. Keep the `at_risk` assertion.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_submit_scorecard.py -q ; echo $?`
Expected: FAIL — `ModuleNotFoundError: ...tools.scorecard` (and `ToolContext` has no `eval_draft_section_count`).

- [ ] **Step 3a: Add two fields to `ToolContext`** in `registry.py`, immediately after the `origin_job_id` field (line 92):

```python
    # SP2b-3a: the evaluator pass pins the section counts of the artifacts it
    # assembled into the instruction (captured at run start), so submit_scorecard
    # validates deep_link.section_index against the SAME HEAD it evaluated.
    eval_draft_section_count: int | None = field(default=None)
    eval_spec_section_count: int | None = field(default=None)
```

- [ ] **Step 3b: Create `tools/scorecard.py`**:

```python
"""submit_scorecard tool impl: the evaluator's ONLY tool. Validates the agent's
per-criterion judgments against the workspace rubric (index identity + name
cross-check), computes the deterministic overall band, and writes the scorecard
JSONB onto the running evaluator_jobs row. Mirrors the propose_* impl contract:
returns {"error": ...} for every expected failure, never raises."""

from __future__ import annotations

from typing import Any

from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.evaluator.scoring import compute_overall_band
from call_manager.proposal_assist.tools.registry import ToolContext

_BANDS = ("strong", "adequate", "at_risk", "gap")


def _effective_weight(raw: Any) -> tuple[float, float]:
    weight = float(raw) if isinstance(raw, (int, float)) else 0.0
    return weight, (weight if weight > 0 else 1.0)


async def submit_scorecard_impl(
    ctx: ToolContext, *, criteria: list[dict[str, Any]], summary: str
) -> dict[str, Any]:
    with ctx.session_factory() as session:
        ws = session.get(Workspace, ctx.workspace_id)
        rubric = list(ws.rubric_criteria) if ws and ws.rubric_criteria else []
        n = len(rubric)

        # Index identity: must cover 0..n-1 exactly once.
        indexes = [c.get("index") for c in criteria]
        if sorted(i for i in indexes if isinstance(i, int)) != list(range(n)) or len(
            indexes
        ) != n:
            return {
                "error": f"scorecard must cover criterion indexes 0..{n - 1} exactly once; "
                f"got {indexes}"
            }

        out: list[dict[str, Any]] = []
        for c in criteria:
            idx = c["index"]
            name = c.get("name")
            if name != rubric[idx].get("name"):
                return {
                    "error": f"criterion {idx}: name {name!r} does not match rubric "
                    f"{rubric[idx].get('name')!r}"
                }
            band = c.get("band")
            if band not in _BANDS:
                return {"error": f"criterion {idx}: band must be one of {_BANDS}, got {band!r}"}
            note = (c.get("note") or "").strip()
            if not note:
                return {"error": f"criterion {idx}: note must be a non-empty paragraph"}
            deep_link = c.get("deep_link")
            if deep_link is not None:
                art = deep_link.get("artifact")
                si = deep_link.get("section_index")
                count = {
                    "draft": ctx.eval_draft_section_count,
                    "spec": ctx.eval_spec_section_count,
                }.get(art)
                if count is None or not isinstance(si, int) or not (0 <= si < count):
                    return {"error": f"criterion {idx}: invalid deep_link {deep_link!r}"}
            weight, eff = _effective_weight(rubric[idx].get("weight"))
            out.append(
                {
                    "index": idx,
                    "name": name,
                    "band": band,
                    "note": note,
                    "deep_link": deep_link,
                    "weight": weight,
                    "effective_weight": eff,
                }
            )

        overall = compute_overall_band([(c["band"], c["effective_weight"]) for c in out])
        scorecard = {"criteria": out, "overall_band": overall, "summary": (summary or "").strip()}

        # Guard the write: row-lock the target and require it is THIS workspace's
        # still-running evaluator job, so a stale/mismatched ctx.origin_job_id (or a
        # job the reaper/cancel already failed) can never have a scorecard written
        # onto it. (origin_kind isn't a job column; ctx.origin_kind is fixed to
        # 'evaluator_session' by build_evaluator_adapter — the enforceable guards are
        # workspace identity + running status.)
        job = session.get(EvaluatorJob, ctx.origin_job_id, with_for_update=True)
        if job is None or job.workspace_id != ctx.workspace_id or job.status != "running":
            return {"error": "scorecard target is not an active evaluator run for this workspace"}
        job.scorecard = scorecard
        session.commit()
    return {"stored": True, "overall_band": overall}
```

- [ ] **Step 3c: Register the tool** in `registry.py`. Inside `_collect_tools`, add the import near the other tool imports (after the `tools.proposal` import block, ~line 202):

```python
    from call_manager.proposal_assist.tools.scorecard import submit_scorecard_impl
```

Add the `@tool` handler before the `return [ ... ]` list (after `build_bibliography`, ~line 670):

```python
    @tool(
        "submit_scorecard",
        "Submit the COMPLETE evaluation scorecard exactly once. `criteria` MUST contain "
        "one entry per funder criterion, each with `index` (the 0-based [index] shown in the "
        "criteria list), the matching `name`, a `band` (strong|adequate|at_risk|gap), a one-"
        "paragraph `note`, and an optional `deep_link` {artifact:'draft'|'spec', section_index}. "
        "`summary` is a short overall readiness note. The overall band is computed server-side.",
        {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "name": {"type": "string"},
                            "band": {
                                "type": "string",
                                "enum": ["strong", "adequate", "at_risk", "gap"],
                            },
                            "note": {"type": "string"},
                            "deep_link": {
                                "type": "object",
                                "properties": {
                                    "artifact": {"type": "string", "enum": ["draft", "spec"]},
                                    "section_index": {"type": "integer"},
                                },
                                "required": ["artifact", "section_index"],
                            },
                        },
                        "required": ["index", "name", "band", "note"],
                    },
                },
                "summary": {"type": "string"},
            },
            "required": ["criteria", "summary"],
        },
    )
    async def submit_scorecard(args):
        return await _safe_tool(
            "submit_scorecard",
            ctx,
            submit_scorecard_impl,
            ctx,
            criteria=args.get("criteria") or [],
            summary=args.get("summary", ""),
        )
```

Add `submit_scorecard,` to the returned `tools` list (after `build_bibliography,`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_submit_scorecard.py -q ; echo $?` → Expected: PASS (5 passed).
Also run the existing tool tests to confirm the registry still builds: `uv run pytest tests/proposal_assist/tools -q ; echo $?` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/tools/scorecard.py \
        src/call_manager/proposal_assist/tools/registry.py \
        tests/proposal_assist/evaluator/test_submit_scorecard.py
git commit -m "feat(evaluator): submit_scorecard tool (index-validated) + registry wiring"
```

---

## Task 6: `evaluator/jobs.py` — queue helpers

**Files:**
- Create: `src/call_manager/proposal_assist/evaluator/jobs.py`
- Test: `tests/proposal_assist/evaluator/test_jobs.py`

This is the coherence `jobs.py` mirrored, **minus** all `supersede_*`/`_supersede` (the scorecard is a single row, not inbox rows), **plus** `latest_completed_evaluator_job`. Keep the **atomic cancel** and the **FOR UPDATE SKIP LOCKED** claim/reap exactly.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_jobs.py
from datetime import UTC, datetime, timedelta

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.evaluator import jobs
from tests.proposal_assist.conftest import make_user, make_workspace


def test_enqueue_create_or_return_active(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        a = jobs.enqueue_evaluator_run(s, workspace_id=ws.id, requested_by=None)
        s.commit()
        b = jobs.enqueue_evaluator_run(s, workspace_id=ws.id, requested_by=None)
        s.commit()
        assert a.id == b.id  # returns the existing active job


def test_claim_skips_running_and_picks_a_queued_in_another_workspace(pg_session_factory):
    # The active-unique index forbids queued+running in ONE workspace, so use two:
    # ws has only a running job (not claimable); ws2 has a queued job (claimable).
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="running"))
        ws2 = make_workspace(s, owner=make_user(s, email="ws2-owner@ki.se"))
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws2.id, status="queued"))
        s.commit()
        claimed = jobs.claim_next_evaluator_job(s)
        assert claimed is not None and claimed.workspace_id == ws2.id
        assert claimed.status == "running" and claimed.started_at is not None


def test_claim_returns_none_when_only_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="running"))
        s.commit()
        assert jobs.claim_next_evaluator_job(s) is None


def test_claim_skips_soft_deleted_workspace(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        s.commit()
        assert jobs.claim_next_evaluator_job(s) is None


def test_cancel_atomic_leaves_completed_untouched(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        done = EvaluatorJob(workspace_id=ws.id, status="completed", scorecard={"criteria": []})
        running = EvaluatorJob(workspace_id=ws.id, status="running")
        s.add_all([done, running])
        s.commit()
        n = jobs.cancel_evaluator_jobs_for_workspace(s, workspace_id=ws.id)
        s.refresh(done)
        s.refresh(running)
        assert n == 1
        assert done.status == "completed"  # not clobbered
        assert running.status == "failed"


def test_reaper_fails_stale_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = EvaluatorJob(
            workspace_id=ws.id,
            status="running",
            started_at=datetime.now(UTC) - timedelta(seconds=600),
        )
        s.add(job)
        s.commit()
        n = jobs.reap_stuck_evaluator_jobs(s, timeout_seconds=300)
        s.refresh(job)
        assert n == 1 and job.status == "failed"


def test_cap_blocks_over_budget(pg_session_factory, _settings_env):
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(
            EvaluatorJob(
                workspace_id=ws.id,
                status="completed",
                input_tokens=settings.evaluator_daily_token_cap,
                output_tokens=0,
            )
        )
        s.commit()
        assert jobs.evaluator_cap_exceeded(s, workspace_id=ws.id, settings=settings) is True


def test_latest_completed_returns_completed_only(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="completed", scorecard={"criteria": []}))
        s.commit()
        s.add(EvaluatorJob(workspace_id=ws.id, status="failed"))
        s.commit()
        job = jobs.latest_completed_evaluator_job(s, workspace_id=ws.id)
        assert job is not None and job.status == "completed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_jobs.py -q ; echo $?`
Expected: FAIL — `ModuleNotFoundError: ...evaluator.jobs`.

- [ ] **Step 3: Create `evaluator/jobs.py`**:

```python
"""Pure-DB helpers for the evaluator_jobs queue. The caller owns the transaction
(these flush, never commit), mirroring coherence/jobs.py. No supersede logic — the
scorecard is a single JSONB on the job row, so 'latest completed wins' is a read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.turns.caps import _stockholm_day_start_utc


def enqueue_evaluator_run(
    session: Session, *, workspace_id: uuid.UUID, requested_by: int | None
) -> EvaluatorJob:
    """Create a queued job, or return the existing active (queued|running) one.
    Race-safe against uq_evaluator_jobs_active: the INSERT runs inside a SAVEPOINT
    so a unique-violation rolls back only the savepoint, leaving the outer txn
    usable for the re-read."""
    existing = session.scalar(
        select(EvaluatorJob).where(
            EvaluatorJob.workspace_id == workspace_id,
            EvaluatorJob.status.in_(("queued", "running")),
        )
    )
    if existing is not None:
        return existing
    job = EvaluatorJob(workspace_id=workspace_id, status="queued", requested_by=requested_by)
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        if job in session:
            session.expunge(job)
        winner = session.scalar(
            select(EvaluatorJob).where(
                EvaluatorJob.workspace_id == workspace_id,
                EvaluatorJob.status.in_(("queued", "running")),
            )
        )
        if winner is None:
            raise RuntimeError(
                "evaluator enqueue lost the active-unique race but found no active job"
            ) from None
        return winner
    return job


def claim_next_evaluator_job(session: Session) -> EvaluatorJob | None:
    """Claim the oldest queued job whose workspace is LIVE and has no running job.
    FOR UPDATE SKIP LOCKED so parallel ticks don't fight. Soft-deleted workspaces
    are excluded so a pass can't burn tokens after deletion."""
    running_ws = select(EvaluatorJob.workspace_id).where(EvaluatorJob.status == "running")
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    job = session.scalar(
        select(EvaluatorJob)
        .where(
            EvaluatorJob.status == "queued",
            EvaluatorJob.workspace_id.not_in(running_ws),
            EvaluatorJob.workspace_id.in_(live_ws),
        )
        .order_by(EvaluatorJob.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    session.flush()
    return job


def cancel_evaluator_jobs_for_workspace(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Fail any active (queued|running) evaluator job for a workspace (soft-delete
    handler) so a queued/running pass can't spend tokens after deletion. ONE atomic
    UPDATE ... WHERE status IN (...) RETURNING — never an ORM load-then-write, which
    could clobber a job the success finalizer is concurrently completing."""
    cancelled_ids = (
        session.execute(
            update(EvaluatorJob)
            .where(
                EvaluatorJob.workspace_id == workspace_id,
                EvaluatorJob.status.in_(("queued", "running")),
            )
            .values(status="failed", error="workspace deleted", completed_at=datetime.now(UTC))
            .returning(EvaluatorJob.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    session.flush()
    return len(cancelled_ids)


def evaluator_cap_exceeded(
    session: Session, *, workspace_id: uuid.UUID, settings: Settings
) -> bool:
    """True if today's (Stockholm-day) evaluator in+out tokens are at/over the cap."""
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(
            func.coalesce(func.sum(EvaluatorJob.input_tokens + EvaluatorJob.output_tokens), 0)
        ).where(
            EvaluatorJob.workspace_id == workspace_id,
            EvaluatorJob.queued_at >= day_start,
        )
    )
    return int(total or 0) >= settings.evaluator_daily_token_cap


def reap_stuck_evaluator_jobs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running job older than the TTL, unwedging the
    workspace's active-unique slot. Pure DB. FOR UPDATE SKIP LOCKED."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(EvaluatorJob)
        .where(EvaluatorJob.status == "running", EvaluatorJob.started_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in rows:
        job.status = "failed"
        job.error = "timed out"
        job.completed_at = datetime.now(UTC)
    session.flush()
    return len(rows)


def latest_evaluator_job(session: Session, *, workspace_id: uuid.UUID) -> EvaluatorJob | None:
    return session.scalar(
        select(EvaluatorJob)
        .where(EvaluatorJob.workspace_id == workspace_id)
        .order_by(EvaluatorJob.queued_at.desc())
        .limit(1)
    )


def latest_completed_evaluator_job(
    session: Session, *, workspace_id: uuid.UUID
) -> EvaluatorJob | None:
    return session.scalar(
        select(EvaluatorJob)
        .where(
            EvaluatorJob.workspace_id == workspace_id,
            EvaluatorJob.status == "completed",
        )
        .order_by(EvaluatorJob.queued_at.desc())
        .limit(1)
    )
```

> **Verify before writing:** `_stockholm_day_start_utc` is importable from `call_manager.proposal_assist.turns.caps` (coherence/jobs.py:15 imports it from there) and `Workspace.soft_deleted_at` exists (used by coherence's claim).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_jobs.py -q ; echo $?` → Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/evaluator/jobs.py \
        tests/proposal_assist/evaluator/test_jobs.py
git commit -m "feat(evaluator): evaluator_jobs queue helpers (atomic cancel, no supersede)"
```

---

## Task 7: `evaluator/agent.py` — one-shot adapter factory

**Files:**
- Create: `src/call_manager/proposal_assist/evaluator/agent.py`
- Test: `tests/proposal_assist/evaluator/test_evaluator_agent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_evaluator_agent.py
import uuid

from call_manager.config import Settings
from call_manager.proposal_assist.evaluator.agent import (
    build_evaluator_adapter,
    evaluator_allowed_tools,
)


def test_allowlist_is_only_submit_scorecard():
    assert evaluator_allowed_tools() == ["submit_scorecard"]


def test_adapter_builds_with_pinned_section_counts(_settings_env):
    adapter = build_evaluator_adapter(
        settings=Settings(),
        session_factory=lambda: None,
        workspace_id=uuid.uuid4(),
        requested_by=None,
        job_id=uuid.uuid4(),
        draft_section_count=3,
        spec_section_count=2,
    )
    assert adapter._allowed_tools == ["submit_scorecard"]
    # tools_factory stamps evaluator provenance + the pinned counts on the ToolContext.
    server = None  # building the MCP server requires a repo_dir; just assert the closure exists
    assert callable(adapter._tools_factory)
```

> **Verify before writing:** confirm `ClaudeAgentSDKAdapter` stores the tools factory on an attribute you can assert (e.g. `self._tools_factory`). If the attribute differs, drop the last assertion — the allowlist assertion is the essential one.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_evaluator_agent.py -q ; echo $?`
Expected: FAIL — `ModuleNotFoundError: ...evaluator.agent`.

- [ ] **Step 3: Create `evaluator/agent.py`**:

```python
"""Build the evaluator's OWN one-shot SDK adapter — never WorkspaceController.

The evaluator gets a dedicated adapter + MCP server + ToolContext stamped with
evaluator provenance (origin_kind='evaluator_session', origin_job_id) and the
PINNED section counts (so submit_scorecard validates deep_link against the same
HEAD the pass evaluated). Its ONLY tool is submit_scorecard — no live reads, so
it cannot judge against bytes newer than the recorded evaluated_*_sha."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.proposal_assist.drafting.claude_adapter import ClaudeAgentSDKAdapter
from call_manager.proposal_assist.tools.registry import (
    ToolContext,
    build_proposal_assist_mcp_server,
)


def evaluator_allowed_tools() -> list[str]:
    return ["submit_scorecard"]


def build_evaluator_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    requested_by: int | None,
    job_id: uuid.UUID,
    draft_section_count: int,
    spec_section_count: int,
) -> ClaudeAgentSDKAdapter:
    def tools_factory(repo_dir: Path, ws_id: uuid.UUID):
        ctx = ToolContext(
            workspace_id=ws_id,
            repo_dir=repo_dir,
            session_factory=session_factory,
            settings=settings,
            origin_kind="evaluator_session",
            origin_job_id=job_id,
            created_by=requested_by,
            eval_draft_section_count=draft_section_count,
            eval_spec_section_count=spec_section_count,
        )
        return build_proposal_assist_mcp_server(ctx)

    return ClaudeAgentSDKAdapter(
        model=settings.proposal_assist_llm_model,
        workspaces_dir=settings.workspaces_dir,
        tools_factory=tools_factory,
        allowed_tools=evaluator_allowed_tools(),
    )
```

> **Verify before writing:** confirm `ClaudeAgentSDKAdapter.__init__` accepts `model`, `workspaces_dir`, `tools_factory`, `allowed_tools` (coherence/agent.py passes exactly these; `allowed_tools` confirmed at claude_adapter.py:159).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_evaluator_agent.py -q ; echo $?` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/evaluator/agent.py \
        tests/proposal_assist/evaluator/test_evaluator_agent.py
git commit -m "feat(evaluator): one-shot adapter factory (submit_scorecard-only allowlist)"
```

---

## Task 8: `evaluator/pass_runner.py` — run one pass + drain tick

**Files:**
- Create: `src/call_manager/proposal_assist/evaluator/pass_runner.py`
- Test: `tests/proposal_assist/evaluator/test_pass_runner.py`

Mirrors `coherence/pass_runner.py` with every hardening, but: step 1 captures the staleness markers + pins section counts; the failure path just `_fail`s (no supersede); finalize **requires `job.scorecard is not None`**.

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_pass_runner.py
import asyncio

import pytest

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.evaluator.pass_runner import run_evaluator_pass
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir, ws_id):
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text("## Aims\n\nDraft.\n")
    (repo / "proposal-spec.md").write_text("## Aims\n\nSpec.\n")


def _make_running_job(s, ws_id):
    job = EvaluatorJob(workspace_id=ws_id, status="running")
    s.add(job)
    s.flush()
    return job


@pytest.mark.asyncio
async def test_success_requires_scorecard_then_completes(
    tmp_path, pg_session_factory, _settings_env
):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    # Fake adapter whose send_turn writes the scorecard onto the job (simulating the
    # submit_scorecard tool), then emits TurnDoneEvent.
    class _Adapter(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            with pg_session_factory() as s:
                j = s.get(EvaluatorJob, job_id)
                j.scorecard = {"criteria": [], "overall_band": "gap", "summary": "x"}
                s.commit()
            yield TurnDoneEvent(tokens_in=50, tokens_out=20, model="claude-x", session_id="sess")

    await run_evaluator_pass(
        job_id=job_id,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: _Adapter(),
    )
    with pg_session_factory() as s:
        done = s.get(EvaluatorJob, job_id)
        assert done.status == "completed"
        assert done.input_tokens == 50 and done.output_tokens == 20
        assert done.evaluated_draft_sha and done.evaluated_spec_sha  # staleness markers captured


@pytest.mark.asyncio
async def test_no_scorecard_fails(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()
    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m", session_id="s")])
    await run_evaluator_pass(
        job_id=job_id,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: fake,
    )
    with pg_session_factory() as s:
        job = s.get(EvaluatorJob, job_id)
        assert job.status == "failed" and "no scorecard" in (job.error or "")


@pytest.mark.asyncio
async def test_timeout_on_launch_fails(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.evaluator_pass_timeout_seconds = 0.1
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _make_running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _HangLaunch(FakeAgentAdapter):
        async def launch(self, context):
            await asyncio.sleep(2)
            return await super().launch(context)

    await run_evaluator_pass(
        job_id=job_id,
        session_factory=pg_session_factory,
        settings=settings,
        adapter_builder=lambda **kw: _HangLaunch(),
    )
    with pg_session_factory() as s:
        assert s.get(EvaluatorJob, job_id).status == "failed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_pass_runner.py -q ; echo $?`
Expected: FAIL — `ModuleNotFoundError: ...evaluator.pass_runner`.

- [ ] **Step 3: Create `evaluator/pass_runner.py`**:

```python
"""Run one evaluator pass for a claimed (running) evaluator_jobs row, and the async
drain tick. Fresh-stateless: a dedicated one-shot adapter, no resume, terminated in
finally; output is the scorecard JSONB written by submit_scorecard. Carries the
SP2b-2c hardening: launch+drive bounded together, bounded terminate, FOR UPDATE
finalize guard. The separate sync reaper handles crash recovery."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.db.models import EvaluatorJob, Workspace
from call_manager.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent
from call_manager.proposal_assist.evaluator.agent import build_evaluator_adapter
from call_manager.proposal_assist.evaluator.context import assemble_evaluator_context
from call_manager.proposal_assist.evaluator.jobs import (
    claim_next_evaluator_job,
    evaluator_cap_exceeded,
)

logger = logging.getLogger(__name__)


def _fail(session, job: EvaluatorJob, message: str) -> None:
    job.status = "failed"
    job.error = message
    job.completed_at = datetime.now(UTC)
    session.flush()


async def run_evaluator_pass(
    *,
    job_id: uuid.UUID,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_evaluator_adapter,
) -> None:
    # 1. Short read session: capture staleness markers + assemble context (pinned HEAD).
    with session_factory() as s:
        job = s.get(EvaluatorJob, job_id)
        if job is None or job.status != "running":
            return
        ws = s.get(Workspace, job.workspace_id)
        workspace_id = job.workspace_id
        requested_by = job.requested_by
        if evaluator_cap_exceeded(s, workspace_id=workspace_id, settings=settings):
            _fail(s, job, "daily evaluation budget reached")
            s.commit()
            return
        ctx = assemble_evaluator_context(
            session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
        )
        # The context hashed the EXACT bytes it assembled (single read) — use those so
        # the staleness markers match what the model actually evaluated.
        job.evaluated_draft_sha = ctx.draft_sha
        job.evaluated_spec_sha = ctx.spec_sha
        job.evaluated_rubric_updated_at = ws.rubric_updated_at if ws else None
        instruction = ctx.instruction
        draft_n, spec_n = ctx.draft_section_count, ctx.spec_section_count
        s.commit()

    adapter = adapter_builder(
        settings=settings,
        session_factory=session_factory,
        workspace_id=workspace_id,
        requested_by=requested_by,
        job_id=job_id,
        draft_section_count=draft_n,
        spec_section_count=spec_n,
    )
    handle = None
    done: TurnDoneEvent | None = None
    try:

        async def _run() -> TurnDoneEvent | None:
            nonlocal handle
            handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
            last = None
            async for event in adapter.send_turn(handle, instruction):
                if isinstance(event, TurnDoneEvent):
                    last = event
            return last

        done = await asyncio.wait_for(
            _run(), timeout=settings.evaluator_pass_timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 — terminal: fail the job, never propagate
        logger.exception("evaluator pass failed for job %s", job_id)
        message = (
            "Evaluation timed out."
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            else "Evaluation failed."
        )
        with session_factory() as s:
            job = s.get(EvaluatorJob, job_id, with_for_update=True)
            if job is not None and job.status == "running":
                _fail(s, job, message)
            s.commit()
        return
    finally:
        if handle is not None:
            try:
                await asyncio.wait_for(
                    adapter.terminate(handle),
                    timeout=settings.evaluator_terminate_timeout_seconds,
                )
            except Exception:  # noqa: BLE001
                logger.exception("evaluator adapter terminate failed for job %s", job_id)

    # 2. Success finalize under a row lock; ONLY if still running AND a scorecard was written.
    with session_factory() as s:
        job = s.get(EvaluatorJob, job_id, with_for_update=True)
        if job is None or job.status != "running":
            s.commit()
            return
        if job.scorecard is None:
            _fail(s, job, "evaluator produced no scorecard")
            s.commit()
            return
        job.status = "completed"
        job.completed_at = datetime.now(UTC)
        if done is not None:
            job.input_tokens = done.tokens_in or 0
            job.output_tokens = done.tokens_out or 0
            job.model = done.model
            job.sdk_session_id = done.session_id
        s.commit()


async def drain_evaluator_queue_tick(
    *,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_evaluator_adapter,
) -> None:
    """Claim one job and run it. The reaper is a separate sync tick."""
    with session_factory() as s:
        job = claim_next_evaluator_job(s)
        s.commit()
        job_id = job.id if job is not None else None
    if job_id is None:
        return
    await run_evaluator_pass(
        job_id=job_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_pass_runner.py -q ; echo $?` → Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/proposal_assist/evaluator/pass_runner.py \
        tests/proposal_assist/evaluator/test_pass_runner.py
git commit -m "feat(evaluator): pass runner + drain tick (require-scorecard finalize)"
```

---

## Task 9: `web/evaluator.py` endpoints + router mount

**Files:**
- Create: `src/call_manager/proposal_assist/web/evaluator.py`
- Modify: `src/call_manager/main.py` (mount the router next to `coherence_router`)
- Test: `tests/proposal_assist/evaluator/test_web_evaluator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/proposal_assist/evaluator/test_web_evaluator.py
import uuid

import pytest

from call_manager.db.models import EvaluatorJob, Workspace
from tests.proposal_assist.conftest import make_workspace


def _ws(_app, current_user, pg_session_factory, tmp_path, *, phase="active", ack=True):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=ack, current_phase=phase)
        s.flush()
        ws.rubric_criteria = []
        # NOTE: do NOT set rubric_updated_at — it's a non-null column with a server
        # default; the staleness test reads the real value to seed a matching marker.
        s.commit()
        return str(ws.id)


def test_post_setup_409(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path, phase="setup")
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    assert r.status_code == 409


def test_post_missing_ack_403(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path, ack=False)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    assert r.status_code == 403


def test_post_create_or_return_active(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    a = client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    b = client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_latest_null_then_job(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    assert client_authed_owner.get(f"/api/workspaces/{ws_id}/evaluator-runs/latest").json() is None
    client_authed_owner.post(f"/api/workspaces/{ws_id}/evaluator-runs")
    assert client_authed_owner.get(
        f"/api/workspaces/{ws_id}/evaluator-runs/latest"
    ).json()["status"] in ("queued", "running")


def test_scorecard_null_then_value_with_stale_flags(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    assert client_authed_owner.get(f"/api/workspaces/{ws_id}/scorecard").json() is None
    wid = uuid.UUID(ws_id)
    # seed a completed job whose evaluated markers MATCH the current state: the repo
    # has no proposal.md/proposal-spec.md (content_sha == "") and the rubric marker is
    # the workspace's real rubric_updated_at -> stale must be False.
    with pg_session_factory() as s:
        rubric_ts = s.get(Workspace, wid).rubric_updated_at
        s.add(
            EvaluatorJob(
                workspace_id=wid,
                status="completed",
                scorecard={"criteria": [], "overall_band": "gap", "summary": "x"},
                evaluated_draft_sha="",
                evaluated_spec_sha="",
                evaluated_rubric_updated_at=rubric_ts,
            )
        )
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/scorecard").json()
    assert body["scorecard"]["overall_band"] == "gap"
    assert body["stale"] is False and body["stale_reasons"] == []


def test_non_member_403(client_authed_owner, client_unauth, _app, current_user,
                        pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    # an unauthenticated client is rejected by get_current_user (401/403)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/scorecard")
    assert r.status_code in (401, 403)
```

> The `_app`/`current_user`/`client_authed_owner`/`client_unauth` fixtures are in `tests/proposal_assist/conftest.py`. `_app.state.settings.workspaces_dir` is set so the staleness recompute reads an empty repo (`content_sha == ""`), matching the seeded markers.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/proposal_assist/evaluator/test_web_evaluator.py -q ; echo $?`
Expected: FAIL — 404s (router not mounted) / import error.

- [ ] **Step 3: Create `web/evaluator.py`**:

```python
"""Member-gated manual evaluator endpoints. POST enqueues (create-or-return-active);
GET latest is polled by the SPA; GET scorecard returns the latest completed job's
scorecard plus server-computed staleness (draft / spec / rubric)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import EvaluatorJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.evaluator.context import content_sha
from call_manager.proposal_assist.evaluator.jobs import (
    enqueue_evaluator_run,
    latest_completed_evaluator_job,
    latest_evaluator_job,
)
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/api/workspaces", tags=["evaluator"])


def _serialize(job: EvaluatorJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _gate_cloud_llm(*, ws: Workspace, request: Request, session: Session, user) -> None:
    # Mirror the coherence/turns gates — SAME order + status codes, evaluator wording.
    if ws.current_phase == "setup":
        raise HTTPException(
            status_code=409,
            detail="Workspace is still setting up — review the rubric to start drafting.",
        )
    if not request.app.state.settings.drafting_enabled:
        raise HTTPException(
            status_code=503,
            detail="Drafting is currently disabled by an administrator. Please try again later.",
        )
    prior_ack = session.scalar(
        select(WorkspaceEvent.id)
        .where(
            WorkspaceEvent.workspace_id == ws.id,
            WorkspaceEvent.kind == "disclosure_acknowledged",
            WorkspaceEvent.actor_id == user.id,
        )
        .limit(1)
    )
    if prior_ack is None:
        raise HTTPException(
            status_code=403,
            detail="Please acknowledge the data-disclosure notice before running an evaluation.",
        )


@router.post("/{workspace_id}/evaluator-runs")
def start_evaluator_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    _gate_cloud_llm(ws=ws, request=request, session=session, user=user)
    job = enqueue_evaluator_run(session, workspace_id=workspace_id, requested_by=user.id)
    session.commit()
    return _serialize(job)


@router.get("/{workspace_id}/evaluator-runs/latest")
def latest_evaluator_run(
    workspace_id: uuid.UUID,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    require_member(workspace_id=workspace_id, user=user, session=session)
    job = latest_evaluator_job(session, workspace_id=workspace_id)
    return _serialize(job) if job is not None else None


@router.get("/{workspace_id}/scorecard")
def get_scorecard(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    ws = require_member(workspace_id=workspace_id, user=user, session=session)
    job = latest_completed_evaluator_job(session, workspace_id=workspace_id)
    if job is None:
        return None
    repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
    reasons: list[str] = []
    if job.evaluated_draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME):
        reasons.append("draft")
    if job.evaluated_spec_sha != content_sha(repo_dir / SPEC_FILENAME):
        reasons.append("spec")
    if job.evaluated_rubric_updated_at != ws.rubric_updated_at:
        reasons.append("rubric")
    return {
        "scorecard": job.scorecard,
        "evaluated_draft_sha": job.evaluated_draft_sha,
        "evaluated_spec_sha": job.evaluated_spec_sha,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "stale": bool(reasons),
        "stale_reasons": reasons,
    }
```

- [ ] **Step 4: Mount the router** in `src/call_manager/main.py`. Find where `coherence_router` (or `coherence`) is imported and `include_router`-ed and add the evaluator alongside it:

```python
    from call_manager.proposal_assist.web.evaluator import router as evaluator_router
    app.include_router(evaluator_router)
```

> **Verify before writing:** grep `main.py` for `coherence` to copy the exact import + `include_router` placement (the coherence router is included there per `web/coherence.py:1` "mounted in main.py").

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/proposal_assist/evaluator/test_web_evaluator.py -q ; echo $?` → Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add src/call_manager/proposal_assist/web/evaluator.py src/call_manager/main.py \
        tests/proposal_assist/evaluator/test_web_evaluator.py
git commit -m "feat(evaluator): POST/GET endpoints (runs + latest + scorecard w/ staleness)"
```

---

## Task 10: Scheduler ticks + soft-delete cancellation

**Files:**
- Modify: `src/call_manager/scheduler.py` (add `_evaluator_tick` + `_evaluator_reaper_tick` next to the coherence ones, ~line 560)
- Modify: `src/call_manager/proposal_assist/web/workspaces.py` (soft-delete handler also cancels evaluator jobs)
- Test: `tests/proposal_assist/evaluator/test_scheduler_wiring.py`, `tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/proposal_assist/evaluator/test_scheduler_wiring.py
import pytest

from call_manager.config import Settings


@pytest.mark.asyncio
async def test_build_scheduler_registers_evaluator_ticks(monkeypatch, pg_url):
    from unittest.mock import MagicMock

    from call_manager.db.session import make_engine, make_session_factory
    from call_manager.proposal_assist.drafting.factory import build_workspace_controller
    from call_manager.scheduler import build_scheduler

    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("DRAFTING_ADAPTER", "fake")
    settings = Settings()
    engine = make_engine(settings.database_url)
    try:
        sf = make_session_factory(engine)
        controller = build_workspace_controller(settings=settings, session_factory=sf)

        class _Alerter:
            def record_failure(self, source_slug, *, error=None): ...
            def record_success(self, source_slug): ...

        sched = build_scheduler(
            session_factory=sf, settings=settings, alerter=_Alerter(),
            ollama_url="http://localhost", gemma_model="g", ranking_tick_seconds=60,
            ranking_queue_batch=10, embedding_pregate_threshold=0.3,
            embed_client=MagicMock(model="bge-m3"), workspace_controller=controller, start=False,
        )
        try:
            drain = sched.get_job("evaluator_drain")
            reaper = sched.get_job("evaluator_reaper")
            assert drain is not None
            assert drain.trigger.interval.total_seconds() == settings.evaluator_tick_seconds
            assert reaper is not None
            assert reaper.trigger.interval.total_seconds() == settings.evaluator_reaper_tick_seconds
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
```

```python
# tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py
from call_manager.db.models import EvaluatorJob
from tests.proposal_assist.conftest import make_workspace


def test_soft_delete_cancels_active_evaluator_jobs(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(EvaluatorJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/workspaces/{ws_id}/delete", follow_redirects=False)
    assert r.status_code in (200, 303)
    with pg_session_factory() as s:
        job = s.query(EvaluatorJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "failed"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/proposal_assist/evaluator/test_scheduler_wiring.py tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py -q ; echo $?`
Expected: FAIL — `get_job("evaluator_drain")` is None; soft-delete leaves the job queued.

- [ ] **Step 3a: Add the ticks** to `scheduler.py` immediately after the coherence reaper registration (~line 560). Mirror `_coherence_tick`/`_coherence_reaper_tick` exactly:

```python
    # SP2b-3a: evaluator drain tick (async — one fresh-stateless scorecard pass).
    def _evaluator_tick() -> None:
        import asyncio

        from call_manager.proposal_assist.evaluator.pass_runner import (
            drain_evaluator_queue_tick,
        )

        if not settings.drafting_enabled:
            return
        coro = drain_evaluator_queue_tick(session_factory=session_factory, settings=settings)
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("evaluator tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=settings.evaluator_pass_timeout_seconds + 60)
        except Exception:
            _logger.exception("evaluator tick failed")

    sched.add_job(
        _evaluator_tick,
        trigger=IntervalTrigger(seconds=settings.evaluator_tick_seconds),
        id="evaluator_drain",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-3a: evaluator reaper tick (sync, pure DB). Crash/hung-job recovery.
    def _evaluator_reaper_tick() -> None:
        from call_manager.proposal_assist.evaluator.jobs import reap_stuck_evaluator_jobs

        try:
            with session_factory() as s:
                reap_stuck_evaluator_jobs(s, timeout_seconds=settings.evaluator_job_timeout_seconds)
                s.commit()
        except Exception:
            _logger.exception("evaluator reaper tick failed")

    sched.add_job(
        _evaluator_reaper_tick,
        trigger=IntervalTrigger(seconds=settings.evaluator_reaper_tick_seconds),
        id="evaluator_reaper",
        max_instances=1,
        coalesce=True,
    )
```

> **Verify before writing:** confirm the in-scope names `settings`, `session_factory`, `app_loop`, `_logger`, `sched`, `IntervalTrigger` match the coherence block (they do — same function body). Copy the coherence block's exact indentation.

- [ ] **Step 3b: Cancel evaluator jobs on soft-delete.** In `web/workspaces.py`, find the soft-delete handler that calls `cancel_coherence_jobs_for_workspace` and add, right after it:

```python
    from call_manager.proposal_assist.evaluator.jobs import cancel_evaluator_jobs_for_workspace
    cancel_evaluator_jobs_for_workspace(session, workspace_id=workspace_id)
```

> **Verify before writing:** grep `web/workspaces.py` for `cancel_coherence_jobs_for_workspace` to copy the exact call site, the session variable name, and the `workspace_id` variable name in that scope.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/proposal_assist/evaluator/test_scheduler_wiring.py tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py -q ; echo $?` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/call_manager/scheduler.py src/call_manager/proposal_assist/web/workspaces.py \
        tests/proposal_assist/evaluator/test_scheduler_wiring.py \
        tests/proposal_assist/evaluator/test_soft_delete_cancels_evaluator.py
git commit -m "feat(evaluator): scheduler drain+reaper ticks + soft-delete cancellation"
```

---

## Task 11: Frontend — Scorecard tab

**Files:**
- Create: `frontend/src/api/evaluator.ts`
- Create: `frontend/src/cockpit/ScorecardPanel.tsx`
- Create: `frontend/src/cockpit/ScorecardPanel.test.tsx`
- Modify: `frontend/src/routes/Cockpit.tsx` (third view tab)
- Modify: `frontend/src/styles/cockpit.css` (band-pill styles)
- Modify: `frontend/src/api/schema.gen.ts` (regenerated)

- [ ] **Step 1: Regenerate the OpenAPI types** (the backend endpoints from Task 9 are live):

```bash
cd frontend && EMBEDDING_MODEL=bge-m3 pnpm gen:api
```
Confirm `git diff src/api/schema.gen.ts` shows the new `/api/workspaces/{workspace_id}/evaluator-runs`, `.../evaluator-runs/latest`, and `.../scorecard` paths.

- [ ] **Step 2: Write the failing test**

```tsx
// frontend/src/cockpit/ScorecardPanel.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ScorecardPanel } from "./ScorecardPanel";

vi.mock("../api/evaluator", () => ({
  useLatestEvaluator: () => ({ data: { status: "completed", error: null } }),
  useStartEvaluator: () => ({ mutate: vi.fn(), isPending: false }),
  useScorecard: () => ({
    data: {
      scorecard: {
        overall_band: "at_risk",
        summary: "Mostly there.",
        criteria: [
          { index: 0, name: "Quality", band: "strong", note: "Rigorous.",
            deep_link: null, weight: 0.6, effective_weight: 0.6 },
          { index: 1, name: "Impact", band: "gap", note: "No KPIs.",
            deep_link: { artifact: "draft", section_index: 1 }, weight: 0.4, effective_weight: 0.4 },
        ],
      },
      stale: true,
      stale_reasons: ["draft", "rubric"],
      completed_at: "2026-06-22T10:00:00Z",
    },
  }),
}));

function wrap(ui: ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ScorecardPanel", () => {
  it("renders criteria bands sorted by weight, overall band, and staleness", () => {
    wrap(<ScorecardPanel id="ws1" onNavigate={vi.fn()} />);
    expect(screen.getByText("Quality")).toBeInTheDocument();
    expect(screen.getByText("Impact")).toBeInTheDocument();
    expect(screen.getByText(/at.?risk/i)).toBeInTheDocument(); // overall band pill
    expect(screen.getByText(/changed since/i)).toBeInTheDocument();
    expect(screen.getByText(/draft, rubric/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd frontend && pnpm test -- ScorecardPanel ; echo $?`
Expected: FAIL — cannot resolve `./ScorecardPanel`.

- [ ] **Step 4a: Create `frontend/src/api/evaluator.ts`**:

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";

export type EvaluatorRun = {
  id: string;
  status: "queued" | "running" | "completed" | "failed";
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type ScorecardCriterion = {
  index: number;
  name: string;
  band: "strong" | "adequate" | "at_risk" | "gap";
  note: string;
  deep_link: { artifact: "draft" | "spec"; section_index: number } | null;
  weight: number;
  effective_weight: number;
};

export type ScorecardResponse = {
  scorecard: {
    criteria: ScorecardCriterion[];
    overall_band: "strong" | "adequate" | "at_risk" | "gap";
    summary: string;
  };
  evaluated_draft_sha: string | null;
  evaluated_spec_sha: string | null;
  completed_at: string | null;
  stale: boolean;
  stale_reasons: string[];
};

/** GET latest evaluator run (or null). Polls while queued/running. */
export function useLatestEvaluator(id: string) {
  return useQuery({
    queryKey: ["evaluator-latest", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/evaluator-runs/latest", {
          params: { path: { workspace_id: id } },
        }),
      ) as EvaluatorRun | null,
    refetchInterval: (q) => {
      const s = (q.state.data as EvaluatorRun | null)?.status;
      return s === "queued" || s === "running" ? 1500 : false;
    },
  });
}

/** GET the latest completed scorecard (or null). */
export function useScorecard(id: string) {
  return useQuery({
    queryKey: ["scorecard", id],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/scorecard", {
          params: { path: { workspace_id: id } },
        }),
      ) as ScorecardResponse | null,
  });
}

/** POST an evaluator run (create-or-return-active). Refresh the scorecard on completion. */
export function useStartEvaluator(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/evaluator-runs", {
          params: { path: { workspace_id: id } },
        }),
      ) as EvaluatorRun,
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ["evaluator-latest", id] });
      if (run.status === "completed") {
        qc.invalidateQueries({ queryKey: ["scorecard", id] });
      }
    },
  });
}
```

- [ ] **Step 4b: Create `frontend/src/cockpit/ScorecardPanel.tsx`**:

```tsx
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  type ScorecardCriterion,
  useLatestEvaluator,
  useScorecard,
  useStartEvaluator,
} from "../api/evaluator";

const BAND_CLASS: Record<ScorecardCriterion["band"], string> = {
  strong: "band-strong",
  adequate: "band-adequate",
  at_risk: "band-at-risk",
  gap: "band-gap",
};
const BAND_LABEL: Record<ScorecardCriterion["band"], string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};

export function ScorecardPanel({
  id,
  onNavigate,
}: {
  id: string;
  onNavigate: (artifact: "draft" | "spec", sectionIndex: number) => void;
}) {
  const latest = useLatestEvaluator(id);
  const start = useStartEvaluator(id);
  const scorecard = useScorecard(id);
  const status = latest.data?.status;
  const running = status === "queued" || status === "running";
  const qc = useQueryClient();
  useEffect(() => {
    if (status === "completed") qc.invalidateQueries({ queryKey: ["scorecard", id] });
  }, [status, id, qc]);

  const data = scorecard.data;
  const criteria = data
    ? [...data.scorecard.criteria].sort((a, b) => b.effective_weight - a.effective_weight)
    : [];

  return (
    <main className="scorecard-shell">
      <div className="scorecard-header">
        <h1>Scorecard</h1>
        {data && (
          <span className={`band-pill ${BAND_CLASS[data.scorecard.overall_band]}`}>
            {BAND_LABEL[data.scorecard.overall_band]}
          </span>
        )}
        <button type="button" disabled={running || start.isPending} onClick={() => start.mutate()}>
          {running ? "Evaluating…" : "Re-evaluate"}
        </button>
        {status === "failed" && (
          <span className="scorecard-error" role="status">
            {latest.data?.error ?? "Evaluation failed."}
          </span>
        )}
      </div>

      {data && (
        <p className="scorecard-meta">
          evaluated {data.completed_at ? new Date(data.completed_at).toLocaleString() : ""}
          {data.stale && <span className="scorecard-stale"> · ⚠ changed since: {data.stale_reasons.join(", ")}</span>}
        </p>
      )}

      {!data && <p className="scorecard-empty">No evaluation yet — Re-evaluate to generate a scorecard.</p>}
      {data && criteria.length === 0 && (
        <p className="scorecard-empty">This call has no extracted criteria to evaluate.</p>
      )}

      <ul className="scorecard-list">
        {criteria.map((c) => (
          <li key={c.index} className="scorecard-row">
            <span className={`band-pill ${BAND_CLASS[c.band]}`}>{BAND_LABEL[c.band]}</span>
            <span className="scorecard-name">{c.name}</span>
            <span className="scorecard-weight">w {c.weight}</span>
            {c.deep_link && (
              <button
                type="button"
                className="scorecard-link"
                onClick={() => onNavigate(c.deep_link!.artifact, c.deep_link!.section_index)}
              >
                Go to §{c.deep_link.section_index}
              </button>
            )}
            <p className="scorecard-note">{c.note}</p>
          </li>
        ))}
      </ul>
    </main>
  );
}
```

- [ ] **Step 4c: Wire the third tab** in `frontend/src/routes/Cockpit.tsx`:

```tsx
import { useState } from "react";
import { useParams } from "react-router-dom";

import { DraftPanel } from "../cockpit/DraftPanel";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { SpecPanel } from "../cockpit/SpecPanel";

type View = "draft" | "spec" | "scorecard";

export function Cockpit() {
  const { id = "" } = useParams();
  const [view, setView] = useState<View>("draft");
  const onNavigate = (artifact: "draft" | "spec") => setView(artifact);
  return (
    <div className="cockpit-shell">
      <div role="tablist" aria-label="Artifact">
        <button type="button" role="tab" aria-selected={view === "draft"} onClick={() => setView("draft")}>
          Draft
        </button>
        <button type="button" role="tab" aria-selected={view === "spec"} onClick={() => setView("spec")}>
          Spec
        </button>
        <button type="button" role="tab" aria-selected={view === "scorecard"} onClick={() => setView("scorecard")}>
          Scorecard
        </button>
      </div>
      {view === "draft" && <DraftPanel id={id} />}
      {view === "spec" && <SpecPanel id={id} />}
      {view === "scorecard" && <ScorecardPanel id={id} onNavigate={onNavigate} />}
    </div>
  );
}
```

> **Deep-link floor (spec §4.4):** `onNavigate` switches the view tab to the target artifact. Scrolling to the exact section is a deferred enhancement (the `ArtifactSectionEditor` section-anchor contract isn't established in this slice) — `sectionIndex` is accepted by the callback signature so the enhancement is non-breaking. Keep the tab-switch behavior.

- [ ] **Step 4d: Add band-pill styles** to `frontend/src/styles/cockpit.css` (reuse the existing semantic tokens; the `.band-pill` base mirrors `.source-viewer-stance` at line 94):

```css
.band-pill {
  display: inline-block;
  font-size: 0.75em;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  padding: 0 0.4rem;
  border-radius: 999px;
  border: 1px solid transparent;
}
.band-strong   { background: var(--cm-good-bg);    color: var(--cm-good-text);    border-color: var(--cm-good-border); }
.band-adequate { background: var(--cm-neutral-bg); color: var(--cm-neutral-text); border-color: var(--cm-neutral-border); }
.band-at-risk  { background: var(--cm-warn-bg);    color: var(--cm-warn-text);    border-color: var(--cm-warn-border); }
.band-gap      { background: var(--cm-bad-bg);     color: var(--cm-bad-text);     border-color: var(--cm-bad-border); }
.scorecard-row { list-style: none; border-top: 1px solid var(--cm-border); padding-top: 0.5rem; margin-top: 0.5rem; }
.scorecard-note { margin: 0.25rem 0 0; white-space: pre-wrap; }
.scorecard-stale { color: var(--cm-warn-text); }
.scorecard-error { color: var(--cm-bad-text); }
```

- [ ] **Step 5: Run to verify it passes + type-check**

```bash
cd frontend
pnpm test -- ScorecardPanel ; echo $?      # Expected: PASS
pnpm exec tsc --noEmit ; echo $?           # Expected: 0 (no type errors)
```
Also run the existing Cockpit test to confirm the tab change didn't break it:
`pnpm test -- Cockpit ; echo $?` → Expected: PASS (adjust `Cockpit.test.tsx` only if it asserted exactly two tabs).

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/evaluator.ts frontend/src/cockpit/ScorecardPanel.tsx \
        frontend/src/cockpit/ScorecardPanel.test.tsx frontend/src/routes/Cockpit.tsx \
        frontend/src/styles/cockpit.css frontend/src/api/schema.gen.ts
git commit -m "feat(evaluator): Scorecard tab (bands, readiness, staleness, deep-link)"
```

---

## Final verification (after all tasks)

- [ ] Backend — run the whole evaluator suite + the touched-tool suite, capture exit codes:
```bash
uv run pytest tests/proposal_assist/evaluator tests/proposal_assist/tools -q ; echo $?
```
Expected: all pass (exit 0). Re-run any `connection refused` failures in isolation (testcontainer flake).
- [ ] `uv run ruff check src/call_manager tests/proposal_assist/evaluator && uv run ruff format --check src/call_manager/proposal_assist/evaluator src/call_manager/proposal_assist/tools/scorecard.py src/call_manager/proposal_assist/web/evaluator.py` — clean.
- [ ] `uv run alembic heads` → `0054 (head)` (single head).
- [ ] Frontend — `cd frontend && pnpm test ; echo $?` and `pnpm exec tsc --noEmit ; echo $?` — both clean. No `schema.gen.ts` drift (re-run `EMBEDDING_MODEL=bge-m3 pnpm gen:api`; `git diff --exit-code src/api/schema.gen.ts`).
- [ ] Then hand off to **superpowers:finishing-a-development-branch**.

---

## Self-review notes (plan author)

- **Spec coverage:** §1 data model → T1; §1.2 scorecard shape → T5/T11; §2.1 context → T3; §2.2 agent → T7; §2.3 submit_scorecard → T5; §2.4 roll-up → T4; §2.5 jobs → T6; §2.6 pass runner → T8; §3.1 endpoints → T9; §3.2 scheduler → T10; §3.3 config → T2; §3.4 soft-delete → T10; §4 frontend → T11; §5 tests → every task. All covered.
- **Staleness implementation note:** the spec calls the markers `evaluated_draft_sha`/`evaluated_spec_sha`; this plan defines them as **per-artifact content hashes** (sha256 of file bytes) rather than the shared repo HEAD, because both artifacts share one git repo and the repo HEAD can't distinguish a draft-only edit from a spec-only edit — which is exactly the per-input staleness Codex's review required. Same column names, faithful to the spec's intent.
- **Type consistency:** `EvaluatorJob`, `submit_scorecard_impl`, `compute_overall_band`, `assemble_evaluator_context`/`EvaluatorContext`, `evaluator_allowed_tools`, `build_evaluator_adapter(..., draft_section_count, spec_section_count)`, `run_evaluator_pass`/`drain_evaluator_queue_tick`, the `ToolContext.eval_draft_section_count`/`eval_spec_section_count` fields, and the frontend `EvaluatorRun`/`ScorecardResponse` types are used consistently across tasks.
- **Verify-against-codebase notes** are flagged inline (Section attr name, `JSONB` import, adapter ctor/attrs, `_stockholm_day_start_utc` import, `main.py` mount site, the `cancel_coherence_jobs_for_workspace` call site).
- **Codex plan-review fixes applied (2026-06-22):** (1) `submit_scorecard_impl` row-locks the target and requires it to be THIS workspace's still-`running` job before writing; (2) the context reads each artifact's bytes ONCE and returns `draft_sha`/`spec_sha`, which the pass runner records as the staleness markers — no TOCTOU re-read; (3) Task 3 counts corrected for the sectionizer's always-present section-0 preamble (`## Aims…` → 2 sections); (4) Task 4 threshold tests corrected (score 2.50 → `strong`) with true fractional-weight boundary cases; (5) the claim test uses two workspaces (the active-unique index forbids queued+running in one); (6) the web test no longer nulls the non-null `rubric_updated_at` and seeds the job's rubric marker from the workspace's real value.
