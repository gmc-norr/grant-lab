from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from call_manager.auth.domain import domain_allowed
from call_manager.auth.email import EmailSendFailed
from call_manager.auth.redirects import safe_next
from call_manager.auth.session_cookie import InvalidSession
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
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(...),
    next: str | None = Form(default=None),
):
    settings = request.app.state.settings
    email_limiter = request.app.state.email_limiter
    ip_limiter = request.app.state.ip_limiter
    email_sender = request.app.state.email_sender
    session_factory = request.app.state.session_factory

    # Validate `next` once. It rides into the magic-link URL and the
    # "Back to sign in" link on the check-your-email page. It NEVER
    # influences any allow/deny/rate-limit branch below, so the response
    # stays byte-identical across branches (enumeration-resistance intact).
    safe = safe_next(next)
    back_href = "/login"
    if safe:
        back_href = "/login?" + urlencode({"next": safe})

    # Always show the same page. Never leak rate-limit/allowlist/shape state.
    response = request.app.state.templates.TemplateResponse(
        request=request, name="login_sent.html", context={"back_href": back_href}
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
    # Domain check BEFORE the per-email limiter. Consuming the per-email
    # bucket for an address that will never receive mail (disallowed domain)
    # lets an attacker lock out any *known* allowed email by spamming it
    # — a targeted account-lockout DoS. Only emails that resolve to a real,
    # mail-eligible user should spend a per-email token. The IP limiter
    # above still caps every request regardless of domain.
    #
    # The response is identical on every branch (the shared `response`
    # object), so reordering does NOT change what the client observes —
    # the enumeration-resistance contract is preserved.
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        _logger.info(
            "login rejected: domain not allowed",
            extra={"event": "login_domain_rejected", "email_fp": email_fp},
        )
        return response
    if not email_limiter.allow(email_normalized):
        _logger.info(
            "login rate-limited by email",
            extra={"event": "login_rate_limited_email", "email_fp": email_fp},
        )
        return response

    with session_factory() as session:
        user = session.execute(
            select(User).where(User.email == email_normalized)
        ).scalar_one_or_none()
        # Magic-link is login-only: never auto-provision. Only issue a token for
        # an existing ACTIVE account. The response body is identical on every
        # branch (enumeration-resistant *body*); note the timing residual risk
        # documented in the spec — bounded by the per-email/per-IP limiters.
        if user is None or user.status != "active":
            _logger.info(
                "login skipped: no active account",
                extra={"event": "login_no_active_account", "email_fp": email_fp},
            )
            return response
        raw = issue_token(session, user_id=user.id, ttl_minutes=15)
        session.commit()

    params = {"token": raw}
    if safe:
        params["next"] = safe
    link = f"{settings.app_base_url.rstrip('/')}/login/verify?{urlencode(params)}"
    try:
        email_sender.send(
            to=email_normalized,
            subject="Your Call Manager login link",
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


@router.get("/login/verify", response_class=HTMLResponse)
def login_verify_confirm(request: Request, token: str, next: str | None = None):
    """Render the scanner-proof 'Confirm sign-in' interstitial.

    NO side effects: this does not consume the token. Hospital mail
    gateways (Safe Links / Proofpoint / Mimecast) pre-fetch URLs with a
    GET to scan them, so leaving the token untouched here means the human's
    later POST still works.

    The response OVERRIDES the app-wide `Referrer-Policy: same-origin`
    (set by SecurityHeadersMiddleware, which leaves a handler's own header
    alone). The page extends base.html, which loads same-origin /static
    assets (pico/app CSS+JS, favicon, htmx); under `same-origin` those
    sub-requests would carry the full verify URL — INCLUDING ?token= — in
    their Referer. `strict-origin` sends only scheme+host (no path/query),
    so the token never leaks, while still giving the CSRF check the
    non-null Origin/Referer it needs for the confirm POST. (`no-referrer`
    is forbidden: it nulls Origin on form POSTs → 403.)
    """
    response = request.app.state.templates.TemplateResponse(
        request=request,
        name="login_confirm.html",
        context={"token": token, "next_path": safe_next(next)},
    )
    response.headers["Referrer-Policy"] = "strict-origin"
    return response


@router.post("/login/verify")
def login_verify_consume(
    request: Request,
    token: str = Form(...),
    next: str | None = Form(default=None),
):
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    ip_limiter = request.app.state.ip_limiter
    settings = request.app.state.settings

    # The IP rate-limit lives on the verb that actually consumes the token.
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
        except InvalidToken:
            # A human clicked a stale/used interstitial. Show a dedicated
            # recovery page (HTTP 400, preserving the status semantics)
            # with an inline new-link form that carries `next` forward.
            return request.app.state.templates.TemplateResponse(
                request=request,
                name="login_link_expired.html",
                context={
                    "next_path": safe_next(next),
                    "allowed_domains": settings.allowed_email_domains,
                },
                status_code=400,
            )
        session.commit()

    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="invalid link")
        # Re-check status at consume: the account may have been disabled between
        # the link being mailed and clicked.
        if user.status != "active":
            target = "/auth/pending" if user.status == "pending" else "/auth/no-access"
            return RedirectResponse(url=target, status_code=303)
        user.last_login_at = datetime.now(UTC)
        session_epoch = user.session_epoch
        session.commit()

    cookie_value = session_signer.make(user_id=user_id, session_epoch=session_epoch)
    target = safe_next(next) or "/"
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        "session",
        cookie_value,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,  # True when APP_BASE_URL is https://
        samesite="lax",
    )
    # The POST body carried the single-use token; suppress Referer so the
    # post-login page can't leak anything to third parties. Safe on this
    # redirect (the next request is a GET — no CSRF check — so no Origin:
    # null regression).
    response.headers["Referrer-Policy"] = "no-referrer"
    _logger.info("verify success", extra={"event": "verify_success"})
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
