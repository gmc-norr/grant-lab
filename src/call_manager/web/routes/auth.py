from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from call_manager.auth.domain import domain_allowed
from call_manager.auth.email import EmailSendFailed
from call_manager.auth.tokens import InvalidToken, consume_token, issue_token
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

    When `settings.trusted_proxy` is True, we read the **rightmost** entry
    in X-Forwarded-For. Nginx's `$proxy_add_x_forwarded_for` (and most
    reverse proxies on default settings) *appends* the connecting-peer IP
    to any XFF the client sent, so the rightmost is the only IP the
    trusted proxy vouches for — the leftmost is whatever the client
    wanted to claim, which makes it a free rate-limit bypass if trusted.

    Deployments with multiple trusted hops need to strip proxy IPs off
    the right before the client IP emerges; that's out of scope for v1
    (single hospital-edge proxy). If we ever terminate TLS behind a
    second trusted layer, generalize this to `split(",")[-hops]`.

    Otherwise we use `request.client` directly and fail loudly (400) if
    it's absent — silently bucketing unknown clients into a shared
    "0.0.0.0" key breaks the rate limiter for all legitimate traffic.
    """
    settings = request.app.state.settings
    if getattr(settings, "trusted_proxy", False):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Rightmost non-empty entry. `split(",")` followed by reversed
            # walk handles stray whitespace and trailing commas defensively.
            for entry in reversed(xff.split(",")):
                peer = entry.strip()
                if peer:
                    return peer
    if request.client is None:
        raise HTTPException(status_code=400, detail="cannot determine client")
    return request.client.host


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return request.app.state.templates.TemplateResponse(
        request=request, name="login.html", context={}
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...)):
    settings = request.app.state.settings
    email_limiter = request.app.state.email_limiter
    ip_limiter = request.app.state.ip_limiter
    email_sender = request.app.state.email_sender
    session_factory = request.app.state.session_factory

    # Always show the same page. Never leak rate-limit/allowlist/shape state.
    response = request.app.state.templates.TemplateResponse(
        request=request, name="login_sent.html", context={}
    )

    # 1. Length cap BEFORE anything that keys off the email. Prevents an
    #    unbounded email field from growing the rate-limiter dict.
    if len(email) > MAX_EMAIL_LEN:
        _logger.info(
            "login rejected: email too long",
            extra={"event": "login_rejected_oversized"},
        )
        return response

    email_normalized = email.strip().lower()
    client_ip = _client_ip(request)
    email_fp = _email_fingerprint(email_normalized)

    _logger.info(
        "login requested",
        extra={"event": "login_requested", "email_fp": email_fp, "ip": client_ip},
    )

    if not ip_limiter.allow(client_ip):
        _logger.info(
            "login rate-limited by IP",
            extra={"event": "login_rate_limited_ip", "ip": client_ip},
        )
        return response
    if not email_limiter.allow(email_normalized):
        _logger.info(
            "login rate-limited by email",
            extra={"event": "login_rate_limited_email", "email_fp": email_fp},
        )
        return response
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        _logger.info(
            "login rejected: domain not allowed",
            extra={"event": "login_domain_rejected", "email_fp": email_fp},
        )
        return response

    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        if user is None:
            user = User(email=email_normalized)
            session.add(user)
            session.flush()

        raw = issue_token(session, user_id=user.id, ttl_minutes=15)
        session.commit()

    link = f"{settings.app_base_url.rstrip('/')}/login/verify?token={raw}"
    try:
        email_sender.send(
            to=email_normalized,
            subject="Your Call Monitor login link",
            text_body=f"Click to sign in (valid 15 min):\n\n{link}\n",
        )
    except EmailSendFailed:
        # Log and return the same "check your email" page. Raising would
        # surface a 500 which (a) leaks that this address was worth sending
        # to and (b) leaves the user behind the email rate-limit for 15 min.
        _logger.warning(
            "email send failed",
            extra={"event": "login_email_send_failed", "email_fp": email_fp},
            exc_info=True,
        )
        return response

    _logger.info(
        "login email sent",
        extra={"event": "login_email_sent", "email_fp": email_fp},
    )
    return response


@router.get("/login/verify")
def login_verify(request: Request, token: str):
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    ip_limiter = request.app.state.ip_limiter
    settings = request.app.state.settings

    # Rate-limit by IP: the token is 32-byte secrets.token_urlsafe so an
    # online guess is computationally infeasible, but this is a cheap
    # brake against future regressions and straightforward DDoS (each
    # attempt is one DB UPDATE).
    client_ip = _client_ip(request)
    if not ip_limiter.allow(client_ip):
        _logger.info(
            "verify rate-limited by IP",
            extra={"event": "verify_rate_limited_ip", "ip": client_ip},
        )
        raise HTTPException(status_code=429, detail="too many requests")

    # Commit consumption in its own transaction so any later failure
    # (user missing, DB hiccup, code change that raises) cannot roll the
    # consumption back. Single-use must not depend on what happens after.
    with session_factory() as session:
        try:
            user_id = consume_token(session, token)
        except InvalidToken as e:
            raise HTTPException(status_code=400, detail="invalid or expired link") from e
        session.commit()

    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="invalid link")
        user.last_login_at = datetime.now(UTC)
        session.commit()

    cookie_value = session_signer.make(user_id=user_id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        "session",
        cookie_value,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,  # True when APP_BASE_URL is https://
        samesite="lax",
    )
    _logger.info("verify success", extra={"event": "verify_success"})
    return response


@router.post("/logout")
def logout_submit():
    """Clear the session cookie and bounce back to the login page.

    POST (not GET) so a stray link can't end someone's session — same
    safety we get from the CSRF middleware on every other state-changing
    route. Browsers respect samesite=lax on the deletion too.
    """
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session", samesite="lax")
    return response
