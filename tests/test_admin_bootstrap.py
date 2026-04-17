from call_monitor.admin_bootstrap import bootstrap_admins
from call_monitor.db.models import User


def test_creates_users_with_admin_flag(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="root@ki.se").one()
    assert u.is_admin is True


def test_promotes_existing_user(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="alice@ki.se", is_admin=False))
        s.commit()
    bootstrap_admins(pg_session_factory, emails=["alice@ki.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="alice@ki.se").one()
    assert u.is_admin is True


def test_is_idempotent(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"])
    bootstrap_admins(pg_session_factory, emails=["root@ki.se"])
    with pg_session_factory() as s:
        assert s.query(User).filter_by(email="root@ki.se").count() == 1


def test_no_admins_in_env_is_noop(pg_session_factory):
    bootstrap_admins(pg_session_factory, emails=[])
    with pg_session_factory() as s:
        assert s.query(User).count() == 0
