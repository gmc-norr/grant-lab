"""Tests for the canonical claim_sentence_around extractor.

This is the single source of truth used by insert_citation,
update_citation(reaccept_claim), and Slice-3 validate_citations. The marker
span [marker_start, marker_end) is computed by embedding a sentinel marker in
the text so the offsets match what find_markers would report in real prose.
"""

from grant_lab.proposal_assist.citations.claims import (
    claim_sentence_around,
    iter_proposal_sentences,
)

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


def test_marker_right_after_heading_excludes_heading():
    # Defect B7-2: a cite immediately after `## Aims` must NOT absorb the
    # heading markup as its claim — the backward scan stops at the heading line.
    assert _around("## Aims\n", " trailing.") == ""


def test_marker_after_heading_then_prose_keeps_only_prose():
    # Heading, then real claim prose on the next line, then the marker: the
    # claim is the prose, never the heading.
    assert _around("## Aims\nWe will cure cancer", ". Next.") == "We will cure cancer"


def test_marker_after_atx_heading_with_indent_excludes_heading():
    assert _around("  ### Background\n", " more.") == ""


# --- iter_proposal_sentences ------------------------------------------------


def _sentences(text: str) -> list[str]:
    return [s for s, _ in iter_proposal_sentences(text)]


def test_iter_skips_heading_lines():
    text = "# Title\n## Section\nThe drug reduces recurrence."
    assert _sentences(text) == ["The drug reduces recurrence"]


def test_iter_skips_indented_heading():
    text = "   # Indented heading\nReal claim here."
    assert _sentences(text) == ["Real claim here"]


def test_iter_skips_fenced_code_block():
    text = "Before code.\n```\ncode line one.\ncode line two!\n```\nAfter code."
    assert _sentences(text) == ["Before code", "After code"]


def test_iter_skips_fenced_code_block_with_language():
    text = "Intro.\n```python\nprint('not a sentence.')\n```\nOutro."
    assert _sentences(text) == ["Intro", "Outro"]


def test_iter_multi_sentence_paragraph_yields_each():
    text = "First claim. Second claim! Third claim?"
    assert _sentences(text) == ["First claim", "Second claim", "Third claim"]


def test_iter_offsets_index_original_text():
    text = "First claim. Second claim here."
    results = list(iter_proposal_sentences(text))
    for sentence, offset in results:
        # The first word of the stripped sentence appears at the offset.
        first_word = sentence.split(" ", 1)[0]
        assert text[offset : offset + len(first_word)] == first_word


def test_iter_offset_after_heading_points_into_original():
    text = "# Heading\nThe claim begins here."
    ((sentence, offset),) = iter_proposal_sentences(text)
    assert sentence == "The claim begins here"
    assert text[offset:].startswith("The claim begins here")


def test_iter_strips_marker_from_sentence_text():
    text = f"Recurrence is reduced{MARKER}. Next sentence."
    sentences = _sentences(text)
    assert sentences[0] == "Recurrence is reduced"
    assert MARKER not in sentences[0]


def test_iter_skips_marker_only_fragment():
    # A fragment that is only a marker (between terminators) is dropped.
    text = f"A real claim. {MARKER}. Another claim."
    assert _sentences(text) == ["A real claim", "Another claim"]


def test_iter_skips_whitespace_and_punctuation_only():
    text = "Real one.   \n...\n!!!\nReal two."
    assert _sentences(text) == ["Real one", "Real two"]


def test_iter_empty_text_yields_nothing():
    assert list(iter_proposal_sentences("")) == []


def test_iter_boundary_agreement_with_claim_sentence_around():
    # The sentence iter yields for a marker-bearing sentence must match what
    # claim_sentence_around extracts for that same marker (shared _TERMINATORS).
    prefix = "First sentence. We argue the effect is large"
    suffix = ". Then more."
    text = prefix + MARKER + suffix
    via_around = claim_sentence_around(text, len(prefix), len(prefix) + len(MARKER))
    iter_sentences = _sentences(text)
    assert via_around in iter_sentences
    assert via_around == "We argue the effect is large"


def test_iter_offset_skips_leading_marker_to_prose():
    # A non-trailing marker preceding the prose: the offset must point at the
    # first prose char ('P'), not the marker's '['.
    from grant_lab.proposal_assist.citations.markers import build_marker

    marker = build_marker("k", "ev-000000000000")
    text = f"First sentence. {marker} Prose after marker is here."
    out = list(iter_proposal_sentences(text))
    # Find the prose sentence.
    prose = next((s, off) for s, off in out if s.startswith("Prose after marker"))
    sentence_text, offset = prose
    assert text[offset:].startswith("Prose after marker"), (offset, text[offset : offset + 10])
