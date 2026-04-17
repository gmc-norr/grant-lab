from __future__ import annotations

from fastapi import Cookie, HTTPException, Request

from call_monitor.auth.session_cookie import InvalidSession
from call_monitor.db.models import User


def get_current_user(request: Request, session: str | None = Cookie(default=None)) -> User:
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    signer = request.app.state.session_signer
    session_factory = request.app.state.session_factory
    try:
        user_id = signer.verify(session)
    except InvalidSession as e:
        raise HTTPException(status_code=401, detail="invalid session") from e

    with session_factory() as db:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="user not found")
        db.expunge(user)
    return user


def require_admin(request: Request, session: str | None = Cookie(default=None)) -> User:
    user = get_current_user(request, session=session)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin required")
    return user
