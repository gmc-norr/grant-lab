"""A funding call, its source, and the data derived from its text."""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from grant_lab.db.base import Base


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Persisted failure streak — survives restarts. Incremented on each
    # ingestion failure, reset to 0 on success. The alerter reads/writes this
    # column transactionally alongside last_fetched_at so the count is never
    # stale after an OOM kill or rolling deploy.
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Set to now() each time an alert is successfully sent for this source.
    # The alerter gates re-alerting on this so repeated runs after the
    # threshold don't spam the admin.
    last_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    calls: Mapped[list[Call]] = relationship(back_populates="source")


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        # Unique per source per external_id — the idempotency contract for
        # upsert; the implicit unique index backs the ON CONFLICT clause.
        UniqueConstraint("source_id", "external_id", name="uq_calls_source_external"),
        # Indices declared on the model so `alembic check` / autogenerate stay
        # consistent with the migration. The dashboard sorts on
        # published_at/deadline; the digest filters keywords via && (overlap),
        # which a GIN index accelerates.
        Index("ix_calls_keywords_gin", "keywords", postgresql_using="gin"),
        Index("ix_calls_deadline", "deadline"),
        Index("ix_calls_published_at", "published_at"),
        # Embedding + model are written together (see migration 0007). Declared
        # here so the model — not just the migration — is the source of truth;
        # otherwise autogenerate would emit a spurious DROP for these.
        CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)",
            name="ck_calls_embedding_model_paired",
        ),
        Index(
            "ix_calls_embedding_null",
            "id",
            postgresql_where=text("embedding IS NULL"),
        ),
        # Trigram GIN indexes back the digest must-include ILIKE '%term%'
        # matching (see digest.assemble) — leading-wildcard LIKE is otherwise
        # a sequential scan. Requires the pg_trgm extension (migration 0033).
        Index(
            "ix_calls_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_calls_summary_trgm",
            "summary",
            postgresql_using="gin",
            postgresql_ops={"summary": "gin_trgm_ops"},
        ),
        Index(
            "ix_calls_full_text_trgm",
            "full_text",
            postgresql_using="gin",
            postgresql_ops={"full_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    budget_min: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    programme: Mapped[str | None] = mapped_column(String(200), nullable=True)
    keywords: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1024),
        nullable=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[Source] = relationship(back_populates="calls")

    @property
    def structured(self) -> dict:
        """Alias for `raw` — duck-types the criteria-module interface which
        reads trl_min/trl_max from call.structured. Returns {} when raw is
        empty so gate checks default to UNKNOWN (not FAIL)."""
        return self.raw or {}


class CallSectionExtract(Base):
    __tablename__ = "call_section_extracts"
    __table_args__ = (
        CheckConstraint(
            "section IN ('eligibility','scope','budget',"
            "'expected_outcomes','restrictions','mandatory')",
            name="ck_call_section_extracts_section",
        ),
    )

    call_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("calls.id", ondelete="CASCADE"),
        primary_key=True,
    )
    section: Mapped[str] = mapped_column(Text, primary_key=True)
    extract_text: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extracted_by_model: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_text_hash: Mapped[str] = mapped_column(Text, nullable=False)


class CallRubric(Base):
    __tablename__ = "call_rubrics"

    call_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("calls.id", ondelete="CASCADE"),
        primary_key=True,
    )
    criteria: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    funder_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extracted_by_model: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_text_hash: Mapped[str] = mapped_column(Text, nullable=False)
