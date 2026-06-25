"""Workspace lifecycle service functions: `create_workspace` and `soft_delete_workspace`.

`create_workspace` does three things in one
unit of work:

  1. Insert a Workspace row.
  2. Write a workspace_events(kind='created') audit row.
  3. Enqueue a workspace_setup_jobs row so the Phase 0 worker
     (src/call_manager/proposal_assist/phases/setup.py) snapshots the
     call body, extracts the section + rubric templates, and yields at
     waiting_for_rubric_review for the workspace owner to confirm.

The caller commits. If any of the three fails, the caller rolls back and
no partial state lands.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.db.models import TurnJob, Workspace, WorkspaceEvent, WorkspaceSetupJob
from call_manager.proposal_assist.attachments.storage import AttachmentStorage
from call_manager.proposal_assist.coherence.jobs import cancel_coherence_jobs_for_workspace
from call_manager.proposal_assist.evaluator.jobs import cancel_evaluator_jobs_for_workspace
from call_manager.proposal_assist.references.storage import ReferenceStorage
from call_manager.proposal_assist.review.jobs import cancel_review_runs_for_workspace
from call_manager.proposal_assist.turns.outcomes import record_terminal_outcome
from call_manager.proposal_assist.uncited_claims.jobs import (
    cancel_uncited_claims_jobs_for_workspace,
)

logger = logging.getLogger(__name__)


def create_workspace(
    *,
    session: Session,
    owner_id: int,
    title: str,
    call_id: int | None = None,
    profile_id: int | None = None,
) -> Workspace:
    """Create a new workspace, write the 'created' audit event, and enqueue
    the Phase 0 setup job. Returns the Workspace (with `.id` populated via
    session.flush())."""
    ws = Workspace(
        owner_id=owner_id,
        title=title,
        call_id=call_id,
        profile_id=profile_id,
    )
    session.add(ws)
    session.flush()  # populates ws.id from the DB default

    session.add(
        WorkspaceEvent(
            workspace_id=ws.id,
            kind="created",
            payload={
                "title": title,
                "call_id": call_id,
                "profile_id": profile_id,
            },
            actor_id=owner_id,
        )
    )
    session.add(
        WorkspaceSetupJob(
            workspace_id=ws.id,
            status="queued",
        )
    )
    return ws


def soft_delete_workspace(
    session: Session, *, workspace_id: uuid.UUID, workspaces_dir: Path
) -> None:
    """Soft-delete a workspace: mark it deleted, cancel every active background
    job + queued turn so none can burn cloud tokens after deletion, commit, then
    best-effort scrub attachment + reference bytes from disk.

    Order is deliberate (a prior Codex-finding fix): DB commit FIRST so the
    user-visible 'deleted' state is durable, THEN best-effort FS cleanup. A
    cleanup failure leaves the workspace soft-deleted with harmless orphan files
    (no code reads them; the row-source-of-truth lookup gates serving on a live
    DB row, and a future sweep collects the bytes). Attachment/reference DB rows
    are intentionally preserved for the audit trail; only the on-disk bytes go.

    The caller resolves ownership (require_owner) before calling this.
    """
    ws = session.get(Workspace, workspace_id)
    if ws is not None:
        ws.soft_deleted_at = datetime.now(UTC)
    # Fail any active (queued|running) background pass so it can't claim and burn
    # cloud tokens after the workspace is gone (each claim_next also excludes
    # soft-deleted workspaces; this also unwedges the active-unique slot). Same txn.
    cancel_coherence_jobs_for_workspace(session, workspace_id=workspace_id)
    cancel_evaluator_jobs_for_workspace(session, workspace_id=workspace_id)
    cancel_review_runs_for_workspace(session, workspace_id=workspace_id)
    cancel_uncited_claims_jobs_for_workspace(session, workspace_id=workspace_id)
    # Cancel any QUEUED turns so a deletion-race turn can't dispatch. Route through
    # the helper for audit parity (closes each cloud_turn_sent chain), but
    # emit_chat=False — the workspace is 404 the instant it's soft-deleted, so a
    # viewer-safe row would be dead-on-arrival. 'cancelled' (owner-initiated,
    # never started), not 'failed'.
    queued = session.execute(
        select(TurnJob.id).where(
            TurnJob.workspace_id == workspace_id,
            TurnJob.status == "queued",
        )
    ).all()
    for row in queued:
        record_terminal_outcome(
            session,
            turn_job_id=row.id,
            workspace_id=workspace_id,
            outcome="cancelled",
            expected_status="queued",
            raw_error="workspace soft-deleted",
            emit_chat=False,
        )
    session.commit()
    # Scrub attachment bytes (DB rows remain — audit trail). Best-effort.
    try:
        AttachmentStorage(root=workspaces_dir).delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete_workspace: attachment cleanup failed for workspace %s; "
            "files will be collected by a future sweep",
            workspace_id,
            exc_info=True,
        )
    # Scrub reference bytes (original files + extracted text.txt). Best-effort.
    try:
        ReferenceStorage(root=workspaces_dir).delete_workspace(workspace_id=workspace_id)
    except OSError:
        logger.warning(
            "soft_delete_workspace: reference cleanup failed for workspace %s; "
            "files will be collected by a future sweep",
            workspace_id,
            exc_info=True,
        )
