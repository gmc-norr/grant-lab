from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from call_monitor.ranking.aggregate import (
    AggregateResult,
    LLMResult,
    aggregate,
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


@dataclass(frozen=True)
class RankResult:
    """Output of rank_pair — the worker uses these fields to write
    CallRanking + per-criterion CallRankingCriterion rows without
    re-parsing the LLM envelope."""
    score: int
    reason: str | None
    gates: Gates
    parsed: Parsed | None  # None if a gate short-circuited before LLM call
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
    embedding_threshold: float,
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
        embedding_threshold=embedding_threshold,
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
    )
    content = envelope["choices"][0]["message"]["content"]
    parsed = parse_response(content, verification_text=_build_verification_text(call))

    llm = LLMResult(
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


def _build_verification_text(call) -> str:
    """Title + summary + FULL full_text (no truncation).

    Used as the haystack for quoted_evidence substring checks in
    parse_response. Distinct from `embeddings.similarity.build_call_text`,
    which truncates full_text to 8000 chars for embedding input — we must
    not truncate here, or quotes from past that point would be falsely
    nulled.
    """
    parts = [call.title or ""]
    if call.summary:
        parts.append(call.summary)
    full_text = getattr(call, "full_text", None)
    if full_text:
        parts.append(full_text)
    return "\n".join(parts)


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
