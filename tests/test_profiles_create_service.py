from call_manager.profiles.create import merge_bootstrap_into_profile
from call_manager.profiles.seed import DraftProfile


class _P:
    def __init__(self, kind, description, structured):
        self.kind, self.description, self.structured = kind, description, structured


def test_merge_fills_empty_and_keeps_user_values():
    p = _P("organizational_unit", "", {"areas": ["oncology"], "must_include": ["x"]})
    draft = DraftProfile(
        description="Drafted.",
        structured={
            "areas": ["genomics"],
            "must_include": ["y"],
            "budget_min": 100000,
            "trl_max": 7,
            "hard_exclusions": ["defence"],
            "geo": {"country": "Sweden"},
        },
        publications_fetched=2,
        publications_skipped=0,
    )
    merge_bootstrap_into_profile(p, draft)
    assert p.description == "Drafted."
    assert p.structured["areas"] == ["oncology"]  # user wins
    assert p.structured["must_include"] == ["x"]  # user wins
    assert p.structured["budget_min"] == 100000  # filled from draft (rubric scalar)
    assert p.structured["trl_max"] == 7  # filled from draft
    assert p.structured["hard_exclusions"] == ["defence"]  # filled from draft (rubric list)
    assert p.structured["geo"]["country"] == "Sweden"  # filled from draft namespace (sanitized)


def test_kind_inappropriate_namespace_field_is_dropped():
    # career_stage is individual-only; on an organizational_unit it must NOT be persisted.
    p = _P("organizational_unit", "", {"areas": []})
    draft = DraftProfile(
        description="D.",
        structured={"eligibility": {"career_stage": "senior"}},
        publications_fetched=0,
        publications_skipped=0,
    )
    merge_bootstrap_into_profile(p, draft)
    assert "career_stage" not in p.structured.get("eligibility", {})


def test_merge_does_not_overwrite_nonempty_description():
    p = _P("organizational_unit", "User wrote this.", {"areas": []})
    draft = DraftProfile(
        description="LLM draft.",
        structured={"areas": ["x"]},
        publications_fetched=0,
        publications_skipped=0,
    )
    merge_bootstrap_into_profile(p, draft)
    assert p.description == "User wrote this."
    assert p.structured["areas"] == ["x"]
