
import pytest

from call_monitor.ranking.aggregate import (
    LLMResult,
    aggregate,
    logprob_expected,
)
from call_monitor.ranking.criteria import GateResult, Gates, GateStatus

# --- logprob_expected ---
#
# Critical correctness note (Codex H2): the LLM emits a JSON object like
# `{"topic_fit": {"rating": 4, ...}, ...}`. The first content tokens are
# JSON syntax. The numeric rating is buried several tokens deep.
# logprob_expected MUST walk the content stream looking for the first
# integer-in-[0,5] token (only topic_fit.rating is in that range), then
# use ITS top_logprobs.


def _content_tokens(*tokens_with_top: tuple[str, list[tuple[str, float]]]) -> list[dict]:
    """Build a content_logprobs list mimicking what Ollama returns."""
    out = []
    for token, top in tokens_with_top:
        out.append({
            "token": token,
            "logprob": next((lp for t, lp in top if t == token), 0.0),
            "top_logprobs": [{"token": t, "logprob": lp} for t, lp in top],
        })
    return out


def test_logprob_expected_finds_rating_token_in_realistic_json_stream():
    """Mimic Ollama's tokenization of `{"topic_fit": {"rating": 4, ...}}`.
    The integer rating shows up several tokens in; the function must walk
    until it finds it."""
    content = _content_tokens(
        ('{',           [('{', -0.0)]),
        ('"topic_fit"', [('"topic_fit"', -0.1)]),
        (':',           [(':', -0.0)]),
        (' {',          [(' {', -0.0)]),
        ('"rating"',    [('"rating"', -0.1)]),
        (':',           [(':', -0.0)]),
        (' ',           [(' ', -0.0)]),
        # Here's the rating token — top_logprobs gives the G-Eval signal.
        ('4', [
            ('4', -0.1),
            ('3', -1.5),
            ('5', -2.0),
        ]),
        (',',           [(',', -0.0)]),
    )
    e = logprob_expected(content)
    assert e is not None
    # softmax([-0.1, -1.5, -2.0]) ≈ [0.74, 0.18, 0.08]
    # E = 4*0.74 + 3*0.18 + 5*0.08 ≈ 3.90
    assert 3.7 < e < 4.1


def test_logprob_expected_skips_non_integer_alternatives_at_rating_position():
    """At the rating token's top_logprobs, ignore tokens like ' ' or 'four';
    only integer tokens in [0, 5] contribute."""
    content = _content_tokens(
        ('"rating"', [('"rating"', -0.1)]),
        (':', [(':', -0.0)]),
        ('4', [
            ('4', -0.1),
            (' ', -1.5),     # skipped — non-integer
            ('five', -1.6),  # skipped — non-integer
            ('3', -2.0),
        ]),
    )
    e = logprob_expected(content)
    assert e is not None
    assert 3.5 < e < 4.0


def test_logprob_expected_returns_none_when_no_integer_token_found():
    """Whole content stream has no integer in [0, 5] — caller falls back to argmax."""
    content = _content_tokens(
        ('{', [('{', -0.0)]),
        ('"oops"', [('"oops"', -0.1)]),
    )
    assert logprob_expected(content) is None


def test_logprob_expected_handles_empty_or_malformed():
    assert logprob_expected([]) is None
    assert logprob_expected(None) is None


def test_logprob_expected_filters_out_of_range_integers():
    """Token alternatives like '6' or '9' (or random IDs from elsewhere)
    are ignored at the rating position."""
    content = _content_tokens(
        ('"rating"', [('"rating"', -0.1)]),
        (':', [(':', -0.0)]),
        ('4', [
            ('4', -0.1),
            ('6', -1.5),  # skipped — out of range
            ('9', -2.0),  # skipped — out of range
        ]),
    )
    e = logprob_expected(content)
    assert e is not None
    assert e == pytest.approx(4.0, rel=0.01)


# --- aggregate function ---

def _gates_pass():
    return Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
    )


def _llm_clean(topic_expected=4.0, eligibility="pass", exclusions="pass"):
    return LLMResult(
        topic_expected=topic_expected,
        topic_argmax=int(round(topic_expected)),
        eligibility=eligibility,
        exclusions=exclusions,
    )


def test_aggregate_clean_pass_uses_topic_expected_times_20():
    r = aggregate(_gates_pass(), _llm_clean(topic_expected=4.0))
    assert r.score == 80
    assert r.reason is None


def test_aggregate_timeline_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.FAIL, reason="too_soon"),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.5))
    assert r.score == 0
    assert r.reason == "timeline"


def test_aggregate_timeline_fail_ignored_with_must_include_override():
    """Spec line 282: must_include calls bypass the timeline gate. The
    LLM's verdict carries through even though timeline FAILED."""
    g = Gates(
        timeline=GateResult(GateStatus.FAIL, reason="too_soon"),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.PASS),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.5), must_include_override=True)
    # 4.5 × 20 = 90, not 0.
    assert r.score == 90
    assert r.reason is None


def test_aggregate_budget_fail_still_short_circuits_even_with_override():
    """must_include only bypasses TIMELINE per spec, not budget/TRL.
    A user typing must_include=['pathology'] doesn't also want calls
    outside their budget range."""
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.FAIL),
        trl=GateResult(GateStatus.PASS),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.5), must_include_override=True)
    assert r.score == 0
    assert r.reason == "budget"


def test_aggregate_budget_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.FAIL),
        trl=GateResult(GateStatus.PASS),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.5))
    assert r.score == 0
    assert r.reason == "budget"


def test_aggregate_trl_fail_short_circuits():
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.PASS),
        trl=GateResult(GateStatus.FAIL),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.5))
    assert r.score == 0
    assert r.reason == "trl"


def test_aggregate_eligibility_fail_short_circuits():
    r = aggregate(_gates_pass(), _llm_clean(eligibility="fail"))
    assert r.score == 0
    assert r.reason == "eligibility"


def test_aggregate_eligibility_unclear_caps_at_50():
    r = aggregate(_gates_pass(), _llm_clean(topic_expected=4.5, eligibility="unclear"))
    # 4.5 * 20 = 90, capped to 50.
    assert r.score == 50
    assert r.reason is None


def test_aggregate_exclusions_fail_caps_low():
    """exclusions=fail → min(10, topic_expected*2)."""
    r = aggregate(_gates_pass(), _llm_clean(topic_expected=4.0, exclusions="fail"))
    # min(10, 4.0 * 2) = 8
    assert r.score == 8
    assert r.reason == "exclusion"


def test_aggregate_exclusions_fail_caps_at_10_for_high_topic():
    r = aggregate(_gates_pass(), _llm_clean(topic_expected=5.0, exclusions="fail"))
    # min(10, 5.0 * 2) = 10
    assert r.score == 10
    assert r.reason == "exclusion"


def test_aggregate_unknown_gates_do_not_short_circuit():
    """UNKNOWN ≠ FAIL — gates that can't be evaluated don't penalize."""
    g = Gates(
        timeline=GateResult(GateStatus.PASS),
        budget=GateResult(GateStatus.UNKNOWN),
        trl=GateResult(GateStatus.UNKNOWN),
    )
    r = aggregate(g, _llm_clean(topic_expected=4.0))
    assert r.score == 80
    assert r.reason is None
