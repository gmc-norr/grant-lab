"""Migration 0075: the two freshness-fingerprint columns."""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _cfg(pg_url):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    return cfg


def test_both_fingerprint_columns_exist(migrated_engine):
    insp = inspect(migrated_engine)
    assert "embedding_fingerprint" in {c["name"] for c in insp.get_columns("profiles")}
    assert "profile_fingerprint" in {c["name"] for c in insp.get_columns("call_rankings")}


def test_profiles_keep_their_vector_with_a_null_fingerprint(migrated_engine):
    """Deliberate asymmetry: profiles are re-embedded, rankings are not re-ranked.

    A paired-NULL CHECK constraint here would look symmetrical with
    ck_profiles_embedding_model_paired and would FAIL this migration.
    """
    names = {c["name"] for c in inspect(migrated_engine).get_check_constraints("profiles")}
    assert "ck_profiles_embedding_fingerprint_paired" not in names
    assert "ck_profiles_embedding_model_paired" in names


def test_existing_rankings_are_stamped_legacy(migrated_engine, pg_url):
    """The grandfathering gate: seed a ranking AT 0074, then upgrade.

    `finally` restores head unconditionally -- without it, a failure here
    leaves the shared container one revision behind and every later test
    file on this xdist worker fails on a missing column.
    """
    engine = create_engine(pg_url)
    try:
        command.downgrade(_cfg(pg_url), "0074")
        with engine.begin() as conn:
            assert "profile_fingerprint" not in {
                c["name"] for c in inspect(conn).get_columns("call_rankings")
            }
            src = conn.execute(
                text("INSERT INTO sources (name, slug) VALUES ('S0075','s0075') RETURNING id")
            ).scalar_one()
            uid = conn.execute(
                text(
                    "INSERT INTO users (email, status) "
                    "VALUES ('m0075@umu.se','active') RETURNING id"
                )
            ).scalar_one()
            pid = conn.execute(
                text(
                    "INSERT INTO profiles (name, kind, description, structured, created_by) "
                    "VALUES ('M0075','organizational_unit','d','{}'::jsonb, :u) RETURNING id"
                ),
                {"u": uid},
            ).scalar_one()
            cid = conn.execute(
                text(
                    "INSERT INTO calls (source_id, external_id, title, url) "
                    "VALUES (:s,'M0075','t','https://example.org/m0075') RETURNING id"
                ),
                {"s": src},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO call_rankings "
                    "(call_id, profile_id, aggregate_score, model, prompt_version, "
                    " raw_response) "
                    "VALUES (:c,:p,50,'gemma','v1','{}'::jsonb)"
                ),
                {"c": cid, "p": pid},
            )

        command.upgrade(_cfg(pg_url), "head")
        with engine.connect() as conn:
            got = conn.execute(
                text(
                    "SELECT profile_fingerprint FROM call_rankings "
                    "WHERE call_id=:c AND profile_id=:p"
                ),
                {"c": cid, "p": pid},
            ).scalar_one()
        assert got == "legacy", f"pre-0075 ranking was not grandfathered: {got!r}"
    finally:
        command.upgrade(_cfg(pg_url), "head")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM call_rankings"))
            conn.execute(text("DELETE FROM calls"))
            conn.execute(text("DELETE FROM profiles"))
            conn.execute(text("DELETE FROM users"))
            conn.execute(text("DELETE FROM sources WHERE slug='s0075'"))
        engine.dispose()
