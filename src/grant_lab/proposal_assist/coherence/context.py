"""Assemble the coherence-keeper's instruction: criteria + full spec + full
draft + spec-lock state. Pure (DB read + two file reads); no LLM."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from grant_lab.db.models import Workspace
from grant_lab.proposal_assist.collab.canonical import current_artifact_text_and_sha
from grant_lab.proposal_assist.paths import workspace_repo_dir

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


def _read(session, workspace_id, repo_dir: Path, artifact: str) -> str:
    """The artifact's CURRENT text: the live room when collab is on, raw HEAD when
    off. Was a direct disk read, which handed the coherence keeper a document the
    researcher had already moved past."""
    return current_artifact_text_and_sha(
        session, workspace_id=workspace_id, artifact=artifact, repo_dir=repo_dir
    )[0]


def assemble_coherence_context(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> str:
    ws = session.get(Workspace, workspace_id)
    locked = ws is not None and ws.spec_locked_at is not None
    criteria = json.dumps(ws.rubric_criteria, indent=2) if ws and ws.rubric_criteria else "[]"
    funder_notes = (ws.rubric_funder_notes if ws else None) or "(none)"
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    draft = _read(session, workspace_id, repo_dir, "draft") or "(draft is empty)"
    spec = _read(session, workspace_id, repo_dir, "spec") or "(spec is empty)"

    return (
        f"{_ROLE}{_LOCKED if locked else _UNLOCKED}"
        f"\n# Funder criteria\n{criteria}\n\n# Funder notes\n{funder_notes}\n"
        f"\n# SPEC (proposal-spec.md)\n{spec}\n"
        f"\n# DRAFT (proposal.md)\n{draft}\n"
    )
