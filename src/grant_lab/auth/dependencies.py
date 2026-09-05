from __future__ import annotations

from fastapi import Cookie, HTTPException, Request

from grant_lab.auth.session_cookie import InvalidSession
from grant_lab.db.models import User


class AccountInactive(Exception):
    """Authenticated cookie, but the account is not active (pending/disabled)."""

    def __init__(self, status: str):
        self.status = status
        super().__init__(status)


def get_current_user(
    request: Request, session: str | None = Cookie(default=None, include_in_schema=False)
) -> User:
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    signer = request.app.state.session_signer
    session_factory = request.app.state.session_factory
    try:
        data = signer.verify(session)
    except InvalidSession as e:
        raise HTTPException(status_code=401, detail="invalid session") from e

    with session_factory() as db:
        user = db.get(User, data.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="user not found")
        # Status gate BEFORE the epoch check: the admin disable flow sets
        # status='disabled' AND bumps session_epoch in the same transaction. If
        # the epoch check ran first it would mask a disabled account as a generic
        # 401 ("session revoked" -> /login), so the AccountInactive handler
        # (-> /auth/no-access, clearing the cookie) would never fire for the
        # canonical disable path. Checking status first routes pending/disabled
        # accounts to their holding page regardless of epoch.
        if user.status != "active":
            raise AccountInactive(user.status)
        # Reject cookies minted before a logout / admin revocation bumped the
        # epoch. Stateless cookies are otherwise valid for their full TTL.
        if user.session_epoch != data.session_epoch:
            raise HTTPException(status_code=401, detail="session revoked")
        db.expunge(user)
    return user


def require_admin(
    request: Request, session: str | None = Cookie(default=None, include_in_schema=False)
) -> User:
    user = get_current_user(request, session=session)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin required")
    return user
