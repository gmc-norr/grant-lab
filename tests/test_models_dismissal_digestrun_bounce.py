from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import (
    Call,
    CallDecision,
    DigestRun,
    Profile,
    ProfileKind,
    Source,
    User,
)


def _user_and_call(s, email="u@umu.se"):
    u = User(email=email)
    s.add(u)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id,
        external_id=f"E-{email}",
        url="https://x",
        title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return u.id, c.id


def _profile(s, uid, name="P"):
    p = Profile(
        name=name,
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="d",
        structured={},
        created_by=uid,
    )
    s.add(p)
    s.flush()
    return p.id


# The ORM half of the decision contract. The migration test pins the DDL; this
# pins that the MAPPED class carries the same key, which is what application
# code actually writes through.
def test_decision_create_and_defaults(pg_session_factory):
    with pg_session_factory() as s:
        uid, cid = _user_and_call(s)
        pid = _profile(s, uid)
        s.add(CallDecision(user_id=uid, profile_id=pid, call_id=cid, state="hidden"))
        s.commit()
    with pg_session_factory() as s:
        d = s.execute(select(CallDecision)).scalar_one()
    assert (d.user_id, d.profile_id, d.call_id) == (uid, pid, cid)
    assert d.state == "hidden"
    assert d.decided_at is not None
    assert d.note is None and d.decided_by is None


def test_decision_unique_key_includes_the_profile(pg_session_factory):
    """(user, profile, call) is unique - but the SAME call on a DIFFERENT profile
    is a different decision. A (user, call) key would reject the second insert."""
    with pg_session_factory() as s:
        uid, cid = _user_and_call(s)
        p1 = _profile(s, uid, "P1")
        p2 = _profile(s, uid, "P2")
        s.add(CallDecision(user_id=uid, profile_id=p1, call_id=cid, state="hidden"))
        s.add(CallDecision(user_id=uid, profile_id=p2, call_id=cid, state="saved"))
        s.commit()
        assert len(s.execute(select(CallDecision)).scalars().all()) == 2
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        s.add(CallDecision(user_id=uid, profile_id=p1, call_id=cid, state="saved"))
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


def test_digest_run_week_start_generated_from_sent_at(pg_session_factory):
    """The DB populates week_start = date_trunc('week', sent_at,
    'Europe/Stockholm') — matching digest.send.week_start() so the app's
    skip check and the DB's unique constraint agree on week boundaries.
    The app never sets it directly — verify it appears on read."""
    from zoneinfo import ZoneInfo

    with pg_session_factory() as s:
        uid, _cid = _user_and_call(s)
        # Wednesday 2026-04-22 14:30 UTC. Monday 00:00 Europe/Stockholm of
        # that week is 2026-04-20 00:00 +02:00 == 2026-04-19 22:00 UTC.
        sent = datetime(2026, 4, 22, 14, 30, tzinfo=UTC)
        s.add(DigestRun(user_id=uid, included_call_ids=[], sent_at=sent))
        s.commit()
    with pg_session_factory() as s:
        run = s.execute(select(DigestRun)).scalar_one()
    assert run.week_start is not None
    # Compare as an instant: Stockholm-Monday-midnight for this week.
    expected = datetime(2026, 4, 20, tzinfo=ZoneInfo("Europe/Stockholm"))
    assert run.week_start == expected


def test_digest_run_user_week_uniqueness(pg_session_factory):
    """One DigestRun per (user, UTC week). A second insert for the same
    user in the same week must be rejected by the DB — this is the
    guard that protects against a silent double-send if the
    single-instance scheduler invariant ever breaks."""
    with pg_session_factory() as s:
        uid, _cid = _user_and_call(s)
        monday = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)
        s.add(DigestRun(user_id=uid, sent_at=monday, included_call_ids=[]))
        s.commit()

    with pg_session_factory() as s, pytest.raises(IntegrityError):
        # Different hour, same week — still rejected.
        tuesday_same_week = datetime(2026, 4, 21, 8, 0, tzinfo=UTC)
        s.add(DigestRun(user_id=uid, sent_at=tuesday_same_week, included_call_ids=[]))
        s.commit()


def test_user_bounce_columns_default_zero_and_null(pg_session_factory):
    with pg_session_factory() as s:
        u = User(email="bounce@umu.se")
        s.add(u)
        s.commit()
        s.refresh(u)
    assert u.digest_bounce_count == 0
    assert u.digest_disabled_at is None
