"""Assemble the uncited-claims instruction (the indexed DRAFT with its existing citation
markers) and capture the per-section texts (for the runner's substring filter) + the draft
content sha (for staleness). Pure (DB read + one file read)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from grant_lab.proposal_assist.collab.canonical import current_artifact_text_and_sha
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir

_ROLE = (
    "You are auditing a grant proposal DRAFT for UNCITED factual claims. The draft below is "
    "shown section-by-section as [§index], INCLUDING its existing inline citation markers. "
    "Flag every factual/empirical assertion — statistics, prior-work claims, mechanism or "
    "efficacy claims, comparative claims — that LACKS an adjacent citation marker. EXCLUDE: "
    "the applicant's own proposed/novel work and aims; common knowledge and definitions; and "
    "anything already adjacent to a citation marker. For each flagged claim return the "
    "VERBATIM claim text (copied from its section so it is locatable), its section_index, and "
    "a one-line reason. Call submit_uncited_claims EXACTLY ONCE with the complete list (an "
    "empty list if there are none). You have no other tools.\n"
)


@dataclass
class UncitedClaimsContext:
    instruction: str
    draft_section_count: int
    # index -> exact section text the model was shown; the runner keeps a claim only if its
    # claim_text is a substring of its referenced section here (drops hallucinations).
    section_texts: dict[int, str]
    # sha256 of the EXACT draft bytes assembled (read once — no TOCTOU), so the job's
    # evaluated_draft_sha matches what the model scanned.
    draft_sha: str


def _read_draft(session, workspace_id, repo_dir: Path) -> tuple[str, str]:
    """The draft's CURRENT text and sha — the live room when collab is on, raw HEAD
    when off, from one call so they cannot disagree. Was a direct disk read, which
    made uncited-claims flag claims that had been cited since the last checkpoint.
    Missing -> ('', '') so '' still marks 'no draft' on both sides of staleness."""
    return current_artifact_text_and_sha(
        session, workspace_id=workspace_id, artifact="draft", repo_dir=repo_dir
    )


def _render_sections(sections: list) -> str:
    return "\n\n".join(f"[§{s.index}]\n{s.text}" for s in sections)


def assemble_uncited_claims_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> UncitedClaimsContext:
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft_text, draft_sha = _read_draft(session, workspace_id, repo_dir)
    sections = split_sections(draft_text) if draft_text.strip() else []
    section_texts = {s.index: s.text for s in sections}
    draft_block = _render_sections(sections) if sections else "(draft is empty)"
    instruction = f"{_ROLE}\n# DRAFT (proposal.md)\n{draft_block}\n"
    return UncitedClaimsContext(
        instruction=instruction,
        draft_section_count=len(sections),
        section_texts=section_texts,
        draft_sha=draft_sha,
    )
