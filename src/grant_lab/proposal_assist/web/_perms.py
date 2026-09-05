"""Shared workspace permission guards.

`require_member` = owner OR a workspace_viewers row. For references, members
have full management rights (spec 2026-06-14). `require_owner` stays for
owner-only surfaces (workspace settings)."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from grant_lab.db.models import Workspace, WorkspaceViewer


def require_owner(*, workspace_id: uuid.UUID, user, session: Session) -> Workspace:
    ws = session.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.soft_deleted_at.is_(None))
    )
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id != user.id:
        raise HTTPException(status_code=403, detail="owner only")
    return ws


def require_member(*, workspace_id: uuid.UUID, user, session: Session) -> Workspace:
    ws = session.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.soft_deleted_at.is_(None))
    )
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.owner_id == user.id:
        return ws
    member = session.scalar(
        select(WorkspaceViewer.user_id).where(
            WorkspaceViewer.workspace_id == workspace_id,
            WorkspaceViewer.user_id == user.id,
        )
    )
    if member is None:
        raise HTTPException(status_code=403, detail="not a workspace member")
    return ws


def is_member(*, workspace_id: uuid.UUID, user, session: Session) -> bool:
    """Non-raising membership check for capability computation (used later)."""
    ws = session.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.soft_deleted_at.is_(None),
        )
    )
    if ws is None:
        return False
    if ws.owner_id == user.id:
        return True
    return (
        session.scalar(
            select(WorkspaceViewer.user_id).where(
                WorkspaceViewer.workspace_id == workspace_id,
                WorkspaceViewer.user_id == user.id,
            )
        )
        is not None
    )
