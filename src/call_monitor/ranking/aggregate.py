"""G-Eval logprob weighting + ranking v2 aggregation."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from call_monitor.ranking.criteria import Gates, GateStatus

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMResult:
    """The parsed LLM rubric output, after logprob expansion.

    `topic_expected` is the G-Eval expected value (float in [0, 5]).
    `topic_argmax` is the argmax integer rating used as fallback.
    `eligibility` is 'pass' | 'fail' | 'unclear'.
    `exclusions` is 'pass' | 'fail'.
    """
    topic_expected: float
    topic_argmax: int
    eligibility: str
    exclusions: str


@dataclass(frozen=True)
class AggregateResult:
    score: int  # 0-100
    reason: str | None  # 'timeline'|'budget'|'trl'|'eligibility'|'exclusion'|None


def aggregate(
    gates: Gates,
    llm: LLMResult,
    *,
    must_include_override: bool = False,
) -> AggregateResult:
    """Combine gates + LLM into a final score per the v2 spec's aggregation table.

    `must_include_override=True` skips ONLY the timeline-FAIL short-circuit
    (spec line 282 — must_include is a force-include for topic, not for
    budget/TRL). Budget/TRL FAILs still zero the score even with override.
    """
    # Rule-based gates short-circuit (timeline conditional on override).
    if not must_include_override and gates.timeline.status == GateStatus.FAIL:
        return AggregateResult(0, "timeline")
    if gates.budget.status == GateStatus.FAIL:
        return AggregateResult(0, "budget")
    if gates.trl.status == GateStatus.FAIL:
        return AggregateResult(0, "trl")

    # LLM gates short-circuit next.
    if llm.eligibility == "fail":
        return AggregateResult(0, "eligibility")

    if llm.exclusions == "fail":
        # Spec: min(10, topic_expected * 2). Matches v1's "≤ 10" behavior.
        return AggregateResult(min(10, int(llm.topic_expected * 2)), "exclusion")

    # Clean path: topic-expected × 20.
    base = int(round(llm.topic_expected * 20))
    if llm.eligibility == "unclear":
        base = min(base, 50)
    return AggregateResult(base, None)


def logprob_expected(content_logprobs: list[dict] | None) -> float | None:
    """G-Eval expected value from top-k logprobs at the rating-token position.

    `content_logprobs` is the OpenAI-shape `choices[0].logprobs.content` list,
    one entry per OUTPUT TOKEN. The LLM emits a JSON object like
    `{"topic_fit": {"rating": 4, ...}, ...}`, so the rating token is buried
    several tokens deep — past `{`, `"topic_fit"`, `:`, ` {`, `"rating"`, etc.

    We walk the content stream looking for the first token whose stripped
    value is a single-digit integer in [0, 5]. That token is unambiguously
    the topic_fit.rating (the only int 0-5 the v2 schema produces). At
    that position, use its `top_logprobs` to compute the expected value.

    Returns the expected value (float in [0, 5]) or None if no usable
    rating-position is found. Caller falls back to argmax on None.
    """
    if not content_logprobs:
        return None

    rating_top: list[dict] | None = None
    for entry in content_logprobs:
        if not isinstance(entry, dict):
            continue
        tok = entry.get("token")
        if not isinstance(tok, str):
            continue
        stripped = tok.strip()
        if stripped.isdigit() and 0 <= int(stripped) <= 5:
            rating_top = entry.get("top_logprobs")
            break

    if not rating_top:
        _logger.warning(
            "ranking.logprob_rating_token_not_found",
            extra={
                "event": "logprob_rating_token_not_found",
                "stream_tokens": [
                    e.get("token") for e in content_logprobs if isinstance(e, dict)
                ][:30],
            },
        )
        return None

    pairs: list[tuple[int, float]] = []
    for entry in rating_top:
        if not isinstance(entry, dict):
            continue
        tok = entry.get("token")
        lp = entry.get("logprob")
        if not isinstance(tok, str) or not isinstance(lp, (int, float)):
            continue
        stripped = tok.strip()
        if not stripped.isdigit():
            continue
        n = int(stripped)
        if not 0 <= n <= 5:
            continue
        pairs.append((n, float(lp)))

    if not pairs:
        return None

    # Softmax-normalize, then weighted sum.
    max_lp = max(lp for _, lp in pairs)
    exps = [(n, math.exp(lp - max_lp)) for n, lp in pairs]
    z = sum(x for _, x in exps)
    return sum(n * (x / z) for n, x in exps)
