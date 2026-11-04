"""Background work queued against a workspace."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base


class TurnJob(Base):
    __tablename__ = "turn_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed','cancelled')",
            name="ck_turn_jobs_status",
        ),
        Index("ix_turn_jobs_ws_queued", "workspace_id", "queued_at"),
        Index(
            "uq_turn_jobs_running",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
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
    user_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Fence F1 (spec 2026-08-17 §3.3). The workspaces.lifecycle_generation this
    # actor observed AT CLAIM TIME. Every semantic effect it later produces is
    # fenced against this value, so work belonging to an epoch the workspace has
    # left cannot commit. NULL = legacy row, claimed before the fence existed;
    # those fall back to the liveness check. NOT defaulted to 0 — 0 is a real
    # generation. Set by the claim statement in F4-F7; unused in F1.
    claimed_at_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    submitted_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # Explicit RESTRICT — matches the prior default NO ACTION behavior.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    fetch_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


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
    # Fence F1 (spec 2026-08-17 §3.3). The workspaces.lifecycle_generation this
    # actor observed AT CLAIM TIME. Every semantic effect it later produces is
    # fenced against this value, so work belonging to an epoch the workspace has
    # left cannot commit. NULL = legacy row, claimed before the fence existed;
    # those fall back to the liveness check. NOT defaulted to 0 — 0 is a real
    # generation. Set by the claim statement in F4-F7; unused in F1.
    claimed_at_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # Fence F1 (spec 2026-08-17 §3.3). The workspaces.lifecycle_generation this
    # actor observed AT CLAIM TIME. Every semantic effect it later produces is
    # fenced against this value, so work belonging to an epoch the workspace has
    # left cannot commit. NULL = legacy row, claimed before the fence existed;
    # those fall back to the liveness check. NOT defaulted to 0 — 0 is a real
    # generation. Set by the claim statement in F4-F7; unused in F1.
    claimed_at_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # Fence F1 (spec 2026-08-17 §3.3). The workspaces.lifecycle_generation this
    # actor observed AT CLAIM TIME. Every semantic effect it later produces is
    # fenced against this value, so work belonging to an epoch the workspace has
    # left cannot commit. NULL = legacy row, claimed before the fence existed;
    # those fall back to the liveness check. NOT defaulted to 0 — 0 is a real
    # generation. Set by the claim statement in F4-F7; unused in F1.
    claimed_at_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


class AttachmentJob(Base):
    __tablename__ = "attachment_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','completed','failed')",
            name="ck_attachment_jobs_status",
        ),
        CheckConstraint(
            "kind IN ('describe_figure')",
            name="ck_attachment_jobs_kind",
        ),
        Index(
            "ix_attachment_jobs_status_updated",
            "status",
            "updated_at",
        ),
        Index(
            "uq_attachment_jobs_active",
            "attachment_id",
            "kind",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
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
    attachment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("attachments.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ClaimAnalysisJob(Base):
    """Tier-2 claim-analysis job for one section snapshot. Append-only and
    content-addressed: a row is never retargeted; supersession at drain time
    skips jobs whose section_sha no longer exists at HEAD. Spec 2026-06-10 §3."""

    __tablename__ = "claim_analysis_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','done','failed','superseded')",
            name="ck_claim_analysis_jobs_status",
        ),
        # Content-keyed enqueue dedup: at most one QUEUED job per section content.
        Index(
            "uq_claim_analysis_jobs_ws_sha_queued",
            "workspace_id",
            "section_sha",
            unique=True,
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_claim_analysis_jobs_status_queued_at", "status", "queued_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)  # display hint only
    section_heading: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    section_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    # Fence F1 (spec 2026-08-17 §3.3). The workspaces.lifecycle_generation this
    # actor observed AT CLAIM TIME. Every semantic effect it later produces is
    # fenced against this value, so work belonging to an epoch the workspace has
    # left cannot commit. NULL = legacy row, claimed before the fence existed;
    # those fall back to the liveness check. NOT defaulted to 0 — 0 is a real
    # generation. Set by the claim statement in F4-F7; unused in F1.
    claimed_at_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
