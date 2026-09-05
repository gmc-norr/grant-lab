"""Verify that v2-era migrations (0006, 0007, 0008) can be applied, rolled
back, and re-applied without schema corruption.

Plan E (0008) drops v1 entirely. The round-trip test no longer asserts
"v1 rows survive" (no v1 schema at head) but does still verify:
  - schema round-trips cleanly across the full v2 chain;
  - 0006's documented v2-row-deletion behavior triggers on the way down,
    so any rows seeded at head (which become scheme='v2' rows after 0008's
    down restores the column with that server_default) are dropped at
    0006→0005 and the schema is left clean for re-upgrade."""

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
    """Migration chain (0006 → 0007 → 0008) is bidirectional and clean.

    With v1 entirely removed at head (Plan E / 0008), seeded rows that are
    promoted to scheme='v2' on the way down get deleted by 0006's down-
    migration. The test verifies that the schema is left intact and re-
    upgradable; row preservation is no longer in scope."""
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
        conn.execute(text("DELETE FROM call_decisions"))
        conn.execute(text("DELETE FROM subscriptions"))
        conn.execute(text("DELETE FROM profiles"))
        conn.execute(text("DELETE FROM users"))
        conn.execute(text("DELETE FROM calls"))

        # Seed: one user, one profile, one call, one ranking — all using the
        # post-0008 schema (no scheme / setwise_offset / ranking_scheme).
        conn.execute(
            text(
                "INSERT INTO users (email, is_admin, created_at) VALUES ('rt@umu.se', false, now())"
            )
        )
        uid = conn.execute(text("SELECT id FROM users WHERE email = 'rt@umu.se'")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO profiles (name, kind, description, structured, "
                "                       created_by, is_active, "
                "                       created_at, updated_at) "
                "VALUES ('RT', 'organizational_unit', '', '{}'::jsonb, :uid, true, now(), now())"
            ),
            {"uid": uid},
        )
        pid = conn.execute(text("SELECT id FROM profiles WHERE name = 'RT'")).scalar_one()
        sid = conn.execute(text("SELECT id FROM sources WHERE slug = 'vinnova'")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO calls (source_id, external_id, url, title, "
                "                    keywords, raw, published_at, fetched_at) "
                "VALUES (:sid, 'RT-1', 'https://x', 'RT', '{}', '{}'::jsonb, now(), now())"
            ),
            {"sid": sid},
        )
        cid = conn.execute(text("SELECT id FROM calls WHERE external_id = 'RT-1'")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO call_rankings (call_id, profile_id, "
                "                            aggregate_score, rationale, "
                "                            model, prompt_version, ranked_at) "
                "VALUES (:cid, :pid, 22, 'v2', 'm', 'v2.0', now())"
            ),
            {"cid": cid, "pid": pid},
        )

    # Down to 0005 — exercises 0008→0007→0006→0005. After 0008's downgrade,
    # the seeded row gets scheme='v2' (server_default); 0006's downgrade then
    # deletes all v2 rows by design.
    #
    # CRITICAL: this downgrade crosses BELOW 0031, where the `references` table
    # is created. The testcontainer DB is SESSION-scoped — shared by the whole
    # suite — and every test's `pg_session_factory` teardown TRUNCATEs all
    # tables (incl. `references`). If anything between the downgrade and the
    # re-upgrade raises (an assertion, a transient PG/embed hiccup under load, a
    # slow re-upgrade), the shared DB would be stranded below 0031 with NO
    # `references` table, and the NEXT test's TRUNCATE would cascade an opaque
    # `UndefinedTable: relation "references" does not exist` — misattributed as
    # a connection flake. The try/finally GUARANTEES the DB is restored to head
    # even on failure (mirrors the precedent in test_migration_0050_magic_link).
    try:
        command.downgrade(alembic_cfg, "0005")
        with engine.begin() as conn:
            n_rows = conn.execute(text("SELECT count(*) FROM call_rankings")).scalar_one()
        assert n_rows == 0, "0006's down-migration deletes the v2-tagged seed row"
    finally:
        # Re-upgrade to head. Schema must reach the post-0008 state cleanly.
        command.upgrade(alembic_cfg, "head")

    with engine.begin() as conn:
        cols = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'call_rankings'"
                )
            )
            .scalars()
            .all()
        )
    assert "scheme" not in cols
    assert "setwise_offset" not in cols


def test_0007_adds_pgvector_columns_and_partial_indexes(migrated_engine):
    """After alembic upgrade head, pgvector columns and partial NULL indexes exist."""
    from sqlalchemy import inspect, text

    insp = inspect(migrated_engine)

    cols_calls = {c["name"] for c in insp.get_columns("calls")}
    cols_profiles = {c["name"] for c in insp.get_columns("profiles")}
    cols_rankings = {c["name"] for c in insp.get_columns("call_rankings")}
    assert "embedding" in cols_calls
    assert "embedding_model" in cols_calls
    assert "embedding" in cols_profiles
    assert "embedding_model" in cols_profiles
    assert "embed_similarity" in cols_rankings

    # Partial indexes: querying pg_indexes is the cleanest check.
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE indexname IN ('ix_calls_embedding_null', 'ix_profiles_embedding_null')"
            )
        ).all()
    assert {r[0] for r in rows} == {
        "ix_calls_embedding_null",
        "ix_profiles_embedding_null",
    }


def test_0007_extension_vector_installed(migrated_engine):
    """pgvector extension is installed and the vector type usable."""
    from sqlalchemy import text

    with migrated_engine.connect() as conn:
        rows = conn.execute(text("SELECT extname FROM pg_extension WHERE extname='vector'")).all()
    assert len(rows) == 1


def test_0008_drops_v1_and_setwise_machinery(migrated_engine):
    """After 0008 (which is now head), dual-scheme + setwise columns are gone."""
    from sqlalchemy import inspect

    insp = inspect(migrated_engine)

    cols_calls = {c["name"] for c in insp.get_columns("call_rankings")}
    cols_profiles = {c["name"] for c in insp.get_columns("profiles")}
    cols_jobs = {c["name"] for c in insp.get_columns("ranking_jobs")}
    cols_criteria = {c["name"] for c in insp.get_columns("call_ranking_criteria")}

    assert "scheme" not in cols_calls
    assert "scheme" not in cols_jobs
    assert "scheme" not in cols_criteria
    assert "ranking_scheme" not in cols_profiles
    assert "setwise_offset" not in cols_calls
    assert "rating_expected" not in cols_criteria

    pk_rankings = insp.get_pk_constraint("call_rankings")["constrained_columns"]
    assert pk_rankings == ["call_id", "profile_id"]

    pk_criteria = insp.get_pk_constraint("call_ranking_criteria")["constrained_columns"]
    assert pk_criteria == ["call_id", "profile_id", "criterion"]

    idx_names = {ix["name"] for ix in insp.get_indexes("call_rankings")}
    assert "ix_call_rankings_profile_score" in idx_names
    assert "ix_call_rankings_profile_scheme_score" not in idx_names


def test_0007_check_constraint_rejects_orphan_embedding(pg_session_factory):
    """Setting embedding without embedding_model (or vice versa) must fail."""
    from datetime import UTC, datetime

    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from call_manager.db.models import Call, Source

    with pg_session_factory() as s:
        src = s.execute(select(Source).where(Source.slug == "vinnova")).scalar_one()
        c = Call(
            source_id=src.id,
            external_id="ck-1",
            url="https://x",
            title="T",
            published_at=datetime.now(UTC),
            embedding=[0.1] * 1024,
            embedding_model=None,  # orphan — should violate CHECK
        )
        s.add(c)
        try:
            s.commit()
        except IntegrityError as e:
            assert "ck_calls_embedding_model_paired" in str(e)
        else:
            raise AssertionError("CHECK constraint should have rejected orphan embedding")
