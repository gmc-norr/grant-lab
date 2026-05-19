import json
from unittest.mock import Mock

import pytest

from call_monitor.proposal_assist.extractors.rubric import (
    extract_rubric,
)


def _envelope(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "model": "claude-opus-4-7",
    }


def test_extract_rubric_returns_criteria_and_notes():
    payload = {
        "criteria": [
            {
                "name": "Excellence",
                "description": "Scientific quality",
                "weight": 0.5,
                "scale_max": 5,
            },
            {
                "name": "Impact",
                "description": "Translational potential",
                "weight": 0.5,
                "scale_max": 5,
            },
        ],
        "funder_notes": "Note: late submissions rejected.",
    }
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"

    result = extract_rubric(call_text="dummy", client=client)

    assert result["funder_notes"] == "Note: late submissions rejected."
    assert len(result["criteria"]) == 2
    assert result["criteria"][0]["name"] == "Excellence"


def test_extract_rubric_defaults_funder_notes_to_empty_string():
    payload = {
        "criteria": [
            {"name": "X", "description": "x", "weight": 1.0, "scale_max": 5},
        ],
    }
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"

    result = extract_rubric(call_text="dummy", client=client)

    assert result["funder_notes"] == ""


def test_extract_rubric_raises_on_malformed_json():
    client = Mock()
    client.chat.return_value = _envelope("not json")
    client.model = "claude-opus-4-7"
    with pytest.raises(ValueError, match="malformed JSON"):
        extract_rubric(call_text="dummy", client=client)


def test_extract_rubric_raises_on_empty_criteria():
    payload = {"criteria": [], "funder_notes": ""}
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"
    with pytest.raises(ValueError, match="at least one criterion"):
        extract_rubric(call_text="dummy", client=client)


def test_extract_rubric_raises_on_missing_required_criterion_fields():
    payload = {
        "criteria": [{"name": "Only a name"}],  # missing description/weight/scale_max
        "funder_notes": "",
    }
    client = Mock()
    client.chat.return_value = _envelope(json.dumps(payload))
    client.model = "claude-opus-4-7"
    with pytest.raises(ValueError, match="missing required field"):
        extract_rubric(call_text="dummy", client=client)


def test_extract_rubric_raises_on_bad_envelope_shape():
    client = Mock()
    client.chat.return_value = {}  # missing "choices"
    client.model = "claude-opus-4-7"

    with pytest.raises(ValueError, match="unexpected envelope shape"):
        extract_rubric(call_text="dummy", client=client)
