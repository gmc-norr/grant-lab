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
from call_manager.proposal_assist.citations.markers import anchor_occurrences
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


def _relocated_selection_block(sel: SelectionInput, live) -> dict:
    """Collab path: the client's base_section_sha and offsets are git-derived and the
    live room is ahead of git, so re-locate the anchor in the LIVE section text and
    ignore the stale sha. Verified iff the anchor occurs exactly once (marker-aware).
    A modify that can't be uniquely located is a real conflict (409); an ask stays
    leniently unverified."""
    occ = anchor_occurrences(live.text, sel.anchor)
    if len(occ) == 1:
        frm = occ[0]
        to = frm + len(sel.anchor)
        if to - frm > MAX_SELECTION_CODEPOINTS:
            raise SelectionConflictError("selection is too long — select a smaller span")
        return {
            "type": "selection",
            "request_type": sel.request_type,
            "section_index": sel.section_index,
            "anchor": sel.anchor,
            "from_offset": frm,
            "to_offset": to,
            "verified": True,
            "heading": live.heading,
        }
    if sel.request_type == "modify":
        if not occ:
            raise SelectionConflictError(
                "the text you selected is no longer in this section — reselect"
            )
        raise SelectionConflictError(
            "your selection appears more than once — select a more specific range"
        )
    # ask: leniently unverified (unchanged spirit of the git-path unverified branch).
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
        "heading": live.heading,
    }


def build_selection_block(
    *, repo_dir: Path, sel: SelectionInput, sections=None, allow_anchor_relocation=False
) -> dict:
    if sections is None:
        proposal = repo_dir / PROPOSAL_FILENAME
        sections = split_sections(proposal.read_text()) if proposal.exists() else []
    live = sections[sel.section_index] if 0 <= sel.section_index < len(sections) else None

    if allow_anchor_relocation and live is not None:
        return _relocated_selection_block(sel, live)

    verified = live is not None and live.sha == sel.base_section_sha

    if verified:
        text = live.text  # type: ignore[union-attr]
        n = len(text)  # code points (str len)
        if sel.from_offset == sel.to_offset:
            raise SelectionConflictError("nothing selected — select some text first")
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
