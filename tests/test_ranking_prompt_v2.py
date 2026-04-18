import json

import pytest

from call_monitor.ranking.prompt import (
    PROMPT_VERSION_V2,
    ParsedV2,
    ParseV2Error,
    build_messages_v2,
    parse_v2_response,
)


def test_prompt_version_v2_constant():
    assert PROMPT_VERSION_V2 == "v2.0"


def test_build_messages_v2_includes_profile_and_call_blocks():
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
    msgs = build_messages_v2(profile=profile, call=call)
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


def test_build_messages_v2_truncates_call_text_at_4000():
    long_summary = "x" * 6000
    profile = type("P", (), {
        "name": "P", "kind": type("K", (), {"value": "group"})(),
        "description": "", "structured": {"areas": [], "hard_exclusions": []},
    })()
    call = type("C", (), {
        "title": "T", "summary": long_summary, "programme": None,
        "deadline": None, "budget_min": None, "budget_max": None, "currency": None,
        "raw": {},
    })()
    msgs = build_messages_v2(profile=profile, call=call)
    # Count the x's in the user message — should be capped at 4000.
    assert msgs[1]["content"].count("x") <= 4000


def test_build_messages_v2_falls_back_to_full_text_when_summary_missing():
    """M1 regression: calls with full_text but no summary must still get
    their body in the prompt — v1 does this, v2 must too."""
    profile = type("P", (), {
        "name": "P", "kind": type("K", (), {"value": "group"})(),
        "description": "", "structured": {"areas": [], "hard_exclusions": []},
    })()
    call = type("C", (), {
        "title": "T",
        "summary": None,
        "full_text": "Detailed eligibility criteria buried in the body.",
        "programme": None,
        "deadline": None, "budget_min": None, "budget_max": None, "currency": None,
        "raw": {},
    })()
    msgs = build_messages_v2(profile=profile, call=call)
    assert "Detailed eligibility criteria" in msgs[1]["content"]


def test_parse_v2_clean_response():
    content = json.dumps({
        "topic_fit": {"rating": 4, "rationale": "good", "quoted_evidence": "AI for medical"},
        "eligibility": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
        "exclusions": {"rating": "pass", "rationale": "ok", "quoted_evidence": None},
    })
    out = parse_v2_response(content)
    assert isinstance(out, ParsedV2)
    assert out.topic_fit_rating == 4
    assert out.topic_fit_rationale == "good"
    assert out.topic_fit_quoted == "AI for medical"
    assert out.eligibility_rating == "pass"
    assert out.exclusions_rating == "pass"


def test_parse_v2_invalid_json_raises():
    with pytest.raises(ParseV2Error):
        parse_v2_response("not json")


def test_parse_v2_missing_topic_fit_raises():
    content = json.dumps({"eligibility": {"rating": "pass"}, "exclusions": {"rating": "pass"}})
    with pytest.raises(ParseV2Error, match="topic_fit"):
        parse_v2_response(content)


def test_parse_v2_invalid_topic_rating_raises():
    content = json.dumps({
        "topic_fit": {"rating": 9, "rationale": "x"},
        "eligibility": {"rating": "pass"},
        "exclusions": {"rating": "pass"},
    })
    with pytest.raises(ParseV2Error, match="topic_fit.rating"):
        parse_v2_response(content)


def test_parse_v2_invalid_eligibility_rating_raises():
    content = json.dumps({
        "topic_fit": {"rating": 3},
        "eligibility": {"rating": "maybe"},
        "exclusions": {"rating": "pass"},
    })
    with pytest.raises(ParseV2Error, match="eligibility"):
        parse_v2_response(content)
