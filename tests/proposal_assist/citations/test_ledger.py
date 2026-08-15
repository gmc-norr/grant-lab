"""Tests for the citations.json v2 in-memory ledger module.

Written TDD-first: all tests must fail before ledger.py exists,
then pass after implementation. No database, no file I/O, no git.
"""

from __future__ import annotations

import json
import re

import pytest

from call_manager.proposal_assist.citations.ledger import (
    LEDGER_VERSION,
    append_evidence,
    build_evidence_row,
    dump_ledger,
    empty_ledger,
    find_by_citekey,
    find_by_id,
    load_ledger,
    new_ev_id,
)

# ---------------------------------------------------------------------------
# empty_ledger / dump_ledger / load_ledger basics
# ---------------------------------------------------------------------------


def test_empty_ledger_shape():
    ledger = empty_ledger()
    assert ledger["version"] == LEDGER_VERSION
    assert ledger["evidence"] == []


def test_dump_ledger_ends_with_newline():
    text = dump_ledger(empty_ledger())
    assert text.endswith("\n")


def test_dump_ledger_sorted_keys():
    ledger = {"version": LEDGER_VERSION, "evidence": [{"z": 1, "a": 2}]}
    text = dump_ledger(ledger)
    data = json.loads(text)
    # Verify round-trips cleanly
    assert data["evidence"][0] == {"z": 1, "a": 2}
    # Confirm keys in the serialised text are sorted (version < evidence in raw JSON)
    # "evidence" < "version" alphabetically — so "evidence" must appear first
    ev_pos = text.index('"evidence"')
    ver_pos = text.index('"version"')
    assert ev_pos < ver_pos


def test_round_trip_empty_via_dump_load():
    text = dump_ledger(empty_ledger())
    loaded = load_ledger(text)
    assert loaded == empty_ledger()


def test_load_ledger_none_returns_empty():
    assert load_ledger(None) == empty_ledger()


def test_load_ledger_empty_string_returns_empty():
    assert load_ledger("") == empty_ledger()


def test_load_ledger_whitespace_only_returns_empty():
    assert load_ledger("   \n\t  ") == empty_ledger()


def test_load_ledger_valid_v2():
    doc = json.dumps({"version": 2, "evidence": [{"id": "ev-aabbccddeeff"}]})
    ledger = load_ledger(doc)
    assert ledger["version"] == 2
    assert len(ledger["evidence"]) == 1


def test_load_ledger_missing_evidence_key_defaults_to_empty_list():
    """A v2 doc without the evidence key should get defaulted to []."""
    doc = json.dumps({"version": 2})
    ledger = load_ledger(doc)
    assert ledger["evidence"] == []


def test_load_ledger_wrong_version_raises():
    doc = json.dumps({"version": 1, "evidence": []})
    with pytest.raises(ValueError, match="unsupported citations.json"):
        load_ledger(doc)


def test_load_ledger_non_dict_array_raises():
    with pytest.raises(ValueError, match="unsupported citations.json"):
        load_ledger(json.dumps([1, 2, 3]))


def test_load_ledger_non_dict_scalar_raises():
    with pytest.raises(ValueError, match="unsupported citations.json"):
        load_ledger(json.dumps(42))


def test_load_ledger_malformed_json_raises():
    with pytest.raises(ValueError):
        load_ledger("{broken json")


def test_load_ledger_non_list_evidence_raises():
    doc = json.dumps({"version": 2, "evidence": {"not": "a list"}})
    with pytest.raises(ValueError, match="'evidence' must be a list"):
        load_ledger(doc)


# ---------------------------------------------------------------------------
# new_ev_id
# ---------------------------------------------------------------------------


def test_new_ev_id_format():
    ev_id = new_ev_id()
    assert re.fullmatch(r"ev-[0-9a-f]{12}", ev_id), f"Unexpected id: {ev_id!r}"


def test_new_ev_id_unique():
    ids = {new_ev_id() for _ in range(200)}
    assert len(ids) == 200


# ---------------------------------------------------------------------------
# append_evidence — non-mutating
# ---------------------------------------------------------------------------


def _make_row(ev_id: str = "ev-aabbccddeeff", citekey: str = "smith2021") -> dict:
    return {
        "id": ev_id,
        "citekey": citekey,
        "reference_id": "ref-uuid",
        "locator": {"chunk_index": 0, "section_path": "Abstract"},
        "source_content_hash": None,
        "chunk_content_hash": "abc123",
        "evidence_quote": "some quoted text",
        "evidence_quote_offsets": [0, 16],
        "stance": "supports",
        "confidence": "high",
        "claim_context": {"text": "claim sentence", "hash": "def456"},
        "citation_snapshot": "Smith et al. (2021)",
        "created_at": "2026-06-07T00:00:00Z",
        "updated_at": "2026-06-07T00:00:00Z",
    }


def test_append_evidence_returns_new_ledger_with_row():
    original = empty_ledger()
    row = _make_row()
    result = append_evidence(original, row)
    assert len(result["evidence"]) == 1
    assert result["evidence"][0] is row


def test_append_evidence_does_not_mutate_input():
    original = empty_ledger()
    row = _make_row()
    append_evidence(original, row)
    assert original["evidence"] == []  # original untouched


def test_append_evidence_preserves_version():
    ledger = empty_ledger()
    result = append_evidence(ledger, _make_row())
    assert result["version"] == LEDGER_VERSION


def test_append_evidence_accumulates():
    ledger = empty_ledger()
    ledger = append_evidence(ledger, _make_row("ev-000000000001", "alpha"))
    ledger = append_evidence(ledger, _make_row("ev-000000000002", "beta"))
    assert len(ledger["evidence"]) == 2


# ---------------------------------------------------------------------------
# find_by_id
# ---------------------------------------------------------------------------


def test_find_by_id_hit():
    row = _make_row("ev-aabbccddeeff")
    ledger = append_evidence(empty_ledger(), row)
    found = find_by_id(ledger, "ev-aabbccddeeff")
    assert found is not None
    assert found["id"] == "ev-aabbccddeeff"


def test_find_by_id_miss():
    ledger = empty_ledger()
    assert find_by_id(ledger, "ev-aabbccddeeff") is None


# ---------------------------------------------------------------------------
# find_by_citekey
# ---------------------------------------------------------------------------


def test_find_by_citekey_returns_all_matching():
    ledger = empty_ledger()
    ledger = append_evidence(ledger, _make_row("ev-000000000001", "jones2020"))
    ledger = append_evidence(ledger, _make_row("ev-000000000002", "jones2020"))
    ledger = append_evidence(ledger, _make_row("ev-000000000003", "other2019"))
    results = find_by_citekey(ledger, "jones2020")
    assert len(results) == 2
    assert all(r["citekey"] == "jones2020" for r in results)


def test_find_by_citekey_no_match_returns_empty_list():
    ledger = append_evidence(empty_ledger(), _make_row("ev-aabbccddeeff", "smith2021"))
    assert find_by_citekey(ledger, "nobody1900") == []


# ---------------------------------------------------------------------------
# round-trip with evidence row
# ---------------------------------------------------------------------------


def test_round_trip_with_evidence_row():
    """Non-empty ledger survives dump → load unchanged."""
    ledger = empty_ledger()
    ledger = append_evidence(ledger, _make_row("ev-aabbccddeeff", "smith2021"))
    assert load_ledger(dump_ledger(ledger)) == ledger


# ---------------------------------------------------------------------------
# load_ledger — evidence element must be a dict
# ---------------------------------------------------------------------------


def test_load_ledger_non_dict_evidence_element_raises() -> None:
    """Evidence elements that are not dicts raise ValueError with the index."""
    doc = json.dumps({"version": 2, "evidence": ["x"]})
    with pytest.raises(ValueError, match=r"evidence\[0\] must be a dict"):
        load_ledger(doc)


def test_load_ledger_mixed_evidence_elements_raises() -> None:
    """A list with a dict followed by a non-dict raises on the non-dict index."""
    doc = json.dumps({"version": 2, "evidence": [{"id": "ev-000000000001"}, 42]})
    with pytest.raises(ValueError, match=r"evidence\[1\] must be a dict"):
        load_ledger(doc)


# ---------------------------------------------------------------------------
# dump_ledger — ensure_ascii=False (non-ASCII survives un-escaped)
# ---------------------------------------------------------------------------


def test_dump_ledger_non_ascii_unescaped() -> None:
    """Non-ASCII characters in evidence quotes appear un-escaped in dump output."""
    ledger = empty_ledger()
    row = {**_make_row(), "evidence_quote": "café — naïve"}
    ledger = append_evidence(ledger, row)
    dumped = dump_ledger(ledger)
    assert "café" in dumped
    assert "naïve" in dumped
    # Must NOT appear as \\u-escaped sequences
    assert "\\u" not in dumped


def test_dump_load_non_ascii_round_trip() -> None:
    """A non-ASCII evidence quote survives dump → load unchanged."""
    ledger = empty_ledger()
    row = {**_make_row(), "evidence_quote": "café — naïve"}
    ledger = append_evidence(ledger, row)
    loaded = load_ledger(dump_ledger(ledger))
    assert loaded["evidence"][0]["evidence_quote"] == "café — naïve"


# ---------------------------------------------------------------------------
# build_evidence_row — spec-exact schema + round-trip
# ---------------------------------------------------------------------------

_FULL_ROW_KWARGS = dict(
    ev_id="ev-aabbccddeeff",
    citekey="smith2021",
    reference_id="ref-uuid-1234",
    chunk_index=12,
    section_path="Abstract",
    source_content_hash="sha256:abc",
    chunk_content_hash="sha256:def",
    evidence_quote="the exact span relied on",
    evidence_quote_offsets=[0, 24],
    stance="supports",
    confidence="high",
    claim_context_text="claim sentence snapshot",
    claim_context_hash="sha256:ghi",
    citation_snapshot="Smith et al. (2021)",
    created_at="2026-06-07T00:00:00Z",
    updated_at="2026-06-07T00:00:00Z",
)


def test_build_evidence_row_returns_dict() -> None:
    row = build_evidence_row(**_FULL_ROW_KWARGS)
    assert isinstance(row, dict)


def test_build_evidence_row_top_level_keys() -> None:
    """Row has exactly the spec-mandated top-level keys."""
    row = build_evidence_row(**_FULL_ROW_KWARGS)
    expected_keys = {
        "id",
        "citekey",
        "reference_id",
        "locator",
        "source_content_hash",
        "chunk_content_hash",
        "evidence_quote",
        "evidence_quote_offsets",
        "stance",
        "confidence",
        "claim_context",
        "citation_snapshot",
        "created_at",
        "updated_at",
    }
    assert set(row.keys()) == expected_keys


def test_build_evidence_row_locator_shape() -> None:
    row = build_evidence_row(**_FULL_ROW_KWARGS)
    assert row["locator"] == {"chunk_index": 12, "section_path": "Abstract"}


def test_build_evidence_row_claim_context_shape() -> None:
    row = build_evidence_row(**_FULL_ROW_KWARGS)
    assert row["claim_context"] == {
        "text": "claim sentence snapshot",
        "hash": "sha256:ghi",
    }


def test_build_evidence_row_scalar_fields() -> None:
    row = build_evidence_row(**_FULL_ROW_KWARGS)
    assert row["id"] == "ev-aabbccddeeff"
    assert row["citekey"] == "smith2021"
    assert row["stance"] == "supports"
    assert row["confidence"] == "high"
    assert row["evidence_quote_offsets"] == [0, 24]


def test_build_evidence_row_nullable_fields() -> None:
    """section_path, source_content_hash, and confidence may be None."""
    kwargs = {
        **_FULL_ROW_KWARGS,
        "section_path": None,
        "source_content_hash": None,
        "confidence": None,
    }
    row = build_evidence_row(**kwargs)
    assert row["locator"]["section_path"] is None
    assert row["source_content_hash"] is None
    assert row["confidence"] is None


def test_build_evidence_row_round_trips_through_ledger() -> None:
    """A row built with build_evidence_row survives append → dump → load unchanged."""
    row = build_evidence_row(**_FULL_ROW_KWARGS)
    ledger = append_evidence(empty_ledger(), row)
    loaded = load_ledger(dump_ledger(ledger))
    assert loaded["evidence"][0] == row
