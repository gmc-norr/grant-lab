"""Flip pending proposed_change rows to 'stale' when their targeted section's
base_section_sha no longer matches the live artifact. Called by the list endpoint
(proactive UX) — accept-time StaleSectionError is the authoritative guard."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.artifacts import Artifact, filename_for
from call_manager.proposal_assist.collab import canonical
from call_manager.proposal_assist.collab.state import is_collab_enabled
from call_manager.proposal_assist.editing.sectionizer import split_sections
from call_manager.proposal_assist.paths import workspace_repo_dir


def refresh_staleness(
    *, session: Session, workspace_id: uuid.UUID, workspaces_dir: Path | str
) -> int:
    repo_dir = workspace_repo_dir(workspaces_dir, workspace_id)
    # Live section shas per artifact, read once. A missing artifact file (workspace
    # still in setup, or no spec yet) contributes no live sections, so its pending
    # rows simply aren't flipped here (accept-time staleness remains authoritative).
    live: dict[str, dict[int, str]] = {}
    for artifact in (Artifact.DRAFT, Artifact.SPEC):
        art = str(artifact)  # "draft" / "spec"
        if is_collab_enabled(session, workspace_id=workspace_id, artifact=art):
            # Collab-on: compare against the ROOM-CANONICAL live sections (Slice 3a
            # C1) so a normalized base is not false-flagged stale before accept.
            md = canonical.canonical_artifact_markdown(
                session, workspace_id=workspace_id, artifact=art, repo_dir=repo_dir
            )
            live[art] = {i: sec.sha for i, sec in enumerate(split_sections(md))}
        else:
            path = repo_dir / filename_for(artifact)
            if path.exists():
                live[art] = {
                    i: sec.sha for i, sec in enumerate(split_sections(path.read_text()))
                }

    # FOR UPDATE SKIP LOCKED: don't fight an in-flight accept/reject (its row lock
    # decides its fate); rows we lock stay pending until this sweep commits.
    rows = session.scalars(
        select(ProposedChange)
        .where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.status == "pending",
        )
        .with_for_update(skip_locked=True)
    ).all()
    n = 0
    for pc in rows:
        sections = live.get(pc.target_artifact)
        if sections is None:
            continue  # artifact file absent -> leave pending
        current = sections.get(pc.section_index)
        if current is None or current != pc.base_section_sha:
            pc.status = "stale"
            n += 1
    session.flush()
    return n
