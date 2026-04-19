import json

import pytest

from call_monitor.ranking.prompt import (
    PROMPT_VERSION,
    Parsed,
    ParseError,
    build_messages,
    parse_response,
)


def test_prompt_version_bumped_to_v3():
    """The prompt's input shape changed — bump PROMPT_VERSION for audit/A-B."""
    assert PROMPT_VERSION == "v3.0"


def test_build_messages_includes_profile_and_call_blocks():
    profile = type("P", (), {
        "name": "Pathology",
        "kind": type("K", (), {"value": "group"})(),
        "description": "Digital pathology AI",
        "structured": {
            "areas": ["digital pathology", "AI"],
            "hard_exclusions": ["animal studies"],
        },
    })()
    call = type("C", (), {
        "title": "AI for medical imaging",
        "summary": "Open call for AI-driven medical imaging research.",
        "programme": "Vinnova",
        "deadline": None,
        "budget_min": None,
        "budget_max": None,
        "currency": None,
        "raw": {},
    })()
    msgs = build_messages(profile=profile, call=call)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    sys = msgs[0]["content"]
    assert "Pathology" in sys
    assert "digital pathology" in sys.lower()
    assert "animal studies" in sys
    user = msgs[1]["content"]
    assert "AI for medical imaging" in user
    assert "AI-driven medical imaging" in user


def test_build_messages_concatenates_summary_and_full_text():
    """Plan F prompt change: summary AND full_text both reach the LLM,
    not just one or the other (was: OR-pick)."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    profile = SimpleNamespace(
        name="Cancer Research", description="cancer biomarkers",
        kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    call = SimpleNamespace(
        title="A relevant call",
        summary="Brief abstract.",
        full_text="Substantive body paragraph 1.\n\nSubstantive body paragraph 2.",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    user_text = messages[1]["content"]
    # Both reach the LLM
    assert "Brief abstract." in user_text
    assert "Substantive body paragraph 1." in user_text
    assert "Substantive body paragraph 2." in user_text


def test_build_messages_uses_summary_only_when_full_text_is_none():
    """Backwards-compat: when full_text is None, prompt still uses summary alone."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    profile = SimpleNamespace(
        name="P", description="d", kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    call = SimpleNamespace(
        title="t", summary="only summary here", full_text=None,
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    assert "only summary here" in messages[1]["content"]


def test_build_messages_uses_full_text_only_when_summary_is_none():
    """And vice versa — full_text alone when summary is None."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    profile = SimpleNamespace(
        name="P", description="d", kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    call = SimpleNamespace(
        title="t", summary=None, full_text="only the body",
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    assert "only the body" in messages[1]["content"]


def test_build_messages_truncates_at_call_text_limit():
    """CALL_TEXT_LIMIT bumped to 16000 — verify the cap.

    Note: the cap is on the COMBINED summary+full_text after concat. To make
    the assertion crisp, we leave summary=None so the entire budget is
    available for the body of x's.
    """
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.prompt import CALL_TEXT_LIMIT

    assert CALL_TEXT_LIMIT == 16000

    profile = SimpleNamespace(
        name="P", description="d", kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    huge_full_text = "x" * 30_000
    call = SimpleNamespace(
        title="t", summary=None, full_text=huge_full_text,
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    user_text = messages[1]["content"]
    # The body section should be exactly CALL_TEXT_LIMIT chars of x.
    assert "x" * CALL_TEXT_LIMIT in user_text
    assert "x" * (CALL_TEXT_LIMIT + 1) not in user_text


def test_build_messages_truncates_combined_summary_plus_full_text():
    """When BOTH summary and full_text are provided, the truncation cap
    applies to their CONCATENATION (summary + '\\n\\n' + full_text), not
    to full_text alone. The available x-budget is reduced by the summary
    + separator length."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from call_monitor.ranking.prompt import CALL_TEXT_LIMIT

    profile = SimpleNamespace(
        name="P", description="d", kind=SimpleNamespace(value="group"),
        structured={"areas": [], "hard_exclusions": []},
    )
    summary = "Short abstract."
    huge_full_text = "x" * 30_000
    call = SimpleNamespace(
        title="t", summary=summary, full_text=huge_full_text,
        deadline=datetime.now(UTC) + timedelta(days=30),
        budget_min=None, budget_max=None, currency=None, programme=None,
    )
    messages = build_messages(profile=profile, call=call)
    user_text = messages[1]["content"]
    # Summary appears verbatim
    assert summary in user_text
    # Body x-run is shorter than CALL_TEXT_LIMIT by exactly len(summary) + 2
    # (the "\n\n" separator). Verify the budget accounting.
    expected_body_len = CALL_TEXT_LIMIT - len(summary) - 2
    assert "x" * expected_body_len in user_text
    assert "x" * (expected_body_len + 1) not in user_text


def test_parse_clean_response():
    content = json.dumps({
        "topic_fit": {"rating": 4, "rationale": "good", "quoted_evidence": "AI for medical"},
        "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
        "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
    })
    out = parse_response(content)
    assert isinstance(out, Parsed)
    assert out.topic_fit_rating == 4
    assert out.topic_fit_rationale == "good"
    assert out.topic_fit_quoted == "AI for medical"
    assert out.eligibility_rating == "pass"
    assert out.exclusions_rating == "pass"


def test_parse_invalid_json_raises():
    with pytest.raises(ParseError):
        parse_response("not json")


def test_parse_missing_topic_fit_raises():
    content = json.dumps({"eligibility": {"rating": "pass"}, "exclusions": {"rating": "pass"}})
    with pytest.raises(ParseError, match="topic_fit"):
        parse_response(content)


def test_parse_invalid_topic_rating_raises():
    content = json.dumps({
        "topic_fit": {"rating": 9, "rationale": "x"},
        "eligibility": {"rating": "pass"},
        "exclusions": {"rating": "pass"},
    })
    with pytest.raises(ParseError, match="topic_fit.rating"):
        parse_response(content)


def test_parse_invalid_eligibility_rating_raises():
    content = json.dumps({
        "topic_fit": {"rating": 3},
        "eligibility": {"rating": "maybe"},
        "exclusions": {"rating": "pass"},
    })
    with pytest.raises(ParseError, match="eligibility"):
        parse_response(content)


def test_parse_rejects_non_string_quoted_evidence():
    """Codex N3: quoted_evidence must be string or null; a list (or other
    non-string non-null type) must raise ParseError."""
    content = json.dumps({
        "topic_fit": {"rating": 4, "rationale": "x", "quoted_evidence": ["a list"]},
        "eligibility": {"rating": "pass"},
        "exclusions": {"rating": "pass"},
    })
    with pytest.raises(ParseError, match="quoted_evidence"):
        parse_response(content)


def _verification_payload(topic_quote: str) -> str:
    """Helper: build a minimal valid LLM JSON response with the given
    topic_fit quote and null quotes elsewhere — keeps the verification
    tests focused on the only field they exercise."""
    return json.dumps({
        "topic_fit": {"rating": 4, "rationale": "x", "quoted_evidence": topic_quote},
        "eligibility": {"rating": "pass", "rationale": "x", "quoted_evidence": None},
        "exclusions": {"rating": "pass", "rationale": "x", "quoted_evidence": None},
    })


def test_parse_response_verification_returns_verbatim_quote_when_in_haystack():
    """Quote present verbatim in verification_text → returned as-is."""
    content = _verification_payload("AI for medical imaging")
    haystack = "Title: A cancer call\n\nSummary: AI for medical imaging in cancer screening"
    parsed = parse_response(content, verification_text=haystack)
    assert parsed.topic_fit_quoted == "AI for medical imaging"


def test_parse_response_verification_nulls_quote_when_not_in_haystack():
    """Quote absent from verification_text → None."""
    content = _verification_payload("fabricated quote that does not exist")
    haystack = "Title: A cancer call\n\nSummary: real text from the call"
    parsed = parse_response(content, verification_text=haystack)
    assert parsed.topic_fit_quoted is None


def test_parse_response_verification_is_case_insensitive():
    """Case mismatch between LLM quote and source → still matched."""
    content = _verification_payload("CANCER BIOMARKER")
    haystack = "Funding for cancer biomarker discovery in pathology"
    parsed = parse_response(content, verification_text=haystack)
    # Returned quote keeps the LLM's original capitalization
    assert parsed.topic_fit_quoted == "CANCER BIOMARKER"


def test_parse_response_verification_tolerates_whitespace_normalization():
    """Codex finding: PDF-derived full_text has hard line breaks; LLM quotes
    are normalized. Substring check must match across whitespace differences."""
    content = _verification_payload("imaging in cancer screening")
    # Haystack has the same content with hard line breaks + NBSP — common in
    # PDF-extracted text. Without whitespace normalization, the substring
    # check would falsely null the (legitimate) quote.
    haystack = "AI for medical\nimaging in cancer\xa0screening and treatment"
    parsed = parse_response(content, verification_text=haystack)
    assert parsed.topic_fit_quoted == "imaging in cancer screening"
