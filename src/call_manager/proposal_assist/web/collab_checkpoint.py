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
                # TWO statuses for one user-visible condition on this one route,
                # recorded here because nothing else records it:
                #   * failing to ACQUIRE the mutex (this arm)          -> 503
                #   * a bounded mutex-held TAIL giving up (55P03),
                #     answered centrally by main.py's OperationalError
                #     handler, which never sees this exception class   -> 429
                # Both mean "another writer holds the workspace; retry" — but a
                # caller cannot tell that from the response: this is an /api
                # route, so main.py's StarletteHTTPException handler replaces
                # the body of every >= 500 response with the fixed internal_error
                # envelope, and BUSY_ERROR_MESSAGE never reaches the wire; the
                # 429 carries {"code": "too_many_requests", "message":
                # BUSY_ERROR_MESSAGE} plus Retry-After: 2. Status, envelope
                # code, message AND Retry-After all differ.
                #
                # It exists because this route is the documented 503 holdout (see
                # CommitMutexTimeout's docstring in drafting/locking.py — every
                # other route answers 429 + Retry-After: 2) while the tail path
                # was added centrally by Slice C3 and cannot know about the
                # holdout. Left as-is deliberately: the 503 is pinned by
                # tests/proposal_assist/test_mutex_tail_bound.py's
                # test_the_collab_checkpoint_route_still_answers_503_not_the_new_429,
                # so aligning the two is a behaviour change for its own slice —
                # this comment only records that the split is known, not accidental.
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
