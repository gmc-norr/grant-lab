from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from grant_lab.db.models import (
    Call,
    Profile,
    ProfileKind,
    RankingRemoteAttempt,
    Source,
    User,
)
from grant_lab.ranking.remote_attempts import count_today, record_attempt
from grant_lab.timeutil import STOCKHOLM


def _seed(s):
    u = User(email="r@umu.se")
    s.add(u)
    s.flush()
    p = Profile(
        name="P",
        kind=ProfileKind.ORGANIZATIONAL_UNIT,
        description="d",
        structured={},
        created_by=u.id,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id,
        external_id="e",
        url="u",
        title="t",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id, p.id


def test_record_and_count_today(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        record_attempt(
            s,
            call_id=cid,
            profile_id=pid,
            model="claude-sonnet-4-6",
            outcome="success",
        )
        record_attempt(
            s,
            call_id=cid,
            profile_id=pid,
            model="claude-sonnet-4-6",
            outcome="llm_error",
        )
        s.commit()
        assert count_today(s) == 2


def test_count_today_ignores_yesterday(pg_session_factory):
    with pg_session_factory() as s:
        cid, pid = _seed(s)
        record_attempt(s, call_id=cid, profile_id=pid, model="m", outcome="success")
        s.commit()
        # Backdate the row two days.
        row = s.execute(select(RankingRemoteAttempt)).scalar_one()
        row.created_at = datetime.now(UTC) - timedelta(days=2)
        s.commit()
        assert count_today(s) == 0


@pytest.mark.parametrize(
    ("day", "label"),
    [(date(2026, 1, 15), "CET (UTC+1)"), (date(2026, 7, 1), "CEST (UTC+2)")],
)
def test_count_today_rolls_over_at_stockholm_midnight_not_utc_midnight(
    pg_session_factory, day, label
):
    """RANKING_REMOTE_DAILY_CAP rolls over at STOCKHOLM midnight.

    `count_today` reused `stockholm_day_start_utc` but called it with no
    argument, so nothing could falsify WHICH midnight it meant: mutating this
    function to plain UTC midnight left the whole ranking/spend-boundary
    selection green (review 2026-10-26). Stockholm is UTC+1/+2, so local
    midnight is 23:00 or 22:00 UTC the PREVIOUS day. A UTC boundary therefore
    keeps counting the tail of yesterday's escalations for the first 1-2 hours
    of the new day, and the cap opens 1-2 hours late — Sonnet calls the operator
    paid for yesterday suppressing today's.

    The two rows sit one second either side of the boundary; only the one on
    D+1 may count when asked on D+1. `now` is a parameter for the same reason
    `stockholm_day_start_utc` takes one: a function that reads the clock itself
    cannot be falsified by a test."""
    last_second_of_d = datetime.combine(day, time(23, 59, 59), tzinfo=STOCKHOLM)
    first_second_of_d1 = last_second_of_d + timedelta(seconds=2)
    assert first_second_of_d1.astimezone(STOCKHOLM).time() == time(0, 0, 1), label

    with pg_session_factory() as s:
        cid, pid = _seed(s)
        record_attempt(s, call_id=cid, profile_id=pid, model="m", outcome="success")
        record_attempt(s, call_id=cid, profile_id=pid, model="m", outcome="success")
        s.commit()
        older, newer = (
            s.execute(select(RankingRemoteAttempt).order_by(RankingRemoteAttempt.id))
            .scalars()
            .all()
        )
        older.created_at = last_second_of_d
        newer.created_at = first_second_of_d1
        s.commit()

        # Asked on D+1, 30 minutes in: yesterday's 23:59:59 row must be gone and
        # the 00:00:01 row must be there. A UTC-midnight boundary sees BOTH.
        asked_on_d1 = datetime.combine(day + timedelta(days=1), time(0, 30), tzinfo=STOCKHOLM)
        assert count_today(s, now=asked_on_d1) == 1, label

        # Non-vacuity: both rows exist and both are inside day D's window
        # (`count_today` has no upper bound), so the 1 above is the lower edge
        # moving, not a row that failed to insert.
        asked_on_d = datetime.combine(day, time(0, 30), tzinfo=STOCKHOLM)
        assert count_today(s, now=asked_on_d) == 2, label
