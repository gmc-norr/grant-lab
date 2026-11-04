"""Reference material, its chunks, and the citation/claim audit trail."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base


class ReviewRun(Base):
    __tablename__ = "review_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','done','failed','cancelled','partial')",
            name="ck_review_runs_status",
        ),
        Index(
            "ix_review_runs_ws_triggered",
            "workspace_id",
            text("triggered_at DESC"),
        ),
        Index(
            "uq_review_runs_active",
            "workspace_id",
            unique=True,
            postgresql_where=text("status IN ('queued','running')"),
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
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Fence F1 (spec 2026-08-17 §3.3). The workspaces.lifecycle_generation this
    # actor observed AT CLAIM TIME. Every semantic effect it later produces is
    # fenced against this value, so work belonging to an epoch the workspace has
    # left cannot commit. NULL = legacy row, claimed before the fence existed;
    # those fall back to the liveness check. NOT defaulted to 0 — 0 is a real
    # generation. Set by the claim statement in F4-F7; unused in F1.
    claimed_at_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    draft_sha: Mapped[str] = mapped_column(Text, nullable=False)
    fit_check_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric_review_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    waived_from_review_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    # Numeric(...) hydrates to decimal.Decimal, not float (see ReviewRun above).
    estimated_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 3),
        nullable=True,
    )


class ReviewRunItem(Base):
    __tablename__ = "review_run_items"
    __table_args__ = (
        UniqueConstraint(
            "review_run_id",
            "kind",
            name="uq_review_run_items_run_kind",
        ),
        CheckConstraint(
            "kind IN ('fit_check',"
            "'persona_domain_skeptic',"
            "'persona_methodology_critic',"
            "'persona_translation_skeptic',"
            "'persona_devils_advocate',"
            "'consolidation')",
            name="ck_review_run_items_kind",
        ),
        CheckConstraint(
            "status IN ('queued','running','done','failed','skipped','cancelled')",
            name="ck_review_run_items_status",
        ),
        Index("ix_review_run_items_run", "review_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("review_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    # Numeric(...) hydrates to decimal.Decimal, not float (see ReviewRun above).
    estimated_dollars: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 4),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )


class Reference(Base):
    """A per-workspace reference — uploaded paper, prior proposal, analysis,
    or data file. See docs/superpowers/specs/2026-05-25-references-corpus-design.md."""

    __tablename__ = "references"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('paper','prior_proposal','analysis','data')",
            name="ck_references_kind",
        ),
        CheckConstraint(
            "source_type IN ('uploaded','crossref','pubmed','arxiv','web')",
            name="ck_references_source_type",
        ),
        CheckConstraint(
            "text_status IN ('pending','running','done','failed','skipped')",
            name="ck_references_text_status",
        ),
        CheckConstraint(
            "outline_status IN ('pending','running','done','failed','skipped')",
            name="ck_references_outline_status",
        ),
        CheckConstraint(
            "metadata_status IN ('pending','extracted','verified_external','approved','failed')",
            name="ck_references_metadata_status",
        ),
        CheckConstraint(
            "original_sha256 IS NULL OR original_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_references_original_sha256_hex",
        ),
        Index(
            "uq_references_workspace_citekey",
            "workspace_id",
            "citekey",
            unique=True,
            postgresql_where=text("citekey IS NOT NULL"),
        ),
        # Created by 0031, absent from the ORM until the 2026-09-02 review
        # (tests/test_schema_drift.py). The indexing backlog scan reads it.
        Index(
            "ix_references_indexing_backlog",
            "workspace_id",
            "created_at",
            postgresql_where=text(
                "text_status IN ('pending','running') OR outline_status IN ('pending','running')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    bytes_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    text_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    text_chars_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outline_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="skipped")
    outline_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    outline_indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        # Explicit RESTRICT — matches the prior default NO ACTION behavior.
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    csl_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    csl_json_extracted: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    citekey: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    metadata_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Soft-delete timestamp (tombstone). A cited reference is tombstoned rather
    # than hard-deleted so existing citations keep resolving; tombstoned refs are
    # filtered out of discovery (list/search) and cannot be newly cited.
    # No partial index is added here (deliberately — per-workspace row counts are
    # small and workspace_id is already indexed, so the added complexity of a
    # partial index is not warranted).
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Metadata-waiver flag: when True, the uploader has confirmed that the
    # missing/incomplete bibliographic metadata is acceptable (e.g. grey
    # literature, personal communication). The reason field captures the
    # human-entered justification for audit purposes.
    metadata_waiver: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
    )
    metadata_waiver_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReferenceChunk(Base):
    """A chunk of a Reference, with its embedding for semantic search."""

    __tablename__ = "reference_chunks"
    __table_args__ = (
        UniqueConstraint("reference_id", "chunk_index", name="uq_reference_chunks_ref_idx"),
        # Both indexes existed only in migrations (0031 raw SQL, 0032) until the
        # 2026-09-02 review: `alembic revision --autogenerate` would have DROPPED
        # the vector index. tests/test_schema_drift.py now pins ORM == database.
        Index("ix_reference_chunks_ws_model", "workspace_id", "embedding_model"),
        Index(
            "ix_reference_chunks_ws_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("references.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalized from the parent Reference for single-table search filters.
    # FK (CASCADE) ensures it can't point at a non-existent/deleted workspace.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)


class CitationLlmUsage(Base):
    """Append-only ledger of every LLM call the citation subsystem makes
    (metadata extraction now; audit later). Basis for per-workspace + global
    daily caps. Written only via CitationLLMService. See the spec."""

    __tablename__ = "citation_llm_usage"
    __table_args__ = (
        CheckConstraint("status IN ('ok','failed','timeout')", name="ck_citation_llm_usage_status"),
        CheckConstraint(
            "tokens_in >= 0 AND tokens_out >= 0", name="ck_citation_llm_usage_tokens_nonneg"
        ),
        Index("ix_citation_llm_usage_ws_created", "workspace_id", "created_at"),
        Index("ix_citation_llm_usage_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE", name="fk_citation_llm_usage_workspace_id"),
        nullable=False,
    )
    op: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CitationAuditVerdict(Base):
    """Cache of semantic-verdict results from the citation audit validator.
    Keyed by a content hash (cache_key) including the relied-on quote, so
    re-running validate_citations over an unchanged draft is free. Bump
    validator_version to invalidate the whole cache. See the spec."""

    __tablename__ = "citation_audit_verdicts"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('supports','contradicts','unsupported','relevant','irrelevant')",
            name="ck_citation_audit_verdicts_verdict",
        ),
        Index("ix_citation_audit_verdicts_ws_cache", "workspace_id", "cache_key"),
        Index("ix_citation_audit_verdicts_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "workspaces.id",
            ondelete="CASCADE",
            name="fk_citation_audit_verdicts_workspace_id",
        ),
        nullable=False,
    )
    cache_key: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    validator_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ClaimClassification(Base):
    """Reusable content-level cache: is this sentence a claim, and what did
    retrieval find for it? Keyed by claim content, NOT occurrence. Spec §3."""

    __tablename__ = "claim_classifications"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('not_claim','supported','no_match')",
            name="ck_claim_classifications_outcome",
        ),
        UniqueConstraint(
            "workspace_id",
            "claim_sha",
            "prompt_version",
            name="uq_claim_classifications_ws_sha_pv",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    claim_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False)
    is_claim: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("references.id", ondelete="SET NULL"), nullable=True
    )
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    stale_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserClaimAnnotation(Base):
    """Occurrence-addressed, user-facing analysis finding. One row per
    occurrence: section snapshot + duplicate-section ordinal + sentence
    ordinal (+ evidence id for cited kinds). Spec §3."""

    __tablename__ = "user_claim_annotations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('uncited_supported','uncited_no_match','cited_unsupported')",
            name="ck_user_claim_annotations_kind",
        ),
        CheckConstraint(
            "status IN ('open','inserted','dismissed','stale')",
            name="ck_user_claim_annotations_status",
        ),
        UniqueConstraint(
            "workspace_id",
            "section_sha",
            "section_occurrence",
            "sentence_ordinal",
            "kind",
            "evidence_id",
            name="uq_user_claim_annotations_occurrence",
        ),
        Index("ix_user_claim_annotations_ws_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    section_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    section_occurrence: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    section_heading: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    sentence_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    char_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # "" sentinel for uncited kinds; ev-id for cited_unsupported (keeps the
    # composite unique constraint enforceable — Postgres treats NULLs as
    # distinct).
    evidence_id: Mapped[str] = mapped_column(String(32), nullable=False, server_default="")
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("references.id", ondelete="SET NULL"), nullable=True
    )
    chunk_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
