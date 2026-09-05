from grant_lab.proposal_assist.citations.reader import locate_evidence

FULL = (
    "Intro paragraph about methods.\n\n"
    "The system achieved an AUC of 0.925 for whole-slide image classification.\n\n"
    "Later the AUC of 0.925 is mentioned again in a different sentence.\n"
)
CHUNK = "The system achieved an AUC of 0.925 for whole-slide image classification."
QUOTE = "AUC of 0.925 for whole-slide\nimage classification"  # newline like a PDF


def _span(full, se):
    s, e = se
    return full[s:e]


def test_locates_via_chunk_anchor_even_when_quote_is_duplicated():
    se = locate_evidence(FULL, CHUNK, QUOTE)
    assert se is not None
    assert _span(FULL, se).replace("\n", " ") == "AUC of 0.925 for whole-slide image classification"
    assert se[0] < FULL.index("Later")


def test_missing_chunk_falls_back_to_unique_quote_search():
    uniq = "AUC of 0.925 for whole-slide image classification"
    full = "Only one mention: " + uniq + " here."
    se = locate_evidence(full, None, uniq)
    assert se is not None and _span(full, se) == uniq


def test_ambiguous_quote_without_usable_chunk_returns_none():
    assert locate_evidence(FULL, None, "AUC of 0.925") is None


def test_chunk_ambiguous_in_full_text_falls_back_to_unique_quote():
    # chunk appears twice; we cannot anchor by it, but the quote is unique in
    # full_text -> path 2 should locate it correctly.
    repeated_chunk = "Methods section text."
    full = (
        repeated_chunk + "\n\n"
        "The sensitivity was 0.97 in this cohort.\n\n"
        + repeated_chunk + "\n"
    )
    quote = "sensitivity was 0.97 in this cohort"
    se = locate_evidence(full, repeated_chunk, quote)
    assert se is not None
    assert _span(full, se).replace("\n", " ") == "sensitivity was 0.97 in this cohort"


def test_quote_absent_returns_none():
    assert locate_evidence(FULL, CHUNK, "not present anywhere") is None
