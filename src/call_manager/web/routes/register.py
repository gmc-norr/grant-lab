from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import IntegrityError

from call_manager.auth.domain import domain_allowed
from call_manager.auth.passwords import verify_password
from call_manager.db.models import User
from call_manager.web.routes.auth import MAX_EMAIL_LEN, _client_ip, _email_fingerprint

_logger = logging.getLogger(__name__)
router = APIRouter()

_NAME_MAX = 200  # users.name is VARCHAR(200)


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    settings = request.app.state.settings
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"allowed_domains": settings.allowed_email_domains},
    )


@router.post("/register", response_class=HTMLResponse)
def register_submit(request: Request, email: str = Form(...), name: str = Form(default="")):
    settings = request.app.state.settings
    ip_limiter = request.app.state.ip_limiter
    email_limiter = request.app.state.email_limiter
    session_factory = request.app.state.session_factory

    def _received() -> HTMLResponse:
        return request.app.state.templates.TemplateResponse(
            request=request, name="register_received.html", context={}
        )

    if len(email) > MAX_EMAIL_LEN:
        return _received()
    email_normalized = email.strip().lower()
    if not ip_limiter.allow(_client_ip(request)):
        return _received()
    if not domain_allowed(email_normalized, settings.allowed_email_domains):
        return _received()
    if not email_limiter.allow(email_normalized):
        return _received()

    # Eligible attempt: ONE dummy argon2 for timing parity (registration sets no
    # password) — on BOTH the fresh and the duplicate path. Then insert-then-catch
    # (no pre-SELECT): the unique-email constraint makes a duplicate raise
    # IntegrityError, which we treat identically. Race-safe vs concurrent
    # local/Google registration.
    verify_password(None, "timing-parity")
    with session_factory() as session:
        session.add(
            User(email=email_normalized, name=(name.strip()[:_NAME_MAX] or None), status="pending")
        )
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            _logger.info(
                "register: email already exists",
                extra={
                    "event": "register_duplicate",
                    "email_fp": _email_fingerprint(email_normalized),
                },
            )
            return _received()
        session.commit()

    _logger.info(
        "register: request created",
        extra={"event": "register_created", "email_fp": _email_fingerprint(email_normalized)},
    )
    return _received()
