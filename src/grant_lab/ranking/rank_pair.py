from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from grant_lab.ranking.aggregate import (
    AggregateResult,
    LLMResult,
    aggregate,
)
from grant_lab.ranking.criteria import Gates, GateStatus, evaluate_gates
from grant_lab.ranking.ev import extract_topic_expected
from grant_lab.ranking.ollama_client import LLMError, OllamaClient
from grant_lab.ranking.prompt import (
    PROMPT_VERSION,
    Parsed,
    ParseError,
    build_messages,
    parse_response,
    rank_response_format,
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
    must_include_override: bool  # True iff the call matched a must_include term
    model: str
    prompt_version: str
    raw_response: dict  # full LLM envelope (for debugging) or {} on short-circuit
    # Cosine similarity between profile.embedding and call.embedding at the
    # time of ranking. None when either vector was missing OR the two were
    # embedded under different models (Codex H2 — meaningless cosine).
    # Persisted on every result path (clean LLM, gate FAIL, must_include
    # bypass) for downstream analysis. Default None so existing
    # constructors don't break.
    embed_similarity: float | None = None
    # G-Eval expected value of topic_fit (Phase 3). None on argmax fallback,
    # gate short-circuit, or remote (Anthropic exposes no logprobs).
    topic_fit_expected: float | None = None
    # Optional applicant-facing analysis. Populated only when the Sonnet
    # path ran with enable_analysis=True. Gemma + gate-fail paths leave
    # both fields None and the worker writes NULL to the DB columns,
    # which the UI uses to decide whether to show the analysis section.
    summary: str | None = None
    applicant_notes: list[dict] | None = None
    # Outcome-based escalation (Phase 4). `escalated` is True only when the
    # remote (Sonnet) result actually replaced the local one; on a remote
    # failure the local result survives with escalation_attempted=True and
    # the failure class in escalation_outcome.
    escalated: bool = False
    escalated_from_score: int | None = None
    escalation_attempted: bool = False
    escalation_outcome: str | None = None  # 'success'|'llm_error'|'parse_error'
    analysis_model: str | None = None
    # The local outcome qualifies for a second reading -- score >=
    # escalation_min_score, or eligibility 'fail' with topic >= 3 -- whether or
    # not a remote client was available (ranking spec D6). The worker uses it to
    # tell "owed" from "not needed".
    escalation_wanted: bool = False


def rank_pair(
    *,
    client: OllamaClient,
    profile,
    call,
    now: datetime,
    embedding_threshold: float,
    embedding_hard_fail_threshold: float | None = None,
    remote_client=None,  # AnthropicClient | None
    escalation_min_score: int = 45,
    logprobs_enabled: bool = False,
) -> RankResult:
    """Run gates → local LLM → parse → verify quotes → aggregate →
    (maybe) escalate to the remote LLM. Returns a structured RankResult.

    Rule-based-gate handling:
      - Budget FAIL or TRL FAIL → short-circuit, no LLM call.
      - Timeline FAIL (the call has closed) → short-circuit, no LLM call.
      - Embedding FAIL → short-circuit UNLESS the call matches a must_include keyword.

    Outcome-based escalation (Phase 4):
      Local Gemma ALWAYS runs first (logprobs on, enable_analysis=False).
      Then, when `remote_client` is non-None AND the local outcome warrants
      it — local score ≥ `escalation_min_score`, OR eligibility=='fail'
      with topic argmax ≥ 3 (Codex R3: legalese eligibility false-fails on
      topically strong calls) — the pair re-runs on the remote client
      (Anthropic Sonnet, enable_analysis=True) and the remote result
      REPLACES the local one (score, criteria, summary/notes, model).
      On remote failure (LLMError/ParseError) the local result is kept
      and the attempt is recorded via escalation_attempted/_outcome.
      `CallRanking.model` records which backend produced the final score;
      the UI displays that for transparency.

    On LOCAL LLM parse failure, raises ParseError (caller decides retry /
    dead-letter). On LOCAL LLM API failure, raises LLMError. Remote
    failures never propagate — the local result survives them.
    """
    # Embedding pre-gate input: cosine of the two stored vectors. Returns
    # None (→ UNKNOWN gate → LLM runs) when either vector is missing OR
    # the two were embedded under different models (Codex H2). Cosine
    # between mismatched-model vectors is meaningless; treating as
    # UNKNOWN is the safe fallback. Operator should run the backfill CLI
    # to bring everything to the current model.
    embed_sim = _safe_cosine(profile, call)

    gates = evaluate_gates(
        profile,
        call,
        now=now,
        embed_similarity=embed_sim,
        embedding_threshold=embedding_threshold,
        embedding_hard_fail_threshold=embedding_hard_fail_threshold,
    )
    must_include_match = _call_matches_must_include(profile, call)

    # Budget/TRL fail unconditionally (must_include doesn't override these).
    if gates.budget.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "budget", must_include_match, embed_similarity=embed_sim)
    if gates.trl.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "trl", must_include_match, embed_similarity=embed_sim)
    # Timeline FAIL (a closed call) short-circuits unconditionally (ranking spec §2).
    if gates.timeline.status == GateStatus.FAIL:
        return _gate_fail_result(gates, "timeline", must_include_match, embed_similarity=embed_sim)
    # Embedding fail short-circuits ONLY when must_include doesn't match
    # (the only gate must_include bypasses — it forces the topic in).
    if gates.embedding.status == GateStatus.FAIL and not must_include_match:
        return _gate_fail_result(gates, "embedding", must_include_match, embed_similarity=embed_sim)

    # Local Gemma always runs first. enable_analysis stays False on the local
    # path: Gemma's smaller capacity produces lower-quality prose; cheaper to
    # leave it as rating-only.
    #
    # logprobs (Phase 3) are OPT-IN. Requesting them makes the server emit the
    # top 10 alternatives for EVERY generated token — a 6KB response becomes
    # 1.2MB and a 21s call becomes 127s — and only ONE token's block is ever
    # read (the topic_fit rating digit). See RANKING_LOGPROBS_ENABLED for the
    # measurement that set the default. With them off, extract_topic_expected
    # returns None through its silent no-logprobs branch and aggregate() falls
    # back to argmax, which is the same path the remote Sonnet ranker uses.
    logprobs_kwargs = {"logprobs": True, "top_logprobs": 10} if logprobs_enabled else {}
    local_envelope = client.chat(
        messages=build_messages(profile=profile, call=call, enable_analysis=False),
        response_format={"type": "json_object"},
        **logprobs_kwargs,
    )
    local_content = local_envelope["choices"][0]["message"]["content"]
    local_parsed = parse_response(local_content, verification_text=_build_verification_text(call))

    # EV extraction must run BEFORE the strip — it reads the logprobs block.
    topic_expected = extract_topic_expected(local_envelope, local_parsed.topic_fit_rating)
    local_envelope = _strip_logprobs(local_envelope)

    local_llm = LLMResult(
        topic_argmax=local_parsed.topic_fit_rating,
        eligibility=local_parsed.eligibility_rating,
        exclusions=local_parsed.exclusions_rating,
        topic_expected=topic_expected,
    )
    local_agg: AggregateResult = aggregate(
        gates, local_llm, must_include_override=must_include_match
    )

    # Escalate on OUTCOME, not cosine: a promising local score, or the
    # eligibility-fail-but-strong-topic combination (Codex R3 — Gemma
    # misreads eligibility legalese; Sonnet adjudicates).
    escalation_wanted = local_agg.score >= escalation_min_score or (
        local_parsed.eligibility_rating == "fail" and local_parsed.topic_fit_rating >= 3
    )
    should_escalate = remote_client is not None and escalation_wanted

    if not should_escalate:
        return RankResult(
            score=local_agg.score,
            reason=local_agg.reason,
            gates=gates,
            parsed=local_parsed,
            must_include_override=must_include_match,
            model=client.model,
            prompt_version=PROMPT_VERSION,
            raw_response=local_envelope,
            embed_similarity=embed_sim,
            topic_fit_expected=topic_expected,
            summary=None,
            applicant_notes=None,
            analysis_model=None,
            escalated=False,
            escalated_from_score=None,
            escalation_attempted=False,
            escalation_outcome=None,
            escalation_wanted=escalation_wanted,
        )

    # Escalate: re-run on the remote client with analysis enabled. No
    # logprobs kwargs — AnthropicClient.chat() rejects them (the API
    # exposes no logprobs), so the remote topic score is argmax-only.
    try:
        remote_envelope = remote_client.chat(
            messages=build_messages(profile=profile, call=call, enable_analysis=True),
            response_format=rank_response_format(),
        )
        remote_content = remote_envelope["choices"][0]["message"]["content"]
        remote_parsed = parse_response(
            remote_content, verification_text=_build_verification_text(call)
        )
    except (LLMError, ParseError) as e:
        # ParseError first: an UnusableReplyError (refusal, truncation) is both,
        # and it recurs on the same inputs, so it counts toward gave_up.
        outcome = "parse_error" if isinstance(e, ParseError) else "llm_error"
        # NB: never use "message" as an extra key — reserved LogRecord attribute.
        _logger.warning(
            "ranking.escalation_failed",
            extra={
                "event": "ranking_escalation_failed",
                "error_class": type(e).__name__,
                "error": str(e),
                "profile_id": getattr(profile, "id", None),
                "call_id": getattr(call, "id", None),
            },
        )
        return _local_with_failed_escalation(
            local_agg,
            gates,
            local_parsed,
            must_include_match,
            client.model,
            local_envelope,
            embed_sim,
            topic_expected,
            outcome=outcome,
            escalation_wanted=escalation_wanted,
        )

    remote_llm = LLMResult(
        topic_argmax=remote_parsed.topic_fit_rating,
        eligibility=remote_parsed.eligibility_rating,
        exclusions=remote_parsed.exclusions_rating,
    )
    remote_agg = aggregate(gates, remote_llm, must_include_override=must_include_match)
    # The remote result REPLACES the local one. `model` records which
    # backend produced the final score so the UI can show "Analyzed by
    # Sonnet 4.6" and downstream audits can split evals by backend;
    # escalated_from_score keeps the local score for calibration.
    remote_model = getattr(remote_client, "model", "") or client.model
    return RankResult(
        score=remote_agg.score,
        reason=remote_agg.reason,
        gates=gates,
        parsed=remote_parsed,
        must_include_override=must_include_match,
        model=remote_model,
        prompt_version=PROMPT_VERSION,
        raw_response=remote_envelope,
        embed_similarity=embed_sim,
        topic_fit_expected=None,  # remote: no logprobs
        summary=remote_parsed.summary,
        applicant_notes=remote_parsed.applicant_notes,
        analysis_model=remote_model,
        escalated=True,
        escalated_from_score=local_agg.score,
        escalation_attempted=True,
        escalation_outcome="success",
        escalation_wanted=True,
    )


def _local_with_failed_escalation(
    local_agg: AggregateResult,
    gates: Gates,
    parsed: Parsed,
    must_include_match: bool,
    model: str,
    envelope: dict,
    embed_sim: float | None,
    topic_expected: float | None,
    *,
    outcome: str,
    escalation_wanted: bool,
) -> RankResult:
    """Local result surviving a failed remote escalation: identical to the
    no-escalation local return except attempted=True + the failure class."""
    return RankResult(
        score=local_agg.score,
        reason=local_agg.reason,
        gates=gates,
        parsed=parsed,
        must_include_override=must_include_match,
        model=model,
        prompt_version=PROMPT_VERSION,
        raw_response=envelope,
        embed_similarity=embed_sim,
        topic_fit_expected=topic_expected,
        summary=None,
        applicant_notes=None,
        analysis_model=None,
        escalated=False,
        escalated_from_score=None,
        escalation_attempted=True,
        escalation_outcome=outcome,
        escalation_wanted=escalation_wanted,
    )


def _strip_logprobs(envelope: dict) -> dict:
    """Remove the bulky per-token logprobs block from the envelope before it
    is persisted to call_rankings.raw_response (tens of KB at top_logprobs=10).
    Keep nothing of it — the expected value is already extracted and stored in
    its own column. Returns a shallow-copied envelope; the original is left
    intact for the EV extraction that ran just before."""
    choices = envelope.get("choices")
    if not isinstance(choices, list) or not choices:
        return envelope
    new_choices = []
    for ch in choices:
        if isinstance(ch, dict) and "logprobs" in ch:
            ch = {k: v for k, v in ch.items() if k != "logprobs"}
        new_choices.append(ch)
    return {**envelope, "choices": new_choices}


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
    """Case-insensitive WORD-BOUNDARY match of profile.must_include keywords
    against title + summary + full_text + keywords. Same logic as the
    digest must-include bucket (assemble.py) — both paths share the term
    normalization (min length) and word-boundary semantics defined in
    `must_include` so they cannot drift (B11 Defect 1).

    Word-boundary (not bare substring): a short term like 'ai' no longer
    force-includes a call whose text merely contains 'training'/'brain', which
    used to drag off-topic calls into the paid LLM/escalation path. Terms
    shorter than MUST_INCLUDE_MIN_LEN are ignored entirely.

    Codex Plan F regression fix: `full_text` MUST be in the haystack —
    after Plan F moved Vinnova's substantive prose out of `summary` and
    into `full_text`, a must_include term that lives only in the body
    text would otherwise be silently missed.

    A match bypasses the embedding gate only; it never rescues a closed call
    (ranking-freshness spec §2)."""
    from grant_lab.ranking.must_include import compile_term_pattern, normalize_must_terms

    structured = profile.structured or {}
    must_terms = normalize_must_terms(structured.get("must_include"))
    if not must_terms:
        return False
    full_text = getattr(call, "full_text", None)
    haystack = " ".join(
        filter(
            None,
            [
                call.title or "",
                call.summary or "",
                full_text or "",
                # `keywords` is non-nullable on the real Call ORM model; this hasattr
                # accommodates duck-typed test doubles that omit it.
                " ".join(call.keywords or []) if hasattr(call, "keywords") else "",
            ],
        )
    )
    return any(compile_term_pattern(t).search(haystack) for t in must_terms)


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
    from grant_lab.embeddings.similarity import cosine

    p_model = getattr(profile, "embedding_model", None)
    c_model = getattr(call, "embedding_model", None)
    p_vec = getattr(profile, "embedding", None)
    c_vec = getattr(call, "embedding", None)
    # A meaningful cosine requires BOTH vectors to come from the SAME model.
    # Treat as UNKNOWN (None) when:
    #   - the models are known and differ, OR
    #   - exactly one model is known while the other is NULL but both vectors
    #     exist. The asymmetric case (e.g. the profile was re-embedded under a
    #     named model but a legacy call row predates the embedding_model column)
    #     is exactly the "meaningless cosine" this guard exists to prevent, so
    #     we must NOT fall through to a naive comparison.
    both_vectors_present = p_vec is not None and c_vec is not None
    models_known_and_differ = p_model is not None and c_model is not None and p_model != c_model
    one_model_unknown = (p_model is None) != (c_model is None)
    if models_known_and_differ or (one_model_unknown and both_vectors_present):
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
    return cosine(p_vec, c_vec)
