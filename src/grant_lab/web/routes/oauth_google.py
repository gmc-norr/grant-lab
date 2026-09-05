# src/grant_lab/web/routes/oauth_google.py
from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from grant_lab.auth.accounts import link_google_identity, resolve_google_account
from grant_lab.auth.domain import domain_allowed
from grant_lab.auth.oauth_txn import InvalidTransaction
from grant_lab.auth.oidc import IdTokenInvalid, OIDCError, generate_pkce, generate_token
from grant_lab.auth.redirects import safe_next
from grant_lab.db.models import User
from grant_lab.web.client_ip import client_ip

_logger = logging.getLogger(__name__)
router = APIRouter()

_TXN_COOKIE = "oauth_txn"
_TXN_MAX_AGE = 600  # 10 minutes


def _require_enabled(request: Request):
    client = request.app.state.google_oidc_client
    if client is None:
        raise HTTPException(status_code=404, detail="not found")
    return client


def _clean(target: str) -> RedirectResponse:
    """Every callback exit goes through here: delete txn cookie, suppress
    Referer (the callback URL carries ?code=), redirect to a clean URL."""
    resp = RedirectResponse(url=target, status_code=303)
    resp.delete_cookie(_TXN_COOKIE, path="/auth/google")
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.post("/auth/google/start")
def google_start(
    request: Request,
    next: str | None = Form(default=None),
    link: str | None = Form(default=None),
):
    client = _require_enabled(request)
    settings = request.app.state.settings
    cipher = request.app.state.oauth_txn_cipher
    if not request.app.state.ip_limiter.allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")

    verifier, challenge = generate_pkce()
    state, nonce = generate_token(), generate_token()
    payload = {"state": state, "nonce": nonce, "code_verifier": verifier, "next": safe_next(next)}

    if link == "1":
        # Connect-Google: require an authenticated ACTIVE session; fail closed.
        from grant_lab.auth.dependencies import AccountInactive, get_current_user

        try:
            user = get_current_user(request, session=request.cookies.get("session"))
        except (HTTPException, AccountInactive):
            return RedirectResponse(url="/login", status_code=303)
        payload["link_uid"] = user.id
        payload["link_epoch"] = user.session_epoch

    url = client.build_authorization_url(
        redirect_uri=settings.google_redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=challenge,
    )
    resp = RedirectResponse(url=url, status_code=303)
    resp.set_cookie(
        _TXN_COOKIE,
        cipher.make(payload),
        max_age=_TXN_MAX_AGE,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/auth/google",
    )
    return resp


@router.get("/auth/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    client = _require_enabled(request)
    settings = request.app.state.settings
    cipher = request.app.state.oauth_txn_cipher
    session_factory = request.app.state.session_factory
    session_signer = request.app.state.session_signer

    # No rate-limit on the callback: it is a public, side-effect-free GET until
    # the encrypted txn cookie + state match, which already gate the only
    # expensive work (the Google token exchange). Consuming the shared login
    # ip_limiter here would let anyone hammering this URL (or a mail-scanner
    # prefetching the redirect) exhaust an IP's budget and lock real users out of
    # /login. Rate-limiting stays on /auth/google/start, the user-initiated leg.
    if error or not code or not state:
        return _clean("/auth/error")

    raw = request.cookies.get(_TXN_COOKIE)
    if not raw:
        return _clean("/auth/error")
    try:
        txn = cipher.read(raw, max_age_seconds=_TXN_MAX_AGE)
    except InvalidTransaction:
        return _clean("/auth/error")
    if state != txn.get("state"):
        return _clean("/auth/error")

    try:
        token = client.exchange_code(
            code=code,
            code_verifier=txn["code_verifier"],
            redirect_uri=settings.google_redirect_uri,
        )
        claims = client.validate_id_token(token["id_token"], expected_nonce=txn["nonce"])
    except (OIDCError, IdTokenInvalid, KeyError, TypeError):
        return _clean("/auth/error")

    iss, sub = claims.get("iss"), claims.get("sub")
    email, name = claims.get("email"), claims.get("name")
    if not iss or not sub or not email:
        return _clean("/auth/error")

    # Connect-Google (link mode): revalidate the initiating session, fail closed.
    link_uid = txn.get("link_uid")
    if link_uid is not None:
        # Enforce domain allowlist on the Google identity being linked. Without
        # this check an active umu.se user could link a personal@gmail.com account,
        # which would let them bypass the domain gate on future sign-ins.
        google_email = email.strip().lower()
        if not domain_allowed(google_email, settings.allowed_email_domains):
            return _clean("/auth/error")
        with session_factory() as db:
            u = db.get(User, int(link_uid))
            if u is None or u.status != "active" or u.session_epoch != txn.get("link_epoch"):
                return _clean("/auth/error")
            ok = link_google_identity(db, user_id=u.id, iss=iss, sub=sub)
            db.commit()
        return _clean("/me" if ok else "/auth/error")

    # Enforce ALLOWED_EMAIL_DOMAINS on Google SIGN-IN, same as password
    # registration and admin bootstrap. Without this, a Google account at ANY
    # domain could create a pending account (and an admin could later approve it
    # by mistake). Link mode above is exempt — it attaches an identity to an
    # account that was already admitted through the allow-list.
    if not domain_allowed(email.strip().lower(), settings.allowed_email_domains):
        return _clean("/auth/error")

    with session_factory() as db:
        res = resolve_google_account(db, iss=iss, sub=sub, email=email, name=name)
        if res.outcome == "email_collision":
            db.rollback()
            return _clean("/auth/error")
        user_id, status, session_epoch = res.user.id, res.user.status, res.user.session_epoch
        if status == "active":
            res.user.last_login_at = datetime.now(UTC)
        db.commit()

    if status == "pending":
        return _clean("/auth/pending")
    if status == "disabled":
        return _clean("/auth/no-access")

    target = safe_next(txn.get("next")) or "/"
    resp = _clean(target)
    resp.set_cookie(
        "session",
        session_signer.make(user_id=user_id, session_epoch=session_epoch),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    _logger.info("google sign-in success", extra={"event": "google_signin_success"})
    return resp

