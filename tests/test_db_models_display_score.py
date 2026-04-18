"""Tests for the `CallRanking.display_score` hybrid_property.

display_score = aggregate_score + setwise_offset, clamped to [0, 100].
Used by every render and band-filter path so the user-visible score
matches what the dashboard, detail view, compare view, and digest all
read.

Both forms must agree:
- Python form (instance-level): tested with constructed-but-unflushed
  CallRanking objects so the assertions don't depend on Postgres.
- SQL form (expression): tested by SELECTing display_score from a real
  pg_session_factory query and comparing against the Python value.
  Critical: Postgres GREATEST/LEAST IGNORE NULL inputs, so the SQL form
  uses CASE WHEN aggregate_score IS NULL THEN NULL to preserve NULL
  semantics; the LEFT-JOIN test pins that.
"""
from datetime import UTC, datetime

from sqlalchemy import select

from call_monitor.db.models import (
    Call,
    CallRanking,
    Profile,
    ProfileKind,
    Source,
    User,
)


def _seed_pair(s, *, email="ds@ki.se", scheme="v1"):
    u = User(email=email)
    s.add(u)
    s.flush()
    p = Profile(
        name=f"DS-{email}", kind=ProfileKind.GROUP, description="d",
        structured={}, created_by=u.id, ranking_scheme=scheme,
    )
    s.add(p)
    s.flush()
    src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
    c = Call(
        source_id=src.id, external_id=f"E-{email}", url="https://x", title="T",
        published_at=datetime.now(UTC),
    )
    s.add(c)
    s.flush()
    return c.id, p.id


# --- Python (instance) form ----------------------------------------------


def test_display_score_v1_no_offset_equals_aggregate():
    """v1 rows always have setwise_offset=0 (no setwise pass against v1)
    so display_score must equal aggregate_score on the dot."""
    rk = CallRanking(
        call_id=1, profile_id=1, scheme="v1",
        aggregate_score=72, setwise_offset=0,
        rationale="r", model="m", prompt_version="v1",
    )
    assert rk.display_score == 72


def test_display_score_v2_borderline_positive_offset():
    """v2 borderline call lifted by +5 (rank-1 in setwise group)."""
    rk = CallRanking(
        call_id=1, profile_id=1, scheme="v2",
        aggregate_score=55, setwise_offset=5,
        rationale="r", model="m", prompt_version="v2.0",
    )
    assert rk.display_score == 60


def test_display_score_v2_borderline_negative_offset():
    """v2 borderline call dropped by -5 (rank-4 in setwise group)."""
    rk = CallRanking(
        call_id=1, profile_id=1, scheme="v2",
        aggregate_score=55, setwise_offset=-5,
        rationale="r", model="m", prompt_version="v2.0",
    )
    assert rk.display_score == 50


def test_display_score_clamps_above_100():
    """aggregate=98 + offset=+5 = 103 → clamped to 100."""
    rk = CallRanking(
        call_id=1, profile_id=1, scheme="v2",
        aggregate_score=98, setwise_offset=5,
        rationale="r", model="m", prompt_version="v2.0",
    )
    assert rk.display_score == 100


def test_display_score_clamps_below_zero():
    """aggregate=2 + offset=-5 = -3 → clamped to 0."""
    rk = CallRanking(
        call_id=1, profile_id=1, scheme="v2",
        aggregate_score=2, setwise_offset=-5,
        rationale="r", model="m", prompt_version="v2.0",
    )
    assert rk.display_score == 0


def test_display_score_handles_zero_aggregate():
    """Edge: aggregate=0 (gate-failed in v2) + offset=0 → 0."""
    rk = CallRanking(
        call_id=1, profile_id=1, scheme="v2",
        aggregate_score=0, setwise_offset=0,
        rationale="r", model="m", prompt_version="v2.0",
    )
    assert rk.display_score == 0


def test_display_score_handles_max_aggregate():
    """Edge: aggregate=100 + offset=0 → 100; +5 still clamps to 100."""
    rk_a = CallRanking(
        call_id=1, profile_id=1, scheme="v2",
        aggregate_score=100, setwise_offset=0,
        rationale="r", model="m", prompt_version="v2.0",
    )
    rk_b = CallRanking(
        call_id=2, profile_id=1, scheme="v2",
        aggregate_score=100, setwise_offset=5,
        rationale="r", model="m", prompt_version="v2.0",
    )
    assert rk_a.display_score == 100
    assert rk_b.display_score == 100


# --- SQL (expression) form -----------------------------------------------


def test_display_score_sql_form_round_trips_through_postgres(pg_session_factory):
    """SELECT CallRanking.display_score from Postgres against seeded rows
    and assert each computed value matches the Python form. This pins the
    SQL clamp expression — without the round trip we'd only test the
    Python instance form, which can't catch a Postgres syntax error or
    semantic divergence."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s, email="sql-form@ki.se", scheme="v2")
        # Five rows covering: no-offset, +offset, -offset, upper-clamp, lower-clamp.
        cases = [
            ("v1", 72, 0, 72),
            ("v2", 55, 5, 60),
            ("v2", 55, -5, 50),
            # upper-clamp & lower-clamp need their own rows; reuse the same
            # (call, profile) but a different scheme to dodge the (call,
            # profile, scheme) PK. We've used v1 + v2; for a third we'd
            # need another call. Easier to seed extra calls.
        ]
        # Insert the first three (v1 takes scheme=v1, v2 takes scheme=v2).
        # The third reuses v2 — PK collision. Solve by adding a second call.
        for i, (scheme, agg, off, _exp) in enumerate(cases):
            if i == 0:
                # v1 row against the existing call
                s.add(CallRanking(
                    call_id=cid, profile_id=pid, scheme=scheme,
                    aggregate_score=agg, setwise_offset=off,
                    rationale="r", model="m", prompt_version="v1",
                ))
            elif i == 1:
                # v2 row against the existing call
                s.add(CallRanking(
                    call_id=cid, profile_id=pid, scheme=scheme,
                    aggregate_score=agg, setwise_offset=off,
                    rationale="r", model="m", prompt_version="v2.0",
                ))
            else:
                # Need a fresh call for the remaining v2 rows.
                src = s.execute(
                    select(Source).where(Source.slug == "vinnova")
                ).scalar_one()
                c2 = Call(
                    source_id=src.id, external_id=f"sql-{i}", url="https://x",
                    title="T2", published_at=datetime.now(UTC),
                )
                s.add(c2)
                s.flush()
                s.add(CallRanking(
                    call_id=c2.id, profile_id=pid, scheme=scheme,
                    aggregate_score=agg, setwise_offset=off,
                    rationale="r", model="m", prompt_version="v2.0",
                ))
        # Add the upper-clamp and lower-clamp rows on fresh calls.
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        for tag, agg, off, _exp in [
            ("upper", 98, 5, 100),
            ("lower", 2, -5, 0),
        ]:
            c = Call(
                source_id=src.id, external_id=f"sql-{tag}", url="https://x",
                title=f"T-{tag}", published_at=datetime.now(UTC),
            )
            s.add(c)
            s.flush()
            s.add(CallRanking(
                call_id=c.id, profile_id=pid, scheme="v2",
                aggregate_score=agg, setwise_offset=off,
                rationale="r", model="m", prompt_version="v2.0",
            ))
        s.commit()

    with pg_session_factory() as s:
        rows = s.execute(
            select(
                CallRanking.aggregate_score,
                CallRanking.setwise_offset,
                CallRanking.display_score,
            )
        ).all()
    # Build expected from clamp formula and compare.
    for agg, off, ds in rows:
        expected = max(0, min(100, agg + off))
        assert ds == expected, (
            f"SQL display_score mismatch: agg={agg} off={off} "
            f"expected={expected} got={ds}"
        )


def test_display_score_sql_form_left_join_returns_null(pg_session_factory):
    """Critical regression: GREATEST/LEAST IGNORE NULL in Postgres, so
    GREATEST(0, LEAST(100, NULL)) collapses to 0 instead of NULL — which
    would make every unranked call sort as score=0 instead of NULLS LAST
    in the calls index. The expression must use CASE to preserve NULL
    when aggregate_score IS NULL. This test pins that."""
    with pg_session_factory() as s:
        # Seed a Call with NO matching CallRanking row.
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id, external_id="orphan", url="https://x",
            title="Orphan call", published_at=datetime.now(UTC),
        )
        s.add(c)
        s.commit()

    with pg_session_factory() as s:
        # LEFT JOIN call_rankings → CallRanking columns are all NULL.
        result = s.execute(
            select(Call.title, CallRanking.display_score)
            .outerjoin(CallRanking, CallRanking.call_id == Call.id)
        ).all()
    assert len(result) == 1
    title, display_score = result[0]
    assert title == "Orphan call"
    assert display_score is None, (
        "display_score must be NULL for unranked rows so DESC NULLS LAST "
        "actually sinks them; otherwise GREATEST(0, ...) returns 0 and "
        "they sort as worst-ranked."
    )


def test_display_score_sql_form_in_order_by(pg_session_factory):
    """ORDER BY display_score DESC must put +offset rows ahead of -offset
    rows when bare aggregates are tied. The SQL expression form has to
    work in both SELECT and ORDER BY positions."""
    with pg_session_factory() as s:
        cid, pid = _seed_pair(s, email="orderby@ki.se", scheme="v2")
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        # Two more calls, both at aggregate=55 but different offsets.
        ids = [cid]
        for i in range(2):
            c = Call(
                source_id=src.id, external_id=f"ob-{i}", url="https://x",
                title=f"OB-{i}", published_at=datetime.now(UTC),
            )
            s.add(c)
            s.flush()
            ids.append(c.id)
        # First (cid): aggregate 55, offset 0  → display 55
        s.add(CallRanking(
            call_id=ids[0], profile_id=pid, scheme="v2",
            aggregate_score=55, setwise_offset=0,
            rationale="r", model="m", prompt_version="v2.0",
        ))
        # Second: aggregate 55, offset +5 → display 60
        s.add(CallRanking(
            call_id=ids[1], profile_id=pid, scheme="v2",
            aggregate_score=55, setwise_offset=5,
            rationale="r", model="m", prompt_version="v2.0",
        ))
        # Third: aggregate 55, offset -5 → display 50
        s.add(CallRanking(
            call_id=ids[2], profile_id=pid, scheme="v2",
            aggregate_score=55, setwise_offset=-5,
            rationale="r", model="m", prompt_version="v2.0",
        ))
        s.commit()

    with pg_session_factory() as s:
        ordered = s.execute(
            select(CallRanking.call_id, CallRanking.display_score)
            .where(CallRanking.profile_id == pid)
            .order_by(CallRanking.display_score.desc())
        ).all()
    # +5 row first (60), 0-offset (55), -5 last (50).
    assert [d for _, d in ordered] == [60, 55, 50]
    assert ordered[0][0] == ids[1]   # +5 wins
    assert ordered[2][0] == ids[2]   # -5 loses
