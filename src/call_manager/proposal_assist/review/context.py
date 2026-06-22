"""Assemble the mock-panel sub-call instructions (persona role + criteria + indexed
spec + indexed draft) and capture the draft's content sha256 for staleness. Pure
(DB read + file reads). Reuses the evaluator's artifact/criteria renderers."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.evaluator.context import (
    _read_artifact,
    _render_criteria,
    _render_sections,
)
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.review.personas import persona_prompt


@dataclass
class ReviewContext:
    draft_text: str
    spec_text: str
    rubric: list[dict]
    funder_notes: str
    draft_sha: str  # sha256 of the EXACT proposal.md bytes assembled


def assemble_review_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> ReviewContext:
    ws = session.get(Workspace, workspace_id)
    rubric = list(ws.rubric_criteria) if ws and ws.rubric_criteria else []
    funder_notes = (ws.rubric_funder_notes if ws else None) or "(none)"
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft_text, draft_sha = _read_artifact(repo_dir, PROPOSAL_FILENAME)
    spec_text, _ = _read_artifact(repo_dir, SPEC_FILENAME)
    return ReviewContext(
        draft_text=draft_text,
        spec_text=spec_text,
        rubric=rubric,
        funder_notes=funder_notes,
        draft_sha=draft_sha,
    )


def _blocks(ctx: ReviewContext) -> str:
    draft_sections = split_sections(ctx.draft_text) if ctx.draft_text.strip() else []
    spec_sections = split_sections(ctx.spec_text) if ctx.spec_text.strip() else []
    draft_block = _render_sections(draft_sections) if draft_sections else "(draft is empty)"
    spec_block = _render_sections(spec_sections) if spec_sections else "(spec is empty)"
    return (
        f"\n# Funder criteria\n{_render_criteria(ctx.rubric)}\n"
        f"\n# Funder notes\n{ctx.funder_notes}\n"
        f"\n# SPEC (proposal-spec.md)\n{spec_block}\n"
        f"\n# DRAFT (proposal.md)\n{draft_block}\n"
    )


def persona_instruction(kind: str, ctx: ReviewContext) -> str:
    return f"{persona_prompt(kind)}\n{_blocks(ctx)}"


def consolidation_instruction(
    ctx: ReviewContext, persona_md: dict[str, str], fit_check_md: str | None
) -> str:
    parts = [persona_prompt("consolidation"), _blocks(ctx), "\n# Panelist critiques\n"]
    if fit_check_md:
        parts.append(f"\n## Eligibility / fit verdict\n{fit_check_md}\n")
    for kind, md in persona_md.items():
        parts.append(f"\n## {kind}\n{md}\n")
    return "".join(parts)
