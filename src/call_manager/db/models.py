from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from call_manager.db.base import Base


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
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="ck_users_status_valid",
        ),
        CheckConstraint(
            "(oidc_iss IS NULL) = (oidc_sub IS NULL)",
            name="ck_users_oidc_paired_null",
        ),
        Index(
            "uq_users_oidc_identity",
            "oidc_iss",
            "oidc_sub",
            unique=True,
            postgresql_where=text("oidc_sub IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Fails CLOSED: a User created without an explicit status is `pending`
    # (no access). Only the migration backfill and admin bootstrap make a
    # user `active`.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    oidc_iss: Mapped[str | None] = mapped_column(Text, nullable=True)
    oidc_sub: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    digest_bounce_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    digest_disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Monotonic counter embedded in the signed session cookie. Bumping it
    # (on logout or admin revocation) invalidates every outstanding cookie
    # for this user, since sessions are otherwise stateless signed tokens.
    session_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # Set during first-run value loop: the profile the user most recently
    # bootstrapped or selected as their default. SET NULL on profile delete.
    default_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # use_alter: `users` and `profiles` reference each other (this column,
        # and profiles.created_by -> users.id), so SQLAlchemy cannot order the
        # two for whole-metadata DDL. Deferring THIS side to a post-create
        # ALTER breaks the cycle; it has to be this side because it is the
        # nullable one — profiles.created_by is NOT NULL and cannot wait.
        # The name matches alembic 0046's create_foreign_key, so the model and
        # the database agree and no migration is needed.
        ForeignKey(
            "profiles.id",
            ondelete="SET NULL",
            name="fk_users_default_profile_id",
            use_alter=True,
        ),
        nullable=True,
    )


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
    last_alerted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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


class ProfileKind(enum.StrEnum):
    ORGANIZATIONAL_UNIT = "organizational_unit"
    INDIVIDUAL = "individual"


class ProfileLevel(enum.StrEnum):
    LAB = "lab"
    GROUP = "group"
    DEPARTMENT = "department"
    CLINIC = "clinic"
    INSTITUTION = "institution"
    OTHER = "other"


class RankingJobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    DONE = "done"


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        # Partial UNIQUE on active names. Closes the race in the handler
        # that pre-checks, releases the session, then inserts. Archived
        # profiles (is_active=false) can reuse the name — that's why it's
        # partial. See alembic/versions/0009_*.
        Index(
            "uq_profiles_active_name",
            "name",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
        # Mirror migration 0007's embedding constraints/indexes in the ORM so
        # the model stays the source of truth (autogenerate parity).
        CheckConstraint(
            "(embedding IS NULL) = (embedding_model IS NULL)",
            name="ck_profiles_embedding_model_paired",
        ),
        Index(
            "ix_profiles_embedding_null",
            "id",
            postgresql_where=text("embedding IS NULL"),
        ),
        CheckConstraint(
            "kind IN ('organizational_unit','individual')",
            name="ck_profiles_kind",
        ),
        CheckConstraint(
            "level IS NULL OR level IN ('lab','group','department','clinic','institution','other')",
            name="ck_profiles_level",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
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
        Vector(1024),
        nullable=True,
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
        # dashboard query. NB: aggregate_score is NOT NULL, so an UNRANKED call
        # simply has no call_rankings row at all — it never appears in this
        # index. The trailing NULLS LAST is therefore a no-op on real data;
        # it's retained only to match the existing on-disk index DDL (changing
        # it would force a cosmetic migration for zero behavioural gain).
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
    # G-Eval expected value of topic_fit over the rating-token logprobs
    # (Phase 3). NULL ⇒ argmax fallback was used, or a pre-v5.0 / remote row.
    topic_fit_expected: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Populated only when the high-cosine pair was scored by the remote
    # (Sonnet) backend with enable_analysis=True. NULL for Gemma rows
    # and for gate-short-circuited rows (no LLM ran). See
    # ranking/prompt.py::_sanitize_applicant_notes for the shape contract.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicant_notes: Mapped[list[dict] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # Outcome-based escalation (Phase 4). escalated=True ⇒ a remote (Sonnet)
    # result replaced the local one; escalated_from_score keeps the local
    # score for disagreement mining. analysis_model attributes the *displayed*
    # analysis (summary/applicant_notes), which can differ from `model` after a
    # failed/capped escalation preserved older Sonnet prose.
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    escalated_from_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    analysis_model: Mapped[str | None] = mapped_column(Text, nullable=True)


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
        # Non-active row query indexes (migration 0057):
        # ix_ranking_jobs_call_profile — correlated NOT EXISTS on (call_id,
        # profile_id) for failed rows in reconcile_missing_rankings, and the
        # _ranking_counts JOIN key.
        Index("ix_ranking_jobs_call_profile", "call_id", "profile_id"),
        # ix_ranking_jobs_profile_status — (profile_id, status) grouping used
        # by _ranking_counts and per-profile status breakdown queries.
        Index("ix_ranking_jobs_profile_status", "profile_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
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
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
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
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # BigInteger[] (NOT Integer[]) because calls.id is BigInteger — silently
    # truncating a 2^31+ id would be a nasty post-release bug.
    #
    # DENORMALIZED AUDIT SNAPSHOT — INTENTIONALLY NO FK / NO REFERENTIAL
    # INTEGRITY. This is an immutable historical record of exactly which call
    # ids went into a given digest at send time. It must NEVER be re-resolved
    # against the calls table to "freshen" it: calls can be deleted, merged, or
    # re-ranked afterwards, and the snapshot must still reflect what was sent.
    # If these ids are ever joined back to calls, tolerate misses (a missing
    # call is expected, not an error) — do not add a FK and do not assume every
    # id still exists.
    included_call_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list
    )
    # Populated by the DB via `GENERATED ALWAYS AS (date_trunc('week',
    # sent_at, 'Europe/Stockholm')) STORED`. The Computed() marker tells
    # SQLAlchemy to leave it out of INSERT/UPDATE statements. Backs the
    # unique index that enforces one-digest-per-user-per-Stockholm-week
    # idempotency. MUST use the same timezone as digest.send.week_start()
    # — a UTC truncation here disagrees with the app's Stockholm-week skip
    # check in the Sun-22:00..Mon-00:00-UTC seam, causing missed/duplicate
    # digests (see migration 0033).
    week_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        Computed("date_trunc('week', sent_at, 'Europe/Stockholm')", persisted=True),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_digest_runs_user_sent", "user_id", text("sent_at DESC")),
        Index("uq_digest_runs_user_week", "user_id", "week_start", unique=True),
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


class RankingRemoteAttempt(Base):
    """Append-only ledger of remote (Sonnet) escalation attempts. The daily
    cap counts rows here, not call_rankings.escalated, so repeated reranks and
    failed-but-billed calls are counted honestly (Codex R1)."""

    __tablename__ = "ranking_remote_attempts"
    __table_args__ = (
        Index("ix_ranking_remote_attempts_created_at", "created_at"),
        CheckConstraint(
            "outcome IN ('success','llm_error','parse_error')",
            name="ck_ranking_remote_attempts_outcome",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GoldenSetTriple(Base):
    """Curated (call, profile) → expected_score rows the weekly eval scores
    Kendall's tau against. DB-backed so admins can add/remove from the UI;
    the JSON fixture (tests/fixtures/golden_set.json) survives only for unit
    tests of the tau math."""

    __tablename__ = "golden_set_triples"
    __table_args__ = (
        UniqueConstraint("call_id", "profile_id", name="uq_golden_set_triples_pair"),
        CheckConstraint(
            "expected_score BETWEEN 0 AND 100",
            name="ck_golden_set_triples_score_range",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    expected_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


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


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status IN ('drafting','submitted','awarded','rejected','abandoned')",
            name="ck_workspaces_status",
        ),
        CheckConstraint(
            "current_phase IN ('setup','active')",
            name="ck_workspaces_current_phase",
        ),
        CheckConstraint(
            "citation_style IN ('vancouver','author-year')",
            name="ck_workspaces_citation_style",
        ),
        Index(
            "ix_workspaces_owner_status",
            "owner_id",
            "status",
            postgresql_where=text("soft_deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[int] = mapped_column(
        BigInteger,
        # Explicit RESTRICT makes the (previously default NO ACTION) delete
        # policy intentional: a user who owns workspaces cannot be hard-deleted.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    call_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("calls.id", ondelete="SET NULL"),
        nullable=True,
        # Indexed so deleting a Call (SET NULL) doesn't full-scan workspaces.
        index=True,
    )
    profile_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="drafting",
    )
    deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
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
    soft_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Retention Slice C0. Incremented by EVERY lifecycle transition (delete
    # today, restore in C1) inside that transition's own transaction, under the
    # commit mutex — so the mutex totally orders generations and a transition's
    # teardown can be recognised as belonging to a superseded one. Never read
    # and re-written from Python: see bump_lifecycle_generation.
    lifecycle_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    current_phase: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="setup",
    )
    spec_locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    spec_locked_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    call_body_snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="",
    )
    profile_desc_snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="",
    )
    snapshot_refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    rubric_criteria: Mapped[list[dict]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    rubric_funder_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    section_extracts: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    review_runs_per_day_override: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    # Numeric(...) hydrates to decimal.Decimal, not float — annotate as such so
    # arithmetic with Decimal callers stays type-correct (float mixing raises).
    review_dollar_cap_override: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 3),
        nullable=True,
    )
    last_sdk_session_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    daily_token_cap_in_override: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    daily_token_cap_out_override: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    citation_style: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default="vancouver",
    )


class WorkspaceViewer(Base):
    __tablename__ = "workspace_viewers"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class WorkspaceEvent(Base):
    __tablename__ = "workspace_events"
    __table_args__ = (
        Index(
            "ix_workspace_events_ws_created",
            "workspace_id",
            text("created_at DESC"),
        ),
        Index(
            "uq_workspace_events_rubric_initialized",
            "workspace_id",
            unique=True,
            postgresql_where=text("kind = 'rubric_initialized'"),
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
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ChatTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant','tool','system')",
            name="ck_chat_turns_role",
        ),
        Index("ix_chat_turns_ws_created", "workspace_id", "created_at"),
        Index("ix_chat_turns_ws_position", "workspace_id", "position"),
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
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    position: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("nextval('chat_turns_position_seq')"),
    )


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


class ChatSummary(Base):
    __tablename__ = "chat_summaries"
    __table_args__ = (
        Index(
            "ix_chat_summaries_ws_created",
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
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    summarized_through: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="CASCADE"),
        nullable=False,
    )
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (
        CheckConstraint(
            "created_by IN ('agent','user','system','reconciled','unrecoverable')",
            name="ck_artifact_versions_created_by",
        ),
        Index(
            "ix_artifact_versions_ws_created",
            "workspace_id",
            text("created_at DESC"),
        ),
        Index(
            "uq_artifact_versions_ws_sha",
            "workspace_id",
            "commit_sha",
            unique=True,
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
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    triggering_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ProposedChange(Base):
    __tablename__ = "proposed_changes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','accepted','rejected','superseded','stale')",
            name="ck_proposed_changes_status",
        ),
        CheckConstraint(
            "target_artifact IN ('draft','spec')",
            name="ck_proposed_changes_target_artifact",
        ),
        CheckConstraint(
            "kind IN ('prose_edit','citation_insert','citation_remove','citation_update')",
            name="ck_proposed_changes_kind",
        ),
        CheckConstraint(
            "origin_kind IN ('interactive_turn','coherence_session','evaluator_session','human')",
            name="ck_proposed_changes_origin_kind",
        ),
        Index("ix_proposed_changes_ws_status", "workspace_id", "status"),
        Index(
            "ix_proposed_changes_ws_target",
            "workspace_id",
            "target_artifact",
            "section_index",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    target_artifact: Mapped[str] = mapped_column(Text, nullable=False)
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    base_commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    base_section_sha: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    origin_kind: Mapped[str] = mapped_column(Text, nullable=False)
    origin_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    triggering_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    triggering_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resulting_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('figure','preliminary_data','past_grant','other')",
            name="ck_attachments_kind",
        ),
        Index("ix_attachments_ws", "workspace_id"),
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
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[int] = mapped_column(
        BigInteger,
        # Explicit RESTRICT — matches the prior default NO ACTION behavior.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # B7: image dimensions + pre-computed vision token estimate.
    # Populated at upload time from a Pillow probe (see web/files.py).
    # NULLable for two narrow reasons:
    # 1. T2 panel inserts rows before T3 backfills the upload handler;
    #    rows from that window have NULL dims until re-probed on submit.
    # 2. Pre-B7 rows from any prior B5 demo data.
    # NULL is NOT treated as zero in the cap sum — submit_user_turn
    # re-probes from disk and persists the values before counting
    # (see proposal_assist/turns/submit.py).
    width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)


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


class PhaseApproval(Base):
    __tablename__ = "phase_approvals"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('spec','plan','draft','review')",
            name="ck_phase_approvals_phase",
        ),
        Index(
            "ix_phase_approvals_ws_phase_at",
            "workspace_id",
            "phase",
            text("approved_at DESC"),
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
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    approving_user_id: Mapped[int] = mapped_column(
        BigInteger,
        # Explicit RESTRICT — matches the prior default NO ACTION behavior.
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_artifact_sha: Mapped[str] = mapped_column(Text, nullable=False)


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


class WorkspaceSetupJob(Base):
    __tablename__ = "workspace_setup_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_workspace_setup_jobs_ws"),
        CheckConstraint(
            "status IN ('queued','running','waiting_for_rubric_review','done',"
            "'failed','cancelled')",
            name="ck_workspace_setup_jobs_status",
        ),
        CheckConstraint(
            "cancelled_from IS NULL OR cancelled_from IN "
            "('queued','running','waiting_for_rubric_review')",
            name="ck_workspace_setup_jobs_cancelled_from",
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
    attempts: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Retention Slice C1 (spec 5.2.2). The non-terminal state this job was
    # cancelled FROM when its workspace was deleted, so restore can map it back
    # to a state that progresses. NULL means "not cancelled by a deletion" —
    # restore leaves those alone. Consumed (set back to NULL) by restore.
    cancelled_from: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class ProfileBootstrapJob(Base):
    __tablename__ = "profile_bootstrap_jobs"
    __table_args__ = (
        UniqueConstraint("profile_id", name="uq_profile_bootstrap_jobs_profile"),
        CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name="ck_profile_bootstrap_jobs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CLEARED on terminal completion
    seed: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pubmed_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    publications_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


class YjsUpdate(Base):
    """Append-only Yjs update log for the collaborative editor.

    One row per persisted CRDT update, keyed by (workspace_id, artifact) =
    the room. `actor_id` is the authenticated user who sent the update (for
    authorship in later slices). Ordering is by the autoincrement `id`.
    """

    __tablename__ = "yjs_updates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    artifact: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    update_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("artifact IN ('draft','spec')", name="ck_yjs_updates_artifact"),
        Index("ix_yjs_updates_room", "workspace_id", "artifact", "id"),
    )


class CollabArtifactState(Base):
    """Per-(workspace, artifact) collaborative-editing state: the feature flag
    (`enabled_at`, NULL = off) plus the checkpoint watermark (the highest
    yjs_updates.id already checkpointed to git) and last checkpoint commit."""

    __tablename__ = "collab_artifact_state"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    artifact: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checkpoint_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_checkpoint_commit_sha: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("artifact IN ('draft','spec')", name="ck_collab_artifact_state_artifact"),
    )
