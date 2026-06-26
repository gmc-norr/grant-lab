from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from call_manager.auth.dependencies import require_admin
from call_manager.db.models import User

router = APIRouter()

_PER_PAGE = 50


class AdminUserOut(BaseModel):
    id: int
    email: str
    name: str | None
    status: str
    is_admin: bool


class AdminUsersPageOut(BaseModel):
    users: list[AdminUserOut]
    page: int
    per_page: int
    total: int


class AdminPendingUserOut(BaseModel):
    id: int
    email: str
    name: str | None
    created_at: datetime
    google_linked: bool


class AdminPendingPageOut(BaseModel):
    users: list[AdminPendingUserOut]
    page: int


@router.get("/admin/users", response_model=AdminUsersPageOut)
def admin_users_list(
    request: Request,
    page: int = 1,
    admin: User = Depends(require_admin),  # noqa: B008
):
    page = max(1, page)
    with request.app.state.session_factory() as db:
        rows = (
            db.execute(
                select(User)
                .where(User.status != "pending")
                .order_by(User.email.asc())
                .limit(_PER_PAGE)
                .offset((page - 1) * _PER_PAGE)
            )
            .scalars()
            .all()
        )
        total = db.execute(
            select(func.count()).select_from(User).where(User.status != "pending")
        ).scalar_one()
        users = [
            AdminUserOut(
                id=u.id, email=u.email, name=u.name, status=u.status, is_admin=u.is_admin
            )
            for u in rows
        ]
    return AdminUsersPageOut(users=users, page=page, per_page=_PER_PAGE, total=total)


@router.get("/admin/users/pending", response_model=AdminPendingPageOut)
def admin_pending_list(
    request: Request,
    page: int = 1,
    admin: User = Depends(require_admin),  # noqa: B008
):
    page = max(1, page)
    with request.app.state.session_factory() as db:
        rows = (
            db.execute(
                select(User)
                .where(User.status == "pending")
                .order_by(User.created_at.asc())
                .limit(_PER_PAGE)
                .offset((page - 1) * _PER_PAGE)
            )
            .scalars()
            .all()
        )
        users = [
            AdminPendingUserOut(
                id=u.id,
                email=u.email,
                name=u.name,
                created_at=u.created_at,
                google_linked=u.oidc_sub is not None,
            )
            for u in rows
        ]
    return AdminPendingPageOut(users=users, page=page)
