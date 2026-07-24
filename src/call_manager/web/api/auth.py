from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.auth.domain import domain_allowed
from call_manager.auth.passwords import hash_password, verify_password
from call_manager.auth.redirects import safe_next
from call_manager.auth.session_cookie import InvalidSession
from call_manager.db.models import User
from call_manager.proposal_assist.collab.permissions import signal_user_revocation
from call_manager.web.client_ip import MAX_EMAIL_LEN, client_ip, email_fingerprint

_logger = logging.getLogger(__name__)

router = APIRouter()

# The ONLY login-failure response. Generic + status 400 (NOT 401: unwrap loops to
# /login on 401; NOT 429 on rate-limit: non-disclosure). Identical across
# unknown-email / wrong-password / no-credential / rate-limited.
_INVALID = "Invalid email or password."

_NAME_MAX = 200  # users.name is VARCHAR(200)


class AuthConfigOut(BaseModel):
    google_oidc_enabled: bool
    allowed_domains: list[str]


class LoginIn(BaseModel):
    email: str
    # 1024-char cap: argon2 is O(n) in password length and an adversary can
    # craft a multi-MB payload to spike CPU. Reject before hashing.
    password: str = Field(max_length=1024)
    next: str | None = None


class LoginOut(BaseModel):
    redirect: str


class RegisterIn(BaseModel):
    email: str
    name: str | None = None


class RegisterOut(BaseModel):
    ok: bool


class LogoutOut(BaseModel):
    ok: bool


@router.get("/auth/config", response_model=AuthConfigOut)
def auth_config(request: Request) -> AuthConfigOut:
    """Public, unauthenticated. The pre-auth React pages read this to decide
    whether to show 'Continue with Google' and to derive the email-domain hint —
    they CANNOT call an authenticated /api endpoint (would 401-loop)."""
    s = request.app.state.settings
    return AuthConfigOut(
        google_oidc_enabled=s.google_oidc_enabled,
        allowed_domains=list(s.allowed_email_domains),
    )


@router.post("/auth/login", response_model=LoginOut)
def login(request: Request, body: LoginIn, response: Response) -> LoginOut:
    settings = request.app.state.settings
    email_limiter = request.app.state.email_limiter
    ip_limiter = request.app.state.ip_limiter
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    safe = safe_next(body.next)

    def _denied() -> HTTPException:
        return HTTPException(status_code=400, detail=_INVALID)

    if len(body.email) > MAX_EMAIL_LEN:
        raise _denied()
    email_normalized = body.email.strip().lower()
    email_fp = email_fingerprint(email_normalized)

    if not ip_limiter.allow(client_ip(request)) or not email_limiter.allow(email_normalized):
        _logger.info(
            "login rate-limited", extra={"event": "login_rate_limited", "email_fp": email_fp}
        )
        raise _denied()

    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        stored = user.password_hash if user is not None else None
        # dummy-hash path when stored is None (unknown email → timing parity)
        ok, needs_rehash = verify_password(stored, body.password)
        if not ok:
            _logger.info("login failed", extra={"event": "login_failed", "email_fp": email_fp})
            raise _denied()
        if user.status != "active":
            # pending → /auth/pending; any other non-active status → /auth/no-access.
            # Do NOT hard-code status == "disabled" (silently drops a future status).
            target = "/auth/pending" if user.status == "pending" else "/auth/no-access"
            return LoginOut(redirect=target)  # no cookie
        user.last_login_at = datetime.now(UTC)
        if needs_rehash:
            # Argon2 parameter upgrade: persist the re-hashed credential while we
            # already have the plaintext in hand. Safe to do before commit because
            # the surrounding `with session_factory() as session` block rolls back
            # on any exception.
            user.password_hash = hash_password(body.password)
        user_id = user.id
        session_epoch = user.session_epoch
        session.commit()

    response.set_cookie(
        "session",
        session_signer.make(user_id=user_id, session_epoch=session_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    _logger.info("login success", extra={"event": "login_success", "email_fp": email_fp})
    return LoginOut(redirect=safe or "/")


@router.post("/auth/register", response_model=RegisterOut)
def register(request: Request, body: RegisterIn) -> RegisterOut:
    """Anti-enumeration: ALWAYS 200 {ok: true}. Silent on duplicate / disallowed
    domain / rate-limit; one dummy argon2 for timing parity on every eligible
    attempt."""
    settings = request.app.state.settings
    ip_limiter = request.app.state.ip_limiter
    email_limiter = request.app.state.email_limiter
    session_factory = request.app.state.session_factory
    ok = RegisterOut(ok=True)

    if len(body.email) > MAX_EMAIL_LEN:
        return ok
    email_normalized = body.email.strip().lower()
    if not ip_limiter.allow(client_ip(request)):
        return ok
    # domain_allowed is listed in the 19-tool allowlist for the agent SDK
    # (propose-only mode) — it is read-only and cannot modify state, so it is
    # safe to call here. The silent return keeps anti-enumeration intact.
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        return ok
    if not email_limiter.allow(email_normalized):
        return ok

    # dummy-hash on BOTH fresh & dup paths; return value unused
    verify_password(None, "timing-parity")
    name = (body.name or "").strip()[:_NAME_MAX] or None
    with session_factory() as session:
        session.add(User(email=email_normalized, name=name, status="pending"))
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            _logger.info(
                "register: email already exists",
                extra={
                    "event": "register_duplicate",
                    "email_fp": email_fingerprint(email_normalized),
                },
            )
            return ok
        session.commit()
    _logger.info(
        "register: request created",
        extra={"event": "register_created", "email_fp": email_fingerprint(email_normalized)},
    )
    return ok


@router.post("/auth/logout", response_model=LogoutOut)
def logout(
    request: Request,
    response: Response,
    session: str | None = Cookie(default=None),
) -> LogoutOut:
    """Bump session_epoch (revokes EVERY outstanding cookie for the user) and
    clear the session cookie. Does NOT touch oauth_txn — strict parity with the
    deleted logout_submit. React then full-page-navs to /login."""
    settings = request.app.state.settings
    if session is not None:
        signer = request.app.state.session_signer
        session_factory = request.app.state.session_factory
        try:
            data = signer.verify(session)
        except InvalidSession:
            data = None
        if data is not None:
            revoked = False
            with session_factory() as db:
                user = db.get(User, data.user_id)
                if user is not None:
                    user.session_epoch = (user.session_epoch or 0) + 1
                    db.commit()
                    revoked = True
            if revoked:
                # The epoch bump invalidates the cookie for future requests, but an
                # open collab socket rides cached write permission — tear it down
                # (terminal 1008). Bounded barrier; never raises into the route.
                signal_user_revocation(request, data.user_id)
    response.delete_cookie(
        "session", path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
    return LogoutOut(ok=True)
