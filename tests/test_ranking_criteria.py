from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from grant_lab.ranking.criteria import GateStatus
from grant_lab.ranking.criteria import evaluate_gates as _evaluate_gates

# Default cosine threshold mirrors Settings.embedding_pregate_threshold (0.30).
# Tests that don't care about the threshold (i.e. don't pass embed_similarity)
# get the same default behaviour as production via this partial wrapper.
_DEFAULT_EMBEDDING_THRESHOLD = 0.30
evaluate_gates = partial(_evaluate_gates, embedding_threshold=_DEFAULT_EMBEDDING_THRESHOLD)


@dataclass
class _Profile:
    structured: dict
    budget_currency: str | None = None


@dataclass
class _Call:
    deadline: datetime | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    currency: str | None = None
    structured: dict | None = None  # not used by gates; kept for parallel shape


_NOW = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)


def _profile(**structured) -> _Profile:
    return _Profile(
        structured={
            "budget_min": None,
            "budget_max": None,
            "trl_min": None,
            "trl_max": None,
            **structured,
        }
    )


# --- Timeline gate ---


def test_timeline_passes_when_deadline_is_15_days_away():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW + timedelta(days=15)), now=_NOW)
    assert g.timeline.status == GateStatus.PASS


def test_timeline_passes_when_deadline_is_13_days_away():
    """Spec §2: urgency is not suitability. The 14-day zeroing is gone."""
    g = evaluate_gates(_profile(), _Call(deadline=_NOW + timedelta(days=13)), now=_NOW)
    assert g.timeline.status == GateStatus.PASS
    assert g.timeline.reason is None


def test_timeline_passes_one_minute_before_the_deadline():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW + timedelta(minutes=1)), now=_NOW)
    assert g.timeline.status == GateStatus.PASS


def test_timeline_fails_exactly_at_the_deadline():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW), now=_NOW)
    assert g.timeline.status == GateStatus.FAIL
    assert g.timeline.reason == "expired"


def test_timeline_fails_when_deadline_passed():
    g = evaluate_gates(_profile(), _Call(deadline=_NOW - timedelta(days=1)), now=_NOW)
    assert g.timeline.status == GateStatus.FAIL
    assert g.timeline.reason == "expired"


def test_timeline_passes_when_deadline_is_none():
    g = evaluate_gates(_profile(), _Call(deadline=None), now=_NOW)
    assert g.timeline.status == GateStatus.PASS


# --- Budget gate ---


def test_budget_overlap_with_matching_currency():
    g = evaluate_gates(
        _Profile(
            structured={"budget_min": 100_000, "budget_max": 1_000_000},
            budget_currency="SEK",
        ),
        _Call(budget_min=500_000, budget_max=2_000_000, currency="SEK"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.PASS


def test_budget_no_overlap_with_matching_currency():
    g = evaluate_gates(
        _Profile(structured={"budget_min": 100_000, "budget_max": 500_000}, budget_currency="SEK"),
        _Call(budget_min=1_000_000, budget_max=2_000_000, currency="SEK"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.FAIL


def test_budget_unknown_when_currencies_differ():
    """Different currencies → unknown, NOT fail. Spec M2 fix."""
    g = evaluate_gates(
        _Profile(structured={"budget_min": 100_000, "budget_max": 500_000}, budget_currency="SEK"),
        _Call(budget_min=10_000, budget_max=50_000, currency="EUR"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.UNKNOWN
    assert g.budget.reason == "currency_mismatch"


def test_budget_unknown_when_profile_has_no_currency():
    g = evaluate_gates(
        _Profile(structured={"budget_min": 100_000, "budget_max": 500_000}, budget_currency=None),
        _Call(budget_min=200_000, budget_max=300_000, currency="SEK"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.UNKNOWN


def test_budget_unknown_when_either_side_has_no_range():
    g = evaluate_gates(
        _Profile(structured={"budget_min": None, "budget_max": None}, budget_currency="SEK"),
        _Call(budget_min=200_000, budget_max=300_000, currency="SEK"),
        now=_NOW,
    )
    assert g.budget.status == GateStatus.UNKNOWN


# --- TRL gate ---


def test_trl_overlap_passes():
    g = evaluate_gates(
        _profile(trl_min=4, trl_max=8),
        _Call(structured={"trl_min": 6, "trl_max": 9}),
        now=_NOW,
    )
    assert g.trl.status == GateStatus.PASS


def test_trl_disjoint_fails():
    g = evaluate_gates(
        _profile(trl_min=1, trl_max=3),
        _Call(structured={"trl_min": 7, "trl_max": 9}),
        now=_NOW,
    )
    assert g.trl.status == GateStatus.FAIL


def test_trl_unknown_when_either_side_missing():
    g = evaluate_gates(
        _profile(trl_min=4, trl_max=8),
        _Call(structured=None),
        now=_NOW,
    )
    assert g.trl.status == GateStatus.UNKNOWN


# --- Embedding gate ---


def test_embedding_gate_passes_above_threshold():
    from grant_lab.ranking.criteria import GateStatus, _embedding

    g = _embedding(_DEFAULT_EMBEDDING_THRESHOLD + 0.01, threshold=_DEFAULT_EMBEDDING_THRESHOLD)
    assert g.status == GateStatus.PASS


def test_embedding_gate_fails_below_threshold():
    from grant_lab.ranking.criteria import GateStatus, _embedding

    g = _embedding(_DEFAULT_EMBEDDING_THRESHOLD - 0.01, threshold=_DEFAULT_EMBEDDING_THRESHOLD)
    assert g.status == GateStatus.FAIL
    assert g.reason == "topic_dissimilar"


def test_embedding_gate_passes_at_exact_threshold():
    """Spec: >= threshold passes. Exactly-equal is PASS."""
    from grant_lab.ranking.criteria import GateStatus, _embedding

    g = _embedding(_DEFAULT_EMBEDDING_THRESHOLD, threshold=_DEFAULT_EMBEDDING_THRESHOLD)
    assert g.status == GateStatus.PASS


def test_embedding_gate_unknown_in_review_band():
    """Two-band gate: a similarity between hard_fail and pass thresholds
    returns UNKNOWN so the LLM adjudicates rather than silently zeroing
    a potentially-on-topic pair. The hard FAIL stays reserved for
    obviously-off-topic pairs (sim < hard_fail)."""
    from grant_lab.ranking.criteria import GateStatus, _embedding

    # 0.25 is between hard_fail=0.20 and threshold=0.30 → UNKNOWN
    g = _embedding(0.25, threshold=0.30, hard_fail_threshold=0.20)
    assert g.status == GateStatus.UNKNOWN


def test_embedding_gate_fails_below_hard_threshold():
    """Sim below the hard-fail threshold still short-circuits the LLM."""
    from grant_lab.ranking.criteria import GateStatus, _embedding

    g = _embedding(0.10, threshold=0.30, hard_fail_threshold=0.20)
    assert g.status == GateStatus.FAIL
    assert g.reason == "topic_dissimilar"


def test_embedding_gate_passes_above_pass_threshold():
    from grant_lab.ranking.criteria import GateStatus, _embedding

    g = _embedding(0.50, threshold=0.30, hard_fail_threshold=0.20)
    assert g.status == GateStatus.PASS


def test_embedding_gate_fails_when_hard_and_pass_collapse():
    """Legacy behavior: when hard_fail_threshold == threshold the gate
    is strictly two-state (FAIL below, PASS at/above) — no UNKNOWN band.
    Tests that construct `_embedding` without hard_fail must keep working."""
    from grant_lab.ranking.criteria import GateStatus, _embedding

    g_below = _embedding(0.29, threshold=0.30, hard_fail_threshold=0.30)
    g_at = _embedding(0.30, threshold=0.30, hard_fail_threshold=0.30)
    assert g_below.status == GateStatus.FAIL
    assert g_at.status == GateStatus.PASS


def test_embedding_gate_unknown_when_similarity_is_none():
    """Missing embedding (model down or backfill incomplete) -> UNKNOWN,
    not FAIL. Caller falls through to LLM."""
    from grant_lab.ranking.criteria import GateStatus, _embedding

    g = _embedding(None, threshold=_DEFAULT_EMBEDDING_THRESHOLD)
    assert g.status == GateStatus.UNKNOWN


def test_evaluate_gates_includes_embedding_field():
    """Gates dataclass now has 4 fields, embedding being the new one."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from grant_lab.ranking.criteria import Gates

    profile = SimpleNamespace(structured={}, budget_currency=None)
    call = SimpleNamespace(
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None,
        budget_max=None,
        currency=None,
        structured={},
    )
    gates = evaluate_gates(profile, call, now=datetime.now(UTC), embed_similarity=0.5)
    assert isinstance(gates, Gates)
    assert hasattr(gates, "embedding")
    assert gates.embedding.status.value == "pass"


def test_evaluate_gates_default_embed_similarity_none_yields_unknown():
    """Backwards-compat: callers that don't pass embed_similarity get UNKNOWN."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from grant_lab.ranking.criteria import GateStatus

    profile = SimpleNamespace(structured={}, budget_currency=None)
    call = SimpleNamespace(
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None,
        budget_max=None,
        currency=None,
        structured={},
    )
    gates = evaluate_gates(profile, call, now=datetime.now(UTC))
    assert gates.embedding.status == GateStatus.UNKNOWN
