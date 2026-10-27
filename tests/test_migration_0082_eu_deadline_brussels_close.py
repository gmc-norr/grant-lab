"""Migration 0082: stored eu_portal midnight deadlines become the 17:00 Brussels
close, WITHOUT re-ranking the EU feed (review 2026-10-06 H3).

The deadline is hashed into call_rank_fingerprint, so a bare UPDATE would make
every open EU pair stale. The money claim is therefore tested end to end: after
the upgrade, `enqueue_stale_pairs` must enqueue nothing for a pair that was
current before it -- and must still enqueue a pair that was already stale, so
the zero is not vacuous.

`ranking_jobs` carries the SAME three stamps on a terminally-failed job (spec
D7: the inputs it failed on), and 0082 re-stamps that table too -- but only
`call_rankings` was tested, so the mutant `for table in ("call_rankings",)` left
the suite green (review 2026-10-26). A job whose stamp was not moved describes
inputs that no longer exist, so the pair stops being suppressed and re-ranks:
the same money the call_rankings half exists to protect, on the failure side.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from alembic import command
from grant_lab.db.models import Profile
from grant_lab.profiles.fingerprint import call_rank_fingerprint, rank_fingerprint
from grant_lab.ranking.freshness import enqueue_stale_pairs
from grant_lab.sources.eu_portal import brussels_close_if_midnight
from tests.freshness_helpers import ctx_for

_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0082_eu_deadline_brussels_close.py"

_COLS = (
    "id, url, title, summary, full_text, deadline, budget_min, budget_max, currency, "
    "programme, keywords, published_at, raw, embedding_fingerprint, embedding_model"
)


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def _load_migration():
    spec = importlib.util.spec_from_file_location("m0082", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Row(dict):
    def __getattr__(self, name):
        return self[name]


def _fp(conn, cid) -> str:
    row = conn.execute(text(f"SELECT {_COLS} FROM calls WHERE id=:c"), {"c": cid}).mappings()
    return call_rank_fingerprint(_Row(row.one()))


@pytest.mark.parametrize(
    "instant",
    [
        datetime(2027, 6, 15, tzinfo=UTC),  # CEST
        datetime(2026, 11, 30, tzinfo=UTC),  # CET
        datetime(2027, 3, 28, tzinfo=UTC),  # the spring-forward day itself
        datetime(2026, 10, 25, tzinfo=UTC),  # the fall-back day itself
        datetime(2027, 6, 15, 17, 0, tzinfo=UTC),  # a real time: untouched
    ],
)
def test_the_migration_and_the_adapter_agree(instant):
    """The migration inlines its arithmetic; it must equal the adapter's."""
    m = _load_migration()
    moved = m._to_close(instant)
    assert (moved or instant) == brussels_close_if_midnight(instant)
    if moved is not None:
        assert m._to_midnight(moved) == instant, "downgrade must invert upgrade"


def test_0082_moves_eu_midnights_and_keeps_current_rankings_current(migrated_engine, pg_url):
    """`migrated_engine` forces `upgrade head` first, as in the 0080/0081 tests."""
    engine = create_engine(pg_url)
    created_eu_source = False
    ids: dict[str, int] = {}
    try:
        command.downgrade(_cfg(pg_url), "0081")
        with engine.begin() as conn:
            eu = conn.execute(
                text(
                    "INSERT INTO sources (name, slug) VALUES ('EU portal','eu_portal') "
                    "ON CONFLICT (slug) DO NOTHING RETURNING id"
                )
            ).scalar()
            created_eu_source = eu is not None
            if eu is None:
                eu = conn.execute(text("SELECT id FROM sources WHERE slug='eu_portal'")).scalar()
            other = conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S0082','s0082') RETURNING id")
            ).scalar_one()
            uid = conn.execute(
                text(
                    "INSERT INTO users (email, status) "
                    "VALUES ('m0082@umu.se','active') RETURNING id"
                )
            ).scalar_one()
            ids["profile"] = conn.execute(
                text(
                    "INSERT INTO profiles (name, kind, description, structured, created_by) "
                    "VALUES ('M0082','organizational_unit','d','{}'::jsonb, :u) RETURNING id"
                ),
                {"u": uid},
            ).scalar_one()
            ids["user"] = uid

            def call(key, source, deadline):
                ids[key] = conn.execute(
                    text(
                        "INSERT INTO calls (source_id, external_id, url, title, deadline) "
                        "VALUES (:s, :x, :u, 't', :d) RETURNING id"
                    ),
                    {"s": source, "x": f"M0082-{key}", "u": f"https://e.org/{key}", "d": deadline},
                ).scalar_one()

            call("current", eu, datetime(2027, 6, 15, tzinfo=UTC))
            call("stale", eu, datetime(2027, 6, 16, tzinfo=UTC))
            call("timed", eu, datetime(2027, 6, 17, 17, 0, tzinfo=UTC))
            call("vinnova", other, datetime(2027, 6, 18, tzinfo=UTC))

            old_fp = _fp(conn, ids["current"])
            for key, stamp in (("current", old_fp), ("stale", "an-older-fingerprint")):
                conn.execute(
                    text(
                        "INSERT INTO call_rankings (call_id, profile_id, aggregate_score, model, "
                        "prompt_version, raw_response, call_fingerprint) "
                        "VALUES (:c, :p, 40, 'gemma', 'v5.0', '{}'::jsonb, :fp)"
                    ),
                    {"c": ids[key], "p": ids["profile"], "fp": stamp},
                )
                # The failure side of the same pair. `failure_kind='service'`
                # never suppresses a re-rank, so these rows cannot perturb the
                # enqueue claim below; and `status='failed'` keeps them out of
                # uq_ranking_jobs_active_pair, which covers pending/running only.
                conn.execute(
                    text(
                        "INSERT INTO ranking_jobs (call_id, profile_id, status, attempts, "
                        "last_error, call_fingerprint, failure_kind) "
                        "VALUES (:c, :p, 'failed', 3, 'boom', :fp, 'service')"
                    ),
                    {"c": ids[key], "p": ids["profile"], "fp": stamp},
                )

        command.upgrade(_cfg(pg_url), "head")

        with engine.connect() as conn:
            deadlines = dict(
                conn.execute(
                    text("SELECT external_id, deadline FROM calls WHERE external_id LIKE 'M0082-%'")
                ).all()
            )
            new_fp = _fp(conn, ids["current"])
            stamps = dict(
                conn.execute(
                    text("SELECT call_id, call_fingerprint FROM call_rankings WHERE profile_id=:p"),
                    {"p": ids["profile"]},
                ).all()
            )
            job_stamps = dict(
                conn.execute(
                    text(
                        "SELECT call_id, call_fingerprint FROM ranking_jobs "
                        "WHERE profile_id=:p AND status='failed'"
                    ),
                    {"p": ids["profile"]},
                ).all()
            )
        # 2027-06 is CEST: 17:00 Brussels = 15:00 UTC.
        assert deadlines["M0082-current"] == datetime(2027, 6, 15, 15, 0, tzinfo=UTC)
        assert deadlines["M0082-stale"] == datetime(2027, 6, 16, 15, 0, tzinfo=UTC)
        assert deadlines["M0082-timed"] == datetime(2027, 6, 17, 17, 0, tzinfo=UTC)
        assert deadlines["M0082-vinnova"] == datetime(2027, 6, 18, tzinfo=UTC)
        assert new_fp != old_fp, "the deadline is hashed -- the re-stamp is load-bearing"
        assert stamps[ids["current"]] == new_fp
        assert stamps[ids["stale"]] == "an-older-fingerprint"
        # The second table in the migration's loop, which nothing pinned.
        assert job_stamps[ids["current"]] == new_fp, "a failed job's stamp must move too"
        assert job_stamps[ids["stale"]] == "an-older-fingerprint"

        # The money claim: stamp the other two fingerprints current, then ask
        # freshness what it would re-rank for this profile.
        ctx = ctx_for()
        with Session(engine) as session:
            profile = session.get(Profile, ids["profile"])
            session.execute(
                text(
                    "UPDATE call_rankings SET profile_fingerprint=:pf, scoring_fingerprint=:sf "
                    "WHERE profile_id=:p"
                ),
                {"pf": rank_fingerprint(profile), "sf": ctx.scoring.fingerprint(), "p": profile.id},
            )
            enqueue_stale_pairs(session, ctx=ctx, call_ids=[ids["current"], ids["stale"]])
            queued = set(
                session.execute(
                    text(
                        "SELECT call_id FROM ranking_jobs WHERE profile_id=:p AND status='pending'"
                    ),
                    {"p": profile.id},
                ).scalars()
            )
            session.rollback()
        assert queued == {ids["stale"]}, (
            "the moved-but-current pair must not re-rank; the already-stale one must"
        )

        # Round trip: downgrade restores both the midnight and the old stamp.
        command.downgrade(_cfg(pg_url), "0081")
        with engine.connect() as conn:
            back = conn.execute(
                text("SELECT deadline FROM calls WHERE id=:c"), {"c": ids["current"]}
            ).scalar_one()
            stamp = conn.execute(
                text("SELECT call_fingerprint FROM call_rankings WHERE call_id=:c"),
                {"c": ids["current"]},
            ).scalar_one()
            job_stamp = conn.execute(
                text(
                    "SELECT call_fingerprint FROM ranking_jobs WHERE call_id=:c AND status='failed'"
                ),
                {"c": ids["current"]},
            ).scalar_one()
        assert back == datetime(2027, 6, 15, tzinfo=UTC)
        assert stamp == old_fp
        assert job_stamp == old_fp
    finally:
        try:
            command.upgrade(_cfg(pg_url), "head")
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM calls WHERE external_id LIKE 'M0082-%'"))
                if "profile" in ids:
                    conn.execute(text("DELETE FROM profiles WHERE id=:p"), {"p": ids["profile"]})
                if "user" in ids:
                    conn.execute(text("DELETE FROM users WHERE id=:u"), {"u": ids["user"]})
                conn.execute(text("DELETE FROM sources WHERE slug='s0082'"))
                if created_eu_source:
                    conn.execute(text("DELETE FROM sources WHERE slug='eu_portal'"))
        finally:
            engine.dispose()
