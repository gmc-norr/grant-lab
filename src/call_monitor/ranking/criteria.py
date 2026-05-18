"""Rule-based gates for ranking.

Four deterministic checks (timeline, budget, TRL, embedding) evaluated
in Python before any LLM call. A FAIL on any gate short-circuits the
LLM call and yields aggregate_score=0 with the gate name as
aggregate_reason.

Gates return UNKNOWN (not FAIL) when input data is missing — source
adapters often don't populate budget/TRL, and treating null as fail
would reject valid calls. Same applies to embedding similarity: a
missing cosine (model down, backfill incomplete) is UNKNOWN, not FAIL.

The embedding gate is intentionally pure: callers compute the cosine
elsewhere (see `rank_pair`) and pass the float in. This keeps
`criteria.py` free of HTTP/DB dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

TIMELINE_MIN_DAYS = 14


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    reason: str | None = None  # 'too_soon' | 'expired' | 'currency_mismatch' | None


@dataclass(frozen=True)
class Gates:
    timeline: GateResult
    budget: GateResult
    trl: GateResult
    embedding: GateResult


def evaluate_gates(
    profile,
    call,
    *,
    now: datetime,
    embedding_threshold: float,
    embed_similarity: float | None = None,
    embedding_hard_fail_threshold: float | None = None,
) -> Gates:
    """Evaluate the four rule-based gates for a (profile, call) pair.

    `profile` and `call` are duck-typed: `profile` needs `structured` (dict)
    and `budget_currency`; `call` needs `deadline`, `budget_min`, `budget_max`,
    `currency`, and `structured` (dict-or-None for TRL fields).

    `embed_similarity` is the precomputed cosine between profile and call
    embeddings (caller's responsibility — keeps this module pure). Default
    None means UNKNOWN (callers that don't yet wire up embeddings get the
    same fall-through-to-LLM behaviour as a missing budget).

    `embedding_threshold` is the cosine cutoff at which the pre-gate PASSES.
    Pairs below this (but at/above `embedding_hard_fail_threshold`) surface
    as UNKNOWN so the LLM can adjudicate. Required: callers must thread the
    value (in practice from Settings.embedding_pregate_threshold) — there
    is no module-level default to fall back on. This keeps the Settings
    field as the single source of truth.

    `embedding_hard_fail_threshold` is the cutoff below which the gate
    short-circuits to FAIL (no LLM call). Codex finding #1: splitting the
    cutoff into a two-band gate (PASS / UNKNOWN-review / FAIL) leaves the
    "obviously off-topic" short-circuit in place while letting borderline
    pairs reach the LLM rather than being silently zeroed. Defaults to
    `embedding_threshold` when not provided, preserving legacy single-cut
    behaviour for callers that haven't plumbed the new setting through.
    """
    hard = (embedding_hard_fail_threshold
            if embedding_hard_fail_threshold is not None
            else embedding_threshold)
    return Gates(
        timeline=_timeline(call.deadline, now),
        budget=_budget(profile, call),
        trl=_trl(profile, call),
        embedding=_embedding(
            embed_similarity,
            threshold=embedding_threshold,
            hard_fail_threshold=hard,
        ),
    )


def _timeline(deadline: datetime | None, now: datetime) -> GateResult:
    if deadline is None:
        return GateResult(GateStatus.PASS)
    if deadline <= now:
        return GateResult(GateStatus.FAIL, reason="expired")
    if deadline < now + timedelta(days=TIMELINE_MIN_DAYS):
        return GateResult(GateStatus.FAIL, reason="too_soon")
    return GateResult(GateStatus.PASS)


def _budget(profile, call) -> GateResult:
    p = profile.structured or {}
    p_min, p_max = p.get("budget_min"), p.get("budget_max")
    c_min, c_max = call.budget_min, call.budget_max
    if p_min is None or p_max is None or c_min is None or c_max is None:
        return GateResult(GateStatus.UNKNOWN)
    p_currency = (profile.budget_currency or "").upper() or None
    c_currency = (call.currency or "").upper() or None
    if p_currency is None or c_currency is None:
        return GateResult(GateStatus.UNKNOWN)
    if p_currency != c_currency:
        return GateResult(GateStatus.UNKNOWN, reason="currency_mismatch")
    # Range overlap: max(p_min, c_min) <= min(p_max, c_max)
    if max(p_min, c_min) <= min(p_max, c_max):
        return GateResult(GateStatus.PASS)
    return GateResult(GateStatus.FAIL)


def _trl(profile, call) -> GateResult:
    p = profile.structured or {}
    p_min, p_max = p.get("trl_min"), p.get("trl_max")
    c = call.structured or {}
    c_min, c_max = c.get("trl_min"), c.get("trl_max")
    if any(v is None for v in (p_min, p_max, c_min, c_max)):
        return GateResult(GateStatus.UNKNOWN)
    if max(p_min, c_min) <= min(p_max, c_max):
        return GateResult(GateStatus.PASS)
    return GateResult(GateStatus.FAIL)


def _embedding(
    sim: float | None,
    *,
    threshold: float,
    hard_fail_threshold: float | None = None,
) -> GateResult:
    """Cosine-similarity pre-gate.

    None                       -> UNKNOWN (missing embedding; caller -> LLM).
    sim < hard_fail_threshold  -> FAIL with reason='topic_dissimilar'.
    hard <= sim < threshold    -> UNKNOWN (review band — LLM adjudicates).
    sim >= threshold           -> PASS.

    When `hard_fail_threshold` is omitted (or equals `threshold`), the
    gate collapses back to a strict two-state FAIL/PASS — the legacy
    behaviour before Codex finding #1 split the cutoff. This keeps old
    tests (and callers that haven't plumbed the new setting through) green.
    """
    if sim is None:
        return GateResult(GateStatus.UNKNOWN)
    hard = hard_fail_threshold if hard_fail_threshold is not None else threshold
    if sim < hard:
        return GateResult(GateStatus.FAIL, reason="topic_dissimilar")
    if sim < threshold:
        # In the review band: don't zero the pair, let the LLM weigh in.
        return GateResult(GateStatus.UNKNOWN, reason="topic_borderline")
    return GateResult(GateStatus.PASS)
