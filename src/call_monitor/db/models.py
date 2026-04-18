from __future__ import annotations

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from call_monitor.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Defense in depth: the application always lowercases before insert,
        # but Postgres `UNIQUE(email)` is case-sensitive, so a rogue SQL
        # admin task could create both `Alice@ki.se` and `alice@ki.se` and
        # split a single person across two accounts. The CHECK forces
        # lowercase at the DB level — combined with UNIQUE(email), this
        # guarantees one row per address regardless of case.
        CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    digest_bounce_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    digest_disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    magic_link_tokens: Mapped[list[MagicLinkToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class MagicLinkToken(Base):
    __tablename__ = "magic_link_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="magic_link_tokens")

    # `unique=True` on token_hash already creates a unique index — no separate
    # Index() needed. Verification path is `WHERE token_hash = :h`, which uses
    # the unique index.


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

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
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[Source] = relationship(back_populates="calls")

    @property
    def structured(self) -> dict:
        """Alias for `raw` — duck-types the criteria-module interface which
        reads trl_min/trl_max from call.structured. Returns {} when raw is
        empty so gate checks default to UNKNOWN (not FAIL)."""
        return self.raw or {}


class ProfileKind(enum.StrEnum):
    ORG = "org"
    GROUP = "group"
    USER = "user"


class RankingJobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    DONE = "done"


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[ProfileKind] = mapped_column(
        Enum(ProfileKind, name="profile_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    structured: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # ISO 4217. NULL = unknown — v2's budget gate treats NULL as no penalty.
    budget_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(1024), nullable=True,
    )
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    digest_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CallRanking(Base):
    __tablename__ = "call_rankings"
    __table_args__ = (
        # Covers WHERE profile_id=? ORDER BY aggregate_score DESC, the hot
        # dashboard query. NULLS LAST so unranked calls sort to the bottom.
        Index(
            "ix_call_rankings_profile_score",
            "profile_id",
            text("aggregate_score DESC NULLS LAST"),
            postgresql_using="btree",
        ),
    )

    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    # Renamed from `score` to align with v2 vocabulary.
    aggregate_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # 'timeline' | 'budget' | 'trl' | 'eligibility' | 'exclusion' | NULL
    aggregate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sample_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    ranked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    embed_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)


class RankingJob(Base):
    __tablename__ = "ranking_jobs"
    __table_args__ = (
        Index("ix_ranking_jobs_status_updated", "status", "updated_at"),
        Index(
            "uq_ranking_jobs_active_pair",
            "call_id",
            "profile_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[RankingJobStatus] = mapped_column(
        Enum(
            RankingJobStatus,
            name="ranking_job_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )


class UserCallDismissal(Base):
    __tablename__ = "user_call_dismissals"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DigestRun(Base):
    __tablename__ = "digest_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # BigInteger[] (NOT Integer[]) because calls.id is BigInteger — silently
    # truncating a 2^31+ id would be a nasty post-release bug.
    included_call_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list
    )

    __table_args__ = (
        Index("ix_digest_runs_user_sent", "user_id", text("sent_at DESC")),
    )


class CallRankingCriterion(Base):
    __tablename__ = "call_ranking_criteria"

    call_id: Mapped[int] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), primary_key=True
    )
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True
    )
    # 'topic_fit' | 'eligibility' | 'exclusions' | 'timeline' | 'budget' | 'trl'
    criterion: Mapped[str] = mapped_column(String(20), primary_key=True)
    # Canonical string. e.g. "5", "pass", "fail", "unclear".
    rating: Mapped[str] = mapped_column(String(20), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    quoted_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    ranked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
