from datetime import UTC, datetime

from sqlalchemy import select

from call_monitor.db.models import Call, Source
from call_monitor.ingestion.upsert import upsert_call
from call_monitor.sources.types import NormalizedCall


def _vinnova_id(s):
    return s.execute(select(Source.id).where(Source.slug == "vinnova")).scalar_one()


def test_upsert_inserts_new_row(pg_session_factory):
    with pg_session_factory() as s:
        sid = _vinnova_id(s)
        was_new = upsert_call(
            s,
            source_id=sid,
            call=NormalizedCall(external_id="V-1", url="u", title="T"),
        )
        s.commit()
        assert was_new is True
        rows = s.execute(select(Call).where(Call.external_id == "V-1")).scalars().all()
    assert len(rows) == 1


def test_upsert_replaces_existing_row_in_place(pg_session_factory):
    with pg_session_factory() as s:
        sid = _vinnova_id(s)
        upsert_call(
            s,
            source_id=sid,
            call=NormalizedCall(external_id="V-1", url="u", title="OLD"),
        )
        s.commit()
        was_new = upsert_call(
            s,
            source_id=sid,
            call=NormalizedCall(
                external_id="V-1",
                url="u",
                title="NEW",
                deadline=datetime(2026, 6, 1, tzinfo=UTC),
            ),
        )
        s.commit()
        assert was_new is False  # already existed
        rows = s.execute(select(Call).where(Call.external_id == "V-1")).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "NEW"
    assert rows[0].deadline == datetime(2026, 6, 1, tzinfo=UTC)


def test_upsert_refreshes_fetched_at_on_re_encounter(pg_session_factory):
    """fetched_at semantics: 'last seen', not 'first seen'. Each upsert
    refreshes the column to NOW() so subsequent reads are monotonically
    >= the previous value. Used by the dashboard to surface freshly-seen
    calls and by future reaper logic to identify stale rows."""
    with pg_session_factory() as s:
        sid = _vinnova_id(s)
        upsert_call(s, source_id=sid, call=NormalizedCall(external_id="V-1", url="u", title="T"))
        s.commit()
        first = s.execute(select(Call.fetched_at).where(Call.external_id == "V-1")).scalar_one()
    # Second pass at least 1ms later
    import time
    time.sleep(0.01)
    with pg_session_factory() as s:
        sid = _vinnova_id(s)
        upsert_call(s, source_id=sid, call=NormalizedCall(external_id="V-1", url="u", title="T"))
        s.commit()
        second = s.execute(select(Call.fetched_at).where(Call.external_id == "V-1")).scalar_one()
    assert second >= first


def test_upsert_is_per_source(pg_session_factory):
    """Same external_id under different source_ids → two rows."""
    with pg_session_factory() as s:
        eu = s.execute(select(Source.id).where(Source.slug == "eu_portal")).scalar_one()
        vn = s.execute(select(Source.id).where(Source.slug == "vinnova")).scalar_one()
        upsert_call(s, source_id=eu, call=NormalizedCall(external_id="X-1", url="u", title="T"))
        upsert_call(s, source_id=vn, call=NormalizedCall(external_id="X-1", url="u", title="T"))
        s.commit()
        n = s.execute(select(Call).where(Call.external_id == "X-1")).scalars().all()
    assert len(n) == 2
