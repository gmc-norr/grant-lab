from call_manager.proposal_assist.citations.reader import (
    build_citation_numbering,
    build_citation_stances,
    evidence_order_for_reference,
)

LEDGER = {
    "version": 2,
    "evidence": [
        {"id": "ev-aaaaaa", "citekey": "wang2016", "reference_id": "R1"},
        {"id": "ev-bbbbbb", "citekey": "liu2017", "reference_id": "R2"},
        {"id": "ev-cccccc", "citekey": "wang2016", "reference_id": "R1"},
    ],
}
PROPOSAL = (
    "First claim [@wang2016]<!-- ev:ev-aaaaaa -->. "
    "Second [@liu2017]<!-- ev:ev-bbbbbb -->. "
    "Third reuses [@wang2016]<!-- ev:ev-cccccc -->."
)


def test_numbering_is_per_reference_document_order():
    nums = build_citation_numbering(PROPOSAL, LEDGER)
    assert nums == {"ev-aaaaaa": 1, "ev-bbbbbb": 2, "ev-cccccc": 1}


def test_numbering_excludes_markers_absent_from_ledger():
    text = PROPOSAL + " Bogus [@ghost]<!-- ev:ev-ffffff -->."
    nums = build_citation_numbering(text, LEDGER)
    assert "ev-ffffff" not in nums


def test_numbering_empty_on_empty_ledger():
    assert build_citation_numbering(PROPOSAL, {"version": 2, "evidence": []}) == {}


def test_evidence_order_for_reference_is_document_order():
    order = evidence_order_for_reference(PROPOSAL, LEDGER, "R1")
    assert order == ["ev-aaaaaa", "ev-cccccc"]
    assert evidence_order_for_reference(PROPOSAL, LEDGER, "R2") == ["ev-bbbbbb"]


def test_evidence_order_dedups_repeated_ev_id():
    # A duplicated marker id (data anomaly) must appear once, order preserved.
    text = (
        "A [@wang2016]<!-- ev:ev-aaaaaa -->. "
        "B [@wang2016]<!-- ev:ev-cccccc -->. "
        "Dup [@wang2016]<!-- ev:ev-aaaaaa -->."
    )
    assert evidence_order_for_reference(text, LEDGER, "R1") == ["ev-aaaaaa", "ev-cccccc"]


def test_build_citation_stances_maps_ev_to_stance():
    led = {
        "version": 2,
        "evidence": [
            {"id": "ev-aaaaaa", "citekey": "w", "reference_id": "R1", "stance": "supports"},
            {"id": "ev-bbbbbb", "citekey": "l", "reference_id": "R2"},
        ],
    }
    assert build_citation_stances(led) == {"ev-aaaaaa": "supports"}
