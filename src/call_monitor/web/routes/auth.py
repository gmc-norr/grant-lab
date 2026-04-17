from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from call_monitor.auth.domain import domain_allowed
from call_monitor.auth.tokens import (
    TokenAlreadyConsumed,
    TokenExpired,
    TokenNotFound,
    consume_token,
    issue_token,
)
from call_monitor.db.models import User

router = APIRouter()


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

    email_normalized = email.strip().lower()
    client_ip = request.client.host if request.client else "0.0.0.0"

    # Always show the same page. Never leak rate-limit/allowlist state.
    response = request.app.state.templates.TemplateResponse(
        request=request, name="login_sent.html", context={}
    )

    if not ip_limiter.allow(client_ip):
        return response
    if not email_limiter.allow(email_normalized):
        return response
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        return response

    with session_factory() as session:
        user = session.query(User).filter_by(email=email_normalized).one_or_none()
        if user is None:
            user = User(email=email_normalized)
            session.add(user)
            session.flush()

        raw = issue_token(session, user_id=user.id, ttl_minutes=15)
        session.commit()

    link = f"{settings.app_base_url.rstrip('/')}/login/verify?token={raw}"
    email_sender.send(
        to=email_normalized,
        subject="Your Call Monitor login link",
        text_body=f"Click to sign in (valid 15 min):\n\n{link}\n",
    )
    return response


@router.get("/login/verify")
def login_verify(request: Request, token: str):
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer
    settings = request.app.state.settings

    with session_factory() as session:
        try:
            user_id = consume_token(session, token)
        except (TokenNotFound, TokenExpired, TokenAlreadyConsumed) as e:
            raise HTTPException(status_code=400, detail="invalid or expired link") from e

        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=400, detail="invalid link")
        user.last_login_at = datetime.now(UTC)
        session.commit()

    cookie_value = session_signer.make(user_id=user_id)
    # Foundation has no "/" yet — land the user on /me, which exists.
    # The Ingestion plan will replace this with "/" once the dashboard is introduced.
    response = RedirectResponse(url="/me", status_code=303)
    response.set_cookie(
        "session",
        cookie_value,
        max_age=30 * 24 * 3600,
        httponly=True,
        secure=settings.cookie_secure,  # True when APP_BASE_URL is https://
        samesite="lax",
    )
    return response
