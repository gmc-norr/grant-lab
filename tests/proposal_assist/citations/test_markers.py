"""Tests for inline citation markers (pure string helpers).

TDD-first: written before markers.py exists. All tests must fail on first run,
then pass after implementation. No database, no file I/O, no git.
"""

from __future__ import annotations

import pytest

from call_manager.proposal_assist.citations.markers import (
    AnchorError,
    build_marker,
    find_markers,
    insert_marker,
)

# ---------------------------------------------------------------------------
# build_marker — validation guards
# ---------------------------------------------------------------------------


def test_build_marker_invalid_citekey_raises() -> None:
    """A citekey containing ']' (or any disallowed char) raises ValueError."""
    with pytest.raises(ValueError, match="invalid citekey"):
        build_marker("abc]def", "ev-000000000001")


def test_build_marker_invalid_ev_id_raises() -> None:
    """An ev_id that does not match ev-<6+hex> raises ValueError."""
    with pytest.raises(ValueError, match="invalid ev_id"):
        build_marker("ok2021", "nope")


def test_build_marker_ev_id_too_short_raises() -> None:
    """An ev_id with fewer than 6 hex chars after 'ev-' raises ValueError."""
    with pytest.raises(ValueError, match="invalid ev_id"):
        build_marker("ok2021", "ev-1234")


# ---------------------------------------------------------------------------
# insert_marker — validation propagates from build_marker
# ---------------------------------------------------------------------------


def test_insert_marker_invalid_citekey_raises_no_mutation() -> None:
    """insert_marker propagates ValueError from build_marker; document is not mutated."""
    text = "Some claim text."
    with pytest.raises(ValueError, match="invalid citekey"):
        insert_marker(text, "claim text", "bad]key", "ev-000000000001")


# ---------------------------------------------------------------------------
# build_marker — valid cases (existing tests continue to pass)
# ---------------------------------------------------------------------------


def test_build_marker_format() -> None:
    result = build_marker("smith2021", "ev-a13f2c91e0b4")
    assert result == "[@smith2021]<!-- ev:ev-a13f2c91e0b4 -->"


def test_build_marker_ref_citekey() -> None:
    """ref-<hex> style citekeys must be supported."""
    result = build_marker("ref-a1b2c3d4e5f6", "ev-000000000001")
    assert result == "[@ref-a1b2c3d4e5f6]<!-- ev:ev-000000000001 -->"


def test_build_marker_citekey_with_trailing_letter_suffix() -> None:
    """Citekeys like smith2021a (trailing letter suffix) must be supported."""
    result = build_marker("smith2021a", "ev-abcdef123456")
    assert result == "[@smith2021a]<!-- ev:ev-abcdef123456 -->"


# ---------------------------------------------------------------------------
# find_markers
# ---------------------------------------------------------------------------


def test_find_markers_empty_string() -> None:
    assert find_markers("") == []


def test_find_markers_no_markers() -> None:
    assert find_markers("No citations here. Plain text.") == []


def test_find_markers_single() -> None:
    text = "Reduces recurrence by 30% [@smith2021]<!-- ev:ev-a13f2c91e0b4 -->."
    result = find_markers(text)
    assert len(result) == 1
    assert result[0]["citekey"] == "smith2021"
    assert result[0]["ev_id"] == "ev-a13f2c91e0b4"


def test_find_markers_multiple_in_order() -> None:
    text = (
        "First claim [@jones2020]<!-- ev:ev-000000000001 --> "
        "and second claim [@smith2021]<!-- ev:ev-000000000002 -->."
    )
    result = find_markers(text)
    assert len(result) == 2
    assert result[0]["citekey"] == "jones2020"
    assert result[0]["ev_id"] == "ev-000000000001"
    assert result[1]["citekey"] == "smith2021"
    assert result[1]["ev_id"] == "ev-000000000002"
    # document order: first starts before second
    assert result[0]["start"] < result[1]["start"]


def test_find_markers_same_citekey_different_ev_ids() -> None:
    """Two uses of the same citekey must be individually addressable."""
    text = (
        "Claim A [@smith2021]<!-- ev:ev-aaaaaaaaaaaa --> "
        "and Claim B [@smith2021]<!-- ev:ev-bbbbbbbbbbbb -->."
    )
    result = find_markers(text)
    assert len(result) == 2
    assert result[0]["citekey"] == "smith2021"
    assert result[1]["citekey"] == "smith2021"
    assert result[0]["ev_id"] != result[1]["ev_id"]


def test_find_markers_start_end_positions() -> None:
    text = "X[@a]<!-- ev:ev-000000000001 -->Y"
    result = find_markers(text)
    assert len(result) == 1
    assert result[0]["start"] == 1
    assert result[0]["end"] == len(text) - 1  # before "Y"
    assert text[result[0]["start"] : result[0]["end"]] == "[@a]<!-- ev:ev-000000000001 -->"


def test_find_markers_ref_style_citekey() -> None:
    text = "See [@ref-a1b2c3d4e5f6]<!-- ev:ev-cccccccccccc -->."
    result = find_markers(text)
    assert len(result) == 1
    assert result[0]["citekey"] == "ref-a1b2c3d4e5f6"


def test_find_markers_adjacent_no_separator() -> None:
    """Two markers with no whitespace between them must both be returned in order."""
    m1 = "[@jones2020]<!-- ev:ev-000000000001 -->"
    m2 = "[@smith2021]<!-- ev:ev-000000000002 -->"
    text = f"Claim {m1}{m2} done."
    result = find_markers(text)
    assert len(result) == 2
    assert result[0]["citekey"] == "jones2020"
    assert result[1]["citekey"] == "smith2021"
    assert result[0]["start"] < result[1]["start"]


# ---------------------------------------------------------------------------
# insert_marker
# ---------------------------------------------------------------------------


def test_insert_marker_appends_immediately_after_anchor() -> None:
    text = "The treatment reduces recurrence by 30%."
    anchor = "reduces recurrence by 30%"
    result = insert_marker(text, anchor, "smith2021", "ev-a13f2c91e0b4")
    expected = "The treatment reduces recurrence by 30%[@smith2021]<!-- ev:ev-a13f2c91e0b4 -->."
    assert result == expected


def test_insert_marker_no_space_inserted_between_anchor_and_marker() -> None:
    """The marker sits immediately after the anchor with no added whitespace."""
    text = "claim text."
    result = insert_marker(text, "claim text", "jones2020", "ev-000000000001")
    assert result == "claim text[@jones2020]<!-- ev:ev-000000000001 -->."


def test_insert_marker_anchor_not_found_raises() -> None:
    with pytest.raises(AnchorError, match="not found"):
        insert_marker("Hello world.", "missing phrase", "k", "ev-000000000001")


def test_insert_marker_anchor_ambiguous_raises() -> None:
    text = "the cat sat near the cat."
    with pytest.raises(AnchorError, match="ambiguous"):
        insert_marker(text, "the cat", "k", "ev-000000000001")


def test_insert_marker_empty_anchor_raises() -> None:
    with pytest.raises(AnchorError):
        insert_marker("some text", "", "k", "ev-000000000001")


def test_insert_marker_preserves_rest_of_document() -> None:
    text = "Intro sentence. Target sentence ends here. Another sentence."
    anchor = "Target sentence ends here"
    result = insert_marker(text, anchor, "ref-abcdef123456", "ev-111111111111")
    assert result.startswith("Intro sentence. ")
    assert result.endswith(". Another sentence.")
    assert "[@ref-abcdef123456]<!-- ev:ev-111111111111 -->" in result


def test_insert_marker_anchor_only_inside_existing_marker_raises() -> None:
    """An anchor that appears only inside an existing marker span is ignored;
    the function must raise AnchorError('anchor not found')."""
    # "smith2021" appears solely as part of [@smith2021]<!-- ev:ev-aaaaaaaaaaaa -->
    text = "Some prose [@smith2021]<!-- ev:ev-aaaaaaaaaaaa --> more prose."
    with pytest.raises(AnchorError, match="not found"):
        insert_marker(text, "smith2021", "jones2022", "ev-bbbbbbbbbbbb")


def test_insert_marker_anchor_in_prose_and_unrelated_marker_substring() -> None:
    """Anchor that occurs once in prose inserts correctly even when an unrelated
    marker also contains a matching substring (the marker occurrence is ignored)."""
    # "smith2021" appears in prose AND inside an unrelated marker for jones2020
    # that happens to have "smith2021" in its ev comment — we use a citekey that
    # shares the anchor text only inside the marker.
    text = "See smith2021 for details [@smith2021]<!-- ev:ev-aaaaaaaaaaaa --> already cited."
    # "smith2021" occurs once in prose (before "for details") and once inside
    # the existing marker — the marker occurrence must be ignored, leaving
    # exactly one valid occurrence → insert succeeds.
    result = insert_marker(text, "smith2021", "jones2022", "ev-bbbbbbbbbbbb")
    expected_new_marker = "[@jones2022]<!-- ev:ev-bbbbbbbbbbbb -->"
    assert expected_new_marker in result
    # The new marker is placed right after the prose "smith2021"
    prose_idx = result.index("smith2021")
    anchor_end = prose_idx + len("smith2021")
    assert result[anchor_end : anchor_end + len(expected_new_marker)] == expected_new_marker


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------


def test_round_trip_insert_and_find() -> None:
    """Insert two markers (same citekey, different ev ids) into a paragraph;
    find_markers returns both, in document order, each individually addressable
    by its own ev id."""
    paragraph = (
        "The treatment significantly reduces recurrence. "
        "Moreover, survival rates improved markedly."
    )
    ev1 = "ev-111111111111"
    ev2 = "ev-222222222222"
    citekey = "smith2021"

    # Insert first marker after first sentence anchor
    text = insert_marker(paragraph, "reduces recurrence", citekey, ev1)
    # Insert second marker after second sentence anchor
    text = insert_marker(text, "improved markedly", citekey, ev2)

    # Both markers present, in document order, individually addressable by ev id
    markers = find_markers(text)
    assert len(markers) == 2
    assert [m["ev_id"] for m in markers] == [ev1, ev2]
    assert all(m["citekey"] == citekey for m in markers)
