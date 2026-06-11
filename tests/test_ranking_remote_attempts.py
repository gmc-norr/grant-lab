from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from call_manager.db.models import (
    Call,
    Profile,
    ProfileKind,
    RankingRemoteAttempt,
    Source,
    User,
)
from call_manager.ranking.remote_attempts import count_today, record_attempt


def _seed(s):
    u = User(email="r@ki.se")
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
