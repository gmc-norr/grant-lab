"""Member-gated propose→accept endpoints for proposed_changes."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import exists, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import CoherenceJob, ProposedChange
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.drafting.locking import CommitMutexTimeout
from call_manager.proposal_assist.editing.save import SectionStructureError, SpecLockedError
from call_manager.proposal_assist.proposals.accepting import (
    CitationAcceptBusy,
    ProposedChangeNotFound,
    ProposedChangeNotPending,
    UnsupportedKernelKind,
    accept_proposed_change,
    reject_proposed_change,
)
from call_manager.proposal_assist.proposals.kernel_accept import (
    accept_via_kernel,
)
from call_manager.proposal_assist.proposals.staleness import refresh_staleness
from call_manager.proposal_assist.web._deps import _session_dep
from call_manager.proposal_assist.web._perms import require_member

router = APIRouter(prefix="/api/workspaces", tags=["proposed-changes"])


def _serialize(pc: ProposedChange) -> dict:
    return {
        "id": str(pc.id),
        "status": pc.status,
        "target_artifact": pc.target_artifact,
        "section_index": pc.section_index,
        "kind": pc.kind,
        "origin_kind": pc.origin_kind,
        "new_text": pc.payload.get("new_text") if pc.kind == "prose_edit" else None,
        "resulting_commit_sha": pc.resulting_commit_sha,
        "created_at": pc.created_at.isoformat() if pc.created_at else None,
        "resolved_by": pc.resolved_by,
        "resolved_at": pc.resolved_at.isoformat() if pc.resolved_at else None,
    }


@router.get("/{workspace_id}/proposed-changes")
def list_proposed_changes(
    workspace_id: uuid.UUID,
    request: Request,
    target_artifact: Literal["draft", "spec"] | None = None,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    require_member(workspace_id=workspace_id, user=user, session=session)
    workspaces_dir = request.app.state.settings.workspaces_dir
    refresh_staleness(session=session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)
    stmt = (
        select(ProposedChange)
        .where(
            ProposedChange.workspace_id == workspace_id,
            ProposedChange.status == "pending",
        )
        .order_by(ProposedChange.created_at)
    )
    # §3.3 visibility gate: a coherence_session row surfaces only once its job is
    # 'completed' — otherwise a half-finished pass flashes partial proposals into
    # <ProposalReview>. Non-coherence rows pass the left disjunct unchanged.
    stmt = stmt.where(
        (ProposedChange.origin_kind != "coherence_session")
        | exists().where(
            CoherenceJob.id == ProposedChange.origin_job_id,
            CoherenceJob.status == "completed",
        )
    )
    if target_artifact is not None:
        stmt = stmt.where(ProposedChange.target_artifact == target_artifact)
    rows = session.scalars(stmt).all()
    session.commit()
    return {"items": [_serialize(pc) for pc in rows]}


def _accept_git_first(session_factory, workspace_id, pc_id, user, workspaces_dir) -> dict:
    """The pre-collab git-first accept path, verbatim — no behavior change."""
    with session_factory() as session:
        require_member(workspace_id=workspace_id, user=user, session=session)
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
        except SpecLockedError as exc:
            session.rollback()
            raise HTTPException(
                status_code=409, detail="spec is locked — unlock to accept"
            ) from exc
        except NotImplementedError as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail="unsupported proposal kind") from exc
        except SectionStructureError as exc:
            # 400 for parity with web/sections.py's handler of the same exception
            # (the proposal's new_text dropped its ## heading or merged sections).
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CitationAcceptBusy as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail="workspace busy; retry the accept") from exc
        except CommitMutexTimeout as exc:
            session.rollback()
            raise HTTPException(
                status_code=429, detail="workspace busy; retry", headers={"Retry-After": "2"}
            ) from exc
        except GitCommitError as exc:
            session.rollback()
            raise HTTPException(
                status_code=500, detail="save failed — nothing was written"
            ) from exc
        except IndexError as exc:
            session.rollback()
            raise HTTPException(status_code=404, detail="no such section") from exc
        session.commit()
        return _serialize(pc)


@router.post("/{workspace_id}/proposed-changes/{pc_id}/accept")
async def accept(
    workspace_id: uuid.UUID,
    pc_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
) -> dict:
    session_factory = request.app.state.session_factory
    workspaces_dir = request.app.state.settings.workspaces_dir

    def _route() -> tuple[bool, str]:
        with session_factory() as s:
            require_member(workspace_id=workspace_id, user=user, session=s)
            pc = s.get(ProposedChange, pc_id)
            if pc is None or pc.workspace_id != workspace_id:
                raise HTTPException(status_code=404, detail="proposed change not found")
            enabled = collab_state.is_collab_enabled(
                s, workspace_id=workspace_id, artifact=pc.target_artifact
            )
            return enabled, pc.target_artifact

    enabled, artifact = await run_in_threadpool(_route)

    if enabled:
        try:
            pc = await accept_via_kernel(
                session_factory=session_factory, workspaces_dir=workspaces_dir,
                workspace_id=workspace_id, pc_id=pc_id, approver=user, artifact=artifact,
            )
        except ProposedChangeNotFound as exc:
            raise HTTPException(status_code=404, detail="proposed change not found") from exc
        except UnsupportedKernelKind as exc:
            raise HTTPException(
                status_code=422,
                detail=f"cannot accept a {exc.args[0]!r} proposal (unsupported)",
            ) from exc
        except SpecLockedError as exc:
            raise HTTPException(
                status_code=409, detail="spec is locked — unlock to accept"
            ) from exc
        except SectionStructureError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProposedChangeNotPending as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CommitMutexTimeout as exc:
            raise HTTPException(
                status_code=429,
                detail="workspace busy; retry",
                headers={"Retry-After": "2"},
            ) from exc
        except GitCommitError as exc:
            raise HTTPException(
                status_code=500, detail="save failed — nothing was written"
            ) from exc
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="no such section") from exc
        return _serialize(pc)

    # flag OFF: the existing git-first accept, verbatim, offloaded to a thread.
    return await run_in_threadpool(
        _accept_git_first, session_factory, workspace_id, pc_id, user, workspaces_dir
    )


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
