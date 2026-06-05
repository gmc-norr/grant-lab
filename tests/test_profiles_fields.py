import pytest

from call_manager.profiles.fields import (
    APPLICANT_TYPES,
    CAREER_STAGES,
    FUNDING_INSTRUMENTS,
    LEVELS,
    MAX_SHORT_TEXT,
    MAX_TAG_LEN,
    ORG_TYPES,
    FieldValidationError,
    parse_structured,
)


def test_allowlists_are_frozen_and_nonempty():
    for s in (APPLICANT_TYPES, CAREER_STAGES, ORG_TYPES, FUNDING_INSTRUMENTS, LEVELS):
        assert isinstance(s, frozenset) and s


def test_parse_structured_individual_happy_path():
    out = parse_structured(
        kind="individual",
        raw={
            "areas": "oncology\nimaging",
            "career_stage": "postdoc",
            "applicant_types": "solo_pi\nconsortium_partner",
            "role": "staff scientist",
            "years_since_phd": "4",
            "funding_instruments": "fellowship",
        },
    )
    assert out["areas"] == ["oncology", "imaging"]
    assert out["eligibility"]["career_stage"] == "postdoc"
    assert out["eligibility"]["applicant_types"] == ["solo_pi", "consortium_partner"]
    assert out["eligibility"]["role"] == "staff scientist"
    assert out["eligibility"]["years_since_phd"] == 4
    assert out["funding"]["instruments"] == ["fellowship"]


def test_parse_structured_rejects_unknown_enum():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(kind="individual", raw={"career_stage": "wizard"})
    assert e.value.status_code == 400


def test_parse_structured_rejects_overlong_tag():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(kind="organizational_unit", raw={"capabilities": "x" * (MAX_TAG_LEN + 1)})
    assert e.value.status_code == 400


def test_parse_structured_rejects_overlong_short_text():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(kind="organizational_unit", raw={"scale": "y" * (MAX_SHORT_TEXT + 1)})
    assert e.value.status_code == 413


def test_parse_structured_rejects_years_out_of_range():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(kind="individual", raw={"years_since_phd": "999"})
    assert e.value.status_code == 400


def test_parse_structured_individual_drops_org_namespace():
    out = parse_structured(kind="individual", raw={"capabilities": "biobank"})
    assert "org" not in out or not out["org"].get("capabilities")


def test_parse_structured_rejects_over_100_items():
    with pytest.raises(FieldValidationError) as e:
        parse_structured(
            kind="organizational_unit",
            raw={"capabilities": "\n".join(str(i) for i in range(101))},
        )
    assert e.value.status_code == 400


def test_sanitize_structured_drops_bad_llm_values():
    from call_manager.profiles.fields import sanitize_structured

    out = sanitize_structured(
        kind="individual",
        structured={
            "eligibility": {"career_stage": "wizard", "applicant_types": ["solo_pi", "bogus"]},
            "funding": {"instruments": ["fellowship", "nope"]},
        },
    )
    assert out["eligibility"]["career_stage"] is None
    assert out["eligibility"]["applicant_types"] == ["solo_pi"]
    assert out["funding"]["instruments"] == ["fellowship"]
