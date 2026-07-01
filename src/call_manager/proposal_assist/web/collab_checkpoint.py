"""Explicit "Save a version" checkpoint endpoint (HTTP)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from call_manager.auth.dependencies import get_current_user
from call_manager.db.models import User
from call_manager.proposal_assist.citations.gitio import GitCommitError
from call_manager.proposal_assist.collab import state as collab_state
from call_manager.proposal_assist.collab.checkpoint import checkpoint_artifact
from call_manager.proposal_assist.collab.ws_auth import resolve_access
from call_manager.proposal_assist.drafting.locking import BUSY_ERROR_MESSAGE, CommitMutexTimeout

router = APIRouter()
_VALID = {"draft", "spec"}


@router.post("/api/workspaces/{workspace_id}/collab/{artifact}/checkpoint")
async def save_version(
    workspace_id: uuid.UUID,
    artifact: str,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
):
    if artifact not in _VALID:
        raise HTTPException(status_code=404, detail="unknown artifact")
    session_factory = request.app.state.session_factory
    workspaces_dir = request.app.state.settings.workspaces_dir

    def _do():
        with session_factory() as s:
            # Membership FIRST so a non-member cannot distinguish "exists but
            # collab-off" (409) from "not found" (404) — no existence oracle.
            is_member, can_write = resolve_access(
                s, user=user, workspace_id=workspace_id, artifact=artifact
            )
            if not is_member:
                raise HTTPException(status_code=404, detail="workspace not found")
            if not collab_state.is_collab_enabled(s, workspace_id=workspace_id, artifact=artifact):
                raise HTTPException(status_code=409, detail="collab not enabled for this artifact")
            if not can_write:
                raise HTTPException(status_code=403, detail="read-only")
            # checkpoint_artifact rebuilds the doc from the durable log itself
            # (Slice 1 persists every update, so the log == the live room),
            # holds the commit mutex, and no-ops when nothing new needs saving —
            # so this works whether or not a room is currently open.
            try:
                res = checkpoint_artifact(
                    s, workspace_id=workspace_id, artifact=artifact, workspaces_dir=workspaces_dir
                )
                s.commit()
            except CommitMutexTimeout as exc:
                raise HTTPException(status_code=503, detail=BUSY_ERROR_MESSAGE) from exc
            except GitCommitError as exc:
                # A genuine git failure while committing (a repo-less workspace
                # is already a clean no-op in checkpoint_artifact). Surface a
                # structured retriable status rather than a raw 500.
                raise HTTPException(
                    status_code=503, detail="checkpoint failed — try again"
                ) from exc
            return {
                "committed": res.committed,
                "commit_sha": res.commit_sha,
                "authors": res.authors,
            }

    return await run_in_threadpool(_do)
