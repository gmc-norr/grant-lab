# src/grant_lab/proposal_assist/collab/ws_auth.py
"""WebSocket-context auth + connect-time permission resolution.

The HTTP get_current_user dependency is Request/Cookie-bound and cannot run in
a WebSocket handshake. user_from_cookie() factors out the verification so both
the WS adapter here and tests can call it directly. resolve_access() computes
(is_member, can_write) once at connect; mid-session re-evaluation is Slice 2.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from grant_lab.auth.session_cookie import InvalidSession, SessionSigner
from grant_lab.db.models import User, Workspace, WorkspaceViewer


class WSAuthError(Exception):
    """Connection cannot be authenticated/authorized; the route closes the socket."""


def user_from_cookie(cookie: str | None, *, signer: SessionSigner, session_factory) -> User:
    """Resolve the authenticated User from a raw session cookie value.

    Mirrors get_current_user's checks (verify -> active status -> epoch match)
    but is Request-free so it works from a WebSocket. Raises WSAuthError on any
    failure (the WS layer maps that to a close, not an HTTP status).
    """
    if not cookie:
        raise WSAuthError("no session cookie")
    try:
        data = signer.verify(cookie)
    except InvalidSession as e:
        raise WSAuthError("invalid session") from e
    with session_factory() as db:
        user = db.get(User, data.user_id)
        if user is None:
            raise WSAuthError("user not found")
        if user.status != "active":
            raise WSAuthError("account inactive")
        if user.session_epoch != data.session_epoch:
            raise WSAuthError("session revoked")
        db.expunge(user)
    return user


async def resolve_ws_user(websocket) -> User:
    """Adapter: read the cookie + app state off the WebSocket and resolve a User.

    Runs the sync DB lookup on a worker thread to avoid blocking the event loop.
    """
    from starlette.concurrency import run_in_threadpool

    cookie = websocket.cookies.get("session")
    signer: SessionSigner = websocket.app.state.session_signer
    session_factory = websocket.app.state.session_factory
    return await run_in_threadpool(
        user_from_cookie, cookie, signer=signer, session_factory=session_factory
    )


def check_origin(websocket) -> bool:
    """Cross-site WebSocket hijacking (CSWSH) defense.

    The HTTP CSRF middleware does not protect WS handshakes (the upgrade is a
    GET — a safe method — so it passes straight through). A cookie-authenticated
    WS would otherwise be a CSRF write hole: a malicious page could open
    ws(s)://our-app/... and the browser would attach the victim's session cookie.

    Mirror the CSRF middleware's Origin semantics EXACTLY by reusing its
    canonical host normalizer (single source of truth — do not reimplement
    origin parsing): reject a present-but-mismatched Origin (and `null`); allow
    a matching Origin; allow an ABSENT Origin (non-browser clients — TestClient,
    server-to-server, our pycrdt test clients — don't send one, and a browser
    always does on a cross-site handshake, so absence is not the attack vector).
    """
    from grant_lab.auth.csrf import _hostport

    expected = _hostport(websocket.app.state.settings.app_base_url)
    if not expected:
        return True
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    if origin == "null":
        return False
    return _hostport(origin) == expected


def resolve_access(
    session: Session, *, user: User, workspace_id: uuid.UUID, artifact: str
) -> tuple[bool, bool]:
    """Return (is_member, can_write) for this user on this (workspace, artifact).

    is_member: owner or a workspace_viewers row. Non-members get no access.
    can_write (connect-time):
      - setup phase            -> owner only
      - locked spec            -> owner only
      - else (active draft /
        unlocked spec)         -> any member
    """
    ws = session.get(Workspace, workspace_id)
    if ws is None or ws.soft_deleted_at is not None:
        return (False, False)
    is_owner = ws.owner_id == user.id
    is_viewer = (
        session.execute(
            select(WorkspaceViewer.user_id).where(
                WorkspaceViewer.workspace_id == workspace_id,
                WorkspaceViewer.user_id == user.id,
            )
        ).first()
        is not None
    )
    is_member = is_owner or is_viewer
    if not is_member:
        return (False, False)

    if ws.current_phase == "setup":
        return (True, is_owner)
    if artifact == "spec" and ws.spec_locked_at is not None:
        return (True, is_owner)
    return (True, True)
