from call_manager.proposal_assist.review.personas import (
    PERSONA_KINDS,
    REVIEW_KINDS,
    WAVE1_KINDS,
    persona_prompt,
)


def test_kind_sets():
    assert set(REVIEW_KINDS) == {
        "fit_check", "persona_domain_skeptic", "persona_methodology_critic",
        "persona_translation_skeptic", "persona_devils_advocate", "consolidation",
    }
    assert "fit_check" not in PERSONA_KINDS and len(PERSONA_KINDS) == 4
    assert ("fit_check", *PERSONA_KINDS) == WAVE1_KINDS


def test_every_kind_has_a_prompt_with_submit_review():
    for kind in REVIEW_KINDS:
        p = persona_prompt(kind)
        assert p and "submit_review" in p
