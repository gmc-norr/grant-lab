from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from call_manager.auth.passwords import verify_password
from call_manager.auth.redirects import safe_next
from call_manager.auth.session_cookie import InvalidSession
from call_manager.db.models import User

_logger = logging.getLogger(__name__)

router = APIRouter()

# Hard cap matches users.email VARCHAR(320). Reject oversized inputs
# *before* they reach the rate limiter so an attacker can't grow the
# limiter dict with junk keys (memory-amplification DoS).
MAX_EMAIL_LEN = 320


def _email_fingerprint(email: str) -> str:
    """Short hex of sha256(email). Safe to log — doesn't expose the
    address but lets an operator correlate events across log lines."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]


def _client_ip(request: Request) -> str:
    """Resolve the caller's IP.

    When `settings.trusted_proxy` is True, we read the client IP from
    X-Forwarded-For. Each trusted reverse-proxy hop *appends* the
    connecting-peer IP (Nginx's `$proxy_add_x_forwarded_for` and most
    proxies on default settings), so with `trusted_proxy_hops = N` the
    real client is the Nth entry from the right. Anything to the LEFT of
    that is client-supplied and untrusted — taking it would be a free
    rate-limit bypass.

    `trusted_proxy_hops` MUST match the actual deployment topology. If it
    is too small, an attacker's spoofed XFF entry leaks through; if it is
    too large (more hops claimed than exist), every external client
    collapses into one bucket. We therefore reject (400) an XFF that has
    fewer entries than the configured hop count rather than silently
    falling back to a spoofable or shared value.

    Otherwise we use `request.client` directly and fail loudly (400) if
    it's absent — silently bucketing unknown clients into a shared
    "0.0.0.0" key breaks the rate limiter for all legitimate traffic.
    """
    settings = request.app.state.settings
    if getattr(settings, "trusted_proxy", False):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            hops = getattr(settings, "trusted_proxy_hops", 1)
            # Keep only non-empty entries (tolerate stray whitespace / trailing
            # commas), preserving left-to-right order.
            entries = [e.strip() for e in xff.split(",") if e.strip()]
            if len(entries) >= hops:
                # Nth-from-the-right: index -hops.
                return entries[-hops]
            # Fewer entries than trusted hops — the header doesn't match the
            # configured topology. Don't trust a value we can't place; fail
            # loudly rather than mis-bucket.
            raise HTTPException(status_code=400, detail="malformed X-Forwarded-For")
    if request.client is None:
        raise HTTPException(status_code=400, detail="cannot determine client")
    return request.client.host


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None):
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next_path": safe_next(next),
            "allowed_domains": settings.allowed_email_domains,
            "google_oidc_enabled": settings.google_oidc_enabled,
            "error": None,
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str | None = Form(default=None),
):
    settings = request.app.state.settings
    email_limiter = request.app.state.email_limiter
    ip_limiter = request.app.state.ip_limiter
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    safe = safe_next(next)

    def _denied() -> HTMLResponse:
        # One generic message for unknown email / wrong password / no-credential /
        # rate-limited. Status 200 so the global 401->/login handler can't loop.
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "next_path": safe,
                "allowed_domains": settings.allowed_email_domains,
                "google_oidc_enabled": settings.google_oidc_enabled,
                "error": "Invalid email or password.",
            },
        )

    if len(email) > MAX_EMAIL_LEN:
        return _denied()
    email_normalized = email.strip().lower()
    email_fp = _email_fingerprint(email_normalized)

    if not ip_limiter.allow(_client_ip(request)) or not email_limiter.allow(email_normalized):
        _logger.info(
            "login rate-limited",
            extra={"event": "login_rate_limited", "email_fp": email_fp},
        )
        return _denied()

    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        # verify_password runs argon2 even when user is None / has no hash
        # (dummy path), so timing doesn't reveal account existence.
        stored = user.password_hash if user is not None else None
        if not verify_password(stored, password):
            _logger.info("login failed", extra={"event": "login_failed", "email_fp": email_fp})
            return _denied()
        if user.status != "active":
            target = "/auth/pending" if user.status == "pending" else "/auth/no-access"
            return RedirectResponse(url=target, status_code=303)
        user.last_login_at = datetime.now(UTC)
        user_id = user.id
        session_epoch = user.session_epoch
        session.commit()

    response = RedirectResponse(url=safe or "/", status_code=303)
    response.set_cookie(
        "session",
        session_signer.make(user_id=user_id, session_epoch=session_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    _logger.info("login success", extra={"event": "login_success", "email_fp": email_fp})
    return response


@router.post("/logout")
def logout_submit(request: Request, session: str | None = Cookie(default=None)):
    """Bump the user's session epoch, clear the cookie, bounce to /login.

    POST (not GET) so a stray link can't end someone's session — same
    safety we get from the CSRF middleware on every other state-changing
    route.

    Bumping session_epoch invalidates *every* outstanding cookie for this
    user (other browsers, a stolen copy), not just the one in this browser —
    stateless signed cookies can't otherwise be revoked before their TTL.
    """
    settings = request.app.state.settings
    if session is not None:
        session_signer = request.app.state.session_signer
        session_factory = request.app.state.session_factory
        try:
            data = session_signer.verify(session)
        except InvalidSession:
            data = None
        if data is not None:
            with session_factory() as db:
                user = db.get(User, data.user_id)
                if user is not None:
                    user.session_epoch = (user.session_epoch or 0) + 1
                    db.commit()

    response = RedirectResponse(url="/login", status_code=303)
    # Mirror the set_cookie attributes so the browser reliably clears it.
    response.delete_cookie(
        "session",
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return response
