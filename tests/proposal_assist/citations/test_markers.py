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
    remove_marker,
    rename_marker_citekey,
)

# ---------------------------------------------------------------------------
# build_marker
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


# ---------------------------------------------------------------------------
# remove_marker
# ---------------------------------------------------------------------------


def test_remove_marker_removes_matching_ev_id() -> None:
    text = "Fact [@smith2021]<!-- ev:ev-aaaaaaaaaaaa --> is established."
    new_text, removed = remove_marker(text, "ev-aaaaaaaaaaaa")
    assert removed is True
    assert "[@smith2021]<!-- ev:ev-aaaaaaaaaaaa -->" not in new_text
    assert new_text == "Fact  is established."


def test_remove_marker_not_present_returns_false() -> None:
    text = "No markers here."
    new_text, removed = remove_marker(text, "ev-aaaaaaaaaaaa")
    assert removed is False
    assert new_text == text


def test_remove_marker_leaves_other_markers_intact() -> None:
    """Removing by ev id must not touch other markers, even with the same citekey."""
    text = (
        "Claim A [@smith2021]<!-- ev:ev-aaaaaaaaaaaa --> "
        "and Claim B [@smith2021]<!-- ev:ev-bbbbbbbbbbbb -->."
    )
    new_text, removed = remove_marker(text, "ev-aaaaaaaaaaaa")
    assert removed is True
    assert "[@smith2021]<!-- ev:ev-aaaaaaaaaaaa -->" not in new_text
    assert "[@smith2021]<!-- ev:ev-bbbbbbbbbbbb -->" in new_text


def test_remove_marker_same_citekey_two_ev_ids_remove_one_leaves_other() -> None:
    """Core per-occurrence-addressability test."""
    ev1 = "ev-111111111111"
    ev2 = "ev-222222222222"
    m1 = build_marker("jones2020", ev1)
    m2 = build_marker("jones2020", ev2)
    text = f"First {m1} and second {m2} claims."

    new_text, removed = remove_marker(text, ev1)

    assert removed is True
    assert m1 not in new_text
    assert m2 in new_text
    assert "First  and second" in new_text


def test_remove_marker_ev_id_not_found_text_unchanged() -> None:
    ev = "ev-aaaaaaaaaaaa"
    marker = build_marker("smith2021", ev)
    text = f"Claim {marker}."
    new_text, removed = remove_marker(text, "ev-bbbbbbbbbbbb")
    assert removed is False
    assert new_text == text


# ---------------------------------------------------------------------------
# rename_marker_citekey
# ---------------------------------------------------------------------------


def test_rename_marker_citekey_rewrites_matching_citekey() -> None:
    text = "Claim [@old]<!-- ev:ev-aaaaaaaaaaaa -->."
    new_text, count = rename_marker_citekey(text, "old", "new_key")
    assert count == 1
    assert "[@new_key]<!-- ev:ev-aaaaaaaaaaaa -->" in new_text
    assert "[@old]" not in new_text


def test_rename_marker_citekey_preserves_ev_id() -> None:
    ev_id = "ev-a13f2c91e0b4"
    text = f"Claim [@smith2021]<!-- ev:{ev_id} -->."
    new_text, count = rename_marker_citekey(text, "smith2021", "jones2022")
    assert count == 1
    assert f"ev:{ev_id}" in new_text


def test_rename_marker_citekey_multiple_occurrences() -> None:
    """All occurrences of old_citekey are renamed; each ev id preserved."""
    ev1, ev2 = "ev-111111111111", "ev-222222222222"
    text = f"A [@smith2021]<!-- ev:{ev1} --> B [@smith2021]<!-- ev:{ev2} -->."
    new_text, count = rename_marker_citekey(text, "smith2021", "jones2022")
    assert count == 2
    assert f"[@jones2022]<!-- ev:{ev1} -->" in new_text
    assert f"[@jones2022]<!-- ev:{ev2} -->" in new_text
    assert "[@smith2021]" not in new_text


def test_rename_marker_citekey_does_not_touch_other_citekeys() -> None:
    ev1, ev2 = "ev-111111111111", "ev-222222222222"
    text = f"A [@smith2021]<!-- ev:{ev1} --> B [@jones2020]<!-- ev:{ev2} -->."
    new_text, count = rename_marker_citekey(text, "smith2021", "new_ref")
    assert count == 1
    assert "[@jones2020]<!-- ev:ev-222222222222 -->" in new_text


def test_rename_marker_citekey_old_not_present_returns_zero() -> None:
    text = "No markers here."
    new_text, count = rename_marker_citekey(text, "ghost", "replacement")
    assert count == 0
    assert new_text == text


def test_rename_marker_citekey_no_markers_at_all() -> None:
    text = "Plain text with no citations."
    new_text, count = rename_marker_citekey(text, "whatever", "other")
    assert count == 0
    assert new_text == text


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------


def test_round_trip_insert_find_remove_rename() -> None:
    """Insert two markers (same citekey, different ev ids) into a paragraph,
    find_markers returns both, remove one by ev id, the other remains,
    then rename the remaining one's citekey."""
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

    # Both markers present
    markers = find_markers(text)
    assert len(markers) == 2
    ev_ids = {m["ev_id"] for m in markers}
    assert ev1 in ev_ids
    assert ev2 in ev_ids
    assert all(m["citekey"] == citekey for m in markers)

    # Remove first marker — second must survive
    text, removed = remove_marker(text, ev1)
    assert removed is True
    markers = find_markers(text)
    assert len(markers) == 1
    assert markers[0]["ev_id"] == ev2

    # Rename remaining marker's citekey
    text, count = rename_marker_citekey(text, citekey, "jones2022")
    assert count == 1
    markers = find_markers(text)
    assert len(markers) == 1
    assert markers[0]["citekey"] == "jones2022"
    assert markers[0]["ev_id"] == ev2
