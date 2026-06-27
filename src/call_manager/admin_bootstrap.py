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
    """Provision configured admins (INITIAL_ADMIN_EMAIL) on startup — idempotently.

    This runs on EVERY boot, so it must NOT silently override in-app state on a
    routine redeploy. Two cases:

    * **First-time bootstrap** — a configured email with no `users` row is
      created as an active admin (and given `INITIAL_ADMIN_PASSWORD` if set).
    * **Lockout recovery ONLY** — an *existing* configured user is restored to
      admin+active solely when the system currently has NO active admin (a
      genuine lockout). When at least one active admin already exists, a
      configured user is LEFT UNTOUCHED: a deliberate demote/disable via the
      admin UI is respected across reboots, the configured password is NOT
      re-applied, and no session epoch is bumped. Manage roles via the admin UI
      once the system is set up.

    This replaces the previous every-boot promote + password-reset + epoch-bump,
    which let anyone controlling the deployment env resurrect a demoted admin or
    overwrite a real user's password and revoke their sessions on each redeploy.

    Entries are silently dropped if empty, malformed, or point to a domain
    outside `allowed_domains` — a WARNING is logged for operator triage. This is
    defense-in-depth: a stale or typo'd env value should not materialize a
    working admin at an attacker-controlled address.
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
        # Snapshot the pre-bootstrap active-admin count: lockout recovery (below)
        # only fires when the system has NO active admin, so a deliberate in-app
        # demote/disable survives a redeploy as long as another admin remains.
        active_admin_count = (
            session.query(User)
            .filter(User.is_admin.is_(True), User.status == "active")
            .count()
        )
        for email in valid:
            user = session.query(User).filter_by(email=email).one_or_none()
            if user is None:
                # First-time bootstrap: create the admin (with the configured
                # password if any). A brand-new user has no sessions, so there is
                # nothing to revoke and no epoch to bump.
                user = User(email=email, is_admin=True, status="active")
                if password is not None:
                    user.password_hash = hash_password(password)
                else:
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
                session.add(user)
            elif active_admin_count == 0:
                # Genuine lockout: no active admin exists anywhere. Restore this
                # configured user as the break-glass recovery path. We touch ONLY
                # role/status — never the password or session epoch — so recovery
                # never clobbers a real credential.
                user.is_admin = True
                user.status = "active"
                _logger.warning(
                    "admin bootstrap restored an admin (no active admin existed)",
                    extra={
                        "event": "admin_bootstrap_recovered",
                        "email": email,
                    },
                )
            # else: existing user + a healthy active-admin set → leave untouched
            # (respect in-app demote/disable; do NOT re-apply the env password).
        session.commit()
