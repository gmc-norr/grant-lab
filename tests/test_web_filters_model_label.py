"""model_label maps raw model IDs to names a non-engineer can read.

Only one label was covered before (indirectly, via test_api_call_detail), so a
model bump could silently ship a raw ID into the UI. The fallback is deliberate
— unmapped models render as the raw string rather than "Unknown" — but the model
this app actually runs should always have an entry.
"""

import pytest

from call_manager.config import Settings
from call_manager.web.filters import model_label


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-opus-5", "Opus 5"),
        ("claude-opus-4-7", "Opus 4.7"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-sonnet-4-6", "Sonnet 4.6"),
        ("claude-haiku-4-5", "Haiku 4.5"),
    ],
)
def test_known_models_get_a_readable_label(model_id, expected):
    assert model_label(model_id) == expected


def test_unknown_model_falls_through_to_the_raw_id(recwarn):
    """Not "Unknown": a mislabelled backend is easier to spot in the UI as its
    raw ID. This pins that deliberate choice."""
    assert model_label("some-future-model-9") == "some-future-model-9"


def test_no_value_renders_an_em_dash():
    assert model_label(None) == "—"
    assert model_label("") == "—"


def test_the_configured_drafting_model_has_a_label():
    """The bump-time guard: whatever PROPOSAL_ASSIST_LLM_MODEL defaults to must be
    in the map, so raising the model never leaks a raw ID into the UI.

    Reads the DECLARED default off the field rather than instantiating Settings —
    no env vars, no database URL, and it asserts exactly what ships."""
    model = Settings.model_fields["proposal_assist_llm_model"].default
    assert model_label(model) != model, f"{model} has no entry in model_label's map"
