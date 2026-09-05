import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from grant_lab.db.models import Profile, Subscription, User


def _user_and_profile(s, email="u@umu.se", profile_name="G"):
    from grant_lab.db.models import ProfileKind

    u = User(email=email)
    s.add(u)
    s.flush()
    p = Profile(
        name=profile_name,
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="",
        structured={},
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    return u.id, p.id


def test_subscription_create_and_read(pg_session_factory):
    with pg_session_factory() as s:
        uid, pid = _user_and_profile(s)
        s.add(Subscription(user_id=uid, profile_id=pid))
        s.commit()
    with pg_session_factory() as s:
        sub = s.execute(select(Subscription)).scalar_one()
    assert sub.user_id == uid and sub.profile_id == pid
    assert sub.digest_enabled is True


def test_subscription_pk_prevents_duplicate(pg_session_factory):
    with pg_session_factory() as s:
        uid, pid = _user_and_profile(s)
        s.add(Subscription(user_id=uid, profile_id=pid))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.add(Subscription(user_id=uid, profile_id=pid))
        s.commit()


def test_subscription_digest_enabled_toggles(pg_session_factory):
    with pg_session_factory() as s:
        uid, pid = _user_and_profile(s)
        s.add(Subscription(user_id=uid, profile_id=pid, digest_enabled=False))
        s.commit()
    with pg_session_factory() as s:
        sub = s.execute(select(Subscription)).scalar_one()
    assert sub.digest_enabled is False
