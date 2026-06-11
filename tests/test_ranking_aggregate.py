from call_manager.ranking.aggregate import (
    LLMResult,
    aggregate,
)
from call_manager.ranking.criteria import GateResult, Gates, GateStatus

# --- aggregate function ---


def _gates_pass():
    return Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        # UNKNOWN = no embedding signal; gate doesn't short-circuit. Tests
        # in this file that explicitly probe embedding behavior construct
        # their own Gates with embedding=FAIL.
        embedding=GateResult(GateStatus.UNKNOWN),
    )


def _llm_clean(topic_argmax=4, eligibility="pass", exclusions="pass"):
    return LLMResult(
        topic_argmax=topic_argmax,
        eligibility=eligibility,
        exclusions=exclusions,
    )


def test_aggregate_clean_pass_uses_topic_argmax_times_20():
    r = aggregate(_gates_pass(), _llm_clean(topic_argmax=4))
    assert r.score == 80
    assert r.reason is None


def test_aggregate_timeline_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.FAIL, reason="too_soon"),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_argmax=5))
    assert r.score == 0
    assert r.reason == "timeline"


def test_aggregate_timeline_fail_ignored_with_must_include_override():
    """Spec line 282: must_include calls bypass the timeline gate. The
    LLM's verdict carries through even though timeline FAILED."""
    g = Gates(
        timeline=GateResult(GateStatus.FAIL, reason="too_soon"),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_argmax=5), must_include_override=True)
    # 5 × 20 = 100, not 0.
    assert r.score == 100
    assert r.reason is None


def test_aggregate_budget_fail_still_short_circuits_even_with_override():
    """must_include only bypasses TIMELINE per spec, not budget/TRL.
    A user typing must_include=['pathology'] doesn't also want calls
    outside their budget range."""
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.FAIL),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_argmax=5), must_include_override=True)
    assert r.score == 0
    assert r.reason == "budget"


def test_aggregate_budget_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.FAIL),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_argmax=5))
    assert r.score == 0
    assert r.reason == "budget"


def test_aggregate_trl_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.FAIL),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_argmax=5))
    assert r.score == 0
    assert r.reason == "trl"


def test_aggregate_eligibility_fail_short_circuits():
    r = aggregate(_gates_pass(), _llm_clean(eligibility="fail"))
    assert r.score == 0
    assert r.reason == "eligibility"


def test_aggregate_eligibility_unclear_caps_at_50():
    r = aggregate(_gates_pass(), _llm_clean(topic_argmax=4, eligibility="unclear"))
    # 4 * 20 = 80, capped to 50.
    assert r.score == 50
    assert r.reason is None


def test_aggregate_exclusions_fail_caps_low():
    """exclusions=fail → min(10, topic_argmax*2)."""
    r = aggregate(_gates_pass(), _llm_clean(topic_argmax=4, exclusions="fail"))
    # min(10, 4 * 2) = 8
    assert r.score == 8
    assert r.reason == "exclusion"


def test_aggregate_exclusions_fail_caps_at_10_for_high_topic():
    r = aggregate(_gates_pass(), _llm_clean(topic_argmax=5, exclusions="fail"))
    # min(10, 5 * 2) = 10
    assert r.score == 10
    assert r.reason == "exclusion"


def test_aggregate_unknown_gates_do_not_short_circuit():
    """UNKNOWN ≠ FAIL — gates that can't be evaluated don't penalize."""
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.UNKNOWN),
        trl=GateResult(GateStatus.UNKNOWN),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_argmax=4))
    assert r.score == 80
    assert r.reason is None


# --- Embedding gate aggregation ---


def test_aggregate_embedding_fail_short_circuits_to_zero():
    from call_manager.ranking.aggregate import LLMResult, aggregate
    from call_manager.ranking.criteria import GateResult, Gates, GateStatus

    gates = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
    )
    llm = LLMResult(topic_argmax=4, eligibility="pass", exclusions="pass")
    r = aggregate(gates, llm)
    assert r.score == 0
    assert r.reason == "embedding"


def test_aggregate_embedding_fail_bypassed_by_must_include():
    """Like timeline, must_include_override skips the embedding short-circuit."""
    from call_manager.ranking.aggregate import LLMResult, aggregate
    from call_manager.ranking.criteria import GateResult, Gates, GateStatus

    gates = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.FAIL, reason="topic_dissimilar"),
    )
    llm = LLMResult(topic_argmax=4, eligibility="pass", exclusions="pass")
    r = aggregate(gates, llm, must_include_override=True)
    # Bypass -> fall through to clean LLM path: 4 * 20 = 80
    assert r.score == 80
    assert r.reason is None


def test_aggregate_embedding_unknown_does_not_short_circuit():
    """UNKNOWN means missing embedding — fall through to LLM as if no gate."""
    from call_manager.ranking.aggregate import LLMResult, aggregate
    from call_manager.ranking.criteria import GateResult, Gates, GateStatus

    gates = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
        embedding=GateResult(GateStatus.UNKNOWN),
    )
    llm = LLMResult(topic_argmax=3, eligibility="pass", exclusions="pass")
    r = aggregate(gates, llm)
    assert r.score == 60
    assert r.reason is None


# --- topic_expected (EV) field ---


def test_aggregate_uses_expected_value_when_present():
    llm = LLMResult(topic_argmax=4, eligibility="pass", exclusions="pass", topic_expected=2.75)
    r = aggregate(_gates_pass(), llm)
    assert r.score == round(2.75 * 20)  # 55
    assert r.reason is None


def test_aggregate_expected_value_rounds_at_bucket_boundaries():
    for ev, want in [(2.25, 45), (2.95, 59), (3.0, 60)]:
        llm = LLMResult(topic_argmax=3, eligibility="pass", exclusions="pass", topic_expected=ev)
        assert aggregate(_gates_pass(), llm).score == want


def test_aggregate_expected_value_respects_unclear_cap():
    llm = LLMResult(topic_argmax=5, eligibility="unclear", exclusions="pass", topic_expected=4.8)
    # 4.8 * 20 = 96, capped to 50
    assert aggregate(_gates_pass(), llm).score == 50


def test_aggregate_expected_value_exclusion_cap():
    llm = LLMResult(topic_argmax=5, eligibility="pass", exclusions="fail", topic_expected=4.6)
    # min(10, round(4.6*2)) = min(10, 9) = 9
    assert aggregate(_gates_pass(), llm).score == 9


def test_aggregate_falls_back_to_argmax_when_expected_none():
    llm = LLMResult(topic_argmax=3, eligibility="pass", exclusions="pass", topic_expected=None)
    assert aggregate(_gates_pass(), llm).score == 60
