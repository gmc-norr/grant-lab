from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy.orm import sessionmaker

from call_manager.auth.domain import domain_allowed
from call_manager.db.models import User

_logger = logging.getLogger(__name__)


def bootstrap_admins(
    session_factory: sessionmaker,
    emails: Iterable[str],
    allowed_domains: list[str],
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
                session.add(User(email=email, is_admin=True, status="active"))
            else:
                # Configured admins are the lockout-recovery path: ensure both
                # admin AND active on every startup. To permanently disable one,
                # remove them from INITIAL_ADMIN_EMAIL first.
                user.is_admin = True
                user.status = "active"
        session.commit()
