"""Verify that 0006 can be applied, rolled back, and re-applied without
schema corruption — and that v2 row deletion in the down path is explicit."""
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command


@pytest.fixture()
def alembic_cfg(pg_url):
    """Build an Alembic config rooted at the repo's alembic.ini."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_round_trip_upgrade_downgrade_upgrade(pg_url, alembic_cfg):
    """Migration is bidirectional. v1 rows survive a round trip; v2 rows
    are deleted on down (documented behavior)."""
    engine = create_engine(pg_url)
    try:
        _run_round_trip(engine, alembic_cfg)
    finally:
        engine.dispose()


def _run_round_trip(engine, alembic_cfg):
    # Ensure we're at head before seeding — when this test runs in isolation
    # (e.g. `pytest tests/test_alembic_v2_round_trip.py`) the migrated_engine
    # session fixture may not have fired yet.
    command.upgrade(alembic_cfg, "head")
    with engine.begin() as conn:
        # Clean any pre-existing data from earlier tests in this container.
        # Order matters — children before parents.
        conn.execute(text("DELETE FROM call_ranking_criteria"))
        conn.execute(text("DELETE FROM ranking_jobs"))
        conn.execute(text("DELETE FROM call_rankings"))
        conn.execute(text("DELETE FROM digest_runs"))
        conn.execute(text("DELETE FROM user_call_dismissals"))
        conn.execute(text("DELETE FROM subscriptions"))
        conn.execute(text("DELETE FROM profiles"))
        conn.execute(text("DELETE FROM users"))
        conn.execute(text("DELETE FROM calls"))

        # Seed: one user, one profile, one call, one v1 ranking, one v2 ranking.
        conn.execute(text(
            "INSERT INTO users (email, is_admin, created_at) "
            "VALUES ('rt@ki.se', false, now())"
        ))
        uid = conn.execute(text(
            "SELECT id FROM users WHERE email = 'rt@ki.se'"
        )).scalar_one()
        conn.execute(text(
            "INSERT INTO profiles (name, kind, description, structured, "
            "                       created_by, ranking_scheme, is_active, "
            "                       created_at, updated_at) "
            "VALUES ('RT', 'group', '', '{}'::jsonb, :uid, 'v1', true, now(), now())"
        ), {"uid": uid})
        pid = conn.execute(text(
            "SELECT id FROM profiles WHERE name = 'RT'"
        )).scalar_one()
        sid = conn.execute(text(
            "SELECT id FROM sources WHERE slug = 'vinnova'"
        )).scalar_one()
        conn.execute(text(
            "INSERT INTO calls (source_id, external_id, url, title, "
            "                    keywords, raw, published_at, fetched_at) "
            "VALUES (:sid, 'RT-1', 'https://x', 'RT', '{}', '{}'::jsonb, now(), now())"
        ), {"sid": sid})
        cid = conn.execute(text(
            "SELECT id FROM calls WHERE external_id = 'RT-1'"
        )).scalar_one()
        conn.execute(text(
            "INSERT INTO call_rankings (call_id, profile_id, scheme, "
            "                            aggregate_score, rationale, "
            "                            model, prompt_version, ranked_at) "
            "VALUES (:cid, :pid, 'v1', 70, 'v1', 'm', 'v1', now()), "
            "       (:cid, :pid, 'v2', 22, 'v2', 'm', 'v2.0', now())"
        ), {"cid": cid, "pid": pid})

    # Down one revision (0006 → 0005). Should drop v2 rows + new schema.
    command.downgrade(alembic_cfg, "0005")
    with engine.begin() as conn:
        # After downgrade, the `scheme` column is gone — query without it.
        # The v1 row from the seed must survive; the v2 row was deleted.
        n_rows = conn.execute(text("SELECT count(*) FROM call_rankings")).scalar_one()
        # The pre-rename column name is `score` (downgrade renamed back).
        scores = conn.execute(text("SELECT score FROM call_rankings")).scalars().all()
    assert n_rows == 1, "expected exactly the v1 row to survive the down-migration"
    assert scores == [70], "v1 row's score column should hold the seeded value"

    # Up again (0005 → 0006). Schema restored; the surviving v1 row picks up
    # scheme='v1' from the server_default.
    command.upgrade(alembic_cfg, "0006")
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT scheme, aggregate_score FROM call_rankings")).fetchall()
    assert rows == [("v1", 70)]
