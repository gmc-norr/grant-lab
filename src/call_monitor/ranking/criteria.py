"""Rule-based gates for ranking v2.

Four deterministic checks (timeline, budget, TRL, embedding) evaluated
in Python before any LLM call. A FAIL on any gate short-circuits the
LLM call and yields aggregate_score=0 with the gate name as
aggregate_reason.

Gates return UNKNOWN (not FAIL) when input data is missing — source
adapters often don't populate budget/TRL, and treating null as fail
would reject valid calls. Same applies to embedding similarity: a
missing cosine (model down, backfill incomplete) is UNKNOWN, not FAIL.

The embedding gate is intentionally pure: callers compute the cosine
elsewhere (see Task 5 / `rank_pair_v2`) and pass the float in. This
keeps `criteria.py` free of HTTP/DB dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

TIMELINE_MIN_DAYS = 14
# Default cosine-similarity threshold for the embedding pre-gate. Mirrors
# the default in `Settings.embedding_pregate_threshold` (config.py); kept
# as a module constant so this module stays import-time-pure (does not
# require env-loaded Settings()). Production callers thread the env value
# through their own paths if they need to override.
EMBEDDING_PREGATE_THRESHOLD = 0.30


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
    embed_similarity: float | None = None,
    embedding_threshold: float = EMBEDDING_PREGATE_THRESHOLD,
) -> Gates:
    """Evaluate the four rule-based gates for a (profile, call) pair.

    `profile` and `call` are duck-typed: `profile` needs `structured` (dict)
    and `budget_currency`; `call` needs `deadline`, `budget_min`, `budget_max`,
    `currency`, and `structured` (dict-or-None for TRL fields).

    `embed_similarity` is the precomputed cosine between profile and call
    embeddings (caller's responsibility — keeps this module pure). Default
    None means UNKNOWN (callers that don't yet wire up embeddings get the
    same fall-through-to-LLM behaviour as a missing budget).

    `embedding_threshold` lets callers (in practice: rank_pair_v2 reading
    Settings.embedding_pregate_threshold) override the module-default
    cosine cutoff. Keeping the default a module constant means this module
    stays import-time-pure (no env load required for unit tests).
    """
    return Gates(
        timeline=_timeline(call.deadline, now),
        budget=_budget(profile, call),
        trl=_trl(profile, call),
        embedding=_embedding(embed_similarity, threshold=embedding_threshold),
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
    threshold: float = EMBEDDING_PREGATE_THRESHOLD,
) -> GateResult:
    """Cosine-similarity pre-gate.

    None  -> UNKNOWN (missing embedding; caller falls through to LLM).
    >= threshold -> PASS.
    <  threshold -> FAIL with reason='topic_dissimilar'.
    """
    if sim is None:
        return GateResult(GateStatus.UNKNOWN)
    if sim < threshold:
        return GateResult(GateStatus.FAIL, reason="topic_dissimilar")
    return GateResult(GateStatus.PASS)
