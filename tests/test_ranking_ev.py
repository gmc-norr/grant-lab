# tests/test_ranking_ev.py
import math

import pytest

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


def test_ev_logs_when_noops_with_logprobs_present(caplog):
    """B11 Defect 4a: when a logprobs block IS present but EV can't bind (e.g.
    the model emitted a QUOTED rating `"4"` so the structural integer literal
    isn't found), EV silently fell back to argmax with no signal. It must now
    emit a WARNING so the rate of EV no-ops is observable."""
    import logging

    # Quoted rating → _locate_rating_literal_offset returns None → EV no-ops.
    content = '{"topic_fit": {"rating": "4", "rationale": "x"}}'
    # A real logprobs block is present (so this is the logprobs path, not the
    # no-logprobs short-circuit).
    env = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "logprobs": {
                    "content": [
                        {"token": "4", "logprob": -0.1, "bytes": [52], "top_logprobs": []}
                    ]
                },
            }
        ]
    }
    with caplog.at_level(logging.WARNING, logger="call_manager.ranking.ev"):
        ev = extract_topic_expected(env, 4)
    assert ev is None
    assert any(
        "ev_noop" in (r.getMessage() or "") or "ev_noop" in r.message for r in caplog.records
    )


def test_ev_no_log_when_logprobs_absent(caplog):
    """When there's no logprobs block at all, EV returns None WITHOUT logging a
    no-op warning — that path is the expected argmax case (e.g. the remote
    Sonnet path exposes no logprobs), not an anomaly worth a warning."""
    import logging

    env = {"choices": [{"message": {"content": '{"topic_fit": {"rating": 4}}'}}]}
    with caplog.at_level(logging.WARNING, logger="call_manager.ranking.ev"):
        ev = extract_topic_expected(env, 4)
    assert ev is None
    assert not any(
        "ev_noop" in (r.getMessage() or "") or "ev_noop" in r.message for r in caplog.records
    )


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
    """A decoy '4' in the rationale prose must not steal the binding — the
    scanner binds to the structural rating literal and uses its top_logprobs."""
    content = '{"topic_fit": {"rating": 4, "rationale": "scored 4/5 overall"}}'
    top = [
        {"token": "4", "logprob": math.log(0.6), "bytes": [52]},
        {"token": "5", "logprob": math.log(0.4), "bytes": [53]},
    ]
    env = _char_tokens(content, rating_top_logprobs=top, rating_char="4")
    ev = extract_topic_expected(env, 4)
    assert ev is not None
    assert abs(ev - (4 * 0.6 + 5 * 0.4)) < 1e-6  # 4.4 — used the structural token


def test_binds_to_structural_rating_despite_decoy_in_rationale_string():
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


def _malformed_envelope(case: str) -> tuple[dict, int]:
    """Envelopes that historically raised despite the never-raises contract."""
    content = '{"topic_fit": {"rating": 4, "rationale": "x"}}'
    if case == "overflow_logprob":
        # math.exp(1000.0) overflows for a finite float — the rating token's
        # top list carries the absurd logprob so the weight loop reaches it.
        env = _char_tokens(
            content,
            rating_top_logprobs=[
                {"token": "4", "logprob": 1000.0, "bytes": [52]},
                {"token": "3", "logprob": -0.5, "bytes": [51]},
            ],
            rating_char="4",
        )
        return env, 4
    if case == "unhashable_token":
        # `["x"] in _DIGITS` raises TypeError (unhashable) in the weight loop.
        env = _char_tokens(
            content,
            rating_top_logprobs=[
                {"token": ["x"], "logprob": -0.1},
                {"token": "4", "logprob": -0.2, "bytes": [52]},
            ],
            rating_char="4",
        )
        return env, 4
    if case == "non_str_token_without_bytes":
        # A token dict with no usable bytes and a non-string token, placed
        # before the rating offset: len(5) raises TypeError in the walk.
        env = _char_tokens(
            content,
            rating_top_logprobs=[
                {"token": "4", "logprob": math.log(0.7), "bytes": [52]},
                {"token": "3", "logprob": math.log(0.3), "bytes": [51]},
            ],
            rating_char="4",
        )
        env["choices"][0]["logprobs"]["content"][0] = {"token": 5}
        return env, 4
    if case == "int_content":
        # Truthy non-string content: len(5) raises TypeError in the scanner.
        env = {"choices": [{"message": {"content": 5}, "logprobs": {"content": []}}]}
        return env, 5
    raise AssertionError(f"unknown case {case!r}")


@pytest.mark.parametrize(
    "case",
    [
        "overflow_logprob",
        "unhashable_token",
        "non_str_token_without_bytes",
        "int_content",
    ],
)
def test_never_raises_on_malformed_envelopes(case):
    env, parsed_rating = _malformed_envelope(case)
    assert extract_topic_expected(env, parsed_rating) is None


def test_tolerates_space_prefixed_tokens():
    """SentencePiece-style tokenizers often merge the preceding space into the
    digit token (' 4'). EV extraction must still bind and weight by digit."""
    content = '{"topic_fit": {"rating": 4, "rationale": "good"}}'
    marker = '"rating":'
    space_pos = content.index(marker) + len(marker)
    assert content[space_pos : space_pos + 2] == " 4"
    top = [
        {"token": " 4", "logprob": math.log(0.7), "bytes": [32, 52]},
        {"token": " 3", "logprob": math.log(0.3), "bytes": [32, 51]},
    ]
    content_tokens = []
    i = 0
    while i < len(content):
        if i == space_pos:
            content_tokens.append(
                {
                    "token": " 4",
                    "bytes": [32, 52],
                    "logprob": math.log(0.7),
                    "top_logprobs": top,
                }
            )
            i += 2
        else:
            ch = content[i]
            content_tokens.append(
                {
                    "token": ch,
                    "bytes": list(ch.encode("utf-8")),
                    "logprob": -0.01,
                    "top_logprobs": [
                        {"token": ch, "logprob": -0.01, "bytes": list(ch.encode("utf-8"))}
                    ],
                }
            )
            i += 1
    env = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "logprobs": {"content": content_tokens},
            }
        ]
    }
    ev = extract_topic_expected(env, 4)
    assert ev is not None
    assert abs(ev - (4 * 0.7 + 3 * 0.3)) < 1e-6  # 3.7


def test_none_when_parsed_rating_mismatches_covering_token():
    # Structural rating is 4 but the caller parsed 3 — sanity check must bail.
    content = '{"topic_fit": {"rating": 4, "rationale": "good"}}'
    top = [
        {"token": "4", "logprob": math.log(0.7), "bytes": [52]},
        {"token": "3", "logprob": math.log(0.3), "bytes": [51]},
    ]
    env = _char_tokens(content, rating_top_logprobs=top, rating_char="4")
    assert extract_topic_expected(env, 3) is None
