import pytest
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import User


def test_new_user_defaults_to_pending(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="a@umu.se")
        s.add(u)
        s.commit()
        s.refresh(u)
        assert u.status == "pending"  # fails CLOSED
        assert u.oidc_sub is None and u.oidc_iss is None


def test_status_check_constraint_rejects_bad_value(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="b@umu.se", status="bogus"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_paired_null_oidc_constraint(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="c@umu.se", oidc_sub="123"))  # iss missing -> violates pairing
        with pytest.raises(IntegrityError):
            s.commit()


def test_oidc_identity_is_unique(pg_session_factory):
    with pg_session_factory() as s:
        s.add(User(email="d@umu.se", oidc_iss="https://accounts.google.com", oidc_sub="sub-1"))
        s.commit()
    with pg_session_factory() as s:
        s.add(User(email="e@umu.se", oidc_iss="https://accounts.google.com", oidc_sub="sub-1"))
        with pytest.raises(IntegrityError):
            s.commit()
