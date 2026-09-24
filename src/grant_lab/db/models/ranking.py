"""Scoring a (call, profile) pair, the jobs that do it, and the digest run."""

from __future__ import annotations

import enum
from datetime import datetime

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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from grant_lab.db.base import Base


class RankingJobStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    DONE = "done"


class CallDecision(Base):
    """A user's triage decision about one call, for one profile.

    Supersedes the table migration 0071 drops. (Its NAME is deliberately not
    written here: the "no consumer remains" gate greps src/ for it, and a
    docstring mention makes that gate fail on correct code. The old name lives
    in the migration, where the grep never looks.) The old key was
    (user_id, call_id): a dismissal was GLOBAL, so a researcher matching against
    two profiles who dismissed a call for one silently dismissed it for both. A
    decision is per profile because that is the entire point of the table.

    THERE IS NO 'undecided' STATE. Undecided is the ABSENCE of a row, so the
    inbox query is a NOT EXISTS and a newly ingested call arrives undecided with
    no backfill. Adding an 'undecided' value here would make every ingest write
    a row per (user, profile, call) and break that.
    """

    __tablename__ = "call_decisions"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "profile_id", "call_id", name="uq_call_decisions_user_profile_call"
        ),
        CheckConstraint("state IN ('saved','hidden')", name="ck_call_decisions_state"),
        # The inbox query is NOT EXISTS (user_id, profile_id, call_id) -- covered
        # by the unique index above. This second index serves the Saved and
        # Hidden LISTS, which filter (user_id, profile_id, state) and order by
        # decided_at DESC (Phase 2 decision D12).
        Index("ix_call_decisions_user_profile_state", "user_id", "profile_id", "state"),
        # Both FKs CASCADE; without these, deleting a Call or a Profile
        # sequentially scans the largest per-user table (D20).
        Index("ix_call_decisions_call_id", "call_id"),
        Index("ix_call_decisions_profile_id", "profile_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False
    )
    call_id: Mapped[int] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), nullable=False)
    # 'saved' | 'hidden'. Enforced by ck_call_decisions_state, not a native enum:
    # String + CHECK is what profiles.visibility uses (the closest precedent --
    # ranking_remote_attempts.outcome is Text + CHECK), and it does not force an
    # ALTER TYPE when a later release adds a state.
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    # The user's own reasoning. NOTHING WRITES THIS IN RELEASE 1 -- the column is
    # created now so the note UI is a frontend change later, not a migration.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Who pressed the key. EQUAL TO user_id FOR EVERY ROW AN ENDPOINT CAN WRITE
    # IN RELEASE 1: the key is per user, so a colleague's decision on a shared
    # profile is their own row, not a rewrite of this one. Kept because spec 4.1
    # asks for it, and so that a per-PROFILE key later does not need a migration
    # for the column. NULLABLE + ON DELETE SET NULL: the backfill sets it to
    # user_id, so RESTRICT would block deleting an ordinary account even though
    # user_id itself CASCADEs. The decision still dies with its OWNER.
    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
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
        CheckConstraint(
            "escalation_state IN ('pending','gave_up')",
            name="ck_call_rankings_escalation_state",
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
    # Which profile this ranking describes: "v2:" + sha256 of the canonical
    # rank payload, captured BEFORE rank_pair ran. 'legacy' marks a row that
    # predates migration 0075. NULL after 0075 means a bug, and reconcile
    # treats it as stale so the bug is loud rather than silent.
    profile_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which call content this ranking describes: call_rank_fingerprint(), taken
    # BEFORE rank_pair ran. NULL = stale (migration 0081 leaves every old row NULL).
    call_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which scorer settings produced it: ScoringConfig.fingerprint() (spec D3).
    scoring_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Whether its second reading is still owed (spec D6). NULL = nothing outstanding.
    escalation_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    # parse_error escalation failures within ONE input revision (spec D6).
    escalation_failures: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), default=0
    )


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
    # The inputs a terminally-failed job failed ON (spec D7). Written by
    # mark_failed(stamps=...); NULL on rows from before migration 0081.
    profile_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    scoring_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
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
