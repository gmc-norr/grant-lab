# SP2b-3d — Uncited-claims (LLM) Problems Signal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an LLM-backed "uncited claims" check to the cockpit — a manual, disclosure-gated background pass that scans the DRAFT for factual claims lacking a citation and surfaces them as a 5th, dismissable Problems signal.

**Architecture:** A faithful copy-not-generalize clone of the evaluator (`proposal_assist/evaluator/`): its own package (`uncited_claims/`), job table (`uncited_claims_jobs`, migration 0055), background-only submit tool, endpoints, and scheduler ticks — plus a new pure builder in the existing `problems/` package and additions to the React `ProblemsPanel`. No generalization of the shared job machinery.

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x / Alembic / APScheduler (single-instance) / Claude Agent SDK adapter (hosted `claude-opus-4-7`); React 18 + Vite + TanStack Query + openapi-fetch.

**Spec:** `docs/superpowers/specs/2026-06-22-cockpit-sp2b-3d-uncited-claims-design.md`

## Global Constraints

- **Branch/worktree:** work in `.worktrees/sp2b-3d-uncited-claims` on branch `feat/sp2b-3d-uncited-claims` (rebased onto master `17bebde`). Subagents MUST `git -C <worktree>` + verify the branch before committing (the worktree-cwd gotcha — git ops default to the main tree).
- **Commits:** sign-off trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`; commit with `git -c commit.gpgsign=false`.
- **Tests are testcontainer-flaky in bulk:** run the affected files only, capture exit codes, NEVER pipe pytest through `tail`. Re-run any `connection refused` failure in isolation before treating it as real.
- **Frontend has no TS linter** — the gate is `pnpm exec tsc --noEmit` (+ `pnpm vitest run`). Run from `frontend/`.
- **OpenAPI client regen** (Task 7 only): `EMBEDDING_MODEL=bge-m3 pnpm gen:api` from `frontend/` (the default `.env` model logs a WARNING to stdout that corrupts `schema.gen.ts`).
- **Must NOT touch:** `drafting/claude_adapter.py`, `drafting/contract.py`, and the `evaluator/` / `coherence/` / `review/` packages — copy from them, never edit them. `claude_adapter.py`'s interactive default allowlist intentionally excludes background-only submit tools.
- **Migration head is 0054.** New migration is `0055`, `down_revision = "0054"`.
- **Dismissal event kind is `uncited_claim_dismissed`** — a DISTINCT kind. NEVER reuse `user_claim_dismissed` (that is `web/annotations.py`'s annotation-dismissal event, payload `{annotation_id, reason}`).
- **Hosted model:** the pass uses `settings.proposal_assist_llm_model` (hosted Opus), like the siblings.

---

## File Structure

**Create (backend):**
- `alembic/versions/0055_uncited_claims_jobs.py` — the queue table migration.
- `src/call_manager/proposal_assist/uncited_claims/__init__.py`
- `src/call_manager/proposal_assist/uncited_claims/jobs.py` — pure-DB queue helpers + dismissed-set.
- `src/call_manager/proposal_assist/uncited_claims/context.py` — assemble instruction + pinned section texts + draft sha.
- `src/call_manager/proposal_assist/uncited_claims/agent.py` — `build_uncited_claims_adapter` + `uncited_claims_allowed_tools`.
- `src/call_manager/proposal_assist/uncited_claims/pass_runner.py` — run one pass (hardening + substring filter + hash/dedupe) + drain tick.
- `src/call_manager/proposal_assist/tools/uncited_claims.py` — `submit_uncited_claims_impl`.
- `src/call_manager/proposal_assist/web/uncited_claims.py` — run/latest/dismiss endpoints (with response_models).
- `frontend/src/api/uncitedClaims.ts` — TanStack Query hooks (generated DTOs).

**Modify (backend):**
- `src/call_manager/db/models.py` — `+ UncitedClaimsJob`.
- `src/call_manager/proposal_assist/tools/registry.py` — `ToolContext.uncited_draft_section_count` + register `submit_uncited_claims` in `_collect_tools`.
- `src/call_manager/proposal_assist/problems/builders.py` — `+ uncited_claim_problems`, extend `_row` with `claim_hash`, add `uncited_claim` to category rank.
- `src/call_manager/proposal_assist/problems/aggregate.py` — 5th signal + `uncited_claims_stale` response field.
- `src/call_manager/proposal_assist/web/workspaces.py` — soft-delete: call `cancel_uncited_claims_jobs_for_workspace`.
- `src/call_manager/config.py` — six `uncited_claims_*` fields.
- `src/call_manager/scheduler.py` — drain + reaper ticks.
- `src/call_manager/main.py` — include the router.

**Modify (frontend):**
- `frontend/src/api/problems.ts` — `uncited_claim` category, optional `claim_hash`, `uncited_claims_stale`.
- `frontend/src/cockpit/ProblemsPanel.tsx` — run button + dismiss action + stale render.
- `frontend/src/api/schema.gen.ts` — regenerated (Task 7).

**Modify (tests — already exist):**
- `tests/proposal_assist/test_tools_registry.py`, `tests/proposal_assist/test_tools_claude_adapter_wiring.py` — extend for the new background tool (Task 3).

**Create (tests):** under `tests/proposal_assist/uncited_claims/` and `tests/proposal_assist/problems/`, mirroring the evaluator suite (see each task).

---

## Task 1: Migration 0055 + `UncitedClaimsJob` model

**Files:**
- Create: `alembic/versions/0055_uncited_claims_jobs.py`
- Modify: `src/call_manager/db/models.py` (add `UncitedClaimsJob` after `EvaluatorJob`, ~line 971)
- Test: `tests/proposal_assist/uncited_claims/__init__.py` (empty), `tests/proposal_assist/uncited_claims/test_jobs_model.py`

**Interfaces:**
- Produces: SQLAlchemy model `UncitedClaimsJob` with columns `id, workspace_id, status, requested_by, error, claims (JSONB), evaluated_draft_sha, queued_at, started_at, completed_at, sdk_session_id, model, input_tokens, output_tokens`; table `uncited_claims_jobs` with `ix_uncited_claims_jobs_ws_queued` + partial-unique `uq_uncited_claims_jobs_active`.

- [ ] **Step 1: Write the migration** — `alembic/versions/0055_uncited_claims_jobs.py`. Mirror `0054_evaluator_jobs.py`, dropping `scorecard` / `evaluated_spec_sha` / `evaluated_rubric_updated_at` and adding `claims`:

```python
"""uncited_claims_jobs queue

Revision ID: 0055
Revises: 0054

The uncited-claims (LLM) background-pass queue. Mirrors evaluator_jobs (active-unique
over status IN ('queued','running')) but drops the spec/rubric staleness markers (this
check depends only on the draft) and replaces scorecard with the claims JSONB.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "uncited_claims_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claims", JSONB(), nullable=True),
        sa.Column("evaluated_draft_sha", sa.Text(), nullable=True),
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
            ["workspace_id"], ["workspaces.id"], ondelete="CASCADE",
            name="fk_uncited_claims_jobs_ws",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], ondelete="SET NULL",
            name="fk_uncited_claims_jobs_requested_by",
        ),
    )
    op.create_check_constraint(
        "ck_uncited_claims_jobs_status",
        "uncited_claims_jobs",
        "status IN ('queued','running','completed','failed')",
    )
    op.create_index(
        "ix_uncited_claims_jobs_ws_queued", "uncited_claims_jobs", ["workspace_id", "queued_at"]
    )
    op.create_index(
        "uq_uncited_claims_jobs_active",
        "uncited_claims_jobs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_uncited_claims_jobs_active", table_name="uncited_claims_jobs")
    op.drop_index("ix_uncited_claims_jobs_ws_queued", table_name="uncited_claims_jobs")
    op.drop_table("uncited_claims_jobs")
```

- [ ] **Step 2: Add the model** — in `src/call_manager/db/models.py`, immediately after the `EvaluatorJob` class (after ~line 970, before `class ChatSummary`). Uses imports already present in the file (`JSONB, UUID, Text, BigInteger, Integer, DateTime, ForeignKey, func, text, Index, CheckConstraint, Mapped, mapped_column, datetime, uuid`):

```python
class UncitedClaimsJob(Base):
    __tablename__ = "uncited_claims_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_uncited_claims_jobs_status",
        ),
        Index("ix_uncited_claims_jobs_ws_queued", "workspace_id", "queued_at"),
        # At most ONE active (queued OR running) uncited-claims job per workspace —
        # makes POST "create-or-return-active" race-safe. Independent of the
        # evaluator/coherence/review slots (separate tables).
        Index(
            "uq_uncited_claims_jobs_active",
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
    # Raw list written by the submit_uncited_claims tool while running; OVERWRITTEN by
    # the pass runner at finalize with the hashed/substring-filtered/deduped final list.
    # Read only when status == 'completed'.
    claims: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Draft CONTENT sha captured at run start (this check depends only on the draft).
    evaluated_draft_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
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

- [ ] **Step 3: Write the model/migration smoke test** — `tests/proposal_assist/uncited_claims/__init__.py` (empty file) and `tests/proposal_assist/uncited_claims/test_jobs_model.py`. The `pg_session_factory` fixture applies all migrations on the testcontainer, so inserting a row verifies the migration applied AND the active-unique constraint:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import UncitedClaimsJob
from tests.proposal_assist.conftest import make_workspace


def test_row_roundtrips_with_claims_jsonb(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = UncitedClaimsJob(
            workspace_id=ws.id,
            status="completed",
            claims=[{"claim_text": "x", "section_index": 0, "reason": "r", "claim_hash": "h"}],
            evaluated_draft_sha="abc",
        )
        s.add(job)
        s.commit()
        got = s.get(UncitedClaimsJob, job.id)
        assert got.claims[0]["claim_hash"] == "h"
        assert got.input_tokens == 0 and got.output_tokens == 0


def test_active_unique_blocks_two_active_in_one_workspace(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="queued"))
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="running"))
        with pytest.raises(IntegrityError):
            s.flush()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/proposal_assist/uncited_claims/test_jobs_model.py -p no:cacheprovider -q`
Expected: `2 passed`. (If `connection refused`, re-run in isolation — testcontainer flake.)

- [ ] **Step 5: Commit**

```bash
git -C .worktrees/sp2b-3d-uncited-claims add -A
git -C .worktrees/sp2b-3d-uncited-claims -c commit.gpgsign=false commit -m "feat(uncited-claims): migration 0055 + UncitedClaimsJob model

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `uncited_claims/jobs.py` — queue helpers + dismissed-set

**Files:**
- Create: `src/call_manager/proposal_assist/uncited_claims/__init__.py` (empty), `src/call_manager/proposal_assist/uncited_claims/jobs.py`
- Test: `tests/proposal_assist/uncited_claims/test_jobs.py`

**Interfaces:**
- Consumes: `UncitedClaimsJob`, `Workspace`, `WorkspaceEvent` (models); `Settings`; `_stockholm_day_start_utc`.
- Produces:
  - `enqueue_uncited_claims_run(session, *, workspace_id, requested_by) -> UncitedClaimsJob`
  - `claim_next_uncited_claims_job(session) -> UncitedClaimsJob | None`
  - `cancel_uncited_claims_jobs_for_workspace(session, *, workspace_id) -> int`
  - `uncited_claims_cap_exceeded(session, *, workspace_id, settings) -> bool`
  - `reap_stuck_uncited_claims_jobs(session, *, timeout_seconds) -> int`
  - `latest_uncited_claims_job(session, *, workspace_id) -> UncitedClaimsJob | None`
  - `latest_completed_uncited_claims_job(session, *, workspace_id) -> UncitedClaimsJob | None`
  - `dismissed_claim_hashes(session, *, workspace_id) -> set[str]`

- [ ] **Step 1: Write the failing tests** — `tests/proposal_assist/uncited_claims/test_jobs.py`. Mirrors `evaluator/test_jobs.py` plus the dismissed-hashes test the spec calls out (reads `uncited_claim_dismissed` only, ignores the annotation `user_claim_dismissed`):

```python
from datetime import UTC, datetime, timedelta

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob, WorkspaceEvent
from call_manager.proposal_assist.uncited_claims import jobs
from tests.proposal_assist.conftest import make_user, make_workspace


def test_enqueue_create_or_return_active(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        a = jobs.enqueue_uncited_claims_run(s, workspace_id=ws.id, requested_by=None)
        s.commit()
        b = jobs.enqueue_uncited_claims_run(s, workspace_id=ws.id, requested_by=None)
        s.commit()
        assert a.id == b.id


def test_claim_picks_queued_skips_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="running"))
        ws2 = make_workspace(s, owner=make_user(s, email="ws2-owner@ki.se"))
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws2.id, status="queued"))
        s.commit()
        claimed = jobs.claim_next_uncited_claims_job(s)
        assert claimed is not None and claimed.workspace_id == ws2.id
        assert claimed.status == "running" and claimed.started_at is not None


def test_claim_skips_soft_deleted_workspace(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        ws.soft_deleted_at = datetime.now(UTC)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="queued"))
        s.commit()
        assert jobs.claim_next_uncited_claims_job(s) is None


def test_cancel_atomic_leaves_completed_untouched(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        done = UncitedClaimsJob(workspace_id=ws.id, status="completed", claims=[])
        running = UncitedClaimsJob(workspace_id=ws.id, status="running")
        s.add_all([done, running])
        s.commit()
        n = jobs.cancel_uncited_claims_jobs_for_workspace(s, workspace_id=ws.id)
        s.refresh(done)
        s.refresh(running)
        assert n == 1 and done.status == "completed" and running.status == "failed"


def test_reaper_fails_stale_running(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = UncitedClaimsJob(
            workspace_id=ws.id, status="running",
            started_at=datetime.now(UTC) - timedelta(seconds=600),
        )
        s.add(job)
        s.commit()
        n = jobs.reap_stuck_uncited_claims_jobs(s, timeout_seconds=300)
        s.refresh(job)
        assert n == 1 and job.status == "failed"


def test_cap_blocks_over_budget(pg_session_factory, _settings_env):
    settings = Settings()
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(
            workspace_id=ws.id, status="completed",
            input_tokens=settings.uncited_claims_daily_token_cap, output_tokens=0,
        ))
        s.commit()
        assert jobs.uncited_claims_cap_exceeded(s, workspace_id=ws.id, settings=settings) is True


def test_latest_completed_returns_completed_only(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="completed", claims=[]))
        s.commit()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="failed"))
        s.commit()
        job = jobs.latest_completed_uncited_claims_job(s, workspace_id=ws.id)
        assert job is not None and job.status == "completed"


def test_dismissed_hashes_reads_only_the_uncited_kind(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        # the feature's own dismissal events
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="uncited_claim_dismissed",
                             payload={"claim_hash": "aaa"}))
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="uncited_claim_dismissed",
                             payload={"claim_hash": "bbb"}))
        # a DIFFERENT feature's event in the same workspace — MUST be ignored
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="user_claim_dismissed",
                             payload={"annotation_id": "n1", "reason": "x"}))
        s.commit()
        assert jobs.dismissed_claim_hashes(s, workspace_id=ws.id) == {"aaa", "bbb"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/proposal_assist/uncited_claims/test_jobs.py -p no:cacheprovider -q`
Expected: collection/import error (`No module named ...uncited_claims.jobs`).

- [ ] **Step 3: Implement** — `src/call_manager/proposal_assist/uncited_claims/__init__.py` (empty) and `jobs.py`. Mirrors `evaluator/jobs.py`; adds `dismissed_claim_hashes`:

```python
"""Pure-DB helpers for the uncited_claims_jobs queue. The caller owns the transaction
(these flush, never commit), mirroring evaluator/jobs.py. The final claims list is a
single JSONB on the job row, so 'latest completed wins' is a read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.turns.caps import _stockholm_day_start_utc


def enqueue_uncited_claims_run(
    session: Session, *, workspace_id: uuid.UUID, requested_by: int | None
) -> UncitedClaimsJob:
    """Create a queued job, or return the existing active (queued|running) one.
    Race-safe against uq_uncited_claims_jobs_active via a SAVEPOINT."""
    existing = session.scalar(
        select(UncitedClaimsJob).where(
            UncitedClaimsJob.workspace_id == workspace_id,
            UncitedClaimsJob.status.in_(("queued", "running")),
        )
    )
    if existing is not None:
        return existing
    job = UncitedClaimsJob(workspace_id=workspace_id, status="queued", requested_by=requested_by)
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        if job in session:
            session.expunge(job)
        winner = session.scalar(
            select(UncitedClaimsJob).where(
                UncitedClaimsJob.workspace_id == workspace_id,
                UncitedClaimsJob.status.in_(("queued", "running")),
            )
        )
        if winner is None:
            raise RuntimeError(
                "uncited-claims enqueue lost the active-unique race but found no active job"
            ) from None
        return winner
    return job


def claim_next_uncited_claims_job(session: Session) -> UncitedClaimsJob | None:
    """Claim the oldest queued job whose workspace is LIVE and has no running job.
    FOR UPDATE SKIP LOCKED. Soft-deleted workspaces excluded (no post-delete spend)."""
    running_ws = select(UncitedClaimsJob.workspace_id).where(UncitedClaimsJob.status == "running")
    live_ws = select(Workspace.id).where(Workspace.soft_deleted_at.is_(None))
    job = session.scalar(
        select(UncitedClaimsJob)
        .where(
            UncitedClaimsJob.status == "queued",
            UncitedClaimsJob.workspace_id.not_in(running_ws),
            UncitedClaimsJob.workspace_id.in_(live_ws),
        )
        .order_by(UncitedClaimsJob.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    job.status = "running"
    job.started_at = datetime.now(UTC)
    session.flush()
    return job


def cancel_uncited_claims_jobs_for_workspace(session: Session, *, workspace_id: uuid.UUID) -> int:
    """Fail any active (queued|running) job for a workspace (soft-delete handler). ONE
    atomic UPDATE ... RETURNING — never an ORM load-then-write (which could clobber a job
    the success finalizer is concurrently completing). Mirrors the evaluator helper."""
    cancelled_ids = (
        session.execute(
            update(UncitedClaimsJob)
            .where(
                UncitedClaimsJob.workspace_id == workspace_id,
                UncitedClaimsJob.status.in_(("queued", "running")),
            )
            .values(status="failed", error="workspace deleted", completed_at=datetime.now(UTC))
            .returning(UncitedClaimsJob.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    session.flush()
    return len(cancelled_ids)


def uncited_claims_cap_exceeded(
    session: Session, *, workspace_id: uuid.UUID, settings: Settings
) -> bool:
    """True if today's (Stockholm-day) in+out tokens are at/over the cap."""
    day_start = _stockholm_day_start_utc()
    total = session.scalar(
        select(
            func.coalesce(
                func.sum(UncitedClaimsJob.input_tokens + UncitedClaimsJob.output_tokens), 0
            )
        ).where(
            UncitedClaimsJob.workspace_id == workspace_id,
            UncitedClaimsJob.queued_at >= day_start,
        )
    )
    return int(total or 0) >= settings.uncited_claims_daily_token_cap


def reap_stuck_uncited_claims_jobs(session: Session, *, timeout_seconds: int) -> int:
    """Crash/restart recovery: fail any running job older than the TTL. FOR UPDATE SKIP LOCKED."""
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_seconds)
    rows = session.scalars(
        select(UncitedClaimsJob)
        .where(UncitedClaimsJob.status == "running", UncitedClaimsJob.started_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in rows:
        job.status = "failed"
        job.error = "timed out"
        job.completed_at = datetime.now(UTC)
    session.flush()
    return len(rows)


def latest_uncited_claims_job(
    session: Session, *, workspace_id: uuid.UUID
) -> UncitedClaimsJob | None:
    return session.scalar(
        select(UncitedClaimsJob)
        .where(UncitedClaimsJob.workspace_id == workspace_id)
        .order_by(UncitedClaimsJob.queued_at.desc())
        .limit(1)
    )


def latest_completed_uncited_claims_job(
    session: Session, *, workspace_id: uuid.UUID
) -> UncitedClaimsJob | None:
    return session.scalar(
        select(UncitedClaimsJob)
        .where(
            UncitedClaimsJob.workspace_id == workspace_id,
            UncitedClaimsJob.status == "completed",
        )
        .order_by(UncitedClaimsJob.queued_at.desc())
        .limit(1)
    )


def dismissed_claim_hashes(session: Session, *, workspace_id: uuid.UUID) -> set[str]:
    """Distinct claim_hash values dismissed in this workspace. Reads ONLY the feature's
    own kind 'uncited_claim_dismissed' — NEVER the annotation 'user_claim_dismissed'."""
    rows = session.scalars(
        select(WorkspaceEvent.payload["claim_hash"].astext).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.kind == "uncited_claim_dismissed",
        )
    ).all()
    return {h for h in rows if h}
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/proposal_assist/uncited_claims/test_jobs.py -p no:cacheprovider -q`
Expected: `8 passed`.

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/uncited_claims/jobs.py tests/proposal_assist/uncited_claims/test_jobs.py
uv run ruff format src/call_manager/proposal_assist/uncited_claims/jobs.py tests/proposal_assist/uncited_claims/test_jobs.py
git -C .worktrees/sp2b-3d-uncited-claims add -A
git -C .worktrees/sp2b-3d-uncited-claims -c commit.gpgsign=false commit -m "feat(uncited-claims): jobs.py queue helpers + dismissed-set

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `submit_uncited_claims` tool + `ToolContext` field + registry + adapter helper + wiring-test updates

**Files:**
- Create: `src/call_manager/proposal_assist/tools/uncited_claims.py`, `src/call_manager/proposal_assist/uncited_claims/agent.py`
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (`ToolContext.uncited_draft_section_count` + register the tool), `tests/proposal_assist/test_tools_registry.py`, `tests/proposal_assist/test_tools_claude_adapter_wiring.py`
- Test: `tests/proposal_assist/uncited_claims/test_submit.py`

**Interfaces:**
- Consumes: `ToolContext` (now with `uncited_draft_section_count`), `UncitedClaimsJob`.
- Produces:
  - `submit_uncited_claims_impl(ctx, *, claims: list[dict]) -> dict` (returns `{"stored": True, "count": n}` or `{"error": ...}`)
  - `uncited_claims_allowed_tools() -> list[str]` (returns `["submit_uncited_claims"]`)
  - `build_uncited_claims_adapter(*, settings, session_factory, workspace_id, requested_by, job_id, draft_section_count) -> ClaudeAgentSDKAdapter`
  - registered `submit_uncited_claims` MCP tool in `_collect_tools` (24th tool, background-only).

- [ ] **Step 1: Write the failing tool test** — `tests/proposal_assist/uncited_claims/test_submit.py`. Mirrors `evaluator/test_submit_scorecard.py`'s guards (range vs pinned count, only-while-running, exactly-once, bool rejection):

```python
import pytest

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob
from call_manager.proposal_assist.tools.registry import ToolContext
from call_manager.proposal_assist.tools.uncited_claims import submit_uncited_claims_impl
from tests.proposal_assist.conftest import make_workspace


def _ctx(tmp_path, session_factory, ws_id, job_id, *, draft_n=2):
    return ToolContext(
        workspace_id=ws_id,
        repo_dir=tmp_path,
        session_factory=session_factory,
        settings=Settings(),
        origin_kind="uncited_claims_session",
        origin_job_id=job_id,
        uncited_draft_section_count=draft_n,
    )


def _setup(pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = UncitedClaimsJob(workspace_id=ws.id, status="running")
        s.add(job)
        s.commit()
        return ws.id, job.id


@pytest.mark.asyncio
async def test_valid_claims_are_written_raw(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(
        ctx,
        claims=[
            {"claim_text": "30% of patients relapse", "section_index": 1, "reason": "statistic"},
            {"claim_text": "X outperforms Y", "section_index": 0, "reason": "comparative"},
        ],
    )
    assert res["stored"] is True and res["count"] == 2
    with pg_session_factory() as s:
        job = s.get(UncitedClaimsJob, job_id)
        # raw shape: no claim_hash yet (the runner adds it at finalize)
        assert job.claims[0]["claim_text"] == "30% of patients relapse"
        assert "claim_hash" not in job.claims[0]


@pytest.mark.asyncio
async def test_empty_claim_text_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "  ", "section_index": 0, "reason": "x"}]
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_section_index_out_of_range_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id, draft_n=1)
    res = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "a", "section_index": 5, "reason": "x"}]
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_bool_section_index_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "a", "section_index": True, "reason": "x"}]
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_only_while_running(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    with pg_session_factory() as s:
        s.get(UncitedClaimsJob, job_id).status = "failed"
        s.commit()
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "a", "section_index": 0, "reason": "x"}]
    )
    assert "error" in res


@pytest.mark.asyncio
async def test_second_submit_rejected(tmp_path, pg_session_factory, _settings_env):
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    first = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "a", "section_index": 0, "reason": "first"}]
    )
    assert first["stored"] is True
    second = await submit_uncited_claims_impl(
        ctx, claims=[{"claim_text": "b", "section_index": 0, "reason": "second"}]
    )
    assert "error" in second
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims[0]["reason"] == "first"


@pytest.mark.asyncio
async def test_empty_claims_list_is_allowed(tmp_path, pg_session_factory, _settings_env):
    # zero uncited claims is a valid, healthy result — must store [] (not None), not error
    ws_id, job_id = _setup(pg_session_factory)
    ctx = _ctx(tmp_path, pg_session_factory, ws_id, job_id)
    res = await submit_uncited_claims_impl(ctx, claims=[])
    assert res["stored"] is True and res["count"] == 0
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).claims == []
```

- [ ] **Step 2: Add the `ToolContext` field** — in `src/call_manager/proposal_assist/tools/registry.py`, after the `eval_*` fields (~line 102), add:

```python
    # SP2b-3d: the uncited-claims pass pins the draft section count it scanned, so
    # submit_uncited_claims validates each claim's section_index against the SAME draft.
    uncited_draft_section_count: int | None = field(default=None)
```

- [ ] **Step 3: Implement the tool impl** — `src/call_manager/proposal_assist/tools/uncited_claims.py`:

```python
"""submit_uncited_claims tool impl: the uncited-claims pass's ONLY tool. Validates the
model's flagged claims against the PINNED draft section count (ctx.uncited_draft_section_count)
and writes the RAW validated list onto the running uncited_claims_jobs row. The verbatim-
substring filter, hashing, and dedupe happen in the pass runner (which holds the pinned
section texts). Mirrors the propose_*/submit_scorecard contract: returns {"error": ...} for
every expected failure, never raises."""

from __future__ import annotations

from typing import Any

from call_manager.db.models import UncitedClaimsJob
from call_manager.proposal_assist.tools.registry import ToolContext


def _is_int(x: Any) -> bool:
    # bool is an int SUBCLASS — reject True/False so they cannot pass as a section_index.
    return isinstance(x, int) and not isinstance(x, bool)


async def submit_uncited_claims_impl(
    ctx: ToolContext, *, claims: list[dict[str, Any]]
) -> dict[str, Any]:
    n = ctx.uncited_draft_section_count or 0
    out: list[dict[str, Any]] = []
    for c in claims:
        raw_text = c.get("claim_text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not text:
            return {"error": "each claim needs a non-empty claim_text"}
        si = c.get("section_index")
        if not _is_int(si) or not (0 <= si < n):
            return {"error": f"claim section_index must be an int in [0,{n}); got {si!r}"}
        raw_reason = c.get("reason")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        if not reason:
            return {"error": "each claim needs a non-empty reason"}
        out.append({"claim_text": text, "section_index": si, "reason": reason})

    # Guarded write: row-lock the target; require it is THIS workspace's still-running job
    # (rejects a stale ctx or a job the reaper/cancel already failed). Exactly-once: a second
    # call must not overwrite the first.
    with ctx.session_factory() as session:
        job = session.get(UncitedClaimsJob, ctx.origin_job_id, with_for_update=True)
        if job is None or job.workspace_id != ctx.workspace_id or job.status != "running":
            return {"error": "target is not an active uncited-claims run for this workspace"}
        if job.claims is not None:
            return {"error": "claims have already been submitted for this run"}
        job.claims = out
        session.commit()
    return {"stored": True, "count": len(out)}
```

- [ ] **Step 4: Register the tool in the registry** — in `src/call_manager/proposal_assist/tools/registry.py`, inside `_collect_tools`, beside the `submit_scorecard` import (~line 631) add `from call_manager.proposal_assist.tools.uncited_claims import submit_uncited_claims_impl`, then add this `@tool` after `submit_review` (~line 754):

```python
    @tool(
        "submit_uncited_claims",
        "Submit the COMPLETE list of uncited factual claims found in the DRAFT, exactly "
        "once. Each entry needs `claim_text` (the VERBATIM assertion text, copied from the "
        "section so it is locatable), `section_index` (the 0-based [§index] it appears in), "
        "and a one-line `reason` (why it needs a citation). Flag only factual/empirical "
        "assertions lacking an adjacent citation marker; EXCLUDE the applicant's own "
        "proposed work, common knowledge, and already-cited statements. Submit an empty "
        "list if the draft has no uncited claims. You have no other tools.",
        {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim_text": {"type": "string"},
                            "section_index": {"type": "integer"},
                            "reason": {"type": "string"},
                        },
                        "required": ["claim_text", "section_index", "reason"],
                    },
                }
            },
            "required": ["claims"],
        },
    )
    async def submit_uncited_claims(args):
        return await _safe_tool(
            "submit_uncited_claims",
            ctx,
            submit_uncited_claims_impl,
            ctx,
            claims=args.get("claims") or [],
        )
```

Then add `submit_uncited_claims` to the `return [...]` list (after `submit_review`, ~line 790):

```python
        # SP2b-3d — uncited-claims (the uncited-claims pass's ONLY tool)
        submit_uncited_claims,
```

- [ ] **Step 5: Implement the adapter helper** — `src/call_manager/proposal_assist/uncited_claims/agent.py`. Mirrors `evaluator/agent.py`:

```python
"""Build the uncited-claims pass's OWN one-shot SDK adapter — never WorkspaceController.

A dedicated adapter + MCP server + ToolContext stamped with uncited-claims provenance
(origin_kind='uncited_claims_session', origin_job_id) and the PINNED draft section count
(so submit_uncited_claims validates section_index against the same draft scanned). Its ONLY
tool is submit_uncited_claims."""

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


def uncited_claims_allowed_tools() -> list[str]:
    return ["submit_uncited_claims"]


def build_uncited_claims_adapter(
    *,
    settings: Settings,
    session_factory: sessionmaker,
    workspace_id: uuid.UUID,
    requested_by: int | None,
    job_id: uuid.UUID,
    draft_section_count: int,
) -> ClaudeAgentSDKAdapter:
    def tools_factory(repo_dir: Path, ws_id: uuid.UUID):
        ctx = ToolContext(
            workspace_id=ws_id,
            repo_dir=repo_dir,
            session_factory=session_factory,
            settings=settings,
            origin_kind="uncited_claims_session",
            origin_job_id=job_id,
            created_by=requested_by,
            uncited_draft_section_count=draft_section_count,
        )
        return build_proposal_assist_mcp_server(ctx)

    return ClaudeAgentSDKAdapter(
        model=settings.proposal_assist_llm_model,
        workspaces_dir=settings.workspaces_dir,
        tools_factory=tools_factory,
        allowed_tools=uncited_claims_allowed_tools(),
    )
```

- [ ] **Step 6: Update the wiring assertion tests** — these are green on master (`17bebde`) for 2 background tools; add the 3rd.

In `tests/proposal_assist/test_tools_registry.py::test_collect_tools_returns_expected_named_tools`, add `"submit_uncited_claims"` to the asserted set (after `"submit_review"`) and bump the count `assert len(tools) == 23` → `24`. Update the docstring count note `21 + 2 = 23` → `21 + 3 = 24`.

In `tests/proposal_assist/test_tools_claude_adapter_wiring.py::test_every_collected_tool_is_allowlisted`, add the import and extend the `background_only` union:

```python
    from call_manager.proposal_assist.uncited_claims.agent import uncited_claims_allowed_tools
```
```python
    background_only = (
        set(evaluator_allowed_tools())
        | set(review_allowed_tools())
        | set(uncited_claims_allowed_tools())
    )
```
(The `assert len(allowed) == 21` stays — the interactive default allowlist is unchanged; only the collected set grows.)

- [ ] **Step 7: Run the tool test + the two wiring tests**

Run: `uv run pytest tests/proposal_assist/uncited_claims/test_submit.py tests/proposal_assist/test_tools_registry.py tests/proposal_assist/test_tools_claude_adapter_wiring.py -p no:cacheprovider -q`
Expected: all pass (7 submit + the full registry/wiring files green at 24 collected tools).

- [ ] **Step 8: Lint + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/tools/uncited_claims.py src/call_manager/proposal_assist/uncited_claims/agent.py src/call_manager/proposal_assist/tools/registry.py tests/proposal_assist/uncited_claims/test_submit.py
uv run ruff format src/call_manager/proposal_assist/tools/uncited_claims.py src/call_manager/proposal_assist/uncited_claims/agent.py
git -C .worktrees/sp2b-3d-uncited-claims add -A
git -C .worktrees/sp2b-3d-uncited-claims -c commit.gpgsign=false commit -m "feat(uncited-claims): submit tool + adapter helper + registry + wiring tests

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `context.py` + `pass_runner.py` — assemble, run, filter, hash, dedupe

**Files:**
- Create: `src/call_manager/proposal_assist/uncited_claims/context.py`, `src/call_manager/proposal_assist/uncited_claims/pass_runner.py`
- Test: `tests/proposal_assist/uncited_claims/test_context.py`, `tests/proposal_assist/uncited_claims/test_pass_runner.py`

**Interfaces:**
- Consumes: `UncitedClaimsJob`, `Workspace`, `build_uncited_claims_adapter`, `claim_next_uncited_claims_job`, `uncited_claims_cap_exceeded`, `split_sections`, `workspace_repo_dir`, `PROPOSAL_FILENAME`, `LaunchContext`, `TurnDoneEvent`.
- Produces:
  - `assemble_uncited_claims_context(*, session, workspace_id, workspaces_dir) -> UncitedClaimsContext` with fields `instruction: str`, `draft_section_count: int`, `section_texts: dict[int, str]`, `draft_sha: str`.
  - `run_uncited_claims_pass(*, job_id, session_factory, settings, adapter_builder=build_uncited_claims_adapter) -> None`
  - `drain_uncited_claims_queue_tick(*, session_factory, settings, adapter_builder=build_uncited_claims_adapter) -> None`
- Consumes: the six `Settings.uncited_claims_*` config fields — **already landed in Task 2** (the config block + `test_config.py` were pulled forward because Task 2's `test_cap_blocks_over_budget` reads `uncited_claims_daily_token_cap`). This task just reads `uncited_claims_pass_timeout_seconds` / `uncited_claims_terminate_timeout_seconds` from `Settings()`.

- [ ] **Step 0: Config already present (from Task 2) — do NOT re-add.** The six `uncited_claims_*` fields live in `src/call_manager/config.py` (added after `evaluator_daily_token_cap`) and `tests/proposal_assist/uncited_claims/test_config.py` exists. Confirm `Settings().uncited_claims_pass_timeout_seconds == 180` resolves, then proceed. (If for any reason the fields are missing, the canonical block is in Task 2's config commit `6e68f18`.)

- [ ] **Step 1: Write the context test** — `tests/proposal_assist/uncited_claims/test_context.py`:

```python
from pathlib import Path

from call_manager.proposal_assist.uncited_claims.context import assemble_uncited_claims_context
from tests.proposal_assist.conftest import make_workspace


def _seed(tmp_path, ws_id, body):
    repo = Path(tmp_path) / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(body)


def test_assembles_sections_and_sha(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    _seed(tmp_path, ws_id, "## Aims\n\nWe claim 30% relapse.\n\n## Methods\n\nMore.\n")
    with pg_session_factory() as s:
        ctx = assemble_uncited_claims_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )
    assert ctx.draft_section_count == 3  # preamble(0) + 2 headed sections
    assert "30% relapse" in ctx.section_texts[1]
    assert ctx.draft_sha  # non-empty content sha
    assert "DRAFT" in ctx.instruction


def test_empty_draft_yields_zero_sections(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.commit()
        ws_id = ws.id
    _seed(tmp_path, ws_id, "")
    with pg_session_factory() as s:
        ctx = assemble_uncited_claims_context(
            session=s, workspace_id=ws_id, workspaces_dir=str(tmp_path)
        )
    assert ctx.draft_section_count == 0 and ctx.section_texts == {}
```

- [ ] **Step 2: Implement `context.py`**:

```python
"""Assemble the uncited-claims instruction (the indexed DRAFT with its existing citation
markers) and capture the per-section texts (for the runner's substring filter) + the draft
content sha (for staleness). Pure (DB read + one file read)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir

_ROLE = (
    "You are auditing a grant proposal DRAFT for UNCITED factual claims. The draft below is "
    "shown section-by-section as [§index], INCLUDING its existing inline citation markers. "
    "Flag every factual/empirical assertion — statistics, prior-work claims, mechanism or "
    "efficacy claims, comparative claims — that LACKS an adjacent citation marker. EXCLUDE: "
    "the applicant's own proposed/novel work and aims; common knowledge and definitions; and "
    "anything already adjacent to a citation marker. For each flagged claim return the "
    "VERBATIM claim text (copied from its section so it is locatable), its section_index, and "
    "a one-line reason. Call submit_uncited_claims EXACTLY ONCE with the complete list (an "
    "empty list if there are none). You have no other tools.\n"
)


@dataclass
class UncitedClaimsContext:
    instruction: str
    draft_section_count: int
    # index -> exact section text the model was shown; the runner keeps a claim only if its
    # claim_text is a substring of its referenced section here (drops hallucinations).
    section_texts: dict[int, str]
    # sha256 of the EXACT draft bytes assembled (read once — no TOCTOU), so the job's
    # evaluated_draft_sha matches what the model scanned.
    draft_sha: str


def _read_draft(repo_dir: Path) -> tuple[str, str]:
    """Read proposal.md bytes EXACTLY ONCE; return (decoded_text, content_sha). Missing ->
    ('', '') so '' marks 'no draft' on both sides of staleness."""
    path = repo_dir / PROPOSAL_FILENAME
    if not path.exists():
        return "", ""
    raw = path.read_bytes()
    return raw.decode(), hashlib.sha256(raw).hexdigest()


def _render_sections(sections: list) -> str:
    return "\n\n".join(f"[§{s.index}]\n{s.text}" for s in sections)


def assemble_uncited_claims_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> UncitedClaimsContext:
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft_text, draft_sha = _read_draft(repo_dir)
    sections = split_sections(draft_text) if draft_text.strip() else []
    section_texts = {s.index: s.text for s in sections}
    draft_block = _render_sections(sections) if sections else "(draft is empty)"
    instruction = f"{_ROLE}\n# DRAFT (proposal.md)\n{draft_block}\n"
    return UncitedClaimsContext(
        instruction=instruction,
        draft_section_count=len(sections),
        section_texts=section_texts,
        draft_sha=draft_sha,
    )
```

> Note: `assemble_uncited_claims_context` takes `session` for signature-parity with the evaluator (and future use); it is currently unused in the body. Keep the parameter — the pass runner calls it inside the short read session. If ruff flags the unused arg, prefix-rename is NOT allowed (the runner passes it by keyword); instead leave it — `session` is a public keyword arg, not a local, so ruff's `ARG` rule does not fire on a public function signature here. (The evaluator's context uses `session`; ours doesn't need it yet but keeps the shape.)

- [ ] **Step 3: Run the context test**

Run: `uv run pytest tests/proposal_assist/uncited_claims/test_context.py -p no:cacheprovider -q`
Expected: `2 passed`.

- [ ] **Step 4: Write the pass_runner test** — `tests/proposal_assist/uncited_claims/test_pass_runner.py`. Mirrors `evaluator/test_pass_runner.py`; adds the substring-filter + hashing assertions:

```python
import asyncio

import pytest

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob
from call_manager.proposal_assist.drafting.contract import TurnDoneEvent
from call_manager.proposal_assist.drafting.fake_adapter import FakeAgentAdapter
from call_manager.proposal_assist.uncited_claims.pass_runner import run_uncited_claims_pass
from tests.proposal_assist.conftest import make_workspace


def _seed_repo(workspaces_dir, ws_id):
    repo = workspaces_dir / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    # section 1 contains the verbatim claim text; section 0 is the (empty) preamble.
    (repo / "proposal.md").write_text("## Aims\n\nWe claim 30% relapse occurs.\n")


def _running_job(s, ws_id):
    job = UncitedClaimsJob(workspace_id=ws_id, status="running")
    s.add(job)
    s.flush()
    return job


@pytest.mark.asyncio
async def test_success_filters_hashes_and_dedupes(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _Adapter(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            with pg_session_factory() as s:
                j = s.get(UncitedClaimsJob, job_id)
                # one real (substring of §1), one duplicate of it, one hallucinated (not present)
                j.claims = [
                    {"claim_text": "30% relapse", "section_index": 1, "reason": "stat"},
                    {"claim_text": "30% relapse", "section_index": 1, "reason": "stat dup"},
                    {"claim_text": "made up fact", "section_index": 1, "reason": "halluc"},
                ]
                s.commit()
            yield TurnDoneEvent(tokens_in=10, tokens_out=5, model="m", session_id="s")

    await run_uncited_claims_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: _Adapter(),
    )
    with pg_session_factory() as s:
        done = s.get(UncitedClaimsJob, job_id)
        assert done.status == "completed"
        assert len(done.claims) == 1  # dup deduped, hallucination dropped
        assert done.claims[0]["claim_text"] == "30% relapse"
        assert done.claims[0]["claim_hash"]  # hash computed server-side
        assert done.input_tokens == 10 and done.output_tokens == 5
        assert done.evaluated_draft_sha  # staleness marker captured


@pytest.mark.asyncio
async def test_empty_submission_completes_with_no_rows(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _Adapter(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            with pg_session_factory() as s:
                s.get(UncitedClaimsJob, job_id).claims = []
                s.commit()
            yield TurnDoneEvent(tokens_in=1, tokens_out=1, model="m", session_id="s")

    await run_uncited_claims_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: _Adapter(),
    )
    with pg_session_factory() as s:
        done = s.get(UncitedClaimsJob, job_id)
        assert done.status == "completed" and done.claims == []


@pytest.mark.asyncio
async def test_no_submission_fails(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()
    fake.script_next_turn([TurnDoneEvent(tokens_in=1, tokens_out=1, model="m", session_id="s")])
    await run_uncited_claims_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: fake,
    )
    with pg_session_factory() as s:
        job = s.get(UncitedClaimsJob, job_id)
        assert job.status == "failed" and "no result" in (job.error or "")


@pytest.mark.asyncio
async def test_late_finalize_records_tokens_without_resurrecting(
    tmp_path, pg_session_factory, _settings_env
):
    # spec §3: if the reaper/cancel failed the job between the turn and finalize, DON'T
    # resurrect it — but still record the tokens it spent so the daily cap stays honest.
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _Adapter(FakeAgentAdapter):
        async def send_turn(self, handle, user_message):
            # simulate the reaper/cancel finalizing the job DURING the turn, after spend
            with pg_session_factory() as s:
                j = s.get(UncitedClaimsJob, job_id)
                j.claims = [{"claim_text": "30% relapse", "section_index": 1, "reason": "r"}]
                j.status = "failed"
                j.error = "timed out"
                s.commit()
            yield TurnDoneEvent(tokens_in=42, tokens_out=7, model="m", session_id="s")

    await run_uncited_claims_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: _Adapter(),
    )
    with pg_session_factory() as s:
        job = s.get(UncitedClaimsJob, job_id)
        assert job.status == "failed"  # not resurrected to completed
        assert job.input_tokens == 42 and job.output_tokens == 7  # late spend still counted


@pytest.mark.asyncio
async def test_timeout_on_launch_fails(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    settings.uncited_claims_pass_timeout_seconds = 0.1
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    class _HangLaunch(FakeAgentAdapter):
        async def launch(self, context):
            await asyncio.sleep(2)
            return await super().launch(context)

    await run_uncited_claims_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=lambda **kw: _HangLaunch(),
    )
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).status == "failed"


@pytest.mark.asyncio
async def test_cancelled_before_launch_skips_the_llm(tmp_path, pg_session_factory, _settings_env):
    settings = Settings()
    settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        job = _running_job(s, ws.id)
        s.commit()
        ws_id, job_id = ws.id, job.id
    _seed_repo(tmp_path, ws_id)

    fake = FakeAgentAdapter()

    def builder(**kw):
        with pg_session_factory() as s:
            j = s.get(UncitedClaimsJob, job_id)
            j.status = "failed"
            j.error = "workspace deleted"
            s.commit()
        return fake

    await run_uncited_claims_pass(
        job_id=job_id, session_factory=pg_session_factory, settings=settings,
        adapter_builder=builder,
    )
    assert fake.launches == []  # pre-launch recheck short-circuited — no LLM spend
    with pg_session_factory() as s:
        assert s.get(UncitedClaimsJob, job_id).status == "failed"
```

- [ ] **Step 5: Implement `pass_runner.py`** — mirrors `evaluator/pass_runner.py`; the finalize step applies the substring filter + hash + dedupe over the raw `job.claims`:

```python
"""Run one uncited-claims pass for a claimed (running) uncited_claims_jobs row, and the
async drain tick. Fresh-stateless: a dedicated one-shot adapter, no resume, terminated in
finally. The submit_uncited_claims tool writes the RAW list; this runner applies the
verbatim-substring filter + hashing + dedupe at finalize (it holds the pinned section texts).
Carries the evaluator hardening: launch+drive bounded together, bounded terminate, FOR UPDATE
finalize guard. The separate sync reaper handles crash recovery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.db.models import UncitedClaimsJob
from call_manager.proposal_assist.drafting.contract import LaunchContext, TurnDoneEvent
from call_manager.proposal_assist.uncited_claims.agent import build_uncited_claims_adapter
from call_manager.proposal_assist.uncited_claims.context import assemble_uncited_claims_context
from call_manager.proposal_assist.uncited_claims.jobs import (
    claim_next_uncited_claims_job,
    uncited_claims_cap_exceeded,
)

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


def _fail(session, job: UncitedClaimsJob, message: str) -> None:
    job.status = "failed"
    job.error = message
    job.completed_at = datetime.now(UTC)
    session.flush()


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def _finalize_claims(raw: list[dict], section_texts: dict[int, str]) -> list[dict]:
    """Keep a claim only if its claim_text is a VERBATIM substring of its referenced pinned
    section (drops hallucinated/paraphrased text); compute claim_hash server-side; dedupe by
    hash. The model is never trusted to supply the hash."""
    out: list[dict] = []
    seen: set[str] = set()
    for c in raw or []:
        text = c.get("claim_text") or ""
        si = c.get("section_index")
        section = section_texts.get(si, "")
        if not text or text not in section:
            continue
        h = hashlib.sha256(_normalize(text).encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        out.append(
            {
                "claim_text": text,
                "section_index": si,
                "reason": c.get("reason") or "",
                "claim_hash": h,
            }
        )
    return out


async def run_uncited_claims_pass(
    *,
    job_id: uuid.UUID,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_uncited_claims_adapter,
) -> None:
    # 1. Short read session: assemble context (pinned draft), capture staleness marker.
    with session_factory() as s:
        job = s.get(UncitedClaimsJob, job_id)
        if job is None or job.status != "running":
            return
        workspace_id = job.workspace_id
        requested_by = job.requested_by
        if uncited_claims_cap_exceeded(s, workspace_id=workspace_id, settings=settings):
            _fail(s, job, "daily uncited-claims budget reached")
            s.commit()
            return
        ctx = assemble_uncited_claims_context(
            session=s, workspace_id=workspace_id, workspaces_dir=settings.workspaces_dir
        )
        job.evaluated_draft_sha = ctx.draft_sha
        instruction = ctx.instruction
        draft_n = ctx.draft_section_count
        section_texts = ctx.section_texts
        s.commit()

    adapter = adapter_builder(
        settings=settings,
        session_factory=session_factory,
        workspace_id=workspace_id,
        requested_by=requested_by,
        job_id=job_id,
        draft_section_count=draft_n,
    )
    handle = None
    done: TurnDoneEvent | None = None
    try:

        async def _run() -> TurnDoneEvent | None:
            nonlocal handle
            # Pre-launch live-status recheck: a soft-delete/cancel may have flipped the job to
            # 'failed' between the read session and here. Don't spend tokens on a dead job.
            with session_factory() as s2:
                live = s2.get(UncitedClaimsJob, job_id)
                if live is None or live.status != "running":
                    return None
            handle = await adapter.launch(LaunchContext(workspace_id=workspace_id))
            last = None
            async for event in adapter.send_turn(handle, instruction):
                if isinstance(event, TurnDoneEvent):
                    last = event
            return last

        done = await asyncio.wait_for(
            _run(), timeout=settings.uncited_claims_pass_timeout_seconds
        )
    except Exception as exc:  # noqa: BLE001 — terminal: fail the job, never propagate
        logger.exception("uncited-claims pass failed for job %s", job_id)
        message = (
            "Uncited-claims check timed out."
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            else "Uncited-claims check failed."
        )
        with session_factory() as s:
            job = s.get(UncitedClaimsJob, job_id, with_for_update=True)
            if job is not None and job.status == "running":
                _fail(s, job, message)
            s.commit()
        return
    finally:
        if handle is not None:
            try:
                await asyncio.wait_for(
                    adapter.terminate(handle),
                    timeout=settings.uncited_claims_terminate_timeout_seconds,
                )
            except Exception:  # noqa: BLE001
                logger.exception("uncited-claims adapter terminate failed for job %s", job_id)

    # 2. Finalize under a row lock. ALWAYS record token/session accounting when a
    #    TurnDoneEvent arrived — even if the reaper/cancel already failed the job
    #    mid-pass — so late spend still counts toward the daily cap (spec §3: "don't
    #    resurrect it; still roll up tokens", mirroring review/pass_runner.py). Only
    #    status/claims/completed_at are gated on the job still running.
    with session_factory() as s:
        job = s.get(UncitedClaimsJob, job_id, with_for_update=True)
        if job is None:
            s.commit()
            return
        if done is not None:
            job.input_tokens = done.tokens_in or 0
            job.output_tokens = done.tokens_out or 0
            job.model = done.model
            job.sdk_session_id = done.session_id
        if job.status != "running":
            # The reaper (TTL) or a soft-delete cancel finalized the job mid-pass. Don't
            # resurrect it (no status/claims change) — but keep the tokens recorded above.
            s.commit()
            return
        if job.claims is None:
            _fail(s, job, "uncited-claims pass produced no result")
            s.commit()
            return
        job.claims = _finalize_claims(job.claims, section_texts)
        job.status = "completed"
        job.completed_at = datetime.now(UTC)
        s.commit()


async def drain_uncited_claims_queue_tick(
    *,
    session_factory: sessionmaker,
    settings: Settings,
    adapter_builder=build_uncited_claims_adapter,
) -> None:
    """Claim one job and run it. The reaper is a separate sync tick."""
    with session_factory() as s:
        job = claim_next_uncited_claims_job(s)
        s.commit()
        job_id = job.id if job is not None else None
    if job_id is None:
        return
    await run_uncited_claims_pass(
        job_id=job_id,
        session_factory=session_factory,
        settings=settings,
        adapter_builder=adapter_builder,
    )
```

> The `section_texts` JSONB keys come back as Python `int`s (the tool validated `section_index` as an int and SQLAlchemy round-trips JSON numbers to int), so `section_texts.get(si, "")` matches. Note `section_texts` is built in the read session and held as a local across the `await` — it is plain data (a dict), not a DB object, so it is safe to use after the session closes.

- [ ] **Step 6: Run the pass_runner test** (the six `uncited_claims_*` config fields were added in Step 0, so this task is self-contained):

Run: `uv run pytest tests/proposal_assist/uncited_claims/test_pass_runner.py -p no:cacheprovider -q`
Expected: `6 passed`.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/call_manager/config.py src/call_manager/proposal_assist/uncited_claims/context.py src/call_manager/proposal_assist/uncited_claims/pass_runner.py tests/proposal_assist/uncited_claims/test_config.py tests/proposal_assist/uncited_claims/test_context.py tests/proposal_assist/uncited_claims/test_pass_runner.py
uv run ruff format src/call_manager/proposal_assist/uncited_claims/context.py src/call_manager/proposal_assist/uncited_claims/pass_runner.py
git -C .worktrees/sp2b-3d-uncited-claims add -A
git -C .worktrees/sp2b-3d-uncited-claims -c commit.gpgsign=false commit -m "feat(uncited-claims): context + pass_runner (substring filter + hash/dedupe)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Problems builder + aggregate wiring (the 5th signal)

**Files:**
- Modify: `src/call_manager/proposal_assist/problems/builders.py`, `src/call_manager/proposal_assist/problems/aggregate.py`
- Test: `tests/proposal_assist/problems/test_uncited_claim_builder.py`, and extend `tests/proposal_assist/problems/test_aggregate.py` (if present) or add `tests/proposal_assist/problems/test_aggregate_uncited.py`

**Interfaces:**
- Consumes: `latest_completed_uncited_claims_job`, `dismissed_claim_hashes`, `content_sha`, `PROPOSAL_FILENAME`.
- Produces:
  - `uncited_claim_problems(claims: list[dict], dismissed: set[str], *, cap: int = 10) -> list[dict]` (rows with `category="uncited_claim"`, `severity="warning"`, plus a `claim_hash` field on real rows; a `"+ N more"` tail row with no `claim_hash`/target).
  - `_row(...)` gains an optional `claim_hash` kwarg (additive — included in the dict only when provided).
  - `assemble_problems(...)` return dict gains `uncited_claims_stale: bool`.

> **Decision (carry into Codex review): `/problems` stays a bare-dict endpoint.** It has no `response_model` today and the frontend `problems.ts` uses hand-written types. SP2b-3d adds the two fields to the bare dict (via `aggregate.py`) + the hand-written frontend types (Task 7). The spec §10 phrase "update its response_model" is a no-op here (none exists); adding a full Pydantic model for `/problems` is out of scope. The NEW uncited-claims endpoints DO get `response_model`s (Task 6) per §6 + the SP2b-3c lesson. `web/problems.py` itself is therefore unchanged.

- [ ] **Step 1: Write the builder test** — `tests/proposal_assist/problems/test_uncited_claim_builder.py`:

```python
from call_manager.proposal_assist.problems.builders import uncited_claim_problems


def _claim(h, text="some claim", si=1, reason="needs cite"):
    return {"claim_text": text, "section_index": si, "reason": reason, "claim_hash": h}


def test_emits_warning_rows_with_hash_and_target():
    rows = uncited_claim_problems([_claim("h1")], set())
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "uncited_claim" and r["severity"] == "warning"
    assert r["claim_hash"] == "h1"
    assert r["target"] == {"top_tab": "draft", "bottom_tab": "problems", "section_index": 1}


def test_dismissed_are_filtered_out():
    rows = uncited_claim_problems([_claim("h1"), _claim("h2")], {"h1"})
    assert [r["claim_hash"] for r in rows] == ["h2"]


def test_caps_and_appends_rollup_tail():
    claims = [_claim(f"h{i}") for i in range(13)]
    rows = uncited_claim_problems(claims, set(), cap=10)
    assert len(rows) == 11  # 10 capped rows + 1 roll-up
    tail = rows[-1]
    assert tail["title"] == "+ 3 more uncited claims"
    assert "claim_hash" not in tail and tail["target"] is None


def test_truncates_long_title():
    long = "x" * 200
    rows = uncited_claim_problems([_claim("h1", text=long)], set())
    assert len(rows[0]["title"]) <= 80
```

- [ ] **Step 2: Extend `_row` + implement the builder** — in `src/call_manager/proposal_assist/problems/builders.py`:

Change the `_row` signature + body to carry an optional `claim_hash` (additive):

```python
def _row(
    category: str,
    severity: str,
    title: str,
    detail: str,
    *,
    top_tab: str | None = None,
    bottom_tab: str | None = None,
    section_index: int | None = None,
    claim_hash: str | None = None,
) -> dict:
    si = (
        section_index
        if isinstance(section_index, int) and not isinstance(section_index, bool)
        else None
    )
    target = None
    if top_tab is not None or bottom_tab is not None or si is not None:
        target = {"top_tab": top_tab, "bottom_tab": bottom_tab, "section_index": si}
    row = {
        "category": category,
        "severity": severity,
        "title": "" if title is None else str(title),
        "detail": "" if detail is None else str(detail),
        "target": target,
    }
    # Additive: only uncited_claim rows carry a claim_hash (so the panel can dismiss them).
    # Existing rows omit the key entirely.
    if claim_hash is not None:
        row["claim_hash"] = claim_hash
    return row
```

Add the category to the rank map (so ordering is stable):

```python
_CATEGORY_RANK = {
    "broken_citation": 0,
    "criterion_gap": 1,
    "over_length": 2,
    "coherence_drift": 3,
    "uncited_claim": 4,
}
```

Add the builder + a title-truncation helper (place near `criterion_gap_problems`):

```python
def _truncate(text: str, n: int = 80) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def uncited_claim_problems(
    claims: list[dict], dismissed: set[str], *, cap: int = 10
) -> list[dict]:
    """Up to `cap` warning rows for the latest completed uncited-claims result, filtering
    out dismissed claim_hashes; if more remain, one '+ N more' roll-up tail row. Pure — no
    `stale` parameter (staleness is the separate top-level uncited_claims_stale flag)."""
    kept = [c for c in (claims or []) if c.get("claim_hash") not in dismissed]
    rows: list[dict] = []
    for c in kept[:cap]:
        rows.append(
            _row(
                "uncited_claim",
                "warning",
                _truncate(c.get("claim_text") or ""),
                c.get("reason") or "",
                top_tab="draft",
                bottom_tab="problems",
                section_index=c.get("section_index"),
                claim_hash=c.get("claim_hash"),
            )
        )
    extra = len(kept) - cap
    if extra > 0:
        rows.append(
            _row(
                "uncited_claim",
                "warning",
                f"+ {extra} more uncited claims",
                "",
                bottom_tab="problems",
            )
        )
    return rows
```

- [ ] **Step 3: Run the builder test**

Run: `uv run pytest tests/proposal_assist/problems/test_uncited_claim_builder.py -p no:cacheprovider -q`
Expected: `4 passed`.

- [ ] **Step 4: Wire the aggregate** — in `src/call_manager/proposal_assist/problems/aggregate.py`:

Add to the imports from `...uncited_claims.jobs` and `...problems.builders`:

```python
from call_manager.proposal_assist.uncited_claims.jobs import (
    dismissed_claim_hashes,
    latest_completed_uncited_claims_job,
)
```
and add `uncited_claim_problems` to the existing `from call_manager.proposal_assist.problems.builders import (...)` block.

After signal block 4 (coherence drift) and before the `counts = ...` line, add the 5th signal + the stale flag:

```python
    # 5. uncited claims (LLM) — latest completed pass; dismissed hashes filtered out.
    uncited_claims_stale = False
    try:
        uc_job = latest_completed_uncited_claims_job(session, workspace_id=workspace_id)
        if uc_job is not None:
            dismissed = dismissed_claim_hashes(session, workspace_id=workspace_id)
            uncited_claims_stale = uc_job.evaluated_draft_sha != content_sha(
                repo_dir / PROPOSAL_FILENAME
            )
            rows += uncited_claim_problems(uc_job.claims or [], dismissed)
    except Exception:
        logger.exception("problems: uncited_claim signal failed")
```

And add the field to the return dict:

```python
    return {
        "problems": ordered,
        "counts": counts,
        "scorecard_stale": scorecard_stale,
        "uncited_claims_stale": uncited_claims_stale,
    }
```

- [ ] **Step 5: Write the aggregate test** — `tests/proposal_assist/problems/test_aggregate_uncited.py`:

```python
from pathlib import Path

from call_manager.db.models import UncitedClaimsJob, WorkspaceEvent
from call_manager.proposal_assist.problems.aggregate import assemble_problems
from tests.proposal_assist.conftest import make_workspace


def _repo(tmp_path, ws_id, body="## Aims\n\nWe claim 30% relapse occurs.\n"):
    repo = Path(tmp_path) / str(ws_id) / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proposal.md").write_text(body)
    return repo


def test_fifth_signal_composes_and_stale_is_independent(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        # completed pass whose evaluated_draft_sha will NOT match the current draft -> stale
        s.add(UncitedClaimsJob(
            workspace_id=ws.id, status="completed", evaluated_draft_sha="stale-sha",
            claims=[{"claim_text": "30% relapse", "section_index": 1, "reason": "stat",
                     "claim_hash": "h1"}],
        ))
        s.commit()
        ws_id, rubric_ts = ws.id, ws.rubric_updated_at
    repo = _repo(tmp_path, ws_id)
    with pg_session_factory() as s:
        out = assemble_problems(
            session=s, workspace_id=ws_id, repo_dir=repo,
            ws_rubric_updated_at=rubric_ts, budget=0,
        )
    uncited = [r for r in out["problems"] if r["category"] == "uncited_claim"]
    assert len(uncited) == 1 and uncited[0]["claim_hash"] == "h1"
    assert out["uncited_claims_stale"] is True
    assert out["scorecard_stale"] == []  # independent of the uncited flag


def test_dismissed_claim_not_shown(tmp_path, pg_session_factory):
    with pg_session_factory() as s:
        ws = make_workspace(s)
        s.flush()
        s.add(UncitedClaimsJob(
            workspace_id=ws.id, status="completed", evaluated_draft_sha="x",
            claims=[{"claim_text": "30% relapse", "section_index": 1, "reason": "r",
                     "claim_hash": "h1"}],
        ))
        s.add(WorkspaceEvent(workspace_id=ws.id, kind="uncited_claim_dismissed",
                             payload={"claim_hash": "h1"}))
        s.commit()
        ws_id, rubric_ts = ws.id, ws.rubric_updated_at
    repo = _repo(tmp_path, ws_id)
    with pg_session_factory() as s:
        out = assemble_problems(
            session=s, workspace_id=ws_id, repo_dir=repo,
            ws_rubric_updated_at=rubric_ts, budget=0,
        )
    assert [r for r in out["problems"] if r["category"] == "uncited_claim"] == []
```

- [ ] **Step 6: Run the builder + aggregate tests**

Run: `uv run pytest tests/proposal_assist/problems/test_uncited_claim_builder.py tests/proposal_assist/problems/test_aggregate_uncited.py -p no:cacheprovider -q`
Expected: all pass.

- [ ] **Step 7: Lint + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/problems/builders.py src/call_manager/proposal_assist/problems/aggregate.py tests/proposal_assist/problems/test_uncited_claim_builder.py tests/proposal_assist/problems/test_aggregate_uncited.py
uv run ruff format src/call_manager/proposal_assist/problems/builders.py src/call_manager/proposal_assist/problems/aggregate.py
git -C .worktrees/sp2b-3d-uncited-claims add -A
git -C .worktrees/sp2b-3d-uncited-claims -c commit.gpgsign=false commit -m "feat(uncited-claims): 5th Problems signal (builder + aggregate + stale flag)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Endpoints + soft-delete cancel + config + scheduler

**Files:**
- Create: `src/call_manager/proposal_assist/web/uncited_claims.py`
- Modify: `src/call_manager/config.py`, `src/call_manager/scheduler.py`, `src/call_manager/main.py`, `src/call_manager/proposal_assist/web/workspaces.py`
- Test: `tests/proposal_assist/uncited_claims/test_web.py`, `tests/proposal_assist/uncited_claims/test_soft_delete.py`, `tests/proposal_assist/uncited_claims/test_scheduler_wiring.py` (config + `test_config.py` landed in Task 4 Step 0)

**Interfaces:**
- Produces: `POST /api/workspaces/{id}/uncited-claims-runs` (response_model `UncitedClaimsRunOut`), `GET .../uncited-claims-runs/latest` (response_model `UncitedClaimsLatestOut | None`), `POST .../uncited-claims/{claim_hash}/dismiss` (response_model `OkOut`); six `Settings.uncited_claims_*` fields; scheduler jobs `uncited_claims_drain` + `uncited_claims_reaper`.

- [ ] **Steps A–B (config): already done in Task 4 Step 0.** The six `Settings.uncited_claims_*` fields + `test_config.py` landed there so Task 4 was self-contained. Nothing to add here — just confirm `Settings().uncited_claims_tick_seconds == 20` resolves (it does if Task 4 Step 0 was applied).

- [ ] **Step C: Write the endpoint test** — `tests/proposal_assist/uncited_claims/test_web.py`. Mirrors `evaluator/test_web_evaluator.py`:

```python
import uuid

from call_manager.db.models import UncitedClaimsJob, WorkspaceEvent
from tests.proposal_assist.conftest import make_workspace


def _ws(_app, current_user, pg_session_factory, tmp_path, *, phase="active", ack=True):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user, ack_disclosure=ack, current_phase=phase)
        s.commit()
        return str(ws.id)


def test_post_setup_409(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path, phase="setup")
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")
    assert r.status_code == 409


def test_post_missing_ack_403(client_authed_owner, _app, current_user, pg_session_factory, tmp_path):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path, ack=False)
    r = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")
    assert r.status_code == 403


def test_post_create_or_return_active(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    a = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")
    b = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_trigger_event_written_once_on_new_run_only(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")  # creates a run
    client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims-runs")  # returns active
    with pg_session_factory() as s:
        events = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=uuid.UUID(ws_id), kind="uncited_claims_run_triggered")
            .all()
        )
        assert len(events) == 1  # only the first POST (new run) wrote the trigger event
        assert events[0].payload["job_id"]


def test_latest_null_then_running_claims_null(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    assert client_authed_owner.get(
        f"/api/workspaces/{ws_id}/uncited-claims-runs/latest"
    ).json() is None
    # seed a RUNNING job whose claims hold the RAW (unhashed) tool output
    with pg_session_factory() as s:
        s.add(UncitedClaimsJob(
            workspace_id=uuid.UUID(ws_id), status="running",
            claims=[{"claim_text": "a", "section_index": 0, "reason": "r"}],
        ))
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/uncited-claims-runs/latest").json()
    assert body["status"] == "running"
    assert body["claims"] is None  # two-shape guard: raw claims never exposed
    assert body["stale"] is False


def test_latest_completed_returns_claims(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    with pg_session_factory() as s:
        s.add(UncitedClaimsJob(
            workspace_id=uuid.UUID(ws_id), status="completed", evaluated_draft_sha="",
            claims=[{"claim_text": "a", "section_index": 0, "reason": "r", "claim_hash": "h"}],
        ))
        s.commit()
    body = client_authed_owner.get(f"/api/workspaces/{ws_id}/uncited-claims-runs/latest").json()
    assert body["status"] == "completed"
    assert body["claims"][0]["claim_hash"] == "h"
    assert body["stale"] is False  # evaluated "" == current "" (no draft on disk)


def test_dismiss_writes_event_and_is_idempotent(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r1 = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims/abc123/dismiss")
    r2 = client_authed_owner.post(f"/api/workspaces/{ws_id}/uncited-claims/abc123/dismiss")
    assert r1.status_code == 200 and r1.json() == {"ok": True}
    assert r2.status_code == 200
    with pg_session_factory() as s:
        events = (
            s.query(WorkspaceEvent)
            .filter_by(workspace_id=uuid.UUID(ws_id), kind="uncited_claim_dismissed")
            .all()
        )
        assert len(events) == 2  # idempotent at the UX level; two dismiss events is harmless
        assert events[0].payload["claim_hash"] == "abc123"


def test_non_member_rejected(
    client_authed_owner, client_unauth, _app, current_user, pg_session_factory, tmp_path
):
    ws_id = _ws(_app, current_user, pg_session_factory, tmp_path)
    r = client_unauth.get(f"/api/workspaces/{ws_id}/uncited-claims-runs/latest")
    assert r.status_code in (401, 403)
```

- [ ] **Step D: Implement the endpoints** — `src/call_manager/proposal_assist/web/uncited_claims.py`:

```python
"""Member-gated manual uncited-claims endpoints. POST enqueues (create-or-return-active,
429 on cap); GET latest is polled by the SPA (claims returned ONLY when completed — the raw
in-flight shape is never exposed); POST dismiss records a uncited_claim_dismissed event.
All carry response_models (SP2b-3c lesson: bare-dict endpoints hide client/server drift)."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import UncitedClaimsJob, Workspace, WorkspaceEvent
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.evaluator.context import content_sha
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.uncited_claims.jobs import (
    enqueue_uncited_claims_run,
    latest_uncited_claims_job,
    uncited_claims_cap_exceeded,
)
from call_manager.proposal_assist.web._cloud_gate import gate_cloud_llm
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/api/workspaces", tags=["uncited-claims"])

_RunStatus = Literal["queued", "running", "completed", "failed"]


class UncitedClaimsRunOut(BaseModel):
    id: str
    status: _RunStatus
    error: str | None
    started_at: str | None
    completed_at: str | None


class ClaimOut(BaseModel):
    claim_text: str
    section_index: int
    reason: str
    claim_hash: str


class UncitedClaimsLatestOut(UncitedClaimsRunOut):
    stale: bool
    # Present ONLY when status == 'completed' (the finalized, hashed, filtered shape);
    # null otherwise so the in-flight raw shape (no claim_hash) is never serialized.
    claims: list[ClaimOut] | None


class OkOut(BaseModel):
    ok: bool


def _serialize_run(job: UncitedClaimsJob) -> dict:
    return {
        "id": str(job.id),
        "status": job.status,
        "error": job.error,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _active_job(session: Session, workspace_id: uuid.UUID) -> UncitedClaimsJob | None:
    return session.scalar(
        select(UncitedClaimsJob).where(
            UncitedClaimsJob.workspace_id == workspace_id,
            UncitedClaimsJob.status.in_(("queued", "running")),
        )
    )


@router.post("/{workspace_id}/uncited-claims-runs", response_model=UncitedClaimsRunOut)
def start_uncited_claims_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    ws: Workspace = require_member(workspace_id=workspace_id, user=user, session=session)
    gate_cloud_llm(
        ws=ws, request=request, session=session, user=user,
        disclosure_action="checking for uncited claims",
    )
    # active-first: return the in-flight job if any; otherwise cap-gate the NEW run.
    active = _active_job(session, workspace_id)
    if active is not None:
        return _serialize_run(active)
    if uncited_claims_cap_exceeded(
        session, workspace_id=workspace_id, settings=request.app.state.settings
    ):
        raise HTTPException(status_code=429, detail="daily uncited-claims budget reached")
    job = enqueue_uncited_claims_run(session, workspace_id=workspace_id, requested_by=user.id)
    # Trigger event on NEWLY-created runs only (mirrors review_run_triggered) — never when
    # the active-first branch above returns an existing run, so a second POST can't duplicate.
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="uncited_claims_run_triggered",
            actor_id=user.id,
            payload={"job_id": str(job.id)},
        )
    )
    session.commit()
    return _serialize_run(job)


@router.get(
    "/{workspace_id}/uncited-claims-runs/latest",
    response_model=UncitedClaimsLatestOut | None,
)
def latest_uncited_claims_run(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict | None:
    require_member(workspace_id=workspace_id, user=user, session=session)
    job = latest_uncited_claims_job(session, workspace_id=workspace_id)
    if job is None:
        return None
    if job.status == "completed":
        repo_dir = workspace_repo_dir(request.app.state.settings.workspaces_dir, workspace_id)
        stale = job.evaluated_draft_sha != content_sha(repo_dir / PROPOSAL_FILENAME)
        claims = job.claims or []
    else:
        stale = False
        claims = None
    return {**_serialize_run(job), "stale": stale, "claims": claims}


@router.post("/{workspace_id}/uncited-claims/{claim_hash}/dismiss", response_model=OkOut)
def dismiss_uncited_claim(
    workspace_id: uuid.UUID,
    claim_hash: str,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    session.add(
        WorkspaceEvent(
            workspace_id=workspace_id,
            kind="uncited_claim_dismissed",
            actor_id=user.id,
            payload={"claim_hash": claim_hash},
        )
    )
    session.commit()
    return {"ok": True}
```

- [ ] **Step E: Include the router** — in `src/call_manager/main.py`, add the import beside the other web routers (~line 46) and include it after `problems_router` (~line 553):

```python
from call_manager.proposal_assist.web.uncited_claims import (
    router as uncited_claims_router,
)
```
```python
    app.include_router(uncited_claims_router)
```

- [ ] **Step F: Wire soft-delete cancel** — in `src/call_manager/proposal_assist/web/workspaces.py`: add `cancel_uncited_claims_jobs_for_workspace` to the import block that already imports `cancel_evaluator_jobs_for_workspace` (~line 59) — import it from `call_manager.proposal_assist.uncited_claims.jobs`. Then call it next to the others (~line 1334):

```python
    cancel_uncited_claims_jobs_for_workspace(session, workspace_id=workspace_id)
```

(Verify the exact import grouping — the evaluator/coherence/review cancels are imported individually; add the uncited import in the same style, e.g. a new `from call_manager.proposal_assist.uncited_claims.jobs import cancel_uncited_claims_jobs_for_workspace` near the other proposal_assist imports.)

- [ ] **Step G: Soft-delete test** — `tests/proposal_assist/uncited_claims/test_soft_delete.py`:

```python
from call_manager.db.models import UncitedClaimsJob
from tests.proposal_assist.conftest import make_workspace


def test_soft_delete_cancels_active_uncited_claims_jobs(
    client_authed_owner, _app, current_user, pg_session_factory, tmp_path
):
    _app.state.settings.workspaces_dir = str(tmp_path)
    with pg_session_factory() as s:
        ws = make_workspace(s, owner=current_user)
        s.flush()
        s.add(UncitedClaimsJob(workspace_id=ws.id, status="queued"))
        s.commit()
        ws_id = str(ws.id)
    r = client_authed_owner.post(f"/workspaces/{ws_id}/delete", follow_redirects=False)
    assert r.status_code in (200, 303)
    with pg_session_factory() as s:
        job = s.query(UncitedClaimsJob).filter_by(workspace_id=ws_id).one()
        assert job.status == "failed"
```

- [ ] **Step H: Add the scheduler ticks** — in `src/call_manager/scheduler.py`, after the review reaper tick (mirror the evaluator drain+reaper pair). Place after the `_review_*` ticks:

```python
    # SP2b-3d: uncited-claims drain tick (async — one fresh-stateless pass).
    def _uncited_claims_tick() -> None:
        import asyncio

        from call_manager.proposal_assist.uncited_claims.pass_runner import (
            drain_uncited_claims_queue_tick,
        )

        if not settings.drafting_enabled:
            return
        coro = drain_uncited_claims_queue_tick(session_factory=session_factory, settings=settings)
        if app_loop is None:
            try:
                asyncio.run(coro)
            except Exception:
                _logger.exception("uncited-claims tick failed")
            return
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        try:
            fut.result(timeout=settings.uncited_claims_pass_timeout_seconds + 60)
        except Exception:
            _logger.exception("uncited-claims tick failed")

    sched.add_job(
        _uncited_claims_tick,
        trigger=IntervalTrigger(seconds=settings.uncited_claims_tick_seconds),
        id="uncited_claims_drain",
        max_instances=1,
        coalesce=True,
    )

    # SP2b-3d: uncited-claims reaper tick (sync, pure DB). Crash/hung-job recovery.
    def _uncited_claims_reaper_tick() -> None:
        from call_manager.proposal_assist.uncited_claims.jobs import (
            reap_stuck_uncited_claims_jobs,
        )

        try:
            with session_factory() as s:
                reap_stuck_uncited_claims_jobs(
                    s, timeout_seconds=settings.uncited_claims_job_timeout_seconds
                )
                s.commit()
        except Exception:
            _logger.exception("uncited-claims reaper tick failed")

    sched.add_job(
        _uncited_claims_reaper_tick,
        trigger=IntervalTrigger(seconds=settings.uncited_claims_reaper_tick_seconds),
        id="uncited_claims_reaper",
        max_instances=1,
        coalesce=True,
    )
```

- [ ] **Step I: Scheduler wiring test** — `tests/proposal_assist/uncited_claims/test_scheduler_wiring.py`. Clone `evaluator/test_scheduler_wiring.py`, asserting the two job ids + intervals:

```python
import pytest

from call_manager.config import Settings


@pytest.mark.asyncio
async def test_build_scheduler_registers_uncited_claims_ticks(monkeypatch, pg_url):
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
            embed_client=MagicMock(model="bge-m3"), workspace_controller=controller,
            start=False,
        )
        try:
            drain = sched.get_job("uncited_claims_drain")
            reaper = sched.get_job("uncited_claims_reaper")
            assert drain is not None
            assert drain.trigger.interval.total_seconds() == settings.uncited_claims_tick_seconds
            assert reaper is not None
            assert (
                reaper.trigger.interval.total_seconds()
                == settings.uncited_claims_reaper_tick_seconds
            )
        finally:
            sched.shutdown(wait=False)
    finally:
        engine.dispose()
```

- [ ] **Step J: Run all Task-6 tests**

Run: `uv run pytest tests/proposal_assist/uncited_claims/test_web.py tests/proposal_assist/uncited_claims/test_soft_delete.py tests/proposal_assist/uncited_claims/test_scheduler_wiring.py -p no:cacheprovider -q`
Expected: all pass (test_web has 8 cases incl. the trigger-event-once + claims-null-while-running guards). (Re-run any `connection refused` in isolation.)

- [ ] **Step K: Lint + commit**

```bash
uv run ruff check src/call_manager/proposal_assist/web/uncited_claims.py src/call_manager/scheduler.py src/call_manager/main.py src/call_manager/proposal_assist/web/workspaces.py tests/proposal_assist/uncited_claims/test_web.py tests/proposal_assist/uncited_claims/test_soft_delete.py tests/proposal_assist/uncited_claims/test_scheduler_wiring.py
uv run ruff format src/call_manager/proposal_assist/web/uncited_claims.py
git -C .worktrees/sp2b-3d-uncited-claims add -A
git -C .worktrees/sp2b-3d-uncited-claims -c commit.gpgsign=false commit -m "feat(uncited-claims): endpoints + soft-delete cancel + config + scheduler

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Frontend — hooks + ProblemsPanel (run / dismiss / stale) + regen

**Files:**
- Create: `frontend/src/api/uncitedClaims.ts`
- Modify: `frontend/src/api/problems.ts`, `frontend/src/cockpit/ProblemsPanel.tsx`, `frontend/src/api/schema.gen.ts` (regen)
- Test: extend `frontend/src/cockpit/ProblemsPanel.test.tsx`

**Interfaces:**
- Consumes: generated `components["schemas"]["UncitedClaimsLatestOut"]` / `["UncitedClaimsRunOut"]` (after regen).
- Produces: `useLatestUncitedClaimsRun(id)`, `useStartUncitedClaimsRun(id)`, `useDismissClaim(id)`; `ProblemRow.category` gains `"uncited_claim"`, optional `claim_hash`; `ProblemsResponse` gains `uncited_claims_stale: boolean`.

- [ ] **Step 1: Regenerate the OpenAPI client** (Task 6's response_models must already exist on disk):

Run (from `frontend/`): `EMBEDDING_MODEL=bge-m3 pnpm gen:api`
Then confirm the schemas exist: `grep -c "UncitedClaimsLatestOut" src/api/schema.gen.ts` → ≥1. (If 0, the router isn't wired / response_model missing — fix Task 6 before continuing.)

- [ ] **Step 2: Extend `problems.ts` types** — in `frontend/src/api/problems.ts`:

```typescript
export type ProblemRow = {
  category: "broken_citation" | "over_length" | "criterion_gap" | "coherence_drift" | "uncited_claim";
  severity: "error" | "warning";
  title: string;
  detail: string;
  target: ProblemTarget | null;
  /** Present only on uncited_claim rows — the dismiss key. */
  claim_hash?: string;
};

export type ProblemsResponse = {
  problems: ProblemRow[];
  counts: { error: number; warning: number };
  scorecard_stale: string[];
  uncited_claims_stale: boolean;
};
```

- [ ] **Step 3: Implement `uncitedClaims.ts`** — `frontend/src/api/uncitedClaims.ts`. Mirrors `review.ts` (generated DTOs) + `evaluator.ts` (poll + invalidate):

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type UncitedClaimsRun = components["schemas"]["UncitedClaimsRunOut"];
export type UncitedClaimsLatest = components["schemas"]["UncitedClaimsLatestOut"];

const latestKey = (id: string) => ["uncited-claims-latest", id] as const;

/** GET latest uncited-claims run (or null). Polls at 4s while queued/running. */
export function useLatestUncitedClaimsRun(id: string) {
  return useQuery({
    queryKey: latestKey(id),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/workspaces/{workspace_id}/uncited-claims-runs/latest", {
          params: { path: { workspace_id: id } },
        }),
      ),
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "queued" || s === "running" ? 4000 : false;
    },
  });
}

/** POST to start a run (create-or-return-active). Invalidates problems + latest. */
export function useStartUncitedClaimsRun(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/uncited-claims-runs", {
          params: { path: { workspace_id: id } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: latestKey(id) });
      qc.invalidateQueries({ queryKey: ["problems", id] });
    },
  });
}

/** POST dismiss one claim by hash. Invalidates problems so the row disappears. */
export function useDismissClaim(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (claimHash: string) =>
      unwrap(
        await api.POST("/api/workspaces/{workspace_id}/uncited-claims/{claim_hash}/dismiss", {
          params: { path: { workspace_id: id, claim_hash: claimHash } },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["problems", id] });
    },
  });
}
```

- [ ] **Step 4: Extend `ProblemsPanel.tsx`** — add the run button (header), the dismiss action on uncited rows, the uncited group label, and the stale render. Full replacement file:

```tsx
import {
  useDismissClaim,
  useLatestUncitedClaimsRun,
  useStartUncitedClaimsRun,
} from "../api/uncitedClaims";
import { type ProblemRow, useProblems } from "../api/problems";
import type { NavTarget, OnNavigate } from "./nav";

const CATEGORY_LABEL: Record<ProblemRow["category"], string> = {
  broken_citation: "Broken citations",
  criterion_gap: "Criteria gaps",
  over_length: "Over-length sections",
  coherence_drift: "Coherence",
  uncited_claim: "Uncited claims",
};
const CATEGORY_ORDER: ProblemRow["category"][] = [
  "broken_citation",
  "criterion_gap",
  "over_length",
  "coherence_drift",
  "uncited_claim",
];

function toNav(target: ProblemRow["target"]): NavTarget {
  return {
    top_tab: target?.top_tab ?? undefined,
    bottom_tab: target?.bottom_tab ?? undefined,
  };
}

export function ProblemsPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const q = useProblems(id);
  const latest = useLatestUncitedClaimsRun(id);
  const start = useStartUncitedClaimsRun(id);
  const dismiss = useDismissClaim(id);
  const runStatus = latest.data?.status;
  const running = runStatus === "queued" || runStatus === "running";

  const runButton = (
    <button
      type="button"
      className="problems-run-uncited"
      disabled={running || start.isPending}
      onClick={() => start.mutate()}
    >
      {running ? "Checking…" : "Check uncited claims"}
    </button>
  );

  if (q.isError) {
    return (
      <div className="problems-panel">
        <div className="problems-toolbar">{runButton}</div>
        <p role="status" className="problems-error">
          Could not load problems.
        </p>
      </div>
    );
  }
  if (q.isLoading) {
    return (
      <div className="problems-panel">
        <div className="problems-toolbar">{runButton}</div>
        <p className="problems-loading">Loading…</p>
      </div>
    );
  }
  const data = q.data;
  const rows = data?.problems ?? [];
  const scorecardStale = (data?.scorecard_stale ?? []).length > 0;
  const uncitedStale = data?.uncited_claims_stale ?? false;
  const groups = CATEGORY_ORDER.map((cat) => ({
    cat,
    items: rows.filter((r) => r.category === cat),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="problems-panel">
      <div className="problems-toolbar">{runButton}</div>
      {rows.length === 0 ? (
        <p className="problems-empty">No problems detected.</p>
      ) : (
        groups.map((g) => (
          <section key={g.cat} className="problems-group" aria-label={CATEGORY_LABEL[g.cat]}>
            <h3 className="problems-group-title">
              {CATEGORY_LABEL[g.cat]}
              {g.cat === "criterion_gap" && scorecardStale && (
                <span className="problems-stale"> · may be outdated</span>
              )}
              {g.cat === "uncited_claim" && uncitedStale && (
                <span className="problems-stale"> · may be outdated</span>
              )}
            </h3>
            <ul>
              {g.items.map((r, i) => (
                <li key={`${r.category}-${i}`} className={`problem-row severity-${r.severity}`}>
                  <span className="problem-sev" aria-hidden="true">
                    {r.severity === "error" ? "⛔" : "⚠"}
                  </span>
                  <span className="problem-title">{r.title}</span>
                  <span className="problem-detail">{r.detail}</span>
                  {r.target && (
                    <button
                      type="button"
                      className="problem-act"
                      onClick={() => onNavigate(toNav(r.target))}
                    >
                      Go to {r.title}
                    </button>
                  )}
                  {r.category === "uncited_claim" && r.claim_hash && (
                    <button
                      type="button"
                      className="problem-dismiss"
                      disabled={dismiss.isPending}
                      onClick={() => dismiss.mutate(r.claim_hash as string)}
                    >
                      Dismiss
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </div>
  );
}
```

- [ ] **Step 5: Extend the vitest** — in `frontend/src/cockpit/ProblemsPanel.test.tsx`. The component now imports the uncited hooks, so mock them. Add at the top (after the existing `vi.mock("../api/problems", ...)`):

```typescript
vi.mock("../api/uncitedClaims", () => ({
  useLatestUncitedClaimsRun: () => ({ data: undefined }),
  useStartUncitedClaimsRun: () => ({ mutate: vi.fn(), isPending: false }),
  useDismissClaim: () => h.dismiss,
}));
```

Extend the hoisted state to carry the dismiss spy:

```typescript
const h = vi.hoisted(
  (): { problems: { data: unknown; isError?: boolean }; dismiss: { mutate: ReturnType<typeof vi.fn>; isPending: boolean } } => ({
    problems: { data: undefined },
    dismiss: { mutate: vi.fn(), isPending: false },
  }),
);
```

(Update `afterEach` to also reset `h.dismiss = { mutate: vi.fn(), isPending: false };`.)

Add a new test:

```typescript
it("renders uncited rows with a dismiss action and a run button", () => {
  h.problems = {
    data: {
      problems: [
        {
          category: "uncited_claim",
          severity: "warning",
          title: "30% relapse",
          detail: "statistic",
          target: { top_tab: "draft", bottom_tab: "problems", section_index: 1 },
          claim_hash: "h1",
        },
      ],
      counts: { error: 0, warning: 1 },
      scorecard_stale: [],
      uncited_claims_stale: true,
    },
  };
  wrap(<ProblemsPanel id="ws1" onNavigate={vi.fn()} />);
  expect(screen.getByRole("button", { name: /check uncited claims/i })).toBeInTheDocument();
  expect(screen.getByText(/may be outdated/i)).toBeInTheDocument(); // uncited stale badge
  fireEvent.click(screen.getByRole("button", { name: /^dismiss$/i }));
  expect(h.dismiss.mutate).toHaveBeenCalledWith("h1");
});
```

> Note: the existing "empty state" test asserts `getByText(/no problems detected/i)` — still true, but the run button is now always present (it lives in the toolbar above the empty message). The existing error/empty/rows tests keep passing because the toolbar is additive; only confirm the `getByText("No problems detected")` assertion still resolves (it does — the empty `<p>` is rendered when `rows.length === 0`).

- [ ] **Step 6: Gate the frontend**

Run (from `frontend/`): `pnpm exec tsc --noEmit` → 0 errors. Then `pnpm vitest run src/cockpit/ProblemsPanel.test.tsx` → all pass.
Then the full frontend gate: `pnpm exec tsc --noEmit && pnpm vitest run` → green.

- [ ] **Step 7: Commit**

```bash
git -C .worktrees/sp2b-3d-uncited-claims add -A
git -C .worktrees/sp2b-3d-uncited-claims -c commit.gpgsign=false commit -m "feat(uncited-claims): frontend hooks + ProblemsPanel run/dismiss/stale + regen

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (after Task 7)

- [ ] **Backend (affected files only — full suite is testcontainer-flaky):**

```bash
uv run pytest tests/proposal_assist/uncited_claims/ tests/proposal_assist/problems/ tests/proposal_assist/test_tools_registry.py tests/proposal_assist/test_tools_claude_adapter_wiring.py -p no:cacheprovider -q
```
Expected: all green. Re-run any `connection refused` file in isolation.

- [ ] **Ruff:** `uv run ruff check src/call_manager/proposal_assist/uncited_claims src/call_manager/proposal_assist/tools/uncited_claims.py src/call_manager/proposal_assist/web/uncited_claims.py src/call_manager/proposal_assist/problems` → clean.

- [ ] **Frontend:** from `frontend/`, `pnpm exec tsc --noEmit && pnpm vitest run` → green.

- [ ] **Stop before merge.** Per the project convention (no auto-push, local-only master), STOP at the green worktree branch. Independent verification + Codex code review happen before any merge to local master.

---

## Self-Review (run by the plan author)

**1. Spec coverage** — every spec section maps to a task:
- §0 locked decisions → enforced across Tasks 1–7 (distinct event kind §0.3 → Tasks 2/6; soft-delete §0.6 → Tasks 2/6; per-claim cap+tail §0.2 → Task 5; JSONB §0.5 → Task 1; section-level deep-link §0.4 → Task 5 builder target).
- §1 migration+model → Task 1. §2 prompt contract → Task 4 `_ROLE`. §3 package → Tasks 2/4 (+ agent helper in Task 3); late-write token roll-up (don't resurrect; still roll up tokens) → Task 4 finalize. §4 submit tool → Task 3. §5 Problems integration → Task 5. §6 endpoints (incl. `uncited_claims_run_triggered` WorkspaceEvent on new runs only) → Task 6. §7 config → Task 4 Step 0; scheduler → Task 6. §8 frontend → Task 7. §9 testing → tests in every task. §10 file map → File Structure section. §11 build decomposition → the 7 tasks (note: agent.py moved into Task 3 so the wiring-test union can source `uncited_claims_allowed_tools()`; config moved into Task 4 Step 0 so each task is independently runnable). §12 out-of-scope → not built (Activity feed = SP2b-3e).

**2. Placeholder scan** — no TBD/TODO/"handle errors"; every code step shows complete code; every test step shows real assertions.

**3. Type consistency** — `claim_hash` (str), `section_index` (int), `claims` (list), `uncited_claims_stale` (bool top-level), `evaluated_draft_sha` (str). `submit_uncited_claims_impl(ctx, *, claims)`, `uncited_claim_problems(claims, dismissed, *, cap=10)` (no `stale`), `build_uncited_claims_adapter(..., draft_section_count)`, `uncited_claims_allowed_tools()`, `assemble_uncited_claims_context(...) -> {instruction, draft_section_count, section_texts, draft_sha}` — names consistent across tasks and matched to the spec.

**One cross-task sequencing note for the executor:**
- Task 7 Step 1 (regen) requires Task 6's response_models to be on disk. (Config now lands in Task 4 Step 0, so every other task is independently runnable as written.)
