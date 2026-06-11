# tests/test_ranking_ev.py
import math

from call_manager.ranking.ev import extract_topic_expected


def _char_tokens(content: str, *, rating_top_logprobs: list[dict], rating_char: str):
    """Build a realistic Ollama logprobs envelope by char-tokenizing `content`
    (one token per character, each with its UTF-8 bytes). At the FIRST
    occurrence of `rating_char` that is preceded by `"rating":` we attach
    `rating_top_logprobs`; every other token gets a trivial single-entry
    top_logprobs. This mirrors the real envelope's shape closely enough to
    exercise the offset binding."""
    content_tokens = []
    marker = '"rating":'
    rating_pos = content.index(marker) + len(marker)
    # advance past spaces to the digit
    while content[rating_pos] == " ":
        rating_pos += 1
    for i, ch in enumerate(content):
        tok = {"token": ch, "bytes": list(ch.encode("utf-8")), "logprob": -0.01}
        if i == rating_pos and ch == rating_char:
            tok["top_logprobs"] = rating_top_logprobs
        else:
            tok["top_logprobs"] = [
                {"token": ch, "logprob": -0.01, "bytes": list(ch.encode("utf-8"))}
            ]
        content_tokens.append(tok)
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "logprobs": {"content": content_tokens},
            }
        ]
    }


def test_expected_value_weights_digits():
    content = '{"topic_fit": {"rating": 4, "rationale": "good"}}'
    top = [
        {"token": "4", "logprob": math.log(0.7), "bytes": [52]},
        {"token": "3", "logprob": math.log(0.3), "bytes": [51]},
    ]
    env = _char_tokens(content, rating_top_logprobs=top, rating_char="4")
    ev = extract_topic_expected(env, 4)
    assert ev is not None
    assert abs(ev - (4 * 0.7 + 3 * 0.3)) < 1e-6  # 3.7


def test_none_when_no_logprobs_block():
    env = {"choices": [{"message": {"content": '{"topic_fit": {"rating": 4}}'}}]}
    assert extract_topic_expected(env, 4) is None


def test_none_for_degenerate_single_digit_top_logprobs():
    content = '{"topic_fit": {"rating": 5, "rationale": "x"}}'
    top = [{"token": "5", "logprob": math.log(0.99), "bytes": [53]}]
    env = _char_tokens(content, rating_top_logprobs=top, rating_char="5")
    assert extract_topic_expected(env, 5) is None  # fewer than 2 digit alternatives


def test_does_not_bind_to_rating_digit_in_rationale_prose():
    """The rationale string contains '4/5' BEFORE no — wait, rating is first.
    Here we put a misleading '4' in the rationale that FOLLOWS rating; the
    scanner must still bind to the structural rating literal, and its
    top_logprobs (the real distribution) is what's used."""
    content = '{"topic_fit": {"rating": 4, "rationale": "scored 4/5 overall"}}'
    top = [
        {"token": "4", "logprob": math.log(0.6), "bytes": [52]},
        {"token": "5", "logprob": math.log(0.4), "bytes": [53]},
    ]
    env = _char_tokens(content, rating_top_logprobs=top, rating_char="4")
    ev = extract_topic_expected(env, 4)
    assert ev is not None
    assert abs(ev - (4 * 0.6 + 5 * 0.4)) < 1e-6  # 4.4 — used the structural token


def test_none_when_rating_literal_inside_a_rationale_string():
    """A rationale string literally containing `"rating": 2` must not fool the
    scanner — the structural topic_fit.rating is 4. Build an envelope whose
    structural rating (4) has a real 2-digit distribution; if the scanner
    mis-locked onto the in-string `"rating": 2`, the covering token would be
    '2' and fail the str(parsed_rating)=='4' sanity check → None. We assert it
    still binds to the structural 4."""
    content = '{"topic_fit": {"rating": 4, "rationale": "the call says \\"rating\\": 2 somewhere"}}'
    top = [
        {"token": "4", "logprob": math.log(0.8), "bytes": [52]},
        {"token": "3", "logprob": math.log(0.2), "bytes": [51]},
    ]
    env = _char_tokens(content, rating_top_logprobs=top, rating_char="4")
    ev = extract_topic_expected(env, 4)
    assert ev is not None
    assert abs(ev - (4 * 0.8 + 3 * 0.2)) < 1e-6


def test_ev_always_in_range_and_never_raises():
    # Garbage envelope shapes must yield None, not exceptions.
    for env in [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"logprobs": {"content": "nope"}}]},
    ]:
        assert extract_topic_expected(env, 3) is None
