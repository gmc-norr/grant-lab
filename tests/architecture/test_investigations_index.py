"""Every prior investigation must be findable from one index.

A document nobody can find is a document nobody reads, and this repo has already paid
for that twice: an afternoon re-deriving the 2026-06-17 editor round-trip spike, and a
separate re-derivation of a finding the 2026-08-25 fence-elimination review had recorded.

This gate does not judge the index's prose. It pins the two things that make it useless
when they rot: a file that exists but is not listed, and a listing that points at nothing.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
SP = REPO / "docs/superpowers"
INDEX = SP / "INVESTIGATIONS.md"
DIRS = ("notes", "reviews")


def _linked() -> set[str]:
    body = INDEX.read_text()
    return set(re.findall(r'\((?:\./)?((?:notes|reviews)/[^)]+\.md)\)', body))


def _on_disk() -> set[str]:
    return {
        f"{d}/{p.name}" for d in DIRS for p in (SP / d).glob("*.md")
    }


def test_every_investigation_is_indexed():
    missing = sorted(_on_disk() - _linked())
    assert not missing, (
        "these investigations are not listed in docs/superpowers/INVESTIGATIONS.md, so "
        f"nobody will find them: {missing}"
    )


def test_every_index_entry_names_a_real_file():
    phantom = sorted(_linked() - _on_disk())
    assert not phantom, (
        f"INVESTIGATIONS.md links to files that do not exist: {phantom}"
    )


def test_the_index_is_not_merely_a_file_listing():
    """Each entry must say what QUESTION it answers, or the index adds nothing over ls."""
    body = INDEX.read_text()
    assert body.count("*") >= len(_on_disk()), (
        "INVESTIGATIONS.md should carry an italicised question or summary per entry"
    )
