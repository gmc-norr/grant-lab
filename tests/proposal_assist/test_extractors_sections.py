import json
from unittest.mock import Mock

import pytest

from call_monitor.proposal_assist.extractors.sections import (
    SECTION_NAMES,
    extract_sections,
)


def _envelope(content: str) -> dict:
    """Build the OpenAI-shaped envelope AnthropicClient.chat returns."""
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "model": "claude-opus-4-7",
    }


def test_extract_sections_returns_six_named_keys():
    payload = {section: f"text for {section}" for section in SECTION_NAMES}
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"

    result = extract_sections(call_text="dummy call body", client=client)

    assert set(result.keys()) == set(SECTION_NAMES)
    for section, text in result.items():
        assert text == f"text for {section}"


def test_extract_sections_fills_missing_sections_with_empty_string():
    # Real LLM output sometimes omits a section entirely; the extractor must
    # backfill so callers always see all six keys.
    partial = {"eligibility": "stuff"}
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(partial))
    client.model = "claude-opus-4-7"

    result = extract_sections(call_text="dummy", client=client)

    assert result["eligibility"] == "stuff"
    assert all(result[s] == "" for s in SECTION_NAMES if s != "eligibility")


def test_extract_sections_raises_on_malformed_json():
    client = Mock()
    client.chat.return_value = _envelope("not json at all")
    client.model = "claude-opus-4-7"

    with pytest.raises(ValueError, match="malformed JSON"):
        extract_sections(call_text="dummy", client=client)


def test_extract_sections_raises_on_non_str_value():
    client = Mock()
    client.chat.return_value = _envelope(json.dumps({"eligibility": 42}))
    client.model = "claude-opus-4-7"

    with pytest.raises(ValueError, match="expected str"):
        extract_sections(call_text="dummy", client=client)


def test_extract_sections_raises_on_bad_envelope_shape():
    client = Mock()
    client.chat.return_value = {}  # missing "choices"
    client.model = "claude-opus-4-7"

    with pytest.raises(ValueError, match="unexpected envelope shape"):
        extract_sections(call_text="dummy", client=client)


def test_extract_sections_passes_call_text_in_user_message():
    client = Mock()
    client.chat.return_value = _envelope(json.dumps({s: "" for s in SECTION_NAMES}))
    client.model = "claude-opus-4-7"

    extract_sections(call_text="UNIQUE-CALL-BODY-TEXT", client=client)

    messages = client.chat.call_args.kwargs["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    assert "UNIQUE-CALL-BODY-TEXT" in user_msg["content"]
