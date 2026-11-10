"""Ranking aggregation (gates + LLM rubric → 0-100 score)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from grant_lab.ranking.criteria import Gates, GateStatus

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResult:
    """The parsed LLM rubric output.

    `topic_argmax` is the integer 0-5 rating from the LLM.
    `eligibility` is 'pass' | 'fail' | 'unclear'. `aggregate` does not read it:
    it is carried for the criterion row and the escalation rule (spec 2026-11-10).
    `exclusions` is 'pass' | 'fail'.
    """

    topic_argmax: int
    eligibility: str
    exclusions: str
    # G-Eval expected value over the rating token's logprobs (Phase 3).
    # None ⇒ extraction failed or remote/argmax path → use topic_argmax.
    topic_expected: float | None = None


@dataclass(frozen=True)
class AggregateResult:
    score: int  # 0-100
    # 'timeline'|'budget'|'trl'|'embedding'|'exclusion'|None
    reason: str | None


def aggregate(
    gates: Gates,
    llm: LLMResult,
    *,
    must_include_override: bool = False,
) -> AggregateResult:
    """Combine gates + LLM into a final score per the v2 spec's aggregation table.

    `must_include_override=True` skips the embedding short-circuit only (a
    profile keyword forces the TOPIC in). The timeline gate now fails only on a
    closed call, and nothing rescues a closed call (ranking spec §2).
    Budget/TRL FAILs still zero the score even with override.
    """
    # Rule-based gates short-circuit (timeline unconditional -- a closed call
    # has nothing to rescue it).
    if gates.timeline.status == GateStatus.FAIL:
        return AggregateResult(0, "timeline")
    if gates.budget.status == GateStatus.FAIL:
        return AggregateResult(0, "budget")
    if gates.trl.status == GateStatus.FAIL:
        return AggregateResult(0, "trl")
    # Embedding pre-gate: cheaper than LLM but pricier than timeline/budget/
    # TRL, so it runs LAST among the rule-based gates. The only gate
    # must_include bypasses: a profile keyword forces the topic in.
    if not must_include_override and gates.embedding.status == GateStatus.FAIL:
        return AggregateResult(0, "embedding")

    # Eligibility is deliberately NOT read here (spec 2026-11-10). On profile 1's
    # open calls the remote model rated it 'unclear' 45 times, 'fail' 18 times and
    # 'pass' never, so zeroing on 'fail' and capping 'unclear' at 50 flattened the
    # top of the feed and sank wanted calls. It shows as its own chip instead.

    # Effective topic: use EV when available, fall back to argmax.
    topic = llm.topic_expected if llm.topic_expected is not None else float(llm.topic_argmax)

    if llm.exclusions == "fail":
        # Spec: min(10, topic * 2). Matches v1's "≤ 10" behavior.
        return AggregateResult(min(10, round(topic * 2)), "exclusion")

    return AggregateResult(round(topic * 20), None)
