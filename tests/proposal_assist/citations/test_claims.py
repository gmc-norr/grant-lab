"""Tests for the canonical claim_sentence_around extractor.

This is the single source of truth used by insert_citation,
update_citation(reaccept_claim), and Slice-3 validate_citations. The marker
span [marker_start, marker_end) is computed by embedding a sentinel marker in
the text so the offsets match what find_markers would report in real prose.
"""

from call_manager.proposal_assist.citations.claims import claim_sentence_around

MARKER = "[@k]<!-- ev:ev-000000000000 -->"


def _around(prefix: str, suffix: str) -> str:
    """Place MARKER between prefix and suffix and extract the claim around it."""
    text = prefix + MARKER + suffix
    return claim_sentence_around(text, len(prefix), len(prefix) + len(MARKER))


def test_marker_trails_sentence_no_terminator_before_marker():
    # "Recurrence is reduced[M]. Next one."
    assert _around("Recurrence is reduced", ". Next one.") == "Recurrence is reduced"


def test_marker_after_period_uses_preceding_sentence():
    # I1 boundary bug: "Recurrence is reduced.[M] Next one." -> preceding sentence.
    assert _around("Recurrence is reduced.", " Next one.") == "Recurrence is reduced"


def test_marker_after_clause_midtext():
    # "First. We argue X[M]. Then Y." -> "We argue X" (leading "First. " excluded).
    assert _around("First. We argue X", ". Then Y.") == "We argue X"


def test_marker_at_very_start_returns_empty():
    assert _around("", " Next one.") == ""


def test_marker_after_only_terminators_returns_empty():
    assert _around(".  ", " Next.") == ""


def test_collapses_internal_whitespace():
    # Multiple spaces/tabs inside the claim collapse to single spaces. A newline
    # is a terminator (see test_newline_acts_as_terminator), so it is NOT used
    # here for the internal-collapse case.
    assert _around("We   argue \t X", ". Then Y.") == "We argue X"


def test_newline_acts_as_terminator():
    # A newline before the claim bounds the sentence start.
    assert _around("Intro line\nThe real claim", ". Next.") == "The real claim"


def test_question_mark_terminator():
    assert _around("Is it true? Yes it is", ". More.") == "Yes it is"


def test_exclamation_terminator_directly_before_marker():
    # Marker after a '!' -> preceding sentence.
    assert _around("It works!", " Next.") == "It works"


def test_marker_with_no_following_text():
    # Marker at the end of the document, claim ends with a period.
    assert _around("The claim is solid.", "") == "The claim is solid"


def test_whole_text_is_single_sentence_no_terminator():
    assert _around("A single bare claim", "") == "A single bare claim"
