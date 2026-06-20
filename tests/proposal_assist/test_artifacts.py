from call_manager.proposal_assist.artifacts import (
    PROPOSAL_FILENAME,
    SPEC_FILENAME,
    Artifact,
    filename_for,
)


def test_filename_for_each_artifact():
    assert filename_for(Artifact.DRAFT) == "proposal.md"
    assert filename_for(Artifact.SPEC) == "proposal-spec.md"


def test_enum_values_match_proposed_change_check():
    # These string values are persisted in ProposedChange.target_artifact and
    # must match the DB CHECK ('draft','spec') — db/models.py:958.
    assert Artifact.DRAFT == "draft"
    assert Artifact.SPEC == "spec"


def test_strenum_normalizes_a_raw_string():
    # A raw "draft" string round-trips to the member (used by save_section's
    # entry normalization so a stringly-typed caller can't skip Tier-1/Tier-2).
    assert Artifact("draft") is Artifact.DRAFT
    assert str(Artifact.SPEC) == "spec"


def test_back_compat_constants():
    assert PROPOSAL_FILENAME == "proposal.md"
    assert SPEC_FILENAME == "proposal-spec.md"
