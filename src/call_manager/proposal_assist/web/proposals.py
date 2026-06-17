"""Member-gated propose→accept endpoints for proposed_changes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import ProposedChange
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.editing.save import SectionStructureError
from call_manager.proposal_assist.proposals.accepting import (
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    accept_proposed_change,
    reject_proposed_change,
)
from call_manager.proposal_assist.proposals.staleness import refresh_staleness
from call_manager.proposal_assist.web._perms import require_member
from call_manager.proposal_assist.web.workspaces import _session_dep

router = APIRouter(prefix="/workspaces", tags=["proposed-changes"])


def _serialize(pc: ProposedChange) -> dict:
    return {
        "id": str(pc.id),
        "status": pc.status,
        "target_artifact": pc.target_artifact,
        "section_index": pc.section_index,
        "kind": pc.kind,
        "origin_kind": pc.origin_kind,
        "resulting_commit_sha": pc.resulting_commit_sha,
        "created_at": pc.created_at.isoformat() if pc.created_at else None,
        "resolved_by": pc.resolved_by,
        "resolved_at": pc.resolved_at.isoformat() if pc.resolved_at else None,
    }


@router.get("/{workspace_id}/proposed-changes")
def list_proposed_changes(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    workspaces_dir = request.app.state.settings.workspaces_dir
    refresh_staleness(session=session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)
    rows = session.scalars(
        select(ProposedChange)
        .where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.status == "pending",
        )
        .order_by(ProposedChange.created_at)
    ).all()
    session.commit()
    return {"items": [_serialize(pc) for pc in rows]}


@router.post("/{workspace_id}/proposed-changes/{pc_id}/accept")
def accept(
    workspace_id: uuid.UUID,
    pc_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    workspaces_dir = request.app.state.settings.workspaces_dir
    try:
        pc = accept_proposed_change(
            session=session,
            proposed_change_id=pc_id,
            workspace_id=workspace_id,
            approver=user,
            workspaces_dir=workspaces_dir,
        )
    except ProposedChangeNotFound as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="proposed change not found") from exc
    except ProposedChangeNotPending as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SectionStructureError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CommitMutexTimeout as exc:
        session.rollback()
        raise HTTPException(status_code=503, detail="workspace busy; retry") from exc
    except GitCommitError as exc:
        session.rollback()
        raise HTTPException(status_code=500, detail="save failed — nothing was written") from exc
    except IndexError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="no such section") from exc
    session.commit()
    return _serialize(pc)


@router.post("/{workspace_id}/proposed-changes/{pc_id}/reject")
def reject(
    workspace_id: uuid.UUID,
    pc_id: uuid.UUID,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    try:
        pc = reject_proposed_change(
            session=session,
            proposed_change_id=pc_id,
            workspace_id=workspace_id,
            approver=user,
        )
    except ProposedChangeNotFound as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="proposed change not found") from exc
    except ProposedChangeNotPending as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return _serialize(pc)
