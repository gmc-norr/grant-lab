"""Validate a user's editor selection and shape it into a stored content block.

The frontend sends `selection_context` as a typed API field; the server (here)
reads the committed section bytes and decides whether the selection is VERIFIED
(clean: offsets+anchor validated against committed text) or UNVERIFIED (the
section moved / dirty buffer). `modify` requires verified — an unverified modify
raises SelectionConflictError (mapped to 409). `ask` is allowed either way; an
unverified ask is labeled so the agent preamble can stay honest.

Offsets are CODE-POINT indices (matching Python str slicing); the client converts
UTF-16 -> code points before submit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections

MAX_SELECTION_CODEPOINTS = 4000


class SelectionConflictError(Exception):
    """Raised when a selection cannot be honored (invalid range/anchor, or an
    unverified modify). The message is user-safe (surfaced as a 409 detail)."""


@dataclass(frozen=True)
class SelectionInput:
    request_type: str  # "ask" | "modify"
    section_index: int
    base_commit_sha: str
    base_section_sha: str
    anchor: str
    from_offset: int
    to_offset: int


def build_selection_block(*, repo_dir: Path, sel: SelectionInput) -> dict:
    proposal = repo_dir / PROPOSAL_FILENAME
    sections = split_sections(proposal.read_text()) if proposal.exists() else []
    live = sections[sel.section_index] if 0 <= sel.section_index < len(sections) else None

    verified = live is not None and live.sha == sel.base_section_sha

    if verified:
        text = live.text  # type: ignore[union-attr]
        n = len(text)  # code points (str len)
        if not (0 <= sel.from_offset < sel.to_offset <= n):
            raise SelectionConflictError("selection is out of range — reselect it")
        if sel.to_offset - sel.from_offset > MAX_SELECTION_CODEPOINTS:
            raise SelectionConflictError("selection is too long — select a smaller span")
        if text[sel.from_offset : sel.to_offset] != sel.anchor:
            raise SelectionConflictError("selection changed — save and reselect")
        return {
            "type": "selection",
            "request_type": sel.request_type,
            "section_index": sel.section_index,
            "anchor": sel.anchor,
            "from_offset": sel.from_offset,
            "to_offset": sel.to_offset,
            "verified": True,
            "heading": live.heading,  # type: ignore[union-attr]
        }

    # Unverified: section moved or dirty buffer.
    if sel.request_type == "modify":
        raise SelectionConflictError("section changed — save and reselect to edit")
    if len(sel.anchor) > MAX_SELECTION_CODEPOINTS:
        raise SelectionConflictError("selection is too long — select a smaller span")
    return {
        "type": "selection",
        "request_type": sel.request_type,
        "section_index": sel.section_index,
        "anchor": sel.anchor,
        "from_offset": sel.from_offset,
        "to_offset": sel.to_offset,
        "verified": False,
        "heading": live.heading if live is not None else "",
    }
