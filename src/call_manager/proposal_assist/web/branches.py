"""Branch routes. Owner-only for anything that changes state; members may list.

This module owns the ONE cross-thread wait in the feature: the room teardown is
asyncio and a switch is a sync request holding the commit mutex, so the bridge
lives here — at the edge, where its timeout is visible — rather than inside
branches.switch_branch.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from call_manager.auth.dependencies import get_current_user
from call_manager.proposal_assist.branches import (
    BranchBusy,
    BranchExists,
    BranchInUse,
    BranchLast,
    BranchNameInvalid,
    BranchNotFound,
    create_workspace_branch,
    delete_workspace_branch,
    list_workspace_branches,
    switch_branch,
)
from call_manager.proposal_assist.collab.room import MANAGER, BranchSwitchTeardown
from call_manager.proposal_assist.ports.stamp import stamp_for_workspace
from call_manager.proposal_assist.web._deps import _session_dep
from call_manager.proposal_assist.web._perms import require_member, require_owner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspaces", tags=["branches"])

#: Bound on the cross-thread wait for the room teardown. This wait happens while
#: the switch transaction holds the commit mutex, so an unbounded one would pin a
#: worker and the workspace together. Comfortably above the room-lock and ops
#: drain budgets the teardown itself uses (room.py:46-53).
_TEARDOWN_BARRIER_SECONDS = 15.0


class BranchListOut(BaseModel):
    branches: list[str]
    current: str


class BranchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def _teardown_via_loop(request: Request):
    """Build the sync callable switch_branch expects.

    Mirrors lifecycle.signal_workspace_restore (lifecycle.py:145-182): schedule
    onto the app loop, wait bounded, and treat "could not schedule" and "timed
    out" as NOT LOCKED — the conservative reading, which makes the switch abort
    rather than proceed on an unproven teardown.
    """

    def _run(workspace_id: uuid.UUID) -> BranchSwitchTeardown:
        loop = getattr(request.app.state, "app_loop", None)
        if loop is None:
            logger.warning(
                "branches.bridge_unavailable",
                extra={"event": "branch_bridge_unavailable", "workspace_id": str(workspace_id)},
            )
            return BranchSwitchTeardown(closed=0, locked=False)
        coro = MANAGER.prepare_branch_switch(workspace_id)
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            coro.close()  # never-awaited coroutine -> close it before reporting
            logger.warning(
                "branches.teardown_schedule_failed",
                extra={
                    "event": "branch_teardown_schedule_failed",
                    "workspace_id": str(workspace_id),
                },
                exc_info=True,
            )
            return BranchSwitchTeardown(closed=0, locked=False)
        try:
            return fut.result(timeout=_TEARDOWN_BARRIER_SECONDS)
        except concurrent.futures.TimeoutError:
            logger.warning(
                "branches.teardown_barrier_timeout",
                extra={
                    "event": "branch_teardown_barrier_timeout",
                    "workspace_id": str(workspace_id),
                },
            )
            return BranchSwitchTeardown(closed=0, locked=False)

    return _run


#: Every refusal this module can produce, mapped to a status the UI can act on.
#: Without this they reach main.py's generic handler as 500s, and "try again" is
#: indistinguishable from "you did something wrong".
_STATUS = {
    BranchNameInvalid: 422,
    BranchNotFound: 404,
    BranchExists: 409,
    BranchInUse: 409,
    BranchLast: 409,
    BranchBusy: 409,
}


def _raise_http(exc: Exception):
    for kind, code in _STATUS.items():
        if isinstance(exc, kind):
            raise HTTPException(status_code=code, detail=str(exc)) from exc
    raise exc


@router.get("/{workspace_id}/branches", response_model=BranchListOut)
def list_branches_route(
    workspace_id: uuid.UUID,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
):
    require_member(workspace_id=workspace_id, user=user, session=session)
    names, current = list_workspace_branches(
        session,
        workspace_id=workspace_id,
        workspaces_dir=request.app.state.settings.workspaces_dir,
    )
    return BranchListOut(branches=names, current=current)


@router.post("/{workspace_id}/branches", status_code=201)
def create_branch_route(
    workspace_id: uuid.UUID,
    body: BranchIn,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
):
    ws = require_owner(workspace_id=workspace_id, user=user, session=session)
    try:
        create_workspace_branch(
            session,
            stamp=stamp_for_workspace(ws),
            workspace_id=workspace_id,
            name=body.name,
            workspaces_dir=request.app.state.settings.workspaces_dir,
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        _raise_http(exc)
    return {"name": body.name}


@router.post("/{workspace_id}/branches/switch", response_model=BranchListOut)
def switch_branch_route(
    workspace_id: uuid.UUID,
    body: BranchIn,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
):
    ws = require_owner(workspace_id=workspace_id, user=user, session=session)
    workspaces_dir = request.app.state.settings.workspaces_dir
    try:
        switch_branch(
            session,
            stamp=stamp_for_workspace(ws),
            workspace_id=workspace_id,
            target=body.name,
            workspaces_dir=workspaces_dir,
            teardown=_teardown_via_loop(request),
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        # Compensating checkout: step 9 is not transactional, so a rollback after
        # it leaves HEAD on the target while the column names the source. §4.1's
        # reconciliation would repair this on the next access anyway; doing it
        # here means the very next git read is already correct.
        try:
            from call_manager.proposal_assist.branches import _reconcile_head

            _reconcile_head(session, workspace_id=workspace_id, workspaces_dir=workspaces_dir)
        except Exception:
            logger.warning(
                "branches.compensating_checkout_failed",
                extra={
                    "event": "branch_compensating_checkout_failed",
                    "workspace_id": str(workspace_id),
                },
                exc_info=True,
            )
        _raise_http(exc)
    names, current = list_workspace_branches(
        session, workspace_id=workspace_id, workspaces_dir=workspaces_dir
    )
    return BranchListOut(branches=names, current=current)


#: `{name:path}` and NOT `{name}`. `git check-ref-format --branch` accepts a
#: slash — `aims/ambitious` is a valid branch name and task 1's tests assert it —
#: while Starlette's default converter stops at a separator, so a branch the
#: create route accepts would have had NO matching delete route.
@router.delete("/{workspace_id}/branches/{name:path}", status_code=204)
def delete_branch_route(
    workspace_id: uuid.UUID,
    name: str,
    request: Request,
    session: Session = Depends(_session_dep),  # noqa: B008
    user=Depends(get_current_user),  # noqa: B008
):
    ws = require_owner(workspace_id=workspace_id, user=user, session=session)
    try:
        delete_workspace_branch(
            session,
            stamp=stamp_for_workspace(ws),
            workspace_id=workspace_id,
            name=name,
            workspaces_dir=request.app.state.settings.workspaces_dir,
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        _raise_http(exc)
    return Response(status_code=204)
