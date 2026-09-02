from __future__ import annotations

from dataclasses import dataclass

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
        # Do NOT stamp last_login_at here — a matched account may be pending or
        # disabled, in which case the sign-in is refused. The callback is the
        # single owner of last_login_at and only stamps it for active logins.
        return AccountResolution("matched", existing)

    holder = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if holder is not None:
        # An unauthenticated password registration can park a PENDING row on any
        # allowed-domain address with neither a Google identity nor a password
        # hash — a squat that used to block the real person's verified Google
        # sign-in forever (review 2026-09-02 A11 F1). Google's email_verified is
        # exactly the proof that registration never had: attach the identity to
        # that row and keep it pending for the admin. Anything that already
        # carries a credential, or is not pending, stays a collision — never
        # auto-link a real account.
        if holder.status == "pending" and holder.oidc_sub is None and holder.password_hash is None:
            holder.oidc_iss = iss
            holder.oidc_sub = sub
            if name and not holder.name:
                holder.name = name[:200]
            session.flush()
            return AccountResolution("matched", holder)
        return AccountResolution("email_collision", None)

    user = User(
        email=email,
        # name comes from Google's `name` claim (validated <= 4096 chars); the
        # column is varchar(200), so truncate to avoid a DataError on insert.
        name=name[:200] if name else None,
        oidc_iss=iss,
        oidc_sub=sub,
        is_admin=False,
        status="pending",
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        winner = _by_identity(session, iss, sub)
        if winner is not None:
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
