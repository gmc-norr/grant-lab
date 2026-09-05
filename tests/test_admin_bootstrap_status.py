# tests/test_admin_bootstrap_status.py
from grant_lab.admin_bootstrap import bootstrap_admins
from grant_lab.db.models import User


def test_creates_active_admin(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@umu.se"], ["umu.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@umu.se").one()
        assert u.is_admin is True and u.status == "active"


def test_reactivates_disabled_configured_admin(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="boss@umu.se", is_admin=False, status="disabled"))
        s.commit()
    bootstrap_admins(pg_session_factory, ["boss@umu.se"], ["umu.se"])
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@umu.se").one()
        assert u.is_admin is True and u.status == "active"
