# tests/test_ranking_ev.py
import math

import pytest

from grant_lab.ranking.ev import extract_topic_expected


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
                    "content": [{"token": "4", "logprob": -0.1, "bytes": [52], "top_logprobs": []}]
                },
            }
        ]
    }
    with caplog.at_level(logging.WARNING, logger="grant_lab.ranking.ev"):
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
    with caplog.at_level(logging.WARNING, logger="grant_lab.ranking.ev"):
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


# ---------------------------------------------------------------------------
# Thinking models: the logprobs stream is LONGER than message.content
# ---------------------------------------------------------------------------


def _prepend_thought(env: dict, thought: str) -> dict:
    """Put a reasoning trace in front of the logprobs tokens, leaving
    `message.content` alone — exactly what Gemma 4 emits.

    Measured against gemma4:e4b-it-q4_K_M on 2026-09-09: `message.content` was
    1130 chars of JSON while the logprobs stream was 4501 chars, opening with
    `<|channel>thought\nHere's a thinking process...`; the content began at
    offset 3371 of the stream. Every pre-existing test in this file builds a
    stream that EQUALS the content, so none of them can see this.
    """
    thought_tokens = [
        {
            "token": ch,
            "bytes": list(ch.encode("utf-8")),
            "logprob": -0.01,
            "top_logprobs": [{"token": ch, "logprob": -0.01, "bytes": list(ch.encode("utf-8"))}],
        }
        for ch in thought
    ]
    choice = env["choices"][0]
    choice["logprobs"]["content"] = thought_tokens + choice["logprobs"]["content"]
    return env


_THOUGHT = (
    "<|channel>thought\nHere's a thinking process to arrive at the desired JSON output:\n"
    "1. Analyze the Profile: the rating should be 5 for a genomics group.\n"
)


def test_ev_binds_when_the_stream_carries_a_reasoning_trace():
    """The regression: a thinking model's logprobs stream is not the content.

    Offsets are found in `message.content` but walked over the token stream, so
    without a shift the walk lands somewhere inside the reasoning trace, the
    covering token is a word rather than the rating digit, and EV returns None
    for EVERY ranking. Measured on the live database before the fix:
    0 of 592 `call_rankings` rows had `topic_fit_expected` populated.
    """
    content = '{"topic_fit": {"rating": 4, "rationale": "good fit"}}'
    top = [
        {"token": "4", "logprob": math.log(0.6), "bytes": [52]},
        {"token": "3", "logprob": math.log(0.4), "bytes": [51]},
    ]
    env = _prepend_thought(
        _char_tokens(content, rating_top_logprobs=top, rating_char="4"), _THOUGHT
    )

    ev = extract_topic_expected(env, 4)

    assert ev is not None, "EV must bind even when the stream carries a reasoning trace"
    assert ev == pytest.approx(0.6 * 4 + 0.4 * 3, abs=1e-6)


def test_the_thought_prefix_really_shifts_the_offsets():
    """Negative control for the fixture itself.

    If `_prepend_thought` did not actually move the rating digit's position in
    the stream, the test above would pass on the broken code and prove nothing.
    """
    content = '{"topic_fit": {"rating": 4, "rationale": "good fit"}}'
    top = [{"token": "4", "logprob": math.log(0.6), "bytes": [52]}]
    plain = _char_tokens(content, rating_top_logprobs=top, rating_char="4")
    n_plain = len(plain["choices"][0]["logprobs"]["content"])

    shifted = _prepend_thought(
        _char_tokens(content, rating_top_logprobs=top, rating_char="4"), _THOUGHT
    )
    n_shifted = len(shifted["choices"][0]["logprobs"]["content"])

    assert n_shifted == n_plain + len(_THOUGHT)
    assert shifted["choices"][0]["message"]["content"] == content, "content must stay clean"


def test_ev_still_binds_when_the_stream_equals_the_content():
    """A non-thinking model must keep working — the shift is zero there."""
    content = '{"topic_fit": {"rating": 2, "rationale": "partial"}}'
    top = [
        {"token": "2", "logprob": math.log(0.7), "bytes": [50]},
        {"token": "1", "logprob": math.log(0.3), "bytes": [49]},
    ]
    env = _char_tokens(content, rating_top_logprobs=top, rating_char="2")

    ev = extract_topic_expected(env, 2)

    assert ev == pytest.approx(0.7 * 2 + 0.3 * 1, abs=1e-6)


def test_ev_declines_when_the_content_is_absent_from_the_stream():
    """Fail closed: if the stream cannot be aligned to the content at all, EV
    must return None rather than weight an arbitrary token."""
    content = '{"topic_fit": {"rating": 4, "rationale": "good fit"}}'
    top = [
        {"token": "4", "logprob": math.log(0.6), "bytes": [52]},
        {"token": "3", "logprob": math.log(0.4), "bytes": [51]},
    ]
    env = _char_tokens(content, rating_top_logprobs=top, rating_char="4")
    # Truncate the stream so the content no longer appears in it.
    env["choices"][0]["logprobs"]["content"] = env["choices"][0]["logprobs"]["content"][5:]

    assert extract_topic_expected(env, 4) is None


def test_a_thought_that_quotes_the_json_does_not_capture_the_alignment():
    """Models rehearse their answer inside the reasoning trace.

    When the trace contains a verbatim copy of the final JSON, the FIRST
    occurrence in the stream is the rehearsal and the LAST is the answer the
    model actually returned. Aligning on the first would weight a token from
    the rehearsal — a different position, whose top_logprobs are not the
    rating's.
    """
    content = '{"topic_fit": {"rating": 4, "rationale": "good fit"}}'
    top = [
        {"token": "4", "logprob": math.log(0.6), "bytes": [52]},
        {"token": "3", "logprob": math.log(0.4), "bytes": [51]},
    ]
    # The trace quotes the answer, then the answer itself follows.
    rehearsal = f"I will answer with {content} — checking it over now.\n"
    env = _prepend_thought(
        _char_tokens(content, rating_top_logprobs=top, rating_char="4"), rehearsal
    )
    stream = env["choices"][0]["logprobs"]["content"]
    joined = "".join(bytes(t["bytes"]).decode("utf-8") for t in stream)
    assert joined.find(content) != joined.rfind(content), "fixture must contain two copies"

    ev = extract_topic_expected(env, 4)

    assert ev == pytest.approx(0.6 * 4 + 0.4 * 3, abs=1e-6)


def test_an_unalignable_stream_does_not_weight_whatever_sits_at_offset_zero():
    """The hazard the fail-closed branch exists for.

    A stream that does not contain the content cannot be aligned. Falling back
    to offset 0 would weight whichever token happens to start the stream — and
    if that token IS the rating digit, every later sanity check passes and EV
    returns a number computed from an unrelated position. Constructed here so
    the wrong answer is a *plausible* one, which is what makes it dangerous.
    """
    content = '{"topic_fit": {"rating": 4, "rationale": "good fit"}}'
    rating_offset = content.index('"rating":') + len('"rating": ')
    # A stream whose token at `rating_offset` is the digit 4 with real
    # top_logprobs, but which does not contain `content` anywhere.
    decoy = "x" * rating_offset + "4" + "y" * 10
    tokens = []
    for i, ch in enumerate(decoy):
        tok = {"token": ch, "bytes": list(ch.encode("utf-8")), "logprob": -0.01}
        tok["top_logprobs"] = (
            [
                {"token": "4", "logprob": math.log(0.6), "bytes": [52]},
                {"token": "3", "logprob": math.log(0.4), "bytes": [51]},
            ]
            if i == rating_offset
            else [{"token": ch, "logprob": -0.01, "bytes": list(ch.encode("utf-8"))}]
        )
        tokens.append(tok)
    env = {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "logprobs": {"content": tokens},
            }
        ]
    }
    assert content not in decoy, "fixture must be unalignable"

    assert extract_topic_expected(env, 4) is None


def test_content_offset_in_stream_contract():
    """Direct contract for the aligner, since `extract_topic_expected` masks
    some of its outcomes behind later checks."""
    from grant_lab.ranking.ev import _content_offset_in_stream

    def tok(ch):
        return {"token": ch, "bytes": list(ch.encode("utf-8"))}

    # Stream equals the content -> zero shift.
    assert _content_offset_in_stream([tok(c) for c in "abc"], "abc") == 0
    # Content preceded by a trace -> shift by the trace length.
    assert _content_offset_in_stream([tok(c) for c in "xxabc"], "abc") == 2
    # Content absent -> None, not 0.
    assert _content_offset_in_stream([tok(c) for c in "xxx"], "abc") is None
    # A junk entry makes the stream uninterpretable -> None, not a silent skip
    # that would shift every following offset by the junk token's width.
    assert _content_offset_in_stream([tok("a"), "not-a-token", tok("b")], "ab") is None
