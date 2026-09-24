"""Migration 0081: ranking-freshness columns (spec D1) and the timeline-row delete (G16)."""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from grant_lab.db.models.ranking import CallRanking, RankingJob, RankingJobStatus


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_the_new_columns_exist(migrated_engine):
    insp = inspect(migrated_engine)
    cr = {c["name"] for c in insp.get_columns("call_rankings")}
    rj = {c["name"] for c in insp.get_columns("ranking_jobs")}
    assert {
        "call_fingerprint",
        "scoring_fingerprint",
        "escalation_state",
        "escalation_failures",
    } <= cr
    assert {"profile_fingerprint", "call_fingerprint", "scoring_fingerprint", "failure_kind"} <= rj


def test_escalation_state_is_checked_live_and_in_the_orm(migrated_engine):
    live = {c["name"] for c in inspect(migrated_engine).get_check_constraints("call_rankings")}
    assert "ck_call_rankings_escalation_state" in live
    orm = {c.name for c in CallRanking.__table__.constraints}
    assert "ck_call_rankings_escalation_state" in orm


def test_escalation_state_accepts_pending_and_rejects_an_unknown_value(pg_session_factory):
    """The CHECK is real, not just named. Seeded on purpose: an INSERT ... SELECT
    over empty tables writes no row and raises nothing, which passes vacuously
    (it did, in the dry run of this plan)."""
    from tests.freshness_helpers import make_call, make_profile

    with pg_session_factory() as s:
        p, c = make_profile(s), make_call(s)
        cr = CallRanking(
            call_id=c.id,
            profile_id=p.id,
            aggregate_score=0,
            rationale="",
            model="m",
            prompt_version="v",
            raw_response={},
            escalation_state="pending",
        )
        s.add(cr)
        s.flush()  # a known state is accepted
        cr.escalation_state = "bogus"
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_failure_kind_is_nullable_and_checked_live_and_in_the_orm(migrated_engine):
    """Spec D7 amendment (final review I1)."""
    insp = inspect(migrated_engine)
    col = {c["name"]: c for c in insp.get_columns("ranking_jobs")}["failure_kind"]
    assert col["nullable"] is True
    live = {c["name"] for c in insp.get_check_constraints("ranking_jobs")}
    assert "ck_ranking_jobs_failure_kind" in live
    orm = {c.name for c in RankingJob.__table__.constraints}
    assert "ck_ranking_jobs_failure_kind" in orm


def test_failure_kind_accepts_the_known_kinds_and_rejects_an_unknown_value(pg_session_factory):
    """Seeded on purpose, like the escalation_state test above: an INSERT ...
    SELECT over empty tables would raise nothing and pass vacuously."""
    from tests.freshness_helpers import make_call, make_profile

    with pg_session_factory() as s:
        p, c = make_profile(s), make_call(s)
        job = RankingJob(call_id=c.id, profile_id=p.id, status=RankingJobStatus.FAILED)
        s.add(job)
        for kind in ("parse", "service", "error", "crash", None):
            job.failure_kind = kind
            s.flush()  # each known kind (and NULL) is accepted
        job.failure_kind = "timeout"
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_0081_adds_failure_kind_null_on_old_rows_and_downgrade_drops_it(migrated_engine, pg_url):
    """A ranking_jobs row written at 0080 reads failure_kind NULL after the
    upgrade (the pre-0081 shape enqueue_stale_pairs keeps pair-wide), and the
    downgrade to 0080 drops the column."""
    engine = create_engine(pg_url)
    try:
        command.downgrade(_cfg(pg_url), "0080")
        with engine.begin() as conn:
            assert "failure_kind" not in {
                c["name"] for c in inspect(conn).get_columns("ranking_jobs")
            }
            src = conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S0081K','s0081k') RETURNING id")
            ).scalar_one()
            uid = conn.execute(
                text(
                    "INSERT INTO users (email, status) "
                    "VALUES ('m0081k@umu.se','active') RETURNING id"
                )
            ).scalar_one()
            pid = conn.execute(
                text(
                    "INSERT INTO profiles (name, kind, description, structured, created_by) "
                    "VALUES ('M0081K','organizational_unit','d','{}'::jsonb, :u) RETURNING id"
                ),
                {"u": uid},
            ).scalar_one()
            cid = conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, title, url) "
                    "VALUES (:s,'M0081K','t','https://example.org/m0081k') RETURNING id"
                ),
                {"s": src},
            ).scalar_one()
            job_id = conn.execute(
                text(
                    "INSERT INTO ranking_jobs (call_id, profile_id, status, attempts, last_error) "
                    "VALUES (:c,:p,'failed',5,'dead-lettered') RETURNING id"
                ),
                {"c": cid, "p": pid},
            ).scalar_one()

        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            kind = conn.execute(
                text("SELECT failure_kind FROM ranking_jobs WHERE id=:j"), {"j": job_id}
            ).scalar_one()
        assert kind is None
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
            with engine.begin() as conn:
                # ranking_jobs rows go with their call and profile (ON DELETE CASCADE).
                conn.execute(text("DELETE FROM calls WHERE external_id='M0081K'"))
                conn.execute(text("DELETE FROM profiles WHERE name='M0081K'"))
                conn.execute(text("DELETE FROM users WHERE email='m0081k@umu.se'"))
                conn.execute(text("DELETE FROM sources WHERE slug='s0081k'"))
        finally:
            engine.dispose()


def test_0081_deletes_timeline_rows_only_and_leaves_stamps_null(migrated_engine, pg_url):
    """G16. Seed AT 0080, upgrade, check. `migrated_engine` is requested so head
    has run before this test's own downgrade (see the 0075 test's docstring)."""
    engine = create_engine(pg_url)
    try:
        command.downgrade(_cfg(pg_url), "0080")
        with engine.begin() as conn:
            assert "call_fingerprint" not in {
                c["name"] for c in inspect(conn).get_columns("call_rankings")
            }
            src = conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S0081','s0081') RETURNING id")
            ).scalar_one()
            uid = conn.execute(
                text(
                    "INSERT INTO users (email, status) "
                    "VALUES ('m0081@umu.se','active') RETURNING id"
                )
            ).scalar_one()
            pid = conn.execute(
                text(
                    "INSERT INTO profiles (name, kind, description, structured, created_by) "
                    "VALUES ('M0081','organizational_unit','d','{}'::jsonb, :u) RETURNING id"
                ),
                {"u": uid},
            ).scalar_one()
            cid = conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, title, url) "
                    "VALUES (:s,'M0081','t','https://example.org/m0081') RETURNING id"
                ),
                {"s": src},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO call_rankings (call_id, profile_id, aggregate_score, model, "
                    "prompt_version, raw_response) VALUES (:c,:p,40,'gemma','v5.0','{}'::jsonb)"
                ),
                {"c": cid, "p": pid},
            )
            for crit in ("timeline", "budget"):
                conn.execute(
                    text(
                        "INSERT INTO call_ranking_criteria "
                        "(call_id, profile_id, criterion, rating) "
                        "VALUES (:c,:p,:k,'pass')"
                    ),
                    {"c": cid, "p": pid, "k": crit},
                )

        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            crits = set(
                conn.execute(
                    text(
                        "SELECT criterion FROM call_ranking_criteria "
                        "WHERE call_id=:c AND profile_id=:p"
                    ),
                    {"c": cid, "p": pid},
                ).scalars()
            )
            row = conn.execute(
                text(
                    "SELECT call_fingerprint, scoring_fingerprint, escalation_state, "
                    "escalation_failures FROM call_rankings WHERE call_id=:c AND profile_id=:p"
                ),
                {"c": cid, "p": pid},
            ).one()
        assert crits == {"budget"}, crits
        assert tuple(row) == (None, None, None, 0), row
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "DELETE FROM call_ranking_criteria WHERE profile_id IN "
                        "(SELECT id FROM profiles WHERE name='M0081')"
                    )
                )
                conn.execute(
                    text(
                        "DELETE FROM call_rankings WHERE profile_id IN "
                        "(SELECT id FROM profiles WHERE name='M0081')"
                    )
                )
                conn.execute(text("DELETE FROM calls WHERE external_id='M0081'"))
                conn.execute(text("DELETE FROM profiles WHERE name='M0081'"))
                conn.execute(text("DELETE FROM users WHERE email='m0081@umu.se'"))
                conn.execute(text("DELETE FROM sources WHERE slug='s0081'"))
        finally:
            engine.dispose()
