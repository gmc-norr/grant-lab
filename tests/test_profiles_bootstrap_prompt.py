import json

from call_manager.profiles.bootstrap_prompt import (
    BOOTSTRAP_PROMPT_VERSION,
    build_bootstrap_messages,
    parse_bootstrap_response,
)
from call_manager.profiles.seed import Publication

_FAKE_PUBS = [
    Publication(pmid="1", title="AI for digital pathology", year=2024, authors=["Smith J"]),
    Publication(pmid="2", title="Federated learning in radiology", year=2025, authors=["Smith J"]),
]


def test_prompt_version_is_v1():
    assert BOOTSTRAP_PROMPT_VERSION == "v1"


def test_messages_include_publications_and_grants():
    msgs = build_bootstrap_messages(
        profile_name="Pathology AI",
        kind="group",
        publications=_FAKE_PUBS,
        pasted_grants="Past grant: digital pathology pilot 2023",
    )
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "Pathology AI" in msgs[0]["content"]
    assert "AI for digital pathology" in msgs[1]["content"]
    assert "digital pathology pilot 2023" in msgs[1]["content"]


def test_parse_happy_path():
    content = json.dumps(
        {
            "description": "Research group on AI for digital pathology.",
            "areas": ["digital pathology", "federated learning"],
            "budget_min": 100000,
            "budget_max": 5000000,
            "trl_min": 4,
            "trl_max": 8,
            "must_include": ["pathology"],
            "hard_exclusions": ["animal studies"],
        }
    )
    out = parse_bootstrap_response(content)
    assert out.description.startswith("Research group")
    assert out.structured["areas"] == ["digital pathology", "federated learning"]
    assert out.structured["budget_min"] == 100000
    assert out.structured["must_include"] == ["pathology"]


def test_parse_missing_optional_fields_uses_defaults():
    content = json.dumps({"description": "Bare draft.", "areas": []})
    out = parse_bootstrap_response(content)
    assert out.description == "Bare draft."
    # Missing numeric fields → None; missing list fields → []
    assert out.structured.get("budget_min") is None
    assert out.structured.get("must_include") == []
    assert out.structured.get("hard_exclusions") == []


def test_parse_invalid_json_raises():
    import pytest

    from call_manager.profiles.bootstrap_prompt import BootstrapParseError

    with pytest.raises(BootstrapParseError):
        parse_bootstrap_response("not json")
