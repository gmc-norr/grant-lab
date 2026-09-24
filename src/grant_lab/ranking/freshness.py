"""Which (call, profile) pairs need ranking work.

Ranking spec: docs/superpowers/specs/2026-09-24-ranking-freshness-design.md.
A pair is ranked once per INPUT REVISION -- profile, call and scoring settings,
each fingerprinted -- and that ranking is driven to completion, including its
second reading (escalation_state). This module owns the rule for "current",
the open-call window, and dead-letter suppression keyed on inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Text, func, literal, or_, select, text
from sqlalchemy.orm import Session

from grant_lab.db.models import Call, Profile
from grant_lab.profiles.fingerprint import (
    LEGACY_SENTINEL,
    RANKED_RAW_KEYS,
    _digest,
    call_rank_fingerprint,
    rank_fingerprint,
)
from grant_lab.ranking.prompt import PROMPT_VERSION

_logger = logging.getLogger(__name__)

# Bump when a change to ranking/criteria.py, ranking/aggregate.py or
# prompt.parse_response changes what a stored ranking would say for the same
# inputs. No gate can see a semantic code change, so this is a convention
# (spec D3); the admin "Re-rank everything" button is the fallback. A bump
# re-ranks every open pair once, on the next ingest or 04:00 reconcile.
SCORING_VERSION = "1"

# How long a terminally-failed job suppresses a retry of the SAME inputs (spec D7).
DEAD_LETTER_SUPPRESS_DAYS = 7

# parse_error escalation failures allowed per input revision (spec D6).
ESCALATION_MAX_PARSE_FAILURES = 3


@dataclass(frozen=True)
class ScoringConfig:
    """The scorer settings that can change a stored result (spec D3).

    Build it from the SAME values drain_queue receives -- for_drain() -- never
    from Settings on one side and parameters on the other: tests run the worker
    with non-default values, and a mismatch makes every pair look stale forever.
    The remote model id is deliberately absent (spec D3, rev 3).
    """

    local_model: str | None
    embedding_threshold: float
    embedding_hard_fail_threshold: float
    escalation_min_score: int
    logprobs_enabled: bool

    @classmethod
    def for_drain(
        cls,
        *,
        client,
        embedding_threshold,
        embedding_hard_fail_threshold,
        escalation_min_score,
        logprobs_enabled,
    ) -> ScoringConfig:
        # criteria.evaluate_gates collapses a None hard-fail to the pregate
        # threshold; normalise here so both spellings hash the same.
        hard = (
            embedding_threshold
            if embedding_hard_fail_threshold is None
            else embedding_hard_fail_threshold
        )
        return cls(
            local_model=getattr(client, "model", None),
            embedding_threshold=float(embedding_threshold),
            embedding_hard_fail_threshold=float(hard),
            escalation_min_score=int(escalation_min_score),
            logprobs_enabled=bool(logprobs_enabled),
        )

    @classmethod
    def from_settings(cls, settings) -> ScoringConfig:
        """For the ingest CLI only, which has no scheduler. Mirrors the one-to-one
        mapping main.py hands build_scheduler."""
        return cls.for_drain(
            client=_Named(settings.gemma_model),
            embedding_threshold=settings.embedding_pregate_threshold,
            embedding_hard_fail_threshold=settings.embedding_hard_fail_threshold,
            escalation_min_score=settings.ranking_escalation_min_score,
            logprobs_enabled=settings.ranking_logprobs_enabled,
        )

    def fingerprint(self) -> str:
        return _digest(
            {
                "prompt_version": PROMPT_VERSION,
                "scoring_version": SCORING_VERSION,
                "local_model": self.local_model,
                "embedding_threshold": self.embedding_threshold,
                "embedding_hard_fail_threshold": self.embedding_hard_fail_threshold,
                "escalation_min_score": self.escalation_min_score,
                "logprobs_enabled": self.logprobs_enabled,
            }
        )


@dataclass(frozen=True)
class _Named:
    model: str


@dataclass(frozen=True)
class FreshnessContext:
    """What the enqueue paths need beyond the database (spec D4).

    `remote_enabled` decides whether a `pending` escalation makes a pair due.
    It is not hashed: adding or removing an API key must not re-rank anything.
    """

    scoring: ScoringConfig
    remote_enabled: bool

    @classmethod
    def from_settings(cls, settings) -> FreshnessContext:
        return cls(
            scoring=ScoringConfig.from_settings(settings),
            remote_enabled=bool(settings.anthropic_api_key),
        )


def next_escalation_state(
    *,
    wanted: bool,
    escalated: bool,
    remote_configured: bool,
    remote_withheld: bool,
    outcome: str | None,
    prev_failures: int,
) -> tuple[str | None, int]:
    """The (escalation_state, escalation_failures) a ranking is stored with (spec D6).

    `prev_failures` is the existing row's count when the three stamps match
    (same input revision), else 0 -- the caller decides that.
    """
    if not wanted or escalated or not remote_configured:
        return None, 0
    if outcome == "parse_error":
        failures = prev_failures + 1
        return ("gave_up" if failures >= ESCALATION_MAX_PARSE_FAILURES else "pending"), failures
    # Withheld by the daily cap, llm_error (the SERVICE failed), or no attempt
    # recorded: owed, and not this pair's fault.
    return "pending", prev_failures


def _profile_rows(session: Session):
    """Active profiles, column-limited: rank_fingerprint never reads the vector.
    tests/test_ranking_queue_row_parity.py pins that these rows hash like
    loaded Profiles."""
    return session.execute(
        select(
            Profile.id,
            Profile.name,
            Profile.kind,
            Profile.description,
            Profile.structured,
            Profile.budget_currency,
            Profile.embedding_fingerprint,
            Profile.embedding_model,
        ).where(Profile.is_active.is_(True))
    ).all()


def _open_calls():
    return or_(Call.deadline.is_(None), Call.deadline > func.now())


def _call_rows(session: Session, call_ids: list[int] | None):
    """Open calls, column-limited: never Call.embedding, and never raw whole
    (13 MB across the open feed, spec D4). `raw` here is the RANKED_RAW_KEYS
    projection, so call_rank_fingerprint reads it exactly as it reads a loaded
    Call's raw. G27 pins that."""
    projection = func.jsonb_build_object(
        *[part for k in RANKED_RAW_KEYS for part in (literal(k, type_=Text), Call.raw[k])]
    ).label("raw")
    stmt = select(
        Call.id,
        Call.url,
        Call.title,
        Call.summary,
        Call.full_text,
        Call.deadline,
        Call.budget_min,
        Call.budget_max,
        Call.currency,
        Call.programme,
        Call.keywords,
        Call.published_at,
        Call.embedding_fingerprint,
        Call.embedding_model,
        projection,
    ).where(_open_calls())
    if call_ids is not None:
        stmt = stmt.where(Call.id.in_(call_ids))
    return session.execute(stmt).all()


def enqueue_stale_pairs(
    session: Session, *, ctx: FreshnessContext, call_ids: list[int] | None = None
) -> int:
    """Enqueue every stale, unsuppressed (open call x active profile) pair.

    `call_ids=None` means every open call (the 04:00 reconcile); a list limits
    the candidates (ingest). Returns the number of jobs inserted. Does not commit.

    Current (spec D4) = the profile arm (fingerprint, or the bounded 'legacy'
    arm) AND call_fingerprint AND scoring_fingerprint match, AND no escalation
    is owed while a remote client can pay it. Suppressed (spec D7) = a failed
    job in the last DEAD_LETTER_SUPPRESS_DAYS on the SAME inputs, or with NULL
    stamps (pre-0081 rows keep the old pair-wide rule).
    """
    if call_ids is not None and not call_ids:
        return 0
    profiles = _profile_rows(session)
    if not profiles:
        # An empty VALUES list is a syntax error.
        return 0
    calls = _call_rows(session, call_ids)
    if not calls:
        return 0

    values_sql = ", ".join(f"(:pid_{i}, :pfp_{i})" for i in range(len(profiles)))
    params: dict[str, object] = {
        "call_ids": [c.id for c in calls],
        "call_fps": [call_rank_fingerprint(c) for c in calls],
        "scoring_fp": ctx.scoring.fingerprint(),
        "remote_enabled": ctx.remote_enabled,
        "legacy": LEGACY_SENTINEL,
        "dead_letter_cutoff": datetime.now(UTC) - timedelta(days=DEAD_LETTER_SUPPRESS_DAYS),
    }
    for i, prof in enumerate(profiles):
        params[f"pid_{i}"] = prof.id
        params[f"pfp_{i}"] = rank_fingerprint(prof)

    result = session.execute(
        text(
            f"""
            INSERT INTO ranking_jobs (call_id, profile_id, status)
            SELECT wc.call_id, p.id, 'pending'
            FROM unnest(CAST(:call_ids AS bigint[]), CAST(:call_fps AS text[]))
                 AS wc(call_id, fingerprint)
            CROSS JOIN (VALUES {values_sql}) AS want(profile_id, fingerprint)
            JOIN profiles p ON p.id = want.profile_id
            WHERE p.is_active = true
              AND NOT EXISTS (
                SELECT 1 FROM call_rankings cr
                WHERE cr.call_id = wc.call_id AND cr.profile_id = p.id
                  AND ((cr.profile_fingerprint = :legacy AND cr.ranked_at >= p.updated_at)
                       OR cr.profile_fingerprint = want.fingerprint)
                  AND cr.call_fingerprint = wc.fingerprint
                  AND cr.scoring_fingerprint = :scoring_fp
                  -- IS DISTINCT FROM, never `NOT (state = 'pending' AND ...)`:
                  -- a NULL state (the normal case) would make this whole test
                  -- NULL and every fresh pair stale (spec D4, "the NULL trap").
                  AND (cr.escalation_state IS DISTINCT FROM 'pending'
                       OR NOT CAST(:remote_enabled AS boolean))
              )
              AND NOT EXISTS (
                SELECT 1 FROM ranking_jobs rj
                WHERE rj.call_id = wc.call_id AND rj.profile_id = p.id
                  AND rj.status = 'failed'
                  AND rj.updated_at > :dead_letter_cutoff
                  AND (rj.profile_fingerprint IS NULL
                       OR (rj.profile_fingerprint = want.fingerprint
                           AND rj.call_fingerprint = wc.fingerprint
                           AND rj.scoring_fingerprint = :scoring_fp))
              )
            ON CONFLICT (call_id, profile_id)
              WHERE status IN ('pending', 'running') DO NOTHING
            """
        ),
        params,
    )
    return result.rowcount or 0
