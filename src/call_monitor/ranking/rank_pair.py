from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import replace as _replace

from call_monitor.db.models import Call, Profile
from call_monitor.ranking.ollama_client import LLMError, OllamaClient
from call_monitor.ranking.prompt import PROMPT_VERSION, build_messages

_logger = logging.getLogger(__name__)
_RATIONALE_MAX = 1000


@dataclass(slots=True)
class RankResult:
    score: int
    rationale: str
    model: str
    prompt_version: str
    raw_response: dict


def _extract_assistant_content(envelope: dict) -> str:
    try:
        message = envelope["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Ollama envelope missing choices[0].message: {e}") from e
    # Gemma may emit tool_calls instead of textual content under JSON mode;
    # in that case content is None or absent. Surface as LLMError so the
    # worker dead-letters cleanly instead of TypeError-ing on json.loads(None).
    content = message.get("content")
    if not content:
        raise LLMError(
            "Ollama returned empty assistant content "
            "(model may have used tool_calls instead of plain content)"
        )
    # Future-proofing: a multimodal provider may return content as a list
    # of parts (e.g., [{"type":"text","text":"..."}]). Guard now so a
    # provider swap fails at LLMError (retried + eventually dead-lettered)
    # rather than TypeError on json.loads([...]).
    if not isinstance(content, str):
        raise LLMError(
            f"Ollama returned non-string content: {type(content).__name__}"
        )
    return content


def _parse_score_rationale(content: str) -> tuple[int, str]:
    try:
        parsed = json.loads(content)
    except ValueError as e:
        raise LLMError(f"assistant content is invalid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise LLMError(f"assistant content is not a JSON object: {type(parsed).__name__}")
    if "score" not in parsed:
        raise LLMError("assistant content missing required key 'score'")
    score = parsed["score"]
    # `isinstance(True, int)` is True in Python (bool subclasses int), so
    # a model returning {"score": true} would otherwise coerce to 1 and
    # silently persist a nonsense ranking. Reject booleans explicitly so
    # malformed LLM output is retried / dead-lettered instead.
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise LLMError(f"score must be an integer in 0–100; got {score!r}")
    rationale = str(parsed.get("rationale") or "")
    if len(rationale) > _RATIONALE_MAX:
        rationale = rationale[:_RATIONALE_MAX]
    return score, rationale


def rank_pair(
    *, client: OllamaClient, profile: Profile, call: Call
) -> RankResult:
    """Score one (call, profile) pair via the LLM.

    Raises LLMError on any HTTP / parse / validation failure; the queue
    worker turns those into job retries (with backoff) and dead-letters
    after the attempt cap.
    """
    messages = build_messages(profile, call)
    envelope = client.chat(messages=messages)
    content = _extract_assistant_content(envelope)
    score, rationale = _parse_score_rationale(content)
    model = envelope.get("model") or client.model  # fallback for sparse responses
    return RankResult(
        score=score,
        rationale=rationale,
        model=model,
        prompt_version=PROMPT_VERSION,
        raw_response=envelope,
    )


# ---------------------------------------------------------------------------
# v2 — gates + G-Eval + criterion-rubric orchestration
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402 — stdlib, no circular risk

from call_monitor.ranking.aggregate import (  # noqa: E402
    AggregateResult,
    LLMResult,
    aggregate,
    logprob_expected,
)
from call_monitor.ranking.criteria import Gates, GateStatus, evaluate_gates  # noqa: E402
from call_monitor.ranking.prompt import (  # noqa: E402
    PROMPT_VERSION_V2,
    ParsedV2,
    build_messages_v2,
    parse_v2_response,
)

_QUOTED_EVIDENCE_MAX = 200


@dataclass(frozen=True)
class RankResultV2:
    """Output of rank_pair_v2 — richer than v1's RankResult so the worker
    can write criteria rows without re-parsing."""
    score: int
    reason: str | None
    gates: Gates
    parsed: ParsedV2 | None  # None if a gate short-circuited before LLM call
    topic_expected: float | None  # G-Eval expected; None if logprobs unavailable
    logprob_fallback: bool         # True iff we fell back to argmax
    must_include_override: bool    # True iff the call matched a must_include term
    model: str
    prompt_version: str
    raw_response: dict             # full LLM envelope (for debugging) or {} on short-circuit


def rank_pair_v2(
    *,
    client: OllamaClient,
    profile,
    call,
    now: datetime,
) -> RankResultV2:
    """Run gates → LLM → parse → verify quotes → aggregate. Returns a
    structured RankResultV2.

    Rule-based-gate handling:
      - Budget FAIL or TRL FAIL → short-circuit, no LLM call.
      - Timeline FAIL → short-circuit UNLESS the call matches one of the
        profile's must_include keywords, in which case timeline is bypassed
        (spec line 282) and the LLM is consulted. The bypass is propagated
        as `must_include_override=True` to aggregate() and through the
        result so the worker's criteria-row writer doesn't bail at the
        timeline-fail row either.

    On LLM parse failure, raises ParseV2Error (caller decides retry /
    dead-letter). On Ollama failure, raises LLMError.
    """
    gates = evaluate_gates(profile, call, now=now)
    must_include_match = _call_matches_must_include(profile, call)

    # Budget/TRL fail unconditionally (must_include doesn't override these).
    if gates.budget.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "budget", must_include_match)
    if gates.trl.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "trl", must_include_match)
    # Timeline fail short-circuits ONLY when must_include doesn't match.
    if gates.timeline.status == GateStatus.FAIL and not must_include_match:
        return _gate_fail_result(gates, "timeline", must_include_match)

    messages = build_messages_v2(profile=profile, call=call)
    envelope = client.chat(
        messages=messages,
        response_format={"type": "json_object"},
        logprobs=True,
        top_logprobs=6,
    )
    content = envelope["choices"][0]["message"]["content"]
    parsed = parse_v2_response(content)
    parsed = _verify_quoted_evidence(parsed, call)
    content_lp = (envelope["choices"][0].get("logprobs") or {}).get("content")
    expected = logprob_expected(content_lp)
    logprob_fallback = expected is None
    if expected is None:
        expected = float(parsed.topic_fit_rating)

    llm = LLMResult(
        topic_expected=expected,
        topic_argmax=parsed.topic_fit_rating,
        eligibility=parsed.eligibility_rating,
        exclusions=parsed.exclusions_rating,
    )
    agg: AggregateResult = aggregate(gates, llm, must_include_override=must_include_match)
    return RankResultV2(
        score=agg.score,
        reason=agg.reason,
        gates=gates,
        parsed=parsed,
        topic_expected=expected,
        logprob_fallback=logprob_fallback,
        must_include_override=must_include_match,
        model=client.model,
        prompt_version=PROMPT_VERSION_V2,
        raw_response=envelope,
    )


def _gate_fail_result(gates: Gates, reason: str, must_include_match: bool) -> RankResultV2:
    return RankResultV2(
        score=0,
        reason=reason,
        gates=gates,
        parsed=None,
        topic_expected=None,
        logprob_fallback=False,
        must_include_override=must_include_match,
        model="",
        prompt_version=PROMPT_VERSION_V2,
        raw_response={},
    )


def _call_matches_must_include(profile, call) -> bool:
    """Case-insensitive substring match of profile.must_include keywords
    against title + summary + keywords. Same logic as the digest
    must-include bucket (assemble.py).

    Spec line 282: must-include calls bypass the timeline gate (only)."""
    structured = profile.structured or {}
    must_terms = [
        t.strip().lower()
        for t in (structured.get("must_include") or [])
        if t and t.strip()
    ]
    if not must_terms:
        return False
    haystack = " ".join(filter(None, [
        (call.title or "").lower(),
        (call.summary or "").lower(),
        " ".join(call.keywords or []).lower() if hasattr(call, "keywords") else "",
    ]))
    return any(t in haystack for t in must_terms)


def _verify_quoted_evidence(parsed: ParsedV2, call) -> ParsedV2:
    """Defense against ungrounded LLM rationales: null out any quoted_evidence
    field whose case-insensitive content is not actually in the call text.
    Truncate over-long quotes to _QUOTED_EVIDENCE_MAX chars.

    We never fail the ranking on a bogus quote — quote-checking is a defense,
    not a gate. The LLM's rating still stands; only the quote display is
    sanitized."""
    haystack = " ".join(filter(None, [
        (call.title or "").lower(),
        (call.summary or "").lower(),
        (getattr(call, "full_text", "") or "").lower(),
    ]))

    def _verify_one(quote: str | None) -> str | None:
        if not quote:
            return None
        truncated = quote[:_QUOTED_EVIDENCE_MAX]
        if truncated.lower() in haystack:
            return truncated
        return None  # bogus quote — discarded silently

    return _replace(
        parsed,
        topic_fit_quoted=_verify_one(parsed.topic_fit_quoted),
        eligibility_quoted=_verify_one(parsed.eligibility_quoted),
        exclusions_quoted=_verify_one(parsed.exclusions_quoted),
    )
