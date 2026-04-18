"""Ranking v2 aggregation (gates + LLM rubric → 0-100 score)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from call_monitor.ranking.criteria import Gates, GateStatus

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResult:
    """The parsed LLM rubric output.

    `topic_argmax` is the integer 0-5 rating from the LLM.
    `eligibility` is 'pass' | 'fail' | 'unclear'.
    `exclusions` is 'pass' | 'fail'.
    """
    topic_argmax: int
    eligibility: str
    exclusions: str


@dataclass(frozen=True)
class AggregateResult:
    score: int  # 0-100
    # 'timeline'|'budget'|'trl'|'embedding'|'eligibility'|'exclusion'|None
    reason: str | None


def aggregate(
    gates: Gates,
    llm: LLMResult,
    *,
    must_include_override: bool = False,
) -> AggregateResult:
    """Combine gates + LLM into a final score per the v2 spec's aggregation table.

    `must_include_override=True` skips the timeline AND embedding short-circuits
    (spec line 282 — must_include is a force-include for *topic*: a profile
    keyword match means the user explicitly wants this call regardless of
    deadline urgency or low cosine similarity to the profile description).
    Budget/TRL FAILs still zero the score even with override.
    """
    # Rule-based gates short-circuit (timeline conditional on override).
    if not must_include_override and gates.timeline.status == GateStatus.FAIL:
        return AggregateResult(0, "timeline")
    if gates.budget.status == GateStatus.FAIL:
        return AggregateResult(0, "budget")
    if gates.trl.status == GateStatus.FAIL:
        return AggregateResult(0, "trl")
    # Embedding pre-gate: cheaper than LLM but pricier than timeline/budget/
    # TRL, so it runs LAST among the rule-based gates. Bypassed by
    # must_include for the same reason as timeline (both are topic gates).
    if not must_include_override and gates.embedding.status == GateStatus.FAIL:
        return AggregateResult(0, "embedding")

    # LLM gates short-circuit next.
    if llm.eligibility == "fail":
        return AggregateResult(0, "eligibility")

    if llm.exclusions == "fail":
        # Spec: min(10, topic_argmax * 2). Matches v1's "≤ 10" behavior.
        return AggregateResult(min(10, llm.topic_argmax * 2), "exclusion")

    # Clean path: topic_argmax × 20.
    base = llm.topic_argmax * 20
    if llm.eligibility == "unclear":
        base = min(base, 50)
    return AggregateResult(base, None)
