from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from call_manager.db.models import User


@dataclass(frozen=True)
class AccountResolution:
    outcome: str  # "matched" | "created" | "email_collision"
    user: User | None


def _by_identity(session: Session, iss: str, sub: str) -> User | None:
    return session.execute(
        select(User).where(User.oidc_iss == iss, User.oidc_sub == sub)
    ).scalar_one_or_none()


def resolve_google_account(
    session: Session, *, iss: str, sub: str, email: str, name: str | None
) -> AccountResolution:
    """1. match (iss,sub); 2. email exists -> collision (never auto-link);
    3. else create a PENDING account. Race-safe via unique-index re-resolution."""
    email = email.strip().lower()

    existing = _by_identity(session, iss, sub)
    if existing is not None:
        existing.last_login_at = datetime.now(UTC)
        return AccountResolution("matched", existing)

    if session.execute(select(User).where(User.email == email)).scalar_one_or_none():
        return AccountResolution("email_collision", None)

    user = User(
        email=email, name=name, oidc_iss=iss, oidc_sub=sub, is_admin=False, status="pending"
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        winner = _by_identity(session, iss, sub)
        if winner is not None:
            winner.last_login_at = datetime.now(UTC)
            return AccountResolution("matched", winner)
        return AccountResolution("email_collision", None)
    return AccountResolution("created", user)


def link_google_identity(session: Session, *, user_id: int, iss: str, sub: str) -> bool:
    """Attach a Google identity to an authenticated user.
    False if that identity already belongs to another account."""
    owner = _by_identity(session, iss, sub)
    if owner is not None:
        return owner.id == user_id
    user = session.get(User, user_id)
    if user is None:
        return False
    user.oidc_iss = iss
    user.oidc_sub = sub
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return False
    return True
