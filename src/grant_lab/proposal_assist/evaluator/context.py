"""Assemble the evaluator's instruction (criteria + indexed spec + indexed draft)
and capture per-artifact content hashes for staleness. Pure (DB read + file reads)."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from grant_lab.db.models import Workspace
from grant_lab.proposal_assist.collab.canonical import current_artifact_text_and_sha
from grant_lab.proposal_assist.editing.sectionizer import split_sections
from grant_lab.proposal_assist.paths import workspace_repo_dir

_ROLE = (
    "You are the CHAIR of a mock review panel for a grant proposal. Assess the DRAFT "
    "against EACH funder criterion below, considering the SPEC (statement of intent). "
    "For each criterion assign a band — strong | adequate | at_risk | gap — with a one-"
    "paragraph note (the rationale; for at_risk/gap, the specific deficiency) and, when "
    "useful, a deep_link to the most relevant draft or spec section to act on. Call "
    "submit_scorecard EXACTLY ONCE, covering every criterion by its [index]. You have no "
    "other tools; everything you need is below.\n"
)


@dataclass
class EvaluatorContext:
    instruction: str
    draft_section_count: int
    spec_section_count: int
    # Hashes of the EXACT bytes assembled into the instruction (read once — no
    # TOCTOU re-read), so the job's evaluated_*_sha matches what the model saw.
    draft_sha: str
    spec_sha: str
    # The rubric criteria snapshot rendered into the instruction — PINNED so the
    # submit_scorecard tool validates/weights against exactly what the model was
    # shown, not a live workspace rubric that may be edited mid-run.
    rubric: list[dict]


def content_sha(path: Path) -> str:
    """sha256 of a file's bytes, or '' if it does not exist. Per-artifact content
    identity (the draft and spec share one git repo HEAD, which can't distinguish
    a draft-only edit from a spec-only edit). Used by the /scorecard endpoint to
    recompute current hashes — must match _read_artifact's hashing exactly."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_artifact(session, workspace_id, repo_dir: Path, artifact: str) -> tuple[str, str]:
    """The artifact's CURRENT text and sha — the live room when collab is on, raw
    HEAD when it is off. Text and sha come from one call so they cannot disagree.

    Was a direct disk read. Git HEAD lags the Yjs log until a checkpoint fires, so
    that sent a stale document to a hosted model on every evaluator and review pass.
    Missing/empty -> ('', '') so '' still marks 'no artifact' on both sides of
    staleness."""
    return current_artifact_text_and_sha(
        session, workspace_id=workspace_id, artifact=artifact, repo_dir=repo_dir
    )


def _render_criteria(rubric: list[dict]) -> str:
    if not rubric:
        return "(no criteria for this call — submit an empty scorecard)"
    lines: list[str] = []
    for i, c in enumerate(rubric):
        lines.append(f"[{i}] {c.get('name', '')} (weight {c.get('weight', '?')})")
        if c.get("description"):
            lines.append(f"    {c['description']}")
        for sub in c.get("subcriteria") or []:
            lines.append(f"      - {sub.get('name', '')}: {sub.get('description', '')}")
    return "\n".join(lines)


def _render_sections(sections: list) -> str:
    return "\n\n".join(f"[§{s.index}]\n{s.text}" for s in sections)


def assemble_evaluator_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> EvaluatorContext:
    ws = session.get(Workspace, workspace_id)
    rubric = list(ws.rubric_criteria) if ws and ws.rubric_criteria else []
    funder_notes = (ws.rubric_funder_notes if ws else None) or "(none)"
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft_text, draft_sha = _read_artifact(session, workspace_id, repo_dir, "draft")
    spec_text, spec_sha = _read_artifact(session, workspace_id, repo_dir, "spec")
    # split_sections ALWAYS returns >=1 section (section 0 is the preamble, possibly
    # empty). For an empty/whitespace artifact we render the "(empty)" placeholder and
    # count 0 (nothing to deep-link); for real content we keep the sectionizer's exact
    # indexes so deep_link.section_index lines up with the editor.
    draft_sections = split_sections(draft_text) if draft_text.strip() else []
    spec_sections = split_sections(spec_text) if spec_text.strip() else []
    draft_block = _render_sections(draft_sections) if draft_sections else "(draft is empty)"
    spec_block = _render_sections(spec_sections) if spec_sections else "(spec is empty)"
    instruction = (
        f"{_ROLE}"
        f"\n# Funder criteria\n{_render_criteria(rubric)}\n"
        f"\n# Funder notes\n{funder_notes}\n"
        f"\n# SPEC (proposal-spec.md)\n{spec_block}\n"
        f"\n# DRAFT (proposal.md)\n{draft_block}\n"
    )
    return EvaluatorContext(
        instruction=instruction,
        draft_section_count=len(draft_sections),
        spec_section_count=len(spec_sections),
        draft_sha=draft_sha,
        spec_sha=spec_sha,
        rubric=rubric,
    )
