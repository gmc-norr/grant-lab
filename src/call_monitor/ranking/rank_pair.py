from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from dataclasses import replace as _replace
from datetime import datetime

from call_monitor.ranking.aggregate import (
    AggregateResult,
    LLMResult,
    aggregate,
    logprob_expected,
)
from call_monitor.ranking.criteria import Gates, GateStatus, evaluate_gates
from call_monitor.ranking.ollama_client import OllamaClient
from call_monitor.ranking.prompt import (
    PROMPT_VERSION,
    Parsed,
    build_messages,
    parse_response,
)

_logger = logging.getLogger(__name__)
_QUOTED_EVIDENCE_MAX = 200


@functools.lru_cache(maxsize=1)
def _get_threshold() -> float:
    """Read the embedding pre-gate threshold from Settings, cached.

    Settings() reads env at construction; Settings is not a singleton in
    this codebase (see main.py — it's instantiated lazily). Without this
    cache we'd build a fresh Settings on every rank_pair call, which is
    wasteful in a 60-s-tick worker. The cache key is empty so env
    overrides take effect on process restart (or after an explicit
    `_get_threshold.cache_clear()`, used by env-override tests).

    Settings has several REQUIRED env vars (DATABASE_URL etc.) and unit
    tests for rank_pair don't set them — falling back to the module
    default in that case keeps the unit tests pure (no env needed) while
    still letting production / e2e tests pick up EMBEDDING_PREGATE_THRESHOLD.
    """
    from call_monitor.ranking.criteria import EMBEDDING_PREGATE_THRESHOLD

    try:
        from call_monitor.config import Settings
        return Settings().embedding_pregate_threshold
    except Exception as e:  # noqa: BLE001 — fall back gracefully on any Settings load failure
        # WARN (not exception) — no stack trace needed; this is an expected
        # path when Settings env vars aren't set (unit tests). In production
        # we want operator visibility if Settings ever fails to load.
        _logger.warning(
            "ranking.embedding_threshold_fallback",
            extra={
                "event": "embedding_threshold_fallback",
                "reason": str(e),
                "fallback_value": EMBEDDING_PREGATE_THRESHOLD,
            },
        )
        return EMBEDDING_PREGATE_THRESHOLD


@dataclass(frozen=True)
class RankResult:
    """Output of rank_pair — the worker uses these fields to write
    CallRanking + per-criterion CallRankingCriterion rows without
    re-parsing the LLM envelope."""
    score: int
    reason: str | None
    gates: Gates
    parsed: Parsed | None  # None if a gate short-circuited before LLM call
    topic_expected: float | None  # G-Eval expected; None if logprobs unavailable
    logprob_fallback: bool         # True iff we fell back to argmax
    must_include_override: bool    # True iff the call matched a must_include term
    model: str
    prompt_version: str
    raw_response: dict             # full LLM envelope (for debugging) or {} on short-circuit
    # Cosine similarity between profile.embedding and call.embedding at the
    # time of ranking. None when either vector was missing OR the two were
    # embedded under different models (Codex H2 — meaningless cosine).
    # Persisted on every result path (clean LLM, gate FAIL, must_include
    # bypass) for downstream analysis. Default None so existing
    # constructors don't break.
    embed_similarity: float | None = None


def rank_pair(
    *,
    client: OllamaClient,
    profile,
    call,
    now: datetime,
) -> RankResult:
    """Run gates → LLM → parse → verify quotes → aggregate. Returns a
    structured RankResult.

    Rule-based-gate handling:
      - Budget FAIL or TRL FAIL → short-circuit, no LLM call.
      - Timeline FAIL → short-circuit UNLESS the call matches one of the
        profile's must_include keywords, in which case timeline is bypassed
        (spec line 282) and the LLM is consulted. The bypass is propagated
        as `must_include_override=True` to aggregate() and through the
        result so the worker's criteria-row writer doesn't bail at the
        timeline-fail row either.

    On LLM parse failure, raises ParseError (caller decides retry /
    dead-letter). On Ollama failure, raises LLMError.
    """
    # Embedding pre-gate input: cosine of the two stored vectors. Returns
    # None (→ UNKNOWN gate → LLM runs) when either vector is missing OR
    # the two were embedded under different models (Codex H2). Cosine
    # between mismatched-model vectors is meaningless; treating as
    # UNKNOWN is the safe fallback. Operator should run the backfill CLI
    # to bring everything to the current model.
    embed_sim = _safe_cosine(profile, call)

    gates = evaluate_gates(
        profile, call, now=now,
        embed_similarity=embed_sim,
        embedding_threshold=_get_threshold(),
    )
    must_include_match = _call_matches_must_include(profile, call)

    # Budget/TRL fail unconditionally (must_include doesn't override these).
    if gates.budget.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "budget", must_include_match,
                                 embed_similarity=embed_sim)
    if gates.trl.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "trl", must_include_match,
                                 embed_similarity=embed_sim)
    # Timeline fail short-circuits ONLY when must_include doesn't match.
    if gates.timeline.status == GateStatus.FAIL and not must_include_match:
        return _gate_fail_result(gates, "timeline", must_include_match,
                                 embed_similarity=embed_sim)
    # Embedding fail short-circuits ONLY when must_include doesn't match
    # (mirrors the timeline bypass — must_include is a topic-force-include).
    if gates.embedding.status == GateStatus.FAIL and not must_include_match:
        return _gate_fail_result(gates, "embedding", must_include_match,
                                 embed_similarity=embed_sim)

    messages = build_messages(profile=profile, call=call)
    envelope = client.chat(
        messages=messages,
        response_format={"type": "json_object"},
        logprobs=True,
        top_logprobs=6,
    )
    content = envelope["choices"][0]["message"]["content"]
    parsed = parse_response(content)
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
    return RankResult(
        score=agg.score,
        reason=agg.reason,
        gates=gates,
        parsed=parsed,
        topic_expected=expected,
        logprob_fallback=logprob_fallback,
        must_include_override=must_include_match,
        model=client.model,
        prompt_version=PROMPT_VERSION,
        raw_response=envelope,
        embed_similarity=embed_sim,
    )


def _gate_fail_result(
    gates: Gates,
    reason: str,
    must_include_match: bool,
    *,
    embed_similarity: float | None = None,
) -> RankResult:
    return RankResult(
        score=0,
        reason=reason,
        gates=gates,
        parsed=None,
        topic_expected=None,
        logprob_fallback=False,
        must_include_override=must_include_match,
        model="",
        prompt_version=PROMPT_VERSION,
        raw_response={},
        embed_similarity=embed_similarity,
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
        # `keywords` is non-nullable on the real Call ORM model; this hasattr
        # accommodates duck-typed test doubles that omit it.
        " ".join(call.keywords or []).lower() if hasattr(call, "keywords") else "",
    ]))
    return any(t in haystack for t in must_terms)


def _verify_quoted_evidence(parsed: Parsed, call) -> Parsed:
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
        # Whitespace-only quotes match almost any haystack (every haystack
        # contains whitespace) but are useless to display. Reject them.
        if not truncated.strip():
            return None
        if truncated.lower() in haystack:
            return truncated
        return None  # bogus quote — discarded silently

    return _replace(
        parsed,
        topic_fit_quoted=_verify_one(parsed.topic_fit_quoted),
        eligibility_quoted=_verify_one(parsed.eligibility_quoted),
        exclusions_quoted=_verify_one(parsed.exclusions_quoted),
    )


def _safe_cosine(profile, call) -> float | None:
    """Compute cosine but return None on missing vectors OR model mismatch.

    A model mismatch (one vector under bge-m3, the other under e.g.
    bge-m3-v2) yields meaningless cosine — treat as UNKNOWN. Logged at
    WARNING level so an operator notices and runs the backfill CLI.
    """
    from call_monitor.embeddings.similarity import cosine

    p_model = getattr(profile, "embedding_model", None)
    c_model = getattr(call, "embedding_model", None)
    if p_model is not None and c_model is not None and p_model != c_model:
        _logger.warning(
            "ranking.embedding_model_mismatch",
            extra={
                "event": "embedding_model_mismatch",
                "profile_id": getattr(profile, "id", None),
                "call_id": getattr(call, "id", None),
                "profile_model": p_model,
                "call_model": c_model,
            },
        )
        return None
    return cosine(
        getattr(profile, "embedding", None),
        getattr(call, "embedding", None),
    )
