from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from grant_lab.auth.dependencies import require_admin
from grant_lab.auth.passwords import WeakPassword, hash_password
from grant_lab.db.models import User

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
            AdminUserOut(id=u.id, email=u.email, name=u.name, status=u.status, is_admin=u.is_admin)
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


class AdminApproveIn(BaseModel):
    role: str = "member"
    initial_password: str | None = None


class AdminActionOut(BaseModel):
    id: int


class AdminRoleIn(BaseModel):
    role: str


class AdminResetPasswordIn(BaseModel):
    new_password: str


def _lock_active_admin_count(db) -> int:
    """Count active admins while LOCKING those rows, so two concurrent
    demote/disable transactions serialize and can't both pass the guard."""
    db.execute(
        select(User.id).where(User.is_admin.is_(True), User.status == "active").with_for_update()
    ).all()
    return db.execute(
        select(func.count())
        .select_from(User)
        .where(User.is_admin.is_(True), User.status == "active")
    ).scalar_one()


@router.post("/admin/users/{user_id}/approve", response_model=AdminActionOut)
def admin_approve_user(
    request: Request,
    user_id: int,
    body: AdminApproveIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    if body.role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "pending":
            raise HTTPException(status_code=400, detail="user is not pending")
        if u.oidc_sub is None and not body.initial_password:
            raise HTTPException(status_code=400, detail="initial password required")
        if body.initial_password:
            try:
                u.password_hash = hash_password(body.initial_password)
            except WeakPassword as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            u.session_epoch = (u.session_epoch or 0) + 1  # credential issued -> revoke prior
        u.status = "active"
        u.is_admin = body.role == "admin"
        u.approved_by_user_id = admin.id
        u.approved_at = datetime.now(UTC)
        db.commit()
    if body.initial_password:
        # A credential was issued (epoch bumped above) — revoke any prior live
        # collab sockets for this account (terminal 1008). Only on the
        # password-issuing path; an OIDC approval without a password bumps nothing.
        # Imported here, not at module scope: this module is in the MATCH edition's
        # surface and collab.permissions pulls the whole collab stack (pycrdt) with it.
        # Pinned by tests/architecture/test_match_edition_process_cut.py.
        from grant_lab.proposal_assist.collab.permissions import signal_user_revocation
        signal_user_revocation(request, user_id)
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/deny", response_model=AdminActionOut)
def admin_deny_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "pending":
            raise HTTPException(status_code=400, detail="user is not pending")
        u.status = "disabled"
        db.commit()
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/release", response_model=AdminActionOut)
def admin_release_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    """Delete a PENDING row so its address is free again. A pending row is a
    registration nobody has approved; deleting it releases a squatted address
    (review 2026-09-02 A11 F1) where `deny` only parked it as disabled, which
    still collides. Anything that was ever active keeps its row and its history."""
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "pending":
            raise HTTPException(status_code=400, detail="only a pending user can be released")
        db.delete(u)
        db.commit()
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/disable", response_model=AdminActionOut)
def admin_disable_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "active":
            raise HTTPException(status_code=400, detail="user is not active")
        if u.is_admin and _lock_active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot disable the last active admin")
        u.status = "disabled"
        u.session_epoch = (u.session_epoch or 0) + 1
        db.commit()
    # Disabled + epoch bumped — tear down the user's live collab sockets (1008).
    # Imported here, not at module scope: this module is in the MATCH edition's
    # surface and collab.permissions pulls the whole collab stack (pycrdt) with it.
    # Pinned by tests/architecture/test_match_edition_process_cut.py.
    from grant_lab.proposal_assist.collab.permissions import signal_user_revocation
    signal_user_revocation(request, user_id)
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/role", response_model=AdminActionOut)
def admin_change_role(
    request: Request,
    user_id: int,
    body: AdminRoleIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    if body.role not in ("member", "admin"):
        raise HTTPException(status_code=400, detail="invalid role")
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "active":
            raise HTTPException(status_code=400, detail="user is not active")
        demoting = u.is_admin and body.role == "member"
        if demoting and _lock_active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="cannot demote the last active admin")
        u.is_admin = body.role == "admin"
        db.commit()
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/enable", response_model=AdminActionOut)
def admin_enable_user(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),  # noqa: B008
):
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        if u.status != "disabled":
            raise HTTPException(status_code=400, detail="user is not disabled")
        u.status = "active"
        db.commit()
    return AdminActionOut(id=user_id)


@router.post("/admin/users/{user_id}/reset-password", response_model=AdminActionOut)
def admin_reset_password(
    request: Request,
    user_id: int,
    body: AdminResetPasswordIn,
    admin: User = Depends(require_admin),  # noqa: B008
):
    # NO status gate — parity: resets a password for a user in any status.
    with request.app.state.session_factory() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="user not found")
        try:
            u.password_hash = hash_password(body.new_password)
        except WeakPassword as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        u.session_epoch = (u.session_epoch or 0) + 1  # revoke the target's sessions
        db.commit()
    # Password reset + epoch bumped — tear down the target's live collab sockets (1008).
    # Imported here, not at module scope: this module is in the MATCH edition's
    # surface and collab.permissions pulls the whole collab stack (pycrdt) with it.
    # Pinned by tests/architecture/test_match_edition_process_cut.py.
    from grant_lab.proposal_assist.collab.permissions import signal_user_revocation
    signal_user_revocation(request, user_id)
    return AdminActionOut(id=user_id)
