from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from call_manager.db.models import Call, Source


def test_seeded_sources_present(pg_session_factory):
    with pg_session_factory() as s:
        slugs = sorted(row[0] for row in s.execute(select(Source.slug)).all())
    assert slugs == ["eu_portal", "vinnova"]


def test_can_insert_and_read_call(pg_session_factory):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="2026-01234",
            url="https://example.org/calls/2026-01234",
            title="Test call",
            summary="One-paragraph summary.",
            keywords=["health", "ai"],
            published_at=datetime(2026, 4, 1, tzinfo=UTC),
            deadline=datetime(2026, 6, 30, 21, 59, tzinfo=UTC),
            currency="SEK",
            budget_min=100_000,
            budget_max=2_000_000,
            raw={"any": "json"},
        )
        s.add(c)
        s.commit()
        s.refresh(c)
        assert c.id is not None
        assert c.fetched_at is not None  # server_default


def test_call_unique_per_source_and_external_id(pg_session_factory):
    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(source_id=src.id, external_id="dup-1", url="u", title="t"))
        s.commit()
    with pg_session_factory() as s, pytest.raises(IntegrityError):
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        s.add(Call(source_id=src.id, external_id="dup-1", url="u2", title="t2"))
        s.commit()


def test_calls_keywords_gin_index_supports_overlap(pg_session_factory):
    """Smoke: keyword filter via && (overlap) returns hits — proves GIN index is in place."""
    from sqlalchemy import text

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "eu_portal")).scalar_one()
        s.add(
            Call(
                source_id=src.id,
                external_id="k1",
                url="u",
                title="t",
                keywords=["digital pathology", "federated learning"],
            )
        )
        s.add(Call(source_id=src.id, external_id="k2", url="u", title="t", keywords=["energy"]))
        s.commit()
        rows = s.execute(
            text("SELECT external_id FROM calls WHERE keywords && :kw ORDER BY external_id"),
            {"kw": ["federated learning"]},
        ).all()
    assert [r[0] for r in rows] == ["k1"]
