"""Admin retention surface: read-only access to soft-deleted workspaces.

Retention Slice B (docs/superpowers/specs/2026-08-16-admin-retention-surface-design.md).
This module REMOVES NOTHING. Purge is deferred to Slice E and requires the
filesystem-activity barrier of Slice D first.

Both routes are deliberately separate from the owner-facing workspace routes.
Those gate on a LIVE row; widening them to serve deleted workspaces would put
the deleted case on the same code path as the live case, where a later edit
could leak one into the other.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User, Workspace

# NO prefix: web/api/__init__.py's router adds /api and the shared error-envelope
# responses. Paths below are written relative to that.
router = APIRouter(tags=["admin-retention"])

_LIST_LIMIT_DEFAULT = 50
_LIST_LIMIT_MAX = 200


class DeletedWorkspaceRow(BaseModel):
    id: uuid.UUID
    title: str
    owner_email: str | None
    soft_deleted_at: datetime


class DeletedWorkspaceListOut(BaseModel):
    items: list[DeletedWorkspaceRow]
    total: int


# Path is RELATIVE. web/api/__init__.py's router supplies the /api prefix along
# with the shared error-envelope responses; an absolute path mounted straight on
# the app would skip those and advertise the wrong 422 shape to the TS codegen.
@router.get("/admin/workspaces/deleted", response_model=DeletedWorkspaceListOut)
def list_deleted_workspaces(
    request: Request,
    limit: int = Query(_LIST_LIMIT_DEFAULT, ge=1, le=_LIST_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),  # noqa: B008
) -> DeletedWorkspaceListOut:
    """List soft-deleted workspaces, newest deletion first.

    Paginated because this grows without bound on a long-lived instance: every
    workspace ever deleted stays here, since nothing removes them.
    """
    sf = request.app.state.session_factory
    with sf() as session:
        total = session.execute(
            select(func.count())
            .select_from(Workspace)
            .where(Workspace.soft_deleted_at.is_not(None))
        ).scalar_one()
        rows = session.execute(
            select(Workspace, User.email)
            .outerjoin(User, User.id == Workspace.owner_id)
            .where(Workspace.soft_deleted_at.is_not(None))
            .order_by(Workspace.soft_deleted_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        items = [
            DeletedWorkspaceRow(
                id=ws.id,
                title=ws.title,
                owner_email=email,
                soft_deleted_at=ws.soft_deleted_at,
            )
            for ws, email in rows
        ]
    return DeletedWorkspaceListOut(items=items, total=total)
