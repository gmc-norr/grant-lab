"""Flip pending proposed_change rows to 'stale' when their targeted section's
base_section_sha no longer matches the live draft. Called by the list endpoint
(proactive UX) — accept-time StaleSectionError is the authoritative guard."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir
from call_manager.proposal_assist.tools.proposal import PROPOSAL_FILENAME


def refresh_staleness(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> int:
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    proposal_path = repo_dir / PROPOSAL_FILENAME
    if not proposal_path.exists():
        # Workspace is still in setup (Phase-0 hasn't written proposal.md yet).
        # There are no live sections to compare against — and no draft proposals
        # could exist yet — so this best-effort sweep is a no-op. Returning early
        # keeps callers (the list endpoint) from 500ing on an un-set-up workspace.
        return 0
    sections = split_sections(proposal_path.read_text())
    live_sha = {i: sec.sha for i, sec in enumerate(sections)}

    rows = session.scalars(
        select(ProposedChange).where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.status == "pending",
            ProposedChange.target_artifact == "draft",
        )
    ).all()
    n = 0
    for pc in rows:
        current = live_sha.get(pc.section_index)
        if current is None or current != pc.base_section_sha:
            pc.status = "stale"
            n += 1
    session.flush()
    return n
