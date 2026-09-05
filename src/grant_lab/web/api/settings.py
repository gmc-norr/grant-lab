"""JSON /api surface for workspace settings + collaborators (Cockpit Slice D).

Owner-gated endpoints use the shared workspace validation constants and
``require_owner`` guard. No database schema changes are introduced here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from grant_lab.auth.dependencies import get_current_user
from grant_lab.db.models import User, WorkspaceEvent, WorkspaceViewer
from grant_lab.proposal_assist.citations.bibliography import (
    CITATION_STYLES as _CITATION_STYLES,
)
from grant_lab.proposal_assist.collab.permissions import signal_from_request
from grant_lab.proposal_assist.ports.stamp import stamp_for_workspace
from grant_lab.proposal_assist.row_locks import WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS
from grant_lab.proposal_assist.web._perms import require_owner
from grant_lab.proposal_assist.web._writes import workspace_write
from grant_lab.proposal_assist.web.workspace_meta import (
    _MAX_WORKSPACE_TITLE_LEN,
    _WORKSPACE_STATUSES,
    _parse_deadline_form,
)
from grant_lab.web.api.workspaces import MemberOut

router = APIRouter()


class SettingsOut(BaseModel):
    title: str
    status: str
    deadline: str | None
    citation_style: str


class SettingsUpdateIn(BaseModel):
    title: str
    status: str
    deadline: str | None = None
    citation_style: str


def _settings_out(ws) -> SettingsOut:
    return SettingsOut(
        title=ws.title,
        status=ws.status,
        deadline=ws.deadline.date().isoformat() if ws.deadline else None,
        citation_style=ws.citation_style,
    )


@router.get("/workspaces/{workspace_id}/settings", response_model=SettingsOut)
def get_settings(
    workspace_id: uuid.UUID,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SettingsOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        return _settings_out(ws)


@router.patch("/workspaces/{workspace_id}/settings", response_model=SettingsOut)
def update_settings(
    workspace_id: uuid.UUID,
    body: SettingsUpdateIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> SettingsOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Body validation stays AFTER require_owner — hoisting it above would let
        # a non-owner with a malformed body get 400 instead of 403/404, leaking
        # that the request shape was wrong to someone with no access. It stays
        # BEFORE the mutex because it takes no lock: a malformed body must not
        # queue behind the commit mutex.
        if body.status not in _WORKSPACE_STATUSES:
            raise HTTPException(status_code=400, detail="invalid status")
        if body.citation_style not in _CITATION_STYLES:
            raise HTTPException(status_code=400, detail="invalid citation_style")
        title = body.title.strip()
        if not title or len(title) > _MAX_WORKSPACE_TITLE_LEN:
            raise HTTPException(status_code=400, detail="invalid title")
        deadline = _parse_deadline_form(body.deadline)  # HTTPException(400) on malformed
        # Stamp minted at the access check (require_owner), before the mutex.
        stamp = stamp_for_workspace(ws)
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner and its lock-free validation, and before any other lock.
        # workspace_write takes the commit mutex at the stamp's generation, bounds
        # every lock wait in the rest of the transaction, and commits INSIDE that
        # bound — soft_delete_workspace_db holds the same mutex for its whole
        # transaction, so a deletion committing while we blocked is observed the
        # instant we win it and WorkspaceDeleted propagates to main.py's 404
        # handler with nothing mutated. Both timeout types become the contracted
        # 429 + Retry-After: 2.
        with workspace_write(
            session,
            stamp=stamp,
            timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            what="settings-update",
        ):
            ws.title = title
            ws.status = body.status
            ws.deadline = deadline
            ws.citation_style = body.citation_style
        # Reads loaded attributes off `ws` and emits no SQL: make_session_factory
        # sets expire_on_commit=False (db/session.py), so the commit above does
        # not expire the instance. Kept outside the bound for that reason — a
        # post-commit SELECT here would be a second untranslated stretch.
        return _settings_out(ws)


class AddMemberIn(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/members", response_model=MemberOut, status_code=201)
def add_member(
    workspace_id: uuid.UUID,
    body: AddMemberIn,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),  # noqa: B008
) -> MemberOut:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Stamp minted at the access check (require_owner), before the mutex.
        stamp = stamp_for_workspace(ws)
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner, before any other lock. WorkspaceDeleted -> main.py -> 404.
        # The bound reaches COMMIT because the WorkspaceEvent insert takes
        # FOR KEY SHARE on its FK parents there — see update_settings' note.
        #
        # The two no-op returns below exit this scope cleanly, so the manager
        # COMMITS an empty transaction where the hand-written version did not
        # (spec §6, a deliberate and pinned behaviour change). Both paths write
        # nothing; test_add_member_no_op_paths_add_no_rows holds that.
        with workspace_write(
            session,
            stamp=stamp,
            timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            what="add-member",
        ):
            # This route's only validation is a DB read on THIS session, so unlike
            # update_settings' body checks it cannot be hoisted above the mutex — a
            # lookup before the lock would read a workspace a committing delete is
            # about to remove. Consequence, deliberately accepted: with a wedged
            # holder an unknown email answers 429 after the timeout rather than 400
            # immediately (which also denies a straddling request a user-existence
            # oracle). Do not "align" this with update_settings by hoisting it.
            target = session.scalar(select(User).where(User.email == body.email))
            # Generic failure (no "no such user" oracle), mirroring the Jinja add_viewer.
            if target is None:
                raise HTTPException(status_code=400, detail="could not add collaborator")
            if target.id == ws.owner_id:
                response.status_code = 200  # owner already has full access — no-op
                return MemberOut(user_id=target.id, email=target.email, role="owner")
            existing = (
                session.query(WorkspaceViewer)
                .filter_by(workspace_id=ws.id, user_id=target.id)
                .first()
            )
            if existing is not None:
                response.status_code = 200  # idempotent
                return MemberOut(user_id=target.id, email=target.email, role="member")
            session.add(WorkspaceViewer(workspace_id=ws.id, user_id=target.id))
            session.add(
                WorkspaceEvent(
                    workspace_id=ws.id,
                    kind="member_added",
                    actor_id=user.id,
                    payload={"target_user_id": target.id, "target_email": target.email},
                )
            )
        # No permission signal: a newly-added member has no live connection to
        # reconcile (connect requires is_member), so connect-time resolution handles them.
        # `target`'s attributes are already loaded and expire_on_commit=False, so this
        # emits no SQL — see update_settings' note on the post-commit read.
        return MemberOut(user_id=target.id, email=target.email, role="member")


@router.delete("/workspaces/{workspace_id}/members/{user_id}", status_code=204)
def remove_member(
    workspace_id: uuid.UUID,
    user_id: int,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
) -> Response:
    with request.app.state.session_factory() as session:
        ws = require_owner(workspace_id=workspace_id, user=user, session=session)
        # Stamp minted at the access check (require_owner), before the mutex.
        stamp = stamp_for_workspace(ws)
        # Slice C2 lock-order rule: the write scope opens immediately after
        # require_owner, before any other lock. WorkspaceDeleted -> main.py -> 404.
        # The bound reaches COMMIT because both the DELETE and the WorkspaceEvent
        # insert flush there — see update_settings' note.
        with workspace_write(
            session,
            stamp=stamp,
            timeout_seconds=WORKSPACE_ROW_LOCK_TIMEOUT_SECONDS,
            what="remove-member",
        ):
            target = session.get(User, user_id)  # resolve email for the audit payload
            deleted = (
                session.query(WorkspaceViewer)
                .filter_by(workspace_id=ws.id, user_id=user_id)
                .delete()
            )
            if deleted:
                session.add(
                    WorkspaceEvent(
                        workspace_id=ws.id,
                        kind="member_removed",
                        actor_id=user.id,
                        payload={
                            "target_user_id": user_id,
                            "target_email": target.email if target else None,
                        },
                    )
                )
    # Stays OUTSIDE both the bound and the session/mutex scope: reconciling live
    # permissions is not part of this transaction and must not extend the stretch
    # that holds the commit mutex.
    signal_from_request(request, workspace_id=workspace_id, artifacts=["draft", "spec"])
    return Response(status_code=204)
