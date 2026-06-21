"""Assemble the coherence-keeper's instruction: criteria + full spec + full
draft + spec-lock state. Pure (DB read + two file reads); no LLM."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from call_manager.db.models import Workspace
from call_manager.proposal_assist.artifacts import PROPOSAL_FILENAME, SPEC_FILENAME
from call_manager.proposal_assist.paths import workspace_repo_dir

_ROLE = (
    "You are the COHERENCE-KEEPER for a grant proposal. Compare the team's SPEC "
    "(statement of intent) against the DRAFT prose. Propose CONSERVATIVE, per-section, "
    "full-text reconciliation edits ONLY where there is genuine drift — do not restate "
    "agreement, and do not rewrite for style. Read an artifact (read_proposal / read_spec) "
    "before proposing an edit to it.\n"
)
_UNLOCKED = (
    "The spec is UNLOCKED (divergent mode): you MAY propose draft edits to realize the "
    "spec AND spec edits to capture intent the draft has grown that the spec lacks.\n"
)
_LOCKED = (
    "The spec is LOCKED (convergent mode): the spec is authoritative and frozen. Propose "
    "DRAFT edits only, to pull the draft back into agreement with the spec.\n"
)


def _read(repo_dir: Path, filename: str) -> str:
    path = repo_dir / filename
    return path.read_text() if path.exists() else ""


def assemble_coherence_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> str:
    ws = session.get(Workspace, workspace_id)
    locked = ws is not None and ws.spec_locked_at is not None
    criteria = json.dumps(ws.rubric_criteria, indent=2) if ws and ws.rubric_criteria else "[]"
    funder_notes = (ws.rubric_funder_notes if ws else None) or "(none)"
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft = _read(repo_dir, PROPOSAL_FILENAME) or "(draft is empty)"
    spec = _read(repo_dir, SPEC_FILENAME) or "(spec is empty)"

    return (
        f"{_ROLE}{_LOCKED if locked else _UNLOCKED}"
        f"\n# Funder criteria\n{criteria}\n\n# Funder notes\n{funder_notes}\n"
        f"\n# SPEC (proposal-spec.md)\n{spec}\n"
        f"\n# DRAFT (proposal.md)\n{draft}\n"
    )
