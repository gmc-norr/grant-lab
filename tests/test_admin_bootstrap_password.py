from grant_lab.admin_bootstrap import bootstrap_admins
from grant_lab.auth.passwords import hash_password, verify_password
from grant_lab.db.models import User


def test_sets_password_when_provided(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@umu.se"], ["umu.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@umu.se").one()
        assert u.is_admin and u.status == "active"
        ok, _ = verify_password(u.password_hash, "a-strong-enough-pw")
        assert ok is True


def test_second_run_does_not_overwrite_changed_password_or_bump_epoch(pg_session_factory):
    """A routine redeploy must NOT re-apply INITIAL_ADMIN_PASSWORD over a
    password the admin changed in-app, nor bump their epoch (revoking sessions)."""
    bootstrap_admins(pg_session_factory, ["boss@umu.se"], ["umu.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@umu.se").one()
        ep1 = u.session_epoch
        # Simulate the admin changing their own password after setup.
        u.password_hash = hash_password("user-changed-pw-1234")
        s.commit()
    # Second boot with the SAME configured password.
    bootstrap_admins(pg_session_factory, ["boss@umu.se"], ["umu.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@umu.se").one()
        assert u.session_epoch == ep1  # NOT bumped — sessions not revoked
        # The user's changed password still works; the env password does NOT.
        assert verify_password(u.password_hash, "user-changed-pw-1234")[0] is True
        assert verify_password(u.password_hash, "a-strong-enough-pw")[0] is False


def test_does_not_repromote_deliberately_demoted_admin_when_another_admin_exists(
    pg_session_factory,
):
    """An admin demoted via the admin UI stays demoted across reboots as long as
    another active admin remains — env must not silently override in-app role."""
    with pg_session_factory() as s:
        s.add(User(email="root@umu.se", is_admin=True, status="active"))  # remaining admin
        s.add(User(email="boss@umu.se", is_admin=False, status="active"))  # demoted in-app
        s.commit()
    bootstrap_admins(pg_session_factory, ["boss@umu.se"], ["umu.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@umu.se").one()
        assert u.is_admin is False  # NOT re-promoted
        assert u.password_hash is None  # env password NOT applied to existing user


def test_recovers_admin_on_total_lockout(pg_session_factory):
    """When NO active admin exists (genuine lockout), a configured user is
    restored to admin+active — but the password is still left untouched."""
    with pg_session_factory() as s:
        s.add(User(email="boss@umu.se", is_admin=False, status="disabled"))
        s.commit()
    bootstrap_admins(pg_session_factory, ["boss@umu.se"], ["umu.se"], password="a-strong-enough-pw")
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@umu.se").one()
        assert u.is_admin is True and u.status == "active"  # recovered
        assert u.password_hash is None  # recovery never sets credentials


def test_no_password_leaves_hash_untouched(pg_session_factory):
    bootstrap_admins(pg_session_factory, ["boss@umu.se"], ["umu.se"], password=None)
    with pg_session_factory() as s:
        u = s.query(User).filter_by(email="boss@umu.se").one()
        assert u.is_admin and u.status == "active" and u.password_hash is None
