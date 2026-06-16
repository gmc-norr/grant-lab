from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy.orm import sessionmaker

from call_manager.auth.domain import domain_allowed
from call_manager.auth.passwords import hash_password
from call_manager.db.models import User

_logger = logging.getLogger(__name__)


def bootstrap_admins(
    session_factory: sessionmaker,
    emails: Iterable[str],
    allowed_domains: list[str],
    password: str | None = None,
) -> None:
    """Promote or create admin users from INITIAL_ADMIN_EMAIL on startup.

    Entries are silently dropped if empty, malformed, or point to a domain
    outside `allowed_domains` — a WARNING is logged for operator triage.
    This is defense-in-depth: a stale or typo'd env value should not
    materialize a working admin at an attacker-controlled address.
    """
    normalized = [e.strip().lower() for e in emails if e and e.strip()]
    if not normalized:
        return

    valid: list[str] = []
    for email in normalized:
        if not domain_allowed(email, allowed_domains):
            _logger.warning(
                "admin bootstrap entry rejected",
                extra={
                    "event": "admin_bootstrap_skipped",
                    "reason": "domain_not_allowed_or_malformed",
                    "email": email,
                },
            )
            continue
        valid.append(email)

    if not valid:
        return

    with session_factory() as session:
        for email in valid:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                user = User(email=email, is_admin=True, status="active")
                session.add(user)
                if password is None:
                    # A freshly-created admin with no password cannot authenticate
                    # by ANY flow: password login fails (no hash), and Google
                    # sign-in treats the pre-created email row as a collision and
                    # refuses to auto-link. Warn loudly so the operator sets
                    # INITIAL_ADMIN_PASSWORD (or pre-registers the user) rather
                    # than getting a silent, permanent lockout.
                    _logger.warning(
                        "admin bootstrap created an admin with no password",
                        extra={
                            "event": "admin_bootstrap_passwordless",
                            "email": email,
                            "detail": (
                                "no INITIAL_ADMIN_PASSWORD set and OIDC cannot link a "
                                "pre-created email; this admin cannot log in until a "
                                "password is configured or the user is pre-registered"
                            ),
                        },
                    )
            else:
                # Configured admins are the lockout-recovery path: ensure both
                # admin AND active on every startup. To permanently disable one,
                # remove them from INITIAL_ADMIN_EMAIL first.
                user.is_admin = True
                user.status = "active"
            if password is not None:
                # Break-glass: (re)apply the configured password every startup and
                # bump the epoch so any session under an old password is revoked.
                user.password_hash = hash_password(password)
                user.session_epoch = (user.session_epoch or 0) + 1
        session.commit()
