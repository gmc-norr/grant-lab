from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_monitor.db.models import (
    Call,
    DigestRun,
    Source,
    User,
    UserCallDismissal,
)


def _user_and_call(s, email="u@ki.se"):
    u = User(email=email)
    s.add(u)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{email}", url="https://x", title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return u.id, c.id


def test_dismissal_create_and_pk(pg_session_factory):
    with pg_session_factory() as s:
        uid, cid = _user_and_call(s)
        s.add(UserCallDismissal(user_id=uid, call_id=cid))
        s.commit()
    with pg_session_factory() as s:
        d = s.execute(select(UserCallDismissal)).scalar_one()
    assert d.user_id == uid and d.call_id == cid
    assert d.dismissed_at is not None


def test_dismissal_pk_prevents_duplicate(pg_session_factory):
    with pg_session_factory() as s:
        uid, cid = _user_and_call(s)
        s.add(UserCallDismissal(user_id=uid, call_id=cid))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.add(UserCallDismissal(user_id=uid, call_id=cid))
        s.commit()


def test_digest_run_stores_included_call_ids(pg_session_factory):
    with pg_session_factory() as s:
        uid, cid = _user_and_call(s)
        s.add(DigestRun(user_id=uid, included_call_ids=[cid, 999]))
        s.commit()
    with pg_session_factory() as s:
        run = s.execute(select(DigestRun)).scalar_one()
    assert run.user_id == uid
    assert run.included_call_ids == [cid, 999]


def test_user_bounce_columns_default_zero_and_null(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="bounce@ki.se")
        s.add(u)
        s.commit()
        s.refresh(u)
    assert u.digest_bounce_count == 0
    assert u.digest_disabled_at is None
