"""Accept a pending proposed_change by REPLAYING the existing mutation
function as the named approver, under the commit mutex (via save_section).
The caller owns the transaction."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import ProposedChange, User, Workspace
from call_manager.proposal_assist.editing.save import StaleSectionError, save_section


class ProposedChangeNotFound(Exception):
    pass


class ProposedChangeNotPending(Exception):
    def __init__(self, status: str) -> None:
        super().__init__(f"proposed_change is {status!r}, not pending")
        self.status = status


def _load_scoped(
    session: Session, proposed_change_id: uuid.UUID, workspace_id: uuid.UUID
) -> ProposedChange:
    """Fetch by BOTH id AND workspace_id, so a member of workspace A can never
    act on a proposed_change id belonging to workspace B (finding #2)."""
    pc = session.scalar(
        select(ProposedChange).where(
            ProposedChange.id == proposed_change_id,
            ProposedChange.workspace_id == workspace_id,
        )
    )
    if pc is None:
        raise ProposedChangeNotFound(str(proposed_change_id))
    return pc


def accept_proposed_change(
    *,
    session: Session,
    proposed_change_id: uuid.UUID,
    workspace_id: uuid.UUID,
    approver: User,
    workspaces_dir: Path | str,
) -> ProposedChange:
    pc = _load_scoped(session, proposed_change_id, workspace_id)
    if pc.status != "pending":
        raise ProposedChangeNotPending(pc.status)

    # SP2a exercises the draft + prose path only; spec/citation land in later plans.
    if pc.target_artifact != "draft":
        raise NotImplementedError("spec target is SP2b")
    if pc.kind != "prose_edit":
        raise NotImplementedError("citation_insert accept is SP2a-2")

    # ws cannot be None here: workspace_id is a NOT NULL FK (ON DELETE CASCADE) and
    # _load_scoped just proved a row with this workspace_id exists in this transaction.
    ws = session.get(Workspace, pc.workspace_id)
    try:
        result = save_section(
            session=session,
            workspace=ws,
            user_id=approver.id,
            section_index=pc.section_index,
            new_text=pc.payload["new_text"],
            base_commit_sha=pc.base_commit_sha,
            base_section_sha=pc.base_section_sha,
            workspaces_dir=workspaces_dir,
        )
    except StaleSectionError:
        pc.status = "stale"
        session.flush()
        return pc

    # A no-op proposal (new_text already equals live text, base still matching) makes
    # save_section short-circuit with changed=False, returning the current HEAD. The
    # proposal's intent already holds, so it is legitimately "accepted" and
    # resulting_commit_sha is that HEAD (the commit at which the accepted state holds).
    pc.status = "accepted"
    pc.resolved_by = approver.id
    pc.resolved_at = datetime.now(UTC)
    pc.resulting_commit_sha = result.commit_sha
    session.flush()
    return pc
