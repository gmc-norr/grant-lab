from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from grant_lab.auth.accounts import lock_user
from grant_lab.auth.domain import domain_allowed, sign_in_allowed
from grant_lab.auth.passwords import hash_password, verify_password
from grant_lab.auth.redirects import safe_next
from grant_lab.auth.session_cookie import InvalidSession
from grant_lab.db.models import User
from grant_lab.web.client_ip import MAX_EMAIL_LEN, client_ip, email_fingerprint

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
        # Locked (security review 2026-10-31 S1): the rehash below WRITES the
        # hash, and unlocked it put the old password back over an admin reset
        # that committed between this verify and this commit.
        user = lock_user(session, User.email == email_normalized)
        stored = user.password_hash if user is not None else None
        # dummy-hash path when stored is None (unknown email → timing parity)
        ok, needs_rehash = verify_password(stored, body.password)
        if not ok:
            _logger.info("login failed", extra={"event": "login_failed", "email_fp": email_fp})
            raise _denied()
        # ok is True only for a real stored hash, which only an existing user has.
        assert user is not None
        if user.status != "active":
            # pending → /auth/pending; any other non-active status → /auth/no-access.
            # Do NOT hard-code status == "disabled" (silently drops a future status).
            target = "/auth/pending" if user.status == "pending" else "/auth/no-access"
            return LoginOut(redirect=target)  # no cookie
        if not sign_in_allowed(user, settings.allowed_email_domains):
            # A member whose domain left ALLOWED_EMAIL_DOMAINS (security review
            # 2026-10-31 S3); admins are exempt -- see auth.domain.sign_in_allowed.
            _logger.info(
                "login refused: email domain not allowed",
                extra={"event": "login_domain_refused", "email_fp": email_fp},
            )
            return LoginOut(redirect="/auth/no-access")  # no cookie
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
    # Silent return keeps anti-enumeration intact.
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
            with session_factory() as db:
                user = lock_user(db, User.id == data.user_id)  # S1: epoch writer
                # The epoch must MATCH, not merely be signature-valid. Verifying
                # the signature alone let a cookie that an earlier logout, an
                # admin disable or a password reset had already revoked bump the
                # epoch again — so whoever still held a dead cookie could log the
                # real user out of the session that replaced it, repeatedly.
                # `get_current_user` has always made this comparison
                # (auth/dependencies.py); this route reached past it for the
                # cookie and never did.
                if user is not None and (user.session_epoch or 0) == data.session_epoch:
                    user.session_epoch = (user.session_epoch or 0) + 1
                    db.commit()
    response.delete_cookie(
        "session", path="/", httponly=True, secure=settings.cookie_secure, samesite="lax"
    )
    return LogoutOut(ok=True)
