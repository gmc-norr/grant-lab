from call_manager.admin_bootstrap import bootstrap_admins
from call_manager.auth.passwords import verify_password
from call_manager.db.models import User


def test_sets_password_when_provided(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.is_admin and u.status == "active"
        ok, _ = verify_password(u.password_hash, "a-strong-enough-pw")
        assert ok is True


def test_reapplies_and_bumps_epoch_on_second_run(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        ep1 = s.query(User).filter_by(email="boss@ki.se").one().session_epoch
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.session_epoch == ep1 + 1
        ok, _ = verify_password(u.password_hash, "a-strong-enough-pw")
        assert ok is True


def test_no_password_leaves_hash_untouched(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@ki.se"], ["ki.se"], password=None)
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@ki.se").one()
        assert u.is_admin and u.status == "active" and u.password_hash is None
