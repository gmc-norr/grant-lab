"""Numbering must be proposal-wide: the same reference keeps its number in the
full render AND in a single-section fragment, and a corrupt ledger never breaks
the proposal render."""


def test_numbering_is_stable_per_section_render(make_workspace_repo):
    settings, wid = make_workspace_repo(
        proposal=(
            "## A\nClaim [@wang2016]<!-- ev:ev-aaaaaa -->.\n\n"
            "## B\nLater [@liu2017]<!-- ev:ev-bbbbbb --> and "
            "again [@wang2016]<!-- ev:ev-cccccc -->.\n"
        ),
        ledger={
            "version": 2,
            "evidence": [
                {"id": "ev-aaaaaa", "citekey": "wang2016", "reference_id": "R1"},
                {"id": "ev-bbbbbb", "citekey": "liu2017", "reference_id": "R2"},
                {"id": "ev-cccccc", "citekey": "wang2016", "reference_id": "R1"},
            ],
        },
    )
    from call_manager.proposal_assist.web.workspaces import _artifact_sections_and_rendered

    raw, rendered, sections = _artifact_sections_and_rendered(settings, wid)
    section_b = next(s for s in sections if s["heading"] == "B")
    assert f'/workspaces/{wid}/citations/ev-cccccc/reader">[1]</a>' in section_b["rendered"]
    assert f'/workspaces/{wid}/citations/ev-bbbbbb/reader">[2]</a>' in section_b["rendered"]


def test_corrupt_ledger_renders_proposal_with_inert_markers(make_workspace_repo):
    settings, wid = make_workspace_repo(
        proposal="Claim [@wang2016]<!-- ev:ev-aaaaaa -->.",
        ledger_raw="{ this is not json",
    )
    from call_manager.proposal_assist.web.workspaces import _artifact_sections_and_rendered

    raw, rendered, sections = _artifact_sections_and_rendered(settings, wid)
    assert "Claim" in rendered
    assert 'class="cite"' not in rendered
    assert "[@wang2016]" in rendered
