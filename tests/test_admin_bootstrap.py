import logging

from call_manager.admin_bootstrap import bootstrap_admins
from call_manager.db.models import User


def test_creates_users_with_admin_flag(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"], allowed_domains=["ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="root@ki.se").one()
    assert u.is_admin is True


def test_promotes_existing_user(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="alice@ki.se", is_admin=False))
        s.commit()
    bootstrap_admins(pg_session_factory, emails=["alice@ki.se"], allowed_domains=["ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="alice@ki.se").one()
    assert u.is_admin is True


def test_is_idempotent(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"], allowed_domains=["ki.se"])
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"], allowed_domains=["ki.se"])
    with pg_session_factory() as s:
        assert s.query(User).filter_by(email="root@ki.se").count() == 1


def test_no_admins_in_env_is_noop(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=[], allowed_domains=["ki.se"])
    with pg_session_factory() as s:
        assert s.query(User).count() == 0


def test_skips_email_outside_allowlist(pg_session_factory, caplog):
    """A typo or stale INITIAL_ADMIN_EMAIL pointing to a domain the app
    doesn't trust must not create an admin account. Silent creation is the
    exact failure mode the domain allowlist is there to prevent."""
    with caplog.at_level(logging.WARNING, logger="call_manager.admin_bootstrap"):
        bootstrap_admins(
            pg_session_factory,
            emails=["intruder@example.com"],
            allowed_domains=["ki.se"],
        )
    with pg_session_factory() as s:
        assert s.query(User).filter_by(email="intruder@example.com").count() == 0
    assert any(
        "admin_bootstrap_skipped" in (getattr(r, "event", "") or r.getMessage())
        for r in caplog.records
    )


def test_skips_malformed_email(pg_session_factory, caplog):
    """An INITIAL_ADMIN_EMAIL without an @ must not persist a broken
    users row that no one can log in to."""
    with caplog.at_level(logging.WARNING, logger="call_manager.admin_bootstrap"):
        bootstrap_admins(
            pg_session_factory,
            emails=["not-an-email"],
            allowed_domains=["ki.se"],
        )
    with pg_session_factory() as s:
        assert s.query(User).count() == 0


def test_mixed_valid_and_invalid_emails(pg_session_factory):
    """Valid entries still bootstrap even when invalid ones are skipped."""
    bootstrap_admins(
        pg_session_factory,
        emails=["root@ki.se", "intruder@example.com", "bad"],
        allowed_domains=["ki.se"],
    )
    with pg_session_factory() as s:
        emails = {u.email for u in s.query(User).all()}
    assert emails == {"root@ki.se"}
