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

from grant_lab.profiles.fingerprint import _digest
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
