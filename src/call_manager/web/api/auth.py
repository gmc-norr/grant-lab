from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class AuthConfigOut(BaseModel):
    google_oidc_enabled: bool
    allowed_domains: list[str]


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
