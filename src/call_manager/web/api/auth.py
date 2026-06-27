from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.auth.domain import domain_allowed
from call_manager.auth.passwords import verify_password
from call_manager.auth.redirects import safe_next
from call_manager.db.models import User
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
    password: str
    next: str | None = None


class LoginOut(BaseModel):
    redirect: str


class RegisterIn(BaseModel):
    email: str
    name: str | None = None


class RegisterOut(BaseModel):
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
        if not verify_password(stored, body.password):  # dummy-hash path when stored is None
            _logger.info("login failed", extra={"event": "login_failed", "email_fp": email_fp})
            raise _denied()
        if user.status != "active":
            # Mirrors web/routes/auth.py:140 — pending vs the no-access CATCH-ALL.
            # Do NOT hard-code status == "disabled" (silently drops a future status).
            target = "/auth/pending" if user.status == "pending" else "/auth/no-access"
            return LoginOut(redirect=target)  # no cookie
        user.last_login_at = datetime.now(UTC)
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
    attempt. Verbatim port of web/routes/register.py."""
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
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        return ok
    if not email_limiter.allow(email_normalized):
        return ok

    verify_password(None, "timing-parity")  # dummy-hash on BOTH fresh & dup paths
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
