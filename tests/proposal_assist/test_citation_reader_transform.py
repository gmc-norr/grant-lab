from grant_lab.proposal_assist.citations.reader import (
    build_citation_numbering,
    build_citation_stances,
    build_source_window,
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


def test_build_source_window_normal_doc_returns_full_text_and_span():
    full = "Intro paragraph.\n\nThe cat sat on the mat.\n\nConclusion.\n"
    chunk = "The cat sat on the mat."
    quote = "The cat sat on the mat."
    text, hl, note = build_source_window(full, chunk, quote)
    assert text == full
    assert hl is not None and full[hl[0] : hl[1]] == quote
    assert note is None


def test_build_source_window_large_doc_windows_around_span():
    quote = "NEEDLE SENTENCE."
    full = ("x" * 20_000) + quote + ("y" * 20_000)
    text, hl, note = build_source_window(full, None, quote, max_chars=1_000, window_radius=50)
    assert hl is not None and text[hl[0] : hl[1]] == quote
    assert len(text) <= 50 + len(quote) + 50
    assert note is not None and "large document" in note


def test_build_source_window_span_not_found_with_chunk_shows_chunk():
    full = "totally unrelated document body."
    chunk = "The cited section text."
    text, hl, note = build_source_window(full, chunk, "quote that is absent everywhere")
    assert text == chunk
    assert hl == (0, len(chunk))
    assert note is not None and "couldn't pinpoint" in note.lower()


def test_build_source_window_span_not_found_no_chunk_returns_head():
    full = "some document " * 50
    text, hl, note = build_source_window(full, None, "absent quote", window_radius=100)
    assert hl is None
    assert text == full[:200]
    assert note is not None and "couldn't pinpoint" in note.lower()


def test_build_source_window_text_absent_chunk_present():
    text, hl, note = build_source_window(None, "the cited chunk", "anything")
    assert text == "the cited chunk"
    assert hl == (0, len("the cited chunk"))
    assert note == "Showing the cited section."


def test_build_source_window_both_absent():
    assert build_source_window(None, None, "q") == ("", None, None)
